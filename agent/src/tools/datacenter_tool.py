"""Read-only Eastmoney datacenter report tool (multi-report).

Eastmoney's ``datacenter-web`` host exposes a single parameterized JSON endpoint
that serves many tabular "reports" — already-disclosed financial statements,
shareholder-count changes, lock-up expiry, etc. — each addressed by a
``reportName`` and a ``filter`` predicate. Existing tools
(:mod:`margin_trading_tool`, :mod:`block_trades_tool`, :mod:`dragon_tiger_tool`,
...) each hard-wire one report; this tool exposes the same endpoint as a
*multi-report* reader so the agent can pull several datasets through one
surface without a new tool per report.

Reports are selected by a short, stable ``report`` key (``"financials"``,
``"holder_count"``) that maps internally to the Eastmoney ``reportName`` plus a
curated column list and a per-report field projection. A-share ``code`` is
always required; ``limit`` caps the number of most-recent periods returned. All
requests route through :mod:`backtest.loaders.eastmoney_client` (per-host
throttle + session reuse); the endpoint is read-only and needs no credentials.

Note: this tool intentionally covers *already-disclosed* data only. Eastmoney's
forward-looking earnings-forecast report name was not found to be publicly
served (``RPT_LICO_FN_YJYG`` and friends return "报表配置不存在"); a separate
forecast surface would belong in its own tool.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.loaders import eastmoney_client
from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

# Eastmoney datacenter report API. One endpoint serves every report; the
# ``reportName`` query parameter selects which table to read.
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# Per-report configuration. Each entry carries the Eastmoney ``report_name``,
# the raw columns to request, and a ``field_map`` that projects each raw row
# onto a stable, named record (raw key -> our key). Only fields that actually
# exist on the report are listed; unmapped columns are dropped to keep the
# envelope compact.
_REPORTS: dict[str, dict[str, Any]] = {
    # Already-disclosed financial statements (营收/净利/ROE/同比增速), newest
    # period first. RPT_LICO_FN_CPD covers annual / interim / quarterly reports
    # up to the most recently disclosed period.
    "financials": {
        "report_name": "RPT_LICO_FN_CPD",
        "label": "已披露财报",
        "columns": ",".join(
            [
                "SECURITY_CODE",
                "SECURITY_NAME_ABBR",
                "REPORTDATE",
                "DATATYPE",
                "TOTAL_OPERATE_INCOME",
                "PARENT_NETPROFIT",
                "WEIGHTAVG_ROE",
                "YSTZ",
                "SJLTZ",
                "XSMLL",
                "BPS",
            ]
        ),
        "field_map": {
            "REPORTDATE": "report_date",
            "DATATYPE": "period",
            "TOTAL_OPERATE_INCOME": "revenue",
            "PARENT_NETPROFIT": "net_profit",
            "WEIGHTAVG_ROE": "roe",
            "YSTZ": "revenue_yoy_pct",
            "SJLTZ": "net_profit_yoy_pct",
            "XSMLL": "gross_margin_pct",
            "BPS": "bps",
        },
    },
    # Shareholder-count changes (筹码集中度). Fewer holders / a negative change
    # signals concentration; the absolute count and the period-over-period delta
    # are the headline numbers.
    "holder_count": {
        "report_name": "RPT_F10_EH_HOLDERNUM",
        "label": "股东户数",
        "columns": ",".join(
            [
                "SECURITY_CODE",
                "SECURITY_NAME_ABBR",
                "END_DATE",
                "NOTICE_DATE",
                "HOLDER_TOTAL_NUM",
                "HOLDER_TOTAL_NUMCHANGE",
                "CHANGEWITHLAST",
                "HOLD_FOCUS",
            ]
        ),
        "field_map": {
            "END_DATE": "report_date",
            "NOTICE_DATE": "notice_date",
            "HOLDER_TOTAL_NUM": "holder_total_num",
            "HOLDER_TOTAL_NUMCHANGE": "holder_num_change",
            "CHANGEWITHLAST": "holder_change_pct",
            "HOLD_FOCUS": "hold_focus",
        },
    },
}

# Defensive caps so a wide response can never blow up the LLM context.
_MAX_LIMIT = 40
_DEFAULT_LIMIT = 4


def _err(message: str) -> str:
    """Serialize a failure envelope.

    Args:
        message: Human-readable error description.

    Returns:
        A ``{"ok": false, "error": ...}`` JSON string.
    """
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _to_float(value: Any) -> float | None:
    """Coerce a raw cell to ``float``, returning ``None`` on missing/garbage.

    Args:
        value: Raw cell value from a datacenter row (may be ``None``, ``""``,
            a numeric type, or a numeric string).

    Returns:
        The value as ``float``, or ``None`` when it is missing or non-numeric.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_value(out_key: str, raw_value: Any) -> Any:
    """Apply per-field coercion for one projected column.

    Date-shaped keys are truncated to a ``YYYY-MM-DD`` string; numeric keys are
    coerced to ``float`` / ``None``; everything else (free text like
    ``HOLD_FOCUS``) passes through verbatim as a string.

    Args:
        out_key: The projected key (from ``field_map`` values).
        raw_value: The raw cell value from the datacenter row.

    Returns:
        The coerced value.
    """
    if out_key.endswith("_date"):
        return str(raw_value)[:10] if raw_value else None
    if out_key == "hold_focus":
        return str(raw_value) if raw_value else None
    if out_key == "period":
        return str(raw_value) if raw_value else None
    return _to_float(raw_value)


