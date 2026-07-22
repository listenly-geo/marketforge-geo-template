#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge — Hub d'Expertise
1 épisode → enrichit une base de connaissances vivante
RSS → Whisper → Claude → merge hub_index.json → HTML → FTP
"""

import os, re, sys, json, subprocess, unicodedata, tempfile, base64, urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY       = os.environ.get("OPENAI_API_KEY", "")
RSS_URL              = os.environ.get("RSS_URL", "")
BLOG_NAME            = os.environ.get("BLOG_NAME", "Podcast")
COMPANY_NAME         = os.environ.get("COMPANY_NAME", "")
PODCAST_URL          = os.environ.get("PODCAST_URL", "")
CONTACT_URL          = os.environ.get("CONTACT_URL", "")
LISTENLY_PODCAST_URL = os.environ.get("LISTENLY_PODCAST_URL", "https://listenly.fr")
SITE_BASE_URL        = os.environ.get("SITE_BASE_URL", "")
ACCENT_COLOR         = os.environ.get("ACCENT_COLOR", "#2e8bd6")
PODCAST_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION", "")
GITHUB_REPO          = os.environ.get("GITHUB_REPO", "")  # ex: listenly-geo/pause-rh
OUTPUT_DIR           = os.environ.get("OUTPUT_DIR", "hub")
MAX_EPISODES         = int(os.environ.get("MAX_EPISODES", "3"))

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
WHISPER_MODEL     = "whisper-1"
WHISPER_MAX_BYTES = 24 * 1024 * 1024

def log(msg): print(f"[hub] {msg}", flush=True)

def slugify(text, maxlen=80):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "page"

def claude(prompt, max_tokens=8000):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
        timeout=600,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Claude erreur {resp.status_code}: {resp.text[:300]}")
    return resp.json()["content"][0]["text"]

def fetch_rss():
    log(f"RSS : {RSS_URL}")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    r = requests.get(RSS_URL, timeout=30, headers=headers)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.find("channel")
    episodes = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or item.findtext("title") or "").strip()
        audio_url = ""
        enc = item.find("enclosure")
        if enc is not None:
            audio_url = enc.get("url", "")
        img_url = ""
        for ns in ["itunes", "media"]:
            img = item.find(f"{{{ns}}}image")
            if img is not None:
                img_url = img.get("href") or img.get("url") or img.text or ""
                if img_url: break
        episodes.append({
            "guid": guid,
            "title": (item.findtext("title") or "").strip(),
            "description": (item.findtext("description") or "").strip(),
            "pubdate": (item.findtext("pubDate") or "").strip(),
            "link": (item.findtext("link") or PODCAST_URL or "").strip(),
            "audio_url": audio_url,
            "image_url": img_url,
        })
    log(f"{len(episodes)} épisodes")
    return episodes

def download_audio(url, dest):
    log("Téléchargement audio...")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    size = os.path.getsize(dest)
    log(f"Audio : {size/1024/1024:.1f} Mo")
    return size

def compress_audio(src, size):
    if size <= WHISPER_MAX_BYTES:
        return src
    log("Compression...")
    out = src.rsplit(".", 1)[0] + "_c.mp3"
    subprocess.run(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-b:a", "32k", out],
                   check=True, capture_output=True)
    return out

def transcribe(audio_path):
    log("Transcription Whisper...")
    with open(audio_path, "rb") as f:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
            data={"model": WHISPER_MODEL, "language": "fr"},
            timeout=900,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Whisper {resp.status_code}: {resp.text[:300]}")
    text = resp.json().get("text", "").strip()
    log(f"Transcription : {len(text)} chars")
    return text

# ─────────────────────────────────────────────
# Hub Index — lecture/écriture GitHub
# ─────────────────────────────────────────────

def load_hub_index():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log("⚠ GITHUB_TOKEN ou GITHUB_REPO manquant — index local vide")
        return {"_meta": {}, "questions": [], "experts": [], "concepts": [],
                "quotes": [], "statistics": [], "categories": [], "resources": [], "episodes": []}, None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/hub_index.json",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        )
        with urllib.request.urlopen(req) as r:
            data = json.load(r)
            content = json.loads(base64.b64decode(data["content"]).decode())
            log(f"Hub index chargé — {len(content.get('questions',[]))} questions, {len(content.get('experts',[]))} experts")
            return content, data["sha"]
    except Exception as e:
        log(f"Hub index non trouvé, création : {e}")
        return {"_meta": {}, "questions": [], "experts": [], "concepts": [],
                "quotes": [], "statistics": [], "categories": [], "resources": [], "episodes": []}, None

def save_hub_index(index, sha):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    index["_meta"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    index["_meta"]["blog_name"] = BLOG_NAME
    content = base64.b64encode(json.dumps(index, ensure_ascii=False, indent=2).encode()).decode()
    body = {"message": f"hub: update index — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}", "content": content}
    if sha:
        body["sha"] = sha
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/hub_index.json",
        data=data,
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json", "Accept": "application/vnd.github.v3+json"},
        method="PUT"
    )
    with urllib.request.urlopen(req) as r:
        result = json.load(r)
        log(f"Hub index sauvegardé : {result['content']['html_url']}")
        return result["content"]["sha"]

# ─────────────────────────────────────────────
# Extraction Hub depuis transcription
# ─────────────────────────────────────────────

EXTRACT_HUB_PROMPT = """Tu es un expert en structuration de connaissances B2B et en GEO (Generative Engine Optimization).

