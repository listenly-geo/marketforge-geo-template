#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — Lead Gen v3
Logique : scrape profils LinkedIn par TARGET_PERSONA défini au setup client
          → Dropcontact enrichissement email
          → Claude génère email signé Etienne/Listenly

Secrets GitHub à configurer au setup de chaque fork client :
  ANTHROPIC_API_KEY
  APIFY_API_KEY
  DROPCONTACT_API_KEY   (optionnel)
  NOTIFY_EMAIL
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
  SITE_BASE_URL
  BLOG_NAME
  TARGET_PERSONA        → ex: "DRH, Responsable RH, Chargée RH"
  TARGET_LOCATION       → ex: "France" (défaut: France)
"""

import os, re, json, csv, time, smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import requests

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_API_KEY       = os.environ.get("APIFY_API_KEY", "")
DROPCONTACT_API_KEY = os.environ.get("DROPCONTACT_API_KEY", "")
NOTIFY_EMAIL        = os.environ.get("NOTIFY_EMAIL", "")
SMTP_HOST           = os.environ.get("SMTP_HOST", "")
SMTP_PORT           = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER           = os.environ.get("SMTP_USER", "")
SMTP_PASS           = os.environ.get("SMTP_PASS", "")
SITE_BASE_URL       = os.environ.get("SITE_BASE_URL", "").rstrip("/")
BLOG_NAME           = os.environ.get("BLOG_NAME", "Notre Podcast")
TARGET_PERSONA      = os.environ.get("TARGET_PERSONA", "")
TARGET_LOCATION     = os.environ.get("TARGET_LOCATION", "France")

ANTHROPIC_MODEL     = "claude-sonnet-4-6"
APIFY_ACTOR         = "harvestapi~linkedin-profile-search"
MAX_PROFILES        = 5   # TEST — remettre 50 en prod
LEADS_DIR           = "leads"


def log(msg):
    print(f"[leadgen] {msg}", flush=True)


def claude(prompt, max_tokens=800):
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
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Claude erreur {resp.status_code}: {resp.text[:300]}")
    return resp.json()["content"][0]["text"]


# ── Étape 1 : contexte épisode ────────────────────────────────────────────────
def extract_episode_context(ep_title, questions):
    log("Étape 1 — Contexte épisode...")
    questions_txt = "\n".join(f"- {q['question']}" for q in questions[:7])
    prompt = f"""Podcast : {BLOG_NAME}
Épisode : {ep_title}
Questions traitées :
{questions_txt}

En 2 phrases max :
1. Le sujet de l'épisode (1 phrase)
2. Pourquoi c'est utile pour : {TARGET_PERSONA} (1 phrase)

