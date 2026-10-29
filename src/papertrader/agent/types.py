"""Types shared by every desk."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Finding status, from harmless to blocking. "unknown" = the data could not be read.
OK, INFO, WARN, BLOCK, UNKNOWN = "ok", "info", "warn", "block", "unknown"
STATUSES = (OK, INFO, WARN, BLOCK, UNKNOWN)


@dataclass
class Finding:
    """One desk's view of one stock, in plain words."""
    desk: str                      # e.g. "news", "events", "insider", "odds"
    status: str                    # one of STATUSES
    summary: str                   # one sentence a person can read
    data: dict[str, Any] = field(default_factory=dict)  # JSON-safe details

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"bad finding status {self.status!r}")

    @property
    def blocks(self) -> bool:
        return self.status == BLOCK

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def unknown(desk: str, why: str) -> Finding:
    return Finding(desk, UNKNOWN, f"{desk} data unavailable: {why}")


def symbol(ticker: str) -> str:
    """Yahoo ticker -> NSE symbol: 'INFY.NS' -> 'INFY', 'M&M.NS' -> 'M&M'."""
    return ticker.upper().removesuffix(".NS").removesuffix(".BO")


def yahoo(sym: str) -> str:
    """NSE symbol -> Yahoo ticker ('INFY' -> 'INFY.NS'); indices and suffixed tickers unchanged."""
    s = sym.strip().upper()
    return s if s.startswith("^") or "." in s else f"{s}.NS"
