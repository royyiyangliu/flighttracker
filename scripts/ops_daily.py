#!/usr/bin/env python3
"""
ops_daily.py - 东航上海↔北京/深圳 航班运营数据抓取

运行方式:
  python scripts/ops_daily.py            # 只抓昨天（每日定时运行）
  python scripts/ops_daily.py --days 60  # 回溯60天（首次运行）
  python scripts/ops_daily.py --skip-discover  # 跳过航班发现步骤

流程:
  Step 1  从东航官网(ceair.com)获取明天的MU航班号 → 更新 flights_list.json
  Step 2  用确认的航班号从 FlightView 爬昨天的机型/延误数据 → 更新 flights.csv
  Step 3  生成前端 JSON 文件
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

try:
    from playwright_stealth import Stealth as _Stealth
    _stealth = _Stealth()
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

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

AIRLINE  = "MU"
MAX_CONC = 4      # 并发浏览器数
REQ_DELAY = 2.0   # FlightView 请求间隔（秒）

CSV_COLS = [
    "date", "flight", "dep_airport", "arr_airport",
    "status", "aircraft",
    "dep_scheduled", "dep_actual", "dep_delay_min",
    "arr_scheduled", "arr_actual", "arr_delay_min",
    "dep_terminal", "dep_gate", "arr_terminal",
    "scraped_at",
]

# 需要扫描的双向航线（ceair.com oneway 页面）
CEAIR_ROUTES = [
    # 上海 → 北京
    ("SHA", "PEK"), ("SHA", "PKX"),
    ("PVG", "PEK"), ("PVG", "PKX"),
    # 北京 → 上海
    ("PEK", "SHA"), ("PEK", "PVG"),
    ("PKX", "SHA"), ("PKX", "PVG"),
    # 上海 → 深圳
    ("SHA", "SZX"), ("PVG", "SZX"),
    # 深圳 → 上海
    ("SZX", "SHA"), ("SZX", "PVG"),
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
}


# ──────────────────────────────────────────────────────────────
# Step 1: 从 ceair.com 发现航班号
# ──────────────────────────────────────────────────────────────

# 复用 daily_update.py 已验证的 DOM 提取逻辑
CEAIR_JS = r"""() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const seen = new Set();
    const flights = [];
    let node;
    while (node = walker.nextNode()) {
        const t = node.textContent.trim();
        if (!/^[\d,]{4,6}$/.test(t)) continue;
        const num = parseInt(t.replace(/,/g, ''));
        if (num < 500 || num > 99999) continue;
        let ctx = node.parentElement;
        for (let i = 0; i < 12; i++) {
            if (!ctx) break;
            const full = ctx.innerText || '';
            const lines = full.split('\n')
                .map(l => l.trim())
                .filter(l => l && l !== '—' && l !== '— —');
            if (!lines.length) { ctx = ctx.parentElement; continue; }
            const fm = lines[0].match(/^(MU)(\d{3,4})$/);
            if (!fm || !full.includes('¥')) { ctx = ctx.parentElement; continue; }
            const key = lines[0];
            if (seen.has(key)) break;
            seen.add(key);
            const times = lines.filter(l => /^\d{2}:\d{2}$/.test(l));
            flights.push({
                flightNo: fm[1] + fm[2],
                depTime:  times[0] || null,
                arrTime:  times[1] || null,
            });
            break;
        }
    }
    return flights;
}"""

CEAIR_WAIT_JS = """() => {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while (n = w.nextNode()) {
        const t = n.textContent.trim();
        if (/^[\d,]{4,6}$/.test(t)) {
            const v = parseInt(t.replace(/,/g, ''));
            if (v > 500 && v < 99999) return true;
        }
    }
    return false;
}"""


async def discover_from_ceair(target_date: str) -> list[dict]:
    """
    爬取 ceair.com 各航线明天的 MU 航班号。
    target_date: YYYY-MM-DD（通常为明天）
    返回: [{"airline","flight_num","dep","arr"}, ...]
    若发现失败（网络/反爬）返回空列表，调用方回退到现有列表。
    """
    confirmed = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = await ctx.new_page()
        if HAS_STEALTH:
            await _stealth.apply_stealth_async(page)
            log.info("  [stealth] 已应用")

        for dep, arr in CEAIR_ROUTES:
            url = f"https://www.ceair.com/zh/cny/shopping/oneway/{dep}-{arr}/{target_date}"
            log.info(f"  {dep}→{arr} …")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_function(CEAIR_WAIT_JS, timeout=30000)
                except Exception:
                    log.warning(f"    等待超时，仍尝试解析")
                await asyncio.sleep(2)

                raw = await page.evaluate(CEAIR_JS)
                found = [f for f in raw if f.get("flightNo", "").startswith("MU")]
                for f in found:
                    num = f["flightNo"][2:]
                    confirmed.append({
                        "airline": "MU", "flight_num": num,
                        "dep": dep, "arr": arr,
                        "dep_time": f.get("depTime"),
                        "arr_time": f.get("arrTime"),
                    })
                log.info(f"    → {len(found)} 班: {[f['flightNo'] for f in found]}")

            except Exception as e:
                log.warning(f"    {dep}→{arr} 失败: {e}")

        await browser.close()

    log.info(f"ceair.com 共发现 {len(confirmed)} 条 MU 航班")
    return confirmed


# ──────────────────────────────────────────────────────────────
# Step 2: FlightView 数据抓取
# ──────────────────────────────────────────────────────────────

def parse_page_text(text: str) -> dict | None:
    if "FLIGHT STATUS" not in text:
        return None

    r: dict = {k: None for k in CSV_COLS}

    for s in ["Arrived", "Canceled", "Cancelled", "In Air", "Delayed", "Diverted", "Scheduled"]:
        if re.search(rf"\b{s}\b", text):
            r["status"] = "Canceled" if s == "Cancelled" else s
            break

    if m := re.search(r"Aircraft[\t ]+(.+)", text):
        r["aircraft"] = m.group(1).strip()

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
            if diff < -360: diff += 1440
            r[dk] = diff

    return r


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
# Step 3: CSV + 前端 JSON
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

    today = datetime.now().strftime("%Y-%m-%d")
    for route, records in records_by_route.items():
        records.sort(key=lambda r: (r["date"], r["flight"]), reverse=True)
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
# 主流程
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="回溯天数（默认1=只抓昨天）")
    parser.add_argument("--skip-discover", action="store_true",
                        help="跳过航班发现步骤，直接使用现有 flights_list.json")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: 从 ceair.com 发现航班号 ──
    flights: list[dict] = []
    if FLIGHTS_FILE.exists():
        with open(FLIGHTS_FILE, encoding="utf-8") as f:
            flights = json.load(f)

    if not args.skip_discover:
        tomorrow = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
        log.info(f"从 ceair.com 获取 {tomorrow} 的 MU 航班号（共 {len(CEAIR_ROUTES)} 条航线）…")
        new_flights = asyncio.run(discover_from_ceair(tomorrow))

        if new_flights:
            # 完全替换为 ceair.com 的结果（最新官方数据）
            flights = new_flights
            flights.sort(key=lambda x: (x["dep"], x["arr"], x["flight_num"]))
            with open(FLIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(flights, f, ensure_ascii=False, indent=2)
            log.info(f"flights_list.json 已更新：{len(flights)} 条")
        else:
            log.warning("ceair.com 发现失败，回退使用现有 flights_list.json")

    if not flights:
        log.error("航班列表为空，退出")
        return

    log.info(f"本次使用航班数：{len(flights)} 条")

    # ── Step 2: 爬 FlightView 历史数据 ──
    today = datetime.now().date()
    dates = [
        (today - timedelta(days=i)).strftime("%Y%m%d")
        for i in range(1, args.days + 1)
    ]
    dates.reverse()
    log.info(f"日期范围：{dates[0]} → {dates[-1]}（{len(dates)} 天）")

    done = load_done()
    task_list = [
        (f["airline"], f["flight_num"], d, f["dep"])
        for d in dates
        for f in flights
        if (f"{f['airline']}{f['flight_num']}", d) not in done
    ]
    log.info(f"待抓取：{len(task_list)} 个任务（已有 {len(done)} 条跳过）")

    if task_list:
        asyncio.run(run_tasks(task_list, on_result=append_csv))

    # ── Step 3: 生成前端 JSON ──
    log.info("生成前端 JSON 文件…")
    generate_route_jsons()

    log.info("全部完成。")


if __name__ == "__main__":
    main()
