#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — Lead Gen automatique post-génération d'articles.
Version 2 : LinkedIn posts → Dropcontact → email signé Etienne/Listenly

Flux :
  1. Claude extrait mots-clés LinkedIn depuis les questions de l'épisode
  2. Apify scrape posts LinkedIn publics sur ces mots-clés
  3. Extrait commentateurs (Prénom + Nom + Entreprise)
  4. Dropcontact API → email pro vérifié
  5. Claude génère email signé Etienne/Listenly par contact
  6. Sauvegarde CSV dans leads/{ep_slug}/leads_YYYYMMDD.csv
  7. Notif email récap

Variables d'environnement :
  ANTHROPIC_API_KEY
  APIFY_API_KEY
  DROPCONTACT_API_KEY   (optionnel — enrichissement email)
  NOTIFY_EMAIL
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
  SITE_BASE_URL
  BLOG_NAME
"""

import os, re, json, csv, time, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import requests

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_API_KEY        = os.environ.get("APIFY_API_KEY", "")
DROPCONTACT_API_KEY  = os.environ.get("DROPCONTACT_API_KEY", "")
NOTIFY_EMAIL         = os.environ.get("NOTIFY_EMAIL", "")
SMTP_HOST            = os.environ.get("SMTP_HOST", "")
SMTP_PORT            = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER            = os.environ.get("SMTP_USER", "")
SMTP_PASS            = os.environ.get("SMTP_PASS", "")
SITE_BASE_URL        = os.environ.get("SITE_BASE_URL", "").rstrip("/")
BLOG_NAME            = os.environ.get("BLOG_NAME", "Notre Podcast")

ANTHROPIC_MODEL      = "claude-sonnet-4-6"
APIFY_ACTOR_LINKEDIN = "apimaestro/linkedin-posts-search-scraper-no-cookies"
MAX_POSTS            = 20   # posts LinkedIn à scraper
MAX_COMMENTERS       = 50   # commentateurs max à extraire
LEADS_DIR            = "leads"


def log(msg):
    print(f"[leadgen] {msg}", flush=True)


# ── Claude helper ─────────────────────────────────────────────────────────────
def claude(prompt, max_tokens=1000):
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


# ── Étape 1 : extraire mots-clés LinkedIn + persona ──────────────────────────
def extract_linkedin_keywords(ep_title, questions):
    log("Étape 1 — Extraction mots-clés LinkedIn + persona cible...")
    questions_txt = "\n".join(f"- {q['question']}" for q in questions[:10])

    prompt = f"""Tu analyses un épisode de podcast B2B pour identifier comment trouver son audience sur LinkedIn.

Podcast : {BLOG_NAME}
Titre épisode : {ep_title}
Questions traitées :
{questions_txt}

Identifie :
1. Le sujet métier en 1 phrase courte
2. L'angle utile pour un professionnel (pourquoi cet épisode les intéresse)
3. 3 mots-clés ou expressions à rechercher sur LinkedIn pour trouver des posts publics sur ce sujet (en français, courts, comme on les tape dans la recherche LinkedIn)
4. Le persona cible : titre de poste typique de la personne concernée

