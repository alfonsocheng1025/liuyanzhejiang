#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实体提取 + 高德地理编码 —— 把留言落点到经纬度/街道，产出 geo.json（与每10分钟的 data.json 解耦）
=====================================================================================
数据源无街道字段、datav 无街道边界，故：正则抽实体(小区/路/公司…) → 高德地理编码 → 经纬度+街道。

两种模式：
  full —— 用历史 CSV 全量地理编码（一次性建底图），并记录每版块水位线 wm（已处理最大 tid）
      python geocode.py --mode full --csv ../shenghang_zhejiang_hangzhou.csv --out ../geo.json
  live —— 读 data.json 的 recent，只地理编码 tid>wm 的"新留言"，增量并入 geo.json（每10分钟 Action 调用）
      python geocode.py --mode live --data ../data.json --out ../geo.json

需 AMAP_KEY 环境变量（高德 Web 服务 key，免费）。实体→结果落盘缓存 .geocache.json，重复不重复请求。
geo.json 结构：{updated, wm:{fid:tid}, districts:{区名:{geo:已编码数, byStreet:{街道:数}, points:[[lng,lat]]}}}
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zj_live import TARGET, now_cn

CACHE_FILE = ".geocache.json"
AMAP_GEO = "https://restapi.amap.com/v3/geocode/geo"
AMAP_REGEO = "https://restapi.amap.com/v3/geocode/regeo"
POINTS_CAP = 600   # 每区热力点位上限（按 tid 取最新）

POI_RE = re.compile(
    r'[一-龥A-Za-z0-9]{2,8}'
    r'(?:小区|花园|公寓|大厦|广场|家园|名苑|苑|公馆|新村|村|社区|路|街|大道|'
    r'工业园|产业园|科技园|创业园|商务区|公司|工厂|厂|医院|卫生院|学校|学院|大学|'
    r'中学|小学|幼儿园|市场|中心|站|桥)')
NOISE = {"该公司", "贵公司", "本公司", "物业公司", "开发商", "建筑公司", "公交车", "服务中心", "政务中心"}


def extract_entities(text):
    out, seen = [], set()
    for m in POI_RE.findall(text or ""):
        if m in NOISE or m in seen:
            continue
        seen.add(m); out.append(m)
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


def amap_regeo(loc, key, delay):
    try:
        time.sleep(delay)
        d = requests.get(AMAP_REGEO, params={"key": key, "location": loc, "extensions": "base"}, timeout=12).json()
        if d.get("status") == "1":
            return d.get("regeocode", {}).get("addressComponent", {}).get("township") or ""
    except Exception:
        pass
    return ""


def amap_geocode(addr, key, district, delay):
    try:
        time.sleep(delay)
        d = requests.get(AMAP_GEO, params={"key": key, "address": addr, "city": "杭州"}, timeout=12).json()
        if d.get("status") == "1" and d.get("geocodes"):
            g = d["geocodes"][0]
            if district and g.get("district") and district not in g.get("district"):
                return None
            loc = g.get("location") or ""
            if "," not in loc:
                return None
            lng, lat = loc.split(",")
            town = g.get("township") or amap_regeo(loc, key, delay)
            return [round(float(lng), 5), round(float(lat), 5), town]
    except Exception as e:
        print(f"  [geo err] {addr}: {e}", file=sys.stderr)
    return None


def geocode_record(district, text, key, cache, delay):
    """返回 ([lng,lat], township) 或 None。"""
    ents = extract_entities(text)
    if not ents:
        return None
    ck = f"{district}|{ents[0]}"
    if ck in cache:
        res = cache[ck]
    else:
        res = amap_geocode(f"{district}{ents[0]}", key, district, delay)
        cache[ck] = res
    if res:
        return [res[0], res[1]], res[2]
    return None


def blank_geo():
    return {"updated": "", "wm": {}, "districts": {}}


def dist_slot(geo, name):
    return geo["districts"].setdefault(name, {"geo": 0, "byStreet": {}, "streetSat": {}, "points": []})


def add_sat(slot, town, gm):
    """记录某街道的满意度评分（态度 1-5★）。"""
    if town and 1 <= gm <= 5:
        ss = slot.setdefault("streetSat", {}).setdefault(town, {"s": 0, "c": 0})
        ss["s"] += gm; ss["c"] += 1


