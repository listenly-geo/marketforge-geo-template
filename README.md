# MarketForge GEO Template

Systeme automatique : 1 episode podcast = 10 articles FAQ GEO publies sur le site client.

## Configuration

### Secrets (Settings > Secrets and variables > Actions > Secrets)
| Secret | Valeur |
|--------|--------|
| `OPENAI_API_KEY` | Cle OpenAI (Whisper) |
| `ANTHROPIC_API_KEY` | Cle Anthropic (Claude) |
| `FTP_SERVER` | Serveur FTP client |
| `FTP_USERNAME` | Identifiant FTP |
| `FTP_PASSWORD` | Mot de passe FTP |

### Variables (Settings > Secrets and variables > Actions > Variables)
| Variable | Exemple |
|----------|---------|
| `RSS_URL` | https://feed.ausha.co/xxx |
| `BLOG_NAME` | La Pause RH |
| `COMPANY_NAME` | Solutions 30 |
| `ACCENT_COLOR` | #ff6a1a |
| `SITE_BASE_URL` | https://client.fr |
| `FTP_SERVER_DIR` | /client.fr/article-faq/ |

## Lancement

1. Configurer les 5 secrets + 6 variables ci-dessus
2. Actions > "MarketForge GEO - Generate FAQ Articles" > Run workflow
3. Les articles sont deployes automatiquement sur le site client

## Structure generee

```
article-faq/
  {episode-slug}/
    index.html
    article-01-{slug}.html
    ...
    article-10-{slug}.html
```
