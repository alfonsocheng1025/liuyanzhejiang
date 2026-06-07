# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in this repository.

## What this is

A **real-time situational-awareness wall (一屏统揽) for the 杭州市 leadership message board** on 人民网
(`liuyan.people.com.cn`). Every 10 minutes it crawls the latest citizen petitions across **11 forums**
(杭州市 + 市委书记 + 市长 + the 8 main urban districts), geocodes them, scores social-risk points with an
LLM, and renders a deep-space / amber-gold ECharts dashboard that auto-refreshes. Fully free-tier; the
heavy work runs in GitHub Actions, Vercel only serves static files + 3 read-only functions.

No framework, no build step. Python pipeline + single static `index.html` + 3 Vercel functions + a cron.

## Architecture / data flow (read first)

```
GitHub Actions (cron */10):
  zj_live.py update  → data.json   (聚合+recent+各区画像+pending未办结池)
  geocode.py live    → geo.json    (实体→高德地理编码→街道留言量/满意度/质心/点位)  [AMAP_KEY secret]
  risk.py            → risk.json   (规则打分+聚类→DeepSeek研判+处置建议)            [DEEPSEEK_API_KEY secret]
  → force-push data.json/geo.json/risk.json + .geocache.json/.riskcache.json to the `data` branch

Vercel (serves `main`): index.html
  fetch /api/data  → api/data.js → raw data branch data.json  (fallback ./data.json seed)
  fetch /api/geo   → api/geo.js  → raw data branch geo.json   (fallback ./geo.json)
  fetch /api/risk  → api/risk.js → raw data branch risk.json  (fallback ./risk.json)
```

- **The `data` branch is the database.** The 10-min Action force-pushes a single squashed commit each run
  (no history bloat; data accumulates *inside* the files). `vercel.json` sets
  `git.deploymentEnabled.data=false` so those 144 pushes/day don't trigger Vercel deploys.
- **Repo must be public** (raw.githubusercontent must be readable; also makes Actions unlimited-free).
- **Seeds on `main`** (`data.json`/`geo.json`/`risk.json`) are the historical baseline + Vercel fallback;
  the Action continues from the data-branch versions, falling back to these seeds on first run.

## Commands

```bash
# Seed data.json from the 3万-row CSV (one-time / on demand)
python scrape/zj_live.py --mode seed --csv shenghang_zhejiang_hangzhou.csv --out data.json
# One incremental crawl cycle (what the cron runs)
python scrape/zj_live.py --mode update --data data.json --out data.json [--delay 2.0] [--pages 5]
# Geocode (needs AMAP_KEY env). full=历史全量(缓存后秒级)  live=只编码新留言
python scrape/geocode.py --mode full|live --out geo.json [--dry-run 只统计实体覆盖率]
# Risk (needs DEEPSEEK_API_KEY env; --no-llm 只跑规则)
python scrape/risk.py --data data.json --out risk.json
# Preview the dashboard (or double-click index.html)
python -m http.server 8099   # → http://127.0.0.1:8099/index.html
```