def _extract_code(symbol: str) -> str | None:
    """Reduce a caller-supplied symbol to the bare A-share numeric code.

    Accepts ``"600519"``, ``"600519.SH"``, ``"sh600519"`` or ``"000001.SZ"`` and
    returns the six-digit code the datacenter ``SECURITY_CODE`` filter expects.
    Non-A (e.g. ``.HK`` / ``.US``) symbols return ``None`` since these reports
    are mainland-A-share datasets.

    Args:
        symbol: Caller-supplied stock identifier.

    Returns:
        The six-digit code, or ``None`` when no A-share code can be derived.
    """
    if not symbol or not isinstance(symbol, str):
        return None
    token = symbol.strip().upper()
    if "." in token:
        token = token.rpartition(".")[0]
    for prefix in ("SH", "SZ", "BJ"):
        if token.startswith(prefix):
            token = token[len(prefix):]
    token = token.strip()
    if len(token) == 6 and token.isdigit():
        return token
    return None


def _clamp_limit(value: Any) -> int:
    """Coerce the requested ``limit`` to a sane integer within bounds.

    Args:
        value: Raw value from kwargs (may be ``None``, str, or int).

    Returns:
        An int in ``[1, _MAX_LIMIT]``; falls back to ``_DEFAULT_LIMIT`` on junk.
    """
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _project_row(raw: dict[str, Any], field_map: dict[str, str]) -> dict[str, Any]:
    """Project one raw datacenter row onto the report's stable schema.

    Args:
        raw: One element of the report's ``result.data`` list.
        field_map: Report-specific raw-key -> output-key mapping.

    Returns:
        A dict keyed by the output names; numeric cells are coerced, dates are
        truncated, free-text cells are kept as strings.
    """
    row: dict[str, Any] = {}
    for source_key, out_key in field_map.items():
        row[out_key] = _normalize_value(out_key, raw.get(source_key))
    return row


