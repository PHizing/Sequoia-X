"""BollBreakoutStrategy 布林通道收口突破策略单元测试。"""

from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from sequoia_x.strategy.boll_breakout import BollBreakoutStrategy


@pytest.fixture
def mock_strategy():
    engine = MagicMock()
    settings = MagicMock()
    settings.db_path = ":memory:"
    strategy = BollBreakoutStrategy(engine=engine, settings=settings)
    return strategy, engine


def test_boll_breakout_empty_data(mock_strategy):
    """测试无数据或数据少于 25 根 K 线时的容错性。"""
    strategy, engine = mock_strategy
    engine.get_local_symbols.return_value = ["600000"]
    engine.get_ohlcv.return_value = pd.DataFrame()

    res = strategy.run()
    assert res == []


def test_boll_breakout_successful_signal(mock_strategy):
    """测试构造完美的布林带收口 + 放量真阳线突破形态。"""
    strategy, engine = mock_strategy

    # 构造 28 天窄幅震荡（价格在 9.9 到 10.1 之间波动，带宽约 0.03~0.05 极窄收口）
    import numpy as np
    np.random.seed(42)
    base_prices = 10.0 + np.sin(np.linspace(0, 3, 28)) * 0.1
    # 第 29 天正常小阳线 10.1，第 30 天大阳线暴涨到 11.5 突破上轨
    prices = list(base_prices) + [10.1, 11.5]
    
    volumes = [1000.0] * 29 + [3000.0]  # 当日放量3倍
    turnovers = [60_000_000.0] * 30      # 满足 5000 万流动性门槛

    df = pd.DataFrame({
        "date": [f"2026-01-{i+1:02d}" for i in range(30)],
        "open": prices.copy(),
        "high": [p + 0.1 for p in prices],
        "low": [p - 0.1 for p in prices],
        "close": prices,
        "volume": volumes,
        "turnover": turnovers,
    })
    # 设置突破日的开盘价低于收盘价（实体阳线）
    df.loc[29, "open"] = 10.2
    df.loc[29, "close"] = 11.5
    df.loc[29, "high"] = 11.6
    df.loc[29, "low"] = 10.1

    engine.get_local_symbols.return_value = ["600000"]
    engine.get_ohlcv.return_value = df

    res = strategy.run()
    assert res == ["600000"]


def test_boll_breakout_rejects_without_volume(mock_strategy):
    """测试若突破日未放量，则不应入选。"""
    strategy, engine = mock_strategy

    import numpy as np
    base_prices = 10.0 + np.sin(np.linspace(0, 3, 28)) * 0.1
    prices = list(base_prices) + [10.1, 11.5]
    volumes = [1000.0] * 30  # 未放量
    turnovers = [60_000_000.0] * 30

    df = pd.DataFrame({
        "date": [f"2026-01-{i+1:02d}" for i in range(30)],
        "open": prices.copy(),
        "high": [p + 0.1 for p in prices],
        "low": [p - 0.1 for p in prices],
        "close": prices,
        "volume": volumes,
        "turnover": turnovers,
    })
    df.loc[29, "open"] = 10.2
    df.loc[29, "close"] = 11.5

    engine.get_local_symbols.return_value = ["600000"]
    engine.get_ohlcv.return_value = df

    res = strategy.run()
    assert res == []
