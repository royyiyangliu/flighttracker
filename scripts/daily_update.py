import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, Page

try:
    from playwright_stealth import Stealth as _Stealth
    _stealth_instance = _Stealth()
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data" / "history"

AIRLINE_NAME_MAP = {
    "MU": "中国东方航空",
    "CA": "中国国际航空",
    "CZ": "中国南方航空",
    "MF": "厦门航空",
    "NH": "全日空",
    "JL": "日本航空",
}

# ── ceair.com URL 构造 ────────────────────────────────────────────
def build_ceair_url(query: dict) -> str:
    """
    东航官网往返查询 URL:
    https://www.ceair.com/zh/cny/shopping/roundtrip/{DEP}-{ARR}/{outbound},{return}
    """
    dep = query["departure_id"].upper()
    arr = query["arrival_id"].upper()
    outbound = query["outbound_date"]
    ret = query.get("return_date", "")
    lang = "zh"
    curr = query.get("currency", "CNY").lower()
    triptype = "roundtrip" if query["type"] == "roundtrip" else "oneway"

    if triptype == "roundtrip" and ret:
        return f"https://www.ceair.com/{lang}/{curr}/shopping/{triptype}/{dep}-{arr}/{outbound},{ret}"
    else:
        return f"https://www.ceair.com/{lang}/{curr}/shopping/{triptype}/{dep}-{arr}/{outbound}"


# ── DOM 解析：从页面提取航班+价格 ─────────────────────────────────
DOM_EXTRACT_JS = """
() => {
    // 遍历文本节点，找价格数字（3~5位），再向上找含航班号的祖先
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const seen = new Set();
    const flights = [];
    let node;
    while (node = walker.nextNode()) {
        const t = node.textContent.trim();
        // 匹配纯数字或带逗号的价格（500-99999）
        if (!/^[\d,]{3,7}$/.test(t)) continue;
        const num = parseInt(t.replace(/,/g, ''));
        if (num < 500 || num > 99999) continue;
        let ctx = node.parentElement;
        for (let i = 0; i < 10; i++) {
            if (!ctx) break;
            const full = ctx.innerText || '';
            const flightMatch = full.match(/([A-Z]{2})\s*(\d{3,4})/);
            const priceMatch = full.match(/[¥￥]\s*([\d,]+)/g);
            if (flightMatch && priceMatch) {
                const airline = flightMatch[1];
                const flightNo = flightMatch[1] + flightMatch[2];
                const price = parseInt(priceMatch[0].replace(/[¥￥,\s]/g, ''));
                const key = flightNo + '|' + price;
                if (!seen.has(key) && price > 500) {
                    seen.add(key);
                    flights.push({ airline, flightNo, price });
                }
                break;
            }
            ctx = ctx.parentElement;
        }
    }
    return flights;
}
"""

async def parse_ceair_dom(page: Page, airline_filter: str) -> tuple[list[float], list[float]]:
    """
    从 ceair.com 页面 DOM 提取航班价格。
    返回 (airline_prices, all_prices)
    airline_filter: 目标航司代码（如 "MU"）
    """
    flights = await page.evaluate(DOM_EXTRACT_JS)
    airline_prices = []
    all_prices = []

    for f in flights:
        airline = f.get("airline", "")
        flight_no = f.get("flightNo", "")
        price = f.get("price")
        if not price:
            continue
        tag = "★" if airline == airline_filter else " "
        print(f"  [DOM]{tag} {flight_no:8s}  ¥{price:,}")
        all_prices.append(float(price))
        if airline == airline_filter:
            airline_prices.append(float(price))

    return airline_prices, all_prices


# ── 主抓取函数 ────────────────────────────────────────────────────
async def scrape_price(query: dict) -> float | None:
    source = query.get("source", "ceair")
    airline_code = query.get("airline_filter", "").upper()
    airline_name = AIRLINE_NAME_MAP.get(airline_code, airline_code)

    if source == "ceair":
        url = build_ceair_url(query)
    else:
        raise ValueError(f"未知 source: {source}")

    print(f"  来源: {source}")
    print(f"  URL:  {url}")
    print(f"  目标航司: {airline_code} ({airline_name})")

    screenshot_path = DATA_DIR / "debug_screenshot.png"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

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
            locale="zh-CN",
        )
        page = await context.new_page()

        # 应用 stealth 补丁，规避 navigator.webdriver 等指纹检测
        if HAS_STEALTH:
            await _stealth_instance.apply_stealth_async(page)
            print("  [stealth] 已应用补丁")
        else:
            print("  [stealth] 未安装，以普通 headless 运行")

        try:
            print(f"  正在加载页面…")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等待航班价格出现：最多等 30 秒
            print(f"  等待航班价格加载（最多30秒）…")
            try:
                await page.wait_for_function(
                    """() => {
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        let node;
                        while (node = walker.nextNode()) {
                            const t = node.textContent.trim();
                            if (/^[\\d,]{4,6}$/.test(t)) {
                                const n = parseInt(t.replace(/,/g,''));
                                if (n > 500 && n < 99999) return true;
                            }
                        }
                        return false;
                    }""",
                    timeout=30000,
                )
                print("  ✓ 价格已出现")
            except Exception:
                print("  ⚠ 等待超时，仍尝试解析")

            # 额外等待 3 秒让所有航班渲染完毕
            await page.wait_for_timeout(3000)

            # 提取航班价格
            airline_prices, all_prices = await parse_ceair_dom(page, airline_code)
            print(f"  共找到 {len(all_prices)} 个价格，其中 {airline_code} {len(airline_prices)} 个")

            # 截图留存
            await page.screenshot(path=str(screenshot_path))
            print(f"  截图已保存")

            currency = query.get("currency", "CNY")

            if airline_prices:
                result = min(airline_prices)
                print(f"  [★{airline_code} 最低价] ¥{result:,.0f} {currency}")
                return result
            if all_prices:
                result = min(all_prices)
                print(f"  [全航司最低价] ¥{result:,.0f} {currency}")
                return result

            print("  未找到有效价格")
            return None

        except Exception as e:
            print(f"  错误: {e}")
            try:
                await page.screenshot(path=str(screenshot_path))
            except Exception:
                pass
            return None
        finally:
            await browser.close()


# ── 历史记录更新 ──────────────────────────────────────────────────
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


# ── 入口 ──────────────────────────────────────────────────────────
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
