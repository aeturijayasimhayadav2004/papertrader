# Running the intraday paper bot on a free server (Oracle Cloud)

PAPER TRADING ONLY. The server runs simulated trades with simulated money. There is no broker
code in this project.

## Why a server

The daily bot runs on GitHub Actions, but GitHub's cron often starts hours late. Intraday
trading needs a cycle every 5 minutes between 09:15 and 15:30 IST, on time, so it needs a
computer that is always on. Oracle Cloud's **Always Free** tier gives you a small Linux VM
for ₹0, for as long as you use it. A reserved public IP is also free, and you will need a
fixed IP later if you ever connect a broker API (SEBI's 2026 rule ties API access to a
registered static IP).

What runs on the VM:

| What | When | How |
|---|---|---|
| `scripts/intraday_loop.py --once` | every 5 min, Mon-Fri 09:15-15:30 IST | `papertrader-intraday.timer` |
| `scripts/run_daily.py` (optional backup) | 16:45 IST Mon-Fri | `papertrader-daily.timer`, off by default |

Each intraday cycle: manage open paper positions (stop / target / flat by 15:15), then look
for opening-range breakouts in the Nifty 50, check news and results dates, run the time
machine, size the trade, run the risk check and, if everything passes, record a paper buy.
Every decision goes to `data/agent/intraday_journal.jsonl` (and the Supabase table
`agent_intraday` with `--sync`). After 15:30 a summary goes to Telegram.

## 1. Create the Oracle Cloud account (you do this)

1. Go to <https://www.oracle.com/cloud/free/> and click **Start for free**.
2. Use your real name and address. Oracle asks for a **credit or debit card** to prove you
   are a person. It places a small temporary hold (often ₹2 or about $1) that is
   reversed. Always Free resources are never charged. Some Indian debit cards are refused;
   a credit card usually works.
3. **Home region:** choose **India West (Mumbai) `ap-mumbai-1`** or **India South
   (Hyderabad) `ap-hyderabad-1`**. This cannot be changed later, and Always Free VMs can
   only be created in the home region. Mumbai is closest to NSE.
4. Wait for the "account is ready" email (minutes to a few hours).

## 2. Make an SSH key on Windows

Open **PowerShell** on your PC:

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\oracle_papertrader -C "papertrader"
```

Press Enter twice for no passphrase, or set one (safer). This makes two files in
`C:\Users\<you>\.ssh\`: `oracle_papertrader` (private: never share it) and
`oracle_papertrader.pub` (public: you upload this one). Show the public key with:

```powershell
Get-Content $HOME\.ssh\oracle_papertrader.pub
```

## 3. Create the VM

In the Oracle Cloud console: **Menu -> Compute -> Instances -> Create instance**.

1. **Name:** `papertrader`.
2. **Image:** click *Edit* -> *Change image* -> **Canonical Ubuntu 24.04** (22.04 also works).
3. **Shape:** click *Change shape*:
   * best: **Ampere -> VM.Standard.A1.Flex**, 1 OCPU and 6 GB memory (Always Free allows up
     to 4 OCPU / 24 GB in total), or
   * fallback: **Specialty and previous generation -> VM.Standard.E2.1.Micro** (1 GB RAM;
     slower, but enough; setup.sh adds swap).

   Both show an **"Always Free-eligible"** label. If you do not see that label, stop.
4. **"Out of host capacity"** for Ampere is common in Mumbai. Try another *Availability
   domain* / *Fault domain*, try 1 OCPU / 6 GB instead of more, try again early morning or
   late night, or use the E2.1.Micro shape for now. Upgrading the account to
   *Pay As You Go* usually removes the capacity error and Always Free resources stay ₹0,
   but then set the budget alert in step 10 before anything else.
5. **Networking:** keep the default new VCN and public subnet, and **Assign a public IPv4
   address**.
6. **Add SSH keys:** *Paste public keys* -> paste the `oracle_papertrader.pub` line.
7. **Boot volume:** default (about 47 GB) is fine and free (Always Free covers 200 GB).
8. Click **Create**. Wait until the state is **Running** and copy the **Public IP address**.

### Reserve the public IP (keep the same address forever)

The default IP is "ephemeral" and can change if the VM is stopped. Make it reserved:

1. **Instance -> Attached VNICs -> the VNIC -> IPv4 Addresses**.
2. On the primary IP, *Edit* -> **No public IP** -> Update; then *Edit* again ->
   **Reserved public IP** -> *Create a new reserved IP* (name `papertrader-ip`) -> Update.
3. Note the new address. Reserved IPs are free while attached to a running instance.

### Firewall: SSH only

The default security list allows inbound TCP 22 (SSH) only. Keep it that way. The bot only
makes **outbound** connections (Yahoo, Google News, NSE, Supabase, Telegram); it does not
need any inbound port. Do not open 80/443/8501: the dashboard stays on Streamlit Cloud.

## 4. Log in

From PowerShell (replace the IP):

```powershell
ssh -i $HOME\.ssh\oracle_papertrader ubuntu@<PUBLIC-IP>
```

Type `yes` the first time. You are now on the server as user `ubuntu`.

## 5. Install PaperTrader

```bash
curl -fsSL https://raw.githubusercontent.com/aeturijayasimhayadav2004/papertrader/main/deploy/oracle/setup.sh -o setup.sh
sudo bash setup.sh
```

It takes 5-15 minutes. It installs Python, creates a service user `papertrader` (no
password, no login), clones the code into `/opt/papertrader`, builds the virtual
environment, runs the tests and installs the systemd units **without** turning them on.

The server clock stays in **UTC**. That is intended: the timers say `Asia/Kolkata`
explicitly and the bot works in IST internally. Check with `timedatectl`.

## 6. Put the secrets in .env

```bash
sudo -u papertrader nano /opt/papertrader/.env
```

Fill in (see `deploy/oracle/env.example`):

* `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`: the same values as your GitHub secrets.
* `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: for trade alerts and the end-of-day summary.
* `ANTHROPIC_API_KEY`: optional.

