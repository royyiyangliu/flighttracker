# flighttracker 项目说明

东方航空（MU）航班追踪系统。纯前端静态页 + Python 爬虫 + GitHub Actions 定时任务，**无后端、无数据库**，所有数据以 JSON/CSV 存于仓库内，由 Actions 自动抓取并 commit。

## 两条独立业务线

| 业务线 | 关注点 | 数据源 | 脚本 | 数据目录 |
|---|---|---|---|---|
| 票价（Price） | 指定往返航线票价随时间变化 | ceair.com（东航官网） | `scripts/daily_update.py` | `data/history/` |
| 运营（Ops） | 沪京/沪深航线机型、延误、准点率 | ceair.com + FlightView | `scripts/ops_daily.py` | `data/ops/` |

## 数据流

```
GitHub Actions（定时）
  ├─ daily_update.py → 爬东航票价 → data/history/{id}.json
  └─ ops_daily.py    → 发现航班号 + 爬 FlightView 运营 → data/ops/flights.csv → data/ops/{route}.json
        ↓ 自动 git commit & push
index.html（静态页）← fetch 读取上述 JSON（带 ?v=时间戳 绕缓存）
```

## 票价线

- `config.json` 定义要监控的航线（航司过滤 MU、出发/返回日期、币种等）。
- `daily_update.py`：Playwright（headless Chromium + stealth）打开东航 shopping 页 → TreeWalker 遍历 DOM 文本提取 MU 航班号/时刻/机型/价格 → 写入 `data/history/{id}.json`。
- 存储为**时序压缩格式**：`timestamps[]`（UTC 整点）+ `prices{航班号:[价格数组]}` + `flight_info{}`，各价格数组与时间戳等长，新航班补 null 占位；同一整点重跑覆盖而非追加。
- 定时：`.github/workflows/daily_update.yml`，每天北京时间 10:00、22:00。

## 运营线

`ops_daily.py` 三步：
1. **发现航班号**：从 ceair.com 扫 12 条沪京/沪深双向航线**明天**的 MU 航班号，合并进 `flights_list.json`（只增不减，随机顺序 + 8~15s 延迟防反爬）。
2. **抓运营数据**：用航班号去 FlightView 抓**昨天**（默认回溯 2 天）的状态/机型/计划实际起降时刻/航站楼，算延误（含跨午夜修正），增量追加 `flights.csv`（按 航班+日期 去重）。4 并发、请求间隔 2s、屏蔽图片字体。
3. **生成前端 JSON**：按航线聚合 CSV → 各 `{route}.json`。

- 定时：`.github/workflows/flight_ops.yml`，每天北京时间 04:00；支持手动触发（`days` 回溯天数、首次填 60；`skip_discover`）。
- `recalc_delays.py`：一次性脚本，用修正公式重算 CSV 全部延误列并重生成 JSON。

## 前端（index.html）

单文件 SPA，仅依赖 CDN 的 Chart.js。两个标签页：
- **运营**（默认）：城市对切换 + 机场/天数/机型筛选 + 准点率等统计卡 + 延误分布图 + 可排序明细表，读 `data/ops/*.json`。
- **票价**：读 `config.json` 航线列表 → 拉 `data/history/{id}.json` 展示价格走势。

## 注意

- 依赖（playwright、playwright-stealth）直接写在 workflow 里，无 requirements.txt。
- 数据强耦合页面 DOM，东航/FlightView 改版会导致解析失效。
- 解析逻辑只认 MU 航班（`MU\d{3,4}`），不含 FM 共享。
