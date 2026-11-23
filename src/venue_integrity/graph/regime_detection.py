"""Market-regime labelling from realised volatility (spec §7.3, §9).

Each session is labelled by where its realised volatility falls in the
distribution of session volatilities in the audit sample. Thresholds are
percentiles from settings, so labels are relative to the sample, which is
documented in every output.
"""

from __future__ import annotations

import numpy as np

from venue_integrity.schemas.enums import MarketRegime


def realised_volatility(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return float("nan")
    return float(np.sqrt(np.sum(r * r)))


def session_volatility(mids: np.ndarray, sessions: np.ndarray) -> dict[int, float]:
    from venue_integrity.graph.lead_lag import session_returns

    returns = session_returns(mids, sessions)
    out = {}
    for s in sorted(set(int(x) for x in np.unique(sessions))):
        out[s] = realised_volatility(returns[sessions == s])
    return out


def classify_sessions(vols: dict[int, float], *, high_percentile: float = 75, low_percentile: float = 25) -> dict[int, MarketRegime]:
    finite = np.array([v for v in vols.values() if np.isfinite(v)])
    if len(finite) == 0:
        return {s: MarketRegime.UNKNOWN for s in vols}
    hi = float(np.percentile(finite, high_percentile))
    lo = float(np.percentile(finite, low_percentile))
    out = {}
    for s, v in sorted(vols.items()):
        if not np.isfinite(v):
            out[s] = MarketRegime.UNKNOWN
        elif v > hi:
            out[s] = MarketRegime.HIGH_VOLATILITY
        elif v < lo:
            out[s] = MarketRegime.LOW_VOLATILITY
        else:
            out[s] = MarketRegime.NORMAL
    return out
