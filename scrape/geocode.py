#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实体提取 + 高德地理编码 —— 把留言落点到街道/经纬度，补强街道级数据
=================================================================
数据源没有街道字段、datav 也无街道边界。本脚本：
  1) 从留言正文/标题正则提取地点实体（小区/花园/路/公司/医院/学校…）
  2) 调高德地理编码 API（需 AMAP_KEY 环境变量）得到 经纬度 + 所属街道(township)
  3) 写入 data.json：各区 byStreet 用地理编码结果覆盖（更准），并新增 points 点位用于热力图
  4) 实体→结果 落盘缓存(.geocache.json)，重复小区不重复请求；限速友好

用法：
  set AMAP_KEY=你的高德web服务key            # Windows PowerShell: $env:AMAP_KEY="..."
  python geocode.py --csv ../shenghang_zhejiang_hangzhou.csv --data ../data.json --out ../data.json
  python geocode.py --dry-run                 # 只统计实体抽取覆盖率，不联网

申请 key：https://lbs.amap.com → 控制台 → 应用管理 → 新建「Web服务」类型 key（免费）
"""
import argparse
import csv
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zj_live import TARGET, load_store, write_store  # 复用版块定义与读写

CACHE_FILE = ".geocache.json"
AMAP_GEO = "https://restapi.amap.com/v3/geocode/geo"
AMAP_REGEO = "https://restapi.amap.com/v3/geocode/regeo"

# 地点/POI 实体：2-8 个字 + 常见后缀
POI_RE = re.compile(
    r'[一-龥A-Za-z0-9]{2,8}'
    r'(?:小区|花园|公寓|大厦|广场|家园|名苑|苑|公馆|新村|村|社区|路|街|大道|'
    r'工业园|产业园|科技园|创业园|商务区|公司|工厂|厂|医院|卫生院|学校|学院|大学|'
    r'中学|小学|幼儿园|市场|中心|站|桥)')
# 噪声词（提取到也无意义）
NOISE = {"该公司", "贵公司", "本公司", "物业公司", "开发商", "建筑公司", "公交车", "服务中心", "政务中心"}


def extract_entities(text):
    out, seen = [], set()
    for m in POI_RE.findall(text or ""):
        if m in NOISE or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out[:3]


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(c):
    json.dump(c, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)


def amap_geocode(addr, key, district, delay=0.25):
    """返回 (lng, lat, township)；失败返回 None。"""
    try:
        time.sleep(delay)
        r = requests.get(AMAP_GEO, params={"key": key, "address": addr, "city": "杭州"}, timeout=12)
        d = r.json()
        if d.get("status") == "1" and d.get("geocodes"):
            g = d["geocodes"][0]
            # 校验落在目标区
            if district and g.get("district") and district not in g.get("district"):
                return None
            loc = g.get("location") or ""
            if "," not in loc:
                return None
            lng, lat = loc.split(",")
            town = g.get("township") or ""
            if not town:  # 地理编码没给街道 → 逆地理编码补
                town = amap_regeo(loc, key, delay)
            return float(lng), float(lat), town
    except Exception as e:
        print(f"  [geo err] {addr}: {e}", file=sys.stderr)
    return None


def amap_regeo(loc, key, delay=0.25):
    try:
        time.sleep(delay)
        r = requests.get(AMAP_REGEO, params={"key": key, "location": loc, "extensions": "base"}, timeout=12)
        d = r.json()
        if d.get("status") == "1":
            comp = d.get("regeocode", {}).get("addressComponent", {})
            return comp.get("township") or ""
    except Exception:
        pass
    return ""


def read_rows(csv_path):
    rows = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                fid = int(r.get("forum_fid") or 0)
            except ValueError:
                continue
            meta = TARGET.get(fid)
            if not meta or not meta[2]:   # 仅 8 主城区（有 district 的）
                continue
            rows.append((meta[2], (r.get("subject") or "") + (r.get("content") or "")))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="../shenghang_zhejiang_hangzhou.csv")
    ap.add_argument("--data", default="../data.json")
    ap.add_argument("--out", default="../data.json")
    ap.add_argument("--dry-run", action="store_true", help="只统计实体抽取覆盖率，不联网")
    ap.add_argument("--limit", type=int, default=0, help="每区最多地理编码多少条(省额度，0=不限)")
    ap.add_argument("--delay", type=float, default=0.25)
    args = ap.parse_args()

    rows = read_rows(args.csv)
    from collections import Counter, defaultdict
    has = sum(1 for _, t in rows if extract_entities(t))
    print(f"[实体抽取] {len(rows)} 条留言，{has} 条含地点实体 ({100*has/max(1,len(rows)):.0f}%)")
    by_dist = defaultdict(int); by_dist_hit = defaultdict(int)
    for dname, t in rows:
        by_dist[dname] += 1
        if extract_entities(t):
            by_dist_hit[dname] += 1
    for d in by_dist:
        print(f"   {d}: {100*by_dist_hit[d]/by_dist[d]:.0f}% 含实体")

    if args.dry_run:
        return

    key = os.environ.get("AMAP_KEY")
    if not key:
        sys.exit("缺少 AMAP_KEY 环境变量。申请：https://lbs.amap.com（Web服务 key），再设置后重跑。")

    cache = load_cache()
    store = load_store(args.data)
    per = defaultdict(lambda: {"street": Counter(), "points": [], "geo": 0, "tot": 0})
    n_calls = 0
    for i, (dname, t) in enumerate(rows):
        per[dname]["tot"] += 1
        if args.limit and per[dname]["geo"] >= args.limit:
            continue
        ents = extract_entities(t)
        if not ents:
            continue
        ent = ents[0]
        ckey = f"{dname}|{ent}"
        if ckey in cache:
            res = cache[ckey]
        else:
            res = amap_geocode(f"{dname}{ent}", key, dname, args.delay)
            n_calls += 1
            cache[ckey] = res
            if n_calls % 50 == 0:
                save_cache(cache)
                print(f"\r  已请求 {n_calls} 次，进度 {i}/{len(rows)}", end="", file=sys.stderr)
        if res:
            lng, lat, town = res
            per[dname]["geo"] += 1
            if town:
                per[dname]["street"][town] += 1
            per[dname]["points"].append([round(lng, 5), round(lat, 5)])
    save_cache(cache)
    print()

    # 合并进 data.json：地理编码结果覆盖 byStreet + 写 points
    for dname, agg in per.items():
        dd = store.get("districts", {}).get(dname)
        if not dd:
            continue
        if agg["street"]:
            dd["byStreet"] = dict(agg["street"].most_common(15))
        # 点位按经纬度聚合计数（同坐标合并，作为热力权重）
        pc = Counter((round(x[0], 4), round(x[1], 4)) for x in agg["points"])
        dd["points"] = [[k[0], k[1], v] for k, v in pc.most_common(400)]
        cov = 100 * agg["geo"] / max(1, agg["tot"])
        print(f"  {dname}: 地理编码 {agg['geo']}/{agg['tot']} ({cov:.0f}%) | 街道 {len(agg['street'])} | 点位 {len(dd['points'])}")

    write_store(store, args.out)
    print(f"[完成] 已写入 {args.out}（缓存 {len(cache)} 条实体）")


if __name__ == "__main__":
    main()