Réponds UNIQUEMENT en JSON valide, sans markdown :
{{
  "sujet": "...",
  "angle": "...",
  "keywords": ["mot-clé 1", "mot-clé 2", "mot-clé 3"],
  "persona": "ex: Responsable RH, DRH, Directeur Marketing..."
}}"""

    raw = claude(prompt, max_tokens=400)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ── Étape 2 : scrape LinkedIn posts via Apify ─────────────────────────────────
def scrape_linkedin_posts(keywords):
    log(f"Étape 2 — Scraping LinkedIn posts pour : {', '.join(keywords)}...")

    # On scrape chaque keyword séparément et on fusionne
    all_posts = []
    for keyword in keywords:
        log(f"  Keyword : {keyword}")
        try:
            run_resp = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_ACTOR_LINKEDIN}/runs?token={APIFY_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "keywords": [keyword],
                    "maxResults": MAX_POSTS,
                    "proxyConfiguration": {"useApifyProxy": True},
                },
                timeout=30,
            )
            run_resp.raise_for_status()
            run_id = run_resp.json()["data"]["id"]
            log(f"    Run Apify : {run_id}")

            # Polling
            for attempt in range(30):
                time.sleep(8)
                status_resp = requests.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_KEY}",
                    timeout=15,
                )
                status = status_resp.json()["data"]["status"]
                if status == "SUCCEEDED":
                    break
                elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    log(f"    Run échoué : {status}")
                    break

            # Résultats
            items_resp = requests.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={APIFY_API_KEY}&limit={MAX_POSTS}",
                timeout=30,
            )
            posts = items_resp.json()
            log(f"    {len(posts)} posts récupérés")
            all_posts.extend(posts)
            time.sleep(3)

        except Exception as e:
            log(f"    Erreur keyword '{keyword}' : {e}")

    log(f"  Total : {len(all_posts)} posts LinkedIn récupérés")
    return all_posts


# ── Étape 3 : extraire commentateurs ─────────────────────────────────────────
def extract_commenters(posts):
    """
    Extrait les auteurs des posts + commentateurs uniques.
    Retourne liste de {first_name, last_name, company, profile_url}
    """
    log("Étape 3 — Extraction des commentateurs...")
    seen = set()
    contacts = []

    for post in posts:
        # Auteur du post
        author = post.get("author") or post.get("authorName") or {}
        if isinstance(author, dict):
            fname = author.get("firstName") or author.get("first_name", "")
            lname = author.get("lastName") or author.get("last_name", "")
            company = author.get("companyName") or author.get("company", "")
            profile = author.get("profileUrl") or author.get("url", "")
        elif isinstance(author, str):
            parts = author.strip().split(" ", 1)
            fname = parts[0] if parts else ""
            lname = parts[1] if len(parts) > 1 else ""
            company = ""
            profile = post.get("authorUrl", "")

        if fname and lname:
            key = f"{fname}|{lname}|{company}"
            if key not in seen:
                seen.add(key)
                contacts.append({
                    "first_name": fname,
                    "last_name":  lname,
                    "company":    company,
                    "profile_url": profile,
                    "source":     "post_author",
                })

        # Commentateurs si disponibles
        for comment in post.get("comments", []) or []:
            commenter = comment.get("author") or comment.get("commenterName") or {}
            if isinstance(commenter, dict):
                fname = commenter.get("firstName") or commenter.get("first_name", "")
                lname = commenter.get("lastName") or commenter.get("last_name", "")
                company = commenter.get("companyName") or commenter.get("company", "")
                profile = commenter.get("profileUrl") or ""
            elif isinstance(commenter, str):
                parts = commenter.strip().split(" ", 1)
                fname = parts[0] if parts else ""
                lname = parts[1] if len(parts) > 1 else ""
                company = ""
                profile = ""

            if fname and lname:
                key = f"{fname}|{lname}|{company}"
                if key not in seen:
                    seen.add(key)
                    contacts.append({
                        "first_name": fname,
                        "last_name":  lname,
                        "company":    company,
                        "profile_url": profile,
                        "source":     "commenter",
                    })

        if len(contacts) >= MAX_COMMENTERS:
            break

    log(f"  {len(contacts)} contacts uniques extraits")
    return contacts[:MAX_COMMENTERS]


# ── Étape 4 : enrichissement Dropcontact ─────────────────────────────────────
def enrich_with_dropcontact(contacts):
    if not DROPCONTACT_API_KEY:
        log("Étape 4 — Dropcontact ignoré (clé manquante)")
        return contacts

    log(f"Étape 4 — Enrichissement Dropcontact ({len(contacts)} contacts)...")
    enriched = []

    for i, contact in enumerate(contacts):
        if not contact.get("first_name") or not contact.get("last_name"):
            enriched.append(contact)
            continue

        try:
            resp = requests.post(
                "https://api.dropcontact.com/v1/enrich/",
                headers={
                    "X-Access-Token": DROPCONTACT_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "data": [{
                        "first_name": contact["first_name"],
                        "last_name":  contact["last_name"],
                        "company":    contact.get("company", ""),
                    }],
                    "siren": False,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                data = result.get("data", [{}])[0] if result.get("data") else {}
                email = ""
                for email_entry in data.get("email", []):
                    if isinstance(email_entry, dict) and email_entry.get("email"):
                        email = email_entry["email"]
                        break
                    elif isinstance(email_entry, str):
                        email = email_entry
                        break
                contact["email"] = email
                if email:
                    log(f"  ✓ {contact['first_name']} {contact['last_name']} → {email}")
                else:
                    log(f"  - {contact['first_name']} {contact['last_name']} → pas d'email")
            else:
                log(f"  Dropcontact erreur {resp.status_code}")
                contact["email"] = ""

        except Exception as e:
            log(f"  Erreur Dropcontact pour {contact.get('first_name')} : {e}")
            contact["email"] = ""

        enriched.append(contact)
        time.sleep(1.5)  # respect rate limit

    emails_found = sum(1 for c in enriched if c.get("email"))
    log(f"  {emails_found}/{len(enriched)} emails trouvés")
    return enriched


# ── Étape 5 : générer les emails ─────────────────────────────────────────────
def generate_emails(contacts, ep_title, ep_slug, targets):
    log(f"Étape 5 — Génération de {len(contacts)} emails...")
    article_url = f"{SITE_BASE_URL}/fiche-geo-ia/{ep_slug}/index.html" if SITE_BASE_URL else f"https://listenly.fr/fiche-geo-ia/{ep_slug}/"

    leads = []
    for i, contact in enumerate(contacts):
        fname = contact.get("first_name", "")
        lname = contact.get("last_name", "")
        company = contact.get("company", "")
        email = contact.get("email", "")

        log(f"  Email {i+1}/{len(contacts)} — {fname} {lname} ({company})")

        prompt = f"""Tu es Etienne, fondateur de Listenly (listenly.fr), l'annuaire de référence des podcasts B2B français.
