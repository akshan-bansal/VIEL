-- =============================================================================
-- Venue Integrity Audit — production PostgreSQL schema (PostgreSQL 14+)
-- =============================================================================
-- Implements spec §4.1 (core relational model) and §4.2 (additional production
-- requirements). Differences from the §4.1 sketch, and why:
--
-- * Venue identity is split into `venues` (stable entity) and
--   `venue_alias_mappings` (bitemporal alias rows). In the §4.1 sketch
--   `venue_registry.venue_id` is a per-row UUID, so one venue with several
--   aliases or versions would acquire several identities.
-- * ExecIDs are unique per sender, not globally: execution keys are
--   (source_system, exec_id).
-- * `execution_events.actual_venue_id` is nullable and constrained to be NULL
--   unless mapping_status = 'MAPPED' — it can never be populated from the
--   intended destination to satisfy a NOT NULL constraint.
-- * Classification has no LEGACY value and is checked against order_type.
-- * raw_events and every *_history table are append-only (triggers), and
--   raw events carry a SHA-256 hash chain.
-- * execution_events is partitioned by trade date.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS via;
SET search_path = via, public;

-- -----------------------------------------------------------------------------
-- Controlled vocabularies
-- -----------------------------------------------------------------------------
CREATE TYPE mapping_status AS ENUM ('MAPPED', 'UNMAPPED', 'AMBIGUOUS', 'INVALID');
CREATE TYPE order_type     AS ENUM ('MARKET', 'LIMIT', 'STOP', 'OTHER', 'UNKNOWN');
CREATE TYPE classification AS ENUM ('MARKET', 'LIMIT', 'OTHER', 'UNRESOLVED');
CREATE TYPE side           AS ENUM ('BUY', 'SELL', 'SELL_SHORT', 'SELL_SHORT_EXEMPT');
CREATE TYPE exec_event_type AS ENUM (
    'NEW', 'TRADE', 'TRADE_CORRECT', 'TRADE_CANCEL', 'CANCELED', 'REPLACED', 'REJECTED', 'EXPIRED',
    'DONE_FOR_DAY', 'PENDING_NEW', 'PENDING_CANCEL', 'PENDING_REPLACE', 'RESTATED', 'ORDER_STATUS');
CREATE TYPE severity         AS ENUM ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO');
CREATE TYPE exception_status AS ENUM ('OPEN', 'ASSIGNED', 'RESOLVED', 'REPROCESSED', 'CLOSED', 'WAIVED');
CREATE TYPE change_status    AS ENUM ('PROPOSED', 'APPROVED', 'REJECTED');

-- -----------------------------------------------------------------------------
-- Append-only guard
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

-- -----------------------------------------------------------------------------
-- Reference data (bitemporal)
-- -----------------------------------------------------------------------------
CREATE TABLE mic_reference_snapshots (
    snapshot_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT NOT NULL,
    sha256          CHAR(64) NOT NULL UNIQUE,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE mic_reference (
    snapshot_id     UUID NOT NULL REFERENCES mic_reference_snapshots(snapshot_id),
    mic             CHAR(4) NOT NULL CHECK (mic ~ '^[A-Z0-9]{4}$'),
    operating_mic   CHAR(4) NOT NULL,
    oprt_sgmt       VARCHAR(4) NOT NULL,
    name            TEXT NOT NULL,
    country         CHAR(2),
    status          VARCHAR(16) NOT NULL,
    PRIMARY KEY (snapshot_id, mic)
);

CREATE TABLE registry_versions (
    version             VARCHAR(32) PRIMARY KEY,
    version_seq         INTEGER NOT NULL UNIQUE,          -- system-time order
    created_at          TIMESTAMPTZ NOT NULL,
    approved_by         VARCHAR(128) NOT NULL,
    change_request_id   UUID,
    description         TEXT NOT NULL
);

CREATE TABLE venues (
    venue_id        VARCHAR(64) PRIMARY KEY,               -- stable business identity, e.g. VENUE_C
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE venue_alias_mappings (
    mapping_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id                VARCHAR(64) NOT NULL REFERENCES venues(venue_id),
    mic                     CHAR(4) CHECK (mic ~ '^[A-Z0-9]{4}$'),
    operating_mic           CHAR(4),
    identifier              VARCHAR(64) NOT NULL,          -- exact raw value, no normalisation
    identifier_type         VARCHAR(8) NOT NULL CHECK (identifier_type IN ('ALIAS', 'MIC')),
    source_system           VARCHAR(64),                    -- NULL = global; else broker-specific alias
    venue_name              VARCHAR(128),
    venue_type              VARCHAR(32) NOT NULL,
    country                 CHAR(2),
    status                  VARCHAR(16) NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'RETIRED')),
    -- business time: when the identifier meant this venue
    valid_from              TIMESTAMPTZ NOT NULL,
    valid_to                TIMESTAMPTZ,
    -- system time: which registry versions contain the row
    introduced_in_version   VARCHAR(32) NOT NULL REFERENCES registry_versions(version),
    superseded_in_version   VARCHAR(32) REFERENCES registry_versions(version),
    approved_by             VARCHAR(128) NOT NULL,
    change_request_id       UUID,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (superseded_in_version IS NULL OR superseded_in_version <> introduced_in_version)
);
-- Ambiguity is allowed to exist (it must be detectable and reported, not hidden),
-- so there is intentionally no exclusion constraint across venues. Within one
-- venue, overlapping identical rows in the same version are prevented:
CREATE UNIQUE INDEX uq_alias_row ON venue_alias_mappings
    (venue_id, identifier, identifier_type, COALESCE(source_system, ''), valid_from, introduced_in_version);
