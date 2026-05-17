import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, Page

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data" / "history"

# 机场代码 → trip.com 城市代码（用于 showfarefirst URL 参数）
AIRPORT_TO_CITY = {
    "PVG": "sha", "SHA": "sha",
    "NRT": "tyo", "HND": "tyo",
    "PEK": "bjs", "PKX": "bjs",
    "CAN": "can", "CTU": "ctu",
    "ICN": "sel", "HKG": "hkg",
    "SIN": "sin", "BKK": "bkk",
    "LHR": "lon", "CDG": "par",
    "JFK": "nyc", "EWR": "nyc", "LGA": "nyc",
    "LAX": "lax", "SYD": "syd", "DXB": "dxb",
}

AIRLINE_NAME_MAP = {
    "MU": "China Eastern",
    "CA": "Air China",
    "CZ": "China Southern",
    "MF": "Xiamen Air",
    "HU": "Hainan Airlines",
    "NH": "ANA",
    "JL": "Japan Airlines",
}


def build_url(query: dict) -> str:
    """
    使用 showfarefirst 端点构造 URL。
    该端点完整支持 ddate/rdate/curr 等参数，
    比旧版 /tickets-xxx/ 路径更可靠。
    """
    dep = query["departure_id"].upper()
    arr = query["arrival_id"].upper()
    dep_city = AIRPORT_TO_CITY.get(dep, dep[:3].lower())
    arr_city = AIRPORT_TO_CITY.get(arr, arr[:3].lower())
    outbound = query["outbound_date"]
    currency = query.get("currency", "CNY")
    quantity = query.get("quantity", 1)
    triptype = "rt" if query["type"] == "roundtrip" else "ow"
    cabin = query.get("cabin", "y")  # y=经济, c=商务, f=头等, s=超经

    params = (
        f"?dcity={dep_city}&acity={arr_city}"
        f"&ddate={outbound}"
        f"&dairport={dep.lower()}&aairport={arr.lower()}"
        f"&triptype={triptype}&class={cabin}"
        f"&quantity={quantity}"
        f"&nonstoponly=off&locale=en-US&curr={currency}"
        f"&lowpricesource=searchform&searchboxarg=t"
    )
    if query["type"] == "roundtrip":
        params += f"&rdate={query['return_date']}"

    return "https://us.trip.com/flights/showfarefirst" + params


def extract_from_middle_search(
    data: dict, airline_code: str
) -> tuple[list[float], set[str]]:
    """
    从 FlightMiddleSearch 提取价格，返回 (prices, airlines)。
    airline_code 不再用于过滤，只用于日志优先标记。
    价格单位：us.trip.com 返回 USD。
    """
    flight_airlines: set[str] = set()
    flight_nos: list[str] = []
    for journey in data.get("journeyList", []):
        for transport in journey.get("transportList", []):
            ai = transport.get("flight", {}).get("airlineInfo", {})
            code = ai.get("code", "")
            fn = transport.get("flight", {}).get("flightNo", "")
            if code:
                flight_airlines.add(code)
            if fn:
                flight_nos.append(fn)

    target_tag = "★" if airline_code and airline_code in flight_airlines else " "
    print(f"    {target_tag} 本次航司: {flight_airlines} | 航班: {flight_nos[:4]}")

    prices: list[float] = []
    for policy in data.get("policyList", []):
        total = policy.get("price", {}).get("totalPrice")
        if isinstance(total, (int, float)) and total > 10:
            prices.append(float(total))

    return prices, flight_airlines


