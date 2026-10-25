"""PaperTrader dashboard (read-only). PAPER TRADING - NO REAL MONEY.

Run locally:  cd dashboard && streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import hmac
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db

IST = ZoneInfo("Asia/Kolkata")
RUN_DEADLINE = dt.time(16, 30)  # by this IST time the bot should have processed today's bar
STALE_TRADING_DAYS = 2

st.set_page_config(page_title="PaperTrader - paper trading only", page_icon=":chart_with_upwards_trend:",
                   layout="wide")


def banner() -> None:
    st.markdown(
        """<div style="background:#b3261e;color:#fff;padding:14px 10px;border-radius:10px;text-align:center;
        font-size:clamp(20px,4vw,34px);font-weight:800;letter-spacing:1px;margin-bottom:8px">
        PAPER TRADING - NO REAL MONEY</div>""",
        unsafe_allow_html=True,
    )


def password_ok() -> bool:
    if st.session_state.get("auth_ok"):
        return True
    try:
        expected = st.secrets["dashboard_password"]
    except Exception:  # no secrets file / key (exception type varies across Streamlit versions)
        st.error("No `dashboard_password` in Streamlit secrets. See docs/DEPLOY.md.")
        return False
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if hmac.compare_digest(pw.encode(), str(expected).encode()):
            st.session_state["auth_ok"] = True
            st.rerun()
        st.error("Wrong password.")
    return False


def pct(v, signed=True) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    return f"{v:+.2%}" if signed else f"{v:.2%}"


def inr(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"Rs {v:,.0f}"


def to_ist(ts) -> str:
    if ts is None or pd.isna(ts):
        return "-"
    t = pd.to_datetime(ts, utc=True).tz_convert(IST)
    return t.strftime("%d %b %Y %H:%M IST")


def expected_latest_bar(now: dt.datetime) -> dt.date:
    """Most recent weekday whose bar the bot should already have processed (ignores NSE holidays)."""
    d = now.date()
    if now.time() < RUN_DEADLINE:
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# ---------------------------------------------------------------- sections
def overview(snaps: pd.DataFrame, trades: pd.DataFrame) -> None:
    st.subheader("Overview")
    if snaps.empty:
        st.info("No data yet. Run the bot once (scripts/run_daily.py).")
        return
    eq = snaps.set_index("as_of_date")["equity"].astype(float)
    start_equity = eq.iloc[0]
    bench = snaps.set_index("as_of_date")["benchmark_close"].astype(float).ffill()
    bench_eq = start_equity * bench / bench.dropna().iloc[0] if bench.notna().any() else None

    total = eq.iloc[-1] / start_equity - 1
    dd = float((eq / eq.cummax() - 1).min())
    bench_ret = bench_eq.iloc[-1] / start_equity - 1 if bench_eq is not None else None
    closed = len(trades)
    win = float((trades["net_pnl"] > 0).mean()) if closed else None

    c = st.columns(6)
    c[0].metric("Equity", inr(eq.iloc[-1]))
    c[1].metric("Total return", pct(total))
    c[2].metric("Nifty 50 same period", pct(bench_ret))
    c[3].metric("Max drawdown", pct(dd))
    c[4].metric("Win rate", pct(win, signed=False))
    c[5].metric("Closed trades", closed)

    fig = go.Figure()
    fig.add_scatter(x=eq.index, y=eq, name="Paper portfolio", line=dict(width=2.5))
    if bench_eq is not None:
        fig.add_scatter(x=bench_eq.index, y=bench_eq, name="Nifty 50 (same start value)",
                        line=dict(width=1.5, dash="dot"))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10), yaxis_title="Rs",
                      legend=dict(orientation="h", y=1.08), hovermode="x unified")
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Tracking since {eq.index[0].date()} ({len(eq)} trading days). "
               "Win rate counts closed trades with positive P&L after costs.")


def today(snaps: pd.DataFrame) -> None:
    st.subheader("Today's decisions")
    if snaps.empty:
        st.info("No decisions yet.")
        return
    last = snaps.iloc[-1]
    as_of = last["as_of_date"].date()
    st.markdown(f"Decisions made at the close of **{as_of:%A %d %b %Y}**. Orders fill at the next trading day's open.")
    st.info(last.get("selection_note") or "")
    if last.get("trading_halted"):
        st.error(f"Trading halted: {last.get('halt_reason')}")

    dec = db.load("decisions", where=("as_of_date", as_of.isoformat()))
    if dec.empty:
        st.warning("No decision rows for this day.")
        return
    counts = dec["action"].value_counts()
    cols = st.columns(5)
    for col, action in zip(cols, ["BUY", "SELL", "BLOCKED_BY_RISK", "NOT_SELECTED", "HOLD"]):
        col.metric(action.replace("_", " ").title(), int(counts.get(action, 0)))

    only_actions = st.toggle("Hide plain HOLD rows", value=False)
    view = dec[dec["action"] != "HOLD"] if only_actions else dec
    for ticker, rows in view.sort_values(["ticker", "strategy"]).groupby("ticker", sort=True):
        acts = ", ".join(sorted(set(rows["action"])))
        close = rows["close_price"].dropna()
        head = f"**{ticker}**" + (f" - close Rs {close.iloc[0]:,.2f}" if len(close) else "") + f" - {acts}"
        with st.expander(head, expanded=bool(set(rows["action"]) & {"BUY", "SELL", "BLOCKED_BY_RISK"})):
            st.dataframe(rows[["strategy", "signal", "action", "reason"]], hide_index=True,
                         width="stretch",
                         column_config={"reason": st.column_config.TextColumn("Reason", width="large")})


def positions() -> None:
    st.subheader("Open positions")
    pos = db.load("positions", order="ticker")
    if pos.empty:
        st.info("No open positions (all cash).")
        return
    pos["distance_to_stop"] = pos["last_price"] / pos["stop_price"] - 1
    c = st.columns(3)
    c[0].metric("Positions", len(pos))
    c[1].metric("Market value", inr(pos["market_value"].sum()))
    c[2].metric("Unrealised P&L", inr(pos["unrealized_pnl"].sum()))
    st.dataframe(
        pos[["ticker", "strategy", "qty", "entry_date", "entry_price", "last_price", "unrealized_pnl",
             "unrealized_pnl_pct", "stop_price", "distance_to_stop"]],
        hide_index=True, width="stretch",
        column_config={
            "entry_price": st.column_config.NumberColumn("Entry", format="%.2f"),
            "last_price": st.column_config.NumberColumn("Last close", format="%.2f"),
            "unrealized_pnl": st.column_config.NumberColumn("P&L (Rs)", format="%.0f"),
            "unrealized_pnl_pct": st.column_config.NumberColumn("P&L %", format="percent"),
            "stop_price": st.column_config.NumberColumn("Stop-loss", format="%.2f"),
            "distance_to_stop": st.column_config.NumberColumn("Above stop by", format="percent"),
        },
    )
    st.caption("P&L is after the entry cost; the exit cost is charged when the position is sold.")


def trade_log(trades: pd.DataFrame) -> None:
    st.subheader("Trade log")
    orders = db.load("orders", order="created_date", desc=True)
    tab_closed, tab_orders = st.tabs(["Closed trades", "All orders"])

    with tab_closed:
        if trades.empty:
            st.info("No closed trades yet.")
        else:
            t = trades.copy()
            t["exit_date"] = pd.to_datetime(t["exit_date"])
            f = st.columns(4)
            tick = f[0].multiselect("Stock", sorted(t["ticker"].unique()))
            strat = f[1].multiselect("Strategy", sorted(t["strategy"].dropna().unique()))
            why = f[2].multiselect("Exit reason", sorted(t["exit_reason"].dropna().unique()))
            lo, hi = t["exit_date"].min().date(), t["exit_date"].max().date()
            rng = f[3].date_input("Exit date range", (lo, hi), min_value=lo, max_value=hi)
            if tick:
                t = t[t["ticker"].isin(tick)]
            if strat:
                t = t[t["strategy"].isin(strat)]
            if why:
                t = t[t["exit_reason"].isin(why)]
            if isinstance(rng, tuple) and len(rng) == 2:
                t = t[(t["exit_date"].dt.date >= rng[0]) & (t["exit_date"].dt.date <= rng[1])]
            st.caption(f"{len(t)} trades, net P&L {inr(t['net_pnl'].sum())}")
            t["exit_date"] = t["exit_date"].dt.date
            st.dataframe(t.drop(columns=["trade_id"]), hide_index=True, width="stretch",
                         column_config={"return_pct": st.column_config.NumberColumn("Return", format="percent")})
            st.download_button("Download CSV", t.to_csv(index=False), "paper_trades.csv", "text/csv")

    with tab_orders:
        if orders.empty:
            st.info("No orders yet.")
        else:
            f = st.columns(2)
            tick = f[0].multiselect("Stock ", sorted(orders["ticker"].unique()))
            status = f[1].multiselect("Status", sorted(orders["status"].dropna().unique()))
            o = orders
            if tick:
                o = o[o["ticker"].isin(tick)]
            if status:
                o = o[o["status"].isin(status)]
            st.dataframe(o, hide_index=True, width="stretch",
                         column_config={"reason": st.column_config.TextColumn("Reason", width="large")})
            st.download_button("Download CSV", o.to_csv(index=False), "paper_orders.csv", "text/csv")


def strategies() -> None:
    st.subheader("Strategy backtests: in-sample vs out-of-sample")
    latest = db.load("backtest_results", "as_of_date", order="as_of_date", desc=True, limit=1)
    if latest.empty:
        st.info("No backtest results yet.")
        return
    as_of = str(latest["as_of_date"].iloc[0])[:10]
    bt = db.load("backtest_results", where=("as_of_date", as_of))
    periods = bt[bt["strategy"] != "buy_and_hold"].groupby("period")[["start_date", "end_date"]].first()
    st.markdown(f"Last re-ranked on **{as_of}**. " + " | ".join(
        f"{p.replace('_', '-')}: {r.start_date} to {r.end_date}" for p, r in periods.iterrows()))
    st.caption("Costs included (per side). A strategy trades only if it beat equal-weight buy-and-hold "
               "out-of-sample. The out-of-sample period was used to pick strategies, so live paper results "
               "are the only truly unseen test.")

    metrics = ["total_return", "cagr", "max_drawdown", "sharpe", "win_rate", "n_trades"]
    wide = bt.pivot(index="strategy", columns="period", values=metrics)
    wide.columns = [f"{m} ({'IS' if p == 'in_sample' else 'OOS'})" for m, p in wide.columns]
    wide = wide[[f"{m} ({p})" for m in metrics for p in ("IS", "OOS")]]
    flags = bt[bt["period"] == "out_of_sample"].set_index("strategy")[["qualified", "overfit_flag", "rank"]]
    table = wide.join(flags).reset_index()
    fmt = {c: st.column_config.NumberColumn(c, format="percent") for c in table.columns
           if c.split(" ")[0] in ("total_return", "cagr", "max_drawdown", "win_rate")}
    fmt.update({c: st.column_config.NumberColumn(c, format="%.2f") for c in table.columns if c.startswith("sharpe")})
    st.dataframe(table, hide_index=True, width="stretch", column_config=fmt)

    oos = bt[bt["period"] == "out_of_sample"]
    for _, r in oos[oos["overfit_flag"] == True].iterrows():  # noqa: E712
        st.warning(f"Possible overfitting - **{r['strategy']}**: {r['note']}")
    for _, r in oos[(oos["strategy"] != "buy_and_hold") & (oos["overfit_flag"] != True)].iterrows():  # noqa: E712
        st.caption(f"{r['strategy']}: {r['note']}")

    fig = go.Figure()
    for period, name in (("in_sample", "In-sample"), ("out_of_sample", "Out-of-sample")):
        part = bt[bt["period"] == period].sort_values("strategy")
        fig.add_bar(x=part["strategy"], y=part["total_return"], name=name)
    fig.update_layout(barmode="group", height=360, yaxis_tickformat=".0%", title="Total return net of costs",
                      margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, width="stretch")


def health(snaps: pd.DataFrame) -> None:
    st.subheader("Health")
    now = dt.datetime.now(IST)
    runs = db.load("runs", order="started_at", desc=True, limit=50)
    if runs.empty:
        st.error("The bot has never run.")
        return
    last = runs.iloc[0]
    c = st.columns(3)
    c[0].metric("Last run", to_ist(last["started_at"]))
    c[1].metric("Last run status", str(last["status"]).upper())
    last_bar = snaps["as_of_date"].max().date() if not snaps.empty else None
    c[2].metric("Latest trading day processed", str(last_bar or "-"))

    if last["status"] in ("failed", "aborted"):
        st.error(f"The last run {last['status']}: {last['message']}")
    elif last["status"] == "running":
        st.warning("The last run is still marked as running (it may have crashed).")
    if isinstance(last["message"], str) and "Supabase not updated" in last["message"]:
        st.warning(last["message"])

    if last_bar:
        expected = expected_latest_bar(now)
        missed = int(np.busday_count(last_bar + dt.timedelta(days=1), expected + dt.timedelta(days=1)))
        if missed > STALE_TRADING_DAYS:
            st.error(f"WARNING: the bot has not processed a trading day for {missed} trading days "
                     f"(last: {last_bar}, expected: {expected}). Check the GitHub Actions run.")
        elif missed > 0:
            st.warning(f"{missed} trading day(s) behind (last processed {last_bar}). Normal if today's run "
                       "hasn't happened yet or it was a market holiday.")
        else:
            st.success(f"Up to date: processed {last_bar}.")
        st.caption("Trading days here are weekdays; NSE holidays can show up as a missed day.")

    st.markdown("#### Risk limits")
    if snaps.empty:
        return
    s = snaps.iloc[-1]
    limit = float(s["max_daily_loss_pct"]) / 100
    day = float(s["daily_pnl_pct"] or 0)
    r = st.columns(3)
    r[0].metric("Day's P&L", pct(day), help=f"New buys stop for the day at -{limit:.0%}")
    r[0].progress(min(max(-day / limit, 0.0), 1.0), text=f"{max(-day, 0) / limit:.0%} of daily loss limit used")
    r[1].metric("Open positions", f"{int(s['open_positions'])} / {int(s['max_open_positions'])}")
    r[1].progress(min(int(s["open_positions"]) / int(s["max_open_positions"]), 1.0))
    r[2].metric("New buys", "HALTED" if s["trading_halted"] else "Allowed")
    if s["trading_halted"]:
        r[2].error(s["halt_reason"])

    with st.expander("Recent runs"):
        show = runs.copy()
        show["started_at"] = show["started_at"].map(to_ist)
        show["finished_at"] = show["finished_at"].map(to_ist)
        st.dataframe(show, hide_index=True, width="stretch")


# ---------------------------------------------------------------- main
banner()
if not password_ok():
    st.stop()

top = st.columns([4, 1])
top[0].caption(f"Data source: {db.source_label()}. Refreshes every 5 minutes.")
if top[1].button("Refresh now"):
    st.cache_data.clear()
    st.rerun()

snaps_df = db.snapshots()
trades_df = db.load("trades", order="exit_date")
tabs = st.tabs(["Overview", "Today", "Positions", "Trade log", "Strategies", "Health"])
with tabs[0]:
    overview(snaps_df, trades_df)
with tabs[1]:
    today(snaps_df)
with tabs[2]:
    positions()
with tabs[3]:
    trade_log(trades_df)
with tabs[4]:
    strategies()
with tabs[5]:
    health(snaps_df)
