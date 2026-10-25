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
