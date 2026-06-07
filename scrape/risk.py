#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
社会风险预警引擎 —— 透明规则打分 + 聚类 + DeepSeek 研判
=========================================================
从 data.json 的「未办结池 pending」中:
  1) 规则打分:按信访风险口径(群体性/欠薪/房产/征拆/极端/公共安全/激化诉诸/环境)分类加权 + 久拖叠加;
  2) 聚类:同一小区/楼盘/公司(实体抽取)或 同议题+同区 → 风险点(≥2 条=群体性);高分单条也成点;
  3) 分级:红/橙/黄(可被 LLM 覆盖);
  4) 对 TOP 风险点调 deepseek-v4-pro 出「风险研判 + 处置建议」(JSON),按签名缓存不重复调用;
  5) 产出 risk.json,供大屏「社会风险预警」看板 + 高危弹窗。

用法: set DEEPSEEK_API_KEY=...   python risk.py --data ../data.json --out ../risk.json
      python risk.py --no-llm    # 只跑规则，不调大模型
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zj_live import now_cn, CN_TZ
from geocode import extract_entities

RISK_CACHE = ".riskcache.json"
DS_URL = "https://api.deepseek.com/chat/completions"
DS_MODEL = "deepseek-v4-pro"
TOP_LLM = 14          # 最多对多少个风险点调大模型
MIN_SINGLE = 5        # 单条留言成"风险点"的最低规则分

# 信访风险口径：类别 -> (关键词, 权重)
RISK_CATS = {
    "群体性":   (["集体", "多人", "业主们", "业主", "联名", "几十户", "数十", "我们", "大家", "全体", "众多", "百余", "上百"], 5),
    "欠薪讨薪": (["欠薪", "讨薪", "拖欠工资", "血汗钱", "农民工", "不发工资", "结算工资", "拖欠款"], 5),
    "房产烂尾": (["烂尾", "停工", "延期交付", "逾期交房", "办不了房产证", "不给办证", "开发商跑路"], 4),
    "征地拆迁": (["拆迁", "强拆", "征地", "征收", "补偿不合理", "安置", "回迁"], 4),
    "极端情绪": (["走投无路", "逼死", "绝望", "轻生", "自杀", "跳楼", "活不下去", "没法活", "拼命"], 6),
    "公共安全": (["危房", "安全隐患", "燃气泄漏", "火灾", "坍塌", "中毒", "爆炸", "塌方", "触电"], 4),
    "激化诉诸": (["上访", "越级", "进京", "赴省", "曝光", "媒体", "记者", "起诉", "法院", "投诉无门", "多次反映", "无人处理", "无人管", "求助无门"], 3),
    "环境污染": (["污染", "排污", "异味", "废气", "废水", "粉尘", "恶臭"], 2),
}


def score_msg(text):
    """返回 (总分, 命中类别集合)。"""
    cats = set()
    s = 0
    for cat, (words, w) in RISK_CATS.items():
        if any(x in text for x in words):
            cats.add(cat); s += w
    return s, cats


def pending_days(date_str):
    if not date_str:
        return 0
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=CN_TZ)
        return max(0, (now_cn() - d).days)
    except Exception:
        return 0


def cluster_key(m, ents):
    """聚类键：优先实体(小区/公司)，否则 区+主类别/领域。"""
    if ents:
        return f"{m.get('district') or m.get('forum')}|{ents[0]}"
    return f"{m.get('district') or m.get('forum')}|{m.get('domain') or '其他'}"


def level_of(score, cats, count):
    if "极端情绪" in cats or count >= 3 or "上访" in cats:
        pass
    hi = ("极端情绪" in cats) or (count >= 3 and ("群体性" in cats or "欠薪讨薪" in cats or "房产烂尾" in cats)) \
        or (("激化诉诸" in cats) and ("群体性" in cats or count >= 2))
    if hi or score >= 11:
        return "红"
    if score >= 6 or count >= 2:
        return "橙"
    return "黄"


