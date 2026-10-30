"""PaperTrader dashboard (read-only). PAPER TRADING - NO REAL MONEY.

Run locally:  cd dashboard && streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
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
        st.error(f"New buys blocked: {last.get('halt_reason')}")

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
    st.caption("Costs included (itemised NSE charges + slippage). A strategy trades only if it beat "
               "equal-weight buy-and-hold out-of-sample (and, in walk-forward mode, in most yearly folds). The "
               "out-of-sample period was used to pick strategies, so live paper results are the only truly "
               "unseen test.")

    metrics = [m for m in ["total_return", "cagr", "max_drawdown", "sharpe", "sortino", "calmar", "profit_factor",
                           "win_rate", "n_trades"] if m in bt.columns and bt[m].notna().any()]
    wide = bt.pivot(index="strategy", columns="period", values=metrics)
    wide.columns = [f"{m} ({'IS' if p == 'in_sample' else 'OOS'})" for m, p in wide.columns]
    wide = wide[[f"{m} ({p})" for m in metrics for p in ("IS", "OOS")]]
    flag_cols = [c for c in ["fold_win_rate", "qualified", "overfit_flag", "rank"] if c in bt.columns]
    flags = bt[bt["period"] == "out_of_sample"].set_index("strategy")[flag_cols]
    table = wide.join(flags).reset_index()
    fmt = {c: st.column_config.NumberColumn(c, format="percent") for c in table.columns
           if c.split(" ")[0] in ("total_return", "cagr", "max_drawdown", "win_rate", "fold_win_rate")}
    fmt.update({c: st.column_config.NumberColumn(c, format="%.2f") for c in table.columns
                if c.split(" ")[0] in ("sharpe", "sortino", "calmar", "profit_factor")})
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


def agent() -> None:
    st.subheader("Trading agent")
    st.markdown("Before any buy, the agent's desks check the stock: **events** (results board meeting "
                "within days), **news** (fraud, regulator action, downgrades), **insider** (promoter selling, "
                "bulk / block deals) and **odds** (how this strategy's past signals on this stock turned out). "
                "A BLOCK verdict skips the buy. PAPER MONEY ONLY.")
    try:
        briefs = db.load("agent_briefs", order="as_of_date", desc=True, limit=200)
        scans = db.load("agent_scans", order="as_of_date", desc=True, limit=300)
    except Exception as exc:  # tables not created yet (run supabase/schema.sql)
        st.warning(f"Agent tables not available yet: {exc}")
        return

    st.markdown("#### Briefs")
    if briefs.empty:
        st.info("No briefs yet: the agent writes one when a strategy says BUY.")
    else:
        icon = {"GO": "🟢", "CAUTION": "🟠", "BLOCK": "🔴"}
        for _, b in briefs.head(30).iterrows():
            findings = b.get("findings")
            if isinstance(findings, str):
                findings = json.loads(findings or "[]")
            head = f"{icon.get(b['verdict'], '')} **{b['ticker']}** ({b['strategy']}) - {b['verdict']} - {b['as_of_date']}"
            with st.expander(head, expanded=b["as_of_date"] == briefs["as_of_date"].iloc[0]):
                st.write(b["summary"])
                if findings:
                    st.dataframe(pd.DataFrame(findings)[["desk", "status", "summary"]], hide_index=True,
                                 width="stretch",
                                 column_config={"summary": st.column_config.TextColumn("Finding", width="large")})

    st.markdown("#### Chart scan (information only)")
    if scans.empty:
        st.info("No scan yet.")
        return
    latest = scans["as_of_date"].iloc[0]
    view = scans[scans["as_of_date"] == latest].sort_values("score", ascending=False)
    st.caption(f"Setups found at the close of {latest}. A setup is a place to look, not an order.")
    st.dataframe(view[["ticker", "setup", "close_price", "score", "detail"]], hide_index=True, width="stretch",
                 column_config={"close_price": st.column_config.NumberColumn("Close", format="%.2f"),
                                "score": st.column_config.NumberColumn("Score", format="%.2f"),
                                "detail": st.column_config.TextColumn("Why", width="large")})


def learning() -> None:
    st.subheader("Learning from results")
    st.markdown("Does the agent help? For every brief and scan setup the bot measures what the stock did "
                "afterwards (1, 5 and 20 trading days, against the average stock). The history rows replay the "
                "same rules over the last 5 years. **Information only:** nothing here changes the settings, and a "
                "row with fewer than 20 cases is too few to judge.")
    try:
        sc = db.load("agent_scorecard")
    except Exception as exc:
        st.warning(f"Learning tables not available yet: {exc}")
        return
    if sc.empty:
        st.info("No scorecards yet: they appear after the next daily run.")
        return
    st.caption(f"Computed {sc['as_of_date'].max()}.")
    pct = {c: st.column_config.NumberColumn(c, format="%.2f%%") for c in ("mean_ret_pct", "median_ret_pct",
                                                                            "mean_excess_pct")}
    pct["hit_rate"] = st.column_config.NumberColumn("hit_rate", format="percent")
    pct["note"] = st.column_config.TextColumn("note", width="large")
    sections = [("setup_history", "Scan setups, last 5 years (excess = vs the average stock that day)"),
                ("odds_history", "Odds desk, last 5 years: trades it would have blocked vs allowed"),
                ("verdict", "Live briefs by verdict"), ("desk", "Live briefs by desk finding"),
                ("brain", "Claude's reviews"), ("setup", "Live scan setups")]
    for kind, title in sections:
        part = sc[sc["kind"] == kind]
        if part.empty:
            continue
        st.markdown(f"#### {title}")
        cols = [c for c in ("key", "status", "horizon_days", "n", "mean_ret_pct", "median_ret_pct", "hit_rate",
                            "mean_excess_pct", "note") if c in part]
        st.dataframe(part.sort_values(["key", "status", "horizon_days"])[cols], hide_index=True, width="stretch",
                     column_config=pct)


def intraday() -> None:
    st.subheader("Intraday paper trading (always-on server)")
    st.markdown("Runs every 5 minutes during market hours on the always-on server: opening-range breakouts, "
                "planned stop and target, odds check, size, risk check, simulated fill, out by 15:15 IST. "
                "Separate Rs 10 lakh paper account. PAPER MONEY ONLY.")
    try:
        j = db.load("agent_intraday", order="ts", desc=True, limit=500)
    except Exception as exc:
        st.warning(f"Intraday table not available yet: {exc}")
        return
    if j.empty:
        st.info("No intraday activity yet: it starts once the server is set up (docs/SERVER.md).")
        return
    days = sorted(j["day"].dropna().unique(), reverse=True)
    day = st.selectbox("Day", days)
    view = j[j["day"] == day]
    c = st.columns(3)
    c[0].metric("Entries", int((view["event"] == "entry").sum()))
    c[1].metric("Exits", int(view["event"].isin(["exit", "flatten"]).sum()))
    c[2].metric("Realised P&L", inr(view["pnl"].fillna(0).sum()))
    st.dataframe(view[["ts", "ticker", "event", "price", "qty", "pnl", "reason"]], hide_index=True, width="stretch",
                 column_config={"reason": st.column_config.TextColumn("Reason", width="large")})


def rotation() -> None:
    st.subheader("Rotation strategy (separate paper account)")
    st.markdown("Each month-end the strategy ranks the Nifty 100 by 12-month return (skipping the last month), "
                "holds the top 10 in equal weights, and goes to cash while Nifty is below its 200-day average. "
                "Orders fill at the next open with NSE charges. It passed the out-of-sample test on the Nifty 100 "
                "but not the Nifty 50 (see reports/rotation_research.md), so this is a live paper trial. "
                "PAPER MONEY ONLY.")
    try:
        rs = db.load("rotation_snapshots", order="as_of_date")
    except Exception as exc:
        st.warning(f"Rotation table not available yet: {exc}")
        return
    if rs.empty:
        st.info("No rotation data yet: it starts on the next daily run.")
        return
    rs["as_of_date"] = pd.to_datetime(rs["as_of_date"])
    last, first = rs.iloc[-1], rs.iloc[0]
    c = st.columns(4)
    c[0].metric("Equity", inr(last["equity"]))
    c[1].metric("Return since start", pct(last["equity"] / first["equity"] - 1))
    if pd.notna(last.get("benchmark_close")) and pd.notna(first.get("benchmark_close")):
        c[2].metric("Nifty 50 same period", pct(last["benchmark_close"] / first["benchmark_close"] - 1))
    c[3].metric("Holdings", int(last["n_holdings"] or 0))
    if len(rs) > 1:
        fig = go.Figure()
        fig.add_scatter(x=rs["as_of_date"], y=rs["equity"] / rs["equity"].iloc[0] * 100, name="Rotation")
        if rs["benchmark_close"].notna().all():
            fig.add_scatter(x=rs["as_of_date"], y=rs["benchmark_close"] / rs["benchmark_close"].iloc[0] * 100,
                            name="Nifty 50")
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Start = 100")
        st.plotly_chart(fig, width="stretch")
    holdings = last.get("holdings")
    if isinstance(holdings, str):
        holdings = json.loads(holdings or "[]")
    if holdings:
        st.markdown("#### Holdings")
        st.dataframe(pd.DataFrame(holdings), hide_index=True, width="stretch",
                     column_config={"weight": st.column_config.NumberColumn("Weight", format="percent"),
                                    "reason": st.column_config.TextColumn("Why", width="large")})
    pending = last.get("pending")
    if isinstance(pending, str):
        pending = json.loads(pending or "[]")
    if pending:
        st.markdown("#### Orders for the next open")
        st.dataframe(pd.DataFrame(pending, columns=["ticker", "side", "qty"]), hide_index=True)
    if last.get("note"):
        st.info(last["note"])


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
tabs = st.tabs(["Overview", "Today", "Agent", "Rotation", "Intraday", "Learning", "Positions",
                "Trade log", "Strategies", "Health"])
with tabs[0]:
    overview(snaps_df, trades_df)
with tabs[1]:
    today(snaps_df)
with tabs[2]:
    agent()
with tabs[3]:
    rotation()
with tabs[4]:
    intraday()
with tabs[5]:
    learning()
with tabs[6]:
    positions()
with tabs[7]:
    trade_log(trades_df)
with tabs[8]:
    strategies()
with tabs[9]:
    health(snaps_df)
