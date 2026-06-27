#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — Niveau 2 : Lead Gen automatique après génération article.

Flow :
  1. Lit le dernier article généré
  2. Claude extrait persona cible + mots-clés LinkedIn
  3. Apify scrape profils LinkedIn correspondants
  4. Dropcontact enrichit les emails pro
  5. Claude rédige 1 email personnalisé par prospect
  6. Brevo envoie les emails
  7. CSV sauvegardé + notif email

Variables d'environnement :
  ANTHROPIC_API_KEY, APIFY_TOKEN, DROPCONTACT_API_KEY
  BREVO_API_KEY, EMAIL_FROM, EMAIL_FROM_NAME, EMAIL_REPLY_TO
  LINKEDIN_LOCATIONS, LEADS_PER_ARTICLE
  ARTICLES_DIR, REGISTRY_PATH
"""

import os, re, sys, json, time, csv, unicodedata
from datetime import datetime, timezone
import requests

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_TOKEN         = os.environ.get("APIFY_TOKEN", "")
DROPCONTACT_API_KEY = os.environ.get("DROPCONTACT_API_KEY", "")
BREVO_API_KEY       = os.environ.get("BREVO_API_KEY", "")
EMAIL_FROM          = os.environ.get("EMAIL_FROM", "")
EMAIL_FROM_NAME     = os.environ.get("EMAIL_FROM_NAME", "Listenly")
EMAIL_REPLY_TO      = os.environ.get("EMAIL_REPLY_TO", EMAIL_FROM)
LINKEDIN_LOCATIONS  = json.loads(os.environ.get("LINKEDIN_LOCATIONS", '["France"]'))
LEADS_PER_ARTICLE   = int(os.environ.get("LEADS_PER_ARTICLE", "50"))
ARTICLES_DIR        = os.environ.get("ARTICLES_DIR", "articles")
BLOG_NAME           = os.environ.get("BLOG_NAME", "Notre Podcast")
SITE_BASE_URL       = os.environ.get("SITE_BASE_URL", "")

ANTHROPIC_MODEL = "claude-sonnet-4-6"
APIFY_ACTOR     = "harvestapi~linkedin-profile-search"

def log(msg): print(f"[leadgen] {msg}", flush=True)

def slugify(text, maxlen=80):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "article"

def claude(prompt, max_tokens=4000):
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
        raise RuntimeError(f"Claude erreur {resp.status_code}: {resp.text[:200]}")
    return resp.json()["content"][0]["text"]


# ── Passe 1 : analyse article → persona + mots-clés ───────────────────────────
PERSONA_PROMPT = """Tu es un expert en lead generation B2B.

Analyse cet article de podcast et identifie le persona professionnel le plus pertinent à cibler pour lui envoyer cet article.

ARTICLE :
Titre : {title}
Contenu résumé : {content}
Podcast : {blog_name}

