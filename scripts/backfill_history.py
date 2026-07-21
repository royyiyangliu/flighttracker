#!/usr/bin/env python3
"""
backfill_history.py —— 通用一次性回补：对指定航班/航线，固定窗口回补历史

用于「中途才入列的航班」或「新加入的航线」——它们只有被发现之后的数据，
入列前的历史从未被抓过（backfill 的日常回补/按区间补缺都补不到）。本脚本用
固定 N 天窗口（不受首见日限制）对选定对象逐日抓取，补齐 CSV 缺失/未完成的行。

抓取/反爬手段：直接复用 ops_daily.run_tasks —— 4 并发、每请求间隔 2s、屏蔽图片与字体，
与日常爬虫一致。仅在抓到真实运营信息（实际起降 / 取消 / 备降）时写入，
避免为未执飞日插入空行。

选择对象（至少给一个；两者同时给取并集）：
  --flights MU5100,MU5232,MU5361     指定航班号（可带或不带 MU 前缀）
  --routes  SHA-HKG,PVG-HKG          指定航线（出发-到达）
  --all                              所有监控航班（谨慎，量大）

用法：
  python scripts/backfill_history.py --flights MU5361                 # 预演
  python scripts/backfill_history.py --flights MU5100,MU5232,MU5361 --apply
  python scripts/backfill_history.py --routes SHA-HKG,PVG-HKG --days 100 --apply
"""

import argparse
import csv
import datetime
import json
import re
from collections import Counter
from datetime import timezone, timedelta
from pathlib import Path

BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data" / "ops"
CSV_FILE     = DATA_DIR / "flights.csv"
FLIGHTS_FILE = DATA_DIR / "flights_list.json"
BEIJING_TZ   = timezone(timedelta(hours=8))

CSV_COLS = [
    "date", "flight", "dep_airport", "arr_airport",
    "status", "aircraft",
    "dep_scheduled", "dep_actual", "dep_delay_min",
    "arr_scheduled", "arr_actual", "arr_delay_min",
    "dep_terminal", "dep_gate", "arr_terminal",
    "scraped_at",
]

# 20 条监控航线（与 ops_daily 保持一致）
ROUTE_LABELS = {
    "SHA-PEK": "虹桥 → 北京首都", "SHA-PKX": "虹桥 → 北京大兴", "SHA-SZX": "虹桥 → 深圳宝安",
    "PVG-PEK": "浦东 → 北京首都", "PVG-PKX": "浦东 → 北京大兴", "PVG-SZX": "浦东 → 深圳宝安",
    "PEK-SHA": "北京首都 → 虹桥", "PEK-PVG": "北京首都 → 浦东",
    "PKX-SHA": "北京大兴 → 虹桥", "PKX-PVG": "北京大兴 → 浦东",
    "SZX-SHA": "深圳宝安 → 虹桥", "SZX-PVG": "深圳宝安 → 浦东",
    "SHA-CAN": "虹桥 → 广州白云", "PVG-CAN": "浦东 → 广州白云",
    "CAN-SHA": "广州白云 → 虹桥", "CAN-PVG": "广州白云 → 浦东",
    "SHA-HKG": "虹桥 → 香港", "PVG-HKG": "浦东 → 香港",
    "HKG-SHA": "香港 → 虹桥", "HKG-PVG": "香港 → 浦东",
}


def csv_key(row: dict) -> tuple:
    return (row["flight"], row["date"], row["dep_airport"])


def route_of(row: dict) -> str:
    return f"{row['dep_airport']}-{row['arr_airport']}"


def shorten_aircraft(s: str) -> str:
    if not s:
        return ""
    m = re.search(r"(A\d{3}[\w-]*|B\d{3}[\w-]*|C\d{3}[\w-]*|ARJ\w*)", s)
    return m.group(1) if m else s.strip()


def int_or_none(s):
    try:
        return int(s)
    except Exception:
        return None


def has_signal(r: dict) -> bool:
    if r.get("arr_actual") or r.get("dep_actual"):
        return True
    return r.get("status") in ("Canceled", "Cancelled", "Diverted")


def is_complete(row: dict) -> bool:
    if row.get("arr_actual"):
        return True
    return row.get("status") in ("Canceled", "Cancelled", "Diverted")


def load_csv_rows() -> dict:
    rows: dict[tuple, dict] = {}
    with open(CSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[csv_key(row)] = row
    return rows


def write_csv_rows(rows: dict):
    ordered = sorted(rows.values(), key=lambda r: (r["date"], r["flight"], r["dep_airport"]))
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)


