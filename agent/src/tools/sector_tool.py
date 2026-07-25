"""Read-only sector / concept board tool backed by the Eastmoney client.

Eastmoney publishes a free, no-auth board taxonomy that groups A-shares into
industry sectors (行业板块) and thematic concept boards (概念板块). This tool
exposes two read-only views over that taxonomy:

* **Membership** — given a stock ``code``, list the industry / concept boards
  that stock belongs to. Served by the push2 ``slist`` endpoint, addressed by
  the same ``secid`` scheme used for klines.
* **Ranking** — with ``mode="ranking"``, rank the industry boards themselves by
  intraday percent change. Served by the push2 ``clist`` endpoint over the
  industry-board universe (``fs=m:90+t:2``).

Both endpoints route through :mod:`backtest.loaders.eastmoney_client` so every
request goes through the shared per-host throttle (Eastmoney rate-limits by IP
and bans bursting clients). Membership covers A-shares (``.SH`` / ``.SZ`` /
``.BJ``); ranking is the A-share board universe.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backtest.loaders.eastmoney_client import fetch_kline, get_json, resolve_secid
from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

# Eastmoney push2 board endpoints. ``slist`` returns the boards one stock
# belongs to; ``clist`` enumerates / ranks a board universe.
_MEMBERSHIP_URL = "https://push2.eastmoney.com/api/qt/slist/get"
_RANKING_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# Field selectors. f12 = board/security code, f14 = name, f3 = change percent,
# f2 = latest price, f104/f105 = up/down constituent counts (ranking only).
_MEMBERSHIP_FIELDS = "f12,f13,f14,f3,f2"
_RANKING_FIELDS = "f12,f14,f3,f2,f104,f105,f128,f140"
# Member (constituent) view fields: quote + valuation. f2 = price, f3 = change
# pct, f6 = amount, f8 = turnover rate, f9 = dynamic PE, f12 = code, f14 = name,
# f20 = total market cap, f23 = PB, f25 = YTD pct, f115 = PE(TTM).
_MEMBERS_FIELDS = "f2,f3,f6,f8,f9,f12,f14,f20,f23,f25,f115"

# Industry-board universe selector for the ranking view (m:90 = board market,
# t:2 = industry board sub-type). Sort by f3 (change percent), descending.
_RANKING_FS = "m:90+t:2"

# Board-history kline interval -> Eastmoney ``klt`` code. Mirrors the daily /
# weekly / monthly entries in eastmoney_client.KLT_BY_INTERVAL; intraday is
# intentionally excluded since boards are tracked at EOD granularity.
_INTERVAL_KLT = {"1D": 101, "1W": 102, "1M": 103}
_VALID_INTERVALS = tuple(_INTERVAL_KLT.keys())

# Defensive caps so a payload can never blow up the LLM context.
_MAX_RANKING = 100
_DEFAULT_RANKING = 30
_MAX_MEMBERS = 100
_DEFAULT_MEMBERS = 30
_MAX_HISTORY = 1000
_DEFAULT_HISTORY = 120
_VALID_MODES = ("membership", "ranking", "members", "board_history")


def _error(message: str) -> str:
    """Build the failure envelope as a JSON string.

    Args:
        message: Human-readable error description.

    Returns:
        A ``{"ok": false, "error": ...}`` JSON string.
    """
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _as_float(value: Any) -> float | None:
    """Coerce an Eastmoney numeric cell to ``float``, or ``None`` if unusable.

    Eastmoney emits ``"-"`` for missing numerics; those map to ``None``.

    Args:
        value: Raw cell value from a push2 row.

    Returns:
        The float value, or ``None`` when the cell is missing / non-numeric.
    """
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_membership_row(row: Any) -> dict[str, Any] | None:
    """Parse one ``slist`` diff row into a labelled board-membership dict.

    Args:
        row: One element of ``data.diff`` (a dict keyed by ``f12``/``f14``/...).

    Returns:
        A dict ``{board_code, board_name, change_pct, price}``, or ``None`` when
        the row lacks an identifiable board code/name.
    """
    if not isinstance(row, dict):
        return None
    board_code = row.get("f12")
    board_name = row.get("f14")
    if not board_code or not board_name:
        return None
    return {
        "board_code": str(board_code),
        "board_name": str(board_name),
        "change_pct": _as_float(row.get("f3")),
        "price": _as_float(row.get("f2")),
    }


def _parse_ranking_row(row: Any) -> dict[str, Any] | None:
    """Parse one ``clist`` diff row into a labelled board-ranking dict.

    Args:
        row: One element of ``data.diff`` (a dict keyed by ``f12``/``f14``/...).

    Returns:
        A dict ``{board_code, board_name, change_pct, index, leader, up_count,
        down_count}``, or ``None`` when the row lacks a board code/name.
    """
    if not isinstance(row, dict):
        return None
    board_code = row.get("f12")
    board_name = row.get("f14")
    if not board_code or not board_name:
        return None
    leader = row.get("f140")
    return {
        "board_code": str(board_code),
        "board_name": str(board_name),
        "change_pct": _as_float(row.get("f3")),
        "index": _as_float(row.get("f2")),
        "up_count": _as_float(row.get("f104")),
        "down_count": _as_float(row.get("f105")),
        "leader": str(leader) if leader and leader != "-" else None,
    }


def _diff_rows(payload: Any) -> list:
    """Extract the ``data.diff`` row list from a push2 payload, defensively.

    Args:
        payload: Decoded JSON from a push2 board endpoint.

    Returns:
        The list of diff rows, or ``[]`` when the payload carries none.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    diff = data.get("diff")
    if isinstance(diff, dict):
        # Some push2 responses key diff rows by string index instead of a list.
        return list(diff.values())
    if isinstance(diff, list):
        return diff
    return []


