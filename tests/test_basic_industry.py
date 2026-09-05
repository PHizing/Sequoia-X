"""Unit tests for stock_basic table, Shenwan industry classification, and report industry filter."""

import json
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
from sequoia_x.data.sw_industry import ShenwanIndustryService
from sequoia_x.report.html_reporter import HtmlReporter


def _generate_mock_df(n_bars: int = 50) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    import numpy as np

    np.random.seed(42)
    base_price = 10.0 + np.cumsum(np.random.randn(n_bars) * 0.2)
    dates = [f"2026-01-{i+1:02d}" if i < 31 else f"2026-02-{i-30:02d}" for i in range(n_bars)]

    return pd.DataFrame({
        "date": dates,
        "open": base_price,
        "high": base_price + 0.5,
        "low": base_price - 0.5,
        "close": base_price + 0.1,
        "volume": [100000.0 + i * 1000 for i in range(n_bars)],
        "turnover": [50_000_000.0 for _ in range(n_bars)],
    })


class TestBasicIndustry(unittest.TestCase):
    """Test stock_basic, ShenwanIndustryService, and report industry filtering."""

    def setUp(self):
        import shutil
        self.tmp_dir = Path(__file__).resolve().parent / ".tmp_test_basic"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp_dir / "test_basic.db"
        self.settings = Settings(db_path=str(self.db_path))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_shenwan_industry_service(self):
        """Test ShenwanIndustryService query and fallback logic."""
        # 1. Test standard stocks
        l1, l2 = ShenwanIndustryService.get_industry("600519")
        self.assertEqual(l1, "食品饮料")
        self.assertEqual(l2, "白酒")
        self.assertNotIn("Ⅱ", l2)
        self.assertNotIn("Ⅲ", l2)

        l1, l2 = ShenwanIndustryService.get_industry("000001")
        self.assertEqual(l1, "银行")
        self.assertEqual(l2, "股份制银行")

        # 2. Test fallback for unknown symbol
        l1_fallback, l2_fallback = ShenwanIndustryService.get_industry("999999", fallback_l1="计算机")
        self.assertEqual(l1_fallback, "计算机")
        self.assertEqual(l2_fallback, "计算机")

        # 3. Test symbol with market prefix
        l1, l2 = ShenwanIndustryService.get_industry("sh.600519")
        self.assertEqual(l1, "食品饮料")
        self.assertEqual(l2, "白酒")

    def test_stock_basic_crud_and_engine(self):
        """Test stock_basic table creation, batch write, and query in DataEngine."""
        engine = DataEngine(self.settings)

        # Ensure table is created
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_basic'")
        self.assertIsNotNone(cur.fetchone())

        # Test insert/upsert into stock_basic
        cur.executemany(
            """
            INSERT OR REPLACE INTO stock_basic (symbol, code, name, industry_l1, industry_l2, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            [
                ("600519", "600519", "贵州茅台", "食品饮料", "白酒"),
                ("000001", "000001", "平安银行", "银行", "银行"),
                ("300750", "300750", "宁德时代", "电力设备", "电池"),
            ],
        )
        conn.commit()
        conn.close()

        # Test get_stock_basic_map
        basic_map = engine.get_stock_basic_map()
        self.assertIn("600519", basic_map)
        self.assertEqual(basic_map["600519"]["name"], "贵州茅台")
        self.assertEqual(basic_map["600519"]["industry_l1"], "食品饮料")
        self.assertEqual(basic_map["600519"]["industry_l2"], "白酒")

        # Test get_all_symbols reads from stock_basic
        all_symbols = engine.get_all_symbols()
        self.assertEqual(set(all_symbols), {"600519", "000001", "300750"})

    def test_html_reporter_with_industry_filter(self):
        """Test HtmlReporter generates industry badges, L1/L2 filter chips, and JS logic."""
        engine = MagicMock()
        mock_df = _generate_mock_df(50)
        engine.get_ohlcv.return_value = mock_df
        engine.get_stock_basic_map.return_value = {
            "600519": {"name": "贵州茅台", "industry_l1": "食品饮料", "industry_l2": "白酒"},
            "000001": {"name": "平安银行", "industry_l1": "银行", "industry_l2": "银行"},
            "000858": {"name": "五粮液", "industry_l1": "食品饮料", "industry_l2": "白酒"},
            "300750": {"name": "宁德时代", "industry_l1": "电力设备", "industry_l2": "电池"},
        }

        reporter = HtmlReporter(engine=engine, settings=self.settings)

        strategy_results = {
            "TurtleTradeStrategy": ["600519", "000858"],
            "MaVolumeStrategy": ["000001"],
            "PrivatePlacementStrategy": ["300750"],
        }

        reports_dir = self.tmp_dir / "reports"
        report_path = reporter.generate(strategy_results, output_dir=str(reports_dir))

        self.assertTrue(Path(report_path).exists())
        content = Path(report_path).read_text(encoding="utf-8")

        # 1. Verify industry badges
        self.assertIn("badge-industry", content)
        self.assertIn("食品饮料 · 白酒", content)
        self.assertIn("银行 · 银行", content)
        self.assertIn("电力设备 · 电池", content)

        # 2. Verify filter chips and container
        self.assertIn("申万一级行业", content)
        self.assertIn("二级行业下钻", content)
        self.assertIn('data-val="食品饮料"', content)
        self.assertIn('data-val="银行"', content)

        # 3. Verify data attributes for JS filtering
        self.assertIn('data-industry-l1="食品饮料"', content)
        self.assertIn('data-industry-l2="白酒"', content)

        # 4. Verify JavaScript filter functions
        self.assertIn("handleL1Filter", content)
        self.assertIn("handleL2Filter", content)
        self.assertIn("applyFilter", content)
        self.assertIn("industryTree", content)


if __name__ == "__main__":
    unittest.main()
