#!/usr/bin/env python3
"""
cleanup_ops.py —— 一次性清洗脚本（只需运行一次，不参与日常定时任务）

背景：早期发现逻辑未过滤中转联程，导致 flights_list.json 混入大量“错误航班”
（把中转段的航班号误标成本航线），进而 flights.csv 里落入大量非监控航线的记录，
以及同一航班被重复抓取的重复行。本脚本做一次性归正：

  1) 依据 CSV 真值（FlightView 实际解析出的 dep→arr）重生成 flights_list.json，
     只保留真实航线属于监控范围（ROUTE_LABELS）的航班，按 (flight_num, dep, arr) 去重。
  2) 清洗 flights.csv：
       - 删除真实 dep→arr 不在监控范围的行；
       - 按 (flight, date, dep) 去重（保留有实际到达时刻/更新时间更晚的一条）。
  3) 用与 ops_daily.generate_route_jsons 完全一致的逻辑重生成各 {route}.json。

用法：
  python scripts/cleanup_ops.py            # 预演(dry-run)：只打印将发生的变化，不写任何文件
  python scripts/cleanup_ops.py --apply    # 实际写入
"""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data" / "ops"
CSV_FILE     = DATA_DIR / "flights.csv"
FLIGHTS_FILE = DATA_DIR / "flights_list.json"

BEIJING_TZ = timezone(timedelta(hours=8))

# 与 ops_daily.py 保持一致
CSV_COLS = [
    "date", "flight", "dep_airport", "arr_airport",
    "status", "aircraft",
    "dep_scheduled", "dep_actual", "dep_delay_min",
    "arr_scheduled", "arr_actual", "arr_delay_min",
    "dep_terminal", "dep_gate", "arr_terminal",
    "scraped_at",
]

ROUTE_LABELS = {
    "SHA-PEK": "虹桥 → 北京首都",
    "SHA-PKX": "虹桥 → 北京大兴",
    "SHA-SZX": "虹桥 → 深圳宝安",
    "PVG-PEK": "浦东 → 北京首都",
    "PVG-PKX": "浦东 → 北京大兴",
    "PVG-SZX": "浦东 → 深圳宝安",
    "PEK-SHA": "北京首都 → 虹桥",
    "PEK-PVG": "北京首都 → 浦东",
    "PKX-SHA": "北京大兴 → 虹桥",
    "PKX-PVG": "北京大兴 → 浦东",
    "SZX-SHA": "深圳宝安 → 虹桥",
    "SZX-PVG": "深圳宝安 → 浦东",
    "SHA-CAN": "虹桥 → 广州白云",
    "PVG-CAN": "浦东 → 广州白云",
    "CAN-SHA": "广州白云 → 虹桥",
    "CAN-PVG": "广州白云 → 浦东",
    "SHA-HKG": "虹桥 → 香港",
    "PVG-HKG": "浦东 → 香港",
    "HKG-SHA": "香港 → 虹桥",
    "HKG-PVG": "香港 → 浦东",
}


def route_of(row: dict) -> str:
    return f"{row['dep_airport']}-{row['arr_airport']}"


def is_monitored(row: dict) -> bool:
    return route_of(row) in ROUTE_LABELS


# 与 ops_daily.py 完全一致的机型缩写/取整
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


