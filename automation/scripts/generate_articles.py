#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO — 1 article expert FAQ GEO par épisode podcast.

Flux :
  1. Lit le flux RSS
  2. Détecte les nouveaux épisodes (registre JSON)
  3. Télécharge le MP3 + transcription Whisper
  4. Passe 1 — Claude identifie la meilleure Q&R de l'épisode
  5. Passe 2 — Claude génère 1 article GEO HTML expert
  6. Écrit le fichier dans OUTPUT_DIR/{episode-slug}.html

Variables d'environnement :
  OPENAI_API_KEY, ANTHROPIC_API_KEY, RSS_URL
  BLOG_NAME, COMPANY_NAME, BLOG_IMAGE_URL, SITE_BASE_URL
  AUTHOR_NAME, ACCENT_COLOR, MAX_NEW_PER_RUN
"""

import os, re, sys, json, subprocess, unicodedata
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests

# ── Config ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RSS_URL           = os.environ.get("RSS_URL", "")
BLOG_NAME         = os.environ.get("BLOG_NAME", "Notre Podcast")
COMPANY_NAME      = os.environ.get("COMPANY_NAME", "")
BLOG_IMAGE_URL    = os.environ.get("BLOG_IMAGE_URL", "")
SITE_BASE_URL     = os.environ.get("SITE_BASE_URL", "").rstrip("/")
AUTHOR_NAME       = os.environ.get("AUTHOR_NAME", "La rédaction")
ACCENT_COLOR      = os.environ.get("ACCENT_COLOR", "#2e8bd6")
MAX_NEW_PER_RUN   = int(os.environ.get("MAX_NEW_PER_RUN", "1"))
OUTPUT_DIR        = os.environ.get("OUTPUT_DIR", "articles")

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
WHISPER_MODEL     = "whisper-1"
WHISPER_MAX_BYTES = 24 * 1024 * 1024

REGISTRY_PATH = os.path.join(
    "automation",
    f"processed_{re.sub(r'[^a-z0-9]+', '-', BLOG_NAME.lower()).strip('-') or 'podcast'}.json"
)


# ── Utilitaires ───────────────────────────────────────────────────────────────
def log(msg):
    print(f"[geo] {msg}", flush=True)

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


# ── RSS ────────────────────────────────────────────────────────────────────────
def fetch_rss_episodes():
    log(f"Lecture RSS : {RSS_URL}")
    r = requests.get(RSS_URL, timeout=30, headers={"User-Agent": "MarketForgeGEO/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS invalide : pas de <channel>")
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
    log(f"{len(episodes)} épisodes dans le flux")
    return episodes


# ── Audio ──────────────────────────────────────────────────────────────────────
def download_audio(url, dest):
    log(f"Téléchargement audio...")
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
    log("Compression ffmpeg (mono 32k)...")
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
    log(f"Transcription OK : {len(text)} caractères")
    return text


# ── Passe 1 : extraction meilleure Q&R ────────────────────────────────────────
EXTRACT_PROMPT = """Tu es un expert en création de contenu GEO (Generative Engine Optimization) pour podcasts B2B.

À partir de la transcription d'un épisode du podcast "{blog_name}", identifie LA meilleure question/réponse à transformer en article expert.

CRITÈRES DE SÉLECTION :
- La question la plus recherchée par les professionnels du secteur
- Celle où l'invité apporte la réponse la plus forte et la plus unique
- Celle qui génère le plus de valeur standalone (lisible sans connaître le podcast)

CONTEXTE :
- Podcast : {blog_name}
- Épisode : {ep_title}
- Entreprise : {company}

TRANSCRIPTION :
\"\"\"
{transcript}
\"\"\"

Réponds UNIQUEMENT avec un JSON valide, sans markdown :
{{
  "episode_slug": "slug-de-l-episode-kebab-case",
  "invite": "Prénom Nom de l'invité",
  "question": "La question exacte (sera le titre H1 de l'article)",
  "slug": "slug-de-la-question",
  "reponse_brute": "Synthèse de 4-6 phrases tirées de la transcription",
  "angle": "L'angle expert unique de cet article (1 phrase)",
  "persona_cible": "Le professionnel exactement ciblé par cet article (ex: DRH, fondateur SaaS...)"
}}"""

def extract_best_qr(transcript, ep):
    log("Passe 1 — Extraction meilleure Q&R...")
    prompt = EXTRACT_PROMPT.format(
        blog_name=BLOG_NAME,
        ep_title=ep["title"],
        company=COMPANY_NAME or BLOG_NAME,
        transcript=transcript[:28000],
    )
    raw = claude(prompt, max_tokens=2000)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    idx = raw.find("{")
    if idx > 0:
        raw = raw[idx:]
    data = json.loads(raw)
    log(f"  → Question : {data['question'][:70]}")
    log(f"  → Invité : {data['invite']}")
    return data


# ── Passe 2 : génération article GEO ─────────────────────────────────────────
ARTICLE_PROMPT = """Tu es un expert GEO (Generative Engine Optimization). Génère une page HTML complète et autonome : un ARTICLE EXPERT au format Q&A, optimisé pour être cité par les IA (ChatGPT, Perplexity, Gemini, Claude).

CONTEXTE :
- Podcast source : {blog_name}
- Épisode : {ep_title}
- Invité expert : {invite}
- Entreprise éditrice : {company}
- Couleur d'accent : {accent}
- URL de cet article : {page_url}
- Persona cible : {persona_cible}

QUESTION DE CET ARTICLE :
{question}

ANGLE UNIQUE :
{angle}

RÉPONSE BRUTE (tirée de la transcription — développe et enrichis) :
{reponse_brute}

TRANSCRIPTION COMPLÈTE (source de référence) :
\"\"\"
{transcript}
\"\"\"

