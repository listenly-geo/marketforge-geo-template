#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — 1 article "Analyse Podcast" par épisode.
Format : analyse experte lisible + GEO maximal + CTA podcast client.
Listenly = moteur invisible (backlinks backend uniquement).
"""

import os, re, sys, json, subprocess, unicodedata
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests

OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RSS_URL           = os.environ.get("RSS_URL", "")
BLOG_NAME         = os.environ.get("BLOG_NAME", "Notre Podcast")
COMPANY_NAME      = os.environ.get("COMPANY_NAME", "")
SITE_BASE_URL     = os.environ.get("SITE_BASE_URL", "").rstrip("/")
PODCAST_URL       = os.environ.get("PODCAST_URL", "")
CONTACT_URL       = os.environ.get("CONTACT_URL", "")
ACCENT_COLOR      = os.environ.get("ACCENT_COLOR", "#2e8bd6")
MAX_NEW_PER_RUN   = int(os.environ.get("MAX_NEW_PER_RUN", "1"))
OUTPUT_DIR        = os.environ.get("OUTPUT_DIR", "articles")
LISTENLY_PODCAST_URL = os.environ.get("LISTENLY_PODCAST_URL", "https://listenly.fr")

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
WHISPER_MODEL     = "whisper-1"
WHISPER_MAX_BYTES = 24 * 1024 * 1024

REGISTRY_PATH = os.path.join(
    "automation",
    f"processed_{re.sub(r'[^a-z0-9]+', '-', BLOG_NAME.lower()).strip('-') or 'podcast'}.json"
)

def log(msg): print(f"[geo] {msg}", flush=True)

def slugify(text, maxlen=80):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "episode"

def load_registry():
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"processed": {}}
    return {"processed": {}}

def save_registry(reg):
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)

def claude(prompt, max_tokens=12000):
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
        timeout=600,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Claude erreur {resp.status_code}: {resp.text[:300]}")
    return resp.json()["content"][0]["text"]

def fetch_rss_episodes():
    log(f"Lecture RSS : {RSS_URL}")
    r = requests.get(RSS_URL, timeout=30, headers={"User-Agent": "MarketForgeGEO/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS invalide")
    episodes = []
    for item in channel.findall("item"):
        title     = (item.findtext("title") or "").strip()
        guid      = (item.findtext("guid") or title).strip()
        desc      = (item.findtext("description") or "").strip()
        pubdate   = (item.findtext("pubDate") or "").strip()
        link      = (item.findtext("link") or "").strip()
        audio_url = ""
        image_url = ""
        enc = item.find("enclosure")
        if enc is not None:
            audio_url = enc.get("url", "")
        # Image épisode
        img = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
        if img is not None:
            image_url = img.get("href", "")
        episodes.append({"guid": guid, "title": title, "description": desc,
                         "pubdate": pubdate, "link": link, "audio_url": audio_url,
                         "image_url": image_url})
    log(f"{len(episodes)} épisodes")
    return episodes

def download_audio(url, dest):
    log("Téléchargement audio...")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "MarketForgeGEO/1.0"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    size = os.path.getsize(dest)
    log(f"Audio : {size/1024/1024:.1f} Mo")
    return size

def compress_audio_if_needed(src, size):
    if size <= WHISPER_MAX_BYTES:
        return src
    log("Compression ffmpeg...")
    out = src.rsplit(".", 1)[0] + "_compressed.mp3"
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
        raise RuntimeError(f"Whisper erreur {resp.status_code}: {resp.text[:300]}")
    text = resp.json().get("text", "").strip()
    log(f"Transcription : {len(text)} chars")
    return text

EXTRACT_PROMPT = """Tu es un expert GEO (Generative Engine Optimization) pour podcasts B2B.

À partir de la transcription, identifie LA meilleure question/réponse pour un article expert.

CRITÈRES : question la plus recherchée par les professionnels, réponse la plus forte et unique de l'invité, valeur standalone maximale.

Podcast : {blog_name} | Épisode : {ep_title} | Entreprise : {company}

TRANSCRIPTION :
\"\"\"{transcript}\"\"\"

