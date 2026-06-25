#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — Lead Gen automatique post-génération d'articles.

Flux :
  1. Reçoit les données de l'épisode traité (titre, questions, slug)
  2. Claude extrait le sujet métier + 3 titres de poste cibles
  3. Apify scrape Indeed France sur ces postes
  4. Filtre les offres avec email public
  5. Claude génère un email signé Etienne/Listenly par offre
  6. Sauvegarde CSV dans leads/{ep_slug}/leads_YYYYMMDD.csv
  7. Envoie une notif email récap via SMTP

Variables d'environnement requises :
  ANTHROPIC_API_KEY
  APIFY_API_KEY
  NOTIFY_EMAIL        — destinataire de la notif (ex: etienne@listenly.fr)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS  — pour l'envoi de notif
  SITE_BASE_URL       — pour construire les liens articles
  BLOG_NAME           — nom du podcast client
"""

import os, re, json, csv, time, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_API_KEY     = os.environ.get("APIFY_API_KEY", "")
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "")
SMTP_PORT         = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")
SITE_BASE_URL     = os.environ.get("SITE_BASE_URL", "").rstrip("/")
BLOG_NAME         = os.environ.get("BLOG_NAME", "Notre Podcast")

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
APIFY_ACTOR       = "misceres/indeed-scraper"
MAX_JOBS          = 50   # offres max à scraper par run
LEADS_DIR         = "leads"


def log(msg):
    print(f"[leadgen] {msg}", flush=True)


# ── Claude helper ─────────────────────────────────────────────────────────────
def claude(prompt, max_tokens=2000):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Claude erreur {resp.status_code}: {resp.text[:300]}")
    return resp.json()["content"][0]["text"]


# ── Étape 1 : extraire sujet + postes cibles ─────────────────────────────────
def extract_targets(ep_title, questions):
    """
    À partir du titre d'épisode et des questions générées,
    Claude identifie le sujet métier et 3 titres de poste cibles.
    """
    log("Étape 1 — Extraction sujet + postes cibles...")
    questions_txt = "\n".join(f"- {q['question']}" for q in questions[:10])
    prompt = f"""Tu analyses un épisode de podcast B2B pour identifier qui devrait recevoir une recommandation de cet épisode.

Podcast : {BLOG_NAME}
Titre épisode : {ep_title}
Questions traitées dans l'épisode :
{questions_txt}

Identifie :
1. Le sujet métier en 1 phrase courte (ex: "la féminisation de la fonction RH")
2. L'angle utile pour un professionnel (ex: "comprendre pourquoi la fonction RH manque de légitimité stratégique")
3. 3 titres de poste en français qu'on trouve sur Indeed, dont les personnes bénéficieraient directement de cet épisode

Réponds UNIQUEMENT en JSON valide, sans markdown :
{{
  "sujet": "...",
  "angle": "...",
  "postes": ["Poste 1", "Poste 2", "Poste 3"]
}}"""

    raw = claude(prompt, max_tokens=500)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ── Étape 2 : scrape Indeed via Apify ────────────────────────────────────────
def scrape_indeed(postes):
    """
    Lance un run Apify misceres/indeed-scraper sur les postes cibles.
    Retourne la liste des offres avec email public si disponible.
    """
    log(f"Étape 2 — Scraping Indeed pour : {', '.join(postes)}...")

    # Lancer le run
    run_resp = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/runs?token={APIFY_API_KEY}",
        headers={"Content-Type": "application/json"},
        json={
            "position": " OR ".join(f'"{p}"' for p in postes),
            "country": "FR",
            "location": "France",
            "maxItems": MAX_JOBS,
            "parseCompanyDetails": False,
            "saveOnlyUniqueItems": True,
            "followApplyRedirects": False,
        },
        timeout=30,
    )
    run_resp.raise_for_status()
    run_id = run_resp.json()["data"]["id"]
    log(f"  Run Apify lancé : {run_id}")

    # Polling jusqu'à SUCCEEDED
    for attempt in range(40):
        time.sleep(8)
        status_resp = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_KEY}",
            timeout=15,
        )
        status = status_resp.json()["data"]["status"]
        log(f"  Statut run : {status} (tentative {attempt+1})")
        if status == "SUCCEEDED":
            break
        elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Run Apify échoué : {status}")

    # Récupérer les résultats
    items_resp = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={APIFY_API_KEY}&limit={MAX_JOBS}",
        timeout=30,
    )
    items = items_resp.json()
    log(f"  {len(items)} offres récupérées")

    # Filtrer : garder uniquement celles avec email public OU conserver toutes
    # (Indeed expose rarement les emails — on garde tout pour la génération email)
    jobs = []
    for item in items:
        if not item.get("company") or not item.get("positionName"):
            continue
        jobs.append({
            "company":      item.get("company", ""),
            "position":     item.get("positionName", ""),
            "location":     item.get("location", ""),
            "job_url":      item.get("externalApplyLink") or item.get("url", ""),
            "email":        item.get("companyEmail", "") or "",  # si dispo
            "description":  (item.get("description", "") or "")[:500],
        })

    log(f"  {len(jobs)} offres valides après filtre")
    return jobs


# ── Étape 3 : générer les emails ─────────────────────────────────────────────
def generate_emails(jobs, ep_title, ep_slug, targets):
    """
    Pour chaque offre, Claude génère un email court signé Etienne/Listenly.
    Retourne la liste des leads enrichis.
    """
    log(f"Étape 3 — Génération de {len(jobs)} emails...")

    # URL de l'article index pour cet épisode
    article_url = f"{SITE_BASE_URL}/fiche-geo-ia/{ep_slug}/index.html" if SITE_BASE_URL else f"https://listenly.fr/fiche-geo-ia/{ep_slug}/"

    leads = []
    for i, job in enumerate(jobs):
        log(f"  Email {i+1}/{len(jobs)} — {job['company']} ({job['position']})")

        prompt = f"""Tu es Etienne, fondateur de Listenly (listenly.fr), l'annuaire de référence des podcasts B2B français.
