#!/usr/bin/env python3
"""
backfill_hkg.py —— 一次性回补：把沪港航线（上海↔香港）过去 N 天的历史补上

沪港航线是新加入的监控航线，flights_list 里已有对应航班，但历史数据只有最近几天。
本脚本对这些香港航班、回溯 N 天（默认 100），把 CSV 中缺失或未完成的 (航班,日期)
用与每日爬虫相同的 FlightView 抓取手段补齐。

反爬/抓取手段：直接复用 ops_daily.run_tasks —— 4 并发、每请求间隔 2s、屏蔽图片与字体，
与日常爬虫完全一致（不额外加压，尽量温和）。

只在抓到真实运营信息（实际起降 / 取消 / 备降）时写入，避免为未执飞日插入空行。

用法：
  python scripts/backfill_hkg.py                 # 预演：列出将补的 (航班,日期)，不抓取不写入
  python scripts/backfill_hkg.py --apply         # 实际补爬并写回 CSV + 重生成 JSON
  python scripts/backfill_hkg.py --days 60       # 自定义回溯天数（默认 100）
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

# 本脚本作用范围：沪港四条航线
HKG_ROUTES = {"SHA-HKG", "PVG-HKG", "HKG-SHA", "HKG-PVG"}

CSV_COLS = [
    "date", "flight", "dep_airport", "arr_airport",
    "status", "aircraft",
    "dep_scheduled", "dep_actual", "dep_delay_min",
    "arr_scheduled", "arr_actual", "arr_delay_min",
    "dep_terminal", "dep_gate", "arr_terminal",
    "scraped_at",
]

ROUTE_LABELS = {
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


def generate_hkg_jsons(rows: list[dict]):
    """仅重生成沪港四条航线的 JSON（其余航线不动）。"""
    today = datetime.datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")
    by_route: dict[str, list] = {}
    for row in rows:
        route = route_of(row)
        if route not in HKG_ROUTES:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际补爬并写回（默认仅预演）")
    ap.add_argument("--days", type=int, default=100, help="回溯天数（默认 100）")
    args = ap.parse_args()

    csv_rows = load_csv_rows()
    fl = json.load(open(FLIGHTS_FILE, encoding="utf-8"))
    hkg_flights = [f for f in fl if f"{f['dep']}-{f['arr']}" in HKG_ROUTES]

    today = datetime.datetime.now(tz=BEIJING_TZ).date()
    dates = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(1, args.days + 1)]

    # 目标：沪港航班 × 回溯窗口内，CSV 缺失或未完成的 (航班,日期,出发)
    tasks = []
    for f in hkg_flights:
        flight = "MU" + f["flight_num"]
        dep = f["dep"]
        for d in dates:
            row = csv_rows.get((flight, d, dep))
            if row is None or not is_complete(row):
                tasks.append((f["airline"], f["flight_num"], d, dep))

    print(f"沪港航班 {len(hkg_flights)} 个，回溯 {args.days} 天")
    print(f"待补爬 (缺失/未完成) 任务：{len(tasks)} 个")
    by_route = Counter(f"{dep}" for (_a, _n, _d, dep) in tasks)
    print(f"  按出发地：{dict(by_route)}")

    if not args.apply:
        print("\n（预演模式，未抓取、未写入；加 --apply 实际执行）")
        return
    if not tasks:
        print("无待补爬任务，结束。")
        return

    import asyncio
    from ops_daily import run_tasks  # 复用日常爬虫的抓取（含反爬手段）

    added, skipped = [], 0

    def on_result(result: dict):
        nonlocal skipped
        if has_signal(result):
            csv_rows[csv_key(result)] = result
            added.append(csv_key(result))
        else:
            skipped += 1  # 抓到但当天无执飞/无信息

    print(f"\n开始补爬 {len(tasks)} 个任务（4 并发 / 间隔 2s / 屏蔽图片字体）…")
    asyncio.run(run_tasks(tasks, on_result=on_result))

    got_arr = sum(1 for k in added if csv_rows[k].get("arr_actual"))
    print(f"\n补爬完成：新增/更新 {len(added)} 行（其中有实际到达 {got_arr}）；"
          f"当天无执飞跳过 {skipped}；无响应 {len(tasks) - len(added) - skipped}")
    write_csv_rows(csv_rows)
    print(f"已写回 flights.csv（{len(csv_rows)} 行）")
    print("重生成沪港航线 JSON …")
    generate_hkg_jsons(list(csv_rows.values()))
    print("全部完成。")


if __name__ == "__main__":
    main()