def extract_from_calendar(data: dict, target_date: str) -> float | None:
    """
    从 GetLowPriceInCalender 响应中提取指定日期的最低价。
    target_date: "2026-07-25"

    trip.com 的 dDate 字段是 Unix 时间戳（UTC 午夜），不是日期字符串。
    同时用 dDateDisplayValue（如 "Sat, Jul 25"）做备用匹配。
    """
    import calendar as _cal
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    target_ts = int(_cal.timegm(dt.timetuple()))   # UTC 午夜时间戳
    # "Jul 25" — Linux 用 %-d 去掉前导零，GitHub Actions (Ubuntu) 支持
    target_mmdd = dt.strftime("%b %-d")

    items = data.get("lowPriceInCalenderDtoInfoList") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        d_ts   = item.get("dDate", 0)
        d_disp = item.get("dDateDisplayValue", "")
        if d_ts == target_ts or target_mmdd in d_disp:
            for key in ("currencyPrice", "price", "lowestPrice", "totalPrice"):
                v = item.get(key)
                if isinstance(v, (int, float)) and v > 100:
                    return float(v)
    return None


def parse_sse_chunk(
    chunk: dict,
    airline_code: str,
    mu_prices: list[float],
    all_prices: list[float],
) -> None:
    """
    解析 FlightListSearchSSE 的单个 data chunk。
    chunk 结构尚未完全确认，先把关键字段全部打印出来，
    同时尝试提取价格和航司。
    """
    # 顶层 key 概览（用于了解数据结构）
    top_keys = list(chunk.keys())

    # 打印 airlineList（如有）——用于了解本次搜索涉及的所有航司
    airline_list = chunk.get("airlineList")
    if isinstance(airline_list, list):
        codes = [a.get("code","") for a in airline_list if isinstance(a, dict)]
        print(f"  [SSE-airlines] 本次搜索涉及航司: {codes}")

    # 尝试找航班列表（trip.com 用 itineraryList）
    flight_list = None
    for key in ("itineraryList", "flightList", "flights", "flightInfoList",
                "data", "result", "flightInfo", "journeyList"):
        v = chunk.get(key)
        if isinstance(v, list) and len(v) > 0:
            flight_list = v
            break

    if flight_list is None:
        if top_keys and top_keys not in (["status"], ["done"]):
            print(f"  [SSE-chunk] 无航班列表，顶层keys={top_keys[:8]}")
        return

    print(f"  [SSE-chunk] 找到航班列表，共 {len(flight_list)} 条，key='{key}'")

    # 打印第一条航班的完整结构（仅首次）
    if flight_list and not getattr(parse_sse_chunk, "_printed_sample", False):
        parse_sse_chunk._printed_sample = True
        sample = json.dumps(flight_list[0], ensure_ascii=False)
        print(f"  [SSE-sample] 第1条航班原始结构（前800字）: {sample[:800]}")

    # 遍历每条航班，提取航司和价格
    for flight in flight_list:
        if not isinstance(flight, dict):
            continue

        # ── 提取航司代码 ────────────────────────────────
        # itineraryList 每条通常包含 legs/segments，每段有 airline
        carrier = ""
        candidate_paths = [
            # itineraryList 常见路径
            ["legs", 0, "marketAirline", "code"],
            ["legs", 0, "airline", "code"],
            ["legs", 0, "airlineCode"],
            ["segments", 0, "marketAirline", "code"],
            ["segments", 0, "airline", "code"],
            ["segments", 0, "airlineCode"],
            # 扁平字段
            ["airlineCode"],
            ["marketAirlineCode"],
            ["airline", "code"],
            ["airlineInfo", "code"],
        ]
        for path in candidate_paths:
            node = flight
            try:
                for p in path:
                    node = node[p]
                if isinstance(node, str) and 2 <= len(node) <= 3:
                    carrier = node.upper()
                    break
            except (KeyError, IndexError, TypeError):
                pass

        # ── 提取价格 ────────────────────────────────────
        price = None
        price_paths = [
            ["price", "totalPrice"],
            ["price", "salePrice"],
            ["priceInfo", "totalPrice"],
            ["lowestPrice"],
            ["totalPrice"],
            ["salePrice"],
            ["minPrice"],
            ["adultPrice"],
        ]
        for path in price_paths:
            node = flight
            try:
                for p in path:
                    node = node[p]
                if isinstance(node, (int, float)) and node > 10:
                    price = float(node)
                    break
            except (KeyError, IndexError, TypeError):
                pass

        tag = "★" if carrier == airline_code else " "
        if carrier or price:
            print(f"  [SSE-flight]{tag} 航司={carrier or '?':4s}  价格={price}")

        if price:
            all_prices.append(price)
            if carrier == airline_code:
                mu_prices.append(price)


