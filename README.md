# VIEL — Venue Integrity Audit (VIA)

A replay-mode audit that detects, reconstructs, quantifies and prevents
discrepancies between **market-venue identity, order classification, execution
attribution, graph-derived venue influence and Smart Order Router (SOR)
decisions**. It builds the chain of evidence

```
Market event → Venue identity → Order intent → Routing decision → Execution → Economic outcome
```

and answers the question behind the reported defect
**"significant venue influence + market order → LEGACY"**: *at exactly which
stage does an order's classification change, and why?*

> **Safety boundary.** The audit runs only in `replay` or `simulation` mode
> (anything else refuses to start). It contains no FIX session layer and no
> order-entry path: no command, API route or module can create, amend, cancel
> or route an order. Alerts notify operations, and the audit never alters live
> trading.

## Quick start

```bash
pip install -e ".[dev]"          # Python 3.12+
via demo --out audit_output      # 8-step prototype acceptance demonstration (≈25 s)
via demo --with-tests            # same, and records the test-suite result in the evidence (Gate 4)
pytest                           # 186 tests (the PostgreSQL test runs when VIA_TEST_PG_DSN is set)
```

The demo writes `audit_output/audit_evidence.json` (the §14 evidence package),
`audit_output/audit_report.md`, the raw capture file and the hash-chained raw
store. A committed copy of the report is in
[`docs/sample_audit_report.md`](docs/sample_audit_report.md).

Other commands:

```bash
via replay audit_output/raw_capture.jsonl --store raw.sqlite   # ingest → identity → reconciliation summary
via resolve ABC_ROUTER --event-time 2026-11-10T14:30:00Z --source-system BROKER_A
via benchmarks                                                  # graph-engine validation suite
via serve                                                       # audit API (see "API" below)
```

## What the prototype demonstrates (spec §19)

All data is synthetic, and every identifier is illustrative. The scenario has 20
sessions, 4 venues, 1,838 child orders, about 7,500 FIX messages and 1,838
recorded SOR decisions, plus a planted fault set covering the §11.4 matrix.

| Step | Result on the scenario |
|---|---|
| 1. Market order with unrecognised venue | From 2026-11-05, BROKER_A routes VENUE_C flow through the new alias `ABC_ROUTER`, which registry v2.0 does not contain |
| 2. Legacy fallback | The reconstructed legacy normaliser labels the order `LEGACY`; 504 market orders in total. It also silently resolves the ambiguous alias `SMART` to the last-loaded venue |
| 3. New resolver | `order_type=MARKET`, `classification=MARKET`, `mapping_status=UNMAPPED`, plus a `UNKNOWN_DESTINATION` exception (HIGH, VENUE-001) |
| 4. Register and reprocess | Maker-checker (self-approval rejected) produces v2.1 (`ABC_ROUTER → VENUE_C`, broker-scoped and effective-dated) and v2.2 (withdraws the wrong `SMART` row). Raw payload hashes and the hash-chain head are unchanged; replay under v2.0 reproduces 100% of the original mappings; the registry change alters 0 classifications; 710 exceptions are resolved and reprocessed |
| 5. Lineage | Client → parent → child → execution → raw message, with payload hashes re-verified |
| 6. Venue TCA | ES / RS / AS at 100 ms–30 s, impact beyond touch, fee-adjusted cost and implementation shortfall, attributed to the *actual* venue, with `UNRESOLVED` kept separate. Production venue analytics saw only 40% of VENUE_C's fills |
| 7. Graph + SOR audit | The lead-lag graph recovers the planted leader (VENUE_C) and the edge directions; 11/11 graph benchmarks pass. Decision reconstruction finds both planted router defects (look-ahead features, stale weights). The feature-integrity check flags VENUE_C's recorded adverse-selection feature from exactly the session the alias changed. Shadow ΔIS vs baseline: integrity-corrected −0.14 bps, graph-enhanced −0.18 bps (95% CIs exclude 0) |
| 8. Evidence package | 12 §14 items, KPI scorecard, go-live gates and a ground-truth check (all 14 planted faults detected). `audit_execution_id` and `package_digest` are reproducible |

**Localisation finding.** The raw OrdType is always parsed correctly, so there
is no protocol-ingestion divergence. The classification changes at
**ORDER_TYPE_NORMALIZATION**. Holding the raw order type fixed, the label
depends on whether the venue resolved: 967/967 mapped market orders keep
MARKET, while 504/504 unmapped ones become LEGACY (Fisher p ≈ 0). The defect
is therefore a **COMBINATION** of a VENUE_REFERENCE_DATA gap and a normaliser
that lets venue resolution drive classification. "Influence" correlates with
LEGACY only because the SOR sends most aggressive flow to the most influential
venue, and the report labels that association as non-causal. One further order
shows a *separate* silent coercion: a missing OrdType was labelled OTHER.