Tu envoies un email court et naturel — une recommandation éditoriale, pas un pitch commercial.

Contexte :
- Destinataire : {fname} {lname}{f' chez {company}' if company else ''}
- Podcast recommandé : {BLOG_NAME}
- Sujet de l'épisode : {targets['sujet']}
- Angle utile : {targets['angle']}
- Lien fiche Listenly : {article_url}
- Persona cible : {targets.get('persona', 'professionnel')}

Rédige un email en français, 5-6 lignes maximum :
- Commence par "Bonjour {fname},"
- Ton naturel, comme si tu envoyais un lien à un collègue
- Mentionne que tu diriges Listenly, l'annuaire des podcasts B2B
- Cite le sujet de l'épisode en lien avec leur profil
- Lien vers la fiche
- Termine par une question ouverte courte
- Signature : Etienne — Listenly.fr
- Jamais de "pitch", "offre", "solution", "ROI"

Format EXACT :
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
                "first_name":     fname,
                "last_name":      lname,
                "company":        company,
                "email":          email,
                "profile_url":    contact.get("profile_url", ""),
                "source":         contact.get("source", ""),
                "email_subject":  subject,
                "email_body":     body,
                "article_url":    article_url,
                "sujet":          targets["sujet"],
                "ep_title":       ep_title,
                "generated_at":   datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log(f"  ✗ Erreur email {fname} {lname} : {e}")

        time.sleep(1)

    return leads


# ── Étape 6 : sauvegarder CSV ─────────────────────────────────────────────────
def save_leads_csv(leads, ep_slug):
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = os.path.join(LEADS_DIR, ep_slug)
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"leads_{today}.csv")

    fieldnames = [
        "first_name", "last_name", "company", "email", "profile_url",
        "source", "email_subject", "email_body", "article_url",
        "sujet", "ep_title", "generated_at",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)

    log(f"CSV sauvegardé : {csv_path} ({len(leads)} leads)")
    return csv_path