Save with Ctrl+O, Enter, Ctrl+X. Then lock the file down (setup.sh already did, check anyway):

```bash
sudo chmod 600 /opt/papertrader/.env
sudo chown papertrader:papertrader /opt/papertrader/.env
ls -l /opt/papertrader/.env      # -rw------- 1 papertrader papertrader
```

## 7. Test with a dry run

A dry run works on a throwaway copy: nothing is written to the real ledger or journal and
Telegram messages are printed instead of sent.

```bash
cd /opt/papertrader
sudo -u papertrader .venv/bin/python scripts/intraday_loop.py --once --dry-run
# replay a past moment (only bars up to that time are used):
sudo -u papertrader .venv/bin/python scripts/intraday_loop.py --once --dry-run --now "2026-10-29 10:30"
# replay a whole past day quickly:
sudo -u papertrader .venv/bin/python scripts/intraday_loop.py --loop --dry-run --now "2026-10-29 09:20"
```

Outside market hours `--once` just says the market is closed: that is correct.

## 8. Turn on the timer

```bash
sudo systemctl enable --now papertrader-intraday.timer
systemctl list-timers 'papertrader-*'
```

It now runs every 5 minutes, Monday-Friday, 09:15:30-15:30:30 IST. On NSE holidays each run
is a no-op. Change settings in `config.yaml` under `intraday:` (watchlist, max loss per
trade, daily loss limit, trades per day, entry window...).

Optional backup of the daily run (GitHub Actions stays the main one; both are idempotent,
so whichever runs second does nothing):

```bash
sudo systemctl enable --now papertrader-daily.timer
```

To stop new paper entries immediately without touching the code:

```bash
echo "PAPERTRADER_KILL_SWITCH=1" | sudo -u papertrader tee -a /opt/papertrader/.env
```

## 9. Check that it is working

```bash
papertrader-status                                         # last cycle, open positions, today's P&L
journalctl -u papertrader-intraday.service -n 50 --no-pager # the last runs' output
journalctl -u papertrader-intraday.service --since today -f # follow live (Ctrl+C to stop)
tail -n 50 /opt/papertrader/logs/papertrader.log
tail -n 5 /opt/papertrader/data/agent/intraday_journal.jsonl
```

If `last cycle` is more than 10 minutes old during market hours, look at `journalctl`.

## 10. Updating

```bash
sudo bash /opt/papertrader/deploy/oracle/update.sh
```

It pulls the latest code, reinstalls requirements, runs the tests (and stops the intraday
timer if they fail), and reloads the systemd units.

## 11. Costs: should be ₹0

* Use only resources marked **Always Free-eligible**: A1.Flex up to 4 OCPU / 24 GB total, or
  2 x E2.1.Micro; up to 200 GB block storage; one reserved IP attached to a running VM.
* Set a budget alert anyway: **Menu -> Billing & Cost Management -> Budgets -> Create
  budget**, amount ₹100 (or $1), alert at 1%, your email. Any charge then emails you.
* Oracle may reclaim an Always Free VM that is idle (very low CPU, network and memory for 7
  days). A 5-minute bot is usually enough activity; if you get a warning email, upgrading
  to Pay As You Go stops reclamation (still ₹0 for Always Free shapes).

## 12. Security basics

* **Key-only SSH.** Ubuntu on Oracle already disables password logins. Check:
  `sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin'` should show
  `passwordauthentication no` and `permitrootlogin without-password` or `no`.
* **Automatic security updates** are on (setup.sh enables `unattended-upgrades`). Reboot now
  and then: `sudo reboot` (outside market hours).
* **Secrets** live only in `/opt/papertrader/.env` (mode 600). Never paste them in chat or
  commit them.
* **The service runs as `papertrader`**, a user with no password and no sudo, inside a
  read-only filesystem except `data/` and `logs/`.
* **fail2ban (optional)** slows down SSH password guessers:
  `sudo apt install -y fail2ban && sudo systemctl enable --now fail2ban`.
* Oracle Ubuntu images also ship an `iptables` firewall that only allows SSH inbound. Leave it.

## Testing on Windows first (before the VM exists)

`deploy/windows/register_task.ps1` registers a Task Scheduler task that runs
`intraday_loop.py --once` every 5 minutes, Mon-Fri 09:15-15:30 IST, while your PC is on:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -DryRun   # dry runs only
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1           # real paper trades
powershell -ExecutionPolicy Bypass -File deploy\windows\register_task.ps1 -Unregister
```

Do not run it on the PC and the VM at the same time with the same Supabase project: two
separate ledgers would both write journal rows.
