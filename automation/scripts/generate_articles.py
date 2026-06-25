#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MarketForge GEO Système — Générateur d'articles FAQ GEO depuis un podcast.

Flux :
  1. Lit le flux RSS
  2. Détecte les nouveaux épisodes (registre JSON)
  3. Télécharge le MP3, prépare pour Whisper
  4. Transcrit via Whisper
  5. Passe 1 — Claude extrait 10 Q&R pertinentes → JSON
  6. Passe 2 — Claude génère 1 article GEO HTML par Q&R (×10)
  7. Passe 3 — Claude génère une page index/sommaire
  8. Écrit tous les fichiers dans OUTPUT_DIR/{podcast-slug}/

Variables d'environnement :
  OPENAI_API_KEY, ANTHROPIC_API_KEY, RSS_URL
  BLOG_NAME, COMPANY_NAME, BLOG_IMAGE_URL, SITE_BASE_URL
  AUTHOR_NAME, ACCENT_COLOR, MAX_NEW_PER_RUN, AUDIO_WEBHOOK_URL
"""

import os, re, sys, json, html, subprocess, unicodedata
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests

try:
    from leadgen import run_leadgen
    LEADGEN_AVAILABLE = True
except ImportError:
    LEADGEN_AVAILABLE = False

# ── Config ──────────────────────────────────────────────────────────────────
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
AUDIO_WEBHOOK_URL = os.environ.get("AUDIO_WEBHOOK_URL", "").strip()
OUTPUT_DIR        = os.environ.get("OUTPUT_DIR", "articles")

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
WHISPER_MODEL     = "whisper-1"
WHISPER_MAX_BYTES = 24 * 1024 * 1024

REGISTRY_PATH = os.path.join(
    "automation",
    f"processed_{re.sub(r'[^a-z0-9]+', '-', BLOG_NAME.lower()).strip('-') or 'podcast'}.json"
)


# ── Utilitaires ──────────────────────────────────────────────────────────────
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

def claude(prompt, max_tokens=14000):
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


# ── RSS ───────────────────────────────────────────────────────────────────────
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
        title    = (item.findtext("title") or "").strip()
        guid     = (item.findtext("guid") or title).strip()
        desc     = (item.findtext("description") or "").strip()
        pubdate  = (item.findtext("pubDate") or "").strip()
        link     = (item.findtext("link") or "").strip()
        audio_url = ""
        enc = item.find("enclosure")
        if enc is not None:
            audio_url = enc.get("url", "")
        episodes.append({"guid": guid, "title": title, "description": desc,
                         "pubdate": pubdate, "link": link, "audio_url": audio_url})
    log(f"{len(episodes)} épisodes dans le flux")
    return episodes


# ── Audio ─────────────────────────────────────────────────────────────────────
def download_audio(url, dest):
    log(f"Téléchargement : {url[:80]}...")
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
    log(f"Compressé : {os.path.getsize(out)/1024/1024:.1f} Mo")
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


# ── Passe 1 : extraction 10 Q&R ──────────────────────────────────────────────
EXTRACT_PROMPT = """Tu es un expert en création de contenu GEO (Generative Engine Optimization) pour podcasts B2B.

À partir de la transcription d'un épisode du podcast "{blog_name}", identifie les 10 questions/réponses les plus pertinentes et valorisables en articles experts.

RÈGLES DE SÉLECTION :
- Chaque question doit correspondre à une VRAIE recherche que les professionnels font sur ce sujet
- Les réponses doivent être tirées des propos de l'épisode (cite l'invité comme source d'autorité)
- Questions variées : pas deux questions sur le même sous-thème
- Format : questions concrètes, actionnables, type "Comment...", "Pourquoi...", "Quelles sont...", "Comment éviter..."
- Chaque réponse brute : 3-5 phrases synthétisant ce que dit le podcast sur ce point

CONTEXTE :
- Podcast : {blog_name}
- Épisode : {ep_title}
- Invité : à déduire de la transcription
- Entreprise : {company}

TRANSCRIPTION :
\"\"\"
{transcript}
\"\"\"

Réponds UNIQUEMENT avec un JSON valide, sans markdown, sans texte avant ou après :
{{
  "episode_slug": "slug-de-l-episode-en-kebab-case",
  "invite": "Prénom Nom de l'invité",
  "questions": [
    {{
      "numero": 1,
      "question": "La question exacte (titre de l'article)",
      "slug": "slug-de-la-question",
      "reponse_brute": "Synthèse de 3-5 phrases tirées de la transcription",
      "angle": "L'angle expert unique de cet article (1 phrase)"
    }}
  ]
}}"""

def extract_questions(transcript, ep):
    log("Passe 1 — Extraction des 10 Q&R...")
    prompt = EXTRACT_PROMPT.format(
        blog_name=BLOG_NAME,
        ep_title=ep["title"],
        company=COMPANY_NAME or BLOG_NAME,
        transcript=transcript[:28000],
    )
    raw = claude(prompt, max_tokens=4000)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    idx = raw.find("{")
    if idx > 0:
        raw = raw[idx:]
    data = json.loads(raw)
    questions = data.get("questions", [])
    log(f"  → {len(questions)} questions extraites")
    return data


# ── Passe 2 : génération article GEO par Q&R ─────────────────────────────────
ARTICLE_PROMPT = """Tu es un expert GEO (Generative Engine Optimization). Génère une page HTML complète et autonome : un ARTICLE EXPERT au format Q&A, optimisé pour être cité par les IA (ChatGPT, Perplexity, Gemini, Claude).