# ── Étape 7 : notif email ─────────────────────────────────────────────────────
def send_notification(ep_title, ep_slug, leads, csv_path):
    if not all([NOTIFY_EMAIL, SMTP_HOST, SMTP_USER, SMTP_PASS]):
        log("Notif email ignorée (variables SMTP manquantes)")
        return

    article_url = f"{SITE_BASE_URL}/fiche-geo-ia/{ep_slug}/index.html" if SITE_BASE_URL else ""
    leads_with_email = [l for l in leads if l.get("email")]
    leads_with_body  = [l for l in leads if l.get("email_body")]

    sample_rows = ""
    for lead in leads_with_body[:5]:
        gmail_url = (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&su={requests.utils.quote(lead.get('email_subject',''))}"
            f"&to={requests.utils.quote(lead.get('email',''))}"
            f"&body={requests.utils.quote(lead.get('email_body',''))}"
        )
        sample_rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <strong>{lead['first_name']} {lead['last_name']}</strong><br>
            <small style="color:#666">{lead.get('company','')}</small>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <small style="color:#444">{lead.get('email','—')}</small><br>
            <em style="color:#888;font-size:12px">{lead.get('email_subject','')}</em>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <a href="{gmail_url}" style="background:#0E2A47;color:white;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:12px">
              Ouvrir Gmail
            </a>
          </td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto">
      <div style="background:#0E2A47;padding:20px 24px;border-radius:8px 8px 0 0">
        <h1 style="color:white;margin:0;font-size:20px">🎙 Listenly Lead Gen</h1>
        <p style="color:#D8A53C;margin:4px 0 0;font-size:14px">{BLOG_NAME} — nouvel épisode traité</p>
      </div>
      <div style="background:#f9f9f9;padding:20px 24px;border:1px solid #eee">
        <p><strong>Épisode :</strong> {ep_title}</p>
        <p><strong>Fiche Listenly :</strong> <a href="{article_url}">{article_url}</a></p>
        <table style="width:100%;border-collapse:collapse;margin:12px 0">
          <tr style="background:#eee">
            <td style="padding:6px 8px;font-size:12px;font-weight:bold">CONTACTS</td>
            <td style="padding:6px 8px;font-size:12px;font-weight:bold">EMAILS TROUVÉS</td>
            <td style="padding:6px 8px;font-size:12px;font-weight:bold">EMAILS RÉDIGÉS</td>
          </tr>
          <tr>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#0E2A47">{len(leads)}</td>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#D8A53C">{len(leads_with_email)}</td>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#2e7d32">{len(leads_with_body)}</td>
          </tr>
        </table>
        <h2 style="font-size:15px;margin-top:20px">5 premiers leads</h2>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#f0f0f0">
            <td style="padding:8px;font-size:12px;font-weight:bold">Contact</td>
            <td style="padding:8px;font-size:12px;font-weight:bold">Email</td>
            <td style="padding:8px;font-size:12px;font-weight:bold">Action</td>
          </tr>
          {sample_rows}
        </table>
        <p style="margin-top:16px;font-size:13px;color:#666">
          📎 CSV complet joint — {len(leads)} leads avec emails prérédigés
        </p>
      </div>
      <div style="background:#0E2A47;padding:12px 24px;border-radius:0 0 8px 8px;text-align:center">
        <p style="color:#aaa;margin:0;font-size:12px">Listenly.fr · MarketForge GEO Lead Gen</p>
      </div>
    </div>"""

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"🎙 {len(leads_with_body)} leads — {ep_title[:50]}"
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with open(csv_path, "rb") as f:
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
        log(f"Notif envoyée à {NOTIFY_EMAIL}")
    except Exception as e:
        log(f"Erreur notif email : {e}")


# ── Point d'entrée principal ──────────────────────────────────────────────────
def run_leadgen(ep_title, ep_slug, questions):
    if not APIFY_API_KEY:
        log("APIFY_API_KEY manquante — lead gen ignoré")
        return
    if not ANTHROPIC_API_KEY:
        log("ANTHROPIC_API_KEY manquante — lead gen ignoré")
        return

    log(f"=== Lead Gen v2 démarré : {ep_title} ===")
    try:
        # 1. Mots-clés LinkedIn
        targets = extract_linkedin_keywords(ep_title, questions)
        log(f"  Sujet : {targets['sujet']}")
        log(f"  Keywords : {', '.join(targets['keywords'])}")
        log(f"  Persona : {targets.get('persona','')}")

        # 2. Scrape LinkedIn posts
        posts = scrape_linkedin_posts(targets["keywords"])
        if not posts:
            log("Aucun post LinkedIn trouvé")
            return

        # 3. Extraire commentateurs
        contacts = extract_commenters(posts)
        if not contacts:
            log("Aucun contact extrait")
            return

        # 4. Enrichissement Dropcontact
        contacts = enrich_with_dropcontact(contacts)

        # 5. Générer emails
        leads = generate_emails(contacts, ep_title, ep_slug, targets)

        # 6. CSV
        csv_path = save_leads_csv(leads, ep_slug)

        # 7. Notif
        send_notification(ep_title, ep_slug, leads, csv_path)

        log(f"=== Lead Gen terminé : {len(leads)} leads → {csv_path} ===")

    except Exception as e:
        log(f"✗ Lead Gen erreur : {e}")
        import traceback; traceback.print_exc()


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        run_leadgen(
            ep_title=sys.argv[1],
            ep_slug=sys.argv[2],
            questions=[{"question": "Question test", "angle": "Angle test"}],
        )
    else:
        print("Usage: python leadgen.py 'Titre épisode' 'episode-slug'")
