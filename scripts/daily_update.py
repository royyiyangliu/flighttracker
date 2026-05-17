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


async def scrape_price(query: dict) -> float | None:
    airline_code = query.get("airline_filter", "").upper()
    airline_name = AIRLINE_NAME_MAP.get(airline_code, airline_code)
    outbound = query["outbound_date"]
    is_roundtrip = query["type"] == "roundtrip"
    ret = query.get("return_date", "") if is_roundtrip else ""

    outbound_dt = datetime.strptime(outbound, "%Y-%m-%d")
    ret_dt = datetime.strptime(ret, "%Y-%m-%d") if ret else None

    url = build_url(query)
    print(f"  URL: {url}")
    print(f"  目标日期: {outbound}{' → ' + ret if ret else ''}")

    # 保存 API 响应到文件的目录
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

        xhr_prices: list[float] = []
        api_log_counter = [0]

        async def on_request(request):
            url_l = request.url.lower()
            if "restapi" in url_l or any(k in url_l for k in ("flight", "search", "ticket")):
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
            # 捕获所有 restapi 端点 + flight/search 关键词
            if "restapi" not in url_l and not any(
                k in url_l for k in ("flight", "search", "ticket")
            ):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                text = await response.text()
                if len(text) < 50:
                    return
                print(f"  [RESP] {response.url[:140]}")

                # 保存完整响应到文件（便于离线分析）
                api_log_counter[0] += 1
                log_name = f"resp_{api_log_counter[0]:02d}.json"
                endpoint = re.sub(r"[^\w]", "_", response.url.split("/")[-1][:40])
                log_path = api_log_dir / f"resp_{api_log_counter[0]:02d}_{endpoint}.json"
                try:
                    log_path.write_text(text, encoding="utf-8")
                    print(f"  [SAVED] {log_path.name} ({len(text)} bytes)")
                except Exception:
                    pass

                # 打印前 600 字符
                print(f"  [PREVIEW] {text[:600]}")

                data = json.loads(text)

                # 不限制航司，先收集所有有效价格
                all_prices = search_prices_in_json(data, airline_name="", depth=0)
                # 再用航司过滤收集一份
                airline_prices = search_prices_in_json(data, airline_name=airline_name, depth=0)

                if airline_prices:
                    print(f"  [XHR 航司匹配价格] {sorted(airline_prices)[:6]}")
                    xhr_prices.extend(airline_prices)
                elif all_prices:
                    print(f"  [XHR 所有价格] {sorted(all_prices)[:6]}")
                    # 如果没有航司匹配的价格，也记录所有价格作为候补
                    xhr_prices.extend(all_prices)

            except Exception as e:
                print(f"  [RESP ERR] {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(10000)

            # 检查页面是否包含正确的出发月份
            page_text = await page.inner_text("body")
            target_month = outbound_dt.strftime("%b")  # "Jul"
            target_year = str(outbound_dt.year)        # "2026"

            # 诊断：打印页面中找到的月份信息
            months_found = re.findall(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}",
                page_text
            )
            print(f"  页面包含月份: {list(set(months_found))[:10]}")

            if target_month in page_text and target_year in page_text:
                print(f"  ✓ 日期验证通过 ({target_month} {target_year})")
            else:
                print(f"  ✗ 页面未显示目标日期，尝试 JS 日历交互...")
                xhr_prices.clear()
                success = await set_dates_via_js(page, outbound_dt, ret_dt)
                if success:
                    print(f"  日历交互完成，等待结果加载...")
                    await page.wait_for_timeout(12000)
                    page_text2 = await page.inner_text("body")
                    months2 = re.findall(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}",
                        page_text2
                    )
                    print(f"  交互后页面月份: {list(set(months2))[:10]}")
                else:
                    print(f"  日历交互失败")

            # 保存截图
            debug_path = DATA_DIR / "debug_screenshot.png"
            await page.screenshot(path=str(debug_path))
            print(f"  截图已保存")

            # 优先用 XHR 价格
            valid_xhr = [x for x in xhr_prices if 1000 < x < 100000]
            if valid_xhr:
                result = min(valid_xhr)
                print(f"  [XHR] 最低价: ¥{result}")
                return result

            # 备用 DOM 解析
            result = await parse_dom_price(page, airline_name)
            if result:
                print(f"  [DOM] 最低价: ¥{result}")
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
        # 诊断：先列出所有含日期关键词的元素
        diag = await page.evaluate("""
            () => {
                const results = [];
                const re = /depart|arrive|date|calendar|picker/i;
                document.querySelectorAll('[class]').forEach(el => {
                    const cls = el.className.toString();
                    if (re.test(cls)) {
                        results.push({
                            tag: el.tagName,
                            cls: cls.slice(0, 80),
                            text: el.textContent.trim().slice(0, 40)
                        });
                    }
                });
                return results.slice(0, 15);
            }
        """)
        print(f"  日期相关元素: {json.dumps(diag, ensure_ascii=False)}")

        # 用 JS click 打开日期选择器（绕过 headless 可见性检查）
        clicked = await page.evaluate("""
            () => {
                const sels = [
                    '.nh_d-departTime',
                    '[class*="departTime"]',
                    '[class*="depart-time"]',
                    '[class*="departure-date"]',
                    '[class*="DepartureDate"]'
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) { el.click(); return s; }
                }
                return null;
            }
        """)
        if not clicked:
            print("  未找到日期元素，放弃日历交互")
            return False

        print(f"  JS click 成功: {clicked}")
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(DATA_DIR / "debug_calendar_open.png"))

        # 诊断：日历打开后的结构
        cal_info = await page.evaluate("""
            () => {
                const sels = [
                    '[class*="calendar"]',
                    '[class*="Calendar"]',
                    '[class*="datepicker"]',
                    '[class*="DatePicker"]'
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetHeight > 0) {
                        return {sel: s, text: el.textContent.slice(0, 200)};
                    }
                }
                return null;
            }
        """)
        print(f"  日历元素: {json.dumps(cal_info, ensure_ascii=False)}")

        # 导航到目标月份并点击日期
        ok1 = await navigate_and_click_js(page, outbound_dt)
        await page.wait_for_timeout(600)

        if ret_dt:
            ok2 = await navigate_and_click_js(page, ret_dt)
            await page.wait_for_timeout(600)

        await page.screenshot(path=str(DATA_DIR / "debug_calendar_selected.png"))

        # 点击搜索按钮
        search_sel = await page.evaluate("""
            () => {
                const sels = [
                    'button[class*="search"]', '[class*="search-btn"]',
                    '[class*="SearchBtn"]', 'button[type="submit"]',
                    '[class*="submit"]'
                ];
                for (const s of sels) {
                    const btn = document.querySelector(s);
                    if (btn) { btn.click(); return s; }
                }
                return null;
            }
        """)
        print(f"  搜索按钮点击: {search_sel}")
        return True

    except Exception as e:
        print(f"  JS 日历交互异常: {e}")
        return False