JSON uniquement, sans markdown :
{{
  "episode_slug": "slug-kebab-case",
  "invite_prenom": "Prénom",
  "invite_nom": "Nom",
  "invite_titre": "Titre professionnel",
  "invite_entreprise": "Entreprise",
  "question": "La question exacte — titre H1",
  "slug": "slug-question",
  "reponse_directe": "2-3 phrases répondant directement, autonomes, citables par une IA",
  "points_cles": ["fait autonome 1", "fait autonome 2", "fait autonome 3", "fait autonome 4"],
  "sections": [
    {{"titre": "Sous-angle 1", "contenu": "2-3 phrases tirées de la transcription"}},
    {{"titre": "Sous-angle 2", "contenu": "2-3 phrases"}},
    {{"titre": "Sous-angle 3", "contenu": "2-3 phrases"}},
    {{"titre": "Ce que ça change concrètement", "contenu": "2-3 phrases actionnables"}}
  ],
  "faq": [
    {{"q": "Question connexe 1", "r": "Réponse 2 phrases"}},
    {{"q": "Question connexe 2", "r": "Réponse 2 phrases"}},
    {{"q": "Question connexe 3", "r": "Réponse 2 phrases"}},
    {{"q": "Question connexe 4", "r": "Réponse 2 phrases"}}
  ],
  "citation_forte": "Citation exacte de l'invité (15-25 mots)",
  "persona_cible": "Le professionnel exactement visé",
  "meta_title": "Titre SEO 50-65 chars",
  "meta_description": "Description 140-155 chars"
}}"""

def extract_qr(transcript, ep):
    log("Extraction Q&R...")
    prompt = EXTRACT_PROMPT.format(
        blog_name=BLOG_NAME,
        ep_title=ep["title"],
        company=COMPANY_NAME or BLOG_NAME,
        transcript=transcript[:28000],
    )
    raw = claude(prompt, max_tokens=3000)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    idx = raw.find("{")
    if idx > 0: raw = raw[idx:]
    data = json.loads(raw)
    log(f"Question : {data['question'][:70]}")
    return data

ARTICLE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{meta_title}</title>
  <meta name="description" content="{meta_description}">
  <link rel="canonical" href="{page_url}">
  <meta name="author" content="{blog_name}">
  <meta property="og:title" content="{meta_title}">
  <meta property="og:description" content="{meta_description}">
  <meta property="og:url" content="{page_url}">
  {og_image}
  <!-- GEO Backend -->
  <link rel="publisher" href="https://listenly.fr">
  <meta name="data-provider" content="Listenly">
  <script type="application/ld+json">{json_ld}</script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Georgia, 'Times New Roman', serif; background: #fff; color: #1a1a1a; line-height: 1.75; }}
    .wrapper {{ max-width: 720px; margin: 0 auto; padding: 32px 20px 64px; }}

    /* Header */
    .pod-badge {{ display: inline-flex; align-items: center; gap: 8px; background: {accent}15; border: 1px solid {accent}40; border-radius: 20px; padding: 6px 14px; font-family: sans-serif; font-size: 13px; color: {accent}; font-weight: 600; margin-bottom: 24px; text-decoration: none; }}
    .pod-badge:hover {{ background: {accent}25; }}
    h1 {{ font-size: clamp(24px, 4vw, 36px); font-weight: 700; line-height: 1.25; color: #111; margin-bottom: 16px; }}
    .meta-line {{ font-family: sans-serif; font-size: 14px; color: #666; margin-bottom: 28px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
    .meta-line strong {{ color: #333; }}
    .sep {{ color: #ccc; }}

    /* CTA bouton */
    .cta-listen {{ display: inline-flex; align-items: center; gap: 8px; background: {accent}; color: #fff; font-family: sans-serif; font-size: 15px; font-weight: 600; padding: 12px 24px; border-radius: 8px; text-decoration: none; margin-bottom: 40px; transition: opacity .2s; }}
    .cta-listen:hover {{ opacity: .85; }}

    /* Séparateur */
    .divider {{ border: none; border-top: 2px solid #f0f0f0; margin: 36px 0; }}

    /* Réponse directe */
    .lead-label {{ font-family: sans-serif; font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: {accent}; margin-bottom: 10px; }}
    .lead {{ font-size: 19px; line-height: 1.65; color: #222; font-style: italic; border-left: 3px solid {accent}; padding-left: 20px; margin-bottom: 36px; }}

    /* Points clés */
    .key-box {{ background: #f8f9fa; border-radius: 10px; padding: 24px 28px; margin-bottom: 36px; }}
    .key-box h2 {{ font-family: sans-serif; font-size: 13px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: {accent}; margin-bottom: 16px; }}
    .key-box ul {{ list-style: none; display: flex; flex-direction: column; gap: 10px; }}
    .key-box li {{ font-size: 16px; padding-left: 24px; position: relative; }}
    .key-box li::before {{ content: "→"; position: absolute; left: 0; color: {accent}; font-weight: 700; }}

    /* Corps article */
    .article-body h2 {{ font-family: sans-serif; font-size: 20px; font-weight: 700; color: #111; margin: 40px 0 12px; padding-top: 8px; border-top: 1px solid #eee; }}
    .article-body p {{ font-size: 17px; margin-bottom: 20px; color: #2a2a2a; }}
    .quote-block {{ border-left: 3px solid {accent}; padding: 16px 20px; margin: 28px 0; background: {accent}08; border-radius: 0 8px 8px 0; font-size: 17px; font-style: italic; color: #333; }}
    .quote-author {{ font-style: normal; font-size: 13px; color: #888; margin-top: 8px; font-family: sans-serif; }}

    /* CTA discret milieu */
    .cta-mid {{ text-align: center; margin: 40px 0; }}
    .cta-mid a {{ font-family: sans-serif; font-size: 14px; color: {accent}; text-decoration: none; border-bottom: 1px solid {accent}40; padding-bottom: 2px; }}
    .cta-mid a:hover {{ border-color: {accent}; }}

    /* FAQ */
    .faq-section {{ margin-top: 48px; }}
    .faq-section h2 {{ font-family: sans-serif; font-size: 13px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: {accent}; margin-bottom: 24px; }}
    .faq-item {{ margin-bottom: 24px; }}
    .faq-item h3 {{ font-size: 17px; font-weight: 600; color: #111; margin-bottom: 8px; }}
    .faq-item p {{ font-size: 16px; color: #444; }}

    /* Card bas — mail-ready */
    .episode-card {{ border: 1px solid #e8e8e8; border-radius: 12px; overflow: hidden; margin-top: 56px; display: flex; gap: 0; }}
    .episode-card img {{ width: 140px; min-height: 140px; object-fit: cover; flex-shrink: 0; }}
    .episode-card-body {{ padding: 20px 24px; flex: 1; display: flex; flex-direction: column; justify-content: space-between; }}
    .episode-card-body h3 {{ font-size: 16px; font-weight: 700; color: #111; margin-bottom: 6px; line-height: 1.4; }}
    .episode-card-body p {{ font-family: sans-serif; font-size: 13px; color: #666; margin-bottom: 16px; }}
    .card-actions {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }}
    .card-listen {{ font-family: sans-serif; font-size: 13px; font-weight: 700; color: {accent}; text-decoration: none; }}
    .card-listen:hover {{ opacity: .75; }}
    .card-contact {{ font-family: sans-serif; font-size: 13px; background: {accent}; color: #fff; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; }}
    .card-contact:hover {{ opacity: .85; }}

    /* Footer */
    footer {{ margin-top: 56px; padding-top: 24px; border-top: 1px solid #eee; font-family: sans-serif; font-size: 12px; color: #aaa; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
    footer a {{ color: #ccc; text-decoration: none; }}
    footer a:hover {{ color: #aaa; }}

    /* Semantic index caché */
    #semantic-index {{ display: none; }}

    @media (max-width: 540px) {{
      .episode-card {{ flex-direction: column; }}
      .episode-card img {{ width: 100%; height: 180px; }}
    }}
  </style>
</head>
<body>
<div class="wrapper">

  <!-- Header -->
  <a class="pod-badge" href="{podcast_url}" target="_blank" rel="noopener">🎙 {blog_name} · Analyse d'épisode</a>
  <h1>{question}</h1>
  <div class="meta-line">
    <span>Avec <strong>{invite_prenom} {invite_nom}</strong></span>
    <span class="sep">·</span>
    <span>{invite_titre} chez {invite_entreprise}</span>
    <span class="sep">·</span>
    <span>{date_pub}</span>
    <span class="sep">·</span>
    <span>⏱ 4 min de lecture</span>
  </div>
  <a class="cta-listen" href="{podcast_url}" target="_blank" rel="noopener">▶ Écouter l'épisode complet</a>

  <hr class="divider">

  <!-- Réponse directe -->
  <div class="lead-label">Ce que dit {invite_prenom} en résumé</div>
  <p class="lead">{reponse_directe}</p>

  <!-- Points clés -->
  <div class="key-box">
    <h2>📌 Les points clés</h2>
    <ul>{points_cles_html}</ul>
  </div>

  <hr class="divider">

  <!-- Corps -->
  <div class="article-body">
    {sections_html}
    <div class="quote-block">
      « {citation_forte} »
      <div class="quote-author">— {invite_prenom} {invite_nom}, {invite_titre}</div>
    </div>
  </div>

  <!-- CTA milieu discret -->
  <div class="cta-mid">
    <a href="{podcast_url}" target="_blank" rel="noopener">→ Découvrir tous les épisodes de {blog_name}</a>
  </div>

  <hr class="divider">

  <!-- FAQ -->
  <div class="faq-section">
    <h2>❓ On répond aussi à ces questions</h2>
    {faq_html}
  </div>

  <!-- Card bas mail-ready -->
  <div class="episode-card">
    {episode_img_html}
    <div class="episode-card-body">
      <div>
        <h3>{ep_title}</h3>
        <p>{invite_prenom} {invite_nom} · {blog_name}</p>
      </div>
      <div class="card-actions">
        <a class="card-listen" href="{podcast_url}" target="_blank" rel="noopener">▶ Écouter maintenant →</a>
        {contact_btn}
      </div>
    </div>
  </div>

  <!-- Footer -->
  <footer>
    <span>© {blog_name} — {company}</span>
    <a href="https://listenly.fr" rel="dofollow" target="_blank">Analyse structurée par Listenly</a>
  </footer>

</div>

<!-- GEO Backend invisible -->
<div id="semantic-index">
  <span class="entity">{blog_name}</span>
  <span class="entity">{invite_prenom} {invite_nom}</span>
  <span class="entity">{invite_entreprise}</span>
  <span class="concept">{persona_cible}</span>
  <span class="publisher">Listenly.fr</span>
  <span class="isPartOf">{listenly_podcast_url}</span>
</div>

</body>
</html>"""

