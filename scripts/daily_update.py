import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, Page

try:
    from playwright_stealth import Stealth as _Stealth
    _stealth_instance = _Stealth()
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

BASE_DIR    = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR    = BASE_DIR / "data" / "history"


# ── URL 构造 ──────────────────────────────────────────────────────
def build_ceair_url(query: dict) -> str:
    dep      = query["departure_id"].upper()
    arr      = query["arrival_id"].upper()
    outbound = query["outbound_date"]
    ret      = query.get("return_date", "")
    curr     = query.get("currency", "CNY").lower()
    triptype = "roundtrip" if query["type"] == "roundtrip" else "oneway"
    if triptype == "roundtrip" and ret:
        return f"https://www.ceair.com/zh/{curr}/shopping/{triptype}/{dep}-{arr}/{outbound},{ret}"
    return f"https://www.ceair.com/zh/{curr}/shopping/{triptype}/{dep}-{arr}/{outbound}"


# ── DOM 提取 JS（只提取 MU 航班，不含 FM 共享）────────────────────
DOM_EXTRACT_JS = """
() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const seen = new Set();
    const flights = [];
    let node;
    while (node = walker.nextNode()) {
        const t = node.textContent.trim();
        if (!/^[\\d,]{4,6}$/.test(t)) continue;
        const num = parseInt(t.replace(/,/g, ''));
        if (num < 500 || num > 99999) continue;
        let ctx = node.parentElement;
        for (let i = 0; i < 12; i++) {
            if (!ctx) break;
            const full = ctx.innerText || '';
            const lines = full.split('\\n')
                .map(l => l.trim())
                .filter(l => l && l !== '—' && l !== '— —');
            if (!lines.length) { ctx = ctx.parentElement; continue; }
            const fm = lines[0].match(/^(MU)(\\d{3,4})$/);
            if (!fm || !full.includes('¥')) { ctx = ctx.parentElement; continue; }
            const key = lines[0];
            if (seen.has(key)) break;
            seen.add(key);
            const times      = lines.filter(l => /^\\d{2}:\\d{2}$/.test(l));
            const priceLines = lines.filter(l => /^¥\\s*[\\d,]+$/.test(l));
            const prices     = priceLines.map(l => parseInt(l.replace(/[¥\\s,]/g, '')));
            const aircraft   = lines.find(l => /空客|波音/i.test(l)) || '';
            flights.push({
                flightNo: fm[1] + fm[2],
                depTime:  times[0] || null,
                arrTime:  times[1] || null,
                aircraft: aircraft,
                direct:   lines.includes('直达'),
                price:    prices[0] || null,
            });
            break;
        }
    }
    return flights;
}
"""


# ── 主抓取函数 ────────────────────────────────────────────────────
async def scrape_flights(query: dict) -> list[dict]:
    """抓取 ceair.com，返回 MU 航班列表（每项含航班详情+价格）"""
    url          = build_ceair_url(query)
    airline_code = query.get("airline_filter", "MU").upper()

    print(f"  URL: {url}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = DATA_DIR / "debug_screenshot.png"

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

        if HAS_STEALTH:
            await _stealth_instance.apply_stealth_async(page)
            print("  [stealth] 已应用补丁")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等待价格数字出现，最多 30 秒
            print("  等待价格加载…")
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

            await page.wait_for_timeout(2000)

            flights = await page.evaluate(DOM_EXTRACT_JS)
            await page.screenshot(path=str(screenshot_path))

            # 只保留目标航司（MU）
            flights = [f for f in flights if f.get("flightNo", "").startswith(airline_code)]

            if flights:
                print(f"  共解析 {len(flights)} 个 {airline_code} 航班：")
                for f in flights:
                    tag = "★"
                    print(f"    {tag} {f['flightNo']:8s}  "
                          f"{f['depTime'] or '?'}→{f['arrTime'] or '?'}  "
                          f"{f['aircraft']:12s}  ¥{f['price']:,}" if f['price'] else
                          f"    {tag} {f['flightNo']:8s}  "
                          f"{f['depTime'] or '?'}→{f['arrTime'] or '?'}  "
                          f"{f['aircraft']:12s}  价格未知")
            else:
                print("  未解析到任何 MU 航班")

            return flights

        except Exception as e:
            print(f"  错误: {e}")
            try:
                await page.screenshot(path=str(screenshot_path))
            except Exception:
                pass
            return []
        finally:
            await browser.close()


# ── 历史记录更新（新格式）────────────────────────────────────────
def update_history(query: dict, flights: list[dict]):
    """
    数据格式：
    {
      "id": ..., "label": ..., "currency": ...,
      "flight_info": { "MU727": {"depTime","arrTime","aircraft","direct"}, ... },
      "timestamps":  ["2026-05-17T02:00", ...],   // UTC，截断到整点
      "prices":      { "MU727": [3955, null, ...], ... }
    }
    timestamps 与 prices 各数组等长。
    """
    if not flights:
        print("  无航班数据，跳过历史更新")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{query['id']}.json"

    # 读取或初始化（兼容旧格式：检测是否有 timestamps）
    history = None
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if "timestamps" in raw and isinstance(raw.get("prices"), dict):
                history = raw
            else:
                print("  检测到旧格式，重置为新格式")
        except Exception:
            pass

    if history is None:
        history = {
            "id":          query["id"],
            "label":       query["label"],
            "currency":    query.get("currency", "CNY"),
            "flight_info": {},
            "timestamps":  [],
            "prices":      {},
        }

    # 当前 UTC 时间，截断到整点（避免重复记录）
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    n      = len(history["timestamps"])

    if now_ts in history["timestamps"]:
        # 同一小时内重跑：更新已有条目
        idx = history["timestamps"].index(now_ts)
        for f in flights:
            fn = f["flightNo"]
            if fn not in history["prices"]:
                history["prices"][fn] = [None] * n
            history["prices"][fn][idx] = f["price"]
        print(f"  更新已有时间点 [{now_ts}]（第 {idx+1} 条）")
    else:
        # 新时间点：追加
        history["timestamps"].append(now_ts)
        flight_map    = {f["flightNo"]: f for f in flights}
        all_flight_nos = set(history["prices"].keys()) | set(flight_map.keys())

        for fn in all_flight_nos:
            if fn not in history["prices"]:
                # 新出现的航班：补 None 占位
                history["prices"][fn] = [None] * n
            price = flight_map.get(fn, {}).get("price")
            history["prices"][fn].append(price)

        print(f"  追加新时间点 [{now_ts}]，共 {len(history['timestamps'])} 条记录")

    # 更新航班静态信息（优先使用非空值）
    for f in flights:
        fn       = f["flightNo"]
        existing = history["flight_info"].get(fn, {})
        history["flight_info"][fn] = {
            "depTime":  f.get("depTime")  or existing.get("depTime", ""),
            "arrTime":  f.get("arrTime")  or existing.get("arrTime", ""),
            "aircraft": f.get("aircraft") or existing.get("aircraft", ""),
            "direct":   f.get("direct", True),
        }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    n_ts = len(history["timestamps"])
    n_fl = len(history["prices"])
    print(f"  已保存：{n_ts} 个时间点 × {n_fl} 条航班")


# ── 入口 ──────────────────────────────────────────────────────────
async def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    for query in config["queries"]:
        print(f"\n{'=' * 55}")
        print(f"航线: {query['label']}")
        flights = await scrape_flights(query)
        update_history(query, flights)

    print(f"\n{'=' * 55}")
    print("全部完成")


if __name__ == "__main__":
    asyncio.run(main())
