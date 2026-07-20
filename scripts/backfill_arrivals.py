#!/usr/bin/env python3
"""
backfill_arrivals.py —— 一次性补爬：回补历史上「到达时刻为空」的记录

背景：航班严重延误跨天时，当初抓取（每日 04:00 抓昨天）往往航班尚未落地，
FlightView 上还没有实际到达时刻，导致 arr_actual / arr_delay_min 为空，
这些记录被前端统计排除。本脚本用与每日爬虫相同的 FlightView 抓取手段
（复用 ops_daily.run_tasks：4 并发、请求间隔 2s、屏蔽图片字体）对这些行
重抓一次，若已落地则就地补全。

仅处理：监控航线内、非取消、arr_actual 为空的行；不新增、不动其它数据。
补爬回来若仍未落地（FlightView 把实际运行归到次日等情形），保留原行不抹除。

用法：
  python scripts/backfill_arrivals.py                      # 预演：只列出将补爬的行，不抓取不写入
  python scripts/backfill_arrivals.py --apply              # 实际补爬并写回 CSV + 重生成 JSON
  python scripts/backfill_arrivals.py --since 20260601     # 只处理该日期(含)之后的行
  python scripts/backfill_arrivals.py --apply --limit 200  # 限制条数（建议先小批验证）
"""

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "ops"
CSV_FILE = DATA_DIR / "flights.csv"

BEIJING_TZ = timezone(timedelta(hours=8))

# 与 ops_daily.py 一致
CSV_COLS = [
    "date", "flight", "dep_airport", "arr_airport",
    "status", "aircraft",
    "dep_scheduled", "dep_actual", "dep_delay_min",
    "arr_scheduled", "arr_actual", "arr_delay_min",
    "dep_terminal", "dep_gate", "arr_terminal",
    "scraped_at",
]

ROUTE_LABELS = {
    "SHA-PEK": "虹桥 → 北京首都", "SHA-PKX": "虹桥 → 北京大兴", "SHA-SZX": "虹桥 → 深圳宝安",
    "PVG-PEK": "浦东 → 北京首都", "PVG-PKX": "浦东 → 北京大兴", "PVG-SZX": "浦东 → 深圳宝安",
    "PEK-SHA": "北京首都 → 虹桥", "PEK-PVG": "北京首都 → 浦东",
    "PKX-SHA": "北京大兴 → 虹桥", "PKX-PVG": "北京大兴 → 浦东",
    "SZX-SHA": "深圳宝安 → 虹桥", "SZX-PVG": "深圳宝安 → 浦东",
    "SHA-CAN": "虹桥 → 广州白云", "PVG-CAN": "浦东 → 广州白云",
    "CAN-SHA": "广州白云 → 虹桥", "CAN-PVG": "广州白云 → 浦东",
}


def csv_key(row: dict) -> tuple:
    return (row["flight"], row["date"], row["dep_airport"])


def route_of(row: dict) -> str:
    return f"{row['dep_airport']}-{row['arr_airport']}"


def is_monitored(row: dict) -> bool:
    return route_of(row) in ROUTE_LABELS


def is_complete(row: dict) -> bool:
    """无需再抓：已有实际到达时刻，或已取消/备降等终态。"""
    if row.get("arr_actual"):
        return True
    if row.get("status") in ("Canceled", "Cancelled", "Diverted"):
        return True
    return False


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


# 与 ops_daily.py 完全一致的 JSON 生成
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