CREATE INDEX idx_alias_lookup ON venue_alias_mappings (identifier, identifier_type, source_system);

-- Only supersession may change an alias row; everything else is immutable.
CREATE OR REPLACE FUNCTION alias_supersede_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'venue_alias_mappings rows cannot be deleted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.superseded_in_version IS NOT NULL
       OR (to_jsonb(NEW) - 'superseded_in_version') <> (to_jsonb(OLD) - 'superseded_in_version') THEN
        RAISE EXCEPTION 'venue_alias_mappings rows are immutable except for one-time supersession'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_alias_supersede_only BEFORE UPDATE OR DELETE ON venue_alias_mappings
    FOR EACH ROW EXECUTE FUNCTION alias_supersede_only();

CREATE TABLE mapping_change_requests (
    request_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposed_by         VARCHAR(128) NOT NULL,
    proposed_at         TIMESTAMPTZ NOT NULL,
    description         TEXT NOT NULL,
    evidence            TEXT NOT NULL CHECK (length(evidence) > 0),
    payload             JSONB NOT NULL,
    status              change_status NOT NULL DEFAULT 'PROPOSED',
    decided_by          VARCHAR(128),
    decided_at          TIMESTAMPTZ,
    resulting_version   VARCHAR(32) REFERENCES registry_versions(version),
    -- maker-checker: the approver can never be the proposer
    CHECK (decided_by IS NULL OR decided_by <> proposed_by),
    CHECK ((status = 'PROPOSED') = (decided_by IS NULL))
);

-- Resolve an identifier with (registry version, event time): spec §11.3 invariant 7.
CREATE OR REPLACE FUNCTION resolve_identifier(
    p_identifier TEXT, p_identifier_type TEXT, p_source_system TEXT,
    p_event_time TIMESTAMPTZ, p_version TEXT)
RETURNS TABLE (venue_id VARCHAR, mic CHAR(4), source_system VARCHAR, status VARCHAR)
LANGUAGE sql STABLE AS $$
    SELECT m.venue_id, m.mic, m.source_system, m.status
    FROM venue_alias_mappings m
    JOIN registry_versions vi ON vi.version = m.introduced_in_version
    LEFT JOIN registry_versions vs ON vs.version = m.superseded_in_version
    JOIN registry_versions vq ON vq.version = p_version
    WHERE m.identifier = p_identifier
      AND m.identifier_type = p_identifier_type
      AND (m.source_system IS NULL OR m.source_system = p_source_system)
      AND vi.version_seq <= vq.version_seq
      AND (vs.version_seq IS NULL OR vs.version_seq > vq.version_seq)
      AND m.valid_from <= p_event_time
      AND (m.valid_to IS NULL OR p_event_time < m.valid_to);