Tu envoies un email court et naturel — une recommandation éditoriale, pas un pitch commercial.

Contexte :
- Entreprise : {job['company']}
- Poste recruté / profil : {job['position']}
- Localisation : {job['location']}
- Podcast recommandé : {BLOG_NAME}
- Sujet de l'épisode : {targets['sujet']}
- Angle utile : {targets['angle']}
- Lien fiche Listenly : {article_url}

Rédige un email en français, 5-6 lignes maximum :
- Ton : naturel, comme si tu envoyais un lien à un collègue
- Pas de "pitch", "offre", "solution", "ROI"
- Mentionne que tu diriges Listenly, l'annuaire des podcasts B2B
- Cite le sujet de l'épisode en lien avec leur contexte métier
- Termine par une question ouverte courte (1 ligne)
- Signature : Etienne — Listenly.fr

Format EXACT (respecter les séparateurs) :
OBJET: [sujet accrocheur 1 ligne]
---
[corps de l'email]"""

        try:
            raw = claude(prompt, max_tokens=400)
            lines = raw.strip().split("\n")
            subject = ""
            body_lines = []
            in_body = False
            for line in lines:
                if line.startswith("OBJET:"):
                    subject = line.replace("OBJET:", "").strip()
                elif line.strip() == "---":
                    in_body = True
                elif in_body:
                    body_lines.append(line)
            body = "\n".join(body_lines).strip()

            leads.append({
                **job,
                "email_subject": subject,
                "email_body":    body,
                "article_url":   article_url,
                "sujet":         targets["sujet"],
                "ep_title":      ep_title,
                "generated_at":  datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log(f"  ✗ Erreur génération email pour {job['company']}: {e}")
            leads.append({**job, "email_subject": "", "email_body": "", "article_url": article_url})

        # Pause légère pour éviter rate limit
        time.sleep(1)

    return leads


# ── Étape 4 : sauvegarder CSV ─────────────────────────────────────────────────
def save_leads_csv(leads, ep_slug):
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = os.path.join(LEADS_DIR, ep_slug)
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"leads_{today}.csv")

    fieldnames = [
        "company", "position", "location", "email",
        "job_url", "email_subject", "email_body", "article_url",
        "sujet", "ep_title", "generated_at",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)

    log(f"CSV sauvegardé : {csv_path} ({len(leads)} leads)")
    return csv_path


# ── Étape 5 : notif email ─────────────────────────────────────────────────────
def send_notification(ep_title, ep_slug, leads, csv_path):
    if not all([NOTIFY_EMAIL, SMTP_HOST, SMTP_USER, SMTP_PASS]):
        log("Notif email ignorée (variables SMTP manquantes)")
        return

    article_url = f"{SITE_BASE_URL}/fiche-geo-ia/{ep_slug}/index.html" if SITE_BASE_URL else ""
    leads_with_email = [l for l in leads if l.get("email")]
    leads_with_body  = [l for l in leads if l.get("email_body")]

    # Construire le récap HTML
    sample_rows = ""
    for lead in leads_with_body[:5]:
        gmail_url = (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&su={requests.utils.quote(lead['email_subject'])}"
            f"&to={requests.utils.quote(lead.get('email',''))}"
            f"&body={requests.utils.quote(lead['email_body'])}"
        )
        sample_rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee"><strong>{lead['company']}</strong><br>
              <small style="color:#666">{lead['position']} · {lead['location']}</small></td>
          <td style="padding:8px;border-bottom:1px solid #eee">
              <em style="color:#444">{lead['email_subject']}</em><br>
              <small style="color:#888">{lead['email_body'][:120]}...</small></td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
              <a href="{gmail_url}" style="background:#0E2A47;color:white;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:12px">Ouvrir Gmail</a>
          </td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto">
      <div style="background:#0E2A47;padding:20px 24px;border-radius:8px 8px 0 0">
        <h1 style="color:white;margin:0;font-size:20px">🎙 Listenly Lead Gen</h1>
        <p style="color:#D8A53C;margin:4px 0 0;font-size:14px">Nouvel épisode traité → leads générés</p>
      </div>
      <div style="background:#f9f9f9;padding:20px 24px;border:1px solid #eee">
        <p><strong>Épisode :</strong> {ep_title}</p>
        <p><strong>Article Listenly :</strong> <a href="{article_url}">{article_url}</a></p>
        <table style="width:100%;border-collapse:collapse;margin-top:8px">
          <tr style="background:#eee">
            <td style="padding:6px 8px;font-size:12px;font-weight:bold">LEADS TOTAL</td>
            <td style="padding:6px 8px;font-size:12px;font-weight:bold">AVEC EMAIL</td>
            <td style="padding:6px 8px;font-size:12px;font-weight:bold">EMAILS RÉDIGÉS</td>
          </tr>
          <tr>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#0E2A47">{len(leads)}</td>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#D8A53C">{len(leads_with_email)}</td>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#2e7d32">{len(leads_with_body)}</td>
          </tr>
        </table>
        <h2 style="font-size:15px;margin-top:20px">5 premiers leads — prêts à envoyer</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#f0f0f0">
            <td style="padding:8px;font-size:12px;font-weight:bold">Entreprise</td>
            <td style="padding:8px;font-size:12px;font-weight:bold">Email prérédigé</td>
            <td style="padding:8px;font-size:12px;font-weight:bold">Action</td>
          </tr>
          {sample_rows}
        </table>
        <p style="margin-top:16px;font-size:13px;color:#666">
          📎 CSV complet joint — {len(leads)} leads avec emails prérédigés
        </p>
      </div>
      <div style="background:#0E2A47;padding:12px 24px;border-radius:0 0 8px 8px;text-align:center">
        <p style="color:#aaa;margin:0;font-size:12px">Listenly.fr · MarketForge GEO Lead Gen · automatique</p>
      </div>
    </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎙 {len(leads_with_body)} leads générés — {ep_title[:50]}"
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    # Joindre le CSV
    with open(csv_path, "rb") as f:
        from email.mime.base import MIMEBase
        from email import encoders
        part = MIMEBase("text", "csv")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(csv_path)}"')
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log(f"Notif email envoyée à {NOTIFY_EMAIL}")
    except Exception as e:
        log(f"Erreur envoi notif email : {e}")


# ── Point d'entrée principal ──────────────────────────────────────────────────
def run_leadgen(ep_title, ep_slug, questions):
    """
    Appelé depuis generate_articles.py après chaque épisode traité.
    questions = liste de dicts {question, slug, angle, reponse_brute, ...}
    """
    if not APIFY_API_KEY:
        log("APIFY_API_KEY manquante — lead gen ignoré")
        return
    if not ANTHROPIC_API_KEY:
        log("ANTHROPIC_API_KEY manquante — lead gen ignoré")
        return

    log(f"=== Lead Gen démarré pour : {ep_title} ===")
    try:
        # 1. Extraire sujet + postes cibles
        targets = extract_targets(ep_title, questions)
        log(f"  Sujet : {targets['sujet']}")
        log(f"  Postes cibles : {', '.join(targets['postes'])}")

        # 2. Scraper Indeed
        jobs = scrape_indeed(targets["postes"])
        if not jobs:
            log("Aucune offre trouvée — lead gen terminé sans résultat")
            return

        # 3. Générer les emails
        leads = generate_emails(jobs, ep_title, ep_slug, targets)

        # 4. Sauvegarder CSV
        csv_path = save_leads_csv(leads, ep_slug)

        # 5. Notif email
        send_notification(ep_title, ep_slug, leads, csv_path)

        log(f"=== Lead Gen terminé : {len(leads)} leads → {csv_path} ===")

    except Exception as e:
        log(f"✗ Lead Gen erreur : {e}")
        import traceback; traceback.print_exc()


if __name__ == "__main__":
    # Test standalone
    import sys
    if len(sys.argv) >= 3:
        run_leadgen(
            ep_title=sys.argv[1],
            ep_slug=sys.argv[2],
            questions=[{"question": "Question test", "angle": "Angle test"}],
        )
    else:
        print("Usage: python leadgen.py 'Titre épisode' 'episode-slug'")
