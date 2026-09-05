"""Shenwan (SW) industry classification helper module.

Provides Level 1 and Level 2 industry mappings for A-share stocks based on the
Shenwan 2021 classification standard.
"""

import json
from pathlib import Path

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_INDUSTRY_DATA_FILE = Path(__file__).resolve().parent / "sw_industry_data.json"
_CACHE: dict[str, dict[str, str]] | None = None


def _load_data() -> dict[str, dict[str, str]]:
    """Load pre-indexed Shenwan stock industry mapping dictionary."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    if _INDUSTRY_DATA_FILE.exists():
        try:
            with open(_INDUSTRY_DATA_FILE, "r", encoding="utf-8") as f:
                _CACHE = json.load(f)
                return _CACHE
        except Exception as exc:
            logger.warning(f"Failed to load sw_industry_data.json: {exc}")

    _CACHE = {}
    return _CACHE


class ShenwanIndustryService:
    """Service to look up Shenwan Level 1 and Level 2 industries for stocks."""

    @classmethod
    def get_industry(cls, symbol: str, fallback_l1: str = "") -> tuple[str, str]:
        """
        Get Shenwan Level 1 and Level 2 industries for a given stock symbol.

        Args:
            symbol: 6-digit numeric stock code, e.g. "600519"
            fallback_l1: Optional fallback Level 1 industry name (e.g. from baostock)

        Returns:
            tuple of (industry_l1, industry_l2), e.g. ("食品饮料", "白酒")
        """
        data = _load_data()
        # Extract pure 6-digit numeric code (e.g. 'sh.600519' -> '600519', '600519.SH' -> '600519')
        pure_digits = "".join(filter(str.isdigit, str(symbol)))
        sym = pure_digits[-6:].zfill(6) if len(pure_digits) >= 6 else str(symbol).zfill(6)

        if sym in data:
            entry = data[sym]
            return entry.get("l1", fallback_l1 or "其他"), entry.get("l2", "其他")

        if fallback_l1:
            return fallback_l1, fallback_l1

        return "其他", "其他"

    @classmethod
    def get_all_mapping(cls) -> dict[str, dict[str, str]]:
        """Return the complete stock-to-industry dictionary."""
        return _load_data()
