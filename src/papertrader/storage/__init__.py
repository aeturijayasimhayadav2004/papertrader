"""Storage facade: SQLite always, Supabase when configured, with an outbox in between.

Reading state: the newest snapshot from either store wins. If Supabase is
configured but unreachable and there is no local state either, we refuse to run
rather than start trading from a blank portfolio.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Protocol

from papertrader.config import PROJECT_ROOT, Config
from papertrader.storage.schema import WriteOp, replace, upsert
from papertrader.storage.sqlite_store import SavedState, SQLiteStore

log = logging.getLogger(__name__)


class StateUnavailableError(RuntimeError):
    pass


class RemoteStore(Protocol):
    def apply(self, ops: list[WriteOp]) -> None: ...
    def latest_state(self, before: dt.date | None = None) -> SavedState | None: ...


class Store:
    def __init__(self, sqlite: SQLiteStore, remote: RemoteStore | None = None, remote_error: str | None = None):
        self.sqlite = sqlite
        self.remote = remote
        self.remote_error = remote_error  # set when Supabase was wanted but could not be created
        self._sync_failed = False

    @classmethod
    def from_config(cls, cfg: Config) -> "Store":
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
        sqlite = SQLiteStore(cfg.storage.sqlite_path)
        if not cfg.storage.use_supabase:
            log.info("Supabase disabled in config: using local SQLite only (%s).", cfg.storage.sqlite_path)
            return cls(sqlite)
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            msg = "SUPABASE_URL / SUPABASE_SERVICE_KEY not set"
            if cfg.storage.require_supabase:
                raise StateUnavailableError(msg + " and Supabase is required.")
            log.warning("%s: using local SQLite only.", msg)
            return cls(sqlite)
        try:
            from papertrader.storage.supabase_store import SupabaseStore

            return cls(sqlite, SupabaseStore(url, key))
        except Exception as exc:
            log.warning("Could not create Supabase client (%s): using local SQLite only for now.", exc)
            if cfg.storage.require_supabase:
                raise StateUnavailableError(f"Supabase client error: {exc}") from exc
            return cls(sqlite, None, remote_error=str(exc))

    @property
    def remote_wanted(self) -> bool:
        return self.remote is not None or self.remote_error is not None

    def sync(self, force: bool = False) -> str | None:
        """Replay queued writes to Supabase in order. Returns an error message or None."""
        if self.remote is None:
            return self.remote_error
        if self._sync_failed and not force:
            return "skipped after an earlier failure"
        for outbox_id, ops in self.sqlite.outbox():
            try:
                self.remote.apply(ops)
            except Exception as exc:
                self._sync_failed = True
                log.warning("Supabase sync failed, will retry on the next run: %s", exc)
                return str(exc)
            self.sqlite.remove_from_outbox(outbox_id)
        self._sync_failed = False
        return None

    def write(self, ops: list[WriteOp]) -> None:
        self.sqlite.apply(ops, queue_for_sync=self.remote_wanted)
        self.sync()

    def pending_sync(self) -> int:
        return self.sqlite.count("outbox")

    def load_latest_state(self, before: dt.date | None = None) -> SavedState | None:
        remote_state, remote_err = None, None
        if self.remote is not None:
            self.sync(force=True)
            try:
                remote_state = self.remote.latest_state(before)
            except Exception as exc:
                remote_err = str(exc)
                log.warning("Could not read state from Supabase: %s", exc)
        elif self.remote_error:
            remote_err = self.remote_error
        local_state = self.sqlite.latest_state(before)

        if remote_err and local_state is None:
            raise StateUnavailableError(
                f"Supabase is unreachable ({remote_err}) and there is no local state. Refusing to trade "
                "rather than start from an empty portfolio."
            )
        candidates = [s for s in (remote_state, local_state) if s is not None]
        return max(candidates, key=lambda s: s[0]) if candidates else None


__all__ = ["Store", "SQLiteStore", "StateUnavailableError", "WriteOp", "upsert", "replace", "SavedState"]
