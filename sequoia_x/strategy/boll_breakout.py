"""布林通道收口突破策略：前期收口蓄势 + 当日放量向上突破布林线上轨。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class BollBreakoutStrategy(BaseStrategy):
    """布林通道收口放量向上突破策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 计算布林线指标：
       - 中轨 mid = 20日收盘价均线
       - 标准差 std = 20日收盘价样本标准差
       - 上轨 upper = mid + 2 * std
       - 下轨 lower = mid - 2 * std
       - 带宽 bandwidth = (upper - lower) / mid
    2. 收口蓄势：
       - 突破日前1交易日，带宽处于低波动收缩状态（prev_bandwidth <= 0.15 或近20日极低波动）
    3. 张口突破：
       - 昨日收盘价处于上轨下方（prev_close <= prev_upper）
       - 今日收盘价强势向上穿透上轨（last_close > last_upper）
       - 今日布林带开始张口扩张（last_bandwidth > prev_bandwidth）
    4. 放量与防诱多：
       - 今日实体为真阳线：last_close > last_open 且 last_close > prev_close
       - 当日成交量放大：last_volume > 20日均量 * 1.5
       - 流动性防守：当日成交额 > 50,000,000 元（过滤无流动性个股）

    Attributes:
        webhook_key: 路由到 'boll_breakout' 专属飞书机器人。
    """

    webhook_key: str = "boll_breakout"
    _MIN_BARS: int = 25  # 至少需要 25 根 K 线确保 20 日指标稳定计算

    def run(self) -> list[str]:
        """遍历本地数据库股票，返回满足布林带收口突破条件的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化计算布林带与成交量均线
                df["mid"] = df["close"].rolling(20).mean()
                df["std"] = df["close"].rolling(20).std()
                df["upper"] = df["mid"] + 2 * df["std"]
                df["lower"] = df["mid"] - 2 * df["std"]
                df["bandwidth"] = (df["upper"] - df["lower"]) / df["mid"]
                df["vol_ma20"] = df["volume"].rolling(20).mean()

                # 提取最近两根 K 线进行判定
                last = df.iloc[-1]
                prev = df.iloc[-2]

                if pd.isna(last["upper"]) or pd.isna(prev["upper"]):
                    continue

                # 条件 1：收口蓄势判断（昨日带宽 <= 0.18 或昨日处于近20日低波动期）
                bandwidth_20_min = df["bandwidth"].shift(1).rolling(20).min().iloc[-1]
                is_squeeze = (prev["bandwidth"] <= 0.18) or (prev["bandwidth"] <= bandwidth_20_min * 1.15)

                # 条件 2：向上突破上轨
                breakout = (prev["close"] <= prev["upper"]) and (last["close"] > last["upper"])

                # 条件 3：张口发散
                is_expanding = last["bandwidth"] > prev["bandwidth"]

                # 条件 4：动量阳线防诱多
                is_yang = (last["close"] > last["open"]) and (last["close"] > prev["close"])

                # 条件 5：放量确认（大于20日均量的1.5倍）
                is_volume_surge = last["volume"] > last["vol_ma20"] * 1.5

                # 条件 6：成交额流动性过滤（> 5000万）
                is_liquid = last["turnover"] >= 50_000_000

                if is_squeeze and breakout and is_expanding and is_yang and is_volume_surge and is_liquid:
                    selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] BollBreakoutStrategy 计算异常：{exc}")
                continue

        logger.info(f"BollBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected
