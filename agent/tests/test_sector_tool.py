"""Tests for sector_tool: envelope shape, parsing, mode dispatch, validation.

All HTTP is mocked at the Eastmoney client functions the tool imports
(:func:`get_json` / :func:`resolve_secid`), so no test touches a live endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.tools.sector_tool import SectorInfoTool

_MEMBERSHIP_PAYLOAD = {
    "data": {
        "diff": [
            {"f12": "BK0477", "f14": "白酒", "f3": 1.23, "f2": 1700.0},
            {"f12": "BK0815", "f14": "酿酒行业", "f3": -0.5, "f2": "-"},
            {"f14": "missing-code"},  # dropped: no f12
        ]
    }
}

_RANKING_PAYLOAD = {
    "data": {
        "diff": [
            {
                "f12": "BK0477",
                "f14": "白酒",
                "f3": 3.4,
                "f2": 12345.0,
                "f104": 18,
                "f105": 2,
                "f140": "贵州茅台",
            },
            {
                "f12": "BK0727",
                "f14": "银行",
                "f3": 1.1,
                "f2": 6789.0,
                "f104": 30,
                "f105": 12,
                "f140": "-",
            },
        ]
    }
}


class TestMembershipEnvelope:
    """A resolvable stock yields the ok envelope with parsed boards."""

    def test_membership_parses_boards(self):
        with patch(
            "src.tools.sector_tool.resolve_secid", return_value="1.600519"
        ), patch(
            "src.tools.sector_tool.get_json", return_value=_MEMBERSHIP_PAYLOAD
        ) as mock_get:
            text = SectorInfoTool().execute(code="600519.SH")

        url = mock_get.call_args[0][0]
        assert "slist/get" in url
        assert mock_get.call_args.kwargs["params"]["secid"] == "1.600519"

        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["market"] == "stock"
        assert payload["source"] == "eastmoney"
        assert payload["mode"] == "membership"
        assert payload["data"]["code"] == "600519.SH"
        assert payload["data"]["secid"] == "1.600519"

        boards = payload["data"]["boards"]
        assert len(boards) == 2  # the f12-less row is dropped
        assert boards[0] == {
            "board_code": "BK0477",
            "board_name": "白酒",
            "change_pct": 1.23,
            "price": 1700.0,
        }
        # "-" price coerces to None.
        assert boards[1]["price"] is None

    def test_membership_default_mode_when_only_code(self):
        with patch(
            "src.tools.sector_tool.resolve_secid", return_value="0.000001"
        ), patch("src.tools.sector_tool.get_json", return_value={"data": {"diff": []}}):
            payload = json.loads(SectorInfoTool().execute(code="000001.SZ"))

        assert payload["mode"] == "membership"
        assert payload["data"]["boards"] == []


class TestRankingEnvelope:
    """mode='ranking' enumerates the industry-board universe."""

    def test_ranking_parses_boards(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value=_RANKING_PAYLOAD
        ) as mock_get:
            text = SectorInfoTool().execute(mode="ranking", limit=20)

        url = mock_get.call_args[0][0]
        assert "clist/get" in url
        assert mock_get.call_args.kwargs["params"]["fs"] == "m:90+t:2"

        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["mode"] == "ranking"
        boards = payload["data"]["boards"]
        assert len(boards) == 2
        assert boards[0]["board_name"] == "白酒"
        assert boards[0]["leader"] == "贵州茅台"
        assert boards[0]["up_count"] == 18.0
        # "-" leader coerces to None.
        assert boards[1]["leader"] is None

    def test_ranking_ignores_code_and_skips_resolve(self):
        with patch("src.tools.sector_tool.resolve_secid") as resolve, patch(
            "src.tools.sector_tool.get_json", return_value=_RANKING_PAYLOAD
        ):
            payload = json.loads(
                SectorInfoTool().execute(mode="ranking", code="600519.SH")
            )

        assert payload["ok"] is True
        resolve.assert_not_called()

    def test_ranking_caps_limit(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value=_RANKING_PAYLOAD
        ) as mock_get:
            SectorInfoTool().execute(mode="ranking", limit=10_000)

        # Request pz is capped at the defensive maximum.
        assert mock_get.call_args.kwargs["params"]["pz"] == "100"

    def test_diff_as_dict_is_handled(self):
        dict_payload = {"data": {"diff": {"0": _RANKING_PAYLOAD["data"]["diff"][0]}}}
        with patch("src.tools.sector_tool.get_json", return_value=dict_payload):
            payload = json.loads(SectorInfoTool().execute(mode="ranking"))

        assert len(payload["data"]["boards"]) == 1


class TestErrorEnvelope:
    """Validation and request failures return the ok=false envelope."""

    def test_missing_code_for_membership_rejected(self):
        payload = json.loads(SectorInfoTool().execute())
        assert payload["ok"] is False
        assert "code" in payload["error"]

    def test_blank_code_rejected(self):
        payload = json.loads(SectorInfoTool().execute(code="   "))
        assert payload["ok"] is False

    def test_invalid_mode_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="trending"))
        assert payload["ok"] is False
        assert "mode" in payload["error"]

    def test_non_positive_limit_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="ranking", limit=0))
        assert payload["ok"] is False
        assert "limit" in payload["error"]

    def test_bool_limit_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="ranking", limit=True))
        assert payload["ok"] is False

    def test_unresolvable_symbol_error_envelope(self):
        with patch("src.tools.sector_tool.resolve_secid", return_value=None):
            payload = json.loads(SectorInfoTool().execute(code="WAT.XYZ"))
        assert payload["ok"] is False
        assert "unresolvable" in payload["error"]

    def test_http_failure_membership_error_envelope(self):
        with patch(
            "src.tools.sector_tool.resolve_secid", return_value="1.600519"
        ), patch(
            "src.tools.sector_tool.get_json", side_effect=RuntimeError("HTTP 429")
        ):
            payload = json.loads(SectorInfoTool().execute(code="600519.SH"))
        assert payload["ok"] is False
        assert "429" in payload["error"]

    def test_http_failure_ranking_error_envelope(self):
        with patch(
            "src.tools.sector_tool.get_json", side_effect=RuntimeError("HTTP 503")
        ):
            payload = json.loads(SectorInfoTool().execute(mode="ranking"))
        assert payload["ok"] is False
        assert "503" in payload["error"]


_MEMBERS_PAYLOAD = {
    "data": {
        "diff": [
            {
                "f12": "688981",
                "f14": "中芯国际",
                "f2": 160.0,
                "f3": 11.11,
                "f6": 17265064638.0,  # ~172.65 亿
                "f8": 5.7,
                "f9": 251.56,
                "f20": 1369728959200.0,  # ~13697 亿
                "f23": 9.14,
                "f25": 30.26,
                "f115": 271.47,
            },
            {
                "f12": "603986",
                "f14": "兆易创新",
                "f2": 475.53,
                "f3": 10.0,
                "f6": "-",
                "f8": "-",
                "f9": "-",
                "f20": "-",
                "f23": "-",
                "f25": "-",
                "f115": "-",
            },
            {"f14": "no-code"},  # dropped: no f12
        ]
    }
}


class TestMembersEnvelope:
    """mode='members' lists a board's constituent stocks with quote+valuation."""

    def test_members_parses_constituents(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value=_MEMBERS_PAYLOAD
        ) as mock_get:
            text = SectorInfoTool().execute(
                mode="members", board_code="BK1137", limit=30
            )

        url = mock_get.call_args[0][0]
        assert "clist/get" in url
        params = mock_get.call_args.kwargs["params"]
        assert params["fs"] == "b:BK1137"
        assert params["fid"] == "f20"  # sorted by market cap

        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["market"] == "stock"
        assert payload["mode"] == "members"
        assert payload["data"]["board_code"] == "BK1137"

        members = payload["data"]["members"]
        assert len(members) == 2  # the f12-less row is dropped
        # Full field mapping for a complete row.
        assert members[0] == {
            "code": "688981",
            "name": "中芯国际",
            "price": 160.0,
            "change_pct": 11.11,
            "amount_yi": 172.65,
            "turnover_rate": 5.7,
            "pe_dynamic": 251.56,
            "pe_ttm": 271.47,
            "market_cap_yi": 13697.29,
            "pb": 9.14,
            "ytd_pct": 30.26,
        }
        # Missing numerics ("-" sentinels) coerce to None.
        sparse = members[1]
        assert sparse["code"] == "603986"
        assert sparse["amount_yi"] is None
        assert sparse["pe_ttm"] is None
        assert sparse["market_cap_yi"] is None

    def test_board_code_normalized_bare_digits_promoted(self):
        """A bare 4-digit numeric code is promoted to the BK-prefixed form."""
        with patch("src.tools.sector_tool.get_json", return_value=_MEMBERS_PAYLOAD):
            payload = json.loads(
                SectorInfoTool().execute(mode="members", board_code="1137")
            )
        assert payload["ok"] is True
        assert payload["data"]["board_code"] == "BK1137"

    def test_board_code_suffix_stripped(self):
        """A board code with an exchange suffix is normalized back to BK<n>."""
        with patch("src.tools.sector_tool.get_json", return_value=_MEMBERS_PAYLOAD):
            payload = json.loads(
                SectorInfoTool().execute(mode="members", board_code="BK1137.SS")
            )
        assert payload["ok"] is True
        assert payload["data"]["board_code"] == "BK1137"

    def test_members_caps_limit(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value=_MEMBERS_PAYLOAD
        ) as mock_get:
            SectorInfoTool().execute(mode="members", board_code="BK1137", limit=10_000)
        assert mock_get.call_args.kwargs["params"]["pz"] == "100"

    def test_members_empty_diff_envelope(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value={"data": {"diff": []}}
        ):
            payload = json.loads(
                SectorInfoTool().execute(mode="members", board_code="BK1137")
            )
        assert payload["ok"] is True
        assert payload["data"]["members"] == []

    def test_missing_board_code_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="members"))
        assert payload["ok"] is False
        assert "board_code" in payload["error"]

    def test_non_digits_only_board_code_rejected(self):
        payload = json.loads(
            SectorInfoTool().execute(mode="members", board_code="存储芯片")
        )
        assert payload["ok"] is False
        assert "board_code" in payload["error"]

    def test_non_positive_limit_rejected(self):
        payload = json.loads(
            SectorInfoTool().execute(mode="members", board_code="BK1137", limit=0)
        )
        assert payload["ok"] is False
        assert "limit" in payload["error"]

    def test_bool_limit_rejected(self):
        payload = json.loads(
            SectorInfoTool().execute(mode="members", board_code="BK1137", limit=True)
        )
        assert payload["ok"] is False

    def test_http_failure_members_error_envelope(self):
        with patch(
            "src.tools.sector_tool.get_json", side_effect=RuntimeError("HTTP 502")
        ):
            payload = json.loads(
                SectorInfoTool().execute(mode="members", board_code="BK1137")
            )
        assert payload["ok"] is False
        assert "502" in payload["error"]


