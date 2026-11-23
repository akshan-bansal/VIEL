"""Command-line interface: ``via <command>``.

    via demo        run the prototype acceptance demonstration (simulation)
    via replay      replay a capture file through ingestion, identity, reconciliation
    via resolve     resolve one raw venue identifier
    via benchmarks  run the graph-engine benchmark suite
    via serve       start the read-mostly audit API

No command connects to an order-routing session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from venue_integrity.timeutil import parse_iso


def _cmd_demo(args) -> int:
    from venue_integrity.demo import run_demo

    run_demo(args.out, verbose=not args.quiet, run_tests=args.with_tests)
    return 0


def _cmd_replay(args) -> int:
    from venue_integrity.ingestion.raw_event_store import RawEventStore
    from venue_integrity.ingestion.sources import read_captures
    from venue_integrity.pipeline import AuditPipeline

    store = RawEventStore(args.store) if args.store else RawEventStore(":memory:")
    pipeline = AuditPipeline(raw_store=store, mode="replay")
    ingestion = pipeline.ingest(list(read_captures(args.captures)))
    normalized = pipeline.normalize(registry_version=args.registry_version)
    report = pipeline.reconcile(normalized)
    out = {
        "ingestion": ingestion.to_dict(),
        "raw_store": store.manifest(),
        "normalization": normalized.stats,
        "reconciliation": report.kpis,
        "exceptions": pipeline.exceptions.summary(),
        "sequence": pipeline.sequence_monitor.summary(),
    }
    text = json.dumps(out, indent=2, sort_keys=True, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _cmd_resolve(args) -> int:
    from venue_integrity.config import load_config
    from venue_integrity.identity.mic_registry import MicReference, VenueRegistry
    from venue_integrity.identity.venue_resolver import resolve_venue
    from venue_integrity.schemas.enums import IdentifierType

    cfg = load_config()
    registry = VenueRegistry.from_config(cfg.venue_registry)
    result = resolve_venue(
        args.raw_value, registry, args.registry_version,
        event_time=parse_iso(args.event_time), source_system=args.source_system,
        identifier_type=IdentifierType(args.identifier_type), mic_reference=MicReference.from_csv(cfg.mic_reference_path),
    )
    print(result.model_dump_json(indent=2))
    return 0 if result.mapping_status.value == "MAPPED" else 2


def _cmd_benchmarks(args) -> int:
    from venue_integrity.graph.validation import run_graph_benchmarks

    results = run_graph_benchmarks(args.seed)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.case:40s} {r.detail}")
    return 0 if all(r.passed for r in results) else 1


def _cmd_serve(args) -> int:  # pragma: no cover - requires a running server
    import uvicorn

    uvicorn.run("venue_integrity.api.main:create_app", factory=True, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="via", description="Venue Integrity Audit (replay/simulation only)")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the prototype acceptance demonstration")
    demo.add_argument("--out", default="audit_output")
    demo.add_argument("--with-tests", action="store_true", help="run the test suite and record results in the evidence")
    demo.add_argument("--quiet", action="store_true")
    demo.set_defaults(func=_cmd_demo)

    replay = sub.add_parser("replay", help="replay a JSON-lines capture file")
    replay.add_argument("captures")
    replay.add_argument("--store", help="SQLite raw store path (default: in-memory)")
    replay.add_argument("--registry-version")
    replay.add_argument("--out")
    replay.set_defaults(func=_cmd_replay)

    resolve = sub.add_parser("resolve", help="resolve a raw venue identifier")
    resolve.add_argument("raw_value")
    resolve.add_argument("--event-time", required=True, help="ISO-8601 with offset, e.g. 2026-11-23T14:30:00Z")
    resolve.add_argument("--source-system")
    resolve.add_argument("--identifier-type", default="ALIAS", choices=["ALIAS", "MIC"])
    resolve.add_argument("--registry-version")
    resolve.set_defaults(func=_cmd_resolve)

    bench = sub.add_parser("benchmarks", help="run graph-engine benchmarks")
    bench.add_argument("--seed", type=int, default=7)
    bench.set_defaults(func=_cmd_benchmarks)

    serve = sub.add_parser("serve", help="start the audit API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
