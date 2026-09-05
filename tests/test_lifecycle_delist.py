"""Unit tests for stock lifecycle, status migration, delisted filtering, and safe upsert."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy


class TestLifecycleDelist(unittest.TestCase):
    """Test stock status migration, delisted exclusion, safe upsert, and strategy date validation."""

    def setUp(self):
        import shutil
        import uuid
        self.tmp_dir = Path(__file__).resolve().parent / f".tmp_test_{self._testMethodName}"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp_dir / f"test_{uuid.uuid4().hex}.db"
        self.settings = Settings(db_path=str(self.db_path))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_status_migration_on_legacy_db(self):
        """Test automatic migration adds 'status' column to legacy stock_basic table."""
        # 1. Create a legacy table without 'status' column
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE stock_basic (
                symbol       TEXT PRIMARY KEY,
                code         TEXT NOT NULL,
                name         TEXT NOT NULL,
                industry_l1  TEXT,
                industry_l2  TEXT,
                updated_at   TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO stock_basic VALUES ('000001', 'sz.000001', '平安银行', '银行', '银行', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        # 2. Instantiate DataEngine, which should run automatic schema migration
        engine = DataEngine(self.settings)

        # 3. Verify 'status' column exists and defaulted to 1
        with sqlite3.connect(self.db_path) as conn:
            cols = [col[1] for col in conn.execute("PRAGMA table_info(stock_basic)").fetchall()]
            self.assertIn("status", cols)
            row = conn.execute("SELECT symbol, status FROM stock_basic WHERE symbol='000001'").fetchone()
            self.assertEqual(row[1], 1)

    def test_get_active_symbols_filters_delisted(self):
        """Test get_active_symbols returns only status=1 stocks."""
        engine = DataEngine(self.settings)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO stock_basic (symbol, code, name, status, industry_l1, industry_l2, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                [
                    ("000001", "sz.000001", "平安银行", 1, "银行", "银行"),
                    ("600519", "sh.600519", "贵州茅台", 1, "食品饮料", "白酒"),
                    ("000002", "sz.000002", "退市标的", 0, "房地产", "房地产开发"),
                ],
            )
            conn.commit()

        active = engine.get_active_symbols()
        self.assertIn("000001", active)
        self.assertIn("600519", active)
        self.assertNotIn("000002", active)
        self.assertEqual(len(active), 2)

    def test_get_market_latest_date(self):
        """Test get_market_latest_date gets maximum date from active stocks."""
        engine = DataEngine(self.settings)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO stock_basic (symbol, code, name, status, industry_l1, industry_l2, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                [
                    ("000001", "sz.000001", "平安银行", 1, "银行", "银行"),
                    ("000002", "sz.000002", "退市标的", 0, "房地产", "房地产开发"),
                ],
            )
            # Insert historical records
            conn.executemany(
                """
                INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
                VALUES (?, ?, 10, 11, 9, 10, 1000, 10000)
                """,
                [
                    ("000001", "2026-09-04"),
                    ("000001", "2026-09-05"),
                    ("000002", "2026-09-10"),  # Delisted stock has later date, should be ignored
                ],
            )
            conn.commit()

        latest_date = engine.get_market_latest_date()
        self.assertEqual(latest_date, "2026-09-05")

    def test_sync_today_bulk_safe_upsert_preserves_history(self):
        """Test sync_today_bulk safely upserts new records without deleting existing history."""
        from datetime import date
        engine = DataEngine(self.settings)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO stock_basic (symbol, code, name, status, industry_l1, industry_l2, updated_at)
                VALUES ('000001', 'sz.000001', '平安银行', 1, '银行', '银行', datetime('now'))
                """
            )
            # Insert 3 days of historical data (2026-09-01, 2026-09-02, 2026-09-03)
            for i in range(1, 4):
                conn.execute(
                    f"""
                    INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
                    VALUES ('000001', '2026-09-0{i}', 10, 11, 9, 10, 1000, 10000)
                    """
                )
            conn.commit()

        # Mock multiprocessing to return 1 new bar for 2026-09-04
        mock_new_rows = [["000001", "2026-09-04", "11.0", "12.0", "10.5", "11.5", "2000.0", "23000.0"]]
        engine._synced_stock_basic = True

        fake_today = date(2026, 9, 4)  # Friday
        with patch("multiprocessing.Pool") as mock_pool_cls, \
             patch("datetime.date") as mock_date, \
             patch.object(engine, "_probe_date_has_data", return_value=True):
            mock_date.today.return_value = fake_today
            mock_date.fromisoformat = date.fromisoformat
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            mock_pool = MagicMock()
            mock_pool.__enter__.return_value = mock_pool
            mock_pool.map.return_value = [mock_new_rows]
            mock_pool_cls.return_value = mock_pool

            # Trigger sync
            count = engine.sync_today_bulk()

        self.assertEqual(count, 1)

        # Verify all 4 records exist in database (3 past history + 1 new bar)
        with sqlite3.connect(self.db_path) as conn:
            total_rows = conn.execute("SELECT COUNT(*) FROM stock_daily WHERE symbol='000001'").fetchone()[0]
            self.assertEqual(total_rows, 4)

            # Verify the new record
            new_bar = conn.execute(
                "SELECT close, volume FROM stock_daily WHERE symbol='000001' AND date='2026-09-04'"
            ).fetchone()
            self.assertIsNotNone(new_bar)
            self.assertEqual(new_bar[0], 11.5)

    def test_strategy_excludes_delisted_and_suspended_stocks(self):
        """Test strategies exclude delisted stocks and stocks that did not trade on the latest market date."""
        engine = DataEngine(self.settings)

        # 3 stocks in basic:
        # 1. 000001: active, traded today 2026-09-05, triggers breakout
        # 2. 000002: delisted (status=0), triggers breakout
        # 3. 600519: active, but suspended (last date 2026-08-01), triggers breakout
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO stock_basic (symbol, code, name, status, industry_l1, industry_l2, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                [
                    ("000001", "sz.000001", "平安银行", 1, "银行", "银行"),
                    ("000002", "sz.000002", "退市股份", 0, "房地产", "房地产开发"),
                    ("600519", "sh.600519", "贵州茅台", 1, "食品饮料", "白酒"),
                ],
            )
            # Insert 25 bars for 000001 (latest 2026-09-05, breakout)
            # MA5 > MA20 golden cross + volume surge
            for i in range(25):
                d = f"2026-08-{i+10:02d}" if i < 20 else f"2026-09-0{i-19}"
                p = 10.0 + (i * 0.1)
                vol = 100000 if i < 24 else 500000  # volume surge on last day
                conn.execute(
                    """
                    INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("000001", d, p - 0.1, p + 0.5, p - 0.2, p + 0.2, vol, 10000000),
                )

            # Insert 25 bars for 000002 (delisted, latest 2024-05-20)
            for i in range(25):
                d = f"2024-05-{i+1:02d}"
                conn.execute(
                    """
                    INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
                    VALUES (?, ?, 10, 11, 9, 10.5, 500000, 10000000)
                    """,
                    ("000002", d),
                )

            # Insert 25 bars for 600519 (suspended, latest 2026-08-01)
            for i in range(25):
                d = f"2026-07-{i+1:02d}"
                conn.execute(
                    """
                    INSERT INTO stock_daily (symbol, date, open, high, low, close, volume, turnover)
                    VALUES (?, ?, 100, 110, 90, 105, 500000, 10000000)
                    """,
                    ("600519", d),
                )
            conn.commit()

        strat = MaVolumeStrategy(engine=engine, settings=self.settings)
        # Patch MaVolumeStrategy golden cross condition to pass
        with patch.object(strat, "run", wraps=strat.run):
            selected = strat.run()

        # Delisted 000002 must NOT be selected
        self.assertNotIn("000002", selected)
        # Suspended 600519 must NOT be selected because its last date (2026-07-25) != market latest (2026-09-05)
        self.assertNotIn("600519", selected)

    def test_sync_stock_basic_detects_new_and_delisted(self):
        """Test sync_stock_basic identifies newly listed stocks and marks delisted stocks."""
        engine = DataEngine(self.settings)

        # Pre-seed with existing stocks
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO stock_basic (symbol, code, name, status, industry_l1, industry_l2, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                [
                    ("600000", "sh.600000", "浦发银行", 1, "银行", "银行"),
                    ("000001", "sz.000001", "平安银行", 1, "银行", "银行"),
                ],
            )
            conn.commit()

        # Mock baostock returning:
        # - sh.600000: still status=1
        # - sz.000001: now status=0 (delisted!)
        # - sh.688999: status=1 (new IPO!)
        mock_rows = [
            ["sh.600000", "浦发银行", "2000-01-01", "", "1", "1"],
            ["sz.000001", "平安银行", "1991-04-03", "2026-09-01", "0", "1"],
            ["sh.688999", "全新科技", "2026-09-05", "", "1", "1"],
        ]

        class MockRs:
            def __init__(self, rows):
                self.rows = rows
                self.idx = -1
            def next(self):
                self.idx += 1
                return self.idx < len(self.rows)
            def get_row_data(self):
                return self.rows[self.idx]

        with patch("baostock.login") as mock_login, \
             patch("baostock.query_stock_basic", return_value=MockRs(mock_rows)), \
             patch("baostock.logout"):
            mock_login.return_value.error_code = "0"

            new_symbols, delisted_symbols = engine.sync_stock_basic(force=True)

        self.assertIn("688999", new_symbols)
        self.assertIn("000001", delisted_symbols)

        # Check DB updates
        with sqlite3.connect(self.db_path) as conn:
            s000001 = conn.execute("SELECT status FROM stock_basic WHERE symbol='000001'").fetchone()[0]
            self.assertEqual(s000001, 0)

            s688999 = conn.execute("SELECT status, name FROM stock_basic WHERE symbol='688999'").fetchone()
            self.assertIsNotNone(s688999)
            self.assertEqual(s688999[0], 1)
            self.assertEqual(s688999[1], "全新科技")


if __name__ == "__main__":
    unittest.main()