Réponds UNIQUEMENT en JSON :
{{"sujet": "...", "angle": "..."}}"""

    raw = claude(prompt, max_tokens=200)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ── Étape 2 : scrape profils LinkedIn par titre de poste ──────────────────────
def scrape_linkedin_profiles(personas):
    """
    personas = liste de titres de poste ex: ["DRH", "Responsable RH"]
    Utilise harvestapi~linkedin-profile-search
    """
    log(f"Étape 2 — Scraping profils LinkedIn : {', '.join(personas)}...")
    all_profiles = []
    seen = set()

    for persona in personas:
        log(f"  Persona : {persona}")
        try:
            run_resp = requests.post(
                f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/runs?token={APIFY_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "searchQuery": persona,
                    "maxItems": MAX_PROFILES // len(personas),
                    "locations": [TARGET_LOCATION],
                    "currentJobTitle": [persona],
                    "autoQuerySegmentation": False,
                    "recentlyChangedJobs": False,
                },
                timeout=30,
            )
            run_resp.raise_for_status()
            run_id = run_resp.json()["data"]["id"]
            log(f"    Run : {run_id}")

            # Polling
            for _ in range(30):
                time.sleep(8)
                s = requests.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_KEY}",
                    timeout=15,
                ).json()["data"]["status"]
                if s == "SUCCEEDED":
                    break
                elif s in ("FAILED", "ABORTED", "TIMED-OUT"):
                    log(f"    Run échoué : {s}")
                    break

            items = requests.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={APIFY_API_KEY}&limit=50",
                timeout=30,
            ).json()

            for item in items:
                # Extraire auteur du post
                author = item.get("author") or {}
                if isinstance(author, str):
                    parts = author.strip().split(" ", 1)
                    fname = parts[0] if parts else ""
                    lname = parts[1] if len(parts) > 1 else ""
                    company = ""
                    headline = ""
                    profile_url = item.get("authorUrl", "")
                else:
                    fname = author.get("firstName") or author.get("first_name", "")
                    lname = author.get("lastName") or author.get("last_name", "")
                    company = author.get("companyName") or author.get("company", "")
                    headline = author.get("headline", "")
                    profile_url = author.get("profileUrl") or author.get("url", "") or item.get("authorUrl", "")

                if not fname or not lname:
                    continue
                key = f"{fname}|{lname}|{company}"
                if key in seen:
                    continue
                seen.add(key)
                all_profiles.append({
                    "first_name":   fname,
                    "last_name":    lname,
                    "company":      company,
                    "headline":     headline,
                    "profile_url":  profile_url,
                    "persona":      persona,
                    "email":        "",
                })

            log(f"    {len(items)} profils récupérés")
            for item in items:
                fname = item.get("firstName", "")
                lname = item.get("lastName", "")
                if not fname or not lname:
                    continue
                current_pos = item.get("currentPosition") or []
                company = current_pos[0].get("companyName", "") if isinstance(current_pos, list) and current_pos else ""
                headline = item.get("headline", "")
                profile_url = item.get("linkedinUrl") or item.get("profileUrl", "")
                key = f"{fname}|{lname}"
                if key in seen:
                    continue
                seen.add(key)
                all_profiles.append({
                    "first_name":  fname,
                    "last_name":   lname,
                    "company":     company,
                    "headline":    headline,
                    "profile_url": profile_url,
                    "persona":     persona,
                    "email":       "",
                })
            time.sleep(3)

        except Exception as e:
            log(f"    Erreur persona '{persona}' : {e}")

    log(f"  Total : {len(all_profiles)} profils uniques")
    return all_profiles[:MAX_PROFILES]


# ── Étape 3 : enrichissement Dropcontact ─────────────────────────────────────
def enrich_with_dropcontact(profiles):
    if not DROPCONTACT_API_KEY:
        log("Étape 3 — Dropcontact ignoré (clé manquante)")
        return profiles

    log(f"Étape 3 — Enrichissement Dropcontact ({len(profiles)} profils)...")
    enriched = []
    for i, p in enumerate(profiles):
        try:
            resp = requests.post(
                "https://api.dropcontact.io/b2b/v2/enrich",
                headers={
                    "X-Access-Token": DROPCONTACT_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "data": [{
                        "first_name": p["first_name"],
                        "last_name":  p["last_name"],
                        "company":    p.get("company", ""),
                    }]
                },
                timeout=60,
            )
            log(f"  DC {resp.status_code}: {resp.text[:150]}")
            if resp.status_code == 200:
                result = resp.json()
                contacts = result.get("data") or [{}]
                data = contacts[0] if contacts else {}
                for e in data.get("email", []):
                    if isinstance(e, dict) and e.get("email"):
                        p["email"] = e["email"]; break
                    elif isinstance(e, str) and "@" in e:
                        p["email"] = e; break
                if p["email"]:
                    log(f"  ✓ {p['first_name']} {p['last_name']} → {p['email']}")
        except Exception as e:
            log(f"  Erreur {p.get('first_name')} : {e}")

        enriched.append(p)
        time.sleep(1.5)

    found = sum(1 for p in enriched if p.get("email"))
    log(f"  {found}/{len(enriched)} emails trouvés")
    return enriched


# ── Étape 4 : générer les emails ─────────────────────────────────────────────
def generate_emails(profiles, ep_title, ep_slug, context):
    log(f"Étape 4 — Génération {len(profiles)} emails...")
    article_url = f"{SITE_BASE_URL}/fiche-geo-ia/{ep_slug}/index.html" if SITE_BASE_URL else f"https://listenly.fr/fiche-geo-ia/{ep_slug}/"

    leads = []
    for i, p in enumerate(profiles):
        fname   = p.get("first_name", "")
        lname   = p.get("last_name", "")
        company = p.get("company", "")
        headline = p.get("headline", p.get("persona", ""))

        log(f"  {i+1}/{len(profiles)} — {fname} {lname} ({company})")

        prompt = f"""Tu es Etienne, fondateur de Listenly (listenly.fr), l'annuaire de référence des podcasts B2B français.