$$;

-- -----------------------------------------------------------------------------
-- Raw events (Contract A): immutable, hash-chained, idempotent
-- -----------------------------------------------------------------------------
CREATE TABLE raw_events (
    chain_index         BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    event_id            UUID PRIMARY KEY,
    source_system       VARCHAR(64) NOT NULL,
    session_id          VARCHAR(128),
    sequence_number     BIGINT CHECK (sequence_number >= 0),
    received_at         TIMESTAMPTZ NOT NULL,
    source_timestamp    TIMESTAMPTZ,
    source_timestamp_ns BIGINT,                          -- exact source precision
    protocol            VARCHAR(32),
    message_type        VARCHAR(16),
    raw_payload         TEXT NOT NULL,
    payload_hash        CHAR(64) NOT NULL CHECK (payload_hash = encode(digest(raw_payload, 'sha256'), 'hex')),
    schema_version      VARCHAR(32) NOT NULL,
    prev_chain_hash     CHAR(64) NOT NULL,
    chain_hash          CHAR(64) NOT NULL,
    validation_status   VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    -- idempotency: identical bytes on the same session/sequence are one event
    UNIQUE (source_system, session_id, sequence_number, payload_hash)
);
CREATE INDEX idx_raw_received ON raw_events (received_at);
CREATE INDEX idx_raw_session_seq ON raw_events (source_system, session_id, sequence_number);
CREATE TRIGGER trg_raw_events_append_only BEFORE UPDATE OR DELETE ON raw_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE raw_delivery_log (
    delivery_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        UUID NOT NULL REFERENCES raw_events(event_id),
    received_at     TIMESTAMPTZ NOT NULL,
    outcome         VARCHAR(24) NOT NULL CHECK (outcome IN ('STORED', 'DUPLICATE_DELIVERY'))
);
CREATE TRIGGER trg_delivery_append_only BEFORE UPDATE OR DELETE ON raw_delivery_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Hash chain: computed by the database so a writer cannot forge it.
CREATE OR REPLACE FUNCTION raw_events_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev CHAR(64);
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('via.raw_events.chain'));
    SELECT chain_hash INTO prev FROM raw_events ORDER BY chain_index DESC LIMIT 1;
    NEW.prev_chain_hash := COALESCE(prev, repeat('0', 64));
    NEW.chain_hash := encode(digest(NEW.prev_chain_hash || '|' || NEW.event_id::text || '|' || NEW.payload_hash, 'sha256'), 'hex');
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_raw_events_chain BEFORE INSERT ON raw_events
    FOR EACH ROW EXECUTE FUNCTION raw_events_chain();

CREATE TABLE sequence_gaps (
    gap_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system   VARCHAR(64) NOT NULL,
    session_id      VARCHAR(128) NOT NULL,
    gap_start       BIGINT NOT NULL,
    gap_end         BIGINT NOT NULL CHECK (gap_end >= gap_start),
    detected_at     TIMESTAMPTZ NOT NULL,
    detected_by_event UUID REFERENCES raw_events(event_id),
    recovered_at    TIMESTAMPTZ
);