def _fetch_membership(code: str) -> str:
    """Fetch the industry / concept boards one stock belongs to.

    Args:
        code: Vibe-Trading A-share symbol (e.g. ``"600519.SH"``).

    Returns:
        A JSON envelope string with the resolved boards, or an error envelope
        when the symbol is unresolvable or the request fails.
    """
    secid = resolve_secid(code)
    if secid is None:
        return _error(f"unresolvable symbol: {code}")

    try:
        payload = get_json(
            _MEMBERSHIP_URL,
            params={
                "secid": secid,
                "spt": "3",
                "pi": "0",
                "pz": "100",
                "fields": _MEMBERSHIP_FIELDS,
                "fltt": "2",
                "po": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
        logger.warning("sector membership fetch failed for %s: %s", code, exc)
        return _error(f"membership request failed: {exc}")

    boards = [
        parsed
        for parsed in (_parse_membership_row(r) for r in _diff_rows(payload))
        if parsed is not None
    ]
    envelope = {
        "ok": True,
        "market": "stock",
        "source": "eastmoney",
        "mode": "membership",
        "data": {"code": code, "secid": secid, "boards": boards},
    }
    return json.dumps(envelope, ensure_ascii=False)


def _fetch_ranking(limit: int) -> str:
    """Fetch the industry-board ranking by intraday percent change.

    Args:
        limit: Number of top boards to keep (already validated and capped).

    Returns:
        A JSON envelope string with the ranked boards, or an error envelope when
        the request fails.
    """
    try:
        payload = get_json(
            _RANKING_URL,
            params={
                "fs": _RANKING_FS,
                "fields": _RANKING_FIELDS,
                "pn": "1",
                "pz": str(limit),
                "po": "1",
                "fid": "f3",
                "fltt": "2",
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
        logger.warning("sector ranking fetch failed: %s", exc)
        return _error(f"ranking request failed: {exc}")

    boards = [
        parsed
        for parsed in (_parse_ranking_row(r) for r in _diff_rows(payload))
        if parsed is not None
    ]
    if len(boards) > limit:
        boards = boards[:limit]
    envelope = {
        "ok": True,
        "market": "stock",
        "source": "eastmoney",
        "mode": "ranking",
        "data": {"boards": boards},
    }
    return json.dumps(envelope, ensure_ascii=False)


def _parse_members_row(row: Any) -> dict[str, Any] | None:
    """Parse one ``clist`` diff row into a board-member (constituent) record.

    Args:
        row: One element of ``data.diff`` for a ``fs=b:<board>`` member query.

    Returns:
        A dict ``{code, name, price, change_pct, amount, turnover, pe_dynamic,
        pe_ttm, market_cap, pb, ytd_pct}``, or ``None`` when the row lacks an
        identifiable code. Numeric cells become ``float`` or ``None`` (Eastmoney
        emits ``"-"`` for missing values); ``market_cap`` and ``amount`` are
        scaled to 100 million RMB units (亿) since the raw cells are in yuan.
    """
    if not isinstance(row, dict):
        return None
    code = row.get("f12")
    if not code:
        return None
    return {
        "code": str(code),
        "name": str(row.get("f14", "")),
        "price": _as_float(row.get("f2")),
        "change_pct": _as_float(row.get("f3")),
        "amount_yi": _div_yi(row.get("f6")),
        "turnover_rate": _as_float(row.get("f8")),
        "pe_dynamic": _as_float(row.get("f9")),
        "pe_ttm": _as_float(row.get("f115")),
        "market_cap_yi": _div_yi(row.get("f20")),
        "pb": _as_float(row.get("f23")),
        "ytd_pct": _as_float(row.get("f25")),
    }


def _div_yi(value: Any) -> float | None:
    """Scale a yuan-valued Eastmoney cell to 100-million units (亿元).

    push2 returns ``f6`` (turnover amount) and ``f20`` (total market cap) in
    raw yuan. Converting to 亿 here keeps the member rows compact and lets the
    LLM reason in the same units Chinese market commentary uses.

    Args:
        value: Raw cell value from a push2 row (yuan), or a missing sentinel.

    Returns:
        The value divided by 1e8, rounded to 2 decimals, or ``None`` when the
        cell is missing / non-numeric.
    """
    amount = _as_float(value)
    if amount is None:
        return None
    return round(amount / 1e8, 2)


def _normalize_board_code(raw: str) -> str | None:
    """Normalize a caller-supplied board identifier to the BK-prefixed form.

    Eastmoney addresses a board by its code (e.g. ``BK1137`` = 存储芯片) and
    accepts it bare or with an exchange suffix. We accept ``"BK1137"``,
    ``"1137"``, ``"BK1137.SS"`` and return the canonical ``"BK1137"``. A bare
    4-digit numeric code is promoted to ``BK<n>``.

    Args:
        raw: Caller-supplied board identifier.

    Returns:
        The canonical ``BK<digits>`` form, or ``None`` when no digits can be
        extracted.
    """
    if not isinstance(raw, str):
        return None
    token = raw.strip().upper().split(".", 1)[0]
    digits = "".join(ch for ch in token if ch.isdigit())
    if not digits:
        return None
    return f"BK{digits}"


def _fetch_members(board_code: str, limit: int) -> str:
    """Fetch the constituent stocks of one board, ranked by market cap.

    Args:
        board_code: Canonical board code (``BK<digits>``), already normalized.
        limit: Number of constituents to keep (already validated and capped).

    Returns:
        A JSON envelope string with the member rows, or an error envelope when
        the request fails.
    """
    try:
        payload = get_json(
            _RANKING_URL,  # members reuse the clist endpoint with a board filter
            params={
                "fs": f"b:{board_code}",
                "fields": _MEMBERS_FIELDS,
                "pn": "1",
                "pz": str(limit),
                "po": "1",
                "fid": "f20",  # sort by total market cap, descending
                "fltt": "2",
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
        logger.warning("sector members fetch failed for %s: %s", board_code, exc)
        return _error(f"members request failed: {exc}")

    members = [
        parsed
        for parsed in (_parse_members_row(r) for r in _diff_rows(payload))
        if parsed is not None
    ]
    if len(members) > limit:
        members = members[:limit]
    envelope = {
        "ok": True,
        "market": "stock",
        "source": "eastmoney",
        "mode": "members",
        "data": {"board_code": board_code, "members": members},
    }
    return json.dumps(envelope, ensure_ascii=False)


def _fetch_history(board_code: str, interval: str, limit: int) -> str:
    """Fetch historical OHLCV klines for one board (轮动时间序列数据源).

    Boards are addressed on Eastmoney's market ``90`` (the same universe
    ``ranking`` enumerates), so a board secid is ``90.BK<digits>``. We build it
    directly from the already-normalized ``board_code`` rather than going
    through :func:`resolve_secid`, which only handles individual-stock symbols
    and returns ``None`` for board codes. The actual fetch + field parsing is
    delegated to :func:`backtest.loaders.eastmoney_client.fetch_kline`, which
    accepts any secid and returns parsed ``{trade_date, open, high, low, close,
    volume, amount}`` dicts in ascending date order.

    Args:
        board_code: Canonical board code (``BK<digits>``), already normalized.
        interval: One of :data:`_VALID_INTERVALS` (``"1D"``/``"1W"``/``"1M"``).
        limit: Number of most-recent bars to keep (already validated/capped).

    Returns:
        A JSON envelope string with the klines, or an error envelope when the
        request fails. Boards have no dividend/split events, so ``fqt=0`` (no
        adjustment) is always passed.
    """
    secid = f"90.{board_code}"
    try:
        klines = fetch_kline(secid, klt=_INTERVAL_KLT[interval], fqt=0)
    except Exception as exc:  # noqa: BLE001 - surface a clean error envelope
        logger.warning("board history fetch failed for %s: %s", board_code, exc)
        return _error(f"board_history request failed: {exc}")

    # fetch_kline returns ascending (rev=1); keep the most-recent ``limit`` bars.
    if len(klines) > limit:
        klines = klines[-limit:]
    envelope = {
        "ok": True,
        "market": "stock",
        "source": "eastmoney",
        "mode": "board_history",
        "data": {
            "board_code": board_code,
            "interval": interval,
            "klines": klines,
        },
    }
    return json.dumps(envelope, ensure_ascii=False)


class SectorInfoTool(BaseTool):
    """Look up sector / concept board membership for a stock, or rank boards."""

    name = "get_sector_info"
    description = (
        "Look up Chinese A-share sector / concept board info via Eastmoney "
        "(free, no auth). Three modes: (1) membership — given a stock 'code' "
        "(e.g. 600519.SH / 000001.SZ / .BJ), list the industry and concept "
        "boards it belongs to; (2) ranking — set mode='ranking' to rank "
        "industry boards by today's percent change (with up/down constituent "
        "counts and the leading stock); (3) members — set mode='members' with "
        "'board_code' (e.g. BK1137=存储芯片, BK0877=PCB) to list that board's "
        "constituent stocks with quote + valuation fields (price, change pct, "
        "turnover, PE_TTM, PB, market cap, YTD pct), sorted by market cap; "
        "(4) board_history — set mode='board_history' with 'board_code' and "
        "optional 'interval' ('1D'/'1W'/'1M', default '1D') to fetch that "
        "board's historical OHLCV klines (one row per bar, ascending date), the "
        "time-series backbone for sector-rotation analysis. Use this to map a "
        "stock to its sectors, see which sectors are hot today, enumerate a "
        "board's constituents for screening, or pull a board's kline history "
        "for rotation/momentum work. Market: A-share stocks. Examples: "
        "{\"code\": \"600519.SH\"} or {\"mode\": \"ranking\", \"limit\": 20} or "
        "{\"mode\": \"members\", \"board_code\": \"BK1137\"} or "
        "{\"mode\": \"board_history\", \"board_code\": \"BK1137\", "
        "\"interval\": \"1W\", \"limit\": 26}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "A-share stock symbol with market suffix, e.g. '600519.SH', "
                    "'000001.SZ', '430139.BJ'. Required when mode='membership' "
                    "(the default); ignored for mode='ranking', mode='members' "
                    "and mode='board_history'."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["membership", "ranking", "members", "board_history"],
                "description": (
                    "'membership' (default) lists the boards a stock belongs to "
                    "and requires 'code'. 'ranking' ranks industry boards by "
                    "today's percent change and ignores 'code'. 'members' lists "
                    "the constituent stocks of one board and requires "
                    "'board_code'. 'board_history' returns a board's historical "
                    "OHLCV klines and requires 'board_code' (optionally "
                    "'interval' and 'limit')."
                ),
                "default": "membership",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Number of rows to return. For mode='ranking' or "
                    f"mode='members': 1-{_MAX_RANKING}. For mode='board_history': "
                    f"1-{_MAX_HISTORY} most-recent bars. Ignored for "
                    f"mode='membership'. Default {_DEFAULT_RANKING} (ranking/"
                    f"members) or {_DEFAULT_HISTORY} (board_history)."
                ),
                "default": _DEFAULT_RANKING,
            },
            "board_code": {
                "type": "string",
                "description": (
                    "Eastmoney board code for mode='members' and "
                    "mode='board_history', e.g. 'BK1137' (存储芯片), 'BK0877' "
                    "(PCB), 'BK1326' (半导体设备). Accepts the bare form or "
                    "with a suffix ('BK1137.SS'). Ignored for other modes."
                ),
            },
            "interval": {
                "type": "string",
                "enum": list(_VALID_INTERVALS),
                "description": (
                    "Kline bar size for mode='board_history': '1D' (daily), "
                    "'1W' (weekly), '1M' (monthly). Ignored for other modes."
                ),
                "default": "1D",
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to the membership / ranking / members / board_history view.

        Args:
            **kwargs: ``mode`` ("membership"|"ranking"|"members"|"board_history",
                default "membership"), ``code`` (str, required for membership),
                ``limit`` (int; default 30 for ranking/members, 120 for
                board_history), ``board_code`` (str, required for members and
                board_history), ``interval`` ("1D"|"1W"|"1M", default "1D", used
                by board_history).

        Returns:
            A JSON string ``{"ok": true, "market": "stock", "source":
            "eastmoney", "mode": ..., "data": {...}}`` on success, or
            ``{"ok": false, "error": ...}`` on a validation / request failure.
        """
        mode = kwargs.get("mode", "membership")
        if mode not in _VALID_MODES:
            return _error(f"mode must be one of {list(_VALID_MODES)}")

        if mode == "ranking":
            limit = kwargs.get("limit", _DEFAULT_RANKING)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                return _error("limit must be a positive integer")
            return _fetch_ranking(min(limit, _MAX_RANKING))

        if mode == "members":
            limit = kwargs.get("limit", _DEFAULT_MEMBERS)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                return _error("limit must be a positive integer")
            board_code = _normalize_board_code(str(kwargs.get("board_code", "")))
            if board_code is None:
                return _error(
                    "board_code must be a non-empty board code for mode='members' "
                    "(e.g. 'BK1137')"
                )
            return _fetch_members(board_code, min(limit, _MAX_MEMBERS))

        if mode == "board_history":
            limit = kwargs.get("limit", _DEFAULT_HISTORY)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                return _error("limit must be a positive integer")
            interval = kwargs.get("interval", "1D")
            if interval not in _VALID_INTERVALS:
                return _error(f"interval must be one of {list(_VALID_INTERVALS)}")
            board_code = _normalize_board_code(str(kwargs.get("board_code", "")))
            if board_code is None:
                return _error(
                    "board_code must be a non-empty board code for "
                    "mode='board_history' (e.g. 'BK1137')"
                )
            return _fetch_history(board_code, interval, min(limit, _MAX_HISTORY))

        code = kwargs.get("code")
        if not isinstance(code, str) or not code.strip():
            return _error("code must be a non-empty string for mode='membership'")
        return _fetch_membership(code.strip())
