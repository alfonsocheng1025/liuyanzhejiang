# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **real-time situational-awareness wall (一屏统揽)** for the 浙江省 / 杭州市 leadership message board
on 人民网 (`liuyan.people.com.cn`). It scrapes the latest citizen petitions every 10 minutes across
**19 forums** (province board + 3 provincial leaders + Hangzhou city + 2 city leaders + 13 Hangzhou
districts/counties), accumulates them, and renders a deep-space / amber-gold sci-fi dashboard that
auto-refreshes. Fully free, no third-party accounts.

There is no framework and no build step: a Python data pipeline + a single static HTML dashboard +
one Vercel serverless read-function + a GitHub Actions cron.

## Data flow (read this first)

```
GitHub Actions (cron */10)  →  scrape/zj_live.py --mode update  →  data.json on the `data` branch
                                                                          │
Vercel: index.html  ──fetch /api/data──►  api/data.js  ──reads raw──►  data branch data.json
            └─ falls back to ./data.json (the seed baseline committed to main) if /api/data fails
```

- **Writer**: GitHub Actions runs `scrape/zj_live.py --mode update` every 10 min, pulls the previous
  accumulated `data.json` from the **`data` branch** (curl raw), merges new items, force-pushes a
  single-commit `data` branch (squashed each run → no history bloat, data accumulates *inside* the file).
- **Store = the git `data` branch** (NOT a database). Chosen over Upstash so the dataset stays
  downloadable for research. Requires a **public repo** (raw.githubusercontent.com must be readable;
  also makes Actions unlimited-free). Private-repo path = swap to Upstash (see README).
- **Reader**: `api/data.js` (Vercel function) auto-derives the data-branch raw URL from
  `VERCEL_GIT_REPO_OWNER`/`VERCEL_GIT_REPO_SLUG` (override with env `DATA_URL`). Returns 200 with
  `s-maxage=120`, or a non-200 so the frontend falls back to the bundled seed `./data.json`.
- **`vercel.json` sets `git.deploymentEnabled.data = false`** — critical: without it, the 144
  data-branch pushes/day would each trigger a Vercel deploy and blow the Hobby 100-deploys/day limit.

## Commands

```bash
# Build the historical baseline from the existing 3万-row CSV (one-time / on demand)
python scrape/zj_live.py --mode seed --csv shenghang_zhejiang_hangzhou.csv --out data.json

# One incremental cycle (what the cron runs). --pages>1 to backfill more on first sync.
python scrape/zj_live.py --mode update --data data.json --out data.json [--delay 2.0] [--pages 5]

# Preview the dashboard locally (or just double-click index.html)
python -m http.server 8099   # → http://127.0.0.1:8099/index.html
```

No lint/test/unit-test framework exists. Visual verification is done by screenshotting
`index.html` with Playwright against a local `http.server` (Playwright is not a committed dependency —
install ad hoc, it's gitignored).

## `scrape/zj_live.py` — things that matter when editing

- **`TARGET` dict** (`fid → (label, level, district)`) is the authoritative scope. `level` is
  province/city/district; `district` (Hangzhou only) is the geojson region name used by the map.
  Change the dashboard's coverage here.
- **Anti-bot is real.** Rapid sequential requests to all 19 forums trigger `HTTP 403` (WAF), even from
  a China IP. Mitigations already in place: a `requests.Session` + `warmup()` GET to grab cookies
  before POSTing, `PAGES_PER_FORUM = 1` (newest page only), `delay 1.5s + jitter`, and 403/429 →
  re-warmup + long sleep + few retries. Don't remove these; raise `--delay` if blocked.
- **Cursor pagination**: `POST .../queryThreadsList` with `fid`, `pageSize`, `lastItem` = the `tid` of
  the previous batch's last item (start 0). Newest items have the **largest** tids.
- **Dedup = per-forum `watermark`** (max tid seen). `add_records` snapshots the watermark *before* the
  batch and dedups within-batch by id set — this was a real bug: using the live watermark mid-batch
  made out-of-order/lower-tid rows get dropped (seed counted only ~21 of 30k). Keep the snapshot.
- **`is_handled()` drives 办结率** from `status` (CSV's `has_reply` column is unreliable / all-0).
- **data.json schema**: `{updated, source, watermark{fid:tid}, totals{all,replied}, byMonth, byDomain,
  byStatus, byType, byForum{label:{count,fid,level}}, byDistrict{name:n}, recent[≤600 newest]}`.
  The frontend reads exactly these keys — keep them in sync with `index.html`'s render functions.

## `index.html` — the dashboard

- Self-contained: ECharts from a CDN (staticfile primary → jsdelivr fallback), Hangzhou geojson from
  `geo.datav.aliyun.com/areas_v3/bound/330100_full.json` (district centroids come from
  `feature.properties.center`). All degrade gracefully if a CDN is unreachable.
- **Fixed 1920×1080 design canvas**, scaled to fit any screen via `transform: scale()` in `fit()`.
  Layout is a 3-column CSS grid; panels size with `flex`.
- Data source order is `['/api/data', './data.json']` (`DATA_SOURCES`); refresh every `REFRESH_MS`
  (10 min). Live clock ticks every second.
- Visual identity: deep-space gradient + amber-gold (`--gold #f5b942`), corner-bracket panels, glow.
  Status colors: 待回复=red, 处理中/办理中=blue, 已办理/已回复/已解决=amber/green.

## Repo / deploy notes

- This folder is its own git repo (the parent `Downloads` is an unrelated git root — ignore it).
- Deploy = push to a **public** GitHub repo → import on Vercel (zero env vars needed). Actions cron
  starts automatically; first manual run (Actions tab) reveals whether GitHub's overseas runners can
  reach people.com.cn. If 403-blocked, the fallback is running `--mode update` on a local always-on
  machine that pushes `data.json` to the `data` branch — identical code, only the runner moves.
- `浙江留言板专项分析.html` (old embedded-150-rows dashboard) and `zj_crawler.py` (old 92-forum full
  crawler) are kept for reference; the live system is `index.html` + `scrape/zj_live.py`.
- Known limitation: status is snapshot-at-ingest (a留言 ingested as 待回复 keeps that tally even after
  it's later handled, since old items aren't re-fetched).
