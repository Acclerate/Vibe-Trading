"""Tests for datacenter_tool: multi-report envelope, field projection, validation.

No request leaves the process: the success path patches the shared Eastmoney
client (:func:`backtest.loaders.eastmoney_client.get_json`) so the tool's own
parsing / projection logic runs against canned datacenter payloads, and the
error path makes that boundary raise.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from backtest.loaders import eastmoney_client
from src.tools.datacenter_tool import DataCenterTool


def _financials_payload() -> dict:
    """Two reporting periods in the RPT_LICO_FN_CPD response shape."""
    return {
        "result": {
            "data": [
                {
                    "SECURITY_CODE": "301308",
                    "SECURITY_NAME_ABBR": "江波龙",
                    "REPORTDATE": "2026-03-31 00:00:00",
                    "DATATYPE": "2026年 一季报",
                    "TOTAL_OPERATE_INCOME": 9908726244.41,
                    "PARENT_NETPROFIT": 3862236561.17,
                    "WEIGHTAVG_ROE": 39.4,
                    "YSTZ": 132.79,
                    "SJLTZ": 2644.05,
                    "XSMLL": 55.53,
                    "BPS": 29.07,
                },
                {
                    "SECURITY_CODE": "301308",
                    "SECURITY_NAME_ABBR": "江波龙",
                    "REPORTDATE": "2025-12-31 00:00:00",
                    "DATATYPE": "2025年 年报",
                    "TOTAL_OPERATE_INCOME": 22766169990.55,
                    "PARENT_NETPROFIT": 1423298162.88,
                    "WEIGHTAVG_ROE": 19.41,
                    "YSTZ": 30.36,
                    "SJLTZ": 185.41,
                    "XSMLL": 19.40,
                    "BPS": 18.74,
                },
            ]
        }
    }


def _holder_count_payload() -> dict:
    """Two periods in the RPT_F10_EH_HOLDERNUM response shape."""
    return {
        "result": {
            "data": [
                {
                    "SECURITY_CODE": "600519",
                    "SECURITY_NAME_ABBR": "贵州茅台",
                    "END_DATE": "2026-03-31 00:00:00",
                    "NOTICE_DATE": "2026-04-25 00:00:00",
                    "HOLDER_TOTAL_NUM": 243159,
                    "HOLDER_TOTAL_NUMCHANGE": -12733,
                    "CHANGEWITHLAST": -4.98,
                    "HOLD_FOCUS": "非常分散",
                }
            ]
        }
    }


class TestFinancialsReport:
    """report='financials' reads already-disclosed statements, newest first."""

    def test_financials_projects_rows(self) -> None:
        tool = DataCenterTool()
        with patch.object(
            eastmoney_client, "get_json", return_value=_financials_payload()
        ) as get_json:
            out = tool.execute(report="financials", code="301308.SH")

        get_json.assert_called_once()
        _, kwargs = get_json.call_args
        assert kwargs["params"]["reportName"] == "RPT_LICO_FN_CPD"
        assert kwargs["params"]["filter"] == '(SECURITY_CODE="301308")'
        assert kwargs["params"]["pageSize"] == "4"  # default limit

        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["market"] == "a_share"
        assert payload["source"] == "eastmoney"
        assert payload["data"]["report"] == "financials"
        assert payload["data"]["code"] == "301308"
        assert payload["data"]["name"] == "江波龙"

        records = payload["data"]["records"]
        assert len(records) == 2
        # Full projection for the newest period.
        assert records[0] == {
            "report_date": "2026-03-31",
            "period": "2026年 一季报",
            "revenue": 9908726244.41,
            "net_profit": 3862236561.17,
            "roe": 39.4,
            "revenue_yoy_pct": 132.79,
            "net_profit_yoy_pct": 2644.05,
            "gross_margin_pct": 55.53,
            "bps": 29.07,
        }
        # Newest first (Q1 2026 before 2025 annual).
        assert records[0]["report_date"] > records[1]["report_date"]

    def test_financials_limit_passed_to_pageSize(self) -> None:
        tool = DataCenterTool()
        with patch.object(
            eastmoney_client, "get_json", return_value=_financials_payload()
        ) as get_json:
            tool.execute(report="financials", code="301308", limit=8)
        assert get_json.call_args.kwargs["params"]["pageSize"] == "8"

    def test_financials_limit_capped(self) -> None:
        tool = DataCenterTool()
        with patch.object(
            eastmoney_client, "get_json", return_value=_financials_payload()
        ) as get_json:
            tool.execute(report="financials", code="301308", limit=10_000)
        # Capped to the defensive maximum (40).
        assert get_json.call_args.kwargs["params"]["pageSize"] == "40"

    def test_financials_resorts_newest_first_client_side(self) -> None:
        """The server's NOTICE_DATE sort is not always honored; we re-sort."""
        out_of_order = {
            "result": {
                "data": [
                    # Oldest first from the server — must be re-sorted.
                    _financials_payload()["result"]["data"][1],  # 2025-12-31
                    _financials_payload()["result"]["data"][0],  # 2026-03-31
                ]
            }
        }
        with patch.object(eastmoney_client, "get_json", return_value=out_of_order):
            payload = json.loads(
                DataCenterTool().execute(report="financials", code="301308")
            )
        dates = [r["report_date"] for r in payload["data"]["records"]]
        assert dates == ["2026-03-31", "2025-12-31"]


