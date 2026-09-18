# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（前复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 两种运行模式

```bash
uv run main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
uv run main.py --backfill    # 回填模式：全市场历史K线一次性灌入（约12分钟）
```

---

## 策略与监控 | Strategies & Monitors

### 1. 量化技术选股策略（基于本地历史日 K 向量化计算）

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **BollBreakout** | 布林通道突破：前期低波动带宽收口蓄势 + 当日放量向上穿透上轨 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |

### 2. 事件驱动与公告监控（基于外部接口实时抓取）

| 模块 | 说明 |
|---|---|
| **PrivatePlacement** | 定向增发公告监控：跟踪近 7 天内最新发布的定向增发方案与发行公告 |

### 3. 量化突破形态走势图报告 | Visual Breakout Reports

每次选股完成后，系统自动在 `reports/` 目录下生成自包含、交互式的现代化暗黑量化走势报告（`reports/breakout_report_YYYYMMDD.html` 并同步更新 `reports/latest.html`）：

- **专业暗黑终端体验**：顶部 Dashboard 汇总策略命中概况，支持按策略 Chip 快捷标签极速锚点跳转；
- **策略专属指标与形态直击**：7 大突破策略绘制 K 线、成交量与形态辅助线（海龟 20 日突破阶梯线、布林通道收口/张口通道、120 日平台线等），图例颜色与线条严格统一；
- **交互式横向滚动条 (DataZoom)**：加载全量历史日 K，默认视窗向右靠齐聚焦最新 50 根突破形态，支持鼠标滚轮与滑块自由回溯历史蓄势平台；
- **大样本视口懒加载**：集成 `IntersectionObserver` 动态渲染，全市场选出数百只标的依然秒开流畅，零卡顿；
- **完全离线与多级兜底**：模块内置离线静态资源，优先本地加载，同时支持阿里云 NPM 镜像与 jsdelivr 在线降级。

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/)（极速 Python 包与虚拟环境管理器）

### 1. 安装依赖

```bash
# 使用 uv 一键创建虚拟环境并同步所有依赖
uv sync
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写飞书 Webhook URL
```

### 3. 首次回填历史数据

```bash
uv run main.py --backfill
```

约 12 分钟完成 ~5200 只 A 股历史前复权日 K 数据回填。

### 4. 日常运行

```bash
uv run main.py
```

执行流程：增量同步行情 -> 跑 8 大量化策略 -> 推送飞书卡片 -> 自动生成全套走势图 HTML 报告。

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && uv run main.py >> log.txt 2>&1
```

### 5. 策略选股预览与测试（可选）

```bash
# 控制台富文本表格快速预览策略选股结果
uv run demo_preview.py

# 运行自动化测试
uv run pytest
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填模式，自动产出报告
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── reports/                     # 可视化 HTML 报告输出目录（不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── report/
│   │   ├── __init__.py          # 报告模块包入口
│   │   ├── assets/              # 本地静态资源（echarts.min.js）
│   │   └── html_reporter.py     # 突破形态可视化 HTML 报告生成器
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   ├── rps_breakout.py      # RPS 突破策略
│   │   ├── boll_breakout.py     # 布林通道突破策略
│   │   └── private_placement.py # 定增公告监控策略
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
└── tests/                       # 单元测试与属性测试
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）
- **复权方式**：前复权（qfq）— 最新价格严格等于市场实盘真实价，历史 K 线平滑无缺口，与招商证券、同花顺、TradingView 等主流看盘软件 100% 对齐
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：8 进程并行通过 baostock 拉取，2~3 分钟完成全市场更新

---

## 许可证 | License

MIT
