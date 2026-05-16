import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data" / "history"

# 机场代码 → trip.com URL 所需城市名称
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
    "ORD": "chicago",  "SFO": "san-francisco",
    "FRA": "frankfurt","AMS": "amsterdam",
}

# 航司 IATA 代码 → 完整名称（用于页面文本匹配）
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
    params = f"?depdate={outbound}&class=y&quantity=1&searchboxarg=t"
    if query["type"] == "roundtrip":
        params += f"&retdate={query['return_date']}"
    return base + params


async def scrape_price(query: dict) -> float | None:
    url = build_url(query)
    airline_code = query.get("airline_filter", "").upper()
    airline_name = AIRLINE_NAME_MAP.get(airline_code, airline_code)
    print(f"  URL: {url}")
    print(f"  筛选航司: {airline_name} ({airline_code})")

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

        # 拦截 XHR 响应，寻找含价格的 API 数据
        xhr_prices: list[float] = []

        async def on_response(response):
            if response.status != 200:
                return
            if not any(k in response.url.lower() for k in ("flight", "search", "ticket", "intl", "query")):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                data = await response.json()
                found = search_prices_in_json(data, airline_name)
                if found:
                    print(f"  [XHR] {response.url[:80]} → 找到价格: {found[:5]}")
                    xhr_prices.extend(found)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # 等待 JS 渲染和价格加载
            await page.wait_for_timeout(12000)

            # 始终保存截图，供调试使用
            debug_path = DATA_DIR / "debug_screenshot.png"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(debug_path), full_page=False)
            print(f"  截图已保存: {debug_path}")

            # 优先使用 XHR 拦截到的价格
            valid_xhr = [p for p in xhr_prices if 1000 < p < 100000]
            if valid_xhr:
                result = min(valid_xhr)
                print(f"  [XHR] 最低价: ¥{result}")
                return result

            # 备选：解析 DOM（国际往返票最低 ¥1000）
            result = await parse_dom_price(page, airline_name)
            if result:
                print(f"  [DOM] 最低价: ¥{result}")
                return result

            print(f"  未找到有效价格")
            return None

        except Exception as e:
            print(f"  爬取出错: {e}")
            return None
        finally:
            await browser.close()


def search_prices_in_json(data, airline_name: str, depth: int = 0) -> list[float]:
    """在 JSON 数据中递归寻找与航司关联的价格字段"""
    if depth > 7:
        return []
    results = []

    if isinstance(data, dict):
        # 检查当前节点是否同时含有航司名和价格
        text_vals = " ".join(str(v) for v in data.values() if isinstance(v, str))
        has_airline = airline_name.lower() in text_vals.lower()

        for k, v in data.items():
            k_lower = k.lower()
            if any(p in k_lower for p in ("price", "fare", "amount", "cost", "total")):
                if isinstance(v, (int, float)) and 200 < v < 100000:
                    if has_airline or not airline_name:
                        results.append(float(v))
            else:
                results.extend(search_prices_in_json(v, airline_name, depth + 1))

    elif isinstance(data, list):
        for item in data[:100]:
            results.extend(search_prices_in_json(item, airline_name, depth + 1))

    return results


async def parse_dom_price(page, airline_name: str) -> float | None:
    """从渲染后的 DOM 中提取价格，尝试多种选择器"""
    # trip.com 常见价格选择器（Webpack 混淆后可能不同，按优先级尝试）
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
                nums = re.findall(r'\d{3,6}', text)
                for n in nums:
                    v = float(n)
                    if 1000 < v < 100000:  # 国际往返票最低 ¥1000
                        prices.append(v)
            if prices:
                return min(prices)
        except Exception:
            continue

    # 最后备选：从页面全文中在航司名附近提取价格
    if airline_name:
        try:
            body_text = await page.inner_text("body")
            idx = body_text.lower().find(airline_name.lower())
            if idx >= 0:
                snippet = body_text[max(0, idx - 100): idx + 600]
                nums = re.findall(r'\b(\d{3,5})\b', snippet)
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
