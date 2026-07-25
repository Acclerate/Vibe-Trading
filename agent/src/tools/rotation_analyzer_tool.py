"""Sector-rotation analyzer: multi-board monthly return matrix + ranking.

A composite read-only tool that batch-fetches monthly klines for a list of
boards (via the shared Eastmoney client, the same ``fetch_kline`` ``sector_tool``
uses for ``mode='board_history'``), derives each board's monthly percent change,
and returns both the rotation matrix and a period-cumulative ranking. This is
the structured-data backbone for sector-rotation analysis: instead of reading
narrative roundups, the agent gets a reproducible month-by-month return table it
can reason over.

Designed to complement ``get_sector_info(mode='board_history')`` — that tool
fetches one board's raw klines; this tool fetches many boards and does the
monthly-return + ranking math so the caller gets a ready-to-read matrix in one
call. All HTTP routes through the shared throttled Eastmoney client; no auth.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from backtest.loaders.eastmoney_client import fetch_kline
from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

# Eastmoney board secid prefix (market 90 = board universe, same as sector_tool).
_BOARD_MARKET_PREFIX = "90"
# Monthly klt code (mirrors sector_tool._INTERVAL_KLT["1M"]).
_KLT_MONTHLY = 103

# Defaults so a no-argument call returns a useful answer, not an empty one.
_DEFAULT_BOARDS: list[dict[str, str]] = [
    {"code": "BK1326", "name": "半导体设备", "category": "科技"},
    {"code": "BK1325", "name": "半导体材料", "category": "科技"},
    {"code": "BK1137", "name": "存储芯片", "category": "科技"},
    {"code": "BK1036", "name": "半导体", "category": "科技"},
    {"code": "BK0877", "name": "PCB", "category": "科技"},
    {"code": "BK1128", "name": "CPO概念", "category": "科技"},
    {"code": "BK1101", "name": "先进封装", "category": "科技"},
    {"code": "BK0475", "name": "白酒", "category": "消费"},
    {"code": "BK0438", "name": "食品饮料", "category": "消费"},
    {"code": "BK1611", "name": "银行", "category": "金融"},
    {"code": "BK0473", "name": "证券", "category": "金融"},
    {"code": "BK0474", "name": "保险", "category": "金融"},
    {"code": "BK0437", "name": "煤炭", "category": "周期"},
    {"code": "BK0478", "name": "有色金属", "category": "周期"},
    {"code": "BK0464", "name": "石油石化", "category": "周期"},
    {"code": "BK0479", "name": "钢铁", "category": "周期"},
    {"code": "BK0428", "name": "电力", "category": "周期"},
    {"code": "BK1216", "name": "医药生物", "category": "医药"},
]

# Bounds on inputs so a fat-fingered call can never blow up the LLM context or
# hammer the rate-limited endpoint.
_MAX_BOARDS = 30
_MAX_MONTHS = 24
_DEFAULT_MONTHS = 8
# Per-board retry on transient push2his failures (the endpoint intermittently
# 502s under load); keeps one flaky board from sinking the whole matrix.
_FETCH_RETRIES = 3
_FETCH_RETRY_DELAY_S = 1.5


def _err(message: str) -> str:
    """Serialize a failure envelope."""
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _normalize_board_code(raw: str) -> str | None:
    """Reduce a caller-supplied board identifier to the canonical ``BK<digits>``.

    Accepts ``"BK1137"``, ``"1137"``, ``"BK1137.SS"`` and returns ``"BK1137"``.
    Returns ``None`` when no digits can be extracted. Mirrors the helper in
    ``sector_tool``; duplicated here so this tool stays self-contained.

    Args:
        raw: Caller-supplied board identifier.

    Returns:
        The canonical ``BK<digits>`` form, or ``None``.
    """
    if not isinstance(raw, str):
        return None
    token = raw.strip().upper().split(".", 1)[0]
    digits = "".join(ch for ch in token if ch.isdigit())
    if not digits:
        return None
    return f"BK{digits}"


def _to_float(value: Any) -> float | None:
    """Coerce a cell to ``float``, ``None`` on missing/garbage."""
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_monthly_klines(board_code: str, months: int) -> list[dict[str, Any]]:
    """Fetch the most-recent ``months`` monthly bars for one board, with retry.

    Args:
        board_code: Canonical ``BK<digits>`` board code.
        months: Number of monthly bars to request (used to size the request).

    Returns:
        Ascending list of ``{trade_date, open, high, low, close, volume,
        amount}`` dicts. Empty list on persistent failure (the caller decides
        whether to skip the board or surface the gap).
    """
    secid = f"{_BOARD_MARKET_PREFIX}.{board_code}"
    last_exc: Exception | None = None
    for attempt in range(_FETCH_RETRIES):
        try:
            klines = fetch_kline(secid, klt=_KLT_MONTHLY, fqt=0)
            if not isinstance(klines, list):
                return []
            # Keep most-recent ``months`` bars (fetch_kline returns ascending).
            return klines[-months:] if len(klines) > months else list(klines)
        except Exception as exc:  # noqa: BLE001 - transient push2his failures
            last_exc = exc
            if attempt < _FETCH_RETRIES - 1:
                time.sleep(_FETCH_RETRY_DELAY_S)
    logger.warning(
        "rotation fetch failed for %s after %d attempts: %s",
        board_code, _FETCH_RETRIES, last_exc,
    )
    return []


def _month_key(trade_date: str) -> str:
    """Collapse a ``YYYY-MM-DD...`` date to its ``YYYY-MM`` month key."""
    return str(trade_date)[:7]


def _monthly_return(bar: dict[str, Any]) -> float | None:
    """Percent change of one monthly bar: ``(close / open - 1) * 100``.

    Returns ``None`` when either leg is missing/non-positive.

    Args:
        bar: One monthly kline dict with ``open`` and ``close`` keys.

    Returns:
        The month's percent change, or ``None``.
    """
    o = _to_float(bar.get("open"))
    c = _to_float(bar.get("close"))
    if o is None or c is None or o <= 0:
        return None
    return round((c / o - 1) * 100, 2)


def _cumulative_return(klines: list[dict[str, Any]]) -> float | None:
    """Percent change from the first to the last bar's close.

    Uses close-to-close (period total return), unlike ``_monthly_return`` which
    uses intra-month open-to-close. Returns ``None`` when there are fewer than
    two bars or the endpoints are unusable.
    """
    if len(klines) < 2:
        return None
    first = _to_float(klines[0].get("close"))
    last = _to_float(klines[-1].get("close"))
    if first is None or last is None or first <= 0:
        return None
    return round((last / first - 1) * 100, 2)


def _build_matrix(
    boards: list[dict[str, str]], months: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch klines for every board and compute the monthly return matrix.

    Args:
        boards: List of ``{code, name, category}`` dicts.
        months: Number of most-recent monthly bars to fetch per board.

    Returns:
        A tuple ``(rows, month_keys)`` where ``rows`` is a list of per-board
        dicts ``{code, name, category, monthly: {YYYY-MM: pct}, cumulative,
        klines_count, fetch_failed}`` and ``month_keys`` is the sorted union of
        all months seen across boards.
    """
    rows: list[dict[str, Any]] = []
    all_months: set[str] = set()
    for spec in boards:
        raw_code = spec.get("code", "")
        board_code = _normalize_board_code(raw_code)
        if board_code is None:
            rows.append({
                "code": raw_code, "name": spec.get("name", ""),
                "category": spec.get("category", ""), "monthly": {},
                "cumulative": None, "klines_count": 0, "fetch_failed": True,
            })
            continue
        klines = _fetch_monthly_klines(board_code, months)
        monthly: dict[str, float] = {}
        for bar in klines:
            mk = _month_key(bar.get("trade_date", ""))
            if not mk:
                continue
            ret = _monthly_return(bar)
            if ret is not None:
                monthly[mk] = ret
                all_months.add(mk)
        rows.append({
            "code": board_code,
            "name": spec.get("name", board_code),
            "category": spec.get("category", ""),
            "monthly": monthly,
            "cumulative": _cumulative_return(klines),
            "klines_count": len(klines),
            "fetch_failed": len(klines) == 0,
        })
    month_keys = sorted(all_months)
    return rows, month_keys


