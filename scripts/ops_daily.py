#!/usr/bin/env python3
"""
ops_daily.py - 东航上海↔北京/深圳/广州 航班运营数据抓取

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
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now() -> datetime:
    """返回当前北京时间（UTC+8）"""
    return datetime.now(tz=BEIJING_TZ)

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
BACKFILL_DAYS = 3 # 回补窗口：每次运行重抓最近 N 天内「到达为空且非取消」的行

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
    # 上海 → 广州
    ("SHA", "CAN"), ("PVG", "CAN"),
    # 广州 → 上海
    ("CAN", "SHA"), ("CAN", "PVG"),
    # 上海 → 香港
    ("SHA", "HKG"), ("PVG", "HKG"),
    # 香港 → 上海
    ("HKG", "SHA"), ("HKG", "PVG"),
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
                direct:   lines.includes('直达'),
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

        # 随机打乱路线顺序，避免每次都以相同模式请求
        routes = list(CEAIR_ROUTES)
        random.shuffle(routes)

        for dep, arr in routes:
            url = f"https://www.ceair.com/zh/cny/shopping/oneway/{dep}-{arr}/{target_date}"
            log.info(f"  {dep}→{arr} …")

            # 最多重试2次
            for attempt in range(2):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    try:
                        await page.wait_for_function(CEAIR_WAIT_JS, timeout=50000)
                    except Exception:
                        if attempt == 0:
                            log.warning(f"    等待超时，5秒后重试…")
                            await asyncio.sleep(5)
                            continue   # 重试
                        else:
                            log.warning(f"    两次超时，跳过")

                    raw = await page.evaluate(CEAIR_JS)
                    mu = [f for f in raw if f.get("flightNo", "").startswith("MU")]
                    # 只保留直达航班：中转联程的分段航班号不属于本航线，丢弃
                    found = [f for f in mu if f.get("direct")]
                    dropped = len(mu) - len(found)
                    if dropped:
                        log.info(f"    过滤掉 {dropped} 个非直达(中转段)航班")
                    for f in found:
                        num = f["flightNo"][2:]
                        confirmed.append({
                            "airline": "MU", "flight_num": num,
                            "dep": dep, "arr": arr,
                            "dep_time": f.get("depTime"),
                            "arr_time": f.get("arrTime"),
                        })
                    log.info(f"    → {len(found)} 班: {[f['flightNo'] for f in found]}")
                    break   # 成功，跳出重试循环

                except Exception as e:
                    log.warning(f"    {dep}→{arr} 第{attempt+1}次失败: {e}")

            # 随机延迟 8-15 秒，模拟人工操作间隔
            delay = random.uniform(8, 15)
            log.info(f"    等待 {delay:.1f}s …")
            await asyncio.sleep(delay)

        await browser.close()

    log.info(f"ceair.com 共发现 {len(confirmed)} 条 MU 航班")
    return confirmed


# ──────────────────────────────────────────────────────────────
# Step 2: FlightView 数据抓取
# ──────────────────────────────────────────────────────────────

def to24h(hhmm: str, ampm: str | None) -> str:
    """将 12 小时制时间字符串转为 24 小时制（HH:MM）。"""
    h, m = map(int, hhmm.split(":"))
    if ampm:
        ap = ampm.strip().upper()
        if ap == "PM" and h != 12:
            h += 12
        elif ap == "AM" and h == 12:
            h = 0
    return f"{h:02d}:{m:02d}"

# FlightView 时间格式：H:MM AM/PM 或 HH:MM AM/PM（12小时制）
_TIME_RE = r"(\d{1,2}:\d{2})[\t ]*([AaPp][Mm])?"

def parse_page_text(text: str) -> dict | None:
    if "FLIGHT STATUS" not in text:
        return None

    r: dict = {k: None for k in CSV_COLS}

    # 注意：FlightView 对晚到/跨天航班状态显示为 "Landed"（而非 "Arrived"），
    # 归一为 Arrived；"Scheduled" 因页面到处有 "Scheduled Time:" 标签，必须放最后兜底。
    for s in ["Arrived", "Landed", "Canceled", "Cancelled", "In Air", "Delayed", "Diverted", "Scheduled"]:
        if re.search(rf"\b{s}\b", text):
            r["status"] = {"Cancelled": "Canceled", "Landed": "Arrived"}.get(s, s)
            break

    if m := re.search(r"Aircraft[\t ]+(.+)", text):
        r["aircraft"] = m.group(1).strip()

    if dep := re.search(r"\nDeparture\n(.*?)\nArrival\n", text, re.DOTALL):
        d = dep.group(1)
        if m := re.search(r"Scheduled Time:[\t ]+" + _TIME_RE, d):
            r["dep_scheduled"] = to24h(m.group(1), m.group(2))
        if m := re.search(r"Take Off Time:[\t ]+" + _TIME_RE, d):
            r["dep_actual"] = to24h(m.group(1), m.group(2))
        if m := re.search(r"Terminal:[\t ]+(.+)", d):
            r["dep_terminal"] = m.group(1).strip()
        if m := re.search(r"Gate:[\t ]+(.+)", d):
            r["dep_gate"] = m.group(1).strip()

    if arr := re.search(r"\nArrival\n(.*?)\nFlight Details", text, re.DOTALL):
        a = arr.group(1)
        if m := re.search(r"Airport[\t ]+.+\((\w{3})\)", a):
            r["arr_airport"] = m.group(1)
        if m := re.search(r"Scheduled Time:[\t ]+" + _TIME_RE, a):
            r["arr_scheduled"] = to24h(m.group(1), m.group(2))
        # 实际到达时刻：正常航班用 "At Gate Time"，晚到/跨天航班用 "Landed Time"
        if m := re.search(r"At Gate Time:[\t ]+" + _TIME_RE, a):
            r["arr_actual"] = to24h(m.group(1), m.group(2))
        elif m := re.search(r"Landed Time:[\t ]+" + _TIME_RE, a):
            r["arr_actual"] = to24h(m.group(1), m.group(2))
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
            if diff < -360: diff += 1440   # 实际在次日，计划在当天
            if diff > 1080: diff -= 1440   # 实际在当天，计划在次日
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
                "scraped_at":  beijing_now().strftime("%Y-%m-%d %H:%M"),
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

def csv_key(row: dict) -> tuple:
    """CSV 行的唯一键：航班 + 计划起飞日 + 出发机场。"""
    return (row["flight"], row["date"], row["dep_airport"])

def load_csv_rows() -> dict:
    """读入 flights.csv → {(flight,date,dep): row}。重复键保留更完整/更新的一条。"""
    rows: dict[tuple, dict] = {}
    if CSV_FILE.exists():
        with open(CSV_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = csv_key(row)
                old = rows.get(k)
                # 已有同键：优先保留有到达时刻的、否则保留 scraped_at 更晚的
                if old is None:
                    rows[k] = row
                elif not old.get("arr_actual") and row.get("arr_actual"):
                    rows[k] = row
                elif (old.get("arr_actual") == row.get("arr_actual")
                      and row.get("scraped_at", "") > old.get("scraped_at", "")):
                    rows[k] = row
    return rows

def write_csv_rows(rows: dict):
    """整表重写（按 日期→航班 排序，稳定输出）。"""
    ordered = sorted(rows.values(), key=lambda r: (r["date"], r["flight"], r["dep_airport"]))
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)

def is_complete(row: dict) -> bool:
    """该行是否已无需再抓：拿到实际到达时刻，或已取消/备降等终态。"""
    if row.get("arr_actual"):
        return True
    if row.get("status") in ("Canceled", "Cancelled", "Diverted"):
        return True
    return False


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
            # 只保留目标航线（上海↔北京/深圳/广州）
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

    today = beijing_now().strftime("%Y-%m-%d")
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
        tomorrow = (beijing_now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
        log.info(f"从 ceair.com 获取 {tomorrow} 的 MU 航班号（共 {len(CEAIR_ROUTES)} 条航线）…")
        new_flights = asyncio.run(discover_from_ceair(tomorrow))

        if new_flights:
            # 合并到现有列表（只增不减）：以 (flight_num, dep) 为唯一键
            existing_keys = {(f["flight_num"], f["dep"]): i for i, f in enumerate(flights)}
            added = 0
            for nf in new_flights:
                key = (nf["flight_num"], nf["dep"])
                if key not in existing_keys:
                    flights.append(nf)
                    existing_keys[key] = len(flights) - 1
                    added += 1
                else:
                    # 更新到达机场和时刻（航班调整时同步）
                    idx = existing_keys[key]
                    for k in ("arr", "dep_time", "arr_time"):
                        flights[idx][k] = nf[k]
            flights.sort(key=lambda x: (x["dep"], x["arr"], x["flight_num"]))
            with open(FLIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(flights, f, ensure_ascii=False, indent=2)
            log.info(f"flights_list.json 已更新：{len(flights)} 条（新增 {added} 条）")
        else:
            log.warning("ceair.com 发现失败，回退使用现有 flights_list.json")

    if not flights:
        log.error("航班列表为空，退出")
        return

    log.info(f"本次使用航班数：{len(flights)} 条")

    # ── Step 2: 爬 FlightView 数据（含最近 N 天缺失到达的回补）──
    csv_rows = load_csv_rows()
    today = beijing_now().date()

    def dstr(i: int) -> str:
        return (today - timedelta(days=i)).strftime("%Y%m%d")

    normal_dates   = {dstr(i) for i in range(1, args.days + 1)}      # 正常抓取：昨天起回溯 args.days 天
    backfill_dates = {dstr(i) for i in range(1, BACKFILL_DAYS + 1)}  # 回补窗口：最近 BACKFILL_DAYS 天
    all_dates = sorted(normal_dates | backfill_dates)
    log.info(f"正常抓取：{sorted(normal_dates)}；回补窗口：{sorted(backfill_dates)}")

    # (airline, flight_num, date, dep)；用 set 天然按 (flight,date,dep) 去重，避免重复落盘
    task_set: set[tuple] = set()
    backfill_n = 0
    for f in flights:
        flight = f"{f['airline']}{f['flight_num']}"
        dep    = f["dep"]
        for d in all_dates:
            row = csv_rows.get((flight, d, dep))
            if d in normal_dates:
                # 正常日期：缺行或未完成 → 抓
                if row is None or not is_complete(row):
                    task_set.add((f["airline"], f["flight_num"], d, dep))
            else:
                # 回补窗口：重抓「已存在但到达仍为空」的行，以及「整行缺失」的
                # （某天运行超时/中断导致整批漏抓时，可在窗口内自愈）
                if row is None or not is_complete(row):
                    task_set.add((f["airline"], f["flight_num"], d, dep))
                    backfill_n += 1

    task_list = sorted(task_set)
    log.info(f"待抓取：{len(task_list)} 个任务（其中回补 {backfill_n} 个），"
             f"现有 CSV {len(csv_rows)} 行")

    def upsert(result: dict):
        csv_rows[csv_key(result)] = result

    if task_list:
        asyncio.run(run_tasks(task_list, on_result=upsert))
        write_csv_rows(csv_rows)
        log.info(f"flights.csv 已写入：{len(csv_rows)} 行")
    else:
        log.info("无待抓取任务，跳过 FlightView 抓取")

    # ── Step 3: 生成前端 JSON ──
    log.info("生成前端 JSON 文件…")
    generate_route_jsons()

    log.info("全部完成。")


if __name__ == "__main__":
    main()
