#!/usr/bin/env python3
"""
浙江省领导留言板全量爬虫 v2（断点续爬 + 限速恢复）
Usage:
  python zj_crawler.py                          # 全部版块，每版块2000条
  python zj_crawler.py --per-forum 2000
  python zj_crawler.py --resume                 # 断点续爬（跳过已有数据的版块）
  python zj_crawler.py --fids 559,560
  python zj_crawler.py --delay 2.0              # 加大请求间隔（被限速时用）
  python zj_crawler.py --retry-failed           # 只重试上次失败/跳过的版块
"""
import requests, time, csv, argparse, sys, json, os
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://liuyan.people.com.cn/pro-dfbbs-front/forum/list?fid=14",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}
URL = "https://liuyan.people.com.cn/pro-dfbbs-front/threads/queryThreadsList"

ZJ_FORUMS = {
    14:"浙江省",559:"浙江省委书记王浩",560:"浙江省长王忠林",
    146:"杭州市",1007:"杭州市委书记刘捷",1008:"杭州市市长姚高员",
    4171:"上城区委书记",4170:"拱墅区委书记",4174:"西湖区委书记",4175:"滨江区委书记",
    4176:"萧山区委书记",4177:"余杭区委书记",5193:"临平区委书记",5194:"钱塘区委书记",
    4179:"富阳区委书记",4180:"临安区委书记",4181:"桐庐县委书记",4182:"淳安县委书记",4178:"建德市委书记",
    145:"宁波市",4183:"海曙区委书记",4185:"江北区委书记",4186:"镇海区委书记",
    4187:"北仑区委书记",4188:"鄞州区委书记",5195:"奉化区委书记",
    4189:"余姚市委书记",4190:"慈溪市委书记",4191:"宁海县委书记",4192:"象山县委书记",
    144:"温州市",4193:"鹿城区委书记",4194:"龙湾区委书记",4195:"瓯海区委书记",
    4196:"洞头区委书记",4197:"永嘉县委书记",4198:"平阳县委书记",4199:"苍南县委书记",
    4200:"文成县委书记",4201:"泰顺县委书记",4202:"瑞安市委书记",4203:"乐清市委书记",5196:"龙港市委书记",
    143:"嘉兴市",4204:"南湖区委书记",4205:"秀洲区委书记",
    4206:"嘉善县委书记",4207:"海盐县委书记",4208:"海宁市委书记",4209:"平湖市委书记",4210:"桐乡市委书记",
    142:"湖州市",4211:"吴兴区委书记",4212:"南浔区委书记",
    4213:"德清县委书记",4214:"长兴县委书记",4215:"安吉县委书记",
    141:"绍兴市",4216:"越城区委书记",4217:"柯桥区委书记",4218:"上虞区委书记",
    4219:"新昌县委书记",4220:"诸暨市委书记",4221:"嵊州市委书记",
    140:"金华市",4222:"婺城区委书记",4223:"金东区委书记",4224:"武义县委书记",
    4225:"浦江县委书记",4226:"磐安县委书记",4227:"兰溪市委书记",
    4228:"义乌市委书记",4229:"东阳市委书记",4230:"永康市委书记",
    139:"衢州市",4231:"柯城区委书记",4232:"衢江区委书记",
    4233:"常山县委书记",4234:"开化县委书记",4235:"龙游县委书记",4236:"江山市委书记",
    138:"舟山市",4237:"定海区委书记",4238:"普陀区委书记",4239:"岱山县委书记",4240:"嵊泗县委书记",
    137:"台州市",4241:"椒江区委书记",4242:"黄岩区委书记",4243:"路桥区委书记",
    4244:"玉环市委书记",4245:"三门县委书记",4246:"天台县委书记",
    4247:"仙居县委书记",4248:"温岭市委书记",4249:"临海市委书记",
    136:"丽水市",4250:"莲都区委书记",4251:"青田县委书记",4252:"缙云县委书记",
    4253:"遂昌县委书记",4254:"松阳县委书记",4255:"云和县委书记",
    4256:"庆元县委书记",4257:"景宁畲族自治县委书记",4258:"龙泉市委书记",
}

