# MarketForge GEO Template

Systeme automatique : 1 episode podcast = 1 article expert FAQ GEO publie sur le site client.
Format "Analyse Podcast" — optimise pour etre cite par les IA et envoye en cold mail dirigeant.

---

## Configuration

### Secrets (Settings > Secrets and variables > Actions > Secrets)

| Secret | Description |
|--------|-------------|
| `OPENAI_API_KEY` | Cle OpenAI — transcription Whisper |
| `ANTHROPIC_API_KEY` | Cle Anthropic — generation Claude |
| `FTP_SERVER` | Serveur FTP client |
| `FTP_USERNAME` | Identifiant FTP |
| `FTP_PASSWORD` | Mot de passe FTP |

### Variables (Settings > Secrets and variables > Actions > Variables)

| Variable | Obligatoire | Exemple |
|----------|-------------|---------|
| `RSS_URL` | Oui | https://feed.ausha.co/xxx |
| `BLOG_NAME` | Oui | La Pause RH |
| `COMPANY_NAME` | Oui | Solutions 30 |
| `ACCENT_COLOR` | Oui | #ff6a1a |
| `SITE_BASE_URL` | Oui | https://client.fr |
| `FTP_SERVER_DIR` | Oui | /client.fr/article-faq/ |
| `PODCAST_URL` | Recommande | https://open.spotify.com/show/xxx |
| `CONTACT_URL` | Recommande | https://client.fr/contact |
| `LISTENLY_PODCAST_URL` | Recommande | https://listenly.fr/podcast/nom-du-podcast |

---

## Lancement

1. Cliquer "Use this template" -> nouveau repo client
2. Configurer les 5 secrets + 9 variables ci-dessus
3. Actions > "MarketForge GEO - Generate FAQ Articles" > Run workflow
4. Les articles sont deployes automatiquement sur le site client

---

## Structure generee

```
article-faq/
  {episode-slug}.html    <- 1 article par episode
```

---

## Variables optionnelles

- `PODCAST_URL` : lien Spotify/Apple du podcast. Si absent, utilise le lien RSS de l'episode.
- `CONTACT_URL` : lien vers la page contact ou appel decouverte. Affiche un bouton "Nous contacter" dans la card bas de l'article.
- `LISTENLY_PODCAST_URL` : URL de la page Listenly du podcast client. Utilise en backend JSON-LD (invisible lecteur, backlink dofollow Listenly).

---

## Architecture

```
RSS -> Whisper (transcription) -> Claude (extraction Q&R) -> HTML (template)
                                                                    |
                                                            pages/article-faq/
                                                                    |
                                                            FTP -> site client
```