def finalize(geo, out):
    for dd in geo["districts"].values():
        if len(dd["byStreet"]) > 15:
            dd["byStreet"] = dict(sorted(dd["byStreet"].items(), key=lambda x: -x[1])[:15])
        # streetSat 只保留 byStreet 里的街道
        dd["streetSat"] = {k: v for k, v in dd.get("streetSat", {}).items() if k in dd["byStreet"]}
        dd["points"] = dd["points"][-POINTS_CAP:]
    geo["updated"] = now_cn().isoformat(timespec="seconds")
    json.dump(geo, open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))


def need_key():
    k = os.environ.get("AMAP_KEY")
    if not k:
        sys.exit("缺少 AMAP_KEY 环境变量（高德 Web 服务 key）。")
    return k


def mode_full(csv_path, out, delay, limit):
    key = need_key()
    cache = load_cache()
    geo = blank_geo()
    rows = []
    maxtid = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                fid = int(r.get("forum_fid") or 0); tid = int(r.get("tid") or 0)
            except ValueError:
                continue
            meta = TARGET.get(fid)
            if not meta or not meta[2]:
                continue
            maxtid[fid] = max(maxtid.get(fid, 0), tid)
            try:
                gm = int(r.get("grade_manner") or 0)
            except ValueError:
                gm = 0
            rows.append((fid, meta[2], tid, (r.get("subject") or "") + (r.get("content") or ""), gm))
    geo["wm"] = {str(k): v for k, v in maxtid.items()}
    per_pts = defaultdict(list)   # district -> [(tid,[lng,lat])]
    done = defaultdict(int); n = 0
    for fid, dname, tid, text, gm in rows:
        if limit and done[dname] >= limit:
            continue
        res = geocode_record(dname, text, key, cache, delay)
        n += 1
        if n % 50 == 0:
            save_cache(cache); print(f"\r  已处理 {n}", end="", file=sys.stderr)
        if res:
            pt, town = res
            slot = dist_slot(geo, dname)
            slot["geo"] += 1; done[dname] += 1
            if town:
                slot["byStreet"][town] = slot["byStreet"].get(town, 0) + 1
                add_sat(slot, town, gm)
            per_pts[dname].append((tid, pt))
    save_cache(cache); print()
    for dname, lst in per_pts.items():
        lst.sort(key=lambda x: x[0])           # 按 tid 升序，finalize 取最新
        dist_slot(geo, dname)["points"] = [p for _, p in lst]
    finalize(geo, out)
    for n2, dd in geo["districts"].items():
        print(f"  {n2}: 编码 {dd['geo']} | 街道 {len(dd['byStreet'])} | 点位 {len(dd['points'])}")
    print(f"[full] → {out}（缓存 {len(cache)}）")


def mode_live(data_path, out, delay):
    key = need_key()
    cache = load_cache()
    geo = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else blank_geo()
    geo.setdefault("wm", {}); geo.setdefault("districts", {})
    data = json.load(open(data_path, encoding="utf-8"))
    new_max = {}
    added = 0
    for it in data.get("recent", []):
        d = it.get("district")
        if not d:
            continue
        fid = it.get("fid"); tid = it.get("tid", 0)
        if tid <= geo["wm"].get(str(fid), 0):
            continue
        new_max[fid] = max(new_max.get(fid, 0), tid)
        res = geocode_record(d, (it.get("title", "") + it.get("content", "")), key, cache, delay)
        if res:
            pt, town = res
            slot = dist_slot(geo, d)
            slot["geo"] += 1; added += 1
            if town:
                slot["byStreet"][town] = slot["byStreet"].get(town, 0) + 1
                add_sat(slot, town, it.get("gManner", 0))
            slot["points"].append(pt)
    for fid, mt in new_max.items():
        geo["wm"][str(fid)] = max(geo["wm"].get(str(fid), 0), mt)
    save_cache(cache)
    finalize(geo, out)
    print(f"[live] 新增编码 {added} 条 → {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "live", "dry-run"], default="full")
    ap.add_argument("--csv", default="../shenghang_zhejiang_hangzhou.csv")
    ap.add_argument("--data", default="../data.json")
    ap.add_argument("--out", default="../geo.json")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.mode == "dry-run":
        rows = 0; hit = 0
        with open(args.csv, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    fid = int(r.get("forum_fid") or 0)
                except ValueError:
                    continue
                if not (TARGET.get(fid) and TARGET[fid][2]):
                    continue
                rows += 1
                if extract_entities((r.get("subject") or "") + (r.get("content") or "")):
                    hit += 1
        print(f"[dry-run] {rows} 条，{hit} 含实体 ({100*hit/max(1,rows):.0f}%)")
    elif args.mode == "full":
        mode_full(args.csv, args.out, args.delay, args.limit)
    else:
        mode_live(args.data, args.out, args.delay)


if __name__ == "__main__":
    main()
