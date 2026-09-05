"""HTML 报告生成器模块：负责量化选股结果的可视化呈现与走势图渲染。

提供自包含的现代暗黑风格 HTML 报告，针对 7 大技术突破策略绘制 K 线主图、
成交量副图、专属指标线（如布林通道、20日新高线、均线金叉等），
并配置向右靠齐的横向交互滑动条。针对定增策略提供结构化公告表格。
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier

logger = get_logger(__name__)

# Strategy display metadata
STRATEGY_META: dict[str, dict[str, str]] = {
    "MaVolumeStrategy": {
        "title": "均线放量突破",
        "badge": "MA+VOL",
        "desc": "MA5 上穿 MA20（金叉）且当日成交量 > 20日均量 1.5 倍",
        "type": "technical",
    },
    "TurtleTradeStrategy": {
        "title": "海龟新高突破",
        "badge": "Turtle",
        "desc": "放量突破前 20 个交易日最高价，实体真阳线防诱多",
        "type": "technical",
    },
    "BollBreakoutStrategy": {
        "title": "布林带收口突破",
        "badge": "BOLL",
        "desc": "前期布林通道收口蓄势，当日大阳线放量张口穿透上轨",
        "type": "technical",
    },
    "HighTightFlagStrategy": {
        "title": "高窄旗形整理",
        "badge": "HTF",
        "desc": "短期暴涨（旗杆超60%）后极度缩量收敛，蓄势待突破",
        "type": "technical",
    },
    "LimitUpShakeoutStrategy": {
        "title": "涨停洗盘回踩",
        "badge": "Shakeout",
        "desc": "昨日涨停板，今日倍量收阴洗盘但不破昨收支撑",
        "type": "technical",
    },
    "UptrendLimitDownStrategy": {
        "title": "上升趋势跌停反包",
        "badge": "TrendReversal",
        "desc": "MA20>MA60 主升浪中出现放量跌停错杀，存在超跌反弹机会",
        "type": "technical",
    },
    "RpsBreakoutStrategy": {
        "title": "欧奈尔 RPS 突破",
        "badge": "RPS90",
        "desc": "全市场相对强度 RPS >= 90，贴近或突破 120 日平台高点",
        "type": "technical",
    },
    "PrivatePlacementStrategy": {
        "title": "破发定增掘金",
        "badge": "Event",
        "desc": "定向增发公告监控与事件驱动机会",
        "type": "event",
    },
}


class HtmlReporter:
    """量化突破走势图 HTML 报告生成器。"""

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        Initialize the reporter.

        Args:
            engine: DataEngine instance to fetch OHLCV historical data.
            settings: Settings instance for configuration.
        """
        self.engine = engine
        self.settings = settings

    @staticmethod
    def _clean_series(s: pd.Series) -> list[float | None]:
        """Convert a pandas Series into a list replacing NaN/inf with None."""
        return [
            round(float(v), 2) if pd.notna(v) and not np.isinf(v) else None
            for v in s
        ]

    def _extract_stock_chart_data(
        self,
        symbol: str,
        name: str,
        strategy_name: str,
    ) -> dict[str, Any] | None:
        """
        Fetch OHLCV data for a symbol and calculate strategy-specific indicator series.

        Returns None if data is insufficient (< 5 bars).
        """
        df = self.engine.get_ohlcv(symbol)
        if df.empty or len(df) < 5:
            return None

        df = df.sort_values("date").reset_index(drop=True)

        # Standard moving averages
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        df["vol_ma20"] = df["volume"].rolling(20).mean()

        # Strategy-specific technical indicators
        if strategy_name == "BollBreakoutStrategy":
            df["boll_mid"] = df["close"].rolling(20).mean()
            df["boll_std"] = df["close"].rolling(20).std()
            df["boll_upper"] = df["boll_mid"] + 2 * df["boll_std"]
            df["boll_lower"] = df["boll_mid"] - 2 * df["boll_std"]
        elif strategy_name == "TurtleTradeStrategy":
            df["turtle_high20"] = df["high"].shift(1).rolling(20).max()
        elif strategy_name == "RpsBreakoutStrategy":
            df["rps_high120"] = df["high"].rolling(120, min_periods=30).max()
        elif strategy_name == "LimitUpShakeoutStrategy":
            # Support line at previous close
            df["prev_close"] = df["close"].shift(1)
        elif strategy_name == "HighTightFlagStrategy":
            df["high40"] = df["high"].rolling(40, min_periods=20).max()
            df["low40"] = df["low"].rolling(40, min_periods=20).min()

        # Build candle values: [open, close, low, high] (ECharts standard)
        dates = df["date"].astype(str).tolist()
        k_values = [
            [
                round(float(row["open"]), 2),
                round(float(row["close"]), 2),
                round(float(row["low"]), 2),
                round(float(row["high"]), 2),
            ]
            for _, row in df.iterrows()
        ]

        # Build volume values: [index, volume, sign (1 for up, -1 for down)]
        volumes = [
            [
                i,
                round(float(row["volume"]), 0),
                1 if row["close"] >= row["open"] else -1,
            ]
            for i, (_, row) in enumerate(df.iterrows())
        ]

        # Latest bar statistics
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest
        latest_close = float(latest["close"])
        prev_close = float(prev["close"]) if (len(df) >= 2 and pd.notna(prev["close"])) else latest_close
        pct_change = ((latest_close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
        latest_vol = float(latest["volume"])
        latest_amount = float(latest.get("turnover", 0.0))

        # Pack indicator series for ECharts
        indicators: dict[str, Any] = {
            "ma5": self._clean_series(df["ma5"]),
            "ma10": self._clean_series(df["ma10"]),
            "ma20": self._clean_series(df["ma20"]),
            "ma60": self._clean_series(df["ma60"]),
            "vol_ma20": self._clean_series(df["vol_ma20"]),
        }

        if strategy_name == "BollBreakoutStrategy":
            indicators["boll_upper"] = self._clean_series(df["boll_upper"])
            indicators["boll_mid"] = self._clean_series(df["boll_mid"])
            indicators["boll_lower"] = self._clean_series(df["boll_lower"])
        elif strategy_name == "TurtleTradeStrategy":
            indicators["turtle_high20"] = self._clean_series(df["turtle_high20"])
        elif strategy_name == "RpsBreakoutStrategy":
            indicators["rps_high120"] = self._clean_series(df["rps_high120"])
        elif strategy_name == "LimitUpShakeoutStrategy":
            indicators["prev_close"] = self._clean_series(df["prev_close"])
        elif strategy_name == "HighTightFlagStrategy":
            indicators["high40"] = self._clean_series(df["high40"])
            indicators["low40"] = self._clean_series(df["low40"])

        # Default zoom window: align to the right, showing last 45 bars
        total_bars = len(dates)
        start_index = max(0, total_bars - 50)
        start_percent = round((start_index / total_bars) * 100, 1) if total_bars > 0 else 0

        xq_code = FeishuNotifier._to_xueqiu_code(symbol)
        elem_id = f"chart_{strategy_name}_{symbol}"

        return {
            "elem_id": elem_id,
            "symbol": symbol,
            "name": name,
            "xq_code": xq_code,
            "strategy_name": strategy_name,
            "dates": dates,
            "k_values": k_values,
            "volumes": volumes,
            "indicators": indicators,
            "start_percent": start_percent,
            "latest_close": round(latest_close, 2),
            "pct_change": round(pct_change, 2),
            "latest_amount_formatted": self._format_amount(latest_amount),
            "latest_date": dates[-1] if dates else "",
        }

    @staticmethod
    def _format_amount(amount: float) -> str:
        """Format turnover amount into human-readable Chinese string."""
        if amount >= 100_000_000:
            return f"{amount / 100_000_000:.2f} 亿"
        elif amount >= 10_000:
            return f"{amount / 10_000:.2f} 万"
        return f"{amount:.0f}"

    def generate(
        self,
        strategy_results: dict[str, list[str]],
        output_dir: str = "reports",
    ) -> Path:
        """
        Generate self-contained HTML report with interactive ECharts for breakout stocks.

        Args:
            strategy_results: Mapping of {strategy_name: [symbols]}.
            output_dir: Directory where reports will be written.

        Returns:
            Path to the generated primary HTML file.
        """
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        # Copy local echarts.min.js to output_dir/assets for offline/sandboxed viewing
        src_asset = Path(__file__).resolve().parent / "assets" / "echarts.min.js"
        dst_asset = out_path / "assets" / "echarts.min.js"
        if src_asset.exists() and not dst_asset.exists():
            try:
                dst_asset.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(src_asset, dst_asset)
            except Exception as e:
                logger.warning(f"复制 ECharts 本地资源失败: {e}")

        # Collect unique symbols to query stock names in bulk
        all_symbols: set[str] = set()
        for symbols in strategy_results.values():
            all_symbols.update(symbols)

        name_map: dict[str, str] = {}
        if all_symbols:
            try:
                name_map = FeishuNotifier._get_stock_names(list(all_symbols))
            except Exception as exc:
                logger.warning(f"获取股票中文名称失败，使用代码代替：{exc}")

        # Segregate technical breakout charts and event announcements
        technical_sections: list[dict[str, Any]] = []
        event_sections: list[dict[str, Any]] = []
        total_hits = 0

        for strat_name, symbols in strategy_results.items():
            meta = STRATEGY_META.get(
                strat_name,
                {
                    "title": strat_name,
                    "badge": strat_name[:6],
                    "desc": "量化选股策略",
                    "type": "technical",
                },
            )

            if not symbols:
                continue

            total_hits += len(symbols)

            if meta["type"] == "event":
                # Render as structured table card for announcements
                records = []
                for s in symbols:
                    name = name_map.get(s, s)
                    xq_code = FeishuNotifier._to_xueqiu_code(s)
                    records.append({
                        "symbol": s,
                        "name": name,
                        "xq_code": xq_code,
                        "xq_url": f"https://xueqiu.com/S/{xq_code}",
                    })
                event_sections.append({
                    "strategy_name": strat_name,
                    "meta": meta,
                    "records": records,
                    "count": len(records),
                })
            else:
                # Render as interactive K-line charts
                charts = []
                for s in symbols:
                    name = name_map.get(s, s)
                    chart_data = self._extract_stock_chart_data(s, name, strat_name)
                    if chart_data:
                        charts.append(chart_data)
                
                if charts:
                    technical_sections.append({
                        "strategy_name": strat_name,
                        "meta": meta,
                        "charts": charts,
                        "count": len(charts),
                    })

        # Render HTML
        html_content = self._render_template(
            technical_sections=technical_sections,
            event_sections=event_sections,
            total_hits=total_hits,
        )

        today_str = date.today().strftime("%Y%m%d")
        report_file = out_path / f"breakout_report_{today_str}.html"
        latest_file = out_path / "latest.html"

        report_file.write_text(html_content, encoding="utf-8")
        latest_file.write_text(html_content, encoding="utf-8")

        logger.info(f"HTML 选股报告生成完成：{report_file.resolve()}")
        return report_file.resolve()

    def _render_template(
        self,
        technical_sections: list[dict[str, Any]],
        event_sections: list[dict[str, Any]],
        total_hits: int,
    ) -> str:
        """Assemble complete modern dark-themed HTML document with embedded ECharts."""
        generated_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db_path = self.settings.db_path

        # JSON data for charts injection (with compact separators)
        charts_payload: list[dict[str, Any]] = []
        for sec in technical_sections:
            for c in sec["charts"]:
                charts_payload.append(c)

        charts_json_str = json.dumps(charts_payload, ensure_ascii=False, separators=(",", ":"))

        # Build navigation items
        nav_items_html = []
        for sec in technical_sections:
            s_name = sec["strategy_name"]
            meta = sec["meta"]
            count = sec["count"]
            nav_items_html.append(
                f'<a href="#{s_name}" class="nav-chip">'
                f'<span class="chip-badge">{meta["badge"]}</span> '
                f'{meta["title"]} ({count})'
                f'</a>'
            )
        for sec in event_sections:
            s_name = sec["strategy_name"]
            meta = sec["meta"]
            count = sec["count"]
            nav_items_html.append(
                f'<a href="#{s_name}" class="nav-chip chip-event">'
                f'<span class="chip-badge">{meta["badge"]}</span> '
                f'{meta["title"]} ({count})'
                f'</a>'
            )

        nav_html = "\n".join(nav_items_html) if nav_items_html else '<span class="empty-text">本次运行无命中标的</span>'

        # Build technical sections HTML
        sections_html = []

        for sec in technical_sections:
            s_name = sec["strategy_name"]
            meta = sec["meta"]
            charts = sec["charts"]

            cards_html = []
            for chart in charts:
                chart_elem_id = chart["elem_id"]
                pct_cls = "up-color" if chart["pct_change"] >= 0 else "down-color"
                pct_sign = "+" if chart["pct_change"] >= 0 else ""

                card = f"""
                <div class="stock-card">
                    <div class="card-header">
                        <div class="header-left">
                            <span class="stock-name">{chart['name']}</span>
                            <span class="stock-code">{chart['symbol']}</span>
                            <a href="https://xueqiu.com/S/{chart['xq_code']}" target="_blank" class="xq-link" title="在雪球中查看行情">雪球 ↗</a>
                        </div>
                        <div class="header-right">
                            <span class="price-val {pct_cls}">¥{chart['latest_close']}</span>
                            <span class="pct-val {pct_cls}">{pct_sign}{chart['pct_change']}%</span>
                            <span class="amount-val">成交额: {chart['latest_amount_formatted']}</span>
                        </div>
                    </div>
                    <div id="{chart_elem_id}" class="chart-container"></div>
                </div>
                """
                cards_html.append(card)

            sec_html = f"""
            <section id="{s_name}" class="strategy-section">
                <div class="section-title-bar">
                    <div class="title-left">
                        <span class="strategy-badge">{meta['badge']}</span>
                        <h2>{meta['title']}</h2>
                        <span class="hit-count">{len(charts)} 只标的</span>
                    </div>
                    <div class="strategy-desc">{meta['desc']}</div>
                </div>
                <div class="charts-grid">
                    {"".join(cards_html)}
                </div>
            </section>
            """
            sections_html.append(sec_html)

        # Build event sections HTML
        for sec in event_sections:
            s_name = sec["strategy_name"]
            meta = sec["meta"]
            records = sec["records"]

            rows_html = []
            for r in records:
                rows_html.append(f"""
                <tr>
                    <td><strong>{r['name']}</strong></td>
                    <td><code>{r['symbol']}</code></td>
                    <td><span class="badge-tag">定向增发</span></td>
                    <td><a href="{r['xq_url']}" target="_blank" class="xq-table-link">雪球行情 ↗</a></td>
                </tr>
                """)

            sec_html = f"""
            <section id="{s_name}" class="strategy-section">
                <div class="section-title-bar">
                    <div class="title-left">
                        <span class="strategy-badge event-badge">{meta['badge']}</span>
                        <h2>{meta['title']}</h2>
                        <span class="hit-count">{len(records)} 只标的</span>
                    </div>
                    <div class="strategy-desc">{meta['desc']}</div>
                </div>
                <div class="event-card">
                    <table class="event-table">
                        <thead>
                            <tr>
                                <th>股票名称</th>
                                <th>股票代码</th>
                                <th>事件类型</th>
                                <th>行情链接</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(rows_html)}
                        </tbody>
                    </table>
                </div>
            </section>
            """
            sections_html.append(sec_html)

        content_body = "\n".join(sections_html) if sections_html else """
        <div class="empty-state">
            <div class="empty-icon">📊</div>
            <h3>本次扫描未命中突破标的</h3>
            <p>全市场策略执行完毕，今日无符合突破或形态筛选条件的股票。</p>
        </div>
        """

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sequoia-X 量化突破走势图报告 | {date.today().strftime('%Y-%m-%d')}</title>
    <!-- Apache ECharts (Local bundled asset with multi-CDN fallback) -->
    <script src="assets/echarts.min.js"></script>
    <script>
        if (!window.echarts) {{
            document.write('<script src="https://registry.npmmirror.com/echarts/5.5.0/files/dist/echarts.min.js"><\\/script>');
        }}
    </script>
    <script>
        if (!window.echarts) {{
            document.write('<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"><\\/script>');
        }}
    </script>
    <style>
        :root {{
            --bg-body: #101216;
            --bg-card: #181b22;
            --bg-card-header: #1e222b;
            --border-color: #2b303c;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-dim: #6e7681;
            --color-up: #ef5350;
            --color-down: #26a69a;
            --color-accent: #388bfd;
            --color-badge: #1f6feb;
            --color-gold: #e3b341;
            --color-purple: #bc8cff;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
            background-color: var(--bg-body);
            color: var(--text-primary);
            line-height: 1.5;
            padding: 24px;
        }}

        .container {{
            max-width: 1560px;
            margin: 0 auto;
        }}

        /* Header Dashboard */
        .dashboard-header {{
            background: linear-gradient(135deg, #1c212c 0%, #161922 100%);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px 32px;
            margin-bottom: 24px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        }}

        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            margin-bottom: 16px;
        }}

        .brand-title {{
            font-size: 24px;
            font-weight: 700;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .brand-tag {{
            background: rgba(56, 139, 253, 0.15);
            color: var(--color-accent);
            border: 1px solid var(--color-accent);
            font-size: 12px;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 600;
        }}

        .meta-info {{
            font-size: 13px;
            color: var(--text-secondary);
        }}

        .nav-chips {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }}

        .nav-label {{
            font-size: 13px;
            color: var(--text-dim);
            margin-right: 4px;
        }}

        .nav-chip {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #212631;
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 13px;
            text-decoration: none;
            transition: all 0.2s ease;
        }}

        .nav-chip:hover {{
            background: #2c3342;
            border-color: var(--color-accent);
            color: #fff;
            transform: translateY(-1px);
        }}

        .chip-badge {{
            background: rgba(56, 139, 253, 0.25);
            color: #79c0ff;
            font-size: 11px;
            padding: 1px 6px;
            border-radius: 10px;
            font-weight: bold;
        }}

        .chip-event .chip-badge {{
            background: rgba(227, 179, 65, 0.25);
            color: #f0883e;
        }}

        /* Strategy Section */
        .strategy-section {{
            margin-bottom: 40px;
        }}

        .section-title-bar {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 10px 10px 0 0;
            padding: 14px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid var(--color-accent);
        }}

        .title-left {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .title-left h2 {{
            font-size: 18px;
            font-weight: 600;
        }}

        .strategy-badge {{
            background: var(--color-badge);
            color: white;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }}

        .event-badge {{
            background: #9e6a03;
        }}

        .hit-count {{
            font-size: 13px;
            color: var(--color-accent);
            font-weight: 600;
        }}

        .strategy-desc {{
            font-size: 13px;
            color: var(--text-secondary);
        }}

        /* Stock Cards Grid */
        .charts-grid {{
            display: flex;
            flex-direction: column;
            gap: 20px;
            background: #14171f;
            border: 1px solid var(--border-color);
            border-top: none;
            border-radius: 0 0 10px 10px;
            padding: 20px;
        }}

        .stock-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 4px 12px rgba(0,0,0,0.25);
        }}

        .card-header {{
            background: var(--bg-card-header);
            border-bottom: 1px solid var(--border-color);
            padding: 10px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .header-left {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .stock-name {{
            font-size: 17px;
            font-weight: 700;
            color: #ffffff;
        }}

        .stock-code {{
            font-family: monospace;
            font-size: 14px;
            color: var(--text-secondary);
            background: #252a35;
            padding: 2px 6px;
            border-radius: 4px;
        }}

        .xq-link {{
            font-size: 12px;
            color: var(--color-accent);
            text-decoration: none;
            border: 1px solid rgba(56, 139, 253, 0.4);
            padding: 2px 8px;
            border-radius: 4px;
            transition: all 0.2s;
        }}

        .xq-link:hover {{
            background: rgba(56, 139, 253, 0.2);
            color: #79c0ff;
        }}

        .header-right {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}

        .price-val {{
            font-size: 18px;
            font-weight: 700;
            font-family: monospace;
        }}

        .pct-val {{
            font-size: 16px;
            font-weight: 700;
            font-family: monospace;
        }}

        .amount-val {{
            font-size: 13px;
            color: var(--text-secondary);
        }}

        .up-color {{
            color: var(--color-up) !important;
        }}

        .down-color {{
            color: var(--color-down) !important;
        }}

        .chart-container {{
            width: 100%;
            height: 480px;
        }}

        /* Event Table */
        .event-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-top: none;
            border-radius: 0 0 10px 10px;
            padding: 20px;
        }}

        .event-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}

        .event-table th {{
            text-align: left;
            padding: 10px 14px;
            background: #1c202a;
            color: var(--text-secondary);
            border-bottom: 1px solid var(--border-color);
        }}

        .event-table td {{
            padding: 12px 14px;
            border-bottom: 1px solid #232733;
        }}

        .badge-tag {{
            background: rgba(227, 179, 65, 0.15);
            color: var(--color-gold);
            border: 1px solid rgba(227, 179, 65, 0.4);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
        }}

        .xq-table-link {{
            color: var(--color-accent);
            text-decoration: none;
            font-size: 13px;
        }}

        /* Empty State */
        .empty-state {{
            background: var(--bg-card);
            border: 1px dashed var(--border-color);
            border-radius: 12px;
            padding: 60px 20px;
            text-align: center;
        }}

        .empty-icon {{
            font-size: 48px;
            margin-bottom: 12px;
        }}

        .empty-state h3 {{
            font-size: 20px;
            margin-bottom: 8px;
            color: var(--text-primary);
        }}

        .empty-state p {{
            color: var(--text-secondary);
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Dashboard Top Header -->
        <header class="dashboard-header">
            <div class="header-top">
                <div class="brand-title">
                    <span>📈 Sequoia-X 量化突破走势图报告</span>
                    <span class="brand-tag">V2 Pro</span>
                </div>
                <div class="meta-info">
                    <span>生成时间: {generated_time}</span> | 
                    <span>命中总标的: <strong style="color:var(--color-accent);">{total_hits}</strong> 只</span> |
                    <span>数据库: <code>{db_path}</code></span>
                </div>
            </div>
            <div class="nav-chips">
                <span class="nav-label">策略快速定位:</span>
                {nav_html}
            </div>
        </header>

        <!-- Main Content Sections -->
        <main>
            {content_body}
        </main>
    </div>

    <!-- Chart Injection Script -->
    <script>
        const chartDataList = {charts_json_str};

        // Cache chart data by elem_id
        const chartDataMap = new Map();
        chartDataList.forEach(data => {{
            chartDataMap.set(data.elem_id, data);
        }});

        // Shared active charts list and debounced resize handler
        const activeCharts = [];
        let resizeTimer = null;
        window.addEventListener("resize", () => {{
            if (resizeTimer) clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {{
                activeCharts.forEach(c => {{
                    try {{ c.resize(); }} catch (e) {{}}
                }});
            }}, 100);
        }});

        document.addEventListener("DOMContentLoaded", () => {{
            if (!window.echarts) {{
                console.error("ECharts script failed to load.");
                return;
            }}

            // IntersectionObserver for lazy chart initialization
            if ("IntersectionObserver" in window) {{
                const observer = new IntersectionObserver((entries, obs) => {{
                    entries.forEach(entry => {{
                        if (entry.isIntersecting) {{
                            const dom = entry.target;
                            const data = chartDataMap.get(dom.id);
                            if (data) {{
                                initStockChart(data);
                                obs.unobserve(dom);
                            }}
                        }}
                    }});
                }}, {{ rootMargin: "300px 0px" }});

                document.querySelectorAll(".chart-container").forEach(el => {{
                    observer.observe(el);
                }});
            }} else {{
                chartDataList.forEach(data => initStockChart(data));
            }}
        }});

        function initStockChart(data) {{
            const dom = document.getElementById(data.elem_id);
            if (!dom || dom.getAttribute("data-rendered") === "true") return;
            dom.setAttribute("data-rendered", "true");

            const myChart = echarts.init(dom, "dark");
            activeCharts.push(myChart);
            const dates = data.dates;
            const kValues = data.k_values;
            const volumes = data.volumes;
            const indicators = data.indicators;
            const stratName = data.strategy_name;

            // Prepare series list
            const seriesList = [];
            const legendData = ["K线", "成交量", "MA5", "MA10", "MA20", "MA60"];

            // 1. Candlestick series (Main Grid: 0)
            seriesList.push({{
                name: "K线",
                type: "candlestick",
                data: kValues,
                xAxisIndex: 0,
                yAxisIndex: 0,
                itemStyle: {{
                    color: "#ef5350",
                    color0: "#26a69a",
                    borderColor: "#ef5350",
                    borderColor0: "#26a69a"
                }},
                markPoint: {{
                    data: [
                        {{
                            name: "突破日",
                            coord: [dates[dates.length - 1], kValues[kValues.length - 1][1]],
                            value: "突破日",
                            itemStyle: {{ color: "#ff9800" }},
                            label: {{ color: "#ffffff", fontWeight: "bold", fontSize: 11 }}
                        }},
                        {{
                            type: "max",
                            valueDim: "highest",
                            name: "近期高点",
                            itemStyle: {{ color: "#f44336" }}
                        }},
                        {{
                            type: "min",
                            valueDim: "lowest",
                            name: "近期低点",
                            itemStyle: {{ color: "#4caf50" }}
                        }}
                    ]
                }}
            }});

            // 2. Standard Moving Averages
            const maConfigs = [
                {{ name: "MA5", key: "ma5", color: "#f5d329" }},
                {{ name: "MA10", key: "ma10", color: "#ab47bc" }},
                {{ name: "MA20", key: "ma20", color: "#29b6f6" }},
                {{ name: "MA60", key: "ma60", color: "#78909c" }}
            ];

            maConfigs.forEach(cfg => {{
                if (indicators[cfg.key]) {{
                    seriesList.push({{
                        name: cfg.name,
                        type: "line",
                        data: indicators[cfg.key],
                        smooth: true,
                        showSymbol: false,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        lineStyle: {{ width: 1.5, color: cfg.color }}
                    }});
                }}
            }});

            // 3. Strategy-specific Overlays
            if (stratName === "BollBreakoutStrategy") {{
                legendData.push("布林上轨", "布林中轨", "布林下轨");
                seriesList.push(
                    {{
                        name: "布林上轨",
                        type: "line",
                        data: indicators["boll_upper"],
                        smooth: true,
                        showSymbol: false,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        lineStyle: {{ color: "#ff5252", width: 2, type: "dashed" }}
                    }},
                    {{
                        name: "布林中轨",
                        type: "line",
                        data: indicators["boll_mid"],
                        smooth: true,
                        showSymbol: false,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        lineStyle: {{ color: "#e0e0e0", width: 1.5, type: "dotted" }}
                    }},
                    {{
                        name: "布林下轨",
                        type: "line",
                        data: indicators["boll_lower"],
                        smooth: true,
                        showSymbol: false,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        lineStyle: {{ color: "#69f0ae", width: 1.5, type: "dashed" }}
                    }}
                );
            }} else if (stratName === "TurtleTradeStrategy") {{
                legendData.push("20日新高阻力线");
                seriesList.push({{
                    name: "20日新高阻力线",
                    type: "line",
                    data: indicators["turtle_high20"],
                    step: "end",
                    showSymbol: false,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    lineStyle: {{ color: "#ff9100", width: 2.2 }}
                }});
            }} else if (stratName === "RpsBreakoutStrategy") {{
                legendData.push("120日平台高点");
                seriesList.push({{
                    name: "120日平台高点",
                    type: "line",
                    data: indicators["rps_high120"],
                    showSymbol: false,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    lineStyle: {{ color: "#ff4081", width: 2, type: "dashed" }}
                }});
            }} else if (stratName === "LimitUpShakeoutStrategy") {{
                legendData.push("昨收支撑线");
                seriesList.push({{
                    name: "昨收支撑线",
                    type: "line",
                    data: indicators["prev_close"],
                    showSymbol: false,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    lineStyle: {{ color: "#00e676", width: 1.8, type: "dotted" }}
                }});
            }} else if (stratName === "HighTightFlagStrategy") {{
                legendData.push("40日旗杆顶", "40日旗杆底");
                seriesList.push(
                    {{
                        name: "40日旗杆顶",
                        type: "line",
                        data: indicators["high40"],
                        showSymbol: false,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        lineStyle: {{ color: "#ffd700", width: 2, type: "dashed" }}
                    }},
                    {{
                        name: "40日旗杆底",
                        type: "line",
                        data: indicators["low40"],
                        showSymbol: false,
                        xAxisIndex: 0,
                        yAxisIndex: 0,
                        lineStyle: {{ color: "#40c4ff", width: 1.5, type: "dashed" }}
                    }}
                );
            }}

            // 4. Volume Bar series (Sub Grid: 1)
            seriesList.push({{
                name: "成交量",
                type: "bar",
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: volumes.map(v => ({{
                    value: v[1],
                    itemStyle: {{
                        color: v[2] > 0 ? "rgba(239, 83, 80, 0.85)" : "rgba(38, 166, 154, 0.85)"
                    }}
                }}))
            }});

            // 5. Volume 20-day MA
            if (indicators["vol_ma20"]) {{
                legendData.push("Vol-MA20");
                seriesList.push({{
                    name: "Vol-MA20",
                    type: "line",
                    xAxisIndex: 1,
                    yAxisIndex: 1,
                    data: indicators["vol_ma20"],
                    smooth: true,
                    showSymbol: false,
                    lineStyle: {{ width: 1.2, color: "#ffb74d" }}
                }});
            }}

            const option = {{
                backgroundColor: "#181b22",
                animation: false,
                legend: {{
                    data: legendData,
                    top: 8,
                    left: 16,
                    textStyle: {{ color: "#8b949e", fontSize: 12 }}
                }},
                tooltip: {{
                    trigger: "axis",
                    axisPointer: {{
                        type: "cross",
                        lineStyle: {{ color: "#586069", width: 1, type: "dashed" }}
                    }},
                    backgroundColor: "rgba(22, 27, 34, 0.95)",
                    borderColor: "#30363d",
                    textStyle: {{ color: "#c9d1d9", fontSize: 12 }},
                    formatter: function (params) {{
                        if (!params || !params.length) return "";
                        let res = `<div style="font-weight:bold;margin-bottom:4px;">${{params[0].axisValue}}</div>`;
                        params.forEach(p => {{
                            if (p.seriesType === "candlestick") {{
                                const open = p.data[1];
                                const close = p.data[2];
                                const low = p.data[3];
                                const high = p.data[4];
                                const color = close >= open ? "#ef5350" : "#26a69a";
                                res += `<div style="color:${{color}}">
                                    开: ¥${{open}} | 收: ¥${{close}}<br/>
                                    低: ¥${{low}} | 高: ¥${{high}}
                                </div>`;
                            }} else if (p.seriesName === "成交量") {{
                                res += `<div>成交量: ${{p.value.toLocaleString()}} 股</div>`;
                            }} else if (p.value !== undefined && p.value !== null) {{
                                res += `<div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${{p.color}};margin-right:5px;"></span>${{p.seriesName}}: ¥${{p.value}}</div>`;
                            }}
                        }});
                        return res;
                    }}
                }},
                axisPointer: {{
                    link: [{{ xAxisIndex: "all" }}]
                }},
                grid: [
                    {{
                        left: "4%",
                        right: "3%",
                        top: "11%",
                        height: "56%"
                    }},
                    {{
                        left: "4%",
                        right: "3%",
                        top: "73%",
                        height: "14%"
                    }}
                ],
                xAxis: [
                    {{
                        type: "category",
                        data: dates,
                        scale: true,
                        boundaryGap: false,
                        axisLine: {{ lineStyle: {{ color: "#30363d" }} }},
                        axisLabel: {{ color: "#8b949e", fontSize: 11 }},
                        splitLine: {{ show: true, lineStyle: {{ color: "#21262d" }} }}
                    }},
                    {{
                        type: "category",
                        gridIndex: 1,
                        data: dates,
                        scale: true,
                        boundaryGap: false,
                        axisLine: {{ lineStyle: {{ color: "#30363d" }} }},
                        axisLabel: {{ show: false }},
                        splitLine: {{ show: true, lineStyle: {{ color: "#21262d" }} }}
                    }}
                ],
                yAxis: [
                    {{
                        scale: true,
                        position: "right",
                        axisLine: {{ lineStyle: {{ color: "#30363d" }} }},
                        axisLabel: {{ color: "#8b949e", fontSize: 11, formatter: "¥{{value}}" }},
                        splitLine: {{ lineStyle: {{ color: "#21262d" }} }}
                    }},
                    {{
                        scale: true,
                        gridIndex: 1,
                        position: "right",
                        axisLine: {{ lineStyle: {{ color: "#30363d" }} }},
                        axisLabel: {{ color: "#8b949e", fontSize: 10, formatter: v => (v >= 1000000 ? (v/1000000).toFixed(1)+'M' : (v/1000).toFixed(0)+'K') }},
                        splitLine: {{ show: false }}
                    }}
                ],
                dataZoom: [
                    {{
                        type: "inside",
                        xAxisIndex: [0, 1],
                        start: data.start_percent,
                        end: 100
                    }},
                    {{
                        show: true,
                        type: "slider",
                        xAxisIndex: [0, 1],
                        top: "91%",
                        height: 20,
                        start: data.start_percent,
                        end: 100,
                        borderColor: "#30363d",
                        backgroundColor: "#161b22",
                        fillerColor: "rgba(56, 139, 253, 0.2)",
                        handleStyle: {{ color: "#58a6ff" }},
                        textStyle: {{ color: "#8b949e" }}
                    }}
                ],
                series: seriesList
            }};

            myChart.setOption(option);
        }}
    </script>
</body>
</html>
"""
