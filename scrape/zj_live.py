#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浙江省 · 杭州市领导留言板 —— 实时统揽数据管线
=================================================
两种模式，产物都是同一个 data.json（大屏直接读取）：

  python zj_live.py --mode seed   --csv ../shenghang_zhejiang_hangzhou.csv --out ../data.json
      # 一次性：用已抓的历史 CSV 建立趋势底图 + 设置 watermark（每版块已见最大 tid）

  python zj_live.py --mode update --data ../data.json --out ../data.json
      # 每 10 分钟：只抓每个版块比 watermark 更新的留言，去重累积进 data.json

设计要点
  * watermark：每个 fid 记录"已见最大 tid"。tid 随时间单调增大，
    所以"新留言"= tid > watermark[fid]，无需保存全部已见 id 即可去重。
  * 聚合（byMonth/byDomain/byStatus/byType/byForum/byDistrict/totals）持续累加 → 趋势越来越丰富。
  * recent：滚动保留最新 RECENT_CAP 条，供大屏滚动栏 / 地图点位 / 今日新增计算。
"""
import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

CN_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
RECENT_CAP = 600                      # 滚动保留的最新留言条数
PAGES_PER_FORUM = 1                   # 每次 update 每版块翻几页（最新 1 页 ≈ 10 条，10 分钟增量足够；反爬越轻越好）
PAGE_SIZE = 20

URL = "https://liuyan.people.com.cn/pro-dfbbs-front/threads/queryThreadsList"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://liuyan.people.com.cn/pro-dfbbs-front/forum/list?fid=14",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

# fid -> (显示名, 层级, 杭州区县名[用于地图，市级为 None])
# 层级: city / district —— 仅杭州市级 + 8 个主城区（去掉省级与富阳/临安/桐庐/淳安/建德外围）
TARGET = {
    146:  ("杭州市",              "city",     None),
    1007: ("杭州市委书记刘捷",     "city",     None),
    1008: ("杭州市市长姚高员",     "city",     None),
    4171: ("上城区委书记",        "district", "上城区"),
    4170: ("拱墅区委书记",        "district", "拱墅区"),
    4174: ("西湖区委书记",        "district", "西湖区"),
    4175: ("滨江区委书记",        "district", "滨江区"),
    4176: ("萧山区委书记",        "district", "萧山区"),
    4177: ("余杭区委书记",        "district", "余杭区"),
    5193: ("临平区委书记",        "district", "临平区"),
    5194: ("钱塘区委书记",        "district", "钱塘区"),
}
# 地图展示的 8 个主城区（前端据此过滤 geojson）
MAIN_DISTRICTS = ["上城区", "拱墅区", "西湖区", "滨江区", "萧山区", "余杭区", "临平区", "钱塘区"]
DISTRICT_OF = {fid: meta[2] for fid, meta in TARGET.items() if meta[2]}

# 各区街道核心名（数据源无街道字段，仅能从留言正文匹配 → 覆盖率有限，约 20%）
# 已剔除与区名重复的歧义词，降低误命中
DISTRICT_STREETS = {
    "上城区": ["湖滨", "清波", "小营", "望江", "紫阳", "南星", "采荷", "凯旋", "四季青", "笕桥", "彭埠", "丁兰", "九堡", "闸弄口"],
    "拱墅区": ["米市巷", "湖墅", "小河", "和睦", "拱宸桥", "大关", "祥符", "半山", "上塘", "康桥", "东新", "长庆", "天水", "武林", "朝晖", "文晖"],
    "西湖区": ["北山", "西溪", "翠苑", "灵隐", "留下", "转塘", "蒋村", "文新", "古荡", "三墩", "双浦"],
    "滨江区": ["西兴", "长河", "浦沿"],
    "萧山区": ["城厢", "北干", "蜀山", "新塘", "闻堰", "宁围", "新街", "衙前", "瓜沥", "党湾", "益农", "靖江", "南阳", "河庄", "义蓬", "临浦", "戴村", "楼塔", "河上", "进化", "所前", "浦阳"],
    "余杭区": ["五常", "仓前", "闲林", "中泰", "仁和", "良渚", "径山", "瓶窑", "百丈", "鸬鸟", "黄湖"],
    "临平区": ["南苑", "东湖", "星桥", "乔司", "运河", "塘栖", "崇贤"],
    "钱塘区": ["下沙", "白杨", "前进", "临江", "新湾", "河庄", "义蓬"],
}


def now_cn():
    return datetime.now(CN_TZ)


SESSION = requests.Session()


def warmup(fid=14):
    """先 GET 一次版块页，拿到 WAF/会话 Cookie，再发 POST 接口，能显著降低 403。"""
    try:
        SESSION.get(f"https://liuyan.people.com.cn/pro-dfbbs-front/forum/list?fid={fid}",
                    headers={"User-Agent": HEADERS["User-Agent"],
                             "Accept-Language": HEADERS["Accept-Language"]},
                    timeout=20)
    except Exception as e:
        print(f"  [warmup 失败] {e}", file=sys.stderr)


def fetch_batch(fid, last_item=0, delay=1.5):
    """抓一页，返回 (items, ok)。403/429 长等待少重试，其余指数退避。"""
    for attempt in range(3):
        try:
            time.sleep(delay + random.uniform(0, delay))  # 间隔 + 抖动
            r = SESSION.post(URL, data={"fid": fid, "lastItem": last_item, "pageSize": PAGE_SIZE},
                             headers={**HEADERS, "Referer": f"https://liuyan.people.com.cn/pro-dfbbs-front/forum/list?fid={fid}"},
                             timeout=20)
            if r.status_code in (403, 429):
                # 限流：重新 warmup 拿新 Cookie，长等待后再试一次
                print(f"  [HTTP {r.status_code} 限流] fid={fid} attempt={attempt+1}", file=sys.stderr)
                time.sleep(8 * (attempt + 1))
                warmup(fid)
                continue
            if r.status_code != 200:
                print(f"  [HTTP {r.status_code}] fid={fid} attempt={attempt+1}", file=sys.stderr)
                time.sleep(delay * 2)
                continue
            text = r.text.strip()
            if not text:
                time.sleep(delay * 2)
                continue
            d = r.json()
            if d.get("result") != "success":
                print(f"  [API:{d.get('resultDesc')}] fid={fid}", file=sys.stderr)
                return [], False
            return d.get("responseData", []) or [], True
        except requests.exceptions.Timeout:
            print(f"  [超时] fid={fid} attempt={attempt+1}", file=sys.stderr)
            time.sleep(delay * 2)
        except Exception as e:
            print(f"  [异常] fid={fid}: {e}", file=sys.stderr)
            time.sleep(delay * 2)
    return [], False


def g5(x):
    """满意度评分归一到 1-5，无效返回 0。"""
    try:
        v = int(x)
        return v if 1 <= v <= 5 else 0
    except (TypeError, ValueError):
        return 0


def parse_item(it, fid):
    """API 原始 item -> 大屏用扁平记录。"""
    label, level, district = TARGET.get(fid, (str(fid), "other", None))
    try:
        dt = datetime.fromtimestamp(it.get("dateline", 0), CN_TZ)
        date = dt.strftime("%Y-%m-%d")
    except Exception:
        date = ""
    return {
        "id": str(it.get("tid")),
        "tid": int(it.get("tid") or 0),
        "date": date,
        "fid": fid,
        "forum": label,
        "level": level,
        "district": district,
        "title": (it.get("subject") or "").strip(),
        "content": (it.get("content") or "").replace("\n", " ").replace("\r", "").strip(),
        "domain": it.get("domainName") or "其他",
        "type": it.get("typeName") or "其他",
        "status": it.get("stateInfo") or "待回复",
        "hasReply": bool(it.get("answerId")),
        "replyOrg": it.get("answerOrganization") or "",
        "replyContent": (it.get("answerContent") or "").replace("\n", " ").strip(),
        "likes": int(it.get("favNum") or 0),
        "ip": it.get("ip") or "",
        "nick": it.get("nickName") or "",
        "gManner": g5(it.get("gradeManner")),
        "gSpeed": g5(it.get("gradeSpeed")),
    }


# ---------------------------------------------------------------- 聚合

def blank_store():
    return {
        "updated": "",
        "source": "",
        "watermark": {},
        "totals": {"all": 0, "replied": 0},
        "byMonth": {},
        "byDomain": {},
        "byStatus": {},
        "byType": {},
        "byForum": {},
        "byDistrict": {},
        "sat": {"mSum": 0, "mCnt": 0, "sSum": 0, "sCnt": 0, "dist": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}},
        "kw": {},          # 诉求热词：word -> count（写盘时裁剪到 TOP）
        "districts": {},   # 各主城区画像：name -> {count,sat,byDomain,byStatus,kw,low}
        "low": [],         # 全局低分留言（态度/速度 ≤2★），写盘裁剪
        "pending": [],     # 未办结池（待回复/处理中/办理中），供风险预警，写盘裁剪
        "recent": [],
    }


def blank_district():
    return {"count": 0,
            "sat": {"mSum": 0, "mCnt": 0, "sSum": 0, "sCnt": 0, "dist": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}},
            "byMonth": {}, "byDomain": {}, "byStatus": {}, "byType": {}, "byStreet": {}, "kw": {}, "low": []}


def is_low(rec):
    """态度或速度被打了 1–2 星 = 低分留言。"""
    gm, gs = rec.get("gManner", 0), rec.get("gSpeed", 0)
    return 1 <= gm <= 2 or 1 <= gs <= 2


def low_msg(rec):
    return {"tid": rec["tid"], "title": rec["title"], "content": (rec["content"] or "")[:140],
            "date": rec["date"], "forum": rec["forum"], "district": rec.get("district"),
            "gm": rec.get("gManner", 0), "gs": rec.get("gSpeed", 0), "status": rec["status"]}


PENDING_SET = {"待回复", "处理中", "办理中", "受理", "已受理"}  # 未办结状态


def is_pending(rec):
    return rec.get("status") in PENDING_SET


def pending_msg(rec):
    return {"tid": rec["tid"], "title": rec["title"], "content": (rec["content"] or "")[:240],
            "date": rec["date"], "forum": rec["forum"], "district": rec.get("district"),
            "domain": rec["domain"], "type": rec["type"], "status": rec["status"]}


def _add_sat(sat, rec):
    if rec.get("gManner"):
        sat["mSum"] += rec["gManner"]; sat["mCnt"] += 1
        sat["dist"][str(rec["gManner"])] = sat["dist"].get(str(rec["gManner"]), 0) + 1
    if rec.get("gSpeed"):
        sat["sSum"] += rec["gSpeed"]; sat["sCnt"] += 1


# ---- 中文分词热词（jieba 懒加载；缺失则跳过，不阻塞抓取）----
_JIEBA = None
STOPWORDS = set("""
的 了 是 我 你 他 她 它 们 在 有 和 与 及 或 这 那 个 也 都 就 还 又 而 被 把 给 让 向 从 对 为 以 于
请 问 问题 关于 一个 怎么 如何 为什么 希望 能否 可以 是否 没有 什么 哪里 这个 那个 我们 你们 他们
情况 处理 解决 反映 咨询 投诉 求助 建议 建言 谢谢 感谢 领导 您好 你好 麻烦 现在 已经 一直 一下 这样
浙江 浙江省 杭州 杭州市 市 区 县 镇 乡 村 街道 小区 这边 那边 目前 相关 部门 单位 关于 以及 进行 是否
要求 上城区 拱墅区 西湖区 滨江区 萧山区 余杭区 临平区 钱塘区 富阳区 临安区 桐庐县 淳安县 建德市 宁波 温州 嘉兴 湖州 绍兴 金华 衢州 舟山 台州 丽水
""".split())


def extract_words(text):
    global _JIEBA
    if _JIEBA is None:
        try:
            import jieba
            jieba.setLogLevel(60)
            _JIEBA = jieba
        except Exception:
            _JIEBA = False
    if not _JIEBA or not text:
        return []
    out = []
    for w in _JIEBA.cut(text):
        w = w.strip()
        if len(w) >= 2 and w not in STOPWORDS and not w.isdigit():
            out.append(w)
    return out


HANDLED = {"已办理", "已回复", "已解决", "办结", "已办结"}  # 视为"已办结"的状态


def is_handled(rec):
    return rec["status"] in HANDLED or rec["hasReply"]


def _inc(d, key, n=1):
    if not key:
        return
    d[key] = d.get(key, 0) + n


def add_records(store, records):
    """把一批新记录累加进 store（聚合 + recent 滚动 + watermark）。返回新增条数。

    watermark 只用于"跨轮次"去重：本批以处理前的快照 base_wm 为准，批内用 id 集合去重，
    避免 CSV/分页乱序时低 tid 行被同批刚写入的高 watermark 误杀。"""
    base_wm = {k: v for k, v in store["watermark"].items()}
    batch_ids = set()
    added = 0
    for rec in records:
        fid = rec["fid"]
        if rec["tid"] <= base_wm.get(str(fid), 0):
            continue          # 上一轮已计入
        if rec["id"] in batch_ids:
            continue          # 同批重复
        batch_ids.add(rec["id"])
        added += 1
        store["totals"]["all"] += 1
        if is_handled(rec):
            store["totals"]["replied"] += 1
        ym = rec["date"][:7] if rec["date"] else ""
        _inc(store["byMonth"], ym)
        _inc(store["byDomain"], rec["domain"])
        _inc(store["byStatus"], rec["status"])
        _inc(store["byType"], rec["type"])
        # byForum 带元数据
        f = store["byForum"].setdefault(rec["forum"], {"count": 0, "fid": fid, "level": rec["level"]})
        f["count"] += 1
        if rec["district"]:
            _inc(store["byDistrict"], rec["district"])
        # 全局满意度 + 热词 + 低分
        _add_sat(store["sat"], rec)
        words = extract_words(rec.get("title", ""))
        for w in words:
            store["kw"][w] = store["kw"].get(w, 0) + 1
        if is_low(rec):
            store["low"].append(low_msg(rec))
        if is_pending(rec):
            store["pending"].append(pending_msg(rec))
        # 各主城区画像
        if rec.get("district"):
            dd = store["districts"].setdefault(rec["district"], blank_district())
            for _k, _v in blank_district().items():
                dd.setdefault(_k, _v)   # 兼容旧 schema：补齐缺失键，防 KeyError
            dd["count"] += 1
            _inc(dd["byMonth"], ym)
            _add_sat(dd["sat"], rec)
            _inc(dd["byDomain"], rec["domain"])
            _inc(dd["byStatus"], rec["status"])
            _inc(dd["byType"], rec["type"])
            # 街道：正文/标题匹配该区街道名（首个命中），覆盖有限
            _txt = (rec.get("title", "") or "") + (rec.get("content", "") or "")
            for _s in DISTRICT_STREETS.get(rec["district"], []):
                if _s in _txt:
                    dd["byStreet"][_s] = dd["byStreet"].get(_s, 0) + 1
                    break
            for w in words:
                dd["kw"][w] = dd["kw"].get(w, 0) + 1
            if is_low(rec):
                dd["low"].append(low_msg(rec))
        store["recent"].append(rec)
        # 推进 watermark（用当前库内最大值，本批结束后生效）
        if rec["tid"] > store["watermark"].get(str(fid), 0):
            store["watermark"][str(fid)] = rec["tid"]
    # recent 去重 + 排序 + 截断
    seen, dedup = set(), []
    for r in sorted(store["recent"], key=lambda x: x["tid"], reverse=True):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        dedup.append(r)
    store["recent"] = dedup[:RECENT_CAP]
    return added


# ---------------------------------------------------------------- 模式

def mode_seed(csv_path, out_path):
    if not os.path.exists(csv_path):
        sys.exit(f"找不到 CSV: {csv_path}")
    store = blank_store()
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                fid = int(row.get("forum_fid") or 0)
            except ValueError:
                continue
            if fid not in TARGET:
                continue   # 只保留杭州市级 + 8 主城区
            label, level, district = TARGET[fid]
            try:
                tid = int(row.get("tid") or 0)
            except ValueError:
                continue
            rows.append({
                "id": str(tid), "tid": tid, "date": (row.get("date") or "")[:10],
                "fid": fid, "forum": label, "level": level, "district": district,
                "title": row.get("subject") or "", "content": row.get("content") or "",
                "domain": row.get("domain") or "其他", "type": row.get("type") or "其他",
                "status": row.get("status") or "待回复",
                "hasReply": str(row.get("has_reply")) in ("1", "True", "true"),
                "replyOrg": row.get("reply_org") or "", "replyContent": row.get("reply_content") or "",
                "likes": int(row.get("fav_num") or 0) if (row.get("fav_num") or "").isdigit() else 0,
                "ip": row.get("ip") or "", "nick": "",
                "gManner": g5(row.get("grade_manner")), "gSpeed": g5(row.get("grade_speed")),
            })
    add_records(store, rows)
    store["source"] = "seed"
    store["updated"] = now_cn().isoformat(timespec="seconds")
    write_store(store, out_path)
    print(f"[seed] {len(rows)} 行 → 累计 {store['totals']['all']} 条 | "
          f"{len(store['byForum'])} 版块 | {len(store['byMonth'])} 个月 → {out_path}")


def mode_update(data_path, out_path, delay, pages=PAGES_PER_FORUM):
    store = load_store(data_path)
    warmup()  # 取一次会话 Cookie
    new_records = []
    for fid in TARGET:
        wm = store["watermark"].get(str(fid), 0)
        last = 0
        page_new = []
        for _ in range(pages):
            items, ok = fetch_batch(fid, last, delay)
            if not ok or not items:
                break
            batch = [parse_item(it, fid) for it in items]
            fresh = [r for r in batch if r["tid"] > wm]
            page_new.extend(fresh)
            last = batch[-1]["tid"]
            if len(fresh) < len(batch):
                break  # 已翻到 watermark 以内，无需再翻
        new_records.extend(page_new)
        label = TARGET[fid][0]
        print(f"  {label[:14]:<14} +{len(page_new)}", file=sys.stderr)
    added = add_records(store, new_records)
    store["source"] = "update"
    store["updated"] = now_cn().isoformat(timespec="seconds")
    write_store(store, out_path)
    print(f"[update] 本轮新增 {added} 条 → 累计 {store['totals']['all']} 条 → {out_path}")


def load_store(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
            for k, v in blank_store().items():
                s.setdefault(k, v)
            return s
        except Exception as e:
            print(f"[警告] 读取 {path} 失败({e})，重建空库", file=sys.stderr)
    return blank_store()


def _topn(d, n):
    return dict(sorted(d.items(), key=lambda x: -x[1])[:n])


def _trim_low(lst, n):
    seen, out = set(), []
    for m in sorted(lst, key=lambda x: -x.get("tid", 0)):
        if m["tid"] in seen:
            continue
        seen.add(m["tid"]); out.append(m)
    return out[:n]


def write_store(store, path):
    # 全局热词 TOP400、低分留言 TOP80
    if len(store.get("kw", {})) > 400:
        store["kw"] = _topn(store["kw"], 400)
    store["low"] = _trim_low(store.get("low", []), 80)
    store["pending"] = _trim_low(store.get("pending", []), 400)
    # 各区：热词 TOP40、领域 TOP10、低分留言 TOP30
    for dd in store.get("districts", {}).values():
        if len(dd.get("kw", {})) > 40:
            dd["kw"] = _topn(dd["kw"], 40)
        if len(dd.get("byDomain", {})) > 10:
            dd["byDomain"] = _topn(dd["byDomain"], 10)
        if len(dd.get("byType", {})) > 8:
            dd["byType"] = _topn(dd["byType"], 8)
        if len(dd.get("byStreet", {})) > 15:
            dd["byStreet"] = _topn(dd["byStreet"], 15)
        dd["low"] = _trim_low(dd.get("low", []), 30)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser(description="浙江·杭州留言板实时统揽数据管线")
    ap.add_argument("--mode", choices=["seed", "update"], default="update")
    ap.add_argument("--csv", default="../shenghang_zhejiang_hangzhou.csv")
    ap.add_argument("--data", default="../data.json")
    ap.add_argument("--out", default="../data.json")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--pages", type=int, default=PAGES_PER_FORUM, help="每版块翻几页（首次回填可调大，如 5）")
    args = ap.parse_args()
    if args.mode == "seed":
        mode_seed(args.csv, args.out)
    else:
        mode_update(args.data, args.out, args.delay, args.pages)


if __name__ == "__main__":
    main()