def build_article(qr, ep):
    ep_slug = slugify(qr.get("episode_slug") or ep["title"])
    page_url = f"{SITE_BASE_URL}/article-faq/{ep_slug}.html" if SITE_BASE_URL else f"{ep_slug}.html"
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    podcast_url = ep.get("link") or PODCAST_URL or "#"
    contact_url = CONTACT_URL or ""

    # Points clés HTML
    points_cles_html = "\n".join(f"<li>{p}</li>" for p in qr.get("points_cles", []))

    # Sections HTML
    sections_html = ""
    for s in qr.get("sections", []):
        sections_html += f"<h2>{s['titre']}</h2>\n<p>{s['contenu']}</p>\n"

    # FAQ HTML
    faq_html = ""
    for item in qr.get("faq", []):
        faq_html += f"""<div class="faq-item"><h3>{item['q']}</h3><p>{item['r']}</p></div>\n"""

    # Image épisode
    img_url = ep.get("image_url", "")
    episode_img_html = f'<img src="{img_url}" alt="{ep["title"]}" loading="lazy">' if img_url else ""
    og_image = f'<meta property="og:image" content="{img_url}">' if img_url else ""

    # Bouton contact
    contact_btn = f'<a class="card-contact" href="{contact_url}" target="_blank" rel="noopener">📞 Nous contacter</a>' if contact_url else ""

    # JSON-LD
    faq_ld = [{"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["r"]}} for f in qr.get("faq", [])]
    json_ld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "BlogPosting",
                "headline": qr["question"],
                "description": qr.get("meta_description", ""),
                "datePublished": today_iso,
                "dateModified": today_iso,
                "mainEntityOfPage": {"@type": "WebPage", "@id": page_url},
                "author": {"@type": "Person", "name": f"{qr['invite_prenom']} {qr['invite_nom']}", "jobTitle": qr.get("invite_titre", "")},
                "publisher": {"@type": "Organization", "name": "Listenly", "url": "https://listenly.fr"},
                "isPartOf": {"@type": "WebSite", "@id": LISTENLY_PODCAST_URL},
                "about": {"@type": "Person", "name": f"{qr['invite_prenom']} {qr['invite_nom']}"},
                "speakable": {"@type": "SpeakableSpecification", "cssSelector": [".lead", ".key-box"]},
            },
            {"@type": "FAQPage", "mainEntity": faq_ld},
            {"@type": "Person", "name": f"{qr['invite_prenom']} {qr['invite_nom']}", "jobTitle": qr.get("invite_titre", ""), "worksFor": {"@type": "Organization", "name": qr.get("invite_entreprise", "")}},
            {"@type": "PodcastSeries", "name": BLOG_NAME, "url": podcast_url},
        ]
    }, ensure_ascii=False, indent=2)

    html = ARTICLE_TEMPLATE.format(
        meta_title=qr.get("meta_title", qr["question"][:65]),
        meta_description=qr.get("meta_description", ""),
        page_url=page_url,
        blog_name=BLOG_NAME,
        company=COMPANY_NAME or BLOG_NAME,
        accent=ACCENT_COLOR,
        podcast_url=podcast_url,
        question=qr["question"],
        invite_prenom=qr["invite_prenom"],
        invite_nom=qr["invite_nom"],
        invite_titre=qr.get("invite_titre", ""),
        invite_entreprise=qr.get("invite_entreprise", ""),
        date_pub=today,
        reponse_directe=qr["reponse_directe"],
        points_cles_html=points_cles_html,
        sections_html=sections_html,
        citation_forte=qr.get("citation_forte", ""),
        faq_html=faq_html,
        ep_title=ep["title"],
        episode_img_html=episode_img_html,
        og_image=og_image,
        contact_btn=contact_btn,
        persona_cible=qr.get("persona_cible", ""),
        listenly_podcast_url=LISTENLY_PODCAST_URL,
        json_ld=json_ld,
    )
    return html, ep_slug