class TestHolderCountReport:
    """report='holder_count' reads shareholder-count changes."""

    def test_holder_count_projects_rows(self) -> None:
        tool = DataCenterTool()
        with patch.object(
            eastmoney_client, "get_json", return_value=_holder_count_payload()
        ) as get_json:
            out = tool.execute(report="holder_count", code="600519")

        _, kwargs = get_json.call_args
        assert kwargs["params"]["reportName"] == "RPT_F10_EH_HOLDERNUM"
        assert kwargs["params"]["filter"] == '(SECURITY_CODE="600519")'

        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["data"]["report"] == "holder_count"
        assert payload["data"]["name"] == "贵州茅台"
        records = payload["data"]["records"]
        assert len(records) == 1
        assert records[0] == {
            "report_date": "2026-03-31",
            "notice_date": "2026-04-25",
            "holder_total_num": 243159.0,
            "holder_num_change": -12733.0,
            "holder_change_pct": -4.98,
            "hold_focus": "非常分散",  # free text kept as string
        }


class TestCodeParsing:
    """The bare code is derived from many caller-supplied forms."""

    def test_bare_code(self) -> None:
        with patch.object(
            eastmoney_client, "get_json", return_value=_financials_payload()
        ) as get_json:
            DataCenterTool().execute(report="financials", code="301308")
        assert get_json.call_args.kwargs["params"]["filter"] == '(SECURITY_CODE="301308")'

    def test_prefixed_form(self) -> None:
        with patch.object(
            eastmoney_client, "get_json", return_value=_financials_payload()
        ) as get_json:
            DataCenterTool().execute(report="financials", code="sh301308")
        assert get_json.call_args.kwargs["params"]["filter"] == '(SECURITY_CODE="301308")'


class TestErrorEnvelope:
    """Validation and request failures return the ok=false envelope."""

    def test_invalid_report_rejected(self) -> None:
        payload = json.loads(
            DataCenterTool().execute(report="forecast", code="301308")
        )
        assert payload["ok"] is False
        assert "report" in payload["error"]

    def test_missing_code_rejected(self) -> None:
        payload = json.loads(DataCenterTool().execute(report="financials"))
        assert payload["ok"] is False
        assert "A-shares" in payload["error"]

    def test_non_a_share_code_rejected(self) -> None:
        payload = json.loads(
            DataCenterTool().execute(report="financials", code="AAPL.US")
        )
        assert payload["ok"] is False

    def test_http_failure_error_envelope(self) -> None:
        with patch.object(
            eastmoney_client, "get_json", side_effect=RuntimeError("HTTP 500")
        ):
            payload = json.loads(
                DataCenterTool().execute(report="financials", code="301308")
            )
        assert payload["ok"] is False
        assert "500" in payload["error"]

    def test_empty_result_error_envelope(self) -> None:
        # A successful response with no matching rows is surfaced as an error
        # rather than an empty-success envelope, so callers can tell "no data"
        # apart from "request worked".
        empty = {"result": None}
        with patch.object(eastmoney_client, "get_json", return_value=empty):
            payload = json.loads(
                DataCenterTool().execute(report="financials", code="999999")
            )
        assert payload["ok"] is False
        assert "999999" in payload["error"]
