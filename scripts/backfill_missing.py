#!/usr/bin/env python3
"""
backfill_missing.py —— 一次性补爬：回补「整行缺失」的 (航班, 日期)

背景：历史上某些日子的定时爬取整批失败/被中断（如 6/20 全航线各缺约一半、
7/5-7/8 广州出发方向整条缺），旧爬虫只在抓成功时写行、又无跨天补漏，导致这些
(航班,日期) 在 CSV 里根本没有行。本脚本对每个监控航班、在其活跃区间内、
CSV 中缺失的日期，用与每日爬虫相同的 FlightView 手段重抓一次，补回真实数据。

- 范围：flights_list 中每个航班，其「首见~末见」区间内、CSV 缺失的日期。
- 只有抓到真实运营信息（有实际起飞/到达，或状态为取消/备降）才写入新行，
  避免为「当天根本没执飞」的日期插入一堆空的 Scheduled 行。
- 复用 ops_daily.run_tasks（4 并发、请求间隔 2s、屏蔽图片字体）。

用法：
  python scripts/backfill_missing.py                    # 预演：列出将补的缺失行，不抓取不写入
  python scripts/backfill_missing.py --apply            # 实际补爬并写回 CSV + 重生成 JSON
  python scripts/backfill_missing.py --since 20260501   # 只处理该日期(含)之后
  python scripts/backfill_missing.py --until 20260709   # 只处理该日期(含)之前
"""

import argparse
import csv
import datetime
import json
import re
from collections import Counter, defaultdict
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

ROUTE_LABELS = {
    "SHA-PEK": "虹桥 → 北京首都", "SHA-PKX": "虹桥 → 北京大兴", "SHA-SZX": "虹桥 → 深圳宝安",
    "PVG-PEK": "浦东 → 北京首都", "PVG-PKX": "浦东 → 北京大兴", "PVG-SZX": "浦东 → 深圳宝安",
    "PEK-SHA": "北京首都 → 虹桥", "PEK-PVG": "北京首都 → 浦东",
    "PKX-SHA": "北京大兴 → 虹桥", "PKX-PVG": "北京大兴 → 浦东",
    "SZX-SHA": "深圳宝安 → 虹桥", "SZX-PVG": "深圳宝安 → 浦东",
    "SHA-CAN": "虹桥 → 广州白云", "PVG-CAN": "浦东 → 广州白云",
    "CAN-SHA": "广州白云 → 虹桥", "CAN-PVG": "广州白云 → 浦东",
}


def _d(s: str) -> datetime.date:
    return datetime.datetime.strptime(s, "%Y%m%d").date()


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
    """是否含真实运营信息（值得写入）：有实际起降，或已取消/备降。"""
    if r.get("arr_actual") or r.get("dep_actual"):
        return True
    return r.get("status") in ("Canceled", "Cancelled", "Diverted")


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


def generate_route_jsons(rows: list[dict]):
    today = datetime.datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")
    by_route: dict[str, list] = {}
    for row in rows:
        route = route_of(row)
        if route not in ROUTE_LABELS:
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


def compute_missing(csv_rows: dict, since: str | None, until: str | None) -> list[tuple]:
    """返回缺失的 (airline, num, date, dep, route)。范围=每航班活跃区间∩[since,until]。"""
    fl = json.load(open(FLIGHTS_FILE, encoding="utf-8"))
    # 每 (flight,dep) 的活跃区间与已有日期
    have = defaultdict(set)
    arr_of = {}
    for (flight, date, dep), r in csv_rows.items():
        have[(flight, dep)].add(date)
        arr_of[(flight, dep)] = r["arr_airport"]

    missing = []
    for f in fl:
        flight = "MU" + f["flight_num"]
        dep = f["dep"]
        ds = have.get((flight, dep))
        if not ds:
            continue  # 该航班无任何历史，无法确定活跃区间，跳过
        lo, hi = min(ds), max(ds)
        if since and lo < since:
            lo = since
        if until and hi > until:
            hi = until
        if lo > hi:
            continue
        cur = _d(lo)
        end = _d(hi)
        while cur <= end:
            dstr = cur.strftime("%Y%m%d")
            if dstr not in ds:
                missing.append((f["airline"], f["flight_num"], dstr, dep, f"{dep}-{f['arr']}"))
            cur += datetime.timedelta(days=1)
    missing.sort(key=lambda x: (x[2], x[0] + x[1]))
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际补爬并写回（默认仅预演）")
    ap.add_argument("--since", type=str, default=None, help="只处理该日期(YYYYMMDD,含)之后")
    ap.add_argument("--until", type=str, default=None, help="只处理该日期(YYYYMMDD,含)之前")
    args = ap.parse_args()

    csv_rows = load_csv_rows()
    missing = compute_missing(csv_rows, args.since, args.until)

    print(f"CSV 现有 {len(csv_rows)} 行；活跃区间内缺失的 (航班,日期)：{len(missing)} 条")
    by_route = Counter(m[4] for m in missing)
    for rt in sorted(by_route):
        print(f"    {rt}: {by_route[rt]}")
    print("  缺失最多的日期(top10):")
    for d, c in Counter(m[2] for m in missing).most_common(10):
        print(f"    {d}: {c}")

    if not args.apply:
        print("\n（预演模式，未抓取、未写入；加 --apply 实际执行）")
        return
    if not missing:
        print("无缺失，结束。")
        return

    import asyncio
    from ops_daily import run_tasks   # 触发 playwright 导入（运行环境需已安装）

    task_list = [(air, num, d, dep) for (air, num, d, dep, _rt) in missing]
    added, skipped = [], []

    def on_result(result: dict):
        if has_signal(result):
            csv_rows[csv_key(result)] = result
            added.append(csv_key(result))
        else:
            skipped.append((result["flight"], result["date"]))  # 抓到了但无真实运营信息（当天没执飞）

    print(f"\n开始补爬 {len(task_list)} 条缺失…")
    asyncio.run(run_tasks(task_list, on_result=on_result))

    got_arr = sum(1 for k in added if csv_rows[k].get("arr_actual"))
    print(f"\n补爬完成：新增行 {len(added)} 条（其中有实际到达 {got_arr} 条）；"
          f"抓到但当天无执飞/无信息（未写入）{len(skipped)} 条；"
          f"完全无响应 {len(task_list) - len(added) - len(skipped)} 条")
    write_csv_rows(csv_rows)
    print(f"已写回 flights.csv（{len(csv_rows)} 行）")
    print("重生成前端 JSON …")
    generate_route_jsons(list(csv_rows.values()))
    print("全部完成。")


if __name__ == "__main__":
    main()
