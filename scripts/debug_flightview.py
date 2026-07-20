#!/usr/bin/env python3
"""
debug_flightview.py —— 诊断脚本：dump FlightView 页面原始文本

用于排查「到达时刻解析不出」的原因：对几个航班（含解析失败的跨天航班
与一个正常到达的对照），用与每日爬虫相同的方式打开 FlightView，
把 <main> 的 innerText 原样打印，并附上现有 parse_page_text 的解析结果，
以便对照真实标签（At Gate Time / Landing Time / Estimated Time / 带日期前缀…）。

用法：python scripts/debug_flightview.py
"""

import asyncio
from playwright.async_api import async_playwright

from ops_daily import parse_page_text  # 复用现有解析器做对照

BASE_URL = "https://www.flightview.com/flight-tracker"

# (airline, num, date, dep)
TARGETS = [
    ("MU", "8420", "20260719", "CAN"),  # 失败：跨天到达，仍为空
    ("MU", "8420", "20260715", "CAN"),  # 失败：跨天到达，仍为空
    ("MU", "8418", "20260719", "SHA"),  # 失败：起飞延误510
    ("MU", "8420", "20260716", "CAN"),  # 对照：正常到达（应有 At Gate Time）
]


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = await browser.new_page()
        for airline, num, date, dep in TARGETS:
            url = f"{BASE_URL}/{airline}/{num}?date={date}&depapt={dep}"
            print("\n" + "=" * 90)
            print(f"URL: {url}")
            print("=" * 90)
            try:
                await page.goto(url, timeout=35000, wait_until="domcontentloaded")
                await page.wait_for_function(
                    "(document.querySelector('main')?.innerText||'').includes('Track Another Flight')",
                    timeout=25000,
                )
                text = await page.locator("main").inner_text()
                print("----- RAW main.innerText -----")
                print(text)
                print("----- parse_page_text() 结果 -----")
                parsed = parse_page_text(text)
                if parsed:
                    for k in ("status", "dep_scheduled", "dep_actual",
                              "arr_scheduled", "arr_actual", "arr_airport"):
                        print(f"  {k} = {parsed.get(k)}")
                else:
                    print("  parse 返回 None")
            except Exception as e:
                print(f"ERROR: {type(e).__name__}: {e}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
