"""Stage-level defect localisation (spec §1).

The audit must establish *where* a classification changes, not assume that
LEGACY means a legacy route. For each order the expected classification is
derived from the raw protocol value with the canonical (venue-independent)
classifier; the audited system's label at every stage is compared with it, and
the first stage that disagrees is the divergence point.

Population evidence: holding the raw order type fixed, a classification that
depends on whether the venue resolved violates Classification ⊥ Venue
Identity. This is tested with Fisher's exact test on the 2x2 table
(venue mapped / unmapped) x (label correct / label changed).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from scipy import stats

from venue_integrity.execution.order_normalizer import classify_order, normalize_order_type
from venue_integrity.schemas.enums import DefectCategory

STAGES: tuple[DefectCategory, ...] = (
    DefectCategory.PROTOCOL_INGESTION,
    DefectCategory.ORDER_TYPE_NORMALIZATION,
    DefectCategory.OMS_EMS_ROUTING,
    DefectCategory.EXECUTION_ATTRIBUTION,
    DefectCategory.ANALYTICS_MODEL_TRAINING,
)


@dataclass(frozen=True)
class OrderStageRecord:
    order_key: str
    raw_ord_type: str | None
    venue_status: str  # outcome of the audited system's venue lookup (VENUE_REFERENCE_DATA stage)
    raw_destination: str | None
    stage_labels: dict[DefectCategory, str]

    @property
    def expected(self) -> str:
        return classify_order(normalize_order_type(self.raw_ord_type)).value


@dataclass(frozen=True)
class OrderLocalisation:
    order_key: str
    expected: str
    divergence_stage: DefectCategory | None
    observed_label: str | None
    venue_status: str
    categories: tuple[DefectCategory, ...]


def localise_order(record: OrderStageRecord) -> OrderLocalisation:
    expected = record.expected
    for stage in STAGES:
        label = record.stage_labels.get(stage)
        if label is None:
            continue
        if label != expected:
            categories: list[DefectCategory] = [stage]
            if record.venue_status != "MAPPED":
                categories.insert(0, DefectCategory.VENUE_REFERENCE_DATA)
            return OrderLocalisation(record.order_key, expected, stage, label, record.venue_status, tuple(categories))
    return OrderLocalisation(record.order_key, expected, None, None, record.venue_status, (DefectCategory.NO_DEFECT,))


def localise(records: list[OrderStageRecord]) -> dict[str, Any]:
    results = [localise_order(r) for r in records]
    stage_counts = Counter(r.divergence_stage.value if r.divergence_stage else "NONE" for r in results)
    category_sets = Counter("+".join(c.value for c in r.categories) for r in results)
    transitions = Counter(f"{r.expected}->{r.observed_label}" for r in results if r.divergence_stage)

    # Classification ⊥ venue identity test, per raw order type
    independence = {}
    by_type: dict[str, list[OrderLocalisation]] = defaultdict(list)
    for record, result in zip(records, results, strict=True):
        by_type[record.raw_ord_type or "MISSING"].append(result)
    for raw_type, group in sorted(by_type.items()):
        mapped = [g for g in group if g.venue_status == "MAPPED"]
        unmapped = [g for g in group if g.venue_status != "MAPPED"]
        a = sum(1 for g in mapped if g.divergence_stage is None)
        b = len(mapped) - a
        c = sum(1 for g in unmapped if g.divergence_stage is None)
        d = len(unmapped) - c
        p = None
        if mapped and unmapped:
            p = float(stats.fisher_exact([[a, b], [c, d]]).pvalue)
        independence[raw_type] = {
            "expected_classification": classify_order(normalize_order_type(None if raw_type == "MISSING" else raw_type)).value,
            "venue_mapped": {"n": len(mapped), "label_changed": b,
                             "change_rate": b / len(mapped) if mapped else None},
            "venue_unmapped": {"n": len(unmapped), "label_changed": d,
                               "change_rate": d / len(unmapped) if unmapped else None},
            "fisher_exact_p": p,
            "violates_independence": p is not None and p < 1e-3 and (d / len(unmapped)) > (b / len(mapped) if mapped else 0),
        }

    diverged = [r for r in results if r.divergence_stage]
    first_stage = None
    if diverged:
        first_stage = min((r.divergence_stage for r in diverged), key=STAGES.index).value
    combination = any(len(r.categories) > 1 for r in diverged)
    conclusion = _conclusion(diverged, first_stage, combination, independence)
    return {
        "orders_examined": len(results),
        "orders_with_divergence": len(diverged),
        "divergence_stage_counts": dict(sorted(stage_counts.items())),
        "defect_category_sets": dict(sorted(category_sets.items())),
        "label_transitions": dict(sorted(transitions.items())),
        "earliest_divergence_stage": first_stage,
        "classification_venue_independence": independence,
        "defect_category": (DefectCategory.COMBINATION.value if combination
                            else (first_stage or DefectCategory.NO_DEFECT.value)),
        "conclusion": conclusion,
    }


def _conclusion(diverged, first_stage, combination, independence) -> str:
    if not diverged:
        return "No stage changed any order's classification relative to its raw protocol order type."
    violated = [t for t, v in independence.items() if v["violates_independence"]]
    parts = [f"{len(diverged)} orders changed classification; the earliest divergence is at {first_stage}."]
    if DefectCategory.PROTOCOL_INGESTION.value != first_stage:
        parts.append("The raw protocol value was parsed correctly (no PROTOCOL_INGESTION divergence).")
    if violated:
        parts.append(
            "Holding the raw order type fixed, the label depends on whether the venue resolved "
            f"(order types {', '.join(violated)}; Fisher exact p < 1e-3): classification is not independent of venue identity."
        )
    with_venue_failure = [r for r in diverged if DefectCategory.VENUE_REFERENCE_DATA in r.categories]
    if combination:
        parts.append(f"{len(with_venue_failure)} of {len(diverged)} divergences co-occur with a failed venue lookup: "
                     "a COMBINATION of VENUE_REFERENCE_DATA (unregistered alias) and a normaliser that lets venue "
                     "resolution drive classification.")
    others = [r for r in diverged if DefectCategory.VENUE_REFERENCE_DATA not in r.categories]
    if others:
        transitions = Counter(f"{r.expected}->{r.observed_label}" for r in others)
        stages = Counter(r.divergence_stage.value for r in others)
        parts.append(f"{len(others)} divergence(s) occur with the venue resolved ({dict(transitions)} at "
                     f"{', '.join(sorted(stages))}): a separate silent order-type coercion.")
    return " ".join(parts)