Réponds UNIQUEMENT en JSON sans markdown :
{{
  "persona_titre": "Titre exact LinkedIn (ex: Directeur RH, DAF, DRH, PDG PME...)",
  "persona_titres_alternatifs": ["variante 1", "variante 2"],
  "secteur": "Secteur d'activité ciblé",
  "accroche_sujet": "En 10 mots max : de quoi parle l'article (ex: féminisation des RH en entreprise)",
  "pourquoi_pertinent": "En 1 phrase : pourquoi ce persona est intéressé par ce sujet",
  "search_query": "Requête LinkedIn exacte pour trouver ces profils (ex: Directeur RH PME France)",
  "email_subject": "Objet email accrocheur 40-55 chars (style naturel, pas commercial)"
}}"""

def extract_persona(article_path):
    log(f"Analyse article : {article_path}")
    with open(article_path, "r", encoding="utf-8") as f:
        content = f.read()

    import re as _re
    title_m = _re.search(r'<h1[^>]*>(.*?)</h1>', content, _re.DOTALL)
    title = _re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else "Article"
    text = _re.sub(r'<[^>]+>', ' ', content)
    text = _re.sub(r'\s+', ' ', text).strip()[:3000]

    prompt = PERSONA_PROMPT.format(
        title=title,
        content=text,
        blog_name=BLOG_NAME
    )
    raw = claude(prompt, max_tokens=1000)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)
    log(f"  Persona : {data['persona_titre']} | Secteur : {data['secteur']}")
    return data, title


# ── Passe 2 : Apify scrape LinkedIn ───────────────────────────────────────────
def scrape_linkedin(persona):
    log(f"Scraping LinkedIn : {persona['search_query']}...")
    job_titles = [persona['persona_titre']] + persona.get('persona_titres_alternatifs', [])

    run_resp = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/runs",
        params={"token": APIFY_TOKEN},
        json={
            "searchQuery": persona['search_query'],
            "maxItems": max(LEADS_PER_ARTICLE + 10, 20),
            "locations": LINKEDIN_LOCATIONS,
            "currentJobTitle": job_titles,
        },
        timeout=30
    )
    if run_resp.status_code not in (200, 201):
        raise RuntimeError(f"Apify erreur {run_resp.status_code}: {run_resp.text[:200]}")

    run_id = run_resp.json()["data"]["id"]
    log(f"  Run Apify : {run_id}")

    for i in range(30):
        time.sleep(8)
        status_resp = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}",
            params={"token": APIFY_TOKEN}
        )
        status = status_resp.json()["data"]["status"]
        log(f"  Statut [{i+1}] : {status}")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {status}")

    items_resp = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
        params={"token": APIFY_TOKEN, "limit": LEADS_PER_ARTICLE}
    )
    profiles = items_resp.json()
    log(f"  {len(profiles)} profils récupérés")
    return profiles


# ── Passe 3 : Dropcontact enrichissement ──────────────────────────────────────
def enrich_emails(profiles):
    log("Enrichissement Dropcontact...")
    contacts = []
    for p in profiles:
        fn = p.get("firstName") or p.get("first_name") or ""
        ln = p.get("lastName") or p.get("last_name") or ""
        company = p.get("companyName") or p.get("company") or ""
        linkedin = p.get("linkedinUrl") or p.get("url") or ""
        if fn and ln and company:
            contacts.append({
                "first_name": fn,
                "last_name": ln,
                "company": company,
                "linkedin": linkedin,
            })

    if not contacts:
        log("  Aucun contact exploitable")
        return []

    resp = requests.post(
        "https://api.dropcontact.com/v1/enrich/",
        headers={
            "X-Access-Token": DROPCONTACT_API_KEY,
            "Content-Type": "application/json"
        },
        json={"data": contacts, "siren": False, "language": "fr"},
        timeout=60
    )
    if resp.status_code != 200:
        log(f"  Dropcontact erreur {resp.status_code}")
        return contacts

    enriched = resp.json().get("data", [])
    with_email = [c for c in enriched if c.get("email") or (isinstance(c.get("email"), list) and c["email"])]
    log(f"  {len(with_email)}/{len(contacts)} emails trouvés")
    return enriched


# ── Passe 4 : Claude génère les emails ────────────────────────────────────────
EMAIL_PROMPT = """Tu es {from_name}, tu envoies un email personnel à un professionnel.

CONTEXTE :
- Tu as trouvé un article issu du podcast "{blog_name}" qui correspond exactement à son contexte
- Ton rôle : recommander naturellement cet article, comme si tu l'avais trouvé en faisant ta veille
- Ton ton : humain, direct, pas commercial, style email perso

DESTINATAIRE :
- Prénom : {prenom}
- Titre : {titre}
- Entreprise : {entreprise}

ARTICLE À RECOMMANDER :
- Titre : {article_title}
- Sujet : {accroche_sujet}
- Pourquoi pertinent pour lui : {pourquoi_pertinent}
- Lien : {article_url}

