"""
一次性脚本：用修正后的公式重算 flights.csv 中所有行的延误时间，
并重新生成前端 JSON 文件。

只修改 dep_delay_min 和 arr_delay_min 两列，其余爬虫数据不变。
"""

import csv
import json
import pathlib
import sys

DATA_DIR = pathlib.Path(__file__).parent.parent / "data" / "ops"
CSV_FILE = DATA_DIR / "flights.csv"

ROUTE_LABELS = {
    "PVG-PKX": "浦东 → 北京大兴",
    "PKX-PVG": "北京大兴 → 浦东",
    "SHA-PEK": "虹桥 → 北京首都",
    "PEK-SHA": "北京首都 → 虹桥",
    "PVG-PEK": "浦东 → 北京首都",
    "PEK-PVG": "北京首都 → 浦东",
    "SHA-PKX": "虹桥 → 北京大兴",
    "PKX-SHA": "北京大兴 → 虹桥",
    "PVG-SZX": "浦东 → 深圳",
    "SZX-PVG": "深圳 → 浦东",
    "SHA-SZX": "虹桥 → 深圳",
    "SZX-SHA": "深圳 → 虹桥",
}


def to_min(t: str) -> int | None:
    if not t:
        return None
    try:
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except Exception:
        return None


def calc_delay(sched: str, actual: str) -> int | None:
    s, a = to_min(sched), to_min(actual)
    if s is None or a is None:
        return None
    diff = a - s
    if diff < -360: diff += 1440   # 实际在次日，计划在当天
    if diff > 1080: diff -= 1440   # 实际在当天，计划在次日
    return diff


def recalc_csv() -> tuple[int, int]:
    """重算 CSV 中所有行的延误，返回 (总行数, 修改行数)。"""
    with open(CSV_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys()) if rows else []

    changed = 0
    for row in rows:
        new_dep = calc_delay(row.get("dep_scheduled", ""), row.get("dep_actual", ""))
        new_arr = calc_delay(row.get("arr_scheduled", ""), row.get("arr_actual", ""))

        old_dep = row.get("dep_delay_min", "")
        old_arr = row.get("arr_delay_min", "")

        row["dep_delay_min"] = "" if new_dep is None else str(new_dep)
        row["arr_delay_min"] = "" if new_arr is None else str(new_arr)

        if row["dep_delay_min"] != old_dep or row["arr_delay_min"] != old_arr:
            changed += 1

    with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    return len(rows), changed


def shorten_aircraft(s: str) -> str:
    for prefix in ("Airbus ", "Boeing "):
        s = s.replace(prefix, "")
    return s.strip()


def int_or_none(s: str) -> int | None:
    try:
        return int(s)
    except Exception:
        return None


def regenerate_jsons():
    """从 CSV 重新生成所有航线 JSON 文件。"""
    records_by_route: dict[str, list] = {}
    with open(CSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dep, arr = row.get("dep_airport", ""), row.get("arr_airport", "")
            if not dep or not arr:
                continue
            route = f"{dep}-{arr}"
            if route not in ROUTE_LABELS:
                continue
            records_by_route.setdefault(route, []).append({
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

    for route, records in records_by_route.items():
        records.sort(key=lambda r: (r["date"], r["flight"]), reverse=True)
        path = DATA_DIR / f"{route}.json"
        existing_updated = ""
        if path.exists():
            with open(path, encoding="utf-8") as f:
                existing_updated = json.load(f).get("updated", "")
        out = {
            "route":   route,
            "label":   ROUTE_LABELS.get(route, route),
            "updated": existing_updated,
            "records": records,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  更新 {path.name}（{len(records)} 条）")


def main():
    if not CSV_FILE.exists():
        print(f"错误：找不到 {CSV_FILE}", file=sys.stderr)
        sys.exit(1)

    print(f"读取 {CSV_FILE} ...")
    total, changed = recalc_csv()
    print(f"完成：共 {total} 行，修改了 {changed} 行的延误时间")

    print("重新生成前端 JSON ...")
    regenerate_jsons()
    print("全部完成。")


if __name__ == "__main__":
    main()