def main():
    missing = [k for k, v in {"OPENAI_API_KEY": OPENAI_API_KEY, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY, "RSS_URL": RSS_URL}.items() if not v]
    if missing:
        log(f"ERREUR variables manquantes : {', '.join(missing)}")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    reg = load_registry()
    episodes = fetch_rss_episodes()
    new_eps = [ep for ep in episodes if ep["guid"] not in reg["processed"]][:MAX_NEW_PER_RUN]
    log(f"{len(new_eps)} nouveaux épisodes à traiter")

    created = 0
    for ep in new_eps:
        if not ep["audio_url"]:
            log(f"Pas d'audio — ignoré : {ep['title']}")
            reg["processed"][ep["guid"]] = {"skipped": "no_audio", "title": ep["title"]}
            continue
        tmp_mp3 = None
        audio_for_whisper = None
        try:
            tmp_mp3 = f"/tmp/{slugify(ep['title'])}.mp3"
            size = download_audio(ep["audio_url"], tmp_mp3)
            audio_for_whisper = compress_audio_if_needed(tmp_mp3, size)
            transcript = transcribe(audio_for_whisper)

            transcript_dir = os.path.join(OUTPUT_DIR, "_transcriptions")
            os.makedirs(transcript_dir, exist_ok=True)
            with open(os.path.join(transcript_dir, f"{slugify(ep['title'])}.txt"), "w", encoding="utf-8") as tf:
                tf.write(f"TITRE: {ep['title']}\nDATE: {ep.get('pubdate','')}\n{'='*60}\n\n{transcript}")

            qr = extract_qr(transcript, ep)
            html_out, ep_slug = build_article(qr, ep)

            filename = f"{ep_slug}.html"
            with open(os.path.join(OUTPUT_DIR, filename), "w", encoding="utf-8") as f:
                f.write(html_out)
            log(f"✓ {filename}")

            reg["processed"][ep["guid"]] = {
                "title": ep["title"], "ep_slug": ep_slug,
                "invite": f"{qr['invite_prenom']} {qr['invite_nom']}",
                "question": qr["question"], "filename": filename,
                "url": f"{SITE_BASE_URL}/article-faq/{ep_slug}.html",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            created += 1

        except Exception as ex:
            log(f"✗ Échec '{ep['title']}' : {ex}")
            import traceback; traceback.print_exc()
        finally:
            for p in [tmp_mp3, audio_for_whisper]:
                if p and os.path.exists(p) and p.startswith("/tmp"):
                    try: os.remove(p)
                    except Exception: pass

    save_registry(reg)
    log(f"Terminé. {created} article(s) créé(s).")

if __name__ == "__main__":
    main()
