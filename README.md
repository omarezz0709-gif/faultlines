# Faultlines

An interactive 3D globe of international relations, indirect links (militant networks, smuggling and migration routes, proxy wars, resources), chokepoints and flashpoints, with country facts and politics.

It runs on its own: GitHub hosts the site for free (GitHub Pages), and a GitHub Actions job refreshes the data from the news at **00:00, 12:00 and 18:00 Berlin time** using Claude with web search. Nothing needs to be running on your computer.

## How it works

| Part | What it does |
|---|---|
| `index.html` | The whole site: globe, panels, filters, comparisons. Built-in baseline data lives inside it. |
| `data/live.json` | Changes from the news (relations, indirect links, locations, politics, update log). Written by the updater, loaded by the page every 10 minutes. |
| `data/wb.json` | World Bank figures (economy, society, military, environment). Refreshed daily. |
| `scripts/refresh.py` | Asks Claude (with web search) what changed since the last run and merges it into `data/live.json`. |
| `scripts/worldbank.py` | Pulls the latest World Bank figures. |
| `.github/workflows/refresh.yml` | Runs both scripts on schedule, commits the data, republishes the site. |
| `.github/workflows/pages.yml` | Republishes the site when you push changes. |

## One-time setup

1. **Create the repository.** On github.com, click **New repository**, name it `faultlines`, set it to **Public** (free GitHub Pages needs a public repo), and don't add a README.
2. **Upload this folder.** In this folder, run:
   ```
   git remote add origin https://github.com/YOUR-USERNAME/faultlines.git
   git push -u origin main
   ```
   Git will ask you to sign in to GitHub the first time.
3. **Add your Anthropic API key as a secret.** Repository → **Settings → Secrets and variables → Actions → New repository secret**. Name: `ANTHROPIC_API_KEY`, value: your key from [console.anthropic.com](https://console.anthropic.com/settings/keys). Secrets are encrypted and never visible in the site or logs.
4. **Turn on GitHub Pages.** Repository → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
5. **Publish and test.** Repository → **Actions → Publish site → Run workflow**, then **Actions → Refresh from the news → Run workflow**. When they finish, the site is at `https://YOUR-USERNAME.github.io/faultlines/`.

## Options

- **Model:** the updater uses Claude Opus 5. To use a cheaper model, add a repository *variable* (Settings → Secrets and variables → Actions → Variables) named `FAULTLINES_MODEL` with `claude-sonnet-5` or `claude-haiku-4-5`.
- **Explain / Analyse / Fill in buttons:** these call Claude from the visitor's browser with a key the visitor enters under **⚙ Settings** on the site (stored only in that browser). Your repository secret is never exposed to visitors.
- **Manual refresh:** Actions → Refresh from the news → Run workflow.
- **Preview locally:** run `python -m http.server` in this folder and open http://localhost:8000 (opening `index.html` directly won't load the data files).

## Costs

GitHub hosting and Actions are free for public repositories. The Anthropic API is billed per use: each refresh is one Claude request with up to 25 web searches (roughly $0.30–1.50 per run with Opus 5, less with Sonnet 5), so about $1–4.50 a day at three runs a day. Set a monthly spend limit in the Anthropic Console to cap it.

## Notes

- GitHub's scheduler can start runs 5–30 minutes late; the updater accepts runs up to 90 minutes after each slot and skips duplicates.
- GitHub pauses scheduled workflows in repositories with no activity for 60 days; the updater's own commits count as activity.
- Relationship statuses are editorial shorthand. Every change from the news is logged on the site with its source link.
