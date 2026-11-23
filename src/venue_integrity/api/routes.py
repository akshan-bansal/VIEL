"""HTTP routes (spec §15 "Required initial API endpoints").

There is deliberately no route that creates, amends, cancels or routes an
order: the audit service is read-mostly and cannot alter live trading.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from venue_integrity.api.monitoring import CONTENT_TYPE
from venue_integrity.api.security import Principal, require
from venue_integrity.identity.mapping_history import MakerCheckerError
from venue_integrity.identity.venue_resolver import resolve_venue
from venue_integrity.router.decision_reconstruction import reconstruct_decision
from venue_integrity.schemas.common import UtcDatetime
from venue_integrity.schemas.enums import IdentifierType
from venue_integrity.schemas.raw_event import RawCapture

router = APIRouter()


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_value: str | None = Field(default=None, max_length=128)
    event_time: UtcDatetime  # a timestamp without an offset is rejected (422), never assumed UTC
    source_system: str | None = Field(default=None, max_length=64)
    identifier_type: IdentifierType = IdentifierType.ALIAS
    registry_version: str | None = Field(default=None, max_length=32)


class VenueRecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    venue_id: str = Field(max_length=64)
    mic: str | None = Field(default=None, max_length=4)
    operating_mic: str | None = Field(default=None, max_length=4)
    raw_aliases: list[str] = Field(default_factory=list, max_length=32)
    broker_aliases: dict[str, list[str]] = Field(default_factory=dict)
    venue_name: str | None = Field(default=None, max_length=128)
    venue_type: str = "OTHER"
    country: str | None = Field(default=None, min_length=2, max_length=2)
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None
    status: str = "ACTIVE"


class ChangeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=512)
    evidence: str = Field(min_length=1, max_length=4096)
    add_records: list[VenueRecordIn] = Field(default_factory=list, max_length=50)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_version: str = Field(min_length=1, max_length=32, pattern=r"^[0-9A-Za-z._-]+$")


def _svc(request: Request):
    return request.app.state.service


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    chain = svc.pipeline.raw_store.verify_chain()
    return {"status": "ok" if chain.ok else "degraded", "mode": svc.pipeline.mode,
            "raw_events": chain.count, "raw_chain_ok": chain.ok, "registry_version": svc.registry.latest_version,
            "open_exceptions": svc.open_count()}


@router.get("/metrics")
def metrics(request: Request) -> Response:
    request.app.state.service.refresh_metrics(request.app.state.metrics)
    return Response(content=request.app.state.metrics.render(), media_type=CONTENT_TYPE)


@router.post("/events/ingest")
def ingest(capture: RawCapture, request: Request, principal: Principal = Depends(require("ingestor"))):
    if len(capture.raw_payload) > 65_536:
        raise HTTPException(413, "payload too large")
    return _svc(request).ingest(capture)


@router.post("/venues/resolve")
def resolve(body: ResolveRequest, request: Request, principal: Principal = Depends(require("reader"))):
    svc = _svc(request)
    if body.registry_version and not svc.registry.has_version(body.registry_version):
        raise HTTPException(404, f"unknown registry version {body.registry_version}")
    result = resolve_venue(body.raw_value, svc.registry, body.registry_version, event_time=body.event_time,
                           source_system=body.source_system, identifier_type=body.identifier_type,
                           mic_reference=svc.pipeline.mic_reference)
    return result.model_dump(mode="json")


@router.get("/venues/{venue_id}")
def venue(venue_id: str, request: Request, registry_version: str | None = None,
          principal: Principal = Depends(require("reader"))):
    svc = _svc(request)
    if registry_version and not svc.registry.has_version(registry_version):
        raise HTTPException(404, f"unknown registry version {registry_version}")
    records = svc.registry.venue_metadata(venue_id, registry_version)
    if not records:
        raise HTTPException(404, f"venue {venue_id} not found")
    return {"venue_id": venue_id, "registry_version": registry_version or svc.registry.latest_version,
            "records": [r.model_dump(mode="json") for r in records]}


@router.get("/audit/exceptions")
def exceptions(request: Request, status: str | None = None, severity: str | None = None,
               exception_type: str | None = None, limit: int = Query(default=100, ge=1, le=1000),
               principal: Principal = Depends(require("reader"))):
    svc = _svc(request)
    svc.reconciliation()  # make sure normalisation/reconciliation exceptions are current
    return {"summary": svc.pipeline.exceptions.summary(),
            "exceptions": svc.exceptions(status=status, severity=severity, exception_type=exception_type, limit=limit)}


@router.get("/audit/reconciliation")
def reconciliation(request: Request, include_children: bool = False, principal: Principal = Depends(require("reader"))):
    report = _svc(request).reconciliation()
    body: dict[str, Any] = {"kpis": report.kpis, "parents": len(report.parents)}
    if include_children:
        body["children"] = [c.to_dict() for c in report.children[:1000]]
    return body


@router.get("/analytics/venue-influence")
def venue_influence(request: Request, principal: Principal = Depends(require("reader"))):
    graph = _svc(request).graph()
    if graph is None:
        raise HTTPException(404, "no market data loaded")
    return {"lead_lag": graph.lead_lag.to_dict(), "influence": graph.influence.to_dict(),
            "session_regimes": {str(k): v.value for k, v in graph.regimes.items()}}


@router.get("/analytics/tca")
def tca(request: Request, principal: Principal = Depends(require("reader"))):
    report = _svc(request).tca()
    if report is None:
        raise HTTPException(404, "no market data loaded")
    return report.to_dict()


@router.get("/router/decisions/{decision_id}")
def decision(decision_id: str, request: Request, principal: Principal = Depends(require("reader"))):
    svc = _svc(request)
    d = svc.decisions.get(decision_id)
    if d is None:
        raise HTTPException(404, f"decision {decision_id} not found")
    result = reconstruct_decision(d, svc.pipeline.router_models, registry=svc.registry,
                                  reconciliation=svc.reconciliation(), tca=svc.tca())
    return {"decision": d.model_dump(mode="json"), "reconstruction": result.to_dict()}


@router.post("/admin/venues/changes", status_code=201)
def propose_change(body: ChangeRequestIn, request: Request, principal: Principal = Depends(require("admin"))):
    svc = _svc(request)
    try:
        req = svc.changes.propose(proposed_by=principal.name, description=body.description, evidence=body.evidence,
                                  add_records=[r.model_dump() for r in body.add_records],
                                  at=datetime.now().astimezone())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return req.to_dict()


@router.post("/admin/venues/changes/{request_id}/approve")
def approve_change(request_id: str, body: ApproveIn, request: Request, principal: Principal = Depends(require("approver"))):
    svc = _svc(request)
    try:
        registry, finding = svc.changes.approve(request_id, approved_by=principal.name, new_version=body.new_version,
                                                at=datetime.now().astimezone())
    except KeyError as exc:
        raise HTTPException(404, f"change request {request_id} not found") from exc
    except MakerCheckerError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    svc.pipeline.exceptions.raise_finding(finding, datetime.now().astimezone())
    svc.invalidate()
    return {"registry_version": registry.latest_version, "request": svc.changes.get(request_id).to_dict()}