FIELDS = ['tid','date','year','month','forum_label','forum_fid',
          'subject','content','domain','type','status',
          'has_reply','reply_date','reply_content','reply_org',
          'grade_manner','grade_speed','fav_num','ip','user_id']

PROGRESS_FILE = '.crawl_progress.json'

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_progress(progress):
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

def fetch_batch(fid, last_item=0, page_size=20, base_delay=1.0):
    """返回 (items, hit_ratelimit)"""
    backoff = base_delay
    for attempt in range(6):
        try:
            time.sleep(backoff)
            r = requests.post(
                URL,
                data={"fid": fid, "lastItem": last_item, "pageSize": page_size},
                headers=HEADERS,
                timeout=20
            )
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"\n  [限速429] 等待 {wait}s...", file=sys.stderr)
                time.sleep(wait)
                backoff = base_delay * 2
                continue

            if r.status_code != 200:
                print(f"\n  [HTTP {r.status_code}] fid={fid}, attempt={attempt+1}", file=sys.stderr)
                time.sleep(backoff * 2)
                backoff = min(backoff * 2, 60)
                continue

            text = r.text.strip()
            if not text:
                if attempt < 3:
                    wait = 15 * (attempt + 1)
                    print(f"\n  [空响应] fid={fid} 等待{wait}s重试...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return [], True

            d = r.json()
            if d.get("result") != "success":
                msg = d.get("resultDesc", "unknown error")
                if attempt < 3:
                    print(f"\n  [API错误:{msg}] fid={fid} 重试...", file=sys.stderr)
                    time.sleep(backoff * 2)
                    continue
                return [], False

            items = d.get("responseData", [])
            return items, False

        except requests.exceptions.Timeout:
            print(f"\n  [超时] fid={fid} attempt={attempt+1}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"\n  [异常] fid={fid}: {e}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    return [], True

def parse_row(item, label, fid):
    dt = datetime.fromtimestamp(item.get('dateline', 0))
    reply_dt = ''
    if item.get('answerDateline'):
        try:
            reply_dt = datetime.fromtimestamp(item['answerDateline']).strftime('%Y-%m-%d')
        except:
            pass
    return {
        'tid': item.get('tid'),
        'date': dt.strftime('%Y-%m-%d'),
        'year': dt.year, 'month': dt.month,
        'forum_label': label, 'forum_fid': fid,
        'subject': item.get('subject', ''),
        'content': (item.get('content', '') or '').replace('\n', ' ').replace('\r', ''),
        'domain': item.get('domainName', ''),
        'type': item.get('typeName', ''),
        'status': item.get('stateInfo', ''),
        'has_reply': 1 if item.get('answerId') else 0,
        'reply_date': reply_dt,
        'reply_content': (item.get('answerContent', '') or '').replace('\n', ' '),
        'reply_org': item.get('answerOrganization', '') or '',
        'grade_manner': item.get('gradeManner', '') or '',
        'grade_speed': item.get('gradeSpeed', '') or '',
        'fav_num': item.get('favNum', 0),
        'ip': item.get('ip', ''),
        'user_id': item.get('userId', ''),
    }

def crawl_forum(fid, label, per_forum=2000, base_delay=1.0, progress=None, resume=False):
    start_last = 0
    start_count = 0
    if resume and progress and str(fid) in progress:
        p = progress[str(fid)]
        start_last = p.get('last_tid', 0)
        start_count = p.get('count', 0)
        if start_count >= per_forum:
            print(f"  [跳过] {label} 已完成({start_count}条)")
            return []

    records = []
    last = start_last
    consecutive_empty = 0

    while len(records) + start_count < per_forum:
        items, rate_limited = fetch_batch(fid, last, 20, base_delay)

        if not items:
            consecutive_empty += 1
            if rate_limited and consecutive_empty <= 3:
                wait = 60 * consecutive_empty
                print(f"\n  [限速] {label} 等待{wait}s后重试...", end='', file=sys.stderr)
                time.sleep(wait)
                continue
            else:
                break

        consecutive_empty = 0
        records.extend([parse_row(it, label, fid) for it in items])
        last = items[-1]['tid']

        total_so_far = len(records) + start_count
        print(f"\r  {label[:16]:<16} | {total_so_far:>5}条 | cursor={last}", end='')

    print()
    return records

def main():
    ap = argparse.ArgumentParser(description='浙江省留言板全量爬虫 v2')
    ap.add_argument('--output', default='zj_liuyan_data.csv')
    ap.add_argument('--per-forum', type=int, default=2000)
    ap.add_argument('--fids', help='指定版块fid，逗号分隔')
    ap.add_argument('--delay', type=float, default=1.0, help='请求间隔秒数（默认1.0，限速时建议2.0）')
    ap.add_argument('--resume', action='store_true', help='断点续爬，跳过已完成版块')
    ap.add_argument('--retry-failed', action='store_true', help='只重试上次+0条的版块')
    args = ap.parse_args()

    if args.fids:
        target = {int(f): ZJ_FORUMS.get(int(f), f'fid_{f}') for f in args.fids.split(',')}
    else:
        target = ZJ_FORUMS

    progress = load_progress()

    if args.retry_failed:
        target = {fid: name for fid, name in target.items()
                  if str(fid) not in progress or progress[str(fid)].get('count', 0) == 0}
        print(f"[断点续爬] 需要重试 {len(target)} 个失败版块")

    print(f"{'='*60}")
    print(f"浙江省留言板爬虫 v2")
    print(f"版块: {len(target)} 个 | 每版块上限: {args.per_forum} | 间隔: {args.delay}s")
    print(f"输出: {args.output} | 断点续爬: {args.resume}")
    print(f"{'='*60}")

    write_mode = 'a' if (args.resume or args.retry_failed) else 'w'
    file_exists = os.path.exists(args.output) and write_mode == 'a'

    seen = set()
    if file_exists:
        try:
            with open(args.output, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('tid'):
                        seen.add(str(row['tid']))
            print(f"[续爬] 已有 {len(seen)} 条记录，将追加新数据\n")
        except Exception as e:
            print(f"[警告] 读取已有文件失败: {e}")

    total_written = 0
    failed_forums = []

    with open(args.output, write_mode, newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not file_exists or write_mode == 'w':
            writer.writeheader()

        for fid, label in target.items():
            rows = crawl_forum(fid, label, args.per_forum, args.delay, progress, args.resume)
            new = [r for r in rows if str(r['tid']) not in seen]
            seen.update(str(r['tid']) for r in new)
            writer.writerows(new)
            f.flush()
            total_written += len(new)

            if len(new) == 0 and not args.resume:
                failed_forums.append((fid, label))
                status = "⚠ 无数据"
            else:
                status = f"✓ +{len(new)}条"
                progress[str(fid)] = {'count': len(new), 'label': label, 'last_tid': 0}
                save_progress(progress)

            print(f"  {status} {label}（累计 {total_written} 条）")

    print(f"\n{'='*60}")
    print(f"✓ 完成！共 {total_written} 条 → {args.output}")

    if failed_forums:
        fids_str = ','.join(str(f) for f, _ in failed_forums)
        print(f"\n⚠ {len(failed_forums)} 个版块未获取到数据（可能被限速）：")
        for fid, name in failed_forums:
            print(f"   fid={fid}: {name}")
        print(f"\n等待30分钟后运行：")
        print(f"  python zj_crawler.py --fids {fids_str} --delay 2.0 --resume")
        with open('.failed_forums.txt', 'w') as ff:
            ff.write(fids_str)
        print(f"失败版块fid已保存到 .failed_forums.txt")

if __name__ == '__main__':
    main()
