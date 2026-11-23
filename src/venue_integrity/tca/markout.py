"""Point-in-time quote lookup and post-trade markouts (spec §8.1, §8.3).

Lookups are strictly as-of: the quote used for time ``t`` is the last quote
stamped at or before ``t``. A quote older than ``max_staleness`` is STALE and
excluded; a horizon that runs past the end of the covered trading window is
INCOMPLETE. Neither is ever treated as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np


class QuoteStatus(StrEnum):
    OK = "OK"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class QuoteLookup:
    status: QuoteStatus
    bid: float | None = None
    ask: float | None = None
    quote_ts_ns: int | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


@dataclass
class QuoteSeries:
    """Consolidated (best bid / best offer) quotes for one instrument."""

    ts_ns: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    windows: list[tuple[int, int]] = field(default_factory=list)  # covered [start, end] trading windows

    def __post_init__(self) -> None:
        self.ts_ns = np.asarray(self.ts_ns, dtype=np.int64)
        self.bid = np.asarray(self.bid, dtype=float)
        self.ask = np.asarray(self.ask, dtype=float)
        if not (len(self.ts_ns) == len(self.bid) == len(self.ask)):
            raise ValueError("quote arrays must have equal length")
        if len(self.ts_ns) > 1 and np.any(np.diff(self.ts_ns) < 0):
            raise ValueError("quote timestamps must be non-decreasing")
        if not self.windows and len(self.ts_ns):
            self.windows = [(int(self.ts_ns[0]), int(self.ts_ns[-1]))]

    def window_end(self, t_ns: int) -> int | None:
        for start, end in self.windows:
            if start <= t_ns <= end:
                return end
        return None

    def asof(self, t_ns: int, max_staleness_ns: int) -> QuoteLookup:
        if len(self.ts_ns) == 0:
            return QuoteLookup(QuoteStatus.NO_DATA)
        idx = int(np.searchsorted(self.ts_ns, t_ns, side="right")) - 1
        if idx < 0:
            return QuoteLookup(QuoteStatus.NO_DATA)
        bid, ask, ts = float(self.bid[idx]), float(self.ask[idx]), int(self.ts_ns[idx])
        if not (np.isfinite(bid) and np.isfinite(ask)) or bid <= 0 or ask <= 0 or ask < bid:
            return QuoteLookup(QuoteStatus.NO_DATA, quote_ts_ns=ts)
        if t_ns - ts > max_staleness_ns:
            return QuoteLookup(QuoteStatus.STALE, bid, ask, ts)
        return QuoteLookup(QuoteStatus.OK, bid, ask, ts)

    def markout(self, t_ns: int, horizon_ns: int, max_staleness_ns: int) -> QuoteLookup:
        end = self.window_end(t_ns)
        if end is None or t_ns + horizon_ns > end:
            return QuoteLookup(QuoteStatus.INCOMPLETE)
        return self.asof(t_ns + horizon_ns, max_staleness_ns)


@dataclass
class MarketData:
    """Market data for an audit run.

    ``consolidated`` holds per-instrument consolidated quotes (TCA reference).
    ``venue_grid`` holds per-venue midpoints sampled on a common grid for the
    graph engine; ``grid_session`` labels each grid point with its session so
    lagged relationships never span a session boundary.
    """

    consolidated: dict[str, QuoteSeries]
    grid_ts_ns: np.ndarray
    venue_mids: dict[str, np.ndarray]
    grid_session: np.ndarray
    grid_tick_ns: int
    feed_latency_ns: dict[str, int] = field(default_factory=dict)
    ack_latency_ms: dict[str, list[float]] = field(default_factory=dict)