CONTEXTE :
- Podcast source : {blog_name}
- Épisode : {ep_title}
- Invité expert : {invite}
- Entreprise éditrice : {company}
- Couleur d'accent : {accent}
- URL de cet article : {page_url}
- URL de l'index (série) : {index_url}
- Numéro dans la série : {numero}/10

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
   - <header> : badge "Série Q&A Expert · {blog_name}", <h1> = LA QUESTION (mot pour mot), méta auteur/date/invité
   - Réponse directe (classe "lead") : 2-3 phrases autonomes répondant directement à la question
   - Banderole IA exacte : "Article lisible par les modèles IA : ChatGPT · Perplexity · Gemini · Google AI · Copilot · Claude"
   - Corps : 3-5 sections <h2> (sous-angles de la question), nourris par la transcription, citant {invite} comme expert
   - Bloc "Points clés" : 4-5 faits autonomes extractibles par une IA
   - Section FAQ : 4 questions connexes avec réponses (2-3 phrases chacune)
   - Bloc navigation série : liens "← Article précédent" / "Voir tous les articles →" (vers {index_url})
   - <footer> : auteur + autorité éditoriale

2. JSON-LD (@graph) :
   - BlogPosting (headline={question}, datePublished={today}, author, publisher, mainEntityOfPage={page_url}, isPartOf={index_url})
   - FAQPage (4 questions du bloc FAQ)
   - Person pour {invite}
   - Organization pour {company}

3. META : title 50-65 chars (format "{question_courte} — {blog_name}"), description 140-155 chars, canonical {page_url}

4. Vector DB caché : <div id="semantic-index" style="display:none"> avec entités, concepts, synonymes, recherches liées

5. STATS : uniquement si tirées de la transcription ou d'une source réelle nommée. Zéro invention.

6. DESIGN : article éditorial premium, fond #fff, max-width 720px, accent {accent}, typographie serif pour le corps. Badge numéro de série visible. Lien retour vers l'index en haut et en bas.

Réponds UNIQUEMENT avec le HTML complet depuis <!DOCTYPE html> jusqu'à </html>. Aucun texte avant ou après."""