def load_rows() -> list[dict]:
    with open(CSV_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _better(a: dict, b: dict) -> dict:
    """在同键两行中选更可信的一条：优先有实际到达，其次 scraped_at 更晚。"""
    if bool(a.get("arr_actual")) != bool(b.get("arr_actual")):
        return a if a.get("arr_actual") else b
    return a if a.get("scraped_at", "") >= b.get("scraped_at", "") else b


def clean_csv(rows: list[dict]) -> tuple[list[dict], dict]:
    """返回 (清洗后行列表, 统计信息)。"""
    total = len(rows)
    monitored = [r for r in rows if is_monitored(r)]
    dropped_nonmon = total - len(monitored)

    deduped: dict[tuple, dict] = {}
    for r in monitored:
        k = (r["flight"], r["date"], r["dep_airport"])
        deduped[k] = r if k not in deduped else _better(deduped[k], r)

    dropped_dup = len(monitored) - len(deduped)
    kept = sorted(
        deduped.values(),
        key=lambda r: (r["date"], r["flight"], r["dep_airport"]),
    )
    stats = {
        "total": total,
        "dropped_nonmonitored": dropped_nonmon,
        "dropped_duplicate": dropped_dup,
        "kept": len(kept),
    }
    return kept, stats


def rebuild_flights_list(rows: list[dict]) -> list[dict]:
    """以 CSV 真值重建 flights_list：真实航线在监控范围内的 (flight,dep)。"""
    arr_ctr   = defaultdict(Counter)   # (flight,dep) -> 到达机场计数
    deptime   = defaultdict(Counter)   # (flight,dep,arr) -> 计划起飞时刻计数
    arrtime   = defaultdict(Counter)
    for r in rows:
        key = (r["flight"], r["dep_airport"])
        arr_ctr[key][r["arr_airport"]] += 1

    out = []
    for (flight, dep), c in arr_ctr.items():
        arr = c.most_common(1)[0][0]           # 真实到达＝众数
        if f"{dep}-{arr}" not in ROUTE_LABELS:
            continue
        # 该真实航线上的计划时刻众数
        dts = Counter(r["dep_scheduled"] for r in rows
                      if r["flight"] == flight and r["dep_airport"] == dep
                      and r["arr_airport"] == arr and r["dep_scheduled"])
        ats = Counter(r["arr_scheduled"] for r in rows
                      if r["flight"] == flight and r["dep_airport"] == dep
                      and r["arr_airport"] == arr and r["arr_scheduled"])
        out.append({
            "airline":   "MU",
            "flight_num": flight[2:] if flight.startswith("MU") else flight,
            "dep":       dep,
            "arr":       arr,
            "dep_time":  dts.most_common(1)[0][0] if dts else None,
            "arr_time":  ats.most_common(1)[0][0] if ats else None,
        })
    out.sort(key=lambda x: (x["dep"], x["arr"], x["flight_num"]))
    return out


def generate_route_jsons(rows: list[dict], apply: bool):
    """与 ops_daily.generate_route_jsons 一致：从清洗后的行重生成各 {route}.json。"""
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
        out = {
            "route":   route,
            "label":   ROUTE_LABELS.get(route, route),
            "updated": today,
            "records": records,
        }
        path = DATA_DIR / f"{route}.json"
        if apply:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"    {route}.json：{len(records)} 条" + ("" if apply else "（预演）"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认仅预演）")
    args = ap.parse_args()

    rows = load_rows()

    # 1) 清洗 CSV
    kept, st = clean_csv(rows)
    print("=== CSV 清洗 ===")
    print(f"  原始 {st['total']} 行")
    print(f"  删除非监控航线 {st['dropped_nonmonitored']} 行")
    print(f"  删除重复副本   {st['dropped_duplicate']} 行")
    print(f"  保留 {st['kept']} 行")

    # 2) 重建 flights_list
    new_list = rebuild_flights_list(rows)
    old_n = len(json.load(open(FLIGHTS_FILE, encoding="utf-8"))) if FLIGHTS_FILE.exists() else 0
    print("\n=== 重建 flights_list.json ===")
    print(f"  {old_n} → {len(new_list)} 条")
    by_route = Counter(f"{f['dep']}-{f['arr']}" for f in new_list)
    for rt in sorted(by_route):
        print(f"    {rt}: {by_route[rt]}")

    # 3) 重生成 JSON
    print("\n=== 重生成各航线 JSON ===")
    generate_route_jsons(kept, args.apply)

    if args.apply:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(kept)
        with open(FLIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(new_list, f, ensure_ascii=False, indent=2)
        print("\n✅ 已写入 flights.csv / flights_list.json / *.json")
    else:
        print("\n（预演模式，未写入任何文件；加 --apply 实际执行）")


if __name__ == "__main__":
    main()
