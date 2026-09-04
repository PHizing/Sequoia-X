"""Sequoia-X 策略选股效果预览与展示脚本。
读取本地 SQLite 数据库，执行全部 7 个量化选股策略，并在控制台以富文本表格呈现。
"""

import os
import sys

# 解决 Windows 控制台默认 GBK 编码导致的字符输出错误
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from sequoia_x.core.config import get_settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.boll_breakout import BollBreakoutStrategy

console = Console()

def run_preview():
    settings = get_settings()
    engine = DataEngine(settings)
    local_symbols = engine.get_local_symbols()
    
    console.print(Panel.fit(
        f"[bold cyan]Sequoia-X A股量化选股系统[/bold cyan]\n"
        f"[green]数据源:[/green] baostock (免费/无限流)\n"
        f"[green]本地数据库:[/green] {settings.db_path}\n"
        f"[green]本地已覆盖样本标的:[/green] {len(local_symbols)} 只",
        title="[bold yellow]系统部署与运行状态[/bold yellow]"
    ))

    strategies = [
        ("均线放量突破 (MaVolume)", MaVolumeStrategy(engine=engine, settings=settings)),
        ("海龟新高突破 (TurtleTrade)", TurtleTradeStrategy(engine=engine, settings=settings)),
        ("高窄旗形整理 (HighTightFlag)", HighTightFlagStrategy(engine=engine, settings=settings)),
        ("涨停洗盘回踩 (LimitUpShakeout)", LimitUpShakeoutStrategy(engine=engine, settings=settings)),
        ("上升趋势跌停反包 (UptrendLimitDown)", UptrendLimitDownStrategy(engine=engine, settings=settings)),
        ("欧奈尔 RPS 突破 (RpsBreakout)", RpsBreakoutStrategy(engine=engine, settings=settings)),
        ("破发定增掘金 (PrivatePlacement)", PrivatePlacementStrategy(engine=engine, settings=settings)),
        ("布林带收口突破 (BollBreakout)", BollBreakoutStrategy(engine=engine, settings=settings)),
    ]

    table = Table(title="[bold magenta]Sequoia-X 量化策略扫描与选股结果清单[/bold magenta]", show_lines=True)
    table.add_column("策略名称", style="cyan", width=34)
    table.add_column("筛选条件简介", style="dim", width=42)
    table.add_column("命中只数", justify="center", style="bold green", width=10)
    table.add_column("选中股票代码及名称", style="yellow")

    # 策略描述映射
    strategy_descs = {
        "均线放量突破 (MaVolume)": "MA5>MA10>MA20 多头排列 + 成交量放大2倍",
        "海龟新高突破 (TurtleTrade)": "突破前20日最高价 + 成交额过亿 + 动量阳线防诱多",
        "高窄旗形整理 (HighTightFlag)": "短期暴涨后缩量窄幅旗形整理突破",
        "涨停洗盘回踩 (LimitUpShakeout)": "近期出现涨停后回踩均线支撑确认",
        "上升趋势跌停反包 (UptrendLimitDown)": "上升主升浪中跌停洗盘后的次日快速反包",
        "欧奈尔 RPS 突破 (RpsBreakout)": "相对市场强度 RPS90 以上 + 形态右侧突破",
        "破发定增掘金 (PrivatePlacement)": "定向增发价格倒挂，安全边际高的优质标的",
        "布林带收口突破 (BollBreakout)": "布林带前期收口 + 放量穿透上轨张口 + 阳线防诱多",
    }

    all_selected_symbols = set()
    strategy_results = []

    for name, strat in strategies:
        try:
            selected = strat.run()
        except Exception as e:
            selected = []
        
        strategy_results.append((name, selected))
        for s in selected:
            all_selected_symbols.add(s)

    # 获取股票名称
    name_map = {}
    if all_selected_symbols:
        try:
            name_map = FeishuNotifier._get_stock_names(list(all_selected_symbols))
        except Exception:
            pass

    report_lines = []
    report_lines.append("# Sequoia-X 选股扫描结果简报\n")
    report_lines.append(f"- 本地扫描样本池: {len(local_symbols)} 只股票")
    report_lines.append(f"- 数据库位置: {settings.db_path}\n")

    for name, selected in strategy_results:
        desc = strategy_descs.get(name, "")
        if selected:
            formatted_stocks = ", ".join([f"{code}({name_map.get(code, 'A股')})" for code in selected])
            table.add_row(name, desc, str(len(selected)), formatted_stocks)
            report_lines.append(f"### {name}")
            report_lines.append(f"- 条件: {desc}")
            report_lines.append(f"- 命中 ({len(selected)} 只): {formatted_stocks}\n")
        else:
            table.add_row(name, desc, "0", "[dim]暂无符合条件的标的[/dim]")
            report_lines.append(f"### {name}: 暂无符合条件的标的\n")

    console.print(table)

    with open("selection_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

if __name__ == "__main__":
    run_preview()
