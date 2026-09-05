"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）
"""

import argparse
import sys
from pathlib import Path
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")
load_dotenv()

from datetime import date

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.boll_breakout import BollBreakoutStrategy
from sequoia_x.report.html_reporter import HtmlReporter


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 检查本地历史数据：若完全无数据，自动从头执行全量回填 ──
        local_symbols = engine.get_local_symbols()
        if not local_symbols:
            logger.warning("本地数据库尚无股票历史数据，策略计算需要历史K线支持。")
            logger.info("自动启动全市场历史数据回填初始化（首次约需十几分钟）...")
            all_symbols = engine.get_all_symbols()
            if not all_symbols:
                logger.error("获取股票代码列表失败，请检查网络后重试")
                return
            engine.backfill(all_symbols)
            logger.info("历史数据初始化回填完成，继续执行日常增量与策略选股...")

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
            BollBreakoutStrategy(engine=engine, settings=settings),
        ]

        notifier = FeishuNotifier(settings)
        strategy_results: dict[str, list[str]] = {}

        # 5. 遍历策略，有结果则推送至对应机器人
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            strategy_results[strategy_name] = selected
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            if selected:
                notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                    webhook_key=strategy.webhook_key,
                )
            else:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")

        # 6. Generate breakout visualization HTML report
        logger.info("开始生成突破走势图 HTML 报告...")
        reporter = HtmlReporter(engine=engine, settings=settings)
        report_path = reporter.generate(strategy_results)
        logger.info(f"选股走势报告已生成：{report_path}")

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
