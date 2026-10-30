"""Supabase (Postgres) store. The bot writes with the service-role key from .env."""
from __future__ import annotations

import datetime as dt
from typing import Any

from papertrader.storage.schema import TABLES, WriteOp

SavedState = tuple[dt.date, dict[str, Any]]
CHUNK = 500


class SupabaseStore:
    def __init__(self, url: str, key: str, timeout: int = 20):
        from supabase import create_client

        try:
            from supabase import ClientOptions
        except ImportError:  # older supabase-py
            from supabase.lib.client_options import ClientOptions

        self.client = create_client(url, key, options=ClientOptions(postgrest_client_timeout=timeout))

    def apply(self, ops: list[WriteOp]) -> None:
        for op in ops:
            pk, _ = TABLES[op.table]
            table = self.client.table(op.table)
            if op.kind == "replace":
                keep = {r[pk] for r in op.rows}
                existing = table.select(pk).execute().data or []
                for row in existing:
                    if row[pk] not in keep:
                        self.client.table(op.table).delete().eq(pk, row[pk]).execute()
            elif op.kind == "delete_eq":
                for f in op.rows:
                    self.client.table(op.table).delete().eq(f["column"], f["value"]).execute()
                continue
            elif op.kind != "upsert":
                raise ValueError(f"unknown op {op.kind}")
            for i in range(0, len(op.rows), CHUNK):
                self.client.table(op.table).upsert(op.rows[i:i + CHUNK], on_conflict=pk).execute()

    def running_since(self, since_iso: str) -> list[dict[str, Any]]:
        q = self.client.table("runs").select("run_id,host,started_at").eq("status", "running")
        return q.gte("started_at", since_iso).execute().data or []

    def latest_state(self, before: dt.date | None = None) -> SavedState | None:
        q = self.client.table("daily_snapshots").select("as_of_date,state_json")
        if before:
            q = q.lt("as_of_date", before.isoformat())
        data = q.order("as_of_date", desc=True).limit(1).execute().data
        if not data or not data[0].get("state_json"):
            return None
        return dt.date.fromisoformat(str(data[0]["as_of_date"])[:10]), data[0]["state_json"]