def generate_route_jsons(rows: list[dict]):
    today = datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")
    by_route: dict[str, list] = {}
    for row in rows:
        route = route_of(row)
        if route not in ROUTE_LABELS:
            continue
        by_route.setdefault(route, []).append({
            "date":       row["date"],
            "flight":     row["flight"],
            "status":     row.get("status", ""),
            "aircraft":   shorten_aircraft(row.get("aircraft", "")),
            "dep_sched":  row.get("dep_scheduled", ""),
            "dep_actual": row.get("dep_actual", ""),
            "dep_delay":  int_or_none(row.get("dep_delay_min")),
            "arr_sched":  row.get("arr_scheduled", ""),
            "arr_actual": row.get("arr_actual", ""),
            "arr_delay":  int_or_none(row.get("arr_delay_min")),
            "dep_t": row.get("dep_terminal", ""),
            "dep_g": row.get("dep_gate", ""),
            "arr_t": row.get("arr_terminal", ""),
        })
    for route, records in sorted(by_route.items()):
        records.sort(key=lambda r: (r["date"], r["flight"]), reverse=True)
        out = {"route": route, "label": ROUTE_LABELS.get(route, route),
               "updated": today, "records": records}
        with open(DATA_DIR / f"{route}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"    {route}.json：{len(records)} 条")


def select_targets(csv_rows: dict, since: str | None) -> list[dict]:
    """监控航线内、非取消、到达为空的行。"""
    out = []
    for row in csv_rows.values():
        if not is_monitored(row):
            continue
        if is_complete(row):
            continue
        if since and row["date"] < since:
            continue
        out.append(row)
    out.sort(key=lambda r: (r["date"], r["flight"], r["dep_airport"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际补爬并写回（默认仅预演）")
    ap.add_argument("--since", type=str, default=None, help="只处理该日期(YYYYMMDD, 含)之后的行")
    ap.add_argument("--limit", type=int, default=None, help="最多补爬条数（先小批验证用）")
    args = ap.parse_args()

    csv_rows = load_csv_rows()
    targets = select_targets(csv_rows, args.since)

    print(f"CSV 共 {len(csv_rows)} 行；待补爬（监控航线/非取消/到达为空）：{len(targets)} 条")
    if args.since:
        print(f"  （已按 --since {args.since} 过滤）")
    by_route = Counter(route_of(r) for r in targets)
    for rt in sorted(by_route):
        print(f"    {rt}: {by_route[rt]}")
    by_date = Counter(r["date"] for r in targets)
    span = f"{min(by_date)} ~ {max(by_date)}" if by_date else "-"
    print(f"  日期跨度：{span}")

    if args.limit:
        targets = targets[:args.limit]
        print(f"  --limit：本次只补爬前 {len(targets)} 条")

    if not args.apply:
        print("\n（预演模式，未抓取、未写入；加 --apply 实际执行）")
        # 列几条样例
        for r in targets[:10]:
            print(f"    {r['date']} {r['flight']} {route_of(r)} "
                  f"status={r['status']} dep_delay={r['dep_delay_min']}")
        return

    if not targets:
        print("无待补爬记录，结束。")
        return

    # ── 实际抓取：复用每日爬虫的 FlightView 手段 ──
    import asyncio
    from ops_daily import run_tasks   # 触发 playwright 导入（运行环境需已安装）

    task_list = [
        (r["flight"][:2], r["flight"][2:], r["date"], r["dep_airport"])
        for r in targets
    ]

    filled, still_empty = [], []

    def on_result(result: dict):
        k = csv_key(result)
        old = csv_rows.get(k)
        if result.get("arr_actual"):
            # 已落地：用新行覆盖（含真实到达时刻/延误/机型/航站楼）
            csv_rows[k] = result
            filled.append(k)
        elif old is not None:
            # 仍未落地：只刷新状态与抓取时间，保留原有数据不抹除
            old["status"] = result.get("status") or old.get("status")
            old["scraped_at"] = result.get("scraped_at") or old.get("scraped_at")

    print(f"\n开始补爬 {len(task_list)} 条…")
    asyncio.run(run_tasks(task_list, on_result=on_result))

    still_empty = [t for t in targets if not csv_rows[csv_key(t)].get("arr_actual")]
    print(f"\n补爬完成：成功补回到达 {len(filled)} 条；仍为空 {len(still_empty)} 条"
          f"（FlightView 无数据/归到次日/历史过久）")

    write_csv_rows(csv_rows)
    print(f"已写回 flights.csv（{len(csv_rows)} 行）")
    print("重生成前端 JSON …")
    generate_route_jsons(list(csv_rows.values()))
    print("全部完成。")


if __name__ == "__main__":
    main()
