"""Table definitions shared by the SQLite and Supabase stores.

supabase/schema.sql must define the same tables and columns
(tests/test_storage.py checks that they match).
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# table -> (primary key, {column: sqlite type})
TABLES: dict[str, tuple[str, dict[str, str]]] = {
    "runs": ("run_id", {
        "run_id": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "status": "TEXT",
        "as_of_date": "TEXT", "days_processed": "INTEGER", "message": "TEXT", "host": "TEXT",
    }),
    "decisions": ("id", {
        "id": "TEXT", "as_of_date": "TEXT", "ticker": "TEXT", "strategy": "TEXT", "signal": "TEXT",
        "action": "TEXT", "reason": "TEXT", "close_price": "REAL", "created_at": "TEXT",
    }),
    "orders": ("order_id", {
        "order_id": "TEXT", "created_date": "TEXT", "ticker": "TEXT", "side": "TEXT", "qty": "INTEGER",
        "strategy": "TEXT", "reason": "TEXT", "status": "TEXT", "fill_date": "TEXT", "fill_price": "REAL",
        "fees": "REAL", "note": "TEXT",
    }),
    "positions": ("ticker", {
        "ticker": "TEXT", "qty": "INTEGER", "entry_date": "TEXT", "entry_price": "REAL", "stop_price": "REAL",
        "strategy": "TEXT", "last_price": "REAL", "market_value": "REAL", "unrealized_pnl": "REAL",
        "unrealized_pnl_pct": "REAL", "as_of_date": "TEXT",
    }),
    "trades": ("trade_id", {
        "trade_id": "TEXT", "ticker": "TEXT", "strategy": "TEXT", "entry_date": "TEXT", "entry_price": "REAL",
        "exit_date": "TEXT", "exit_price": "REAL", "qty": "INTEGER", "gross_pnl": "REAL", "fees": "REAL",
        "net_pnl": "REAL", "return_pct": "REAL", "exit_reason": "TEXT",
    }),
    "daily_snapshots": ("as_of_date", {
        "as_of_date": "TEXT", "cash": "REAL", "positions_value": "REAL", "equity": "REAL",
        "benchmark_close": "REAL", "daily_pnl_pct": "REAL", "open_positions": "INTEGER",
        "max_open_positions": "INTEGER", "max_daily_loss_pct": "REAL", "trading_halted": "INTEGER",
        "halt_reason": "TEXT", "selected_strategies": "TEXT", "selection_note": "TEXT", "state_json": "TEXT",
        "updated_at": "TEXT",
    }),
    "backtest_results": ("id", {
        "id": "TEXT", "as_of_date": "TEXT", "strategy": "TEXT", "period": "TEXT", "start_date": "TEXT",
        "end_date": "TEXT", "total_return": "REAL", "cagr": "REAL", "max_drawdown": "REAL", "sharpe": "REAL",
        "win_rate": "REAL", "n_trades": "INTEGER", "exposure": "REAL", "bh_return": "REAL",
        "excess_return": "REAL", "qualified": "INTEGER", "overfit_flag": "INTEGER", "rank": "INTEGER",
        "note": "TEXT",
    }),
}

JSON_COLUMNS = {"daily_snapshots": {"selected_strategies", "state_json"}}
BOOL_COLUMNS = {"daily_snapshots": {"trading_halted"}, "backtest_results": {"qualified", "overfit_flag"}}


def jsonable(value: Any) -> Any:
    """Convert numpy/pandas/date values to plain JSON-safe Python."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # pandas Timestamp
        return value.isoformat()
    return value


def normalize_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    _, columns = TABLES[table]
    unknown = set(row) - set(columns)
    if unknown:
        raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
    return {k: jsonable(v) for k, v in row.items()}


@dataclass
class WriteOp:
    """upsert: insert-or-update by primary key. replace: upsert and delete rows not in `rows`."""
    kind: str
    table: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "table": self.table, "rows": self.rows}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "WriteOp":
        return cls(data["kind"], data["table"], data["rows"])


def upsert(table: str, rows: list[dict[str, Any]]) -> WriteOp:
    return WriteOp("upsert", table, [normalize_row(table, r) for r in rows])


def replace(table: str, rows: list[dict[str, Any]]) -> WriteOp:
    return WriteOp("replace", table, [normalize_row(table, r) for r in rows])


def dumps(obj: Any) -> str:
    return json.dumps(jsonable(obj), separators=(",", ":"))
