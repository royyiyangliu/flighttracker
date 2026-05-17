import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, Page

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data" / "history"

CITY_MAP = {
    "PVG": "shanghai", "SHA": "shanghai",
    "NRT": "tokyo",    "HND": "tokyo",
    "PEK": "beijing",  "PKX": "beijing",
    "CAN": "guangzhou","CTU": "chengdu",
    "ICN": "seoul",    "HKG": "hong-kong",
    "SIN": "singapore","BKK": "bangkok",
    "LHR": "london",   "CDG": "paris",
    "JFK": "new-york", "LAX": "los-angeles",
    "SYD": "sydney",   "DXB": "dubai",
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
    dep = query["departure_id"].upper()
    arr = query["arrival_id"].upper()
    dep_city = CITY_MAP.get(dep, dep.lower())
    arr_city = CITY_MAP.get(arr, arr.lower())
    outbound = query["outbound_date"]
    base = (
        f"https://us.trip.com/flights/{dep_city}-to-{arr_city}/"
        f"tickets-{dep.lower()}-{arr.lower()}/"
    )
    params = f"?depdate={outbound}&class=y&quantity=1"
    if query["type"] == "roundtrip":
        params += f"&retdate={query['return_date']}"
    return base + params


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
        page = await context.new_page()

        # ── 关键：拦截 FlightListSearchSSE，替换搜索日期 ──────────────────
        route_triggered = [False]

        async def handle_sse_route(route, request):
            if "FlightListSearchSSE" not in request.url:
                await route.continue_()
                return
            try:
                body_text = request.post_data or "{}"
                body = json.loads(body_text)
                journeys = (
                    body.get("searchCriteria", {})
                    .get("journeyInfoTypes", [])
                )
                for j in journeys:
                    dep = j.get("departCode", "")
                    arr = j.get("arriveCode", "")
                    old_date = j.get("departDate", "")
                    if dep == dep_id and arr == arr_id:
                        j["departDate"] = outbound
                        print(f"  [ROUTE] 出发日修改: {old_date} → {outbound}")
                        route_triggered[0] = True
                    elif ret and dep == arr_id and arr == dep_id:
                        j["departDate"] = ret
                        print(f"  [ROUTE] 返程日修改: {old_date} → {ret}")
                modified = json.dumps(body)
                await route.continue_(post_data=modified)
            except Exception as e:
                print(f"  [ROUTE ERR] {e}")
                await route.continue_()

        await page.route("**/*FlightListSearchSSE*", handle_sse_route)
        # ──────────────────────────────────────────────────────────────────

        xhr_prices: list[float] = []
        api_log_counter = [0]

        async def on_request(request):
            url_l = request.url.lower()
            if "restapi" in url_l and any(
                k in url_l for k in ("flight", "search", "middle", "lowprice")
            ):
                body = ""
                try:
                    body = request.post_data or ""
                except Exception:
                    pass
                print(f"  [REQ] {request.method} {request.url[:140]}")
                if body:
                    print(f"  [REQ BODY] {body[:500]}")

        async def on_response(response):
            if response.status != 200:
                return
            url_l = response.url.lower()
            is_middle = "flightmiddlesearch" in url_l
            is_other_api = "restapi" in url_l and any(
                k in url_l for k in ("flight", "search", "lowprice")
            )
            if not (is_middle or is_other_api):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                text = await response.text()
                if len(text) < 50:
                    return
                data = json.loads(text)

                # 保存完整响应到文件
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
                    print(f"  [{tag}FlightMiddleSearch] {log_path.name} "
                          f"→ 价格: {sorted(prices)[:5]}")
                    xhr_prices.extend(prices)
                else:
                    prices = search_prices_in_json(data)
                    if prices:
                        print(f"  [API] {response.url[:80]} → 价格: {sorted(prices)[:5]}")

            except Exception as e:
                print(f"  [RESP ERR] {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"  页面加载完毕，等待搜索结果（25s）…")
            await page.wait_for_timeout(25000)

            # 路由拦截结果报告
            if route_triggered[0]:
                print(f"  ✓ FlightListSearchSSE 已拦截并替换日期为 {outbound}")
                # 路由拦截成功 → 数据已是正确日期，不清空价格、不触发日历交互
            else:
                print(f"  ✗ FlightListSearchSSE 未拦截，尝试 JS 日历交互…")
                # 仅在路由未触发时，才尝试日历操作
                page_text = await page.inner_text("body")
                months_found = re.findall(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}",
                    page_text,
                )
                print(f"  页面显示月份: {list(set(months_found))[:10]}")

                target_month = outbound_dt.strftime("%b")
                target_year = str(outbound_dt.year)
                if target_month not in page_text or target_year not in page_text:
                    xhr_prices.clear()
                    await set_dates_via_js(page, outbound_dt, ret_dt)
                    print(f"  日历交互完成，等待搜索结果…")
                    await page.wait_for_timeout(15000)

            # 保存截图
            await page.screenshot(path=str(DATA_DIR / "debug_screenshot.png"))
            print(f"  截图已保存")

            # 过滤有效价格（> 10，适配 USD 和 CNY）
            valid = [x for x in xhr_prices if x > 10]
            if valid:
                result = min(valid)
                currency = query.get("currency", "USD")
                print(f"  [最终] 最低价: {result} {currency}")
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