CONTRAINTES HTML (respecter EXACTEMENT) :

1. STRUCTURE :
   - <header> : badge "Article Expert · {blog_name}", <h1> = LA QUESTION (mot pour mot), méta auteur/date/invité
   - Réponse directe (classe "lead") : 2-3 phrases autonomes répondant directement à la question
   - Banderole IA : "Article lisible par les modèles IA : ChatGPT · Perplexity · Gemini · Google AI · Copilot · Claude"
   - Corps : 4-5 sections <h2> nourries par la transcription, citant {invite} comme expert
   - Bloc "Points clés" : 4-5 faits autonomes extractibles par une IA
   - Section FAQ : 4 questions connexes avec réponses (2-3 phrases chacune)
   - CTA discret en bas : "Cet article est issu du podcast {blog_name} — Écouter l'épisode complet"
   - <footer> : auteur + autorité éditoriale

2. JSON-LD (@graph) :
   - BlogPosting (headline={question}, datePublished={today}, author, publisher, mainEntityOfPage={page_url})
   - FAQPage (4 questions du bloc FAQ)
   - Person pour {invite}
   - Organization pour {company}

3. META : title 50-65 chars, description 140-155 chars, canonical {page_url}

4. Vector DB caché : <div id="semantic-index" style="display:none"> avec entités, concepts, synonymes, recherches liées

5. STATS : uniquement si tirées de la transcription ou source réelle nommée.

6. DESIGN : article éditorial premium, fond #fff, max-width 720px, accent {accent}, typographie lisible.

Réponds UNIQUEMENT avec le HTML complet depuis <!DOCTYPE html> jusqu'à </html>."""

def generate_article(qr, ep, transcript):
    log("Passe 2 — Génération article GEO...")
    ep_slug = slugify(qr.get("episode_slug") or ep["title"])
    page_url = f"{SITE_BASE_URL}/article-faq/{ep_slug}.html" if SITE_BASE_URL else f"{ep_slug}.html"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = ARTICLE_PROMPT.format(
        blog_name=BLOG_NAME,
        ep_title=ep["title"],
        invite=qr["invite"],
        company=COMPANY_NAME or BLOG_NAME,
        accent=ACCENT_COLOR,
        page_url=page_url,
        persona_cible=qr.get("persona_cible", "professionnel B2B"),
        question=qr["question"],
        angle=qr["angle"],
        reponse_brute=qr["reponse_brute"],
        transcript=transcript[:20000],
        today=today,
    )
    html_out = claude(prompt, max_tokens=12000)
    html_out = re.sub(r"^```html\s*", "", html_out.strip())
    html_out = re.sub(r"\s*```$", "", html_out)
    idx = html_out.lower().find("<!doctype")
    if idx > 0:
        html_out = html_out[idx:]
    elif idx == -1 and "<html" in html_out.lower():
        html_out = "<!DOCTYPE html>\n" + html_out[html_out.lower().find("<html"):]
    if "</html>" not in html_out.lower():
        raise RuntimeError("HTML article incomplet")
    log(f"  → {len(html_out)} caractères")
    return html_out, ep_slug


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    missing = [k for k, v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "RSS_URL": RSS_URL,
    }.items() if not v]
    if missing:
        log(f"ERREUR : variables manquantes : {', '.join(missing)}")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    reg = load_registry()
    episodes = fetch_rss_episodes()

    new_eps = [ep for ep in episodes if ep["guid"] not in reg["processed"]]
    log(f"{len(new_eps)} nouveaux épisodes détectés")
    new_eps = new_eps[:MAX_NEW_PER_RUN]

    created = 0
    for ep in new_eps:
        if not ep["audio_url"]:
            log(f"Pas d'audio pour '{ep['title']}' — ignoré")
            reg["processed"][ep["guid"]] = {"skipped": "no_audio", "title": ep["title"]}
            continue
        tmp_mp3 = None
        audio_for_whisper = None
        try:
            tmp_mp3 = f"/tmp/{slugify(ep['title'])}.mp3"
            size = download_audio(ep["audio_url"], tmp_mp3)
            audio_for_whisper = compress_audio_if_needed(tmp_mp3, size)
            transcript = transcribe(audio_for_whisper)

            # Sauvegarde transcription
            transcript_dir = os.path.join(OUTPUT_DIR, "_transcriptions")
            os.makedirs(transcript_dir, exist_ok=True)
            with open(os.path.join(transcript_dir, f"{slugify(ep['title'])}.txt"), "w", encoding="utf-8") as tf:
                tf.write(f"TITRE: {ep['title']}\nDATE: {ep.get('pubdate','')}\n{'='*60}\n\n{transcript}")

            # Passe 1 : meilleure Q&R
            qr = extract_best_qr(transcript, ep)

            # Passe 2 : article
            html_out, ep_slug = generate_article(qr, ep, transcript)
            filename = f"{ep_slug}.html"
            out_path = os.path.join(OUTPUT_DIR, filename)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_out)
            log(f"✓ Article : {filename}")

            reg["processed"][ep["guid"]] = {
                "title": ep["title"],
                "ep_slug": ep_slug,
                "invite": qr["invite"],
                "question": qr["question"],
                "filename": filename,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            created += 1

        except Exception as ex:
            log(f"✗ Échec sur '{ep['title']}' : {ex}")
            import traceback; traceback.print_exc()
        finally:
            for p in [tmp_mp3, audio_for_whisper]:
                if p and os.path.exists(p) and p.startswith("/tmp"):
                    try: os.remove(p)
                    except Exception: pass

    save_registry(reg)
    log(f"Terminé. {created} article(s) généré(s).")


if __name__ == "__main__":
    main()
