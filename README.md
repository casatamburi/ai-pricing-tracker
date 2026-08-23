# Agentic Token Pricing — Auto-Updating Tracker

Fully automated. Every Monday 09:00 UTC the agent searches the web for new model
releases / price changes, updates the data, regenerates the page, and emails you
the link. **Blended costs and bar widths are computed in Python — never by the LLM —**
so the math is always correct. The LLM only extracts raw facts (names, prices, context).

> **Structure:** the workflow lives at `.github/workflows/check.yml`. Keep that
> path exactly — GitHub Actions only discovers workflows under `.github/workflows/`.

## Files

| File | Role |
|---|---|
| `models.json` | Source of truth — raw facts only |
| `render.py` | Deterministic renderer: computes blended cost + bars, emits `index.html` |
| `check.py` | Weekly agent: web search → extract facts → merge → render → email |
| `.github/workflows/check.yml` | Cron + commit + deploy to GitHub Pages |
| `index.html` | Generated output (served by Pages) |

## One-time setup (~15 min)

### 1. Create the repo
- New GitHub repo (public is fine; private also works with Pages on paid, or keep public).
- Upload all files preserving the `.github/workflows/` folder structure.

### 2. Anthropic API key
- console.anthropic.com → API Keys → Create Key.
- Add credit (this runs ~once/week, cost is cents per run).

### 3. Gmail app-password
- Your Google account needs 2-Step Verification ON.
- myaccount.google.com → Security → 2-Step Verification → App passwords.
- Generate one for "Mail". Copy the 16-char string (no spaces).

### 4. Repo secrets
Settings → Secrets and variables → Actions → New repository secret. Add:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your API key |
| `GMAIL_USER` | your.address@gmail.com |
| `GMAIL_APP_PASSWORD` | the 16-char app password |
| `MAIL_TO` | where to send the alert (can be the same gmail) |
| `PAGES_URL` | `https://<user>.github.io/<repo>/` (fill after step 5) |

### 5. Enable GitHub Pages
- Settings → Pages → Build and deployment → Source: **GitHub Actions**.
- After the first workflow run, your URL is `https://<user>.github.io/<repo>/`.
- Put that URL into the `PAGES_URL` secret.

### 6. Test it
- Actions tab → "weekly-token-pricing" → Run workflow (manual trigger).
- You should get an email within a few minutes and see `index.html` deployed.

## DST note
GitHub cron is UTC and ignores daylight saving. `0 9 * * 1` (Monday 09:00 UTC) =
11:00 in summer (CEST), 10:00 in winter (CET). If you want a fixed local time
year-round, change the winter months — or just accept the 1-hour seasonal drift.

## Editing manually
Edit `models.json`, run `python render.py`, commit. Never hand-edit `index.html`.

## Cost guardrail
One Opus run with web search per week. If you want to cut cost, switch `MODEL`
in `check.py` to `claude-sonnet-4-6`.