# Three parsed kline bars in the shape eastmoney_client.fetch_kline returns:
# ascending date, dict rows with canonical {trade_date, open, high, low, close,
# volume, amount} keys (fqt=0, no adjustment).
_KLINE_BARS = [
    {
        "trade_date": "2026-07-22",
        "open": 2850.43,
        "close": 2835.38,
        "high": 2947.08,
        "low": 2811.35,
        "volume": 57321752.0,
        "amount": 423953500924.0,
    },
    {
        "trade_date": "2026-07-23",
        "open": 2851.53,
        "close": 2777.09,
        "high": 2879.32,
        "low": 2748.13,
        "volume": 41080895.0,
        "amount": 300496376834.0,
    },
    {
        "trade_date": "2026-07-24",
        "open": 2731.76,
        "close": 2761.69,
        "high": 2822.70,
        "low": 2719.54,
        "volume": 42848183.0,
        "amount": 297534568679.0,
    },
]


class TestBoardHistoryEnvelope:
    """mode='board_history' returns a board's historical OHLCV klines."""

    def test_history_parses_klines(self):
        with patch(
            "src.tools.sector_tool.fetch_kline", return_value=_KLINE_BARS
        ) as mock_fetch:
            text = SectorInfoTool().execute(
                mode="board_history", board_code="BK1137", interval="1D", limit=10
            )

        # Board secid is built as 90.BK<digits>, not routed through resolve_secid.
        assert mock_fetch.call_args.args[0] == "90.BK1137"
        assert mock_fetch.call_args.kwargs["klt"] == 101  # 1D
        assert mock_fetch.call_args.kwargs["fqt"] == 0  # boards never adjusted

        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["market"] == "stock"
        assert payload["source"] == "eastmoney"
        assert payload["mode"] == "board_history"
        assert payload["data"]["board_code"] == "BK1137"
        assert payload["data"]["interval"] == "1D"

        klines = payload["data"]["klines"]
        assert len(klines) == 3
        # Ascending date order preserved; full OHLCV field mapping.
        assert klines[0] == _KLINE_BARS[0]
        assert klines[-1]["trade_date"] == "2026-07-24"

    def test_history_caps_limit_keeps_most_recent(self):
        """``limit`` keeps the most-recent N bars (fetch_kline returns ascending)."""
        # Build 5 bars; ask for limit=2 -> keep the last 2.
        bars = [
            {"trade_date": f"2026-07-{20 + i:02d}", "open": 1.0, "close": 1.0,
             "high": 1.0, "low": 1.0, "volume": 1.0, "amount": 1.0}
            for i in range(5)
        ]
        with patch("src.tools.sector_tool.fetch_kline", return_value=bars):
            payload = json.loads(
                SectorInfoTool().execute(
                    mode="board_history", board_code="BK1137", limit=2
                )
            )
        dates = [k["trade_date"] for k in payload["data"]["klines"]]
        assert dates == ["2026-07-23", "2026-07-24"]  # last 2 of 5

    def test_history_interval_validation(self):
        payload = json.loads(
            SectorInfoTool().execute(
                mode="board_history", board_code="BK1137", interval="4H"
            )
        )
        assert payload["ok"] is False
        assert "interval" in payload["error"]

    def test_history_weekly_interval_passed_as_klt_102(self):
        with patch("src.tools.sector_tool.fetch_kline", return_value=_KLINE_BARS) as mock_fetch:
            SectorInfoTool().execute(
                mode="board_history", board_code="BK1137", interval="1W"
            )
        assert mock_fetch.call_args.kwargs["klt"] == 102

    def test_history_board_code_normalized(self):
        """A bare numeric board code is promoted to BK<n> before secid build."""
        with patch("src.tools.sector_tool.fetch_kline", return_value=_KLINE_BARS) as mock_fetch:
            payload = json.loads(
                SectorInfoTool().execute(
                    mode="board_history", board_code="1137", interval="1M"
                )
            )
        assert payload["ok"] is True
        assert payload["data"]["board_code"] == "BK1137"
        assert mock_fetch.call_args.args[0] == "90.BK1137"
        assert mock_fetch.call_args.kwargs["klt"] == 103  # monthly

    def test_history_missing_board_code_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="board_history"))
        assert payload["ok"] is False
        assert "board_code" in payload["error"]

    def test_history_non_digits_board_code_rejected(self):
        payload = json.loads(
            SectorInfoTool().execute(mode="board_history", board_code="存储芯片")
        )
        assert payload["ok"] is False
        assert "board_code" in payload["error"]

    def test_history_non_positive_limit_rejected(self):
        payload = json.loads(
            SectorInfoTool().execute(
                mode="board_history", board_code="BK1137", limit=0
            )
        )
        assert payload["ok"] is False
        assert "limit" in payload["error"]

    def test_history_bool_limit_rejected(self):
        payload = json.loads(
            SectorInfoTool().execute(
                mode="board_history", board_code="BK1137", limit=True
            )
        )
        assert payload["ok"] is False

    def test_history_http_failure_error_envelope(self):
        with patch(
            "src.tools.sector_tool.fetch_kline", side_effect=RuntimeError("HTTP 500")
        ):
            payload = json.loads(
                SectorInfoTool().execute(
                    mode="board_history", board_code="BK1137", interval="1D"
                )
            )
        assert payload["ok"] is False
        assert "500" in payload["error"]

    def test_history_empty_klines_envelope(self):
        """A board with no bars in range still yields ok=true with empty klines."""
        with patch("src.tools.sector_tool.fetch_kline", return_value=[]):
            payload = json.loads(
                SectorInfoTool().execute(
                    mode="board_history", board_code="BK1137", interval="1D"
                )
            )
        assert payload["ok"] is True
        assert payload["data"]["klines"] == []
