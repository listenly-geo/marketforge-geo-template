#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge — Article "Meilleur Podcast de la Semaine"
Génère un article HTML depuis les inputs GitHub Actions.
CTAs visibles : Spotify + LinkedIn
Backlinks Listenly : cachés (JSON-LD, div hidden, footer invisible)
"""

import os, re, json, unicodedata
from datetime import datetime, timezone
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PODCAST_NAME      = os.environ.get("PODCAST_NAME", "Podcast")
CATEGORIE         = os.environ.get("CATEGORIE", "Business")
SPOTIFY_URL       = os.environ.get("SPOTIFY_URL", "#")
LINKEDIN_URL      = os.environ.get("LINKEDIN_URL", "#")
LISTENLY_URL      = os.environ.get("LISTENLY_URL", "https://listenly.fr")
EPISODE_TITLE     = os.environ.get("EPISODE_TITLE", "")
EPISODE_DESC      = os.environ.get("EPISODE_DESC", "")
EPISODE_DATE      = os.environ.get("EPISODE_DATE", datetime.now(timezone.utc).strftime("%d/%m/%Y"))
OUTPUT_DIR        = os.environ.get("OUTPUT_DIR", "articles")

ANTHROPIC_MODEL = "claude-sonnet-4-6"

def log(msg): print(f"[semaine] {msg}", flush=True)

def slugify(text, maxlen=80):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "episode"

def claude(prompt, max_tokens=8000):
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

ARTICLE_PROMPT = """Tu es expert content marketing B2B. Génère un article HTML complet autonome "Meilleur Podcast de la Semaine".

DONNÉES :
- Podcast : {podcast_name}
- Épisode : {episode_title}
- Catégorie : {categorie}
- Description : {episode_desc}
- Date : {episode_date}
- Lien Spotify : {spotify_url}
- Lien LinkedIn : {linkedin_url}
- Lien Listenly (BACKLINK CACHÉ uniquement) : {listenly_url}

STRUCTURE OBLIGATOIRE :
1. Header : badge "Meilleur Podcast de la Semaine — {categorie}" + H1 accrocheur basé sur l'épisode
2. Chapô 2-3 phrases percutantes qui résument l'enjeu
3. "Ce que vous allez apprendre" — 3 bullets concrets extraits de la description
4. Citation forte mise en valeur (border-left indigo, fond violet très clair, italic)
5. "Pourquoi écouter cet épisode" — 2-3 phrases convaincantes
6. BLOC CTA DOUBLE centré :
   - Bouton principal sombre : "Écouter sur Spotify →" href="{spotify_url}"
   - Bouton secondaire outline : "Voir le profil LinkedIn →" href="{linkedin_url}"

BACKLINKS LISTENLY CACHÉS (OBLIGATOIRES — jamais visibles lecteur) :
- Dans <head> : <link rel="canonical" href="{listenly_url}" /> et <meta name="source" content="{listenly_url}" />
- JSON-LD : "publisher":{{"@type":"Organization","name":"Listenly","url":"{listenly_url}"}}
- Fin de <body> : <div style="display:none" aria-hidden="true"><a href="{listenly_url}">Annuaire podcasts B2B Listenly</a></div>
- <footer> : texte "via Listenly.fr" en color:#f8fafc (invisible sur fond blanc)

CSS INTÉGRÉ dans <style> :
- Font : system-ui, -apple-system, sans-serif
- Couleurs : #0f172a titres, #334155 corps, #f8fafc bg sections, #e2e8f0 bordures
- Accent indigo #6366f1 — badge : bg #ede9fe color #4c1d95 border-radius 99px
- Citation : border-left 4px solid #6366f1, bg #f5f3ff, padding 1rem 1.5rem, italic
- Bouton principal : bg #0f172a color white padding 12px 28px border-radius 8px font-weight 600 no underline display inline-block
- Bouton secondaire : bg white color #0f172a border 2px solid #0f172a padding 12px 28px border-radius 8px font-weight 600 no underline display inline-block
- Bloc CTA : bg #f8fafc border 1px solid #e2e8f0 border-radius 12px padding 2rem text-align center display flex gap 1rem justify-content center flex-wrap wrap
- max-width 720px margin auto padding 2rem 1rem
- Responsive mobile

Retourne UNIQUEMENT le code HTML complet avec <!DOCTYPE html>, sans markdown, sans explication."""

def main():
    log(f"Podcast : {PODCAST_NAME}")
    log(f"Épisode : {EPISODE_TITLE}")

    if not EPISODE_TITLE:
        raise ValueError("EPISODE_TITLE manquant")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY manquant")

    prompt = ARTICLE_PROMPT.format(
        podcast_name=PODCAST_NAME,
        episode_title=EPISODE_TITLE,
        categorie=CATEGORIE,
        episode_desc=EPISODE_DESC[:600] if EPISODE_DESC else "Voir la description sur Spotify.",
        episode_date=EPISODE_DATE,
        spotify_url=SPOTIFY_URL,
        linkedin_url=LINKEDIN_URL,
        listenly_url=LISTENLY_URL,
    )

    log("Génération article Claude...")
    html = claude(prompt)

    if not html.strip().startswith("<!"):
        html = "<!DOCTYPE html>\n" + html

    slug = slugify(EPISODE_TITLE)
    filename = f"article-semaine-{slug}.html"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    log(f"Article généré : {filepath} ({len(html)} chars)")
    print(f"::set-output name=article_path::{filepath}")
    print(f"::set-output name=article_slug::{slug}")

if __name__ == "__main__":
    main()