def search_prices_in_json(data, depth: int = 0) -> list[float]:
    """兜底：递归搜索 JSON 中所有价格字段（不过滤航司）"""
    if depth > 8:
        return []
    results = []
    if isinstance(data, dict):
        for k, v in data.items():
            k_lower = k.lower()
            if any(p in k_lower for p in ("price", "fare", "amount", "cost", "total", "money")):
                if isinstance(v, (int, float)) and v > 10:
                    results.append(float(v))
            else:
                results.extend(search_prices_in_json(v, depth + 1))
    elif isinstance(data, list):
        for item in data[:150]:
            results.extend(search_prices_in_json(item, depth + 1))
    return results


async def scrape_price(query: dict) -> float | None:
    airline_code = query.get("airline_filter", "").upper()
    airline_name = AIRLINE_NAME_MAP.get(airline_code, airline_code)
    outbound = query["outbound_date"]
    is_roundtrip = query["type"] == "roundtrip"
    ret = query.get("return_date", "") if is_roundtrip else ""
    dep_id = query["departure_id"].upper()
    arr_id = query["arrival_id"].upper()

    outbound_dt = datetime.strptime(outbound, "%Y-%m-%d")
    ret_dt = datetime.strptime(ret, "%Y-%m-%d") if ret else None

    url = build_url(query)
    print(f"  URL: {url}")
    print(f"  目标日期: {outbound}{' → ' + ret if ret else ''}")
    print(f"  目标航司: {airline_code} ({airline_name})")

    api_log_dir = DATA_DIR / "api_logs"
    api_log_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        # 加载 cookies（来自 GitHub Secret，避免触发 CAPTCHA）
        cookies_path = BASE_DIR / "cookies.json"
        if cookies_path.exists():
            try:
                raw = json.loads(cookies_path.read_text(encoding="utf-8"))
                # 兼容 Cookie Editor 导出格式：expirationDate → expires
                playwright_cookies = []
                for c in raw:
                    cookie = {
                        "name":     c.get("name", ""),
                        "value":    c.get("value", ""),
                        "domain":   c.get("domain", ".trip.com"),
                        "path":     c.get("path", "/"),
                        "httpOnly": c.get("httpOnly", False),
                        "secure":   c.get("secure", False),
                        "sameSite": c.get("sameSite", "Lax"),
                    }
                    exp = c.get("expirationDate") or c.get("expires")
                    if exp and float(exp) > 0:
                        cookie["expires"] = float(exp)
                    if cookie["name"] and cookie["value"]:
                        playwright_cookies.append(cookie)
                if playwright_cookies:
                    await context.add_cookies(playwright_cookies)
                    print(f"  已加载 {len(playwright_cookies)} 个 cookies")
            except Exception as e:
                print(f"  cookies 加载失败: {e}")
        else:
            print("  未找到 cookies.json，以匿名模式运行")

        page = await context.new_page()

        # showfarefirst URL 已携带正确日期和货币，无需路由拦截
        # 仅记录 FlightListSearchSSE 的实际请求日期做验证
        sse_dates_seen: list[str] = []

        async def on_sse_request(route, request):
            """记录 SSE 请求参数（不修改），通过验证日期"""
            try:
                body = json.loads(request.post_data or "{}")
                for j in body.get("searchCriteria", {}).get("journeyInfoTypes", []):
                    d = j.get("departDate", "")
                    if d:
                        sse_dates_seen.append(d)
            except Exception:
                pass
            await route.continue_()

        await page.route("**/*FlightListSearchSSE*", on_sse_request)

        # 价格桶：分别记录 FlightMiddleSearch 和低价日历的价格
        mu_prices:  list[float] = []   # 目标航司价格
        all_prices: list[float] = []   # 全部航班价格（兜底）
        cal_prices: list[float] = []   # GetLowPriceInCalender 价格（最快出现）
        api_log_counter = [0]

        async def on_request(request):
            url_l = request.url.lower()
            if "restapi" in url_l and any(
                k in url_l for k in ("flightlist", "flightmiddle", "lowprice", "getlowprice")
            ):
                body = ""
                try:
                    body = request.post_data or ""
                except Exception:
                    pass
                print(f"  [REQ] {request.method} {request.url[:140]}")
                if body:
                    print(f"  [REQ BODY] {body[:400]}")

        async def on_response(response):
            if response.status != 200:
                return
            url_l = response.url.lower()
            is_middle   = "flightmiddlesearch" in url_l
            is_calendar = "getlowpricein" in url_l or "lowpriceincalender" in url_l
            is_sse      = "flightlistsearchsse" in url_l
            if not (is_middle or is_calendar or is_sse):
                return
            ct = response.headers.get("content-type", "")
            try:
                text = await response.text()
                if len(text) < 50:
                    return

                # ── SSE 流解析 ──────────────────────────────────────────
                if is_sse:
                    print(f"  [SSE] 收到流，总长度 {len(text)} 字节，解析中…")
                    chunks = []
                    for line in text.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "{}":
                            continue
                        try:
                            chunks.append(json.loads(raw))
                        except Exception:
                            pass
                    print(f"  [SSE] 共 {len(chunks)} 个 data chunk")

                    # 保存原始 SSE 文本用于离线分析
                    api_log_counter[0] += 1
                    log_path = api_log_dir / f"resp_{api_log_counter[0]:02d}_SSE.txt"
                    try:
                        log_path.write_text(text, encoding="utf-8")
                    except Exception:
                        pass

                    # 解析每个 chunk，提取航班信息
                    for chunk in chunks:
                        parse_sse_chunk(chunk, airline_code,
                                        mu_prices, all_prices)
                    return

                # ── JSON 响应解析 ────────────────────────────────────────
                if "json" not in ct:
                    return
                data = json.loads(text)

                api_log_counter[0] += 1
                endpoint = re.sub(r"[^\w]", "_", response.url.split("/")[-1][:40])
                log_path = api_log_dir / f"resp_{api_log_counter[0]:02d}_{endpoint}.json"
                try:
                    log_path.write_text(text, encoding="utf-8")
                except Exception:
                    pass

                if is_middle:
                    prices, airlines = extract_from_middle_search(data, airline_code)
                    tag = "★" if airline_code and airline_code in airlines else " "
                    print(f"  [{tag}FlightMiddleSearch] → {sorted(prices)[:5]}")
                    all_prices.extend(prices)
                    if airline_code and airline_code in airlines:
                        mu_prices.extend(prices)

                elif is_calendar:
                    items = data.get("lowPriceInCalenderDtoInfoList") or []
                    print(f"  [Calendar] items={len(items)}, "
                          f"preview={json.dumps(items[:2], ensure_ascii=False)[:200]}")
                    cal = extract_from_calendar(data, outbound)
                    if cal:
                        print(f"  [Calendar] ✓ 出发日 {outbound} → ¥{cal}")
                        cal_prices.append(cal)
                    else:
                        # 兜底：取所有合理价格中的最低值
                        raw = []
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            for key in ("price", "lowestPrice", "totalPrice",
                                        "lowestCurrencyPrice", "avgPrice"):
                                v = item.get(key)
                                if isinstance(v, (int, float)) and v > 100:
                                    raw.append(float(v))
                        if raw:
                            print(f"  [Calendar] 兜底价格列表: {sorted(raw)[:5]}")
                            cal_prices.extend(raw)
                        else:
                            # 兜底2：递归找所有价格字段
                            lcp = data.get("lowestCurrencyPrice")
                            if isinstance(lcp, (int, float)) and lcp > 100:
                                print(f"  [Calendar] lowestCurrencyPrice: {lcp}")
                                cal_prices.append(float(lcp))

            except Exception as e:
                print(f"  [RESP ERR] {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"  页面加载完毕，等待初始结果（15s）…")
            await page.wait_for_timeout(15000)

            # 模拟滚动，触发更多 FlightMiddleSearch 定价请求
            print(f"  开始滚动页面以触发更多航班定价…")
            for scroll_y in [800, 1600, 2400, 3200, 4000, 2000, 0]:
                await page.evaluate(f"window.scrollTo(0, {scroll_y})")
                await page.wait_for_timeout(3000)
                print(f"  滚动到 y={scroll_y}，当前已收集: MU={len(mu_prices)} 全部={len(all_prices)} 日历={len(cal_prices)}")

            print(f"  等待最终响应（10s）…")
            await page.wait_for_timeout(10000)

            # 验证 SSE 请求日期
            if sse_dates_seen:
                print(f"  FlightListSearchSSE 请求日期: {sse_dates_seen}")
                if outbound in sse_dates_seen:
                    print(f"  ✓ 日期验证通过（{outbound}）")
                else:
                    print(f"  ⚠ 日期不符，SSE 请求了: {sse_dates_seen}")

            # 保存截图
            await page.screenshot(path=str(DATA_DIR / "debug_screenshot.png"))
            print(f"  截图已保存")

            currency = query.get("currency", "CNY")

            # 优先级：目标航司 > 任意航班 > 低价日历
            if mu_prices:
                result = min(mu_prices)
                print(f"  [★MU 最低价] {result} {currency}")
                return result
            if all_prices:
                result = min(all_prices)
                print(f"  [全航司最低价] {result} {currency}")
                return result
            if cal_prices:
                result = min(cal_prices)
                print(f"  [低价日历最低价] {result} {currency}")
                return result

            # 兜底 DOM 解析
            result = await parse_dom_price(page, airline_name)
            if result:
                print(f"  [DOM] 最低价: {result}")
                return result

            print("  未找到有效价格")
            return None

        except Exception as e:
            print(f"  错误: {e}")
            try:
                await page.screenshot(path=str(DATA_DIR / "debug_screenshot.png"))
            except Exception:
                pass
            return None
        finally:
            await browser.close()


async def set_dates_via_js(page: Page, outbound_dt: datetime, ret_dt) -> bool:
    """用 page.evaluate() 绕过 headless 可见性限制，直接触发 JS click"""
    try:
        # JS click 打开日期选择器
        clicked = await page.evaluate("""
            () => {
                const sels = [
                    '.nh_d-departTime', '[class*="departTime"]',
                    '[class*="depart-time"]', '[class*="departure-date"]'
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) { el.click(); return s; }
                }
                return null;
            }
        """)
        if not clicked:
            print("  未找到日期元素")
            return False
        print(f"  JS click: {clicked}")
        await page.wait_for_timeout(2000)

        # 诊断：打印日历按钮信息
        cal_buttons = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button, [role="button"]')];
                return btns
                    .filter(b => b.offsetWidth > 0 || b.offsetParent !== null)
                    .map(b => ({tag: b.tagName, cls: b.className.toString().slice(0,60), text: b.textContent.trim().slice(0,20)}))
                    .slice(0, 20);
            }
        """)
        print(f"  可见按钮: {json.dumps(cal_buttons, ensure_ascii=False)}")

        await navigate_and_click_js(page, outbound_dt)
        await page.wait_for_timeout(600)
        if ret_dt:
            await navigate_and_click_js(page, ret_dt)
            await page.wait_for_timeout(600)

        # 点搜索按钮
        await page.evaluate("""
            () => {
                const sels = ['button[class*="search"]','[class*="search-btn"]','button[type="submit"]'];
                for (const s of sels) {
                    const btn = document.querySelector(s);
                    if (btn) { btn.click(); return s; }
                }
                return null;
            }
        """)
        return True
    except Exception as e:
        print(f"  JS 日历异常: {e}")
        return False


async def navigate_and_click_js(page: Page, target_dt: datetime) -> bool:
    target_month = target_dt.strftime("%B")
    target_year = str(target_dt.year)
    day_str = str(target_dt.day)

    for attempt in range(24):
        header = await page.evaluate("""
            () => {
                const months = ['January','February','March','April','May','June',
                                'July','August','September','October','November','December'];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.children.length === 0) {
                        const t = el.textContent.trim();
                        if (months.some(m => t.startsWith(m)) && /\\d{4}/.test(t))
                            return t.slice(0, 20);
                    }
                }
                return '';
            }
        """)
        if target_month in header and target_year in header:
            break

        # 点击 "下一月" 按钮（trip.com 特有类名 + 通用选择器）
        clicked_next = await page.evaluate("""
            () => {
                const sels = [
                    '.nh_cal-next', '[class*="nh_cal-next"]',
                    '[class*="next-month"]','[class*="NextMonth"]',
                    '[aria-label*="next" i]','[aria-label*="Next month" i]',
                    'button[class*="next"]','[class*="arrow"][class*="right"]',
                    '[class*="ArrowRight"]','[class*="right-arrow"]',
                    '[class*="cal-arrow"]','[class*="CalArrow"]',
                    '[class*="slider-arrow"]'
                ];
                for (const s of sels) {
                    const btn = document.querySelector(s);
                    if (btn) { btn.click(); return s; }
                }
                // 兜底：找含 ">" 或 "›" 文本的按钮
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim();
                    if (t === '>' || t === '›' || t === '→' || t === '▶') {
                        b.click(); return 'text:' + t;
                    }
                }
                return null;
            }
        """)
        if not clicked_next:
            print(f"  [第{attempt}次] 未找到下一月按钮，停止导航 (当前: {header!r})")
            break
        print(f"  [第{attempt}次] next btn: {clicked_next}, header={header!r}")
        await page.wait_for_timeout(400)

    # 点击目标日期
    clicked_day = await page.evaluate(f"""
        () => {{
            const target = '{day_str}';
            const sels = [
                '[class*="calendar"] td', '[class*="CalendarDay"]',
                '[class*="day-cell"]',   '[class*="cal-day"]',
                '.nh_cal-day'
            ];
            for (const s of sels) {{
                for (const cell of document.querySelectorAll(s)) {{
                    const t = cell.textContent.trim();
                    if (t === target && !cell.className.includes('disabled')
                            && !cell.className.includes('gray')
                            && !cell.className.includes('other')) {{
                        cell.click();
                        return s + ':' + t;
                    }}
                }}
            }}
            return null;
        }}
    """)
    print(f"  日期点击: {clicked_day}")
    return clicked_day is not None


async def parse_dom_price(page: Page, airline_name: str) -> float | None:
    selectors = [
        "[class*='price-num']", "[class*='priceNum']",
        "[class*='flight-price'] strong", "[class*='flightPrice'] span",
        "[class*='price'] b", "[class*='Price'] strong",
    ]
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
            prices = []
            for el in elements:
                text = (await el.inner_text()).strip().replace(",", "")
                for n in re.findall(r'\d{3,6}', text):
                    v = float(n)
                    if v > 50:
                        prices.append(v)
            if prices:
                return min(prices)
        except Exception:
            continue
    return None


def update_history(query: dict, price: float | None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{query['id']}.json"

    if path.exists():
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = {
            "id": query["id"],
            "label": query["label"],
            "type": query["type"],
            "currency": query.get("currency", "USD"),
            "dates": [],
            "prices": [],
        }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today in history["dates"]:
        history["prices"][history["dates"].index(today)] = price
    else:
        history["dates"].append(today)
        history["prices"].append(price)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"  历史已更新，共 {len(history['dates'])} 条记录")


async def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    for query in config["queries"]:
        print(f"\n{'=' * 55}")
        print(f"航线: {query['label']}")
        price = await scrape_price(query)
        print(f"最终价格: {price}")
        update_history(query, price)

    print(f"\n{'=' * 55}")
    print("全部完成")


if __name__ == "__main__":
    asyncio.run(main())
