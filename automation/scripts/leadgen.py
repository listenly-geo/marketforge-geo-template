#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — Niveau 2 : Lead Gen automatique.
Inspiré du système Radar Alternance (radar-alternance repo).

Flow :
  1. Lit le dernier article généré
  2. Claude extrait persona cible + mots-clés LinkedIn
  3. Apify harvestapi~linkedin-profile-search scrape les profils
  4. Dropcontact enrichit les emails pro
  5. Claude rédige 1 email personnalisé par prospect
  6. Brevo envoie les emails
  7. CSV sauvegardé + notif récap
"""

import os, re, sys, json, time, csv, unicodedata
from datetime import datetime, timezone
import requests

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_TOKEN         = os.environ.get("APIFY_TOKEN", "")
DROPCONTACT_API_KEY = os.environ.get("DROPCONTACT_API_KEY", "")
BREVO_API_KEY       = os.environ.get("BREVO_API_KEY", "")
EMAIL_FROM          = os.environ.get("EMAIL_FROM", "")
EMAIL_FROM_NAME     = os.environ.get("EMAIL_FROM_NAME", "Etienne — Listenly")
EMAIL_REPLY_TO      = os.environ.get("EMAIL_REPLY_TO", EMAIL_FROM)
LEADS_PER_ARTICLE   = int(os.environ.get("LEADS_PER_ARTICLE") or "50")
ARTICLES_DIR        = os.environ.get("ARTICLES_DIR", "articles")
BLOG_NAME           = os.environ.get("BLOG_NAME", "Notre Podcast")
SITE_BASE_URL       = os.environ.get("SITE_BASE_URL", "")

ANTHROPIC_MODEL = "claude-sonnet-4-6"
APIFY_ACTOR     = "harvestapi~linkedin-profile-search"

def log(msg): print(f"[leadgen] {msg}", flush=True)

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
        raise RuntimeError(f"Claude erreur {resp.status_code}: {resp.text[:200]}")
    return resp.json()["content"][0]["text"]


# ── Passe 1 : analyse article → persona ───────────────────────────────────────
def extract_persona(article_path):
    log(f"Analyse article : {article_path}")
    with open(article_path, "r", encoding="utf-8") as f:
        content = f.read()

    title_m = re.search(r'<h1[^>]*>(.*?)</h1>', content, re.DOTALL)
    title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else "Article"
    text = re.sub(r'<[^>]+>', ' ', content)
    text = re.sub(r'\s+', ' ', text).strip()[:3000]

    prompt = f"""Tu es un expert en lead generation B2B.

Analyse cet article de podcast et identifie le meilleur persona à cibler.

Titre : {title}
Contenu : {text}
Podcast : {BLOG_NAME}

Réponds UNIQUEMENT en JSON sans markdown :
{{
  "persona_titre": "Titre exact LinkedIn (ex: Directeur RH, DAF, DRH)",
  "persona_titres_alternatifs": ["variante 1", "variante 2"],
  "search_query": "Requête LinkedIn courte (ex: Directeur RH PME)",
  "accroche_sujet": "De quoi parle l'article en 8 mots max",
  "pourquoi_pertinent": "Pourquoi ce persona est intéressé (1 phrase)",
  "email_subject": "Objet email naturel 40-55 chars"
}}"""

    raw = claude(prompt, max_tokens=800)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    idx = raw.find("{")
    if idx > 0: raw = raw[idx:]
    data = json.loads(raw)
    log(f"  Persona : {data['persona_titre']}")
    log(f"  Requête : {data['search_query']}")
    return data, title


# ── Passe 2 : Apify scrape LinkedIn ───────────────────────────────────────────
def scrape_linkedin(persona):
    log(f"Scraping LinkedIn : {persona['search_query']}...")

    job_titles = [persona['persona_titre']] + persona.get('persona_titres_alternatifs', [])

    # Lancer le run Apify
    run_resp = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/runs?token={APIFY_TOKEN}",
        json={
            "searchQuery": persona['search_query'],
            "maxItems": LEADS_PER_ARTICLE + 10,
            "currentJobTitle": job_titles,
        },
        timeout=30
    )
    if run_resp.status_code not in (200, 201):
        raise RuntimeError(f"Apify erreur {run_resp.status_code}: {run_resp.text[:300]}")

    run_id = run_resp.json()["data"]["id"]
    log(f"  Run ID : {run_id}")

    # Polling toutes les 8 secondes (comme Radar Alternance)
    for i in range(40):
        time.sleep(8)
        status_resp = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_TOKEN}"
        )
        status = status_resp.json()["data"]["status"]
        log(f"  [{i+1}] {status}")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {status}")

    # Récupérer les résultats
    items_resp = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={APIFY_TOKEN}&limit={LEADS_PER_ARTICLE}"
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
        if fn and ln and company:
            contacts.append({
                "first_name": fn,
                "last_name": ln,
                "company": company,
            })

    if not contacts:
        log("  Aucun contact exploitable")
        return []

    log(f"  Envoi de {len(contacts)} contacts à Dropcontact...")
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
        log(f"  Dropcontact erreur {resp.status_code}: {resp.text[:200]}")
        return contacts

    enriched = resp.json().get("data", [])
    with_email = [c for c in enriched if c.get("email")]
    log(f"  {len(with_email)}/{len(contacts)} emails trouvés")
    return enriched


# ── Passe 4 : Claude génère les emails ────────────────────────────────────────
def generate_emails(enriched, persona, article_title, article_url):
    log(f"Génération emails...")
    results = []

    for c in enriched:
        email = c.get("email")
        if isinstance(email, list):
            email = email[0].get("email") if email else None
        if not email:
            continue

        prenom = c.get("first_name", "")
        entreprise = c.get("company", "")

        prompt = f"""Tu es {EMAIL_FROM_NAME}. Écris un email personnel court à {prenom}.