async def navigate_and_click_js(page: Page, target_dt: datetime) -> bool:
    """用 JS 导航日历月份并点击目标日期"""
    target_month = target_dt.strftime("%B")  # "July"
    target_year = str(target_dt.year)
    day_str = str(target_dt.day)

    for attempt in range(24):
        # 读取当前日历标题
        header = await page.evaluate("""
            () => {
                const sels = [
                    '[class*="calendar-header"]', '[class*="CalendarHeader"]',
                    '[class*="month-title"]', '[class*="MonthTitle"]',
                    '[class*="calendar-title"]', '[class*="CalendarTitle"]'
                ];
                for (const s of sels) {
                    const els = document.querySelectorAll(s);
                    for (const el of els) {
                        const t = el.textContent.trim();
                        if (t) return t;
                    }
                }
                // fallback: 找任何包含月份名的可见文本
                const months = ['January','February','March','April','May','June',
                                'July','August','September','October','November','December'];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.children.length === 0) {
                        const t = el.textContent.trim();
                        if (months.some(m => t.includes(m)) && /\\d{4}/.test(t)) return t;
                    }
                }
                return '';
            }
        """)
        print(f"  日历标题[{attempt}]: {header!r}")

        if target_month in header and target_year in header:
            break

        # 点击"下一个月"按钮
        clicked_next = await page.evaluate("""
            () => {
                const sels = [
                    '[class*="next-month"]', '[class*="NextMonth"]',
                    '[class*="next_month"]', '[aria-label*="next" i]',
                    'button[class*="next"]', '[class*="arrow-right"]',
                    '[class*="ArrowRight"]', '.nh_cal-next'
                ];
                for (const s of sels) {
                    const btn = document.querySelector(s);
                    if (btn) { btn.click(); return s; }
                }
                return null;
            }
        """)
        if not clicked_next:
            print(f"  未找到下一月按钮，停止导航")
            break
        await page.wait_for_timeout(300)

    # 点击目标日期
    clicked_day = await page.evaluate(f"""
        () => {{
            const daySels = [
                '[class*="calendar"] td',
                '[class*="CalendarDay"]',
                '[class*="day-cell"]',
                '[class*="cal-day"]',
                '.nh_cal-day'
            ];
            const target = '{day_str}';
            for (const s of daySels) {{
                const cells = document.querySelectorAll(s);
                for (const cell of cells) {{
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
    print(f"  日期点击结果: {clicked_day}")
    return clicked_day is not None


def search_prices_in_json(data, airline_name: str, depth: int = 0) -> list[float]:
    """递归在 JSON 中找价格。airline_name 为空时不过滤航司"""
    if depth > 8:
        return []
    results = []

    if isinstance(data, dict):
        # 检查当前 dict 是否含有航司信息（当 airline_name 非空时）
        if airline_name:
            text_vals = " ".join(str(v) for v in data.values() if isinstance(v, str))
            has_airline = airline_name.lower() in text_vals.lower()
        else:
            has_airline = True

        for k, v in data.items():
            k_lower = k.lower()
            if any(p in k_lower for p in ("price", "fare", "amount", "cost", "total", "money")):
                if isinstance(v, (int, float)) and 1000 < v < 100000:
                    if has_airline:
                        results.append(float(v))
                    elif not airline_name:
                        results.append(float(v))
            else:
                results.extend(search_prices_in_json(v, airline_name, depth + 1))

    elif isinstance(data, list):
        for item in data[:150]:
            results.extend(search_prices_in_json(item, airline_name, depth + 1))

    return results


async def parse_dom_price(page: Page, airline_name: str) -> float | None:
    """从渲染后 DOM 提取价格"""
    price_selectors = [
        "[class*='price-num']",
        "[class*='priceNum']",
        "[class*='flight-price'] strong",
        "[class*='flightPrice'] span",
        "[class*='price'] b",
        "[class*='Price'] strong",
    ]
    for selector in price_selectors:
        try:
            elements = await page.query_selector_all(selector)
            prices = []
            for el in elements:
                text = (await el.inner_text()).strip().replace(",", "")
                nums = re.findall(r'\d{4,6}', text)
                for n in nums:
                    v = float(n)
                    if 1000 < v < 100000:
                        prices.append(v)
            if prices:
                return min(prices)
        except Exception:
            continue

    if airline_name:
        try:
            body_text = await page.inner_text("body")
            idx = body_text.lower().find(airline_name.lower())
            if idx >= 0:
                snippet = body_text[max(0, idx - 100): idx + 600]
                nums = re.findall(r'\b(\d{4,5})\b', snippet)
                prices = [float(n) for n in nums if 1000 < float(n) < 100000]
                if prices:
                    return min(prices)
        except Exception:
            pass

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
            "currency": query.get("currency", "CNY"),
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
