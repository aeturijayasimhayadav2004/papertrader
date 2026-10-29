-- Row-level security: the dashboard key (anon / publishable) can only READ.
-- The bot uses the service_role / secret key, which bypasses RLS.
-- Safe to re-run.

do $$
declare
    t text;
begin
    foreach t in array array['runs', 'decisions', 'orders', 'positions', 'trades',
                             'daily_snapshots', 'backtest_results', 'agent_briefs', 'agent_scans']
    loop
        execute format('alter table public.%I enable row level security', t);

        -- belt and braces: no write privileges at all for the public roles
        execute format('revoke insert, update, delete, truncate on public.%I from anon, authenticated', t);
        execute format('grant select on public.%I to anon, authenticated', t);

        execute format('drop policy if exists "read only" on public.%I', t);
        execute format('create policy "read only" on public.%I for select to anon, authenticated using (true)', t);
    end loop;
end $$;

-- Verify (should list one SELECT policy per table and nothing else):
-- select tablename, policyname, cmd, roles from pg_policies where schemaname = 'public' order by tablename;
