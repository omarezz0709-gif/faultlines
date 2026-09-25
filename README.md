# Faultlines

An interactive 3D globe of international relations, indirect links (militant networks, smuggling and migration routes, proxy wars, resources), chokepoints and flashpoints, with live headlines, current leaders and country figures.

**Completely free.** GitHub hosts the site (GitHub Pages) and refreshes the data at **00:00, 07:00, 12:00 and 18:00 Berlin time** (GitHub Actions). No API keys, no costs, nothing running on your computer.

## What refreshes automatically

| Data | Source (free) | How often |
|---|---|---|
| Latest headlines per country, "news tension" meter, headlines naming two countries together | Google News RSS | 3× a day |
| "Headlines to watch": headlines naming 2+ countries with words like war, sanctions, ceasefire, coup | derived from the headlines | 3× a day |
| Heads of state and government, their party, system of government | Wikidata | daily |
| Economy, society, military and environment figures | World Bank API | daily |
| Relationship statuses, indirect links, locations, election dates (each citing a headline) | Google Gemini free tier reading the collected headlines (`scripts/ai_refresh.py`) | 3× a day, when the `GEMINI_API_KEY` secret is set |

Relationship statuses (Ally, Tense, Hostile…), indirect links and strategic locations start from an editorial baseline inside `index.html`; changes go into `data/live.json` (you can also edit it by hand). Every AI change must cite one of the collected headlines, and changes without a real source are dropped. Without the Gemini secret, everything else still refreshes.

## Files

| File | What it is |
|---|---|
| `index.html` | The whole site. |
| `data/news.json`, `data/politics.json`, `data/wb.json` | Written by the updater, loaded by the site. |
| `data/live.json` | Manual changes to relations, links, locations and the update log. Edit it by hand if you like. |
| `scripts/news.py`, `scripts/wikidata.py`, `scripts/worldbank.py` | The free updaters. |
| `.github/workflows/refresh.yml` | Runs the updaters on schedule, commits the data, republishes the site. |
| `.github/workflows/pages.yml` | Republishes the site when you push changes. |

## One-time setup (about 5 minutes)

1. **Create the repository.** On github.com click **New repository**, name it `faultlines`, choose **Public** (free GitHub Pages needs a public repo), and don't add a README.
2. **Upload this folder.** In this folder run:
   ```
   git remote add origin https://github.com/YOUR-USERNAME/faultlines.git
   git push -u origin main
   ```
   Git asks you to sign in to GitHub the first time.
3. **Turn on GitHub Pages.** Repository → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Publish.** Repository → **Actions → Publish site → Run workflow**. After a minute the site is at `https://YOUR-USERNAME.github.io/faultlines/`. It opens on any device, for anyone with the link.
5. **(Optional) Test a refresh now:** **Actions → Refresh data → Run workflow**.

## Optional extras

- **Explain / Analyse / Fill-in buttons:** these call Claude from the visitor's browser using the visitor's own Anthropic API key, entered under **⚙ Settings** on the site (stored only in that browser). That is paid by whoever enters a key; everything else is free.
- **Preview locally:** run `python -m http.server` in this folder and open http://localhost:8000 (opening `index.html` directly won't load the data files).

## Notes

- GitHub's scheduler can start runs 5–30 minutes late; the scripts accept runs up to 90 minutes after each slot and skip duplicates.
- GitHub pauses scheduled workflows after 60 days without repository activity; the updater's own commits count as activity.
- The news tension meter is a simple keyword measure (share of recent headlines mentioning conflict terms), not an expert assessment.

## AI and news Worker (Cloudflare)

`worker/gemini-proxy.js` runs on Cloudflare as the Worker **faultlines-ai** (AI answers, top stories, videos, live feed, admin log).
It is deployed automatically by `.github/workflows/worker.yml` whenever something in `worker/` changes.
That needs one GitHub secret, `CLOUDFLARE_API_TOKEN` (Cloudflare → My Profile → API Tokens → template "Edit Cloudflare Workers").
The Worker's own secrets (Gemini keys, admin code) live only in Cloudflare and are never part of the repository.
