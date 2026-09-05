"""Unit tests for HtmlReporter module."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from sequoia_x.report.html_reporter import HtmlReporter


def _generate_mock_df(n_bars: int = 50) -> pd.DataFrame:
    """Generate realistic synthetic OHLCV data for testing."""
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


class TestHtmlReporter(unittest.TestCase):
    """HtmlReporter unit test suite."""

    def setUp(self):
        self.tmp_path = Path(__file__).resolve().parent / ".tmp_test_reports"
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.engine = MagicMock()
        self.settings = MagicMock()
        self.settings.db_path = str(self.tmp_path / "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_path, ignore_errors=True)

    def test_reporter_empty_results(self):
        """Test generating report when no strategies hit any stocks."""
        reporter = HtmlReporter(engine=self.engine, settings=self.settings)

        empty_results = {
            "MaVolumeStrategy": [],
            "TurtleTradeStrategy": [],
        }

        report_path = reporter.generate(empty_results, output_dir=str(self.tmp_path / "reports"))

        self.assertTrue(Path(report_path).exists())
        content = Path(report_path).read_text(encoding="utf-8")
        self.assertIn("Sequoia-X 量化突破走势图报告", content)
        self.assertIn("未命中突破标的", content)

        # Verify latest.html is also created
        latest_path = self.tmp_path / "reports" / "latest.html"
        self.assertTrue(latest_path.exists())

    def test_reporter_with_breakout_stocks(self):
        """Test generating report with breakout stocks across different strategies."""
        mock_df = _generate_mock_df(60)
        self.engine.get_ohlcv.return_value = mock_df

        reporter = HtmlReporter(engine=self.engine, settings=self.settings)

        strategy_results = {
            "MaVolumeStrategy": ["600001"],
            "TurtleTradeStrategy": ["600002"],
            "BollBreakoutStrategy": ["600003"],
            "HighTightFlagStrategy": ["600004"],
            "LimitUpShakeoutStrategy": ["600005"],
            "RpsBreakoutStrategy": ["600006"],
            "PrivatePlacementStrategy": ["600007"],
        }

        with patch("sequoia_x.notify.feishu.FeishuNotifier._get_stock_names") as mock_names:
            mock_names.return_value = {
                "600001": "平安银行",
                "600002": "万科A",
                "600003": "特变电工",
                "600004": "白云机场",
                "600005": "包钢股份",
                "600006": "中信证券",
                "600007": "比亚迪",
            }

            report_path = reporter.generate(strategy_results, output_dir=str(self.tmp_path / "reports"))

        self.assertTrue(Path(report_path).exists())
        content = Path(report_path).read_text(encoding="utf-8")

        # Verify stock names and codes
        self.assertIn("平安银行", content)
        self.assertIn("600001", content)
        self.assertIn("万科A", content)
        self.assertIn("600002", content)
        self.assertIn("特变电工", content)
        self.assertIn("600003", content)

        # Verify technical strategy names
        self.assertIn("均线放量突破", content)
        self.assertIn("海龟新高突破", content)
        self.assertIn("布林带收口突破", content)
        self.assertIn("高窄旗形整理", content)
        self.assertIn("涨停洗盘回踩", content)
        self.assertIn("欧奈尔 RPS 突破", content)

        # Verify event table for PrivatePlacement
        self.assertIn("比亚迪", content)
        self.assertIn("定向增发", content)

        # Verify ECharts scripts and DataZoom configuration
        self.assertIn("echarts.min.js", content)
        self.assertIn("dataZoom", content)
        self.assertIn("boll_upper", content)
        self.assertIn("turtle_high20", content)

        # Verify chartDataList has valid elem_id matching container divs
        import json
        import re
        match = re.search(r"const chartDataList = (\[.*?\]);", content)
        self.assertIsNotNone(match, "chartDataList JSON definition must exist in HTML")
        chart_data_list = json.loads(match.group(1))
        self.assertGreater(len(chart_data_list), 0)
        for item in chart_data_list:
            elem_id = item.get("elem_id")
            self.assertIsNotNone(elem_id, "Each chart item must contain 'elem_id'")
            self.assertTrue(elem_id.startswith("chart_"), "elem_id must follow convention 'chart_'")
            self.assertIn(f'id="{elem_id}"', content, f"DOM container with id '{elem_id}' must exist")

    def test_reporter_handles_missing_ohlcv(self):
        """Test that reporter handles empty or short OHLCV data gracefully without error."""
        # Return empty DataFrame for 600999
        self.engine.get_ohlcv.return_value = pd.DataFrame()

        reporter = HtmlReporter(engine=self.engine, settings=self.settings)
        strategy_results = {"TurtleTradeStrategy": ["600999"]}

        with patch("sequoia_x.notify.feishu.FeishuNotifier._get_stock_names") as mock_names:
            mock_names.return_value = {"600999": "测试异常股"}
            report_path = reporter.generate(strategy_results, output_dir=str(self.tmp_path / "reports"))

        self.assertTrue(Path(report_path).exists())
        content = Path(report_path).read_text(encoding="utf-8")
        self.assertIn("未命中突破标的", content)


if __name__ == "__main__":
    unittest.main()
