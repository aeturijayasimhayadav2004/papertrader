"""Local SQLite store: always written, and the fallback when Supabase is down.

Writes destined for Supabase are also queued in an `outbox` table and replayed
in order once Supabase is reachable again.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from papertrader.storage.schema import BOOL_COLUMNS, JSON_COLUMNS, TABLES, WriteOp, dumps

SavedState = tuple[dt.date, dict[str, Any]]


class SQLiteStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._create()

    def _create(self) -> None:
        with self.conn:
            for table, (pk, cols) in TABLES.items():
                body = ", ".join(f'"{c}" {t}{" PRIMARY KEY" if c == pk else ""}' for c, t in cols.items())
                self.conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({body})')
            self.conn.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                              "created_at TEXT, payload TEXT)")

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _to_sql(table: str, col: str, value: Any) -> Any:
        if value is None:
            return None
        if col in JSON_COLUMNS.get(table, ()):
            return dumps(value)
        if col in BOOL_COLUMNS.get(table, ()):
            return int(bool(value))
        return value

    def _apply_one(self, op: WriteOp) -> None:
        pk, _ = TABLES[op.table]
        if op.kind == "replace":
            keys = [r[pk] for r in op.rows]
            marks = ",".join("?" * len(keys))
            where = f'WHERE "{pk}" NOT IN ({marks})' if keys else ""
            self.conn.execute(f'DELETE FROM "{op.table}" {where}', keys)
        elif op.kind != "upsert":
            raise ValueError(f"unknown op {op.kind}")
        for row in op.rows:
            cols = list(row)
            names = ", ".join(f'"{c}"' for c in cols)
            updates = ", ".join(f'"{c}"=excluded."{c}"' for c in cols if c != pk) or f'"{pk}"=excluded."{pk}"'
            self.conn.execute(
                f'INSERT INTO "{op.table}" ({names}) VALUES ({",".join("?" * len(cols))}) '
                f'ON CONFLICT("{pk}") DO UPDATE SET {updates}',
                [self._to_sql(op.table, c, row[c]) for c in cols],
            )

    def apply(self, ops: list[WriteOp], queue_for_sync: bool = False) -> None:
        """Apply all ops in one transaction (and queue them for Supabase if asked)."""
        with self.conn:
            for op in ops:
                self._apply_one(op)
            if queue_for_sync and ops:
                self.conn.execute("INSERT INTO outbox (created_at, payload) VALUES (?, ?)",
                                  (dt.datetime.now(dt.timezone.utc).isoformat(),
                                   json.dumps([op.to_json() for op in ops])))

    def outbox(self) -> list[tuple[int, list[WriteOp]]]:
        rows = self.conn.execute("SELECT id, payload FROM outbox ORDER BY id").fetchall()
        return [(r["id"], [WriteOp.from_json(o) for o in json.loads(r["payload"])]) for r in rows]

    def remove_from_outbox(self, outbox_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))

    def latest_state(self, before: dt.date | None = None) -> SavedState | None:
        sql = "SELECT as_of_date, state_json FROM daily_snapshots"
        args: list[Any] = []
        if before:
            sql += " WHERE as_of_date < ?"
            args.append(before.isoformat())
        row = self.conn.execute(sql + " ORDER BY as_of_date DESC LIMIT 1", args).fetchone()
        if row is None or row["state_json"] is None:
            return None
        return dt.date.fromisoformat(row["as_of_date"]), json.loads(row["state_json"])

    def rows(self, table: str, order_by: str | None = None) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise KeyError(table)
        sql = f'SELECT * FROM "{table}"' + (f' ORDER BY "{order_by}"' if order_by else "")
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def count(self, table: str) -> int:
        if table not in TABLES and table != "outbox":
            raise KeyError(table)
        return self.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
