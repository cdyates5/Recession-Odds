# US Recession Odds Monitor

A self-updating dashboard of US recession odds from the prediction markets: every calendar-year
recession contract on **Kalshi** and **Polymarket** since 2023, sampled weekly (Friday close), with
a life-cycle comparison, the venue gap, a NY Fed yield-curve benchmark and a track record of
resolved contracts.

GitHub Actions rebuilds it every day and publishes it to GitHub Pages:

- Dashboard: `https://<your-username>.github.io/<repo-name>/`
- Weekly data: `https://<your-username>.github.io/<repo-name>/us_recession_odds_weekly.csv`

The CSV link is stable, so other models or spreadsheets can read it directly.

## What's in the repo

| Path | Purpose |
|---|---|
| `rebuild.py` | Fetches the data, discovers new contracts, builds the page, runs the sanity gate. The page template is embedded, so this one file is the whole build. |
| `requirements.txt` | Pinned Python dependencies (pandas, numpy, xlrd). |
| `.github/workflows/refresh.yml` | Daily schedule: build, deploy to Pages, commit the refreshed cache. |
| `cache/` | Raw daily prices per contract plus the NY Fed series. Settled contracts are read from here; live contracts fall back to it if an API is down. The workflow commits it after each run. |
| `.gitignore` | Keeps build output (`_site/`) out of git. |

No API keys or secrets are needed. All three sources are public.

## One-time setup (about 5 minutes)

1. **Create a repository** on GitHub, e.g. `us-recession-odds`. Make it **public**: GitHub Pages
   on a private repo needs a paid plan (Pro, Team or Enterprise).

2. **Add the files.** Either way works:

   *With git:*
   ```bash
   cd us-recession-odds          # the unzipped folder
   git init -b main
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/<your-username>/us-recession-odds.git
   git push -u origin main
   ```

   *In the browser:* on the new repo page choose **uploading an existing file** and drag in the
   *contents* of the unzipped folder. The `.github` folder is hidden by default on a Mac; press
   **Cmd + Shift + .** in Finder to show it. Afterwards, check that
   `.github/workflows/refresh.yml` is visible in the repo. If it isn't, use
   **Add file → Create new file**, type `.github/workflows/refresh.yml` as the name and paste the
   file's contents.

3. **Allow the workflow to commit.** Settings → Actions → General → *Workflow permissions* →
   **Read and write permissions** → Save.

4. **Turn on Pages.** Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.

5. **Run it once.** Actions tab → enable workflows if prompted → **Refresh recession odds** →
   **Run workflow**. It takes about a minute. The page URL appears on the run summary and under
   Settings → Pages.

After that it runs on its own every day.

## Schedule

The workflow runs daily at **22:17 UTC**, which is 09:17 in Melbourne during daylight saving
(AEDT) and 08:17 otherwise (AEST). That is after the US session, so each run picks up a settled
day of trading. GitHub can delay scheduled runs by 5–30 minutes when it is busy.

To change it, edit the `cron:` line in `.github/workflows/refresh.yml`. Cron times are always UTC.
For example, `"17 10,22 * * *"` runs twice a day (21:17 and 09:17 AEDT).

## What updates by itself

- **Prices**: all live contracts on both venues, plus the NY Fed model each month.
- **New contracts**: Kalshi's next contract (e.g. `KXRECSSNBER-28`) and Polymarket's next
  "US recession by end of 2028?" are found automatically when they list. Their rule summaries
  are generated from the venue's own rules text.
- **The year roll**: on 1 January the current-year and next-year roles move forward a year,
  and the colours follow (orange is always the current year, teal the next). Until the new
  next-year contract lists, its tile shows "Not listed" and the same-date comparison switches
  to current-year odds.
- **Readout**: the findings text is regenerated from the numbers on every run.

## What stays manual

- **Event markers.** The numbered annotations (SVB, Liberation Day, the Iran strikes, ...) are the
  `EVENTS` list near the top of `rebuild.py`. Add a line like
  `('2027-03-15', 'Description'),` to mark a new shock.
- **Rule summaries** for auto-discovered contracts can be tidied by adding them to `SEED` in
  `rebuild.py`. Optional; the generated ones are accurate, just terser.

## How it fails safely

- Every request is retried with backoff.
- If one source is down, that source is served from `cache/` and the page shows an amber notice
  naming it and the date its data runs to.
- Before anything is published, a **sanity gate** checks that the latest data is no more than 4
  days old, that no live contract's last real observation lags by more than 6 days, that every
  value is between 0 and 100%, and that a live Kalshi contract and the NY Fed series are present.
- If the gate fails, the run stops and the previous page stays live. GitHub emails you about failed
  scheduled runs (Settings → Notifications → Actions).

## Running it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python rebuild.py _site --cache cache
open _site/index.html            # macOS; use xdg-open on Linux
```

Add `--skip-gate` to write the page even if the checks fail (useful for testing). Running with an
empty or missing `cache/` refetches everything from scratch (about 30 seconds).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The workflow stopped running | GitHub pauses scheduled workflows after 60 days with no repository activity. The daily cache commit normally prevents this; if it has happened, re-enable it on the Actions tab. |
| `Permission denied to github-actions[bot]` on the commit step | Step 3 of setup was skipped. The page still deployed; only the cache commit failed. |
| Page shows 404 | Pages source is not set to **GitHub Actions** (step 4), or the first deploy has not finished. |
| `Branch "x" is not allowed to deploy to github-pages` | The workflow ran from a non-default branch. Run it from `main`, or add the branch under Settings → Environments → github-pages. |
| Amber "could not be refreshed" notice | A source blocked or failed for that run. It clears itself on the next good run. If it persists and data passes 6 days old, the gate fails the run rather than publish stale data. |
| `SANITY GATE FAILED` in the log | The log lists which check failed. Usually a source outage; rerun later with **Run workflow**. |
| Kalshi `429 too many requests` | Rate limiting. The script retries with backoff; a rerun normally succeeds. |

## Method notes

- **Kalshi**: daily last-trade close from the trade API candlesticks (settled contracts via the
  historical endpoint), forward-filled across no-trade days, plus today's last trade.
- **Polymarket**: daily CLOB price history of the Yes token.
- Both are mapped to the US trading day they describe and sampled at the last observation of each
  Friday-ending week. The latest row is the partial current week.
- Kalshi resolves on two consecutive negative quarters of real GDP (BEA). Polymarket also resolves
  Yes on an NBER declaration, and its "by end of" contracts are cumulative from Q2 2025. The
  single-year Polymarket figure is (P<sub>Y</sub> − P<sub>Y−1</sub>) / (1 − P<sub>Y−1</sub>).
- **NY Fed**: probit on the 10y–3m spread, re-dated from the target month to the month the
  forecast was made. It estimates P(in recession 12 months ahead), which is a different quantity
  from a calendar-year contract, so the comparison is directional.
- **Track record**: Brier score of weekly prices against outcomes, compared with a flat 15% base
  rate (roughly the post-1945 frequency of NBER recession starts per year). The sample is small and
  all resolved contracts so far settled No, so treat it as descriptive.