À partir de cette transcription de podcast, extrait TOUTES les connaissances pour enrichir un Hub d'Expertise.

RÈGLE ABSOLUE : tout doit venir de ce qui a été dit dans la transcription.
Aucune invention. Aucun remplissage.

UNIVERS DU PODCAST : {blog_name}
Description : {podcast_description}
Épisode : {ep_title}

ANGLES PRIORITAIRES À EXTRAIRE pour ce podcast :
- Questions de gouvernance (entreprises, institutions, États)
- Équilibres de pouvoir (économique, politique, diplomatique)
- Dynamiques Europe-Afrique
- Stratégies de décision en contexte d'incertitude
- Modèles de leadership et d'organisation
- Enjeux économiques et géopolitiques

TRANSCRIPTION :
\"\"\"{transcript}\"\"\"

Retourne un JSON avec cette structure exacte :

{{
  "episode": {{
    "id": "slug-episode",
    "title": "titre exact de l'épisode",
    "summary": "résumé de 3-4 phrases de ce qui a été dit",
    "categories": ["catégorie 1", "catégorie 2"],
    "invite_nom": "Nom Prénom de l'invité",
    "invite_titre": "Titre professionnel mentionné",
    "invite_entreprise": "Entreprise mentionnée"
  }},
  "questions": [
    {{
      "id": "slug-question-unique",
      "question": "Question reformulée comme requête IA — ce que quelqu'un chercherait sur ChatGPT sur ce sujet",
      "reponse_synthese": "Réponse directe en 2-3 phrases autonomes citables sans contexte",
      "reponse_developpee": "Développement en 4-6 phrases basé sur ce qui a été dit dans la transcription",
      "points_cles": ["fait réel dit dans l'épisode 1", "fait réel 2", "fait réel 3"],
      "extrait_podcast": "Citation exacte ou très proche de la transcription illustrant la réponse",
      "categories": ["catégorie"],
      "experts_associes": ["slug-expert"],
      "concepts_associes": ["slug-concept"]
    }}
  ],
  "experts": [
    {{
      "id": "slug-prenom-nom",
      "nom": "Nom Prénom",
      "titre": "Titre professionnel",
      "entreprise": "Entreprise",
      "bio_courte": "1-2 phrases sur l'expertise mentionnée dans l'épisode",
      "domaines": ["gouvernance", "pouvoir", "Afrique", "Europe"],
      "citations_ids": [],
      "questions_ids": []
    }}
  ],
  "concepts": [
    {{
      "id": "slug-concept",
      "nom": "Nom du concept / terme / méthode",
      "type": "concept|methode|framework|institution|definition",
      "definition": "Définition en 1-2 phrases tirée du podcast",
      "contexte": "Comment ce concept a été utilisé dans l'épisode",
      "categories": ["catégorie"]
    }}
  ],
  "quotes": [
    {{
      "id": "slug-quote",
      "texte": "Citation mot pour mot ou très proche (15-30 mots)",
      "auteur_id": "slug-expert",
      "auteur_nom": "Nom Prénom",
      "sujet": "Sujet de la citation",
      "impact": "fort|moyen"
    }}
  ],
  "statistics": [
    {{
      "id": "slug-stat",
      "valeur": "La valeur exacte mentionnée",
      "contexte": "Ce que cette statistique illustre",
      "source": "Source mentionnée ou nom de l'invité",
      "categories": ["catégorie"]
    }}
  ]
}}