-- -----------------------------------------------------------------------------
-- Normalised orders (Contract C) — one row per normalisation run and registry version
-- -----------------------------------------------------------------------------
CREATE TABLE normalized_orders (
    event_id            UUID NOT NULL REFERENCES raw_events(event_id),
    registry_version    VARCHAR(32) NOT NULL REFERENCES registry_versions(version),
    normalizer_version  VARCHAR(32) NOT NULL,
    source_system       VARCHAR(64) NOT NULL,
    session_id          VARCHAR(128),
    cl_ord_id           VARCHAR(128) NOT NULL,
    orig_cl_ord_id      VARCHAR(128),
    client_order_id     VARCHAR(128),               -- pseudonymised before storage where required
    parent_order_id     VARCHAR(128),
    child_order_id      VARCHAR(128) NOT NULL,
    instrument_id       VARCHAR(64) NOT NULL,
    side                side NOT NULL,
    order_quantity      NUMERIC(24,8) NOT NULL CHECK (order_quantity > 0),
    limit_price         NUMERIC(24,10),
    raw_ord_type        VARCHAR(4),
    order_type          order_type NOT NULL,
    classification      classification NOT NULL,
    time_in_force       VARCHAR(16),
    raw_ex_destination  VARCHAR(64),
    intended_venue_id   VARCHAR(64) REFERENCES venues(venue_id),
    mapping_status      mapping_status NOT NULL,
    mapping_reason      VARCHAR(64) NOT NULL,
    route_category      VARCHAR(32),               -- explicitly governed business tag only
    event_time          TIMESTAMPTZ NOT NULL,
    normalized_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, registry_version, normalizer_version),
    -- Classification is a pure function of order type (spec §5.2)
    CHECK ((order_type, classification) IN (
        ('MARKET'::order_type, 'MARKET'::classification), ('LIMIT', 'LIMIT'), ('STOP', 'OTHER'),
        ('OTHER', 'OTHER'), ('UNKNOWN', 'UNRESOLVED'))),
    -- No silent venue fallback
    CHECK ((mapping_status = 'MAPPED') = (intended_venue_id IS NOT NULL)),
    -- 'LEGACY' can never be a classification; a governed route category may not be produced by a lookup failure
    CHECK (route_category IS NULL OR route_category <> 'LEGACY' OR mapping_status = 'MAPPED')
);
CREATE INDEX idx_orders_parent ON normalized_orders (parent_order_id);
CREATE INDEX idx_orders_child ON normalized_orders (source_system, cl_ord_id);
CREATE TRIGGER trg_orders_append_only BEFORE UPDATE OR DELETE ON normalized_orders
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- Execution events (Contract D) — partitioned by trade date
-- -----------------------------------------------------------------------------
CREATE TABLE execution_events (
    source_system       VARCHAR(64) NOT NULL,
    exec_id             VARCHAR(128) NOT NULL,
    source_event_id     UUID NOT NULL,             -- FK enforced by trigger (partitioned table)
    registry_version    VARCHAR(32) NOT NULL,
    trade_date          DATE NOT NULL,
    exec_type           exec_event_type NOT NULL,
    exec_ref_id         VARCHAR(128),
    session_id          VARCHAR(128),
    sequence_number     BIGINT,
    poss_dup            BOOLEAN NOT NULL DEFAULT FALSE,
    order_id            VARCHAR(128),
    parent_order_id     VARCHAR(128),
    child_order_id      VARCHAR(128),
    instrument_id       VARCHAR(64) NOT NULL,
    side                side NOT NULL,
    raw_last_mkt        VARCHAR(64),
    actual_venue_id     VARCHAR(64),
    venue_mic           CHAR(4),
    mapping_status      mapping_status NOT NULL,
    mapping_reason      VARCHAR(128) NOT NULL,
    executed_quantity   NUMERIC(24,8) NOT NULL CHECK (executed_quantity >= 0),
    execution_price     NUMERIC(24,10),
    cum_qty             NUMERIC(24,8),
    leaves_qty          NUMERIC(24,8),
    execution_timestamp TIMESTAMPTZ NOT NULL,
    execution_timestamp_ns BIGINT NOT NULL,
    payload_hash        CHAR(64) NOT NULL,
    -- one row per raw message; duplicate ExecIDs are retained and detected, not overwritten
    PRIMARY KEY (source_event_id, registry_version, trade_date),
    -- the actual venue is a fact from LastMkt; it is NULL unless MAPPED (never back-filled from intent)
    CHECK ((mapping_status = 'MAPPED') = (actual_venue_id IS NOT NULL)),
    CHECK (exec_type NOT IN ('TRADE', 'TRADE_CORRECT') OR (executed_quantity > 0 AND execution_price > 0)),
    CHECK (exec_type NOT IN ('TRADE_CORRECT', 'TRADE_CANCEL') OR exec_ref_id IS NOT NULL)
) PARTITION BY RANGE (trade_date);
CREATE INDEX idx_execution_exec ON execution_events (source_system, exec_id);
CREATE INDEX idx_execution_parent ON execution_events (parent_order_id);
CREATE INDEX idx_execution_time ON execution_events (execution_timestamp);
CREATE INDEX idx_execution_mapping ON execution_events (mapping_status);
CREATE TABLE execution_events_2026_10 PARTITION OF execution_events FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE execution_events_2026_11 PARTITION OF execution_events FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE execution_events_2026_12 PARTITION OF execution_events FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE execution_events_default PARTITION OF execution_events DEFAULT;

