-- PaperTrader schema. Run once in Supabase: SQL Editor -> paste -> Run.
-- Then run rls.sql. Column names must match src/papertrader/storage/schema.py.

create table if not exists public.runs (
    run_id text primary key,
    started_at timestamptz,
    finished_at timestamptz,
    status text,
    as_of_date date,
    days_processed integer,
    message text,
    host text
);

create table if not exists public.decisions (
    id text primary key,
    as_of_date date not null,
    ticker text not null,
    strategy text not null,
    signal text,
    action text,
    reason text,
    close_price double precision,
    created_at timestamptz
);
create index if not exists decisions_as_of_idx on public.decisions (as_of_date);

create table if not exists public.orders (
    order_id text primary key,
    created_date date not null,
    ticker text not null,
    side text not null,
    qty integer,
    strategy text,
    reason text,
    status text,
    fill_date date,
    fill_price double precision,
    fees double precision,
    note text
);

create table if not exists public.positions (
    ticker text primary key,
    qty integer,
    entry_date date,
    entry_price double precision,
    stop_price double precision,
    strategy text,
    last_price double precision,
    market_value double precision,
    unrealized_pnl double precision,
    unrealized_pnl_pct double precision,
    as_of_date date
);

create table if not exists public.trades (
    trade_id text primary key,
    ticker text not null,
    strategy text,
    entry_date date,
    entry_price double precision,
    exit_date date,
    exit_price double precision,
    qty integer,
    gross_pnl double precision,
    fees double precision,
    net_pnl double precision,
    return_pct double precision,
    exit_reason text
);

create table if not exists public.daily_snapshots (
    as_of_date date primary key,
    cash double precision,
    positions_value double precision,
    equity double precision,
    benchmark_close double precision,
    daily_pnl_pct double precision,
    open_positions integer,
    max_open_positions integer,
    max_daily_loss_pct double precision,
    trading_halted boolean,
    halt_reason text,
    selected_strategies jsonb,
    selection_note text,
    state_json jsonb,
    updated_at timestamptz
);

create table if not exists public.backtest_results (
    id text primary key,
    as_of_date date not null,
    strategy text not null,
    period text not null,
    start_date date,
    end_date date,
    total_return double precision,
    cagr double precision,
    max_drawdown double precision,
    sharpe double precision,
    sortino double precision,
    calmar double precision,
    profit_factor double precision,
    win_rate double precision,
    n_trades integer,
    exposure double precision,
    bh_return double precision,
    excess_return double precision,
    qualified boolean,
    overfit_flag boolean,
    rank integer,
    note text,
    fold_win_rate double precision
);
-- Upgrades for databases created before these columns existed (safe to re-run).
alter table public.backtest_results add column if not exists sortino double precision;
alter table public.backtest_results add column if not exists calmar double precision;
alter table public.backtest_results add column if not exists profit_factor double precision;
alter table public.backtest_results add column if not exists fold_win_rate double precision;
create index if not exists backtest_as_of_idx on public.backtest_results (as_of_date);

-- Trading agent: one brief per buy candidate (every desk's finding + the verdict).
create table if not exists public.agent_briefs (
    id text primary key,
    as_of_date date not null,
    ticker text not null,
    strategy text,
    verdict text,
    summary text,
    findings jsonb,
    created_at timestamptz
);
create index if not exists agent_briefs_as_of_idx on public.agent_briefs (as_of_date);

-- Trading agent: daily chart scan of the wider universe (Nifty 50 by default).
create table if not exists public.agent_scans (
    id text primary key,
    as_of_date date not null,
    ticker text not null,
    setup text not null,
    close_price double precision,
    score double precision,
    detail text,
    created_at timestamptz
);
create index if not exists agent_scans_as_of_idx on public.agent_scans (as_of_date);

-- Learning: what each brief / scan setup did afterwards (1, 5, 20 trading days).
create table if not exists public.agent_outcomes (
    id text primary key,
    brief_id text,
    kind text,
    as_of_date date,
    ticker text,
    strategy text,
    verdict text,
    horizon_days integer,
    ret_pct double precision,
    excess_pct double precision,
    mae_pct double precision,
    status text,
    desks jsonb,
    updated_at timestamptz
);
create index if not exists agent_outcomes_as_of_idx on public.agent_outcomes (as_of_date);

-- Learning: scorecards per desk / verdict / setup, plus history backfills.
create table if not exists public.agent_scorecard (
    id text primary key,
    as_of_date date,
    kind text,
    key text,
    status text,
    horizon_days integer,
    n integer,
    mean_ret_pct double precision,
    median_ret_pct double precision,
    hit_rate double precision,
    mean_excess_pct double precision,
    note text
);

-- Always-on server: intraday paper journal.
create table if not exists public.agent_intraday (
    id text primary key,
    ts timestamptz,
    day date,
    ticker text,
    event text,
    reason text,
    price double precision,
    qty integer,
    ticket_id text,
    pnl double precision,
    details jsonb
);
create index if not exists agent_intraday_day_idx on public.agent_intraday (day);

-- Rotation strategy paper account (separate Rs 10 lakh account), one row per trading day.
create table if not exists public.rotation_snapshots (
    as_of_date date primary key,
    strategy text,
    label text,
    cash double precision,
    holdings_value double precision,
    equity double precision,
    benchmark_close double precision,
    n_holdings integer,
    holdings jsonb,
    pending jsonb,
    fills jsonb,
    note text,
    state_json jsonb,
    updated_at timestamptz
);
