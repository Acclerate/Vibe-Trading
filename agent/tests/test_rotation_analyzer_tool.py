"""Tests for rotation_analyzer_tool: matrix build, ranking, validation.

No request leaves the process: every success path patches
:func:`backtest.loaders.eastmoney_client.fetch_kline` (the same boundary
``sector_tool`` uses) so the tool's matrix / ranking math runs against canned
monthly kline lists.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.tools.rotation_analyzer_tool import RotationAnalyzerTool


# Two canned monthly kline series. Each bar mirrors what
# eastmoney_client.fetch_kline returns: {trade_date, open, high, low, close,
# volume, amount}, ascending date.
_KLINES_GAIN = [
    {"trade_date": "2026-05-29", "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1.0, "amount": 1.0},
    {"trade_date": "2026-06-30", "open": 105.0, "high": 130.0, "low": 100.0, "close": 125.0, "volume": 1.0, "amount": 1.0},
    {"trade_date": "2026-07-31", "open": 125.0, "high": 100.0, "low": 80.0, "close": 90.0, "volume": 1.0, "amount": 1.0},
]

_KLINES_FLAT = [
    {"trade_date": "2026-05-29", "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0, "volume": 1.0, "amount": 1.0},
    {"trade_date": "2026-06-30", "open": 50.0, "high": 52.0, "low": 48.0, "close": 52.0, "volume": 1.0, "amount": 1.0},
    {"trade_date": "2026-07-31", "open": 52.0, "high": 55.0, "low": 50.0, "close": 54.0, "volume": 1.0, "amount": 1.0},
]


class TestMatrixBuild:
    """Custom boards list yields a parsed matrix with monthly returns."""

    def test_two_board_matrix(self):
        boards = [
            {"code": "BK1137", "name": "存储芯片", "category": "科技"},
            {"code": "BK0475", "name": "白酒", "category": "消费"},
        ]
        # Map each secid (90.BK1137 / 90.BK0475) to a canned series so the
        # matrix math runs deterministically.
        canned = {
            "90.BK1137": _KLINES_GAIN,
            "90.BK0475": _KLINES_FLAT,
        }

        def fake_fetch(secid, *, klt, fqt):
            return list(canned[secid])

        with patch(
            "src.tools.rotation_analyzer_tool.fetch_kline", side_effect=fake_fetch
        ) as mock_fetch:
            out = RotationAnalyzerTool().execute(boards=boards, months=3)

        # Both boards went through the board-secid form.
        called_secids = {call.args[0] for call in mock_fetch.call_args_list}
        assert called_secids == {"90.BK1137", "90.BK0475"}
        # Monthly klt, no adjustment.
        for call in mock_fetch.call_args_list:
            assert call.kwargs["klt"] == 103
            assert call.kwargs["fqt"] == 0

        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["market"] == "a_share"
        assert payload["source"] == "eastmoney"
        assert payload["data"]["boards_requested"] == 2
        assert payload["data"]["boards_ok"] == 2
        assert payload["data"]["months"] == ["2026-05", "2026-06", "2026-07"]

        # Per-board monthly returns: open->close pct, 2 decimals.
        matrix_by_code = {r["code"]: r for r in payload["data"]["matrix"]}
        gain = matrix_by_code["BK1137"]
        assert gain["name"] == "存储芯片"
        assert gain["category"] == "科技"
        assert gain["monthly"]["2026-05"] == 5.0      # 105/100-1
        assert gain["monthly"]["2026-06"] == round((125 / 105 - 1) * 100, 2)
        assert gain["monthly"]["2026-07"] == round((90 / 125 - 1) * 100, 2)  # negative
        # Cumulative = close-to-close: 90/105-1
        assert gain["cumulative"] == round((90 / 105 - 1) * 100, 2)

        flat = matrix_by_code["BK0475"]
        assert flat["monthly"]["2026-05"] == 0.0       # 50/50-1
        assert flat["monthly"]["2026-06"] == 4.0       # 52/50-1
        assert flat["cumulative"] == round((54 / 50 - 1) * 100, 2)

    def test_fetch_failure_marks_board_but_keeps_matrix(self):
        """A flaky board is flagged fetch_failed, not dropped from the matrix."""
        boards = [{"code": "BK1137", "name": "存储芯片", "category": "科技"}]

        def fake_fetch(secid, *, klt, fqt):
            raise RuntimeError("HTTP 502")

        with patch(
            "src.tools.rotation_analyzer_tool.fetch_kline", side_effect=fake_fetch
        ):
            payload = json.loads(
                RotationAnalyzerTool().execute(boards=boards, months=3)
            )

        assert payload["ok"] is True
        assert payload["data"]["boards_requested"] == 1
        assert payload["data"]["boards_ok"] == 0
        row = payload["data"]["matrix"][0]
        assert row["fetch_failed"] is True
        assert row["monthly"] == {}
        assert row["cumulative"] is None


class TestRanking:
    """Cumulative ranking sorts best-first; failures sink."""

    def test_ranking_orders_by_cumulative_descending(self):
        boards = [
            {"code": "BK1137", "name": "存储芯片", "category": "科技"},  # GAIN series: -14.29% cum
            {"code": "BK0475", "name": "白酒", "category": "消费"},     # FLAT series: +8.0% cum
        ]
        canned = {"90.BK1137": _KLINES_GAIN, "90.BK0475": _KLINES_FLAT}

        def fake_fetch(secid, *, klt, fqt):
            return list(canned[secid])

        with patch("src.tools.rotation_analyzer_tool.fetch_kline", side_effect=fake_fetch):
            payload = json.loads(RotationAnalyzerTool().execute(boards=boards))

        ranking = payload["data"]["ranking"]
        assert [r["code"] for r in ranking] == ["BK0475", "BK1137"]  # FLAT first (higher cum)


class TestMonthlyLeaders:
    """Per-month leader/laggard picks the best and worst board."""

    def test_monthly_leaders_named(self):
        boards = [
            {"code": "BK1137", "name": "存储芯片", "category": "科技"},
            {"code": "BK0475", "name": "白酒", "category": "消费"},
        ]
        canned = {"90.BK1137": _KLINES_GAIN, "90.BK0475": _KLINES_FLAT}

        def fake_fetch(secid, *, klt, fqt):
            return list(canned[secid])

        with patch("src.tools.rotation_analyzer_tool.fetch_kline", side_effect=fake_fetch):
            payload = json.loads(RotationAnalyzerTool().execute(boards=boards))

        leaders = {l["month"]: l for l in payload["data"]["monthly_leaders"]}
        # 2026-05: GAIN +5.0% beats FLAT 0.0% -> 存储芯片 leads, 白酒 laggards.
        assert leaders["2026-05"]["leader"]["name"] == "存储芯片"
        assert leaders["2026-05"]["leader"]["pct"] == 5.0
        assert leaders["2026-05"]["laggard"]["name"] == "白酒"
        # 2026-07: GAIN goes negative (-28%), FLAT positive (+3.8%) -> 白酒 leads.
        assert leaders["2026-07"]["leader"]["name"] == "白酒"
        assert leaders["2026-07"]["laggard"]["name"] == "存储芯片"


class TestBoardCodeNormalization:
    """Caller-supplied board codes are normalized before secid build."""

    def test_bare_digits_promoted_to_bk(self):
        boards = [{"code": "1137", "name": "存储芯片"}]
        with patch(
            "src.tools.rotation_analyzer_tool.fetch_kline", return_value=_KLINES_GAIN
        ) as mock_fetch:
            RotationAnalyzerTool().execute(boards=boards, months=3)
        assert mock_fetch.call_args.args[0] == "90.BK1137"


class TestErrorEnvelope:
    """Validation failures return ok=false."""

    def test_boards_must_be_non_empty_list(self):
        payload = json.loads(RotationAnalyzerTool().execute(boards=[]))
        assert payload["ok"] is False
        assert "boards" in payload["error"]

    def test_too_many_boards_rejected(self):
        boards = [{"code": "BK0001"}] * 50
        payload = json.loads(RotationAnalyzerTool().execute(boards=boards))
        assert payload["ok"] is False
        assert "too many" in payload["error"]

    def test_invalid_board_entry_rejected(self):
        boards = [{"code": "存储芯片"}]  # no digits
        payload = json.loads(RotationAnalyzerTool().execute(boards=boards))
        assert payload["ok"] is False
        assert "invalid board entry" in payload["error"]

    def test_non_positive_months_rejected(self):
        payload = json.loads(RotationAnalyzerTool().execute(months=0))
        assert payload["ok"] is False
        assert "months" in payload["error"]

    def test_bool_months_rejected(self):
        payload = json.loads(RotationAnalyzerTool().execute(months=True))
        assert payload["ok"] is False


class TestDefaultPool:
    """No-arg call hits the curated default pool (mocked to stay offline)."""

    def test_default_pool_uses_18_boards(self):
        with patch(
            "src.tools.rotation_analyzer_tool.fetch_kline", return_value=_KLINES_GAIN
        ) as mock_fetch:
            payload = json.loads(RotationAnalyzerTool().execute())

        assert payload["ok"] is True
        assert payload["data"]["boards_requested"] == 18
        # All 18 boards attempted (mock never raises here).
        assert mock_fetch.call_count == 18
