"""Spec §11.3 property-based invariants (Hypothesis)."""

import random
from datetime import UTC, datetime, timedelta

from helpers import exec_msg, order_msg, run_flow
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from venue_integrity import MappingStatus, resolve_venue
from venue_integrity.execution.execution_reconciler import reconcile
from venue_integrity.execution.order_normalizer import classify_order, normalize_order_type
from venue_integrity.graph.centrality import calculate_influence
from venue_integrity.graph.network_builder import build_venue_graph
from venue_integrity.identity.mic_registry import VenueRegistry
from venue_integrity.schemas.enums import Classification, IdentifierType, OrderType

REGISTRY = {"ABC": [{"venue_id": "VENUE_A", "mic": "XABC"}]}
SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# 1. An unknown venue must never become a valid venue ID.
@given(venue=st.text(min_size=1).filter(lambda x: x != "ABC"))
def test_unknown_venues_never_resolve(venue):
    result = resolve_venue(venue, REGISTRY, "1.0")
    assert result.mapping_status == MappingStatus.UNMAPPED
    assert result.venue_id is None


@given(venue=st.text(min_size=1), t=st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2027, 1, 1)))
@SETTINGS
def test_unknown_venues_never_resolve_in_real_registry(registry, venue, t):
    known = {a for r in registry.all_records() for a in r.raw_aliases}
    if venue in known:
        return
    res = resolve_venue(venue, registry, event_time=t.replace(tzinfo=UTC))
    assert res.venue_id is None and res.mapping_status is MappingStatus.UNMAPPED


# 2. An order's type must not change when its venue changes.
@given(code=st.sampled_from(["1", "2", "3", "4", "K", "P", "Z", None]),
       dest_a=st.text(max_size=12), dest_b=st.text(max_size=12))
@SETTINGS
def test_order_type_independent_of_venue(code, dest_a, dest_b):
    if any("\x01" in d or "|" in d or "=" in d for d in (dest_a, dest_b)):
        return
    _, n1, _ = run_flow([order_msg("C1", ord_type=code, dest=dest_a or None)], [])
    _, n2, _ = run_flow([order_msg("C1", ord_type=code, dest=dest_b or None)], [])
    (o1,), (o2,) = n1.orders, n2.orders
    assert (o1.order_type, o1.classification) == (o2.order_type, o2.classification)
    assert o1.classification is classify_order(normalize_order_type(code))
    assert o1.classification.value != "LEGACY"


# 3. Mapping is deterministic for the same registry version and effective timestamp.
@given(raw=st.sampled_from(["ABC", "VNB_OLD", "SMART", "B_ROUTE_7", "XVNX", "NOPE", ""]),
       broker=st.sampled_from([None, "BROKER_A", "BROKER_B"]),
       idtype=st.sampled_from(list(IdentifierType)),
       t=st.datetimes(min_value=datetime(2025, 6, 1), max_value=datetime(2027, 1, 1)))
@SETTINGS
def test_mapping_deterministic(registry, mic_reference, raw, broker, idtype, t):
    kw = dict(event_time=t.replace(tzinfo=UTC), source_system=broker, identifier_type=idtype, mic_reference=mic_reference)
    assert resolve_venue(raw, registry, "2.0", **kw) == resolve_venue(raw, registry, "2.0", **kw)


# 4 + 5. Duplicate ingestion and reordering must not change reconciled quantities.
fill_plan = st.lists(st.tuples(st.integers(1, 5), st.sampled_from(["XABC", "XVNA", None])), min_size=1, max_size=6)


@given(plan=fill_plan, dup_indices=st.lists(st.integers(0, 5), max_size=4), seed=st.integers(0, 10_000))
@SETTINGS
def test_duplicates_and_reordering_do_not_change_economics(plan, dup_indices, seed):
    orders = [order_msg("C1", qty=3000)]
    execs = [exec_msg(f"E{i}", "C1", qty=q * 100, last_mkt=mkt, seq=i + 1, offset_ms=i) for i, (q, mkt) in enumerate(plan)]
    _, n_ref, ref = run_flow(orders, execs)
    duplicated = execs + [execs[i] for i in dup_indices if i < len(execs)]
    duplicated += [exec_msg("E0", "C1", qty=plan[0][0] * 100, last_mkt=plan[0][1], seq=1, offset_ms=0, poss_dup=True,
                            sending_offset_ms=500)]
    shuffled = duplicated[:]
    random.Random(seed).shuffle(shuffled)
    _, _, other = run_flow(orders, shuffled)
    assert other.economic_state() == ref.economic_state()
    assert ref.child("C1").filled_quantity == sum(q * 100 for q, _ in plan)
    # same events, arbitrary order, straight into the reconciler
    events = n_ref.executions[:]
    random.Random(seed + 1).shuffle(events)
    assert reconcile(n_ref.orders, events).economic_state() == ref.economic_state()


# 6. An unresolved actual execution venue must remain unresolved.
@given(last_mkt=st.one_of(st.none(), st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=8)),
       dest=st.sampled_from(["ABC", "VNA", "VND", "NOPE"]))
@SETTINGS
def test_unresolved_execution_venue_stays_unresolved(last_mkt, dest):
    if last_mkt in {"XABC", "XVNA", "XVNB", "XVND", "ABC", "VNA", "VNB", "VND", "DARK_D"}:
        return
    _, _, rep = run_flow([order_msg("C1", dest=dest)], [exec_msg("E1", "C1", last_mkt=last_mkt)])
    (e,) = rep.executions
    assert e.actual_venue_id is None and e.venue_key == "UNRESOLVED"


# 7. A historical event must use the mapping valid at its event time.
@given(days=st.integers(-200, 200))
@SETTINGS
def test_historical_event_uses_mapping_valid_at_event_time(registry, days):
    t = datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=days)
    res = resolve_venue("VNB_OLD", registry, event_time=t)
    if datetime(2026, 1, 1, tzinfo=UTC) <= t < datetime(2026, 7, 1, tzinfo=UTC):
        assert res.venue_id == "VENUE_B"
    else:
        assert res.mapping_status is MappingStatus.UNMAPPED


# 8. Graph score must not depend on the order venue records are loaded.
edge = st.tuples(st.sampled_from("ABCDE"), st.sampled_from("ABCDE"), st.floats(0.01, 1.0), st.floats(0.0, 1.0))


@given(edges=st.lists(edge, max_size=12, unique_by=lambda e: (e[0], e[1])), seed=st.integers(0, 1000))
@SETTINGS
def test_graph_scores_independent_of_load_order(edges, seed):
    records = [{"source_venue": s, "target_venue": t, "weight": w, "confidence": c} for s, t, w, c in edges]
    venues = list("ABCDE")
    shuffled = records[:]
    rng = random.Random(seed)
    rng.shuffle(shuffled)
    rng.shuffle(venues)
    a = calculate_influence(build_venue_graph(records, list("ABCDE"))).scores
    b = calculate_influence(build_venue_graph(shuffled, venues)).scores
    assert a == b
    assert all(0.0 <= v <= 1.0 for v in a.values())


def test_simple_alias_map_registry_type():
    assert isinstance(VenueRegistry.from_alias_map(REGISTRY), VenueRegistry)
    assert Classification.MARKET is classify_order(OrderType.MARKET)
