"""Spec §1 / §9.2: localise the LEGACY defect and detect contamination."""

from venue_integrity.analytics.contamination_detector import ChangeEvent, ClassifiedOrder, cusum_upward, detect_contamination
from venue_integrity.analytics.defect_localization import OrderStageRecord, localise
from venue_integrity.execution.legacy_classifier import LEGACY_LABEL, LegacyNormalizer, legacy_alias_table
from venue_integrity.schemas.enums import DefectCategory, ExceptionType

S = DefectCategory


def rec(key, ord_type, venue_status, labels):
    return OrderStageRecord(key, ord_type, venue_status, "D", dict(zip(
        (S.PROTOCOL_INGESTION, S.ORDER_TYPE_NORMALIZATION, S.OMS_EMS_ROUTING, S.ANALYTICS_MODEL_TRAINING), labels)))


def test_legacy_normaliser_reproduces_the_defect(registry):
    legacy = LegacyNormalizer(legacy_alias_table(registry, "2.0"))
    assert legacy.classify("1", "ABC") == "MARKET"
    assert legacy.classify("1", "ABC_ROUTER") == LEGACY_LABEL  # the reported defect
    assert legacy.alias_table["SMART"] == "VENUE_D"  # silent "last wins" ambiguity resolution


def test_localisation_finds_normalisation_stage_and_combination():
    records = [rec(f"m{i}", "1", "MAPPED", ["MARKET"] * 4) for i in range(30)]
    records += [rec(f"u{i}", "1", "UNMAPPED", ["MARKET", "LEGACY", "LEGACY", "LEGACY"]) for i in range(20)]
    out = localise(records)
    assert out["earliest_divergence_stage"] == "ORDER_TYPE_NORMALIZATION"
    assert out["defect_category"] == "COMBINATION"
    ind = out["classification_venue_independence"]["1"]
    assert ind["violates_independence"] and ind["venue_unmapped"]["change_rate"] == 1.0


def test_localisation_identifies_analytics_stage_defect():
    records = [rec(f"x{i}", "1", "MAPPED", ["MARKET", "MARKET", "MARKET", "OTHER"]) for i in range(5)]
    out = localise(records)
    assert out["earliest_divergence_stage"] == "ANALYTICS_MODEL_TRAINING" and out["defect_category"] == "ANALYTICS_MODEL_TRAINING"


def test_no_defect():
    out = localise([rec("a", "2", "MAPPED", ["LIMIT"] * 4)])
    assert out["orders_with_divergence"] == 0 and out["defect_category"] == "NO_DEFECT"


def test_scenario_localisation(scenario_run):
    p, n = scenario_run["pipeline"], scenario_run["normalized"]
    out = p.localise_defect(p.legacy_stage_records(n, "2.0"))
    assert out["earliest_divergence_stage"] == "ORDER_TYPE_NORMALIZATION"
    assert out["divergence_stage_counts"].get("PROTOCOL_INGESTION", 0) == 0
    assert out["classification_venue_independence"]["1"]["violates_independence"]
    assert out["classification_venue_independence"]["1"]["venue_mapped"]["label_changed"] == 0


def test_contamination_detector_change_point_and_concentration():
    orders = []
    for b in range(10):
        for i in range(20):
            legacy = b >= 6 and i % 2 == 0
            orders.append(ClassifiedOrder(f"{b}-{i}", b, f"d{b}", "1", LEGACY_LABEL if legacy else "MARKET",
                                          "ABC_ROUTER" if legacy else "ABC", "VENUE_C" if i % 2 == 0 else "VENUE_A", 100))
    report = detect_contamination(orders, venue_influence={"VENUE_A": 0.3, "VENUE_C": 1.0},
                                  change_events=[ChangeEvent(6, "alias release")])
    assert [c["bucket"] for c in report.change_points if c["method"] == "rolling_zscore"][0] == 6
    assert report.change_points[0]["nearest_preceding_change"]["description"] == "alias release"
    assert report.concentration["by_corrected_venue"] == {"VENUE_C": 1.0}
    assert report.findings[0].exception_type is ExceptionType.MARKET_ORDER_LEGACY_CLASSIFICATION
    assert "not establish" in report.influence_association["caveat"]


def test_cusum():
    assert cusum_upward([0, 0, 0, 0.5, 0.5], k=0.05, h=0.25) == 3
    assert cusum_upward([0.1] * 10, k=0.05, h=0.25) is None


def test_scenario_contamination(scenario_run, scenario, registry):
    p, n, g = scenario_run["pipeline"], scenario_run["normalized"], scenario_run["graph"]
    report = p.contamination(n, "2.0", registry, g.influence.scores, scenario.session_windows, scenario.change_events)
    alias_session = scenario.config.alias_change_session
    assert all(row["legacy_share"] == 0 for row in report.series[:alias_session])
    assert report.change_points[0]["bucket"] == alias_session
    assert report.incidents[0]["raw_destination"] == scenario.config.new_alias
