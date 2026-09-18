"""数据引擎属性测试。"""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


def test_data_engine_adjustflag_is_qfq(mocker) -> None:
    """验证 DataEngine 拉取日线数据时严格使用前复权 (adjustflag='2')。"""
    from unittest.mock import MagicMock, patch
    from sequoia_x.data.engine import _bs_fetch_batch

    mock_rs = MagicMock()
    mock_rs.error_code = "0"
    mock_rs.next.return_value = False

    with patch("baostock.login") as mock_login, \
         patch("baostock.logout") as mock_logout, \
         patch("baostock.query_history_k_data_plus", return_value=mock_rs) as mock_query:
        
        mock_login.return_value.error_code = "0"
        tasks = [("000001", "sz.000001", "2026-09-01", "2026-09-05")]
        _bs_fetch_batch(tasks)

        assert mock_query.called
        _, kwargs = mock_query.call_args
        assert kwargs.get("adjustflag") == "2", "日线拉取必须使用前复权 (adjustflag='2')"


def test_get_market_latest_date_anti_pollution() -> None:
    """验证 get_market_latest_date 能够成功抵御个别孤立超前脏数据的单点污染。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)

        with sqlite3.connect(engine.db_path) as conn:
            # 写入基础信息表，10 只在市股票
            basics = [(f"60000{i}", f"sh.60000{i}", f"测试股{i}", 1) for i in range(10)]
            conn.executemany("INSERT INTO stock_basic (symbol, code, name, status) VALUES (?, ?, ?, ?)", basics)

            # 10 只股票均在 2026-09-17 有正常日K
            records_0917 = [
                (f"60000{i}", "2026-09-17", 10.0, 11.0, 9.0, 10.5, 1000.0, 10500.0)
                for i in range(10)
            ]
            conn.executemany(
                "INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                records_0917,
            )

            # 仅有 1 只股票（600000）被意外插入了一条未收盘/离群日期记录 2026-09-18
            conn.execute(
                "INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("600000", "2026-09-18", 10.5, 10.6, 10.4, 10.5, 50.0, 525.0),
            )
            conn.commit()

        # 调用决策器，验证不会被孤立的 2026-09-18 污染，必须返回全市场主流基准日 2026-09-17
        latest_date = engine.get_market_latest_date()
        assert latest_date == "2026-09-17", f"期望基准日为 2026-09-17，实际得到 {latest_date}"

