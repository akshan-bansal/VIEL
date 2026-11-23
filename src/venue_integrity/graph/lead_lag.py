"""Directed lead-lag estimation between venues (spec §7.1).

Inputs are venue midpoints sampled on a common grid. For every ordered pair
(i -> j) a Granger-style regression is estimated on *returns* (never price
levels):

    r_j,t = a + sum_k b_k r_j,t-k + sum_k c_k r_i,t-k + [sum_k d_k f_t-k + d_0 f_t] + e_t

* own lags of the target control for its autocorrelation and for any
  serially-correlated common component;
* an optional exogenous common-factor series ``f`` (e.g. index futures returns)
  is controlled for contemporaneously and with lags;
* per-venue feed latency is removed before estimation (a late feed otherwise
  looks like a follower);
* lags never cross a session boundary.

Edge construction (spec: W_ij = max(0, rho_ij^lead) * Q_ij):

    rho_ij       partial correlation between r_j,t and sum_k r_i,t-k given the controls
    rho_ij^lead  rho_ij - rho_ji                 (net directional predictive relationship)
    Q_ij         1 - p_ij^Bonferroni              (F-test of c = 0, corrected over all ordered pairs)

An edge i -> j is kept only if it is significant at the family-wise level,
rho_ij > 0 and rho_ij^lead exceeds ``min_net_lead`` (an effect-size floor that
stops near-symmetric pairs, e.g. two venues fed by the same leader, from
producing statistically significant but economically meaningless edges). Orientation convention: **i -> j means i leads j.**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class EdgeEstimate:
    source_venue: str
    target_venue: str
    partial_corr: float
    net_lead: float
    p_value: float
    p_value_adjusted: float
    f_statistic: float
    observations: int
    best_lag: int
    significant: bool

    @property
    def confidence(self) -> float:
        return float(min(1.0, max(0.0, 1.0 - self.p_value_adjusted)))

    @property
    def weight(self) -> float:
        return max(0.0, self.net_lead)

    def to_record(self) -> dict[str, Any]:
        return {"source_venue": self.source_venue, "target_venue": self.target_venue,
                "weight": self.weight, "confidence": self.confidence}

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_venue,
            "target": self.target_venue,
            "partial_corr": round(self.partial_corr, 6),
            "net_lead": round(self.net_lead, 6),
            "p_value_adjusted": float(f"{self.p_value_adjusted:.3e}"),
            "f_statistic": round(self.f_statistic, 3),
            "observations": self.observations,
            "best_lag": self.best_lag,
            "significant": self.significant,
            "weight": round(self.weight, 6),
            "confidence": round(self.confidence, 6),
        }


@dataclass
class LeadLagResult:
    venues: list[str]
    edges: list[EdgeEstimate]
    all_pairs: list[EdgeEstimate]
    excluded_venues: dict[str, str] = field(default_factory=dict)
    skipped_pairs: dict[str, str] = field(default_factory=dict)
    max_lag: int = 0
    significance: float = 0.0
    min_net_lead: float = 0.0
    latency_adjustment_ticks: dict[str, int] = field(default_factory=dict)
    common_factor_controlled: bool = False

    def edge_records(self) -> list[dict[str, Any]]:
        return [e.to_record() for e in self.edges]

    def to_dict(self) -> dict[str, Any]:
        return {
            "orientation": "edge i -> j means i's lagged returns predict j's (i leads j)",
            "venues": self.venues,
            "max_lag_ticks": self.max_lag,
            "family_wise_significance": self.significance,
            "min_net_lead": self.min_net_lead,
            "latency_adjustment_ticks": self.latency_adjustment_ticks,
            "common_factor_controlled": self.common_factor_controlled,
            "own_lag_controls": True,
            "edges": [e.to_dict() for e in self.edges],
            "all_pairs": [e.to_dict() for e in self.all_pairs],
            "excluded_venues": self.excluded_venues,
            "skipped_pairs": self.skipped_pairs,
        }


def shift_within_sessions(x: np.ndarray, shift: int, sessions: np.ndarray) -> np.ndarray:
    """out[t] = x[t - shift] when t and t - shift share a session, else NaN.

    ``shift > 0`` lags the series; ``shift < 0`` leads it.
    """
    x = np.asarray(x, dtype=float)
    out = np.full_like(x, np.nan)
    if shift == 0:
        return x.copy()
    n = len(x)
    if abs(shift) >= n:
        return out
    if shift > 0:
        same = sessions[shift:] == sessions[:-shift]
        out[shift:] = np.where(same, x[:-shift], np.nan)
    else:
        k = -shift
        same = sessions[:-k] == sessions[k:]
        out[:-k] = np.where(same, x[k:], np.nan)
    return out


def session_returns(mids: np.ndarray, sessions: np.ndarray) -> np.ndarray:
    mids = np.asarray(mids, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        logp = np.where(mids > 0, np.log(mids), np.nan)
    prev = shift_within_sessions(logp, 1, sessions)
    return logp - prev


def _residualize(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, float]:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    return resid, float(resid @ resid)


def _pair_test(y: np.ndarray, own: np.ndarray, src: np.ndarray, factor: np.ndarray | None, min_obs: int):
    blocks = [np.ones((len(y), 1)), own]
    if factor is not None:
        blocks.append(factor)
    controls = np.hstack(blocks)
    full = np.hstack([controls, src])
    rows = np.isfinite(y) & np.all(np.isfinite(full), axis=1)
    n = int(rows.sum())
    if n < min_obs:
        return None, n
    y_, c_, s_, f_ = y[rows], controls[rows], src[rows], full[rows]
    k = src.shape[1]
    p_full = f_.shape[1]
    _, rss_r = _residualize(y_, c_)
    beta_u, *_ = np.linalg.lstsq(f_, y_, rcond=None)
    resid_u = y_ - f_ @ beta_u
    rss_u = float(resid_u @ resid_u)
    dof = n - p_full
    if dof <= 0 or rss_u <= 0:
        return None, n
    f_stat = ((rss_r - rss_u) / k) / (rss_u / dof)
    p_value = float(stats.f.sf(f_stat, k, dof))
    # partial correlation of y with the aggregated source given controls
    agg = s_.sum(axis=1)
    ry, _ = _residualize(y_, c_)
    rs, _ = _residualize(agg, c_)
    denom = float(np.sqrt((ry @ ry) * (rs @ rs)))
    partial = float((ry @ rs) / denom) if denom > 0 else 0.0
    coefs = beta_u[-k:]
    best_lag = int(np.argmax(np.abs(coefs))) + 1
    return (partial, f_stat, p_value, best_lag), n


def estimate_lead_lag(
    venue_mids: dict[str, np.ndarray],
    sessions: np.ndarray,
    *,
    max_lag: int = 3,
    significance: float = 0.01,
    min_observations: int = 500,
    latency_adjustment_ticks: dict[str, int] | None = None,
    common_factor_returns: np.ndarray | None = None,
    min_net_lead: float = 0.05,
) -> LeadLagResult:
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    sessions = np.asarray(sessions)
    latency = dict(latency_adjustment_ticks or {})
    venues_all = sorted(venue_mids)
    returns: dict[str, np.ndarray] = {}
    excluded: dict[str, str] = {}
    for venue in venues_all:
        mids = np.asarray(venue_mids[venue], dtype=float)
        if len(mids) != len(sessions):
            raise ValueError(f"venue {venue} series length does not match the grid")
        shift = latency.get(venue, 0)
        if shift:
            mids = shift_within_sessions(mids, -shift, sessions)  # undo feed delay
        r = session_returns(mids, sessions)
        valid = int(np.isfinite(r).sum())
        if valid < min_observations:
            excluded[venue] = f"INSUFFICIENT_OBSERVATIONS ({valid} < {min_observations})"
            continue
        returns[venue] = r
    venues = sorted(returns)

    def lags(x: np.ndarray, include_zero: bool = False, depth: int = max_lag) -> np.ndarray:
        start = 0 if include_zero else 1
        return np.column_stack([shift_within_sessions(x, k, sessions) for k in range(start, depth + 1)])

    factor_block = None
    if common_factor_returns is not None:
        # The factor is controlled contemporaneously and to twice the venue lag depth: venue own-lag
        # regressors carry factor terms up to 2*max_lag back, and an uncontrolled tail would let a
        # source venue's lags "explain" them (a spurious edge).
        factor_block = lags(np.asarray(common_factor_returns, dtype=float), include_zero=True, depth=2 * max_lag + 1)

    n_tests = max(1, len(venues) * (len(venues) - 1))
    raw: dict[tuple[str, str], tuple] = {}
    skipped: dict[str, str] = {}
    own_lags = {v: lags(returns[v]) for v in venues}
    for i in venues:
        for j in venues:
            if i == j:
                continue
            result, n = _pair_test(returns[j], own_lags[j], own_lags[i], factor_block, min_observations)
            if result is None:
                skipped[f"{i}->{j}"] = f"INSUFFICIENT_JOINT_OBSERVATIONS ({n})"
                continue
            raw[(i, j)] = (*result, n)

    all_pairs: list[EdgeEstimate] = []
    for (i, j), (partial, f_stat, p_value, best_lag, n) in sorted(raw.items()):
        reverse = raw.get((j, i))
        net = partial - (reverse[0] if reverse is not None else 0.0)
        p_adj = min(1.0, p_value * n_tests)
        significant = p_adj < significance and partial > 0 and net > min_net_lead
        all_pairs.append(EdgeEstimate(i, j, partial, net, p_value, p_adj, f_stat, n, best_lag, significant))
    edges = [e for e in all_pairs if e.significant]
    return LeadLagResult(
        venues=venues,
        edges=edges,
        all_pairs=all_pairs,
        excluded_venues=excluded,
        skipped_pairs=skipped,
        max_lag=max_lag,
        significance=significance,
        min_net_lead=min_net_lead,
        latency_adjustment_ticks={k: v for k, v in sorted(latency.items())},
        common_factor_controlled=common_factor_returns is not None,
    )
