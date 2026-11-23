"""API contract: auth, roles, maker-checker, rate limits, endpoints of spec §15."""

import pytest
from fastapi.testclient import TestClient
from helpers import capture, order_msg

from venue_integrity.api.main import create_app
from venue_integrity.api.security import KeyStore
from venue_integrity.api.service import AuditService

KEYS = [{"key": "r", "principal": "analyst", "roles": ["reader"]},
        {"key": "i", "principal": "ingest", "roles": ["ingestor"]},
        {"key": "a", "principal": "maker", "roles": ["admin", "reader"]},
        {"key": "p", "principal": "checker", "roles": ["approver"]},
        {"key": "ap", "principal": "maker", "roles": ["approver"]}]
R, I, A, P, SAME = ({"X-API-Key": k} for k in ("r", "i", "a", "p", "ap"))


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(keys=KeyStore(KEYS), service=AuditService(bootstrap="simulation"), rate_limit_per_minute=10_000))


def test_health_and_metrics_public(client):
    health = client.get("/health").json()
    assert health["raw_chain_ok"] and health["mode"] == "replay"
    metrics = client.get("/metrics").text
    assert "via_silent_coercions 0.0" in metrics and "via_silent_venue_fallbacks 0.0" in metrics


def test_authentication_and_roles(client):
    assert client.get("/audit/exceptions").status_code == 401
    assert client.get("/audit/exceptions", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/audit/exceptions", headers=I).status_code == 403
    assert client.post("/events/ingest", headers=R, json={}).status_code == 403


def test_ingest_validates_and_is_idempotent(client):
    body = capture(order_msg("API-1", dest="ABC")).model_dump(mode="json")
    first = client.post("/events/ingest", headers=I, json=body).json()
    second = client.post("/events/ingest", headers=I, json=body).json()
    assert first["outcome"] == "STORED" and second["outcome"] == "DUPLICATE_DELIVERY"
    assert first["event_id"] == second["event_id"]
    assert client.post("/events/ingest", headers=I, json={**body, "extra": 1}).status_code == 422


def test_resolve_and_venue_lookup(client):
    r = client.post("/venues/resolve", headers=R, json={"raw_value": "SMART", "event_time": "2026-11-10T14:30:00Z",
                                                        "source_system": "BROKER_B"}).json()
    assert r["mapping_status"] == "AMBIGUOUS" and r["venue_id"] is None
    naive = client.post("/venues/resolve", headers=R, json={"raw_value": "ABC", "event_time": "2026-11-10T14:30:00"})
    assert naive.status_code == 422  # never assume UTC
    assert client.get("/venues/VENUE_C", headers=R).json()["records"][0]["mic"] == "XABC"
    assert client.get("/venues/NOPE", headers=R).status_code == 404


def test_audit_and_analytics_endpoints(client):
    ex = client.get("/audit/exceptions?severity=CRITICAL&limit=5", headers=R).json()
    assert ex["summary"]["total"] > 0 and all(e["severity"] == "CRITICAL" for e in ex["exceptions"])
    assert client.get("/audit/reconciliation", headers=R).json()["kpis"]["silent_venue_fallbacks"] == 0
    assert client.get("/analytics/venue-influence", headers=R).json()["influence"]["scores"]["VENUE_C"] == 1.0
    assert "VENUE_C" in client.get("/analytics/tca", headers=R).json()["venues"]
    d = client.get("/router/decisions/DEC-S08-P000-C0", headers=R).json()
    assert d["reconstruction"]["status"] == "REPRODUCED"
    assert client.get("/router/decisions/NOPE", headers=R).status_code == 404


def test_maker_checker_over_http(client):
    body = {"description": "add ABC_ROUTER", "evidence": "broker notes",
            "add_records": [{"venue_id": "VENUE_C", "mic": "XABC", "broker_aliases": {"BROKER_A": ["ABC_ROUTER"]},
                             "valid_from": "2026-11-04T00:00:00Z", "venue_type": "EXCHANGE"}]}
    assert client.post("/admin/venues/changes", headers=R, json=body).status_code == 403
    rid = client.post("/admin/venues/changes", headers=A, json=body).json()["request_id"]
    assert client.post(f"/admin/venues/changes/{rid}/approve", headers=SAME, json={"new_version": "2.1"}).status_code == 403
    ok = client.post(f"/admin/venues/changes/{rid}/approve", headers=P, json={"new_version": "2.1"}).json()
    assert ok["registry_version"] == "2.1"
    r = client.post("/venues/resolve", headers=R, json={"raw_value": "ABC_ROUTER", "event_time": "2026-11-10T14:30:00Z",
                                                        "source_system": "BROKER_A"}).json()
    assert r["venue_id"] == "VENUE_C"
    bad_mic = {**body, "add_records": [{**body["add_records"][0], "mic": "XVNX"}]}
    assert client.post("/admin/venues/changes", headers=A, json=bad_mic).status_code == 422


def test_no_order_entry_routes(client):
    paths = client.app.openapi()["paths"]
    mutating = {(path, method.upper()) for path, ops in paths.items() for method in ops
                if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}}
    assert mutating == {("/events/ingest", "POST"), ("/venues/resolve", "POST"), ("/admin/venues/changes", "POST"),
                        ("/admin/venues/changes/{request_id}/approve", "POST")}


def test_rate_limit():
    app = create_app(keys=KeyStore(KEYS), service=AuditService(), rate_limit_per_minute=2)
    c = TestClient(app)
    codes = [c.get("/audit/reconciliation", headers=R).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:]


def test_refuses_live_mode(monkeypatch):
    from venue_integrity.config import ModeError

    monkeypatch.setenv("VIA_MODE", "live")
    with pytest.raises(ModeError):
        create_app(keys=KeyStore(KEYS), service=AuditService())