Destinataire : {fname} {lname}{f', {headline}' if headline else ''}{f' chez {company}' if company else ''}
Podcast : {BLOG_NAME}
Sujet épisode : {context['sujet']}
Pourquoi ça les concerne : {context['angle']}
Lien fiche : {article_url}

Rédige un email français, 5 lignes max :
- "Bonjour {fname},"
- Tu diriges Listenly, annuaire podcasts B2B
- Tu as référencé un épisode qui colle à leur profil
- Lien
- 1 question ouverte courte
- Signature : Etienne — Listenly.fr
- Jamais : "pitch", "offre", "solution", "ROI"

Format :
OBJET: [accroche courte]
---
[corps]"""

        try:
            raw = claude(prompt, max_tokens=350)
            lines = raw.strip().split("\n")
            subject, body_lines, in_body = "", [], False
            for line in lines:
                if line.startswith("OBJET:"):
                    subject = line.replace("OBJET:", "").strip()
                elif line.strip() == "---":
                    in_body = True
                elif in_body:
                    body_lines.append(line)

            leads.append({
                **p,
                "email_subject": subject,
                "email_body":    "\n".join(body_lines).strip(),
                "article_url":   article_url,
                "sujet":         context["sujet"],
                "ep_title":      ep_title,
                "generated_at":  datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log(f"  ✗ Erreur {fname} {lname} : {e}")

        time.sleep(1)

    return leads


# ── Étape 5 : CSV ─────────────────────────────────────────────────────────────
def save_leads_csv(leads, ep_slug):
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = os.path.join(LEADS_DIR, ep_slug)
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"leads_{today}.csv")

    fieldnames = [
        "first_name", "last_name", "company", "headline", "persona",
        "email", "profile_url", "email_subject", "email_body",
        "article_url", "sujet", "ep_title", "generated_at",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)

    log(f"CSV : {csv_path} ({len(leads)} leads)")
    return csv_path


# ── Étape 6 : notif email ─────────────────────────────────────────────────────
def send_notification(ep_title, ep_slug, leads, csv_path):
    if not all([NOTIFY_EMAIL, SMTP_HOST, SMTP_USER, SMTP_PASS]):
        log("Notif ignorée (SMTP manquant)")
        return

    article_url = f"{SITE_BASE_URL}/fiche-geo-ia/{ep_slug}/index.html" if SITE_BASE_URL else ""
    with_email = [l for l in leads if l.get("email")]
    with_body  = [l for l in leads if l.get("email_body")]

    rows = ""
    for lead in with_body[:5]:
        gmail = (
            f"https://mail.google.com/mail/?view=cm&fs=1"
            f"&su={requests.utils.quote(lead.get('email_subject',''))}"
            f"&to={requests.utils.quote(lead.get('email',''))}"
            f"&body={requests.utils.quote(lead.get('email_body',''))}"
        )
        rows += f"""<tr>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <strong>{lead['first_name']} {lead['last_name']}</strong><br>
            <small>{lead.get('headline','')} · {lead.get('company','')}</small>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee">
            <small>{lead.get('email','—')}</small><br>
            <em style="font-size:12px;color:#666">{lead.get('email_subject','')}</em>
          </td>
          <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">
            <a href="{gmail}" style="background:#0E2A47;color:white;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:12px">Gmail</a>
          </td>
        </tr>"""

    html = f"""<div style="font-family:Arial,sans-serif;max-width:700px">
      <div style="background:#0E2A47;padding:20px 24px;border-radius:8px 8px 0 0">
        <h1 style="color:white;margin:0;font-size:18px">🎙 Listenly Lead Gen — {BLOG_NAME}</h1>
      </div>
      <div style="background:#f9f9f9;padding:20px 24px;border:1px solid #eee">
        <p><strong>Épisode :</strong> {ep_title}</p>
        <p><strong>Fiche :</strong> <a href="{article_url}">{article_url}</a></p>
        <p><strong>Persona ciblé :</strong> {TARGET_PERSONA}</p>
        <table style="width:100%;border-collapse:collapse;margin:12px 0">
          <tr style="background:#eee">
            <td style="padding:6px 8px;font-size:12px"><b>PROFILS</b></td>
            <td style="padding:6px 8px;font-size:12px"><b>EMAILS</b></td>
            <td style="padding:6px 8px;font-size:12px"><b>RÉDIGÉS</b></td>
          </tr>
          <tr>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#0E2A47">{len(leads)}</td>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#D8A53C">{len(with_email)}</td>
            <td style="padding:8px;font-size:22px;font-weight:bold;color:#2e7d32">{len(with_body)}</td>
          </tr>
        </table>
        <table style="width:100%;border-collapse:collapse">
          <tr style="background:#f0f0f0">
            <td style="padding:8px;font-size:12px"><b>Contact</b></td>
            <td style="padding:8px;font-size:12px"><b>Email / Objet</b></td>
            <td style="padding:8px;font-size:12px"><b>Action</b></td>
          </tr>
          {rows}
        </table>
      </div>
      <div style="background:#0E2A47;padding:10px 24px;border-radius:0 0 8px 8px;text-align:center">
        <p style="color:#aaa;margin:0;font-size:12px">Listenly.fr · MarketForge GEO Lead Gen</p>
      </div>
    </div>"""

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"🎙 {len(with_body)} leads — {ep_title[:50]}"
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    with open(csv_path, "rb") as f:
        part = MIMEBase("text", "csv")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(csv_path)}"')
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log(f"Notif envoyée → {NOTIFY_EMAIL}")
    except Exception as e:
        log(f"Erreur notif : {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def run_leadgen(ep_title, ep_slug, questions):
    if not APIFY_API_KEY:
        log("APIFY_API_KEY manquante — ignoré")
        return
    if not ANTHROPIC_API_KEY:
        log("ANTHROPIC_API_KEY manquante — ignoré")
        return
    if not TARGET_PERSONA:
        log("TARGET_PERSONA manquant — ignoré (configurer le secret GitHub)")
        return

    log(f"=== Lead Gen v3 : {ep_title} ===")
    log(f"  Persona cible : {TARGET_PERSONA}")

    try:
        # 1. Contexte épisode
        context = extract_episode_context(ep_title, questions)
        log(f"  Sujet : {context['sujet']}")

        # 2. Parse personas
        personas = [p.strip() for p in TARGET_PERSONA.split(",") if p.strip()]

        # 3. Scrape profils LinkedIn
        profiles = scrape_linkedin_profiles(personas)
        if not profiles:
            log("Aucun profil trouvé")
            return

        # 4. Dropcontact
        profiles = enrich_with_dropcontact(profiles)

        # 5. Emails
        leads = generate_emails(profiles, ep_title, ep_slug, context)

        # 6. CSV
        csv_path = save_leads_csv(leads, ep_slug)

        # 7. Notif
        send_notification(ep_title, ep_slug, leads, csv_path)

        log(f"=== Terminé : {len(leads)} leads → {csv_path} ===")

    except Exception as e:
        log(f"✗ Erreur : {e}")
        import traceback; traceback.print_exc()


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        run_leadgen(sys.argv[1], sys.argv[2], [{"question": "test"}])
    else:
        print("Usage: python leadgen.py 'Titre' 'slug'")
