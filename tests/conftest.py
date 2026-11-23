from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from venue_integrity.config import load_config  # noqa: E402
from venue_integrity.identity.mic_registry import MicReference, VenueRegistry  # noqa: E402
from venue_integrity.ingestion.fix_parser import FixDictionary  # noqa: E402


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture(scope="session")
def registry(config):
    return VenueRegistry.from_config(config.venue_registry)


@pytest.fixture(scope="session")
def mic_reference(config):
    return MicReference.from_csv(config.mic_reference_path)


@pytest.fixture(scope="session")
def dictionary(config):
    return FixDictionary(config.fix_dictionary)


@pytest.fixture(scope="session")
def scenario():
    from venue_integrity.simulation.generator import generate_scenario

    return generate_scenario()


@pytest.fixture(scope="session")
def scenario_run(scenario, config):
    """Full pipeline over the synthetic scenario under registry v2.0 (expensive; shared)."""
    from venue_integrity.pipeline import AuditPipeline

    pipeline = AuditPipeline(config, mode="simulation")
    pipeline.ingest(scenario.captures)
    normalized = pipeline.normalize()
    report = pipeline.reconcile(normalized)
    tca = pipeline.run_tca(scenario.market_data, report)
    graph = pipeline.run_graph(scenario.market_data)
    return {"pipeline": pipeline, "normalized": normalized, "report": report, "tca": tca, "graph": graph}