No lint/test framework. Visual checks are done by screenshotting `index.html` with Playwright against a
local `http.server` (Playwright is NOT committed — install ad hoc, it's gitignored).

## Scope (TARGET) & data ranges

- `TARGET` in `zj_live.py` (also mirrored as `MAIN_DISTRICTS`/`ADCODE`) is authoritative: 11 fids —
  146 杭州市, 1007 市委书记, 1008 市长, 4171上城/4170拱墅/4174西湖/4175滨江/4176萧山/4177余杭/5193临平/5194钱塘.
- Monthly trend spans ~2007→now (city board long history); district boards mostly 2021+.
- **`pending` pool** = newest ~400 未办结(待回复/处理中/办理中). Reality: the 11 forums' unresolved are
  almost all 2018–2022 backlog (recent petitions get handled fast), so risk skews to 历史积压 — each risk
  point carries `fresh` (newest member ≤365d) so the UI labels 现行 vs 历史积压.

## Pipeline gotchas (don't re-break)

- **Anti-bot**: rapid requests to all forums trigger HTTP 403 even from a China IP. `zj_live.fetch_batch`
  uses a Session + `warmup()` cookie GET, 1 page/forum, delay+jitter, 403/429→re-warmup+sleep. Don't remove.
- **Cursor pagination**: `lastItem` = previous batch's last `tid` (newest have largest tids).
- **Watermark dedup**: per-forum max tid; `add_records` snapshots it *before* the batch + dedups by id set
  (using the live watermark mid-batch silently dropped out-of-order rows — a real past bug).
- **Status is snapshot-at-ingest** (老留言不回头刷新状态) → 办结率/待回复 are point-in-time.
- **No street polygons exist** (datav only to district level). Street granularity = geocoded points
  (`geocode.py`: 实体正则 → 高德 /v3/geocode/geo + /regeo → 经纬度+街道; ~70% coverage). `geo.json`
  per-district: `byStreet`(量) / `streetSat`(态度均分) / `streetGeo`(街道质心) / `points`(热力点).
- **Caches persist on the data branch** (.geocache.json / .riskcache.json) so geocoding/LLM don't repeat.

## data.json / geo.json / risk.json schema → see 技术与操作手册.md (exhaustive field list & every metric).

## Frontend (index.html) key behaviors

- Fixed 1920×1080 canvas scaled via `fit()`; ECharts + echarts-wordcloud from CDN (staticfile→jsdelivr).
- **Carousel** (`startCar`, 5.5s): rotates the 8 districts then a 全市 step. Each step `spotlight()`→
  `applyScope()` re-renders 满意度仪表/评分分布, 领域, 热词(去本区名 `stripKw`), 办理状态, 诉求性质,
  各区满意率排行(高亮当前区), 月度趋势(按区), and the center map drills into that district.
- **District map** = fancy double-layer glowing geo polygon (`renderDistFocus`) + street bubbles
  (color=satisfaction, size=留言量, label=街道·条数). The 8-district overview shrinks to a top-left inset
  (`maparea.focus`) with names hidden + a red dot (`setInsetDot`) marking the current district.
- **Per-district info box** (`distInfo`, top-right, `distInfoHTML`): stats + 社会风险研判+应对+相关留言原文;
  turns red (`.crisis`) for districts with a 红 risk point; pops in per carousel step.
- **Bottom marquee** = 最新诉求 (recent + date), `renderRecentBar`.
- Click a district → full `openDistrict` modal (satisfaction/status/domain/wordcloud/街道分布/低分/本区研判).
- Data source order: `/api/*` then `./*.json`. Loaded in `boot()` (GEO/RISK), merged each `refresh()`.

## Secrets & keys

- GitHub Actions secrets: `AMAP_KEY` (高德 REST 地理编码), `DEEPSEEK_API_KEY` (deepseek-v4-pro 研判). These
  run server-side only — never in the repo or the page. Rotate via the providers' consoles + `gh secret set`.
- A 高德 **JS API** key was briefly hardcoded for a map-tile basemap, since removed (the dashboard uses
  glowing polygons, not Amap tiles). It survives in git history — rotate/delete it in the 高德 console.

## Repo notes

- This folder is its own git repo. Deploy = push public repo → import on Vercel (zero env vars). Cron is
  `.github/workflows/crawl.yml`; first manual run (Actions tab) confirms overseas reachability of people.com.cn.
- Old reference files kept: `浙江留言板专项分析.html` (original 150-row dashboard), `zj_crawler.py` (92-forum
  full crawler). Live system = `index.html` + `scrape/{zj_live,geocode,risk}.py`.
- After changing TARGET or any schema, re-seed AND reset the data branch (delete it + re-run the Action),
  else the old scope/schema lingers on the data branch.