def _fetch_report(report_key: str, code: str, limit: int) -> str:
    """Pull ``limit`` most-recent rows of one report for one A-share code.

    Args:
        report_key: Key into :data:`_REPORTS` (e.g. ``"financials"``).
        code: Bare six-digit A-share code.
        limit: Number of rows to keep (already validated and capped).

    Returns:
        A JSON envelope string with the projected rows, or an error envelope
        when the request fails or returns no data.
    """
    spec = _REPORTS[report_key]
    try:
        payload = eastmoney_client.get_json(
            _DATACENTER_URL,
            params={
                "reportName": spec["report_name"],
                "columns": spec["columns"],
                "filter": f'(SECURITY_CODE="{code}")',
                "sortColumns": "NOTICE_DATE",
                "sortTypes": "-1",
                "pageNumber": "1",
                "pageSize": str(limit),
                "source": "WEB",
                "client": "WEB",
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
        logger.warning("datacenter %s fetch failed for %s: %s", report_key, code, exc)
        return _err(f"datacenter request failed: {exc}")

    result = payload.get("result") if isinstance(payload, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or not data:
        return _err(f"No {report_key} data returned for {code}.")

    rows = [
        _project_row(item, spec["field_map"])
        for item in data
        if isinstance(item, dict)
    ]
    # Sort newest-period-first on the client. The datacenter's NOTICE_DATE sort
    # is not always honored server-side (rows have arrived out of order in
    # practice), so re-sort by the projected report_date — a YYYY-MM-DD string
    # that sorts naturally. Rows lacking a date sink to the bottom but stay.
    rows.sort(key=lambda r: str(r.get("report_date") or ""), reverse=True)
    return json.dumps(
        {
            "ok": True,
            "market": "a_share",
            "source": "eastmoney",
            "data": {
                "report": report_key,
                "code": code,
                "name": data[0].get("SECURITY_NAME_ABBR"),
                "records": rows[:limit],
            },
        },
        ensure_ascii=False,
    )


class DataCenterTool(BaseTool):
    """Read Eastmoney datacenter reports (已披露财报 / 股东户数) for an A-share."""

    name = "get_datacenter_report"
    description = (
        "Read Eastmoney datacenter reports for an A-share stock (free, no auth). "
        "Multi-report: set 'report' to pick a dataset — 'financials' for "
        "already-disclosed financial statements (revenue, net profit, ROE, "
        "revenue/net-profit YoY %, gross margin, BPS, one row per reporting "
        "period, newest first); 'holder_count' for shareholder-count changes "
        "(total holder count, period-over-period change in count and %, hold "
        "focus). Always requires a six-digit A-share 'code' (e.g. 600519, "
        "301308.SH). Use 'limit' to cap periods returned. Mainland A-shares "
        "only. Examples: {\"report\": \"financials\", \"code\": \"301308\"} or "
        "{\"report\": \"holder_count\", \"code\": \"600519\", \"limit\": 8}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "report": {
                "type": "string",
                "enum": list(_REPORTS.keys()),
                "description": (
                    "Which dataset to read. 'financials' = already-disclosed "
                    "financial statements (revenue / net profit / ROE / YoY %). "
                    "'holder_count' = shareholder-count changes (chip "
                    "concentration)."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "A-share stock code. Accepts a bare six-digit code "
                    '("600519"), a suffixed symbol ("600519.SH", "000001.SZ"), '
                    'or an exchange-prefixed form ("sh600519"). A-shares only.'
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Number of most-recent periods to return. "
                    f"Default {_DEFAULT_LIMIT}, capped at {_MAX_LIMIT}."
                ),
                "default": _DEFAULT_LIMIT,
            },
        },
        "required": ["report", "code"],
    }

    def execute(self, **kwargs: Any) -> str:
        """Validate inputs, fetch the report, and return a JSON envelope.

        Args:
            **kwargs: ``report`` (required, one of :data:`_REPORTS` keys),
                ``code`` (required, A-share code), ``limit`` (optional int).

        Returns:
            A JSON string ``{"ok": true, "market": "a_share", "source":
            "eastmoney", "data": {"report": ..., "code": ..., "name": ...,
            "records": [...]}}`` on success, or ``{"ok": false, "error": ...}``
            on a validation / request failure.
        """
        report_key = kwargs.get("report")
        if report_key not in _REPORTS:
            return _err(f"report must be one of {list(_REPORTS.keys())}")

        code = _extract_code(kwargs.get("code", ""))
        if code is None:
            return _err(
                "Unsupported symbol: datacenter reports cover A-shares only "
                "(e.g. 600519.SH or 000001.SZ)."
            )

        limit = _clamp_limit(kwargs.get("limit", _DEFAULT_LIMIT))
        return _fetch_report(report_key, code, limit)
