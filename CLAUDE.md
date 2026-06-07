# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **real-time situational-awareness wall (一屏统揽)** for the 杭州市 leadership message board
on 人民网 (`liuyan.people.com.cn`). It scrapes the latest citizen petitions every 10 minutes across
**11 forums** (Hangzhou city board + 市委书记 + 市长 + the **8 main urban districts** 上城/拱墅/西湖/
滨江/萧山/余杭/临平/钱塘), accumulates them, and renders a deep-space / amber-gold sci-fi dashboard that
auto-refreshes. Fully free, no third-party accounts. (Earlier versions also covered 浙江省 boards and the
5 outer districts 富阳/临安/桐庐/淳安/建德 — both were intentionally dropped to focus on the urban core.)

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

- **`TARGET` dict** (`fid → (label, level, district)`) is the authoritative scope (now 11: city +
  8 districts). `level` is city/district; `district` is the geojson region name used by the map.
  `MAIN_DISTRICTS` lists the 8 the frontend filters the geojson to. Change coverage here, then re-seed
  AND reset the data branch (delete it + re-run the Action) so the old scope doesn't linger.
- **Per-district profiles** (`districts{name:{count,sat,byDomain,byStatus,byType,kw,low}}`) power both the
  click-to-compare modal AND the **auto-carousel linkage** (see frontend); **low-score messages**
  (`low[]` globally + per district, items rated ≤2★ on manner or speed) power the 督办重点 lists. Built in
  `add_records` (which also back-fills missing keys for old-schema districts), pruned in `write_store`.
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
- **Satisfaction** (`sat`): `gradeManner`/`gradeSpeed` are 1–5★ ratings (~49% of items rated; ~84%
  give 4–5). Accumulated as sums/counts + a 1–5 distribution → drives the 群众满意度 gauge.
- **Keywords** (`kw`): `extract_words()` runs **jieba** (lazy import, no-op if missing) over留言 titles,
  minus `STOPWORDS` → accumulated word→count, pruned to TOP 400 on write → drives the wordcloud.
  `jieba` is a real dependency now (requirements.txt + workflow `pip install`).
- **data.json schema**: `{updated, source, watermark{fid:tid}, totals{all,replied}, byMonth, byDomain,
  byStatus, byType, byForum{label:{count,fid,level}}, byDistrict{name:n}, sat{mSum,mCnt,sSum,sCnt,dist},
  kw{word:count}, districts{name:{count,sat,byDomain,byStatus,byType,kw,low}}, low[≤80], recent[≤600 newest]}`.
  The frontend reads exactly these keys — keep in sync with `index.html`'s render functions.
  (`byType` is still computed but its donut panel was removed.)

## `index.html` — the dashboard

- Self-contained: ECharts + echarts-wordcloud from a CDN (staticfile primary → jsdelivr fallback).
  Hangzhou geojson is served **locally** from `./hangzhou.geo.json` (committed, ~105KB; datav remote is
  only a fallback) — this fixed a "地图加载失败（网络受限）" where the viewer's network blocked datav.
  District centroids come from `feature.properties.center`. Wordcloud degrades to a top-keywords bar if
  the plugin fails to load.
- Panels: KPI strip (今日/本月新增, 办结率, 群众满意率 — always city-wide), 群众满意度 gauge + 1–5★
  评分分布 (`distBar`, shows the 1★ 差评/督办 share), 办理状态+诉求性质 dual donut (`donutMini`),
  杭州主城区热力地图, 月度趋势(+环比/同比 badges, city-wide), 诉求热词云, 最新留言 ticker, 诉求领域 TOP10.
- **Auto-carousel + linkage** (the headline behavior): the map auto-rotates through the 8 districts every
  5.5s (`startCar`), highlighting each + `showTip` with a rich `districtTip` callout (留言量/满意率 vs
  city/办结率/待回复/低分/热词). On each step `spotlight(name)` → `applyScope(district)` re-renders the
  satisfaction gauge+distBar, 领域, 热词, 办理状态, 诉求性质 to THAT district (panel headers show ▶区名 via
  `setTag`); the KPI strip stays city-wide. The rotation ends each lap on an `__ALL__` step → `showAll()`
  reverts every linked panel to 全市. Hover the map to pause; the click modal also pauses it.
- **Click-to-compare modal**: clicking a district calls `openDistrict(name)` → a scaled in-`#stage`
  overlay with that district's satisfaction gauge, 态度/速度, status, domain TOP, keyword cloud, and
  低分留言督办 list, plus a 较全市 delta badge. Builders `satGauge/statusDonut/domainBar/kwOption/donutMini/
  distBar` are shared by main panels, carousel, and modal; `MCH`/`mmk` own the modal's ECharts instances.
  Close via ✕ / backdrop / Esc.
- **Click-to-compare modal**: clicking a district on the map calls `openDistrict(name)` → a scaled
  in-`#stage` overlay showing that district's satisfaction gauge, 态度/速度, status, domain TOP, keyword
  cloud, and 低分留言督办 list, with a 较全市 (vs city-average) delta badge. Chart options come from the
  shared builders `satGauge/statusDonut/domainBar/kwOption` (used by both the main panels and the modal);
  `MCH`/`mmk` manage the modal's own ECharts instances. Close via ✕ / backdrop click / Esc.
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