**KPI and gate outcome.** 26 of 27 §12 KPIs pass. *Historical decision
reconstruction* is 99.89%, below its 100% target: the 2 planted router defects
are reported, not hidden. Gates 1–4 pass. Gate 5 is `SHADOW_ONLY — APPROVAL
REQUIRED` and Gate 6 is `PENDING_SIGN_OFF`. The evidence states that both need
human approval.

## Architecture (spec §2)

| Service | Module | Key controls |
|---|---|---|
| 01 Raw ingestion | `ingestion/` | Append-only SQLite store with UPDATE/DELETE triggers, SHA-256 hash chain, idempotent re-delivery, deterministic event IDs, FIX BodyLength/CheckSum/header validation, dictionary-driven enum and required-tag checks, unknown tags retained and flagged, clock drift, sequence gaps/resends/gap-fills |
| 02 Identity & schema | `identity/` | Bitemporal registry (business validity × registry version), exact matching only, broker-scoped aliases, AMBIGUOUS never picks, MIC validated against an ISO 10383 snapshot, maker-checker change control, exception queue with lifecycle and evidence |
| 03 OMS/EMS lineage | `execution/` | `classify_order(order_type)` has no venue input; actual venue only from LastMkt; set-based reconciliation (duplicates, conflicts, correction chains, busts, FIX 4.2 ExecTransType, cancels, overfills, CumQty cross-check); lineage tracing |
| 04 Market data | `tca/markout.py` | Strict as-of lookups; STALE and INCOMPLETE are explicit states, never zero |
| 05 Graph engine | `graph/` | Granger-style lead-lag on returns with own-lag and exogenous-factor controls, feed-latency adjustment, Bonferroni and effect-size floor, Katz out-reach influence with alternative-method agreement |
| 06 TCA engine | `tca/` | Side-adjusted ES = RS + AS, impact beyond touch, IS (execution / fees / opportunity), unknown fees never assumed zero |
| 07 Venue intelligence & SOR audit | `analytics/`, `router/` | Influence and risk kept separate (risk excludes centrality); contamination detector; stage-level defect localisation; decision reconstruction; feature integrity; three-policy shadow evaluation |
| 08 Governance & monitoring | `reports/`, `api/`, `deployment/` | Reproducible evidence package, KPI scorecard and gates, Prometheus metrics, VENUE-001…008 alert rules, Grafana dashboard |

```
config/        settings, FIX dictionary, venue registry v2.0, model weights, MIC snapshot
src/venue_integrity/
  schemas/     Contracts A–F (Pydantic, closed-world, immutable)
  ingestion/  identity/  execution/  graph/  tca/  analytics/  router/
  simulation/  synthetic scenario + ground-truth outcome model
  pipeline.py  end-to-end AuditPipeline     demo.py  §19 demonstration     cli.py  `via`
  reports/     evidence package, KPI scorecard, go-live gates
  api/         FastAPI app (auth, roles, rate limits, metrics)
database/      PostgreSQL schema + constraint checks
deployment/    Dockerfile, Prometheus config + alert rules, Grafana dashboard
tests/         unit, contract, property, integration, replay, fault_injection, quantitative, database
```

## Design decisions and deviations from the specification

These were deliberate departures from the spec's sketches, each made to honour
the spec's own principles:

1. **AS sign wording (§8.1).** The formulas are kept. The spec says positive AS
   means the midpoint "moves against the aggressor". With
   `AS = 2s(M_h − M_0)/M_0`, positive AS actually means the mid moved *in the
   trade's direction*, which is adverse to the liquidity provider. For an
   aggressive parent order it signals leakage or impact. The docstrings state
   this interpretation.
2. **Venue registry keys (§4.1).** `venue_registry.venue_id` is a per-row UUID
   in the sketch, which would give one venue several identities. The schema
   splits a stable `venues` entity from bitemporal `venue_alias_mappings` rows.
3. **ExecID scope (§4.1).** ExecIDs are unique per sender, so keys are
   `(source_system, exec_id)`.
4. **Router decision contract (§3.5).** Features are stored *per candidate*,
   not only for the selected venue, as §3.5 and §10.3 themselves require.
5. **Reference code sketches.** §8.2's `1 if BUY else -1` would silently treat
   any unknown side as a sell, so side parsing is strict. §6.2's sketch summed
   duplicates, so the dict helper de-duplicates by ExecID.