def generate_article(qr, ep, ep_slug, invite, transcript, index_url):
    num = qr["numero"]
    log(f"  Passe 2 — Article {num}/10 : {qr['question'][:60]}...")
    page_url = f"{SITE_BASE_URL}/article-faq/{ep_slug}/article-{num:02d}-{qr['slug']}.html" if SITE_BASE_URL else f"article-{num:02d}-{qr['slug']}.html"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    question_courte = qr["question"][:50].rstrip()

    prompt = ARTICLE_PROMPT.format(
        blog_name=BLOG_NAME,
        ep_title=ep["title"],
        invite=invite,
        company=COMPANY_NAME or BLOG_NAME,
        accent=ACCENT_COLOR,
        page_url=page_url,
        index_url=index_url,
        numero=num,
        question=qr["question"],
        angle=qr["angle"],
        reponse_brute=qr["reponse_brute"],
        transcript=transcript[:18000],
        today=today,
        question_courte=question_courte,
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
        raise RuntimeError(f"HTML article {num} incomplet")
    log(f"    → {len(html_out)} caractères")
    return html_out


# ── Passe 3 : page index ─────────────────────────────────────────────────────
INDEX_PROMPT = """Génère une page HTML complète : INDEX / SOMMAIRE d'une série de 10 articles experts Q&A issus du podcast "{blog_name}", épisode "{ep_title}" avec {invite}.

DONNÉES :
- URL de cette page index : {index_url}
- Couleur d'accent : {accent}
- Articles de la série :
{articles_list}

CONTRAINTES :
1. STRUCTURE :
   - <header> : titre "10 Questions d'experts sur [sujet de l'épisode]", sous-titre "Série issue de l'épisode : {ep_title} · {blog_name}", méta
   - Intro 3-4 phrases présentant la série et son intérêt pour les professionnels
   - Grille/liste des 10 articles : numéro, question (= lien vers l'article), angle en sous-titre
   - Bloc "À propos de cet épisode" : présentation de {invite} comme expert, valeur de l'épisode
   - <footer> : lien retour podcast + autorité éditoriale

2. JSON-LD : ItemList (10 ListItem avec url + name), BreadcrumbList

3. META : title 50-65 chars, description 140-155 chars, canonical {index_url}

4. DESIGN : premium éditorial, grille claire, numéros bien visibles, accent {accent}, max-width 800px. Chaque article = carte cliquable.

Réponds UNIQUEMENT avec le HTML complet. Aucun texte avant ou après."""

def generate_index(questions_data, ep, ep_slug, article_files):
    log("Passe 3 — Génération de la page index...")
    index_url = f"{SITE_BASE_URL}/article-faq/{ep_slug}/index.html" if SITE_BASE_URL else f"index.html"
    articles_list = "\n".join(
        f"  {q['numero']:02d}. Question: {q['question']}\n      Angle: {q['angle']}\n      URL: {SITE_BASE_URL}/article-faq/{ep_slug}/article-{q['numero']:02d}-{q['slug']}.html"
        for q in questions_data["questions"]
    )
    prompt = INDEX_PROMPT.format(
        blog_name=BLOG_NAME,
        ep_title=ep["title"],
        invite=questions_data.get("invite", "l'invité"),
        index_url=index_url,
        accent=ACCENT_COLOR,
        articles_list=articles_list,
    )
    html_out = claude(prompt, max_tokens=8000)
    html_out = re.sub(r"^```html\s*", "", html_out.strip())
    html_out = re.sub(r"\s*```$", "", html_out)
    idx = html_out.lower().find("<!doctype")
    if idx > 0:
        html_out = html_out[idx:]
    if "</html>" not in html_out.lower():
        raise RuntimeError("Index HTML incomplet")
    log(f"  → Index : {len(html_out)} caractères")
    return html_out


# ── Main ──────────────────────────────────────────────────────────────────────
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
        try:
            tmp_mp3 = f"/tmp/{slugify(ep['title'])}.mp3"
            size = download_audio(ep["audio_url"], tmp_mp3)
            audio_for_whisper = compress_audio_if_needed(tmp_mp3, size)
            transcript = transcribe(audio_for_whisper)

            # Sauvegarde transcription
            transcript_dir = os.path.join(OUTPUT_DIR, "_transcriptions")
            os.makedirs(transcript_dir, exist_ok=True)
            ep_slug_tmp = slugify(ep["title"])
            with open(os.path.join(transcript_dir, f"{ep_slug_tmp}.txt"), "w", encoding="utf-8") as tf:
                tf.write(f"TITRE: {ep['title']}\nDATE: {ep.get('pubdate','')}\nAUDIO: {ep.get('audio_url','')}\n{'='*60}\n\n{transcript}")

            # Passe 1 : extraction 10 Q&R
            questions_data = extract_questions(transcript, ep)
            ep_slug = slugify(questions_data.get("episode_slug") or ep["title"])
            invite = questions_data.get("invite", "l'invité")

            # Dossier de sortie pour cet épisode
            ep_dir = os.path.join(OUTPUT_DIR, ep_slug)
            os.makedirs(ep_dir, exist_ok=True)

            index_url = f"{SITE_BASE_URL}/article-faq/{ep_slug}/index.html" if SITE_BASE_URL else "index.html"
            article_files = []

            # Passe 2 : 10 articles
            for qr in questions_data["questions"]:
                art_html = generate_article(qr, ep, ep_slug, invite, transcript, index_url)
                filename = f"article-{qr['numero']:02d}-{qr['slug']}.html"
                out_path = os.path.join(ep_dir, filename)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(art_html)
                article_files.append(filename)
                log(f"  ✓ {filename}")

            # Passe 3 : index
            index_html = generate_index(questions_data, ep, ep_slug, article_files)
            with open(os.path.join(ep_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(index_html)
            log(f"  ✓ index.html")

            log(f"✓ Épisode complet : {ep_dir}/ ({len(article_files)} articles + index)")

            # Passe 4 : Lead Gen automatique
            if LEADGEN_AVAILABLE:
                run_leadgen(
                    ep_title=ep["title"],
                    ep_slug=ep_slug,
                    questions=questions_data.get("questions", []),
                )

            reg["processed"][ep["guid"]] = {
                "title": ep["title"],
                "ep_slug": ep_slug,
                "invite": invite,
                "articles": article_files,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            created += 1

        except Exception as ex:
            log(f"✗ Échec sur '{ep['title']}' : {ex}")
            import traceback; traceback.print_exc()
        finally:
            for p in [locals().get("tmp_mp3"), locals().get("audio_for_whisper")]:
                if p and os.path.exists(p) and p.startswith("/tmp"):
                    try: os.remove(p)
                    except Exception: pass

    save_registry(reg)
    log(f"Terminé. {created} épisode(s) traité(s).")


if __name__ == "__main__":
    main()