RÈGLES :
- 4-6 lignes maximum
- Commence par "Bonjour {prenom},"
- 1 seule phrase de contexte sur pourquoi tu lui envoies ça
- 1 phrase sur ce que l'article apporte
- Le lien bien visible (→ Lire l'analyse)
- Termine par ton prénom uniquement
- ZERO pitch commercial, ZERO "j'espère que vous allez bien"
- Style : comme l'exemple Thomas Martin dans l'image

Réponds UNIQUEMENT avec le corps de l'email, rien d'autre."""

def generate_emails(enriched, persona, article_title, article_url, from_name):
    log(f"Génération emails ({len(enriched)} contacts)...")
    results = []
    for c in enriched:
        email = c.get("email")
        if isinstance(email, list):
            email = email[0].get("email") if email else None
        if not email:
            continue

        prenom = c.get("first_name", "")
        titre = c.get("job_title") or persona["persona_titre"]
        entreprise = c.get("company", "")

        try:
            body = claude(EMAIL_PROMPT.format(
                from_name=from_name,
                blog_name=BLOG_NAME,
                prenom=prenom,
                titre=titre,
                entreprise=entreprise,
                article_title=article_title,
                accroche_sujet=persona["accroche_sujet"],
                pourquoi_pertinent=persona["pourquoi_pertinent"],
                article_url=article_url,
            ), max_tokens=400)

            results.append({
                "email": email,
                "prenom": prenom,
                "nom": c.get("last_name", ""),
                "entreprise": entreprise,
                "titre": titre,
                "body": body.strip(),
                "subject": persona["email_subject"],
            })
        except Exception as ex:
            log(f"  Erreur email {prenom} {c.get('last_name','')} : {ex}")

    log(f"  {len(results)} emails générés")
    return results


# ── Passe 5 : Brevo envoi ─────────────────────────────────────────────────────
def send_emails(emails, from_name, from_email, reply_to):
    log(f"Envoi Brevo ({len(emails)} emails)...")
    sent = 0
    for e in emails:
        payload = {
            "sender": {"name": from_name, "email": from_email},
            "to": [{"email": e["email"], "name": f"{e['prenom']} {e['nom']}".strip()}],
            "replyTo": {"email": reply_to},
            "subject": e["subject"],
            "textContent": e["body"],
        }
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        if resp.status_code in (200, 201):
            sent += 1
        else:
            log(f"  Erreur envoi {e['email']} : {resp.status_code} {resp.text[:100]}")
        time.sleep(0.3)

    log(f"  {sent}/{len(emails)} emails envoyés")
    return sent


# ── Sauvegarde CSV ─────────────────────────────────────────────────────────────
def save_csv(emails, article_slug):
    os.makedirs("leads", exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"leads/{date}-{article_slug}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["prenom", "nom", "email", "entreprise", "titre", "subject"])
        writer.writeheader()
        for e in emails:
            writer.writerow({k: e.get(k, "") for k in ["prenom", "nom", "email", "entreprise", "titre", "subject"]})
    log(f"CSV sauvegardé : {path}")
    return path


# ── Notif email récap ──────────────────────────────────────────────────────────
def send_notif(sent_count, article_title, article_url, csv_path, from_email, from_name):
    if not BREVO_API_KEY or not from_email:
        return
    body = f"""Récapitulatif leadgen MarketForge GEO

Article : {article_title}
URL : {article_url}
Emails envoyés : {sent_count}
CSV : {csv_path}
Date : {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC
"""
    requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender": {"name": from_name, "email": from_email},
            "to": [{"email": from_email}],
            "subject": f"[LeadGen] {sent_count} emails envoyés — {article_title[:40]}",
            "textContent": body,
        },
        timeout=30
    )
    log("Notif récap envoyée")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "APIFY_TOKEN": APIFY_TOKEN,
        "DROPCONTACT_API_KEY": DROPCONTACT_API_KEY,
        "BREVO_API_KEY": BREVO_API_KEY,
        "EMAIL_FROM": EMAIL_FROM,
    }.items() if not v]
    if missing:
        log(f"ERREUR variables manquantes : {', '.join(missing)}")
        sys.exit(1)

    # Trouver le dernier article généré
    articles = [f for f in os.listdir(ARTICLES_DIR)
                if f.endswith(".html") and not f.startswith("_")]
    if not articles:
        log("Aucun article trouvé dans articles/")
        sys.exit(0)

    # Prendre le plus récent
    articles.sort(key=lambda f: os.path.getmtime(os.path.join(ARTICLES_DIR, f)), reverse=True)
    article_file = articles[0]
    article_path = os.path.join(ARTICLES_DIR, article_file)
    article_slug = article_file.replace(".html", "")
    article_url = f"{SITE_BASE_URL}/article-faq/{article_file}" if SITE_BASE_URL else article_file

    log(f"Article cible : {article_file}")

    try:
        # Passe 1 — persona
        persona, article_title = extract_persona(article_path)

        # Passe 2 — scraping
        profiles = scrape_linkedin(persona)
        if not profiles:
            log("Aucun profil trouvé — arrêt")
            sys.exit(0)

        # Passe 3 — enrichissement
        enriched = enrich_emails(profiles)

        # Passe 4 — génération emails
        from_name = EMAIL_FROM_NAME
        emails = generate_emails(enriched, persona, article_title, article_url, from_name)
        if not emails:
            log("Aucun email généré — arrêt")
            sys.exit(0)

        # Passe 5 — envoi
        sent = send_emails(emails, from_name, EMAIL_FROM, EMAIL_REPLY_TO)

        # Sauvegarde + notif
        csv_path = save_csv(emails, article_slug)
        send_notif(sent, article_title, article_url, csv_path, EMAIL_FROM, from_name)

        log(f"Terminé. {sent} emails envoyés.")

    except Exception as ex:
        log(f"ERREUR : {ex}")
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