CONTEXTE :
- Tu recommandes un article issu du podcast "{BLOG_NAME}"
- L'article parle de : {persona['accroche_sujet']}
- Pourquoi c'est pertinent pour lui : {persona['pourquoi_pertinent']}
- Lien article : {article_url}
- Titre article : {article_title}

RÈGLES STRICTES :
- 4-5 lignes maximum
- Commence par "Bonjour {prenom},"
- 1 phrase de contexte naturelle (pas "j'espère que vous allez bien")
- 1 phrase sur ce que l'article apporte concrètement
- Le lien en évidence : → {article_url}
- Termine par ton prénom uniquement
- Style : email perso d'un fondateur, pas un newsletter
- ZERO pitch commercial

Réponds uniquement avec le corps de l'email."""

        try:
            body = claude(prompt, max_tokens=300)
            results.append({
                "email": email,
                "prenom": prenom,
                "nom": c.get("last_name", ""),
                "entreprise": entreprise,
                "body": body.strip(),
                "subject": persona["email_subject"],
            })
            log(f"  ✓ {prenom} {c.get('last_name','')} — {email}")
        except Exception as ex:
            log(f"  ✗ Erreur {prenom} : {ex}")

    log(f"  {len(results)} emails générés")
    return results


# ── Passe 5 : Brevo envoi ─────────────────────────────────────────────────────
def send_emails(emails):
    log(f"Envoi Brevo ({len(emails)} emails)...")
    sent = 0
    for e in emails:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender": {"name": EMAIL_FROM_NAME, "email": EMAIL_FROM},
                "to": [{"email": e["email"], "name": f"{e['prenom']} {e['nom']}".strip()}],
                "replyTo": {"email": EMAIL_REPLY_TO or EMAIL_FROM},
                "subject": e["subject"],
                "textContent": e["body"],
            },
            timeout=30
        )
        if resp.status_code in (200, 201):
            sent += 1
            log(f"  ✓ Envoyé → {e['email']}")
        else:
            log(f"  ✗ Erreur {e['email']} : {resp.status_code}")
        time.sleep(0.3)

    log(f"  {sent}/{len(emails)} emails envoyés")
    return sent


# ── CSV + notif ────────────────────────────────────────────────────────────────
def save_csv(emails, article_slug):
    os.makedirs("leads", exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"leads/{date}-{article_slug}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["prenom", "nom", "email", "entreprise", "subject"])
        writer.writeheader()
        for e in emails:
            writer.writerow({k: e.get(k, "") for k in ["prenom", "nom", "email", "entreprise", "subject"]})
    log(f"CSV : {path}")
    return path

def send_notif(sent_count, article_title, article_url, csv_path):
    if not BREVO_API_KEY or not EMAIL_FROM:
        return
    requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender": {"name": EMAIL_FROM_NAME, "email": EMAIL_FROM},
            "to": [{"email": EMAIL_FROM}],
            "subject": f"[LeadGen] {sent_count} emails envoyés — {article_title[:40]}",
            "textContent": f"Article : {article_title}\nURL : {article_url}\nEmails envoyés : {sent_count}\nCSV : {csv_path}\nDate : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
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
    }.items() if not v]
    if missing:
        log(f"ERREUR variables manquantes : {', '.join(missing)}")
        sys.exit(1)

    # Trouver le dernier article
    try:
        articles = [f for f in os.listdir(ARTICLES_DIR) if f.endswith(".html")]
    except FileNotFoundError:
        log(f"Dossier {ARTICLES_DIR} introuvable")
        sys.exit(0)

    if not articles:
        log("Aucun article trouvé")
        sys.exit(0)

    articles.sort(key=lambda f: os.path.getmtime(os.path.join(ARTICLES_DIR, f)), reverse=True)
    article_file = articles[0]
    article_path = os.path.join(ARTICLES_DIR, article_file)
    article_slug = article_file.replace(".html", "")
    article_url = f"{SITE_BASE_URL}/article-faq/{article_file}"

    log(f"Article : {article_file}")
    log(f"URL : {article_url}")

    try:
        persona, article_title = extract_persona(article_path)
        profiles = scrape_linkedin(persona)
        if not profiles:
            log("Aucun profil — arrêt")
            sys.exit(0)

        enriched = enrich_emails(profiles)
        emails = generate_emails(enriched, persona, article_title, article_url)
        if not emails:
            log("Aucun email — arrêt")
            sys.exit(0)

        csv_path = save_csv(emails, article_slug)
        if EMAIL_FROM and BREVO_API_KEY:
            sent = send_emails(emails)
            send_notif(sent, article_title, article_url, csv_path)
            log(f"Terminé. {sent} emails envoyés.")
        else:
            log(f"Terminé. {len(emails)} leads dans CSV (envoi désactivé — EMAIL_FROM manquant)")

    except Exception as ex:
        log(f"ERREUR : {ex}")
        import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