def _rank_by_cumulative(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rows sorted by cumulative return, best first; failures sink.

    Args:
        rows: Per-board matrix rows from :func:`_build_matrix`.

    Returns:
        A new list sorted descending by ``cumulative``; rows with ``None``
        cumulative (too few bars or fetch failure) sink to the bottom in their
        original order.
    """
    return sorted(
        rows,
        key=lambda r: (r.get("cumulative") is not None, r.get("cumulative") if r.get("cumulative") is not None else -1e18),
        reverse=True,
    )


def _monthly_leaders(rows: list[dict[str, Any]], month_keys: list[str]) -> list[dict[str, Any]]:
    """For each month, name the best and worst board by monthly return.

    Args:
        rows: Per-board matrix rows.
        month_keys: Sorted month strings.

    Returns:
        A list of ``{month, leader: {name, pct}, laggard: {name, pct}}`` dicts,
        one per month. Months with no data are omitted.
    """
    out: list[dict[str, Any]] = []
    for mk in month_keys:
        valid = [
            (r["name"], r["monthly"].get(mk))
            for r in rows
            if r["monthly"].get(mk) is not None
        ]
        if not valid:
            continue
        valid.sort(key=lambda x: x[1], reverse=True)
        out.append({
            "month": mk,
            "leader": {"name": valid[0][0], "pct": valid[0][1]},
            "laggard": {"name": valid[-1][0], "pct": valid[-1][1]},
        })
    return out


class RotationAnalyzerTool(BaseTool):
    """Build a multi-board monthly rotation matrix with cumulative ranking."""

    name = "analyze_rotation"
    description = (
        "Build a sector-rotation matrix: fetch monthly klines for a list of "
        "A-share boards (sectors/concepts) and return each board's monthly "
        "percent change, the period-cumulative return ranking, and per-month "
        "leader/laggard. Use this for data-driven rotation analysis instead of "
        "reading narrative roundups — month-over-month returns reveal style "
        "switches and crowding unwind that single-day news often misses. Boards "
        "default to a curated 18-board pool spanning tech / financials / cyclicals / consumer / healthcare; "
        "pass 'boards' to override. Call with no args for a ready-to-read H1 "
        "rotation snapshot. Data source: Eastmoney push2his (free, no auth). "
        'Examples: {} (defaults) or {"boards": [{"code":"BK1137","name":"存储 '
        '芯片","category":"科技"}], "months": 12}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "boards": {
                "type": "array",
                "description": (
                    "List of boards to analyze. Each item is "
                    "{code (BK-prefixed, e.g. BK1137), name, category}. When "
                    "omitted, uses a curated 18-board pool covering the main "
                    f"rotation groups (max {_MAX_BOARDS})."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["code"],
                },
            },
            "months": {
                "type": "integer",
                "description": (
                    "Number of most-recent monthly bars to fetch per board. "
                    f"Default {_DEFAULT_MONTHS}, capped at {_MAX_MONTHS}."
                ),
                "default": _DEFAULT_MONTHS,
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        """Validate inputs, build the matrix, and return a JSON envelope.

        Args:
            **kwargs: ``boards`` (optional list of {code,name,category} dicts),
                ``months`` (optional int).

        Returns:
            A JSON string ``{"ok": true, "market": "a_share", "source":
            "eastmoney", "data": {"months": [...], "matrix": [...],
            "ranking": [...], "monthly_leaders": [...]}}`` on success, or
            ``{"ok": false, "error": ...}`` on validation failure.
        """
        # Resolve boards: caller-supplied (validated) or the curated default.
        raw_boards = kwargs.get("boards")
        if raw_boards is None:
            boards = list(_DEFAULT_BOARDS)
        else:
            if not isinstance(raw_boards, list) or not raw_boards:
                return _err("boards must be a non-empty list of {code,name,category} dicts")
            if len(raw_boards) > _MAX_BOARDS:
                return _err(f"too many boards: {len(raw_boards)} > max {_MAX_BOARDS}")
            boards = []
            for item in raw_boards:
                if not isinstance(item, dict) or not _normalize_board_code(str(item.get("code", ""))):
                    return _err(f"invalid board entry (need code): {item!r}")
                boards.append({
                    "code": str(item.get("code")),
                    "name": str(item.get("name", item.get("code"))),
                    "category": str(item.get("category", "")),
                })

        # Resolve months.
        months = kwargs.get("months", _DEFAULT_MONTHS)
        if not isinstance(months, int) or isinstance(months, bool) or months < 1:
            return _err("months must be a positive integer")
        months = min(months, _MAX_MONTHS)

        rows, month_keys = _build_matrix(boards, months)
        ranking = _rank_by_cumulative(rows)
        leaders = _monthly_leaders(rows, month_keys)

        # Count how many boards actually came back with data, so the caller can
        # tell a "no data" matrix apart from a real all-empty matrix.
        ok_boards = sum(1 for r in rows if not r.get("fetch_failed"))

        envelope = {
            "ok": True,
            "market": "a_share",
            "source": "eastmoney",
            "data": {
                "months": month_keys,
                "matrix": rows,
                "ranking": ranking,
                "monthly_leaders": leaders,
                "boards_requested": len(boards),
                "boards_ok": ok_boards,
            },
        }
        return json.dumps(envelope, ensure_ascii=False)
