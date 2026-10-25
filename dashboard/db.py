"""Read-only data access for the dashboard.

Production: Supabase with the anon / publishable key from Streamlit secrets
(row-level security only allows SELECT). For local testing without Supabase,
set PAPERTRADER_DASHBOARD_SQLITE=path/to/papertrader.db to read the bot's SQLite file.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pandas as pd
import streamlit as st

PAGE = 1000
SQLITE_PATH = os.environ.get("PAPERTRADER_DASHBOARD_SQLITE")

SNAPSHOT_COLUMNS = ("as_of_date,cash,positions_value,equity,benchmark_close,daily_pnl_pct,open_positions,"
                    "max_open_positions,max_daily_loss_pct,trading_halted,halt_reason,selected_strategies,"
                    "selection_note,updated_at")


def source_label() -> str:
    return f"local SQLite ({SQLITE_PATH})" if SQLITE_PATH else "Supabase (read-only key)"


@st.cache_resource
def _client():
    from supabase import create_client

    conf = st.secrets["supabase"]
    return create_client(conf["url"], conf["anon_key"])


def _sqlite(table: str, columns: str, where: tuple[str, str] | None, order: str | None,
            desc: bool, limit: int | None) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    try:
        sql = f'SELECT {columns} FROM "{table}"'
        args: list = []
        if where:
            sql += f' WHERE "{where[0]}" = ?'
            args.append(where[1])
        if order:
            sql += f' ORDER BY "{order}" {"DESC" if desc else "ASC"}'
        if limit:
            sql += f" LIMIT {int(limit)}"
        df = pd.read_sql_query(sql, conn, params=args)
    finally:
        conn.close()
    if "selected_strategies" in df:
        df["selected_strategies"] = df["selected_strategies"].map(lambda v: json.loads(v) if v else [])
    for col in ("trading_halted", "qualified", "overfit_flag"):
        if col in df:
            df[col] = df[col].map(lambda v: None if v is None or pd.isna(v) else bool(v))
    return df


@st.cache_data(ttl=300, show_spinner="Loading data...")
def load(table: str, columns: str = "*", where: tuple[str, str] | None = None, order: str | None = None,
         desc: bool = False, limit: int | None = None) -> pd.DataFrame:
    if SQLITE_PATH:
        return _sqlite(table, columns, where, order, desc, limit)
    rows: list[dict] = []
    start = 0
    while True:
        q = _client().table(table).select(columns)
        if where:
            q = q.eq(where[0], where[1])
        if order:
            q = q.order(order, desc=desc)
        end = start + PAGE - 1
        if limit:
            end = min(end, limit - 1)
        batch = q.range(start, end).execute().data or []
        rows.extend(batch)
        if len(batch) < end - start + 1 or (limit and len(rows) >= limit):
            break
        start += PAGE
    return pd.DataFrame(rows)


def snapshots() -> pd.DataFrame:
    df = load("daily_snapshots", SNAPSHOT_COLUMNS, order="as_of_date")
    if not df.empty:
        df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    return df