6. **Directed centrality (§7.2).** NetworkX eigenvector centrality and PageRank
   reward *incoming* edges. With the documented orientation (i → j means i
   leads j), influence is computed on outgoing reach. Katz out-reach is primary
   because it is well defined on DAGs, and the reversed PageRank and
   eigenvector measures are reported with their Kendall agreement.
7. **Common-factor control (§7.1).** Subtracting the cross-sectional mean of
   the venues destroys the leader's signal (the residuals become symmetric in
   lead and lag). Controls are instead the target's own lags plus an optional
   exogenous factor, lagged to 2×max_lag; the `exogenous_factor_control`
   benchmark checks this.
8. **Codes with no canonical type.** OrdType codes such as stop-limit (`4`)
   map to `OTHER` with the raw code retained. Missing or unknown codes map to
   `UNKNOWN → UNRESOLVED`.

## Limitations — what this is not yet

- **No live connectivity.** This is by design. There is no Kafka/Redpanda
  consumer; `ingestion/sources.py` documents the `RawCapture` contract an
  adapter must produce.
- **Storage.** The pipeline persists raw events in SQLite and keeps derived
  state in memory. `database/schema.sql` is validated against PostgreSQL 16
  (`tests/database`), but there is no persistence layer writing to it yet.
- **FIX coverage.** Repeating groups are reported as `DUPLICATE_TAG` rather
  than parsed. Cancel/replace chains are flagged `UNSUPPORTED_ORDER_EVENT`,
  not reconciled.
- **Reference data.** `config/mic_reference.csv` is an illustrative ISO 10383
  snapshot. Production needs the authoritative feed.
- **Model governance.** Model versions live in YAML; there is no MLflow
  integration.
- **Shadow evaluation.** Counterfactual outcomes come from the simulator's
  ground truth. Production replay needs an independently validated cost model.
  The graph-enhanced policy (regime-conditional AS with influence-dependent
  shrinkage) is an illustrative modelling choice: it helps in high-volatility
  sessions (−0.44 vs −0.23 bps) and is slightly worse than the corrected
  policy in low-volatility ones.
- **Scenario assumption.** The scenario *assumes* the production router's
  VENUE_C adverse-selection feature collapsed after the alias change. The audit
  detects this from the recorded features; it does not prove that a real feed
  behaves this way.
- **Performance.** The §12.5 latency targets have not been benchmarked.

## API (spec §15)

`uvicorn venue_integrity.api.main:create_app --factory` with `VIA_API_KEYS`
set (see `.env.example`). Roles are `reader`, `ingestor`, `admin` (propose
reference-data changes) and `approver`; maker-checker is enforced on
principals. There is a per-principal token-bucket rate limit.
`VIA_BOOTSTRAP=simulation` preloads the scenario.

| Endpoint | Role |
|---|---|
| `POST /events/ingest` | ingestor |
| `POST /venues/resolve`, `GET /venues/{venue_id}` | reader |
| `GET /audit/exceptions`, `GET /audit/reconciliation` | reader |
| `GET /analytics/venue-influence`, `GET /analytics/tca` | reader |
| `GET /router/decisions/{id}` (independent reconstruction) | reader |
| `POST /admin/venues/changes`, `POST /admin/venues/changes/{id}/approve` | admin / approver |
| `GET /health`, `GET /metrics` | none |

`docker compose up` (after creating `.env` from `.env.example`) starts the API,
PostgreSQL with the schema, Prometheus with the VENUE-001…008 alert rules, and
the Grafana "Venue Integrity Monitor" dashboard.

## Tests (spec §11)

| Suite | Covers |
|---|---|
| `unit/` | Spec §11.2 tests verbatim, resolver rules, parser, validator, sequence monitor, raw-store tamper detection, exception lifecycle, maker-checker, TCA identities, scoring, reconciliation |
| `contract/` | Contracts A–F: spec examples, closed-world models, invariants |
| `property/` | All eight §11.3 invariants with Hypothesis |
| `fault_injection/` | One test per row of the §11.4 matrix (13 rows) |
| `quantitative/` | §7.3 graph benchmarks across seeds, defect localisation, contamination, TCA attribution, venue intelligence, reconstruction, feature integrity, shadow evaluation |
| `integration/` | Handcrafted FIX end to end, CLI, API (auth, roles, maker-checker, rate limit, no order-entry routes, refuses live mode) |
| `replay/` | §11.5 determinism: same raw-chain head, same economic state under shuffled delivery, unresolved events stay visible, reproducible evidence package (`-m slow`) |
| `database/` | Applies `schema.sql` to a scratch database and runs `database/tests/constraint_checks.sql` |

```bash
pytest -m "not slow"                                   # fast suite (~30 s)
VIA_TEST_PG_DSN=postgresql://postgres@localhost/postgres pytest tests/database
```