CREATE OR REPLACE FUNCTION execution_source_exists() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM raw_events r WHERE r.event_id = NEW.source_event_id AND r.payload_hash = NEW.payload_hash) THEN
        RAISE EXCEPTION 'execution % has no matching raw source event', NEW.exec_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_execution_source BEFORE INSERT ON execution_events
    FOR EACH ROW EXECUTE FUNCTION execution_source_exists();
CREATE TRIGGER trg_execution_append_only BEFORE UPDATE OR DELETE ON execution_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Reconciled economic state (derived, versioned by run).
CREATE TABLE reconciliation_runs (
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_execution_id  UUID NOT NULL,
    registry_version    VARCHAR(32) NOT NULL REFERENCES registry_versions(version),
    economic_state_sha256 CHAR(64) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE effective_executions (
    run_id              UUID NOT NULL REFERENCES reconciliation_runs(run_id),
    source_system       VARCHAR(64) NOT NULL,
    original_exec_id    VARCHAR(128) NOT NULL,
    effective_exec_id   VARCHAR(128) NOT NULL,
    chain               TEXT[] NOT NULL,
    status              VARCHAR(16) NOT NULL CHECK (status IN ('ACTIVE', 'BUSTED', 'AMBIGUOUS')),
    quantity            NUMERIC(24,8) NOT NULL,
    price               NUMERIC(24,10),
    actual_venue_id     VARCHAR(64),
    mapping_status      mapping_status NOT NULL,
    child_order_id      VARCHAR(128),
    parent_order_id     VARCHAR(128),
    lineage_status      VARCHAR(24) NOT NULL,
    PRIMARY KEY (run_id, source_system, original_exec_id),   -- no double counting within a run
    CHECK (status = 'ACTIVE' OR quantity = 0),
    CHECK ((mapping_status = 'MAPPED') = (actual_venue_id IS NOT NULL))
);

-- -----------------------------------------------------------------------------
-- Router decisions (Contract E) and venue metrics (Contract F)
-- -----------------------------------------------------------------------------
CREATE TABLE router_decisions (
    decision_id             VARCHAR(128) PRIMARY KEY,
    parent_order_id         VARCHAR(128) NOT NULL,
    child_order_id          VARCHAR(128),
    decision_timestamp      TIMESTAMPTZ NOT NULL,
    candidate_venues        JSONB NOT NULL CHECK (jsonb_typeof(candidate_venues) = 'array'
                                                  AND jsonb_array_length(candidate_venues) > 0),
    excluded_venues         JSONB NOT NULL DEFAULT '[]',     -- [{venue_id, reason}]
    decision_features       JSONB NOT NULL,                   -- per candidate, point-in-time
    selected_venue_id       VARCHAR(64) REFERENCES venues(venue_id),
    router_score            DOUBLE PRECISION,
    router_model_version    VARCHAR(32) NOT NULL,
    reference_data_version  VARCHAR(32) NOT NULL REFERENCES registry_versions(version),
    decision_reason         TEXT NOT NULL,
    fallbacks_applied       JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_router_parent ON router_decisions (parent_order_id);
CREATE TRIGGER trg_router_append_only BEFORE UPDATE OR DELETE ON router_decisions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE venue_metrics (
    venue_id                VARCHAR(64) NOT NULL REFERENCES venues(venue_id),
    calculation_timestamp   TIMESTAMPTZ NOT NULL,
    model_version           VARCHAR(32) NOT NULL,
    window_start            TIMESTAMPTZ NOT NULL,
    window_end              TIMESTAMPTZ NOT NULL CHECK (window_end > window_start),
    graph_influence         NUMERIC(12,8) CHECK (graph_influence BETWEEN 0 AND 1),
    lead_lag_score          NUMERIC(12,8),
    adverse_selection_bps   NUMERIC(18,8),
    adverse_selection_score NUMERIC(12,8),
    liquidity_score         NUMERIC(12,8),
    market_impact_bps       NUMERIC(18,8),
    latency_risk            NUMERIC(12,8),
    data_quality_score      NUMERIC(12,8) NOT NULL CHECK (data_quality_score BETWEEN 0 AND 1),
    venue_risk              NUMERIC(12,8),
    market_regime           VARCHAR(24) NOT NULL,
    observations            INTEGER NOT NULL CHECK (observations >= 0),
    confidence              NUMERIC(12,8) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    exclusions              JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (venue_id, calculation_timestamp, model_version)
);

-- -----------------------------------------------------------------------------
-- Exception management (spec §5.3) with append-only history
-- -----------------------------------------------------------------------------
CREATE TABLE exceptions (
    exception_id        UUID PRIMARY KEY,
    exception_type      VARCHAR(48) NOT NULL,
    severity            severity NOT NULL,
    alert_code          VARCHAR(16),
    detail              TEXT NOT NULL,
    source_event_id     UUID REFERENCES raw_events(event_id),
    entity_key          VARCHAR(256),
    raw_value           TEXT,
    context             JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL,
    status              exception_status NOT NULL DEFAULT 'OPEN',
    owner               VARCHAR(128),
    root_cause          VARCHAR(48),
    resolution_evidence TEXT,
    reprocessing_outcome TEXT,
    occurrences         INTEGER NOT NULL DEFAULT 1,
    CHECK (status NOT IN ('RESOLVED', 'REPROCESSED', 'CLOSED')
           OR (root_cause IS NOT NULL AND length(coalesce(resolution_evidence, '')) > 0))
);
CREATE INDEX idx_exceptions_open ON exceptions (status, severity) WHERE status IN ('OPEN', 'ASSIGNED');
CREATE TABLE exception_history (
    history_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    exception_id    UUID NOT NULL REFERENCES exceptions(exception_id),
    at              TIMESTAMPTZ NOT NULL,
    action          VARCHAR(24) NOT NULL,
    actor           VARCHAR(128) NOT NULL,
    details         JSONB NOT NULL DEFAULT '{}'
);
CREATE TRIGGER trg_exception_history_append_only BEFORE UPDATE OR DELETE ON exception_history
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- Audit runs (spec §14 evidence package)
-- -----------------------------------------------------------------------------
CREATE TABLE audit_runs (
    audit_execution_id  UUID PRIMARY KEY,
    package_digest      CHAR(64) NOT NULL,
    package             JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TRIGGER trg_audit_runs_append_only BEFORE UPDATE OR DELETE ON audit_runs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- -----------------------------------------------------------------------------
-- Role separation (spec §4.2): ingestion, analytics, administration
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'via_ingest') THEN CREATE ROLE via_ingest NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'via_analytics') THEN CREATE ROLE via_analytics NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'via_refdata') THEN CREATE ROLE via_refdata NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'via_schema_admin') THEN CREATE ROLE via_schema_admin NOLOGIN; END IF;
END
$$;
GRANT USAGE ON SCHEMA via TO via_ingest, via_analytics, via_refdata;
-- ingestion: append raw events only
GRANT SELECT, INSERT ON raw_events, raw_delivery_log, sequence_gaps TO via_ingest;
-- analytics: read everything, write derived tables only
GRANT SELECT ON ALL TABLES IN SCHEMA via TO via_analytics;
GRANT INSERT ON normalized_orders, execution_events, reconciliation_runs, effective_executions,
    venue_metrics, exceptions, exception_history, audit_runs TO via_analytics;
GRANT UPDATE (status, owner, root_cause, resolution_evidence, reprocessing_outcome, occurrences)
    ON exceptions TO via_analytics;
-- reference data: proposals and approved versions, no access to trading data
GRANT SELECT, INSERT ON mapping_change_requests, registry_versions, venues, venue_alias_mappings,
    mic_reference_snapshots, mic_reference TO via_refdata;
GRANT UPDATE (status, decided_by, decided_at, resulting_version) ON mapping_change_requests TO via_refdata;
GRANT UPDATE (superseded_in_version) ON venue_alias_mappings TO via_refdata;
-- administration: schema owner duties only (DDL, partition maintenance, retention)
GRANT USAGE, CREATE ON SCHEMA via TO via_schema_admin;
