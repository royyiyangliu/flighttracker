"""
本地测试：验证 SSE itinerary 价格 + 航司提取逻辑
运行：python scripts/test_sse_parse.py
"""

AIRLINE_CODE = "MU"

def try_extract_airline(itinerary: dict) -> str:
    """trip.com 已确认路径：journeyList->transSectionList->flightInfo.airlineCode"""
    try:
        return itinerary["journeyList"][0]["transSectionList"][0]["flightInfo"]["airlineCode"].upper()
    except (KeyError, IndexError, TypeError):
        pass
    for path in [["airlineCode"], ["marketAirlineCode"], ["airline", "code"]]:
        node = itinerary
        try:
            for p in path:
                node = node[p]
            if isinstance(node, str) and 2 <= len(node) <= 3:
                return node.upper()
        except (KeyError, IndexError, TypeError):
            pass
    return ""


def try_extract_price(itinerary: dict) -> float | None:
    """尝试所有已知路径提取价格"""
    price_paths = [
        ["priceList", 0, "adultPrice"],
        ["priceList", 0, "totalPrice"],
        ["priceList", 0, "price", "adultPrice"],
        ["priceList", 0, "price", "totalPrice"],
        ["price", "totalPrice"],
        ["price", "adultPrice"],
        ["priceInfo", "totalPrice"],
        ["lowestPrice"],
        ["totalPrice"],
        ["adultPrice"],
        ["minPrice"],
    ]
    for path in price_paths:
        node = itinerary
        try:
            for p in path:
                node = node[p]
            if isinstance(node, (int, float)) and node > 10:
                return float(node), ".".join(str(p) for p in path)
        except (KeyError, IndexError, TypeError):
            pass
    return None, None


# ── 模拟各种结构 ───────────────────────────────────────────────
def make_itinerary(airline, price_structure: dict) -> dict:
    base = {
        "journeyList": [{
            "journeyNo": 1,
            "transSectionList": [{
                "segmentNo": 1,
                "flightInfo": {"flightNo": f"{airline}539", "airlineCode": airline},
                "departDateTime": "2026-07-25 08:00:00",
                "arriveDateTime": "2026-07-25 13:05:00",
            }]
        }]
    }
    base.update(price_structure)
    return base


test_cases = [
    ("priceList[0].adultPrice",   {"priceList": [{"adultPrice": 3560, "adultTax": 210}]}),
    ("priceList[0].totalPrice",   {"priceList": [{"totalPrice": 3770}]}),
    ("priceList[0].price.adult",  {"priceList": [{"price": {"adultPrice": 3560}}]}),
    ("price.totalPrice",          {"price": {"totalPrice": 3770}}),
    ("price.adultPrice",          {"price": {"adultPrice": 3560}}),
    ("lowestPrice",               {"lowestPrice": 3560}),
    ("adultPrice (flat)",         {"adultPrice": 3560}),
]

print(f"{'结构':<30} {'航司':<6} {'价格':<10} {'命中路径'}")
print("-" * 65)
for name, price_struct in test_cases:
    itinerary = make_itinerary("MU", price_struct)
    airline = try_extract_airline(itinerary)
    price, path = try_extract_price(itinerary)
    print(f"{name:<30} {airline:<6} {str(price):<10} {path or '-'}")

print()
print("结论：以上所有路径在代码里都已覆盖，实际哪个命中取决于 trip.com 的真实字段名。")
print("下次跑一次，看 [SSE-sample] 顶层keys 就知道了。")
