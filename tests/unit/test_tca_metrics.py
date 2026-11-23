import math

import numpy as np
import pytest

from venue_integrity.schemas.enums import Side
from venue_integrity.tca.implementation_shortfall import implementation_shortfall
from venue_integrity.tca.market_impact import permanent_impact_bps, square_root_impact_bps
from venue_integrity.tca.markout import QuoteSeries, QuoteStatus
from venue_integrity.tca.spread import execution_metrics, impact_beyond_touch_bps, side_direction


def test_decomposition_identity_and_signs():
    m = execution_metrics("BUY", 100.02, 100.0, 100.03)
    assert m.effective_spread_bps == pytest.approx(4.0)
    assert m.realized_spread_bps == pytest.approx(-2.0)
    assert m.adverse_selection_bps == pytest.approx(6.0)  # mid moved up after a buy -> positive
    assert m.effective_spread_bps == pytest.approx(m.realized_spread_bps + m.adverse_selection_bps)
    s = execution_metrics(Side.SELL, 99.98, 100.0, 99.97)
    assert (s.effective_spread_bps, s.adverse_selection_bps) == (pytest.approx(4.0), pytest.approx(6.0))


@pytest.mark.parametrize("side", ["buy", "SELL_SHORT", Side.SELL_SHORT_EXEMPT])
def test_side_parsing(side):
    assert side_direction(side) in (1, -1)


@pytest.mark.parametrize("side", ["B", "", "LONG", None])
def test_unknown_side_is_an_error_not_a_sell(side):
    with pytest.raises(ValueError):
        side_direction(side)


@pytest.mark.parametrize("prices", [(0, 1, 1), (1, -1, 1), (1, 1, math.nan), (1, 1, math.inf)])
def test_invalid_prices(prices):
    with pytest.raises(ValueError):
        execution_metrics("BUY", *prices)


def test_impact_beyond_touch():
    assert impact_beyond_touch_bps("BUY", 100.02, 99.98, 100.01, 100.0) == pytest.approx(1.0)
    assert impact_beyond_touch_bps("SELL", 100.0, 99.99, 100.01, 100.0) == pytest.approx(-1.0)  # improvement


def test_quote_series_staleness_and_incomplete_horizons():
    ns = 1_000_000
    s = QuoteSeries(np.array([0, 100, 200, 5000]) * ns, [99.9] * 4, [100.1] * 4, windows=[(0, 5000 * ns)])
    assert s.asof(150 * ns, 1000 * ns).status is QuoteStatus.OK
    assert s.asof(150 * ns, 10 * ns).status is QuoteStatus.STALE
    assert s.asof(-1, 1000 * ns).status is QuoteStatus.NO_DATA
    assert s.markout(4000 * ns, 2000 * ns, 1000 * ns).status is QuoteStatus.INCOMPLETE  # never zero
    assert s.markout(100 * ns, 100 * ns, 1000 * ns).mid == pytest.approx(100.0)
    with pytest.raises(ValueError):
        QuoteSeries([2, 1], [1, 1], [1, 1])


def test_crossed_quote_is_no_data():
    s = QuoteSeries([0], [100.2], [100.1])
    assert s.asof(0, 1).status is QuoteStatus.NO_DATA


def test_implementation_shortfall_components():
    r = implementation_shortfall("BUY", 1000, 100.0, 101.0, [(600, 100.1, 0.5)])
    assert r.execution_cost_bps == pytest.approx(600 * 0.1 / 100000 * 1e4)
    assert r.fee_bps == pytest.approx(600 * 100.1 * 0.5e-4 / 100000 * 1e4)
    assert r.opportunity_cost_bps == pytest.approx(400 * 1.0 / 100000 * 1e4)
    assert r.total_bps == pytest.approx(r.execution_cost_bps + r.fee_bps + r.opportunity_cost_bps)
    assert r.fill_ratio == pytest.approx(0.6)
    with pytest.raises(ValueError, match="fee unknown"):
        implementation_shortfall("BUY", 1000, 100.0, 101.0, [(600, 100.1, None)])


def test_impact_helpers():
    assert permanent_impact_bps("SELL", 100.0, 99.9) == pytest.approx(10.0)
    assert square_root_impact_bps(10_000, 1_000_000, 100) == pytest.approx(10.0)
