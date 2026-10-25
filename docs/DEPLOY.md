# Deployment guide

PAPER TRADING ONLY. No broker account, no real money, no broker API keys.

Three pieces:

| Piece | Where | Key it uses |
|---|---|---|
| Database | Supabase (free tier) | - |
| Bot (writes) | GitHub Actions, weekdays 16:30 IST | `service_role` / **secret** key (GitHub secret) |
| Dashboard (reads) | Streamlit Community Cloud | `anon` / **publishable** key (Streamlit secret) |

---

## 1. Supabase

1. Create a project at <https://supabase.com> (region: Mumbai `ap-south-1` is closest).
2. **SQL Editor → New query**: paste `supabase/schema.sql`, Run.
3. New query: paste `supabase/rls.sql`, Run. Verify:
   ```sql
   select tablename, policyname, cmd, roles from pg_policies where schemaname = 'public';
   ```
   Expect 7 rows, all `SELECT`.
4. **Project Settings → API (or API Keys)**, note:
   - Project URL → `SUPABASE_URL`
   - `service_role` key (newer projects: a **secret** key, `sb_secret_...`) → bot only
   - `anon` key (newer projects: **publishable** key, `sb_publishable_...`) → dashboard only

## 2. Bot locally (first run + RLS check)

```powershell
copy .env.example .env      # then edit: URL, service key, anon key
.venv\Scripts\activate
python scripts/run_daily.py
python scripts/check_rls.py # must end with "Dashboard key is READ-ONLY."
```

The first run with Supabase configured copies state to Supabase. If you already ran the bot
locally without Supabase, its existing local SQLite state is picked up and synced on the next write.
In Supabase **Table Editor** you should now see rows in `runs`, `decisions`, `daily_snapshots`
and `backtest_results`.

## 3. GitHub repository

```powershell
git init
git add .
git status                  # check: NO .env, NO data/, NO secrets.toml in the list
git commit -m "PaperTrader"
```

Create a **private** repo on GitHub and push (`git remote add origin ...; git push -u origin main`).

## 4. Dashboard on Streamlit Community Cloud

1. <https://share.streamlit.io> → sign in with GitHub → **Create app → Deploy a public app from GitHub**.
2. Repository: your repo. Branch: `main`. **Main file path: `dashboard/app.py`**.
   (Streamlit uses `dashboard/requirements.txt`, the one next to the entry file.)
3. **Advanced settings**:
   - Python version: 3.11
   - Secrets: paste (anon/publishable key, NOT the service key):
     ```toml
     dashboard_password = "a-long-random-password"

     [supabase]
     url = "https://your-project-ref.supabase.co"
     anon_key = "your-anon-or-publishable-key"
     ```
4. **Deploy**. Open the URL, enter the password, check every tab.
5. Optional: in app **Settings → Sharing**, restrict who can view the app (on top of the password).

Local dashboard test:
```powershell
copy dashboard\.streamlit\secrets.toml.example dashboard\.streamlit\secrets.toml   # edit it
cd dashboard; streamlit run app.py
```
Without Supabase you can point it at the bot's SQLite file instead:
```powershell
$env:PAPERTRADER_DASHBOARD_SQLITE = "..\data\papertrader.db"; cd dashboard; streamlit run app.py
```

## 5. Daily schedule (GitHub Actions)

1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (service_role / secret key)
   - optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (run summaries on your phone)
   - optional, under the **Variables** tab: `PAPERTRADER_KILL_SWITCH` = `1` stops new buys
2. **Actions** tab → enable workflows if asked → **daily-paper-trade → Run workflow** (manual test).
3. Green run → dashboard **Health** tab shows the new run time.

Schedule: `0 11 * * 1-5` = 16:30 IST Mon–Fri. GitHub may start cron jobs late (sometimes
15–60 min), which is harmless. Re-runs and double runs are safe.

**Note:** GitHub disables scheduled workflows in repos with no activity for 60 days. Any commit
re-enables them, or press "Enable workflow" in the Actions tab. The dashboard's Health tab warns
when the bot is more than 2 trading days behind.

### Alternative: Windows Task Scheduler (PC must be on)

```powershell
schtasks /Create /TN "PaperTrader daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:30 `
  /TR "\"C:\Users\jaien\Desktop\APPS\TradingBots\PaperTraderBot\.venv\Scripts\python.exe\" \"C:\Users\jaien\Desktop\APPS\TradingBots\PaperTraderBot\scripts\run_daily.py\""
```
Do not run both schedulers: two runners share Supabase state, and although re-runs are
safe, one place of truth is simpler.

## Security checklist

- [ ] `.env` and `dashboard/.streamlit/secrets.toml` are NOT in git (`git ls-files | findstr /i "env secrets"` shows only the `.example` files).
- [ ] Service key exists only in `.env` and GitHub Actions secrets.
- [ ] Dashboard secrets use the anon/publishable key.
- [ ] `python scripts/check_rls.py` reports READ-ONLY.
- [ ] If a key ever leaks: Supabase → API keys → rotate, then update `.env` / GitHub / Streamlit.
