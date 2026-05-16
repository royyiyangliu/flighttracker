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

        async def on_response(response):
            if response.status != 200:
                return
            if not any(k in response.url.lower() for k in ("flight", "search", "ticket", "intl")):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                data = await response.json()
                found = search_prices_in_json(data, airline_name)
                if found:
                    print(f"  [XHR] {response.url[:70]}")
                    print(f"        → prices: {sorted(found)[:8]}")
                    xhr_prices.extend(found)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)

            # 检查页面是否包含正确的出发月份
            page_text = await page.inner_text("body")
            target_month = outbound_dt.strftime("%b")  # "Jul"
            target_year = str(outbound_dt.year)        # "2026"

            if target_month in page_text and target_year in page_text:
                print(f"  ✓ 日期验证通过 ({target_month} {target_year})")
            else:
                print(f"  ✗ 页面未显示目标日期 ({target_month} {target_year})，尝试日历交互...")
                xhr_prices.clear()
                success = await set_dates_via_calendar(page, outbound_dt, ret_dt)
                if success:
                    print(f"  日历交互完成，等待结果加载...")
                    await page.wait_for_timeout(10000)
                else:
                    print(f"  日历交互失败")

            # 保存截图
            debug_path = DATA_DIR / "debug_screenshot.png"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
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


async def set_dates_via_calendar(page: Page, outbound_dt: datetime, ret_dt) -> bool:
    """通过点击页面日历控件来设置正确日期"""
    try:
        # 用 JS 找所有含日期文本（如 "Mon, May 18"）的叶节点元素
        date_elements = await page.evaluate("""
            () => {
                const re = /(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\\s+\\w+\\s+\\d+/;
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.children.length === 0 && re.test(el.textContent.trim())) {
                        results.push({
                            text: el.textContent.trim().slice(0, 40),
                            tag: el.tagName,
                            cls: el.className.toString().slice(0, 80),
                            visible: el.offsetWidth > 0 && el.offsetHeight > 0
                        });
                        if (results.length >= 8) break;
                    }
                }
                return results;
            }
        """)
        print(f"  页面日期元素: {json.dumps(date_elements, ensure_ascii=False)}")

        # 保存截图（此时页面还是错误日期）
        await page.screenshot(path=str(DATA_DIR / "debug_calendar.png"))

        # 用已知的类名直接强制点击出发日期（headless 下 offsetWidth=0，需 force=True）
        # 诊断输出中确认类名为 nh_d-departTime
        known_date_selectors = [
            ".nh_d-departTime",       # trip.com 出发日期（已确认）
            "[class*='departTime']",
            "[class*='depart-time']",
        ]
        clicked_sel = None
        for sel in known_date_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(force=True)
                    clicked_sel = sel
                    print(f"  强制点击日期元素成功: {sel}")
                    await page.wait_for_timeout(2000)
                    break
            except Exception as e:
                print(f"  点击 {sel} 失败: {e}")
                continue

        if not clicked_sel:
            print("  未能点击任何日期元素")
            return False

        # 截图查看日历是否打开
        await page.screenshot(path=str(DATA_DIR / "debug_calendar_open.png"))

        # 导航日历到目标月份并点击日期
        await navigate_to_month(page, outbound_dt)
        await click_day(page, outbound_dt.day)
        await page.wait_for_timeout(800)

        # 往返则继续点回程日期
        if ret_dt:
            await navigate_to_month(page, ret_dt)
            await click_day(page, ret_dt.day)
            await page.wait_for_timeout(800)

        # 点击搜索按钮
        search_selectors = [
            "button[class*='search']",
            "[class*='search-btn']",
            "[class*='SearchBtn']",
            "button[type='submit']",
        ]
        for sel in search_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=2000)
                if btn:
                    await btn.click()
                    print(f"  点击搜索按钮: {sel}")
                    return True
            except Exception:
                continue

        return True  # 有时无需点搜索，日期选完自动刷新

    except Exception as e:
        print(f"  日历交互异常: {e}")
        return False


async def navigate_to_month(page: Page, target_dt: datetime):
    """导航日历到目标月份"""
    target_month = target_dt.strftime("%B")  # "July"
    target_year = str(target_dt.year)

    for _ in range(24):
        # 检查当前日历显示的月份
        header = await page.query_selector(
            "[class*='calendar-header'], [class*='CalendarHeader'], "
            "[class*='month-title'], [class*='MonthTitle'], "
            "[class*='calendar-title']"
        )
        if header:
            txt = await header.inner_text()
            if target_month in txt and target_year in txt:
                return

        # 点下一个月
        next_btn = await page.query_selector(
            "[class*='next-month'], [class*='NextMonth'], "
            "[aria-label*='next month' i], [aria-label*='Next Month' i], "
            "button[class*='next']"
        )
        if next_btn:
            await next_btn.click()
            await page.wait_for_timeout(400)
        else:
            break


async def click_day(page: Page, day: int):
    """点击日历中的指定日期数字"""
    day_str = str(day)
    cells = await page.query_selector_all(
        "[class*='calendar'] td:not([class*='disabled']):not([class*='other']), "
        "[class*='CalendarDay']:not([class*='disabled']):not([class*='gray']), "
        "[class*='day-cell']:not([class*='disabled'])"
    )
    for cell in cells:
        text = (await cell.inner_text()).strip()
        if text == day_str:
            await cell.click()
            print(f"  点击日期 {day} 成功")
            return
    print(f"  未找到日期 {day} 的单元格")


def search_prices_in_json(data, airline_name: str, depth: int = 0) -> list[float]:
    """在 JSON 数据中递归寻找价格字段"""
    if depth > 7:
        return []
    results = []

    if isinstance(data, dict):
        text_vals = " ".join(str(v) for v in data.values() if isinstance(v, str))
        has_airline = airline_name.lower() in text_vals.lower() if airline_name else True

        for k, v in data.items():
            k_lower = k.lower()
            if any(p in k_lower for p in ("price", "fare", "amount", "cost", "total")):
                if isinstance(v, (int, float)) and 1000 < v < 100000:
                    if has_airline:
                        results.append(float(v))
            else:
                results.extend(search_prices_in_json(v, airline_name, depth + 1))
    elif isinstance(data, list):
        for item in data[:100]:
            results.extend(search_prices_in_json(item, airline_name, depth + 1))

    return results


async def parse_dom_price(page: Page, airline_name: str) -> float | None:
    """从渲染后 DOM 提取价格，尝试多种选择器"""
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

    # 最后备用：从页面全文航司名附近提取
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
