# 浙江省 · 杭州市领导留言板 · 实时统揽大屏

每 10 分钟自动抓取**浙江省 + 杭州市 + 杭州市下属 13 个区县**(共 19 个版块)领导留言板的最新留言,
累积入库后展示在一块深空+琥珀金风格的科技感大屏上。全套**免费**,无需任何第三方账号。

```
GitHub Actions (每10分钟跑 Python 抓取)
        │  增量抓最新留言 → 去重累积
        ▼
   data 分支 / data.json   ←─ 既是大屏数据源，也是可下载的研究数据集
        │
        ▼
Vercel (/api/data 读取)  →  index.html 大屏 (每10分钟自动刷新)
```

## 文件结构

| 文件 | 说明 |
|---|---|
| `index.html` | 大屏前端(ECharts + 杭州各区县发光地图),Vercel 部署、双击亦可本地看 |
| `api/data.js` | Vercel 函数:读取 data 分支最新数据,失败回落到内置种子 |
| `scrape/zj_live.py` | 数据管线:`seed`(用历史 CSV 建底图)/ `update`(增量抓最新) |
| `.github/workflows/crawl.yml` | GitHub Actions,每 10 分钟跑一次 `update` 并推到 data 分支 |
| `data.json` | 种子数据(由 CSV 生成的历史底图),上线后 data 分支会在此基础上累积 |
| `shenghang_zhejiang_hangzhou.csv` | 已抓的 ~3 万条历史数据(seed 的输入) |
| `vercel.json` | Vercel 配置(关闭 data 分支的自动部署,避免触发重建) |
| `zj_crawler.py` | 旧版全量爬虫(92 版块),保留备用 |

## 本地预览

```bash
# 1) 用历史 CSV 生成底图(已生成可跳过)
python scrape/zj_live.py --mode seed --csv shenghang_zhejiang_hangzhou.csv --out data.json

# 2) 起本地服务后浏览器打开（直接双击 index.html 也行，会读同目录 data.json）
python -m http.server 8099
#   → http://127.0.0.1:8099/index.html

# 3) 手动抓一轮最新数据看看增量
python scrape/zj_live.py --mode update --data data.json --out data.json
```

## 部署到线上(一次性配置)

> ⚠️ **仓库需设为 Public**:① data 分支的 `data.json` 要能被 `raw.githubusercontent.com` 公开读取;
> ② Public 仓库的 GitHub Actions 完全免费、不计分钟数。(留言板数据本就是公开信息。)
> 若必须私有,见文末"私有仓库方案"。

1. **推到 GitHub**:新建一个 **Public** 仓库,把本目录推上去(`git init` 已就绪,见下)。
2. **连 Vercel**:在 vercel.com 选 *Add New → Project → Import* 该仓库,直接 Deploy。
   几十秒后得到大屏网址(`https://xxx.vercel.app`)。无需任何环境变量——`/api/data` 会自动按仓库名推断 data 分支地址。
3. **定时抓取自动启动**:`.github/workflows/crawl.yml` 已配好,每 10 分钟自动跑。
   想立即验证,去仓库 **Actions → crawl → Run workflow** 手动触发一次。

### 关键一步:验证境外可达性

GitHub Actions 的服务器在境外,**首次手动触发后去看运行日志**:
- 若各版块大多 `+N`(有新增)→ 境外可达,大功告成,以后全自动。
- 若大量 `HTTP 403`→ 被 people.com.cn 拦了境外 IP,改用下面的**本地兜底**。

### 本地兜底(境外被拦时)

抓取脚本放你本地常开的电脑跑(代码完全不变),只把结果推到 data 分支即可:

```powershell
# 在本目录,定时执行(可挂到 Windows 任务计划程序，每10分钟一次)
python scrape/zj_live.py --mode update --data data.json --out data.json
git add data.json; git commit -m "data"; git push -f origin HEAD:data
```

Vercel 端无需任何改动,大屏照常从 data 分支读数。

## 重新生成历史底图 / 调整范围

- **改抓取范围**:编辑 `scrape/zj_live.py` 顶部的 `TARGET` 字典(fid → 名称/层级/区县)。
- **首次回填多页**:`python scrape/zj_live.py --mode update --pages 5`(稳态 1 页即可)。
- **重建底图**:重跑 `--mode seed`。

## 已知约定

- **办理状态是"入库时快照"**:留言被收录时是"待回复",后来被办结了也不会回头改写历史计数。
  对实时监测足够,做严谨研究时请以 data 分支累积的原始 `recent` 记录为准。
- **定时不精确**:GitHub 免费版定时任务高峰期可能延迟数分钟或偶尔跳过,属正常现象。
- **去重靠 watermark**:每版块记录已见最大 `tid`,只收 `tid` 更大的新留言;
  上线初期若某版块短时间新增 >10 条,可能漏中间几条,用 `--pages` 调大补抓。

## 私有仓库方案(可选)

若不能用 Public 仓库:把 data 分支换成 Upstash Redis(Vercel Marketplace 免费档),
`scrape/zj_live.py` 增加一个写 Upstash REST 的分支、`api/data.js` 改为读 Upstash(服务端持 token)。
告诉我即可切换。
