#!/usr/bin/env python3
"""
ops_daily.py - 东航上海↔北京/深圳 航班运营数据抓取

运行方式:
  python scripts/ops_daily.py            # 只抓昨天（每日定时运行）
  python scripts/ops_daily.py --days 60  # 回溯60天（首次运行）
  python scripts/ops_daily.py --skip-discover  # 跳过航班发现步骤

数据来源: FlightView (flightview.com)
数据输出:
  data/ops/flights_list.json  - 已知航班号（自动发现维护）
  data/ops/flights.csv        - 历史运营数据（追加）
  data/ops/{ROUTE}.json       - 前端展示用 JSON（每次覆盖）
"""

import argparse
import asyncio
import csv
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent.parent
DATA_DIR     = BASE_DIR / "data" / "ops"
FLIGHTS_FILE = DATA_DIR / "flights_list.json"
CSV_FILE     = DATA_DIR / "flights.csv"

AIRLINE     = "MU"
MAX_CONC    = 4      # 并发浏览器数（GitHub Actions 限制内）
REQ_DELAY   = 2.0   # 请求间隔（秒）

CSV_COLS = [
    "date", "flight", "dep_airport", "arr_airport",
    "status", "aircraft",
    "dep_scheduled", "dep_actual", "dep_delay_min",
    "arr_scheduled", "arr_actual", "arr_delay_min",
    "dep_terminal", "dep_gate", "arr_terminal",
    "scraped_at",
]

# 目标机场关键词映射
ARR_KW = {
    "PEK": ["Capital", "PEK"],
    "PKX": ["Daxing",  "PKX"],
    "SZX": ["Shenzhen","SZX"],
}
DEP_KW = {
    "SHA": ["Hongqiao", "SHA"],
    "PVG": ["Pudong",   "PVG"],
}

# ──────────────────────────────────────────────────────────────
# 1. 解析 FlightView 页面文本
# ──────────────────────────────────────────────────────────────

def parse_page_text(text: str) -> dict | None:
    if "FLIGHT STATUS" not in text:
        return None

    r: dict = {k: None for k in CSV_COLS}

    # 状态
    for s in ["Arrived", "Canceled", "Cancelled", "In Air", "Delayed", "Diverted", "Scheduled"]:
        if re.search(rf"\b{s}\b", text):
            r["status"] = "Canceled" if s == "Cancelled" else s
            break

    # 机型
    if m := re.search(r"Aircraft[\t ]+(.+)", text):
        r["aircraft"] = m.group(1).strip()

    # 出发段
    if dep := re.search(r"\nDeparture\n(.*?)\nArrival\n", text, re.DOTALL):
        d = dep.group(1)
        if m := re.search(r"Scheduled Time:[\t ]+(\d{1,2}:\d{2})", d):
            r["dep_scheduled"] = m.group(1)
        if m := re.search(r"Take Off Time:[\t ]+(\d{1,2}:\d{2})", d):
            r["dep_actual"] = m.group(1)
        if m := re.search(r"Terminal:[\t ]+(.+)", d):
            r["dep_terminal"] = m.group(1).strip()
        if m := re.search(r"Gate:[\t ]+(.+)", d):
            r["dep_gate"] = m.group(1).strip()

    # 到达段
    if arr := re.search(r"\nArrival\n(.*?)\nFlight Details", text, re.DOTALL):
        a = arr.group(1)
        if m := re.search(r"Airport[\t ]+.+\((\w{3})\)", a):
            r["arr_airport"] = m.group(1)
        if m := re.search(r"Scheduled Time:[\t ]+(\d{1,2}:\d{2})", a):
            r["arr_scheduled"] = m.group(1)
        if m := re.search(r"At Gate Time:[\t ]+(\d{1,2}:\d{2})", a):
            r["arr_actual"] = m.group(1)
        if m := re.search(r"Terminal:[\t ]+(.+)", a):
            r["arr_terminal"] = m.group(1).strip()

    # 计算延误分钟
    def to_min(t):
        if not t: return None
        h, m = map(int, t.split(":"))
        return h * 60 + m

    for sk, ak, dk in [
        ("dep_scheduled", "dep_actual",  "dep_delay_min"),
        ("arr_scheduled", "arr_actual",  "arr_delay_min"),
    ]:
        s, a = to_min(r[sk]), to_min(r[ak])
        if s is not None and a is not None:
            diff = a - s
            if diff < -360: diff += 1440   # 跨午夜修正
            r[dk] = diff

    return r


# ──────────────────────────────────────────────────────────────
# 2. 单次页面抓取
# ──────────────────────────────────────────────────────────────

BASE_URL = "https://www.flightview.com/flight-tracker"