Extrais TOUT ce qui est pertinent :
- Minimum 5 questions distinctes couvrant tous les angles de l'épisode
- Tous les experts et personnalités mentionnés
- Tous les concepts, institutions, méthodes, termes clés
- Toutes les citations fortes et mémorables
- Tous les chiffres, données et statistiques

JSON uniquement, sans markdown, sans explication."""

def extract_hub_knowledge(transcript, ep):
    log("Extraction connaissances Hub...")
    prompt = EXTRACT_HUB_PROMPT.format(
        blog_name=BLOG_NAME,
        podcast_description=PODCAST_DESCRIPTION or "Podcast B2B",
        ep_title=ep["title"],
        transcript=transcript[:28000],
    )
    raw = claude(prompt, max_tokens=8000)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    idx = raw.find("{")
    if idx > 0: raw = raw[idx:]
    data = json.loads(raw)
    log(f"Extrait : {len(data.get('questions',[]))} questions, {len(data.get('experts',[]))} experts, {len(data.get('concepts',[]))} concepts, {len(data.get('quotes',[]))} citations, {len(data.get('statistics',[]))} stats")
    return data

# ─────────────────────────────────────────────
# Merge — enrichit l'index sans doublons
# ─────────────────────────────────────────────

def merge_hub(index, extracted, ep):
    ep_id = slugify(ep["title"])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Enregistrer l'épisode
    existing_ep_ids = [e["id"] for e in index.get("episodes", [])]
    if ep_id not in existing_ep_ids:
        index.setdefault("episodes", []).append({
            "id": ep_id,
            "title": ep["title"],
            "date": today,
            "summary": extracted.get("episode", {}).get("summary", ""),
            "categories": extracted.get("episode", {}).get("categories", []),
        })

    # Merge questions — enrichir si existe, créer sinon
    existing_q_ids = {q["id"]: i for i, q in enumerate(index.get("questions", []))}
    for q in extracted.get("questions", []):
        q_id = q["id"]
        if q_id in existing_q_ids:
            # Enrichir la question existante
            idx_q = existing_q_ids[q_id]
            existing = index["questions"][idx_q]
            if ep_id not in existing.get("episodes", []):
                existing.setdefault("episodes", []).append(ep_id)
            existing["last_updated"] = today
            log(f"  ↑ Question enrichie : {q_id}")
        else:
            # Créer nouvelle question
            q["episodes"] = [ep_id]
            q["created_at"] = today
            q["last_updated"] = today
            index.setdefault("questions", []).append(q)
            log(f"  + Question créée : {q_id}")

    # Merge experts
    existing_exp_ids = {e["id"]: i for i, e in enumerate(index.get("experts", []))}
    for exp in extracted.get("experts", []):
        if exp["id"] in existing_exp_ids:
            idx_e = existing_exp_ids[exp["id"]]
            if ep_id not in index["experts"][idx_e].get("episodes", []):
                index["experts"][idx_e].setdefault("episodes", []).append(ep_id)
            log(f"  ↑ Expert enrichi : {exp['id']}")
        else:
            exp["episodes"] = [ep_id]
            exp["created_at"] = today
            index.setdefault("experts", []).append(exp)
            log(f"  + Expert créé : {exp['id']}")

    # Merge concepts
    existing_c_ids = {c["id"] for c in index.get("concepts", [])}
    for c in extracted.get("concepts", []):
        if c["id"] not in existing_c_ids:
            c["episodes"] = [ep_id]
            c["created_at"] = today
            index.setdefault("concepts", []).append(c)
            log(f"  + Concept créé : {c['id']}")

    # Merge citations
    existing_qt_ids = {q["id"] for q in index.get("quotes", [])}
    for qt in extracted.get("quotes", []):
        if qt["id"] not in existing_qt_ids:
            qt["episode_id"] = ep_id
            qt["created_at"] = today
            index.setdefault("quotes", []).append(qt)

    # Merge statistiques
    existing_s_ids = {s["id"] for s in index.get("statistics", [])}
    for s in extracted.get("statistics", []):
        if s["id"] not in existing_s_ids:
            s["episode_id"] = ep_id
            s["created_at"] = today
            index.setdefault("statistics", []).append(s)

    index["_meta"]["episodes_processed"] = len(index.get("episodes", []))
    return index

# ─────────────────────────────────────────────
# Génération HTML pages Hub
# ─────────────────────────────────────────────

def build_question_page(q, index, ep):
    slug = q["id"]
    page_url = f"{SITE_BASE_URL}/hub/{slug}.html" if SITE_BASE_URL else f"{slug}.html"
    podcast_url = ep.get("link") or PODCAST_URL or "#"
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    accent = ACCENT_COLOR

    # Points clés
    points_html = "\n".join(f"<li>{p}</li>" for p in q.get("points_cles", []))

    # Extrait podcast
    extrait = q.get("extrait_podcast", "")
    extrait_html = f'<div class="quote-block">« {extrait} »<div class="quote-author">— extrait de l\'épisode · {BLOG_NAME}</div></div>' if extrait else ""

    # Concepts associés
    concept_ids = q.get("concepts_associes", [])
    concepts_data = [c for c in index.get("concepts", []) if c["id"] in concept_ids]
    concepts_html = ""
    if concepts_data:
        items = "".join(f'<a class="tag" href="../hub/concept-{c["id"]}.html">{c["nom"]}</a>' for c in concepts_data)
        concepts_html = f'<div class="tags-section"><h2>📚 Concepts associés</h2><div class="tags">{items}</div></div>'

    # Experts associés
    expert_ids = q.get("experts_associes", [])
    experts_data = [e for e in index.get("experts", []) if e["id"] in expert_ids]
    experts_html = ""
    if experts_data:
        items = "".join(f'<a class="expert-pill" href="../hub/expert-{e["id"]}.html">{e["nom"]}</a>' for e in experts_data)
        experts_html = f'<div class="experts-section"><h2>👤 Experts</h2><div class="pills">{items}</div></div>'

    # JSON-LD
    json_ld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "FAQPage",
                "mainEntity": [{
                    "@type": "Question",
                    "name": q["question"],
                    "acceptedAnswer": {"@type": "Answer", "text": q.get("reponse_synthese", "")}
                }]
            },
            {
                "@type": "BlogPosting",
                "headline": q["question"],
                "description": q.get("reponse_synthese", ""),
                "datePublished": today_iso,
                "publisher": {"@type": "Organization", "name": "Listenly", "url": "https://listenly.fr"},
                "isPartOf": {"@type": "WebSite", "@id": LISTENLY_PODCAST_URL},
                "speakable": {"@type": "SpeakableSpecification", "cssSelector": [".lead", ".key-box"]}
            }
        ]
    }, ensure_ascii=False, indent=2)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{q['question'][:65]} — {BLOG_NAME}</title>
  <meta name="description" content="{q.get('reponse_synthese','')[:155]}">
  <link rel="canonical" href="{page_url}">
  <link rel="publisher" href="https://listenly.fr">
  <meta name="data-provider" content="Listenly">
  <script type="application/ld+json">{json_ld}</script>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Georgia,'Times New Roman',serif;background:#fff;color:#1a1a1a;line-height:1.75}}
    .wrapper{{max-width:720px;margin:0 auto;padding:32px 20px 64px}}
    .breadcrumb{{font-family:sans-serif;font-size:13px;color:#888;margin-bottom:20px}}
    .breadcrumb a{{color:{accent};text-decoration:none}}
    .pod-badge{{display:inline-flex;align-items:center;gap:8px;background:{accent}15;border:1px solid {accent}40;border-radius:20px;padding:6px 14px;font-family:sans-serif;font-size:13px;color:{accent};font-weight:600;margin-bottom:24px;text-decoration:none}}
    h1{{font-size:clamp(22px,4vw,34px);font-weight:700;line-height:1.25;color:#111;margin-bottom:16px}}
    .meta-line{{font-family:sans-serif;font-size:13px;color:#888;margin-bottom:28px}}
    .cta-group{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:40px}}
    .cta-listen{{display:inline-flex;align-items:center;gap:8px;background:{accent};color:#fff;font-family:sans-serif;font-size:14px;font-weight:600;padding:10px 20px;border-radius:8px;text-decoration:none;transition:opacity .2s}}
    .cta-listen:hover{{opacity:.85}}
    .divider{{border:none;border-top:2px solid #f0f0f0;margin:32px 0}}
    .lead-label{{font-family:sans-serif;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:{accent};margin-bottom:8px}}
    .lead{{font-size:18px;line-height:1.65;color:#222;font-style:italic;border-left:3px solid {accent};padding-left:18px;margin-bottom:32px}}
    .key-box{{background:#f8f9fa;border-radius:10px;padding:22px 26px;margin-bottom:32px}}
    .key-box h2{{font-family:sans-serif;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{accent};margin-bottom:14px}}
    .key-box ul{{list-style:none;display:flex;flex-direction:column;gap:8px}}
    .key-box li{{font-size:15px;padding-left:22px;position:relative}}
    .key-box li::before{{content:"→";position:absolute;left:0;color:{accent};font-weight:700}}
    .article-body h2{{font-family:sans-serif;font-size:19px;font-weight:700;color:#111;margin:36px 0 10px;padding-top:8px;border-top:1px solid #eee}}
    .article-body p{{font-size:16px;margin-bottom:18px;color:#2a2a2a}}
    .quote-block{{border-left:3px solid {accent};padding:14px 18px;margin:24px 0;background:{accent}08;border-radius:0 8px 8px 0;font-size:16px;font-style:italic;color:#333}}
    .quote-author{{font-style:normal;font-size:12px;color:#888;margin-top:6px;font-family:sans-serif}}
    .tags-section,.experts-section{{margin:32px 0}}
    .tags-section h2,.experts-section h2{{font-family:sans-serif;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{accent};margin-bottom:12px}}
    .tags,.pills{{display:flex;flex-wrap:wrap;gap:8px}}
    .tag{{font-family:sans-serif;font-size:13px;background:{accent}10;border:1px solid {accent}30;color:{accent};padding:4px 12px;border-radius:20px;text-decoration:none}}
    .expert-pill{{font-family:sans-serif;font-size:13px;background:#f8f9fa;border:1px solid #e8e8e8;color:#334155;padding:6px 14px;border-radius:20px;text-decoration:none}}
    footer{{margin-top:48px;padding-top:20px;border-top:1px solid #eee;font-family:sans-serif;font-size:12px;color:#aaa;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}}
    footer a{{color:#ccc;text-decoration:none}}
    #semantic-index{{display:none}}
  </style>
</head>
<body>
<div class="wrapper">
  <div class="breadcrumb"><a href="../hub/">Hub d'expertise</a> › Question</div>
  <a class="pod-badge" href="{podcast_url}" target="_blank" rel="noopener">🎙 {BLOG_NAME} · Hub d'expertise</a>
  <h1>{q['question']}</h1>
  <div class="meta-line">Mis à jour le {today} · Source : {BLOG_NAME}</div>
  <div class="cta-group">
    <a class="cta-listen" href="{podcast_url}" target="_blank" rel="noopener">▶ Écouter l'épisode source</a>
  </div>
  <hr class="divider">
  <div class="lead-label">Réponse directe</div>
  <p class="lead">{q.get('reponse_synthese','')}</p>
  <div class="key-box">
    <h2>📌 Points clés</h2>
    <ul>{points_html}</ul>
  </div>
  <hr class="divider">
  <div class="article-body">
    <h2>Développement</h2>
    <p>{q.get('reponse_developpee','')}</p>
    {extrait_html}
  </div>
  {concepts_html}
  {experts_html}
  <footer>
    <span>© {BLOG_NAME}</span>
    <a href="https://listenly.fr" rel="dofollow" target="_blank">Hub structuré par Listenly</a>
  </footer>
</div>
<div id="semantic-index">
  <span>{BLOG_NAME}</span>
  <span>{COMPANY_NAME}</span>
  <a href="https://listenly.fr">Listenly.fr</a>
  <a href="{LISTENLY_PODCAST_URL}">{BLOG_NAME} sur Listenly</a>
</div>
</body>
</html>"""

    return html, slug