def generate_route_jsons(rows: list[dict], only_routes: set[str]):
    """只重生成受影响航线的 JSON（其余不动），内容取该航线全部行。"""
    today = datetime.datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")
    by_route: dict[str, list] = {}
    for row in rows:
        route = route_of(row)
        if route not in ROUTE_LABELS or route not in only_routes:
            continue
        by_route.setdefault(route, []).append({
            "date": row["date"], "flight": row["flight"], "status": row.get("status", ""),
            "aircraft": shorten_aircraft(row.get("aircraft", "")),
            "dep_sched": row.get("dep_scheduled", ""), "dep_actual": row.get("dep_actual", ""),
            "dep_delay": int_or_none(row.get("dep_delay_min")),
            "arr_sched": row.get("arr_scheduled", ""), "arr_actual": row.get("arr_actual", ""),
            "arr_delay": int_or_none(row.get("arr_delay_min")),
            "dep_t": row.get("dep_terminal", ""), "dep_g": row.get("dep_gate", ""),
            "arr_t": row.get("arr_terminal", ""),
        })
    for route, records in sorted(by_route.items()):
        records.sort(key=lambda r: (r["date"], r["flight"]), reverse=True)
        out = {"route": route, "label": ROUTE_LABELS.get(route, route),
               "updated": today, "records": records}
        with open(DATA_DIR / f"{route}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"    {route}.json：{len(records)} 条")


def select_flights(fl: list[dict], flights_arg: str | None, routes_arg: str | None, all_arg: bool):
    """返回选中的 flights_list 条目。"""
    if all_arg:
        return list(fl)
    want_flights = set()
    if flights_arg:
        for t in flights_arg.split(","):
            t = t.strip().upper()
            if not t:
                continue
            want_flights.add(t if t.startswith("MU") else "MU" + t)
    want_routes = set()
    if routes_arg:
        want_routes = {r.strip().upper() for r in routes_arg.split(",") if r.strip()}
    out = []
    for f in fl:
        flight = "MU" + f["flight_num"]
        route = f"{f['dep']}-{f['arr']}"
        if flight in want_flights or route in want_routes:
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际补爬并写回（默认仅预演）")
    ap.add_argument("--days", type=int, default=100, help="回溯天数（默认 100）")
    ap.add_argument("--flights", type=str, default=None, help="指定航班号，逗号分隔（可带/不带 MU）")
    ap.add_argument("--routes", type=str, default=None, help="指定航线，逗号分隔（DEP-ARR）")
    ap.add_argument("--all", action="store_true", help="所有监控航班（谨慎）")
    args = ap.parse_args()

    if not (args.flights or args.routes or args.all):
        ap.error("必须至少指定 --flights / --routes / --all 之一")

    csv_rows = load_csv_rows()
    fl = json.load(open(FLIGHTS_FILE, encoding="utf-8"))
    chosen = select_flights(fl, args.flights, args.routes, args.all)
    if not chosen:
        print("未匹配到任何航班，检查 --flights / --routes 参数。")
        return

    today = datetime.datetime.now(tz=BEIJING_TZ).date()
    dates = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(1, args.days + 1)]

    tasks = []
    affected_routes = set()
    for f in chosen:
        flight = "MU" + f["flight_num"]
        dep = f["dep"]
        affected_routes.add(f"{dep}-{f['arr']}")
        for d in dates:
            row = csv_rows.get((flight, d, dep))
            if row is None or not is_complete(row):
                tasks.append((f["airline"], f["flight_num"], d, dep))

    print(f"选中航班 {len(chosen)} 个，涉及航线 {sorted(affected_routes)}，回溯 {args.days} 天")
    print(f"待补爬 (缺失/未完成) 任务：{len(tasks)} 个")
    for fnum in sorted({t[1] for t in tasks}):
        print(f"    MU{fnum}: {sum(1 for t in tasks if t[1]==fnum)} 个")

    if not args.apply:
        print("\n（预演模式，未抓取、未写入；加 --apply 实际执行）")
        return
    if not tasks:
        print("无待补爬任务，结束。")
        return

    import asyncio
    from ops_daily import run_tasks  # 复用日常爬虫抓取（含反爬手段）

    added, skipped = [], 0

    def on_result(result: dict):
        nonlocal skipped
        if has_signal(result):
            csv_rows[csv_key(result)] = result
            added.append(csv_key(result))
        else:
            skipped += 1

    print(f"\n开始补爬 {len(tasks)} 个任务（4 并发 / 间隔 2s / 屏蔽图片字体）…")
    asyncio.run(run_tasks(tasks, on_result=on_result))

    got_arr = sum(1 for k in added if csv_rows[k].get("arr_actual"))
    print(f"\n补爬完成：新增/更新 {len(added)} 行（其中有实际到达 {got_arr}）；"
          f"当天无执飞跳过 {skipped}；无响应 {len(tasks) - len(added) - skipped}")
    write_csv_rows(csv_rows)
    print(f"已写回 flights.csv（{len(csv_rows)} 行）")
    print("重生成受影响航线 JSON …")
    generate_route_jsons(list(csv_rows.values()), affected_routes)
    print("全部完成。")


if __name__ == "__main__":
    main()