def build_points(pending):
    groups = defaultdict(list)
    for m in pending:
        text = (m.get("title", "") + m.get("content", ""))
        s, cats = score_msg(text)
        ents = extract_entities(text)
        m["_score"], m["_cats"], m["_days"] = s, cats, pending_days(m.get("date"))
        groups[cluster_key(m, ents)].append(m)

    points = []
    for key, members in groups.items():
        count = len(members)
        catset = set().union(*[m["_cats"] for m in members]) if members else set()
        total = sum(m["_score"] for m in members) + (3 if count >= 3 else 0)
        maxdays = max((m["_days"] for m in members), default=0)
        if maxdays >= 90:
            total += 2
        elif maxdays >= 30:
            total += 1
        # 过滤噪声：单条且分低、且无敏感类别 → 跳过
        if count < 2 and total < MIN_SINGLE:
            continue
        if not catset and count < 3:
            continue
        members.sort(key=lambda x: -x["_score"])
        area = members[0].get("district") or members[0].get("forum")
        topic = next(iter(catset), members[0].get("domain") or "其他")
        ent = key.split("|", 1)[1]
        points.append({
            "key": key,
            "level": level_of(total, catset, count),
            "topic": topic, "area": area, "object": ent,
            "count": count, "days": maxdays, "score": total,
            "signals": sorted(catset),
            "samples": [{"title": x["title"], "date": x.get("date"), "forum": x.get("forum"),
                         "content": (x.get("content") or "")[:120]} for x in members[:5]],
        })
    order = {"红": 0, "橙": 1, "黄": 2}
    points.sort(key=lambda p: (order[p["level"]], -p["score"] * p["count"]))
    return points


# ---------------- DeepSeek 研判 ----------------
def ds_cache():
    if os.path.exists(RISK_CACHE):
        try:
            return json.load(open(RISK_CACHE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def sig(p):
    raw = p["key"] + "|" + "|".join(s["title"] for s in p["samples"])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


SYS = ("你是政府信访风险研判专家。依据群体性、极端化倾向、议题敏感性、紧迫性(久拖)、舆情(曝光/越级)等口径研判。"
       "务必只输出一个 JSON 对象，字段：level(红/橙/黄)、judge(风险研判，60字内)、advice(处置建议，60字内)。不要输出多余文字。")


def ds_judge(p, key):
    body = {
        "model": DS_MODEL, "temperature": 0.3, "max_tokens": 900,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content":
                f"风险点：地区[{p['area']}] 对象/议题[{p['object']}] 涉及{p['count']}条未办结留言，"
                f"最久未回复{p['days']}天，命中信号{p['signals']}。代表留言：" +
                " ；".join(f"《{s['title']}》{s['content']}" for s in p["samples"][:4]) +
                "  请按要求输出 JSON。"},
        ],
    }
    try:
        r = requests.post(DS_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=body, timeout=90)
        txt = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            o = json.loads(m.group(0))
            return {"level": o.get("level", p["level"]), "judge": o.get("judge", ""), "advice": o.get("advice", "")}
    except Exception as e:
        print(f"  [ds err] {p['key']}: {e}", file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="../data.json")
    ap.add_argument("--out", default="../risk.json")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    pending = data.get("pending", [])
    points = build_points(pending)
    print(f"[规则] 未办结 {len(pending)} 条 → 风险点 {len(points)}（红{sum(1 for p in points if p['level']=='红')} "
          f"橙{sum(1 for p in points if p['level']=='橙')} 黄{sum(1 for p in points if p['level']=='黄')}）")

    key = os.environ.get("DEEPSEEK_API_KEY")
    cache = ds_cache()
    if not args.no_llm and key:
        for p in points[:TOP_LLM]:
            sg = sig(p)
            res = cache.get(sg)
            if res is None:
                res = ds_judge(p, key)
                time.sleep(0.4)
                if res:
                    cache[sg] = res
            if res:
                p["level"] = res.get("level", p["level"])  # LLM 可覆盖等级
                p["judge"] = res.get("judge", "")
                p["advice"] = res.get("advice", "")
        json.dump(cache, open(RISK_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        # LLM 可能改级，重排
        order = {"红": 0, "橙": 1, "黄": 2}
        points.sort(key=lambda p: (order.get(p["level"], 3), -p["score"] * p["count"]))
        print(f"[DeepSeek] 已研判 TOP{min(TOP_LLM,len(points))}（缓存 {len(cache)}）")
    elif not args.no_llm:
        print("[提示] 无 DEEPSEEK_API_KEY，跳过研判（只出规则结果）", file=sys.stderr)

    out = {
        "updated": now_cn().isoformat(timespec="seconds"),
        "counts": {lv: sum(1 for p in points if p["level"] == lv) for lv in ("红", "橙", "黄")},
        "pendingTotal": len(pending),
        "points": points[:40],
    }
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"[完成] → {args.out}")


if __name__ == "__main__":
    main()
