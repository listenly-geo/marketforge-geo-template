#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge — Article "Meilleur Podcast de la Semaine"
Même système que generate_articles.py :
RSS → télécharge audio → Whisper transcription → Claude → HTML → FTP
CTAs visibles  : Spotify + LinkedIn
Backlinks      : Listenly caché (JSON-LD, div hidden, footer invisible)
"""

import os, re, sys, json, subprocess, unicodedata, tempfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
RSS_URL           = os.environ.get("RSS_URL", "")
PODCAST_NAME      = os.environ.get("BLOG_NAME", "Podcast")
CATEGORIE         = os.environ.get("CATEGORIE", "Business")
SPOTIFY_URL       = os.environ.get("SPOTIFY_URL", "#")
LINKEDIN_URL      = os.environ.get("LINKEDIN_URL", "#")
LISTENLY_URL      = os.environ.get("LISTENLY_PODCAST_URL", "https://listenly.fr")
EPISODE_INDEX     = int(os.environ.get("EPISODE_INDEX", "0"))
OUTPUT_DIR        = os.environ.get("OUTPUT_DIR", "articles")

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
WHISPER_MODEL     = "whisper-1"
WHISPER_MAX_BYTES = 24 * 1024 * 1024

def log(msg): print(f"[semaine] {msg}", flush=True)

def slugify(text, maxlen=80):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "episode"

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
        enc = item.find("enclosure")
        if enc is not None:
            audio_url = enc.get("url", "")
        episodes.append({"guid": guid, "title": title, "description": desc,
                         "pubdate": pubdate, "link": link, "audio_url": audio_url})
    log(f"{len(episodes)} épisodes trouvés")
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

ARTICLE_PROMPT = """Tu es expert content marketing B2B. Génère un article HTML complet "Meilleur Podcast de la Semaine" basé sur la transcription réelle.

DONNÉES :
- Podcast : {podcast_name}
- Épisode : {episode_title}
- Date : {episode_date}
- Catégorie : {categorie}
- Spotify : {spotify_url}
- LinkedIn : {linkedin_url}
- Listenly (CACHÉ) : {listenly_url}

TRANSCRIPTION :
\"\"\"{transcript}\"\"\"

STRUCTURE :
1. Header : badge "Meilleur Podcast de la Semaine — {categorie}" + H1 accrocheur tiré de la transcription
2. Chapô : 2-3 phrases percutantes sur l'enjeu réel de l'épisode
3. "Ce que vous allez apprendre" — 3 bullets concrets extraits de la transcription
4. Citation forte : phrase marquante extraite mot pour mot de la transcription (border-left #6366f1, bg #f5f3ff, italic)
5. "Pourquoi écouter cet épisode" — 2-3 phrases basées sur le contenu réel
6. BLOC CTA DOUBLE centré :
   - Bouton sombre : "Écouter sur Spotify →" href="{spotify_url}"
   - Bouton outline : "Voir le profil LinkedIn →" href="{linkedin_url}"

BACKLINKS LISTENLY CACHÉS (obligatoires, jamais visibles) :
- <head> : <link rel="canonical" href="{listenly_url}"> et <meta name="source" content="{listenly_url}">
- JSON-LD publisher : {{"@type":"Organization","name":"Listenly","url":"{listenly_url}"}}
- Fin body : <div style="display:none" aria-hidden="true"><a href="{listenly_url}">Annuaire podcasts B2B Listenly</a></div>
- Footer : "via Listenly.fr" en color:#f8fafc

CSS dans <style> : system-ui sans-serif, #0f172a titres, #334155 corps, max-width 720px margin auto, responsive mobile, badge #ede9fe/#4c1d95, bouton sombre #0f172a/blanc, bouton outline blanc/#0f172a border 2px, bloc CTA bg #f8fafc border 1px #e2e8f0 border-radius 12px padding 2rem flex gap 1rem flex-wrap.

Retourne UNIQUEMENT le HTML complet avec <!DOCTYPE html>."""

def build_article(ep, transcript):
    try:
        date_fmt = datetime.strptime(ep["pubdate"], "%a, %d %b %Y %H:%M:%S %z").strftime("%d/%m/%Y")
    except Exception:
        date_fmt = datetime.now(timezone.utc).strftime("%d/%m/%Y")

    prompt = ARTICLE_PROMPT.format(
        podcast_name=PODCAST_NAME, episode_title=ep["title"], episode_date=date_fmt,
        categorie=CATEGORIE, spotify_url=SPOTIFY_URL, linkedin_url=LINKEDIN_URL,
        listenly_url=LISTENLY_URL, transcript=transcript[:28000],
    )
    log("Génération article Claude...")
    html = claude(prompt, max_tokens=8000)
    # Supprimer les backticks markdown si Claude les ajoute
    html = html.strip()
    if html.startswith("```html"):
        html = html[7:]
    if html.startswith("```"):
        html = html[3:]
    if html.endswith("```"):
        html = html[:-3]
    html = html.strip()
    if not html.startswith("<!"):
        html = "<!DOCTYPE html>\n" + html
    return html

def main():
    if not RSS_URL:
        raise ValueError("RSS_URL manquant")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY manquant")

    episodes = fetch_rss_episodes()
    if not episodes:
        raise RuntimeError("Aucun épisode trouvé")
    if EPISODE_INDEX >= len(episodes):
        raise ValueError(f"EPISODE_INDEX {EPISODE_INDEX} hors limites ({len(episodes)} épisodes)")

    ep = episodes[EPISODE_INDEX]
    log(f"Épisode [{EPISODE_INDEX}] : {ep['title']}")

    if not ep["audio_url"]:
        raise RuntimeError("Pas d'URL audio dans le RSS")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_mp3 = os.path.join(tmp, "episode.mp3")
        size = download_audio(ep["audio_url"], tmp_mp3)
        audio_for_whisper = compress_audio_if_needed(tmp_mp3, size)
        transcript = transcribe(audio_for_whisper)

    html = build_article(ep, transcript)
    slug = slugify(ep["title"])
    filename = f"article-semaine-{slug}.html"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Article généré : {filepath} ({len(html)} chars)")

if __name__ == "__main__":
    main()