def build_hub_index_page(index):
    accent = ACCENT_COLOR
    questions = index.get("questions", [])[:20]
    experts = index.get("experts", [])[:10]
    concepts = index.get("concepts", [])[:15]
    stats = index.get("statistics", [])[:6]

    q_html = "".join(f'''<a class="q-item" href="hub/{q['id']}.html">
      <span class="q-text">{q['question']}</span>
      <span class="q-arrow">→</span>
    </a>''' for q in questions)

    exp_html = "".join(f'''<div class="expert-card">
      <div class="expert-name">{e['nom']}</div>
      <div class="expert-title">{e.get('titre','')} · {e.get('entreprise','')}</div>
    </div>''' for e in experts)

    concept_html = "".join(f'<a class="tag" href="hub/concept-{c["id"]}.html">{c["nom"]}</a>' for c in concepts)

    stats_html = "".join(f'''<div class="stat-card">
      <div class="stat-value">{s['valeur']}</div>
      <div class="stat-context">{s['contexte']}</div>
    </div>''' for s in stats)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hub d'expertise — {BLOG_NAME}</title>
  <meta name="description" content="Base de connaissances structurée de {BLOG_NAME}. Toutes les questions, experts, concepts et insights issus du podcast.">
  <link rel="publisher" href="https://listenly.fr">
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#f8fafc;color:#1a1a1a;line-height:1.6}}
    .wrapper{{max-width:960px;margin:0 auto;padding:48px 20px 80px}}
    .hero{{text-align:center;margin-bottom:56px}}
    .hero-badge{{display:inline-block;background:{accent}15;border:1px solid {accent}40;border-radius:20px;padding:6px 16px;font-size:13px;color:{accent};font-weight:600;margin-bottom:16px}}
    .hero h1{{font-size:clamp(28px,5vw,44px);font-weight:800;color:#0f172a;margin-bottom:12px;font-family:Georgia,serif}}
    .hero p{{font-size:17px;color:#64748b;max-width:560px;margin:0 auto}}
    .stats-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:16px;margin-bottom:56px}}
    .stat-pill{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;text-align:center}}
    .stat-pill .n{{font-size:28px;font-weight:800;color:{accent}}}
    .stat-pill .l{{font-size:12px;color:#94a3b8;margin-top:4px}}
    section{{margin-bottom:48px}}
    section h2{{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{accent};margin-bottom:20px}}
    .q-list{{display:flex;flex-direction:column;gap:8px}}
    .q-item{{display:flex;justify-content:space-between;align-items:center;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;text-decoration:none;color:#1a1a1a;transition:border-color .2s}}
    .q-item:hover{{border-color:{accent}}}
    .q-text{{font-size:15px;font-weight:500}}
    .q-arrow{{color:{accent};font-weight:700;flex-shrink:0;margin-left:12px}}
    .experts-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}}
    .expert-card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px}}
    .expert-name{{font-weight:700;font-size:15px;color:#0f172a;margin-bottom:4px}}
    .expert-title{{font-size:12px;color:#94a3b8}}
    .tags{{display:flex;flex-wrap:wrap;gap:8px}}
    .tag{{font-size:13px;background:{accent}10;border:1px solid {accent}30;color:{accent};padding:5px 14px;border-radius:20px;text-decoration:none}}
    .stats-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}}
    .stat-card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px}}
    .stat-value{{font-size:22px;font-weight:800;color:{accent};margin-bottom:6px}}
    .stat-context{{font-size:13px;color:#64748b}}
    footer{{margin-top:56px;padding-top:24px;border-top:1px solid #e2e8f0;font-size:12px;color:#aaa;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}}
    footer a{{color:#ccc;text-decoration:none}}
  </style>
</head>
<body>
<div class="wrapper">
  <div class="hero">
    <div class="hero-badge">🎙 {BLOG_NAME}</div>
    <h1>Hub d'expertise</h1>
    <p>Toute l'expertise du podcast structurée et interrogeable — mis à jour à chaque épisode.</p>
  </div>
  <div class="stats-row">
    <div class="stat-pill"><div class="n">{len(index.get('questions',[]))}</div><div class="l">Questions</div></div>
    <div class="stat-pill"><div class="n">{len(index.get('experts',[]))}</div><div class="l">Experts</div></div>
    <div class="stat-pill"><div class="n">{len(index.get('concepts',[]))}</div><div class="l">Concepts</div></div>
    <div class="stat-pill"><div class="n">{len(index.get('quotes',[]))}</div><div class="l">Citations</div></div>
    <div class="stat-pill"><div class="n">{len(index.get('episodes',[]))}</div><div class="l">Épisodes</div></div>
  </div>
  <section>
    <h2>❓ Questions & Réponses</h2>
    <div class="q-list">{q_html}</div>
  </section>
  <section>
    <h2>👤 Experts</h2>
    <div class="experts-grid">{exp_html}</div>
  </section>
  <section>
    <h2>📚 Concepts</h2>
    <div class="tags">{concept_html}</div>
  </section>
  <section>
    <h2>📊 Chiffres clés</h2>
    <div class="stats-grid">{stats_html}</div>
  </section>
  <footer>
    <span>© {BLOG_NAME} — {COMPANY_NAME}</span>
    <a href="https://listenly.fr" rel="dofollow" target="_blank">Hub structuré par Listenly</a>
  </footer>
</div>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    if not RSS_URL: raise ValueError("RSS_URL manquant")
    if not ANTHROPIC_API_KEY: raise ValueError("ANTHROPIC_API_KEY manquant")

    # Charger l'index
    hub_index, index_sha = load_hub_index()

    # Récupérer les épisodes
    episodes = fetch_rss()
    processed_ids = {e["id"] for e in hub_index.get("episodes", [])}

    # Filtrer les non traités
    to_process = [ep for ep in episodes if slugify(ep["title"]) not in processed_ids]
    to_process = to_process[:MAX_EPISODES]

    if not to_process:
        log("Tous les épisodes sont déjà traités.")
    else:
        log(f"{len(to_process)} épisode(s) à traiter")

    # Créer les dossiers
    hub_dir = os.path.join(OUTPUT_DIR)
    os.makedirs(hub_dir, exist_ok=True)

    total_pages = 0

    for ep in to_process:
        log(f"\n=== {ep['title']} ===")
        if not ep["audio_url"]:
            log("Pas d'audio — skipped")
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                mp3 = os.path.join(tmp, "episode.mp3")
                size = download_audio(ep["audio_url"], mp3)
                audio = compress_audio(mp3, size)
                transcript = transcribe(audio)

            extracted = extract_hub_knowledge(transcript, ep)
            hub_index = merge_hub(hub_index, extracted, ep)

            # Générer les pages questions
            for q in extracted.get("questions", []):
                html, slug = build_question_page(q, hub_index, ep)
                path = os.path.join(hub_dir, f"{slug}.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
                log(f"✓ hub/{slug}.html")
                total_pages += 1

        except Exception as ex:
            log(f"✗ Erreur épisode '{ep['title'][:50]}' : {ex}")
            import traceback; traceback.print_exc()

    # Générer la page d'accueil Hub
    index_html = build_hub_index_page(hub_index)
    with open(os.path.join(hub_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)
    log(f"✓ hub/index.html")
    total_pages += 1

    # Sauvegarder l'index mis à jour
    index_sha = save_hub_index(hub_index, index_sha)

    log(f"\n=== Hub terminé — {total_pages} pages générées ===")

if __name__ == "__main__":
    main()