async def fetch_one(page, airline, num, date_str, dep) -> dict | None:
    url = f"{BASE_URL}/{airline}/{num}?date={date_str}&depapt={dep}"
    try:
        await page.goto(url, timeout=35000, wait_until="domcontentloaded")
        await page.wait_for_function(
            "(document.querySelector('main')?.innerText||'').includes('Track Another Flight')",
            timeout=25000,
        )
        text = await page.locator("main").inner_text()
        parsed = parse_page_text(text)
        if parsed:
            parsed.update({
                "date":        date_str,
                "flight":      f"{airline}{num}",
                "dep_airport": dep,
                "scraped_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
        return parsed
    except Exception as e:
        log.debug(f"  ✗ {airline}{num} {date_str}: {type(e).__name__}")
        return None


# ──────────────────────────────────────────────────────────────
# 3. 并发批量抓取
# ──────────────────────────────────────────────────────────────

async def run_tasks(task_list: list[tuple], on_result=None) -> list[dict]:
    queue = asyncio.Queue()
    for t in task_list:
        await queue.put(t)

    total   = len(task_list)
    done    = [0]
    success = [0]
    results = []
    lock    = asyncio.Lock()

    async def worker(pw):
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()
        # 屏蔽图片/字体，加快加载
        await page.route(
            "**/*.{png,jpg,gif,svg,woff,woff2,ttf,ico}",
            lambda r: r.abort()
        )
        while True:
            try:
                airline, num, date_str, dep = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            result = await fetch_one(page, airline, num, date_str, dep)
            async with lock:
                done[0] += 1
                if result:
                    success[0] += 1
                    results.append(result)
                    if on_result:
                        on_result(result)
                if done[0] % 20 == 0 or done[0] == total:
                    log.info(f"  进度 {done[0]}/{total}，有效 {success[0]} 条")
            queue.task_done()
            await asyncio.sleep(REQ_DELAY)
        await browser.close()

    async with async_playwright() as pw:
        workers = [worker(pw) for _ in range(MAX_CONC)]
        await asyncio.gather(*workers)

    return results


# ──────────────────────────────────────────────────────────────
# 4. 发现新航班（扫描机场出发/到达板）
# ──────────────────────────────────────────────────────────────

TABLE_JS = """() => {
    const t = document.querySelector('table');
    if (!t) return [];
    return Array.from(t.querySelectorAll('tr')).slice(1)
        .map(r => Array.from(r.querySelectorAll('td')).map(td => td.innerText.trim()))
        .filter(r => r.length >= 4);
}"""

def classify(text, mapping):
    for iata, kws in mapping.items():
        if any(k in text for k in kws):
            return iata
    return None

async def discover_flights(existing: list[dict]) -> list[dict]:
    existing_keys = {(f["airline"], f["flight_num"], f["dep"], f["arr"]) for f in existing}
    found = dict()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()
        await page.route("**/*.{png,jpg,gif,svg,woff,woff2,ttf,ico}", lambda r: r.abort())

        # 出发板：SHA / PVG → PEK / PKX / SZX
        for dep in ["SHA", "PVG"]:
            url = f"https://www.flightview.com/airport/{dep}/departures"
            log.info(f"  扫描出发板 {dep}…")
            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await page.wait_for_selector("table", timeout=20000)
                await asyncio.sleep(2)
                rows = await page.evaluate(TABLE_JS)
                for row in rows:
                    name, num, dest = row[0], row[1], row[2]
                    if "Eastern" not in name and AIRLINE not in name:
                        continue
                    arr = classify(dest, ARR_KW)
                    if not arr: continue
                    key = (AIRLINE, num, dep, arr)
                    if key not in existing_keys and key not in found:
                        found[key] = {"airline": AIRLINE, "flight_num": num, "dep": dep, "arr": arr}
                        log.info(f"    新发现: MU{num} {dep}→{arr}")
            except Exception as e:
                log.warning(f"  出发板 {dep} 失败: {e}")

        # 到达板：PEK / PKX / SZX ← SHA / PVG
        for arr in ["PEK", "PKX", "SZX"]:
            url = f"https://www.flightview.com/airport/{arr}/arrivals"
            log.info(f"  扫描到达板 {arr}…")
            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await page.wait_for_selector("table", timeout=20000)
                await asyncio.sleep(2)
                rows = await page.evaluate(TABLE_JS)
                for row in rows:
                    name, num, origin = row[0], row[1], row[2]
                    if "Eastern" not in name and AIRLINE not in name:
                        continue
                    dep = classify(origin, DEP_KW)
                    if not dep: continue
                    key = (AIRLINE, num, dep, arr)
                    if key not in existing_keys and key not in found:
                        found[key] = {"airline": AIRLINE, "flight_num": num, "dep": dep, "arr": arr}
                        log.info(f"    新发现: MU{num} {dep}→{arr}")
            except Exception as e:
                log.warning(f"  到达板 {arr} 失败: {e}")

        await browser.close()

    return list(found.values())


# ──────────────────────────────────────────────────────────────
# 5. CSV 管理
# ──────────────────────────────────────────────────────────────

def load_done() -> set[tuple]:
    done = set()
    if CSV_FILE.exists():
        with open(CSV_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add((row["flight"], row["date"]))
    return done

def append_csv(record: dict):
    is_new = not CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(record)


# ──────────────────────────────────────────────────────────────
# 6. 生成前端 JSON
# ──────────────────────────────────────────────────────────────

ROUTE_LABELS = {
    "SHA-PEK": "上海虹桥 → 北京首都",
    "SHA-PKX": "上海虹桥 → 北京大兴",
    "SHA-SZX": "上海虹桥 → 深圳宝安",
    "PVG-PEK": "上海浦东 → 北京首都",
    "PVG-PKX": "上海浦东 → 北京大兴",
    "PVG-SZX": "上海浦东 → 深圳宝安",
}

def shorten_aircraft(s: str) -> str:
    if not s: return ""
    m = re.search(r"(A\d{3}[\w-]*|B\d{3}[\w-]*|C\d{3}[\w-]*|ARJ\w*)", s)
    return m.group(1) if m else s.strip()

def int_or_none(s):
    try: return int(s)
    except: return None

def generate_route_jsons():
    if not CSV_FILE.exists():
        log.warning("flights.csv 不存在，跳过 JSON 生成")
        return

    records_by_route: dict[str, list] = {}
    with open(CSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dep, arr = row.get("dep_airport", ""), row.get("arr_airport", "")
            if not dep or not arr: continue
            route = f"{dep}-{arr}"
            records_by_route.setdefault(route, []).append({
                "date":     row["date"],
                "flight":   row["flight"],
                "status":   row.get("status", ""),
                "aircraft": shorten_aircraft(row.get("aircraft", "")),
                "dep_sched":  row.get("dep_scheduled", ""),
                "dep_actual": row.get("dep_actual", ""),
                "dep_delay":  int_or_none(row.get("dep_delay_min")),
                "arr_sched":  row.get("arr_scheduled", ""),
                "arr_actual": row.get("arr_actual", ""),
                "arr_delay":  int_or_none(row.get("arr_delay_min")),
                "dep_t":  row.get("dep_terminal", ""),
                "dep_g":  row.get("dep_gate", ""),
                "arr_t":  row.get("arr_terminal", ""),
            })

    today = datetime.now().strftime("%Y-%m-%d")
    for route, records in records_by_route.items():
        records.sort(key=lambda r: r["date"], reverse=True)
        out = {
            "route":   route,
            "label":   ROUTE_LABELS.get(route, route),
            "updated": today,
            "records": records,
        }
        path = DATA_DIR / f"{route}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        log.info(f"  生成 {path.name}（{len(records)} 条）")


# ──────────────────────────────────────────────────────────────
# 7. 主流程
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="回溯天数（默认1=只抓昨天）")
    parser.add_argument("--skip-discover", action="store_true",
                        help="跳过航班发现步骤")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── 读取已知航班列表 ──
    flights: list[dict] = []
    if FLIGHTS_FILE.exists():
        with open(FLIGHTS_FILE, encoding="utf-8") as f:
            flights = json.load(f)
    log.info(f"已知航班：{len(flights)} 条")

    # ── 发现新航班 ──
    if not args.skip_discover:
        log.info("扫描机场航班板，发现新航班…")
        new = asyncio.run(discover_flights(flights))
        if new:
            flights.extend(new)
            flights.sort(key=lambda x: (x["dep"], x["arr"], x["flight_num"]))
            with open(FLIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(flights, f, ensure_ascii=False, indent=2)
            log.info(f"  新增 {len(new)} 条航班，已保存")
        else:
            log.info("  无新航班")

    # ── 确定日期范围 ──
    today = datetime.now().date()
    dates = [
        (today - timedelta(days=i)).strftime("%Y%m%d")
        for i in range(1, args.days + 1)
    ]
    dates.reverse()
    log.info(f"日期范围：{dates[0]} → {dates[-1]}（{len(dates)} 天）")

    # ── 跳过已有数据 ──
    done = load_done()
    task_list = [
        (f["airline"], f["flight_num"], d, f["dep"])
        for d in dates
        for f in flights
        if (f"{f['airline']}{f['flight_num']}", d) not in done
    ]
    log.info(f"待抓取：{len(task_list)} 个任务（已跳过 {len(done)} 条已有数据）")

    if task_list:
        asyncio.run(run_tasks(task_list, on_result=append_csv))

    # ── 生成前端 JSON ──
    log.info("生成前端 JSON 文件…")
    generate_route_jsons()

    log.info("全部完成。")


if __name__ == "__main__":
    main()
