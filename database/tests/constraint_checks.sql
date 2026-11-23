-- Constraint checks for database/schema.sql. Run against a database where the
-- schema has just been applied:  psql -v ON_ERROR_STOP=1 -f constraint_checks.sql
-- Every block either succeeds silently or raises, failing the run.
SET search_path = via, public;

-- fixtures -------------------------------------------------------------------
INSERT INTO registry_versions VALUES ('2.0', 1, '2026-10-01Z', 'REFERENCE_DATA_CONTROL', NULL, 'initial');
INSERT INTO registry_versions VALUES ('2.1', 2, '2026-11-20Z', 'refdata.controller', NULL, 'add ABC_ROUTER');
INSERT INTO venues (venue_id) VALUES ('VENUE_A'), ('VENUE_C'), ('VENUE_D');
INSERT INTO venue_alias_mappings (venue_id, mic, identifier, identifier_type, source_system, venue_type, status,
                                  valid_from, introduced_in_version, approved_by)
VALUES ('VENUE_C', 'XABC', 'ABC', 'ALIAS', NULL, 'EXCHANGE', 'ACTIVE', '2026-01-01Z', '2.0', 'REFERENCE_DATA_CONTROL'),
       ('VENUE_C', 'XABC', 'ABC_ROUTER', 'ALIAS', 'BROKER_A', 'EXCHANGE', 'ACTIVE', '2026-11-04Z', '2.1', 'refdata.controller'),
       ('VENUE_A', 'XVNA', 'SMART', 'ALIAS', 'BROKER_B', 'EXCHANGE', 'ACTIVE', '2026-01-01Z', '2.0', 'REFERENCE_DATA_CONTROL'),
       ('VENUE_D', 'XVND', 'SMART', 'ALIAS', 'BROKER_B', 'ATS', 'ACTIVE', '2026-01-01Z', '2.0', 'REFERENCE_DATA_CONTROL');

INSERT INTO raw_events (event_id, source_system, session_id, sequence_number, received_at, raw_payload, payload_hash, schema_version,
                        prev_chain_hash, chain_hash)
VALUES ('00000000-0000-0000-0000-000000000001', 'BROKER_A', 'S1', 1, '2026-11-05T14:30:42Z', '8=FIX.4.4|35=D|',
        encode(digest('8=FIX.4.4|35=D|', 'sha256'), 'hex'), '1.0', 'x', 'x'),
       ('00000000-0000-0000-0000-000000000002', 'BROKER_A', 'S1', 2, '2026-11-05T14:30:43Z', '8=FIX.4.4|35=8|',
        encode(digest('8=FIX.4.4|35=8|', 'sha256'), 'hex'), '1.0', 'x', 'x');

-- 1. the database computes the hash chain (writer-supplied values are ignored)
DO $$
DECLARE r RECORD; prev CHAR(64) := repeat('0', 64);
BEGIN
    FOR r IN SELECT * FROM raw_events ORDER BY chain_index LOOP
        IF r.prev_chain_hash <> prev OR r.chain_hash <> encode(digest(prev || '|' || r.event_id::text || '|' || r.payload_hash, 'sha256'), 'hex') THEN
            RAISE EXCEPTION 'hash chain broken at %', r.chain_index;
        END IF;
        prev := r.chain_hash;
    END LOOP;
END $$;

-- 2. raw events are append-only and their hash must match the payload
DO $$ BEGIN
    BEGIN UPDATE raw_events SET raw_payload = 'tampered'; RAISE EXCEPTION 'FAIL: update allowed';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN DELETE FROM raw_events; RAISE EXCEPTION 'FAIL: delete allowed';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN
        INSERT INTO raw_events (event_id, source_system, received_at, raw_payload, payload_hash, schema_version, prev_chain_hash, chain_hash)
        VALUES (gen_random_uuid(), 'BROKER_A', NOW(), 'abc', repeat('0', 64), '1.0', 'x', 'x');
        RAISE EXCEPTION 'FAIL: wrong payload hash accepted';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO raw_events (event_id, source_system, session_id, sequence_number, received_at, raw_payload, payload_hash, schema_version, prev_chain_hash, chain_hash)
        VALUES (gen_random_uuid(), 'BROKER_A', 'S1', 1, NOW(), '8=FIX.4.4|35=D|', encode(digest('8=FIX.4.4|35=D|', 'sha256'), 'hex'), '1.0', 'x', 'x');
        RAISE EXCEPTION 'FAIL: duplicate delivery stored twice';
    EXCEPTION WHEN unique_violation THEN NULL; END;
END $$;

-- 3. classification is a function of order type; LEGACY does not exist; no silent venue fallback
DO $$ BEGIN
    INSERT INTO normalized_orders (event_id, registry_version, normalizer_version, source_system, cl_ord_id, child_order_id,
        instrument_id, side, order_quantity, raw_ord_type, order_type, classification, raw_ex_destination,
        intended_venue_id, mapping_status, mapping_reason, event_time, normalized_at)
    VALUES ('00000000-0000-0000-0000-000000000001', '2.0', '1.0.0', 'BROKER_A', 'C1', 'C1', 'X', 'BUY', 700, '1',
            'MARKET', 'MARKET', 'ABC_ROUTER', NULL, 'UNMAPPED', 'NO_MATCHING_IDENTIFIER', NOW(), NOW());
    BEGIN
        INSERT INTO normalized_orders (event_id, registry_version, normalizer_version, source_system, cl_ord_id, child_order_id,
            instrument_id, side, order_quantity, raw_ord_type, order_type, classification, intended_venue_id, mapping_status,
            mapping_reason, event_time, normalized_at)
        VALUES ('00000000-0000-0000-0000-000000000001', '2.1', '1.0.0', 'BROKER_A', 'C1', 'C1', 'X', 'BUY', 700, '1',
                'MARKET', 'OTHER', 'VENUE_C', 'MAPPED', 'MAPPED', NOW(), NOW());
        RAISE EXCEPTION 'FAIL: market order classified OTHER';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO normalized_orders (event_id, registry_version, normalizer_version, source_system, cl_ord_id, child_order_id,
            instrument_id, side, order_quantity, raw_ord_type, order_type, classification, intended_venue_id, mapping_status,
            mapping_reason, event_time, normalized_at)
        VALUES ('00000000-0000-0000-0000-000000000001', '2.1', '1.0.0', 'BROKER_A', 'C1', 'C1', 'X', 'BUY', 700, '1',
                'MARKET', 'MARKET', 'VENUE_C', 'UNMAPPED', 'NO_MATCHING_IDENTIFIER', NOW(), NOW());
        RAISE EXCEPTION 'FAIL: unmapped order carries a venue';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        PERFORM 'LEGACY'::classification;
        RAISE EXCEPTION 'FAIL: LEGACY accepted as a classification';
    EXCEPTION WHEN invalid_text_representation THEN NULL; END;
END $$;

-- 4. executions: venue only when MAPPED, raw source required, busts need a reference
DO $$ BEGIN
    INSERT INTO execution_events (source_system, exec_id, source_event_id, registry_version, trade_date, exec_type, instrument_id,
        side, raw_last_mkt, actual_venue_id, mapping_status, mapping_reason, executed_quantity, execution_price,
        execution_timestamp, execution_timestamp_ns, payload_hash)
    VALUES ('BROKER_A', 'E1', '00000000-0000-0000-0000-000000000002', '2.0', '2026-11-05', 'TRADE', 'X', 'BUY', NULL, NULL,
            'UNMAPPED', 'MIC:MISSING_IDENTIFIER', 500, 101.25, '2026-11-05T14:30:42Z', 0,
            encode(digest('8=FIX.4.4|35=8|', 'sha256'), 'hex'));
    BEGIN
        INSERT INTO execution_events (source_system, exec_id, source_event_id, registry_version, trade_date, exec_type, instrument_id,
            side, actual_venue_id, mapping_status, mapping_reason, executed_quantity, execution_price, execution_timestamp,
            execution_timestamp_ns, payload_hash)
        VALUES ('BROKER_A', 'E2', '00000000-0000-0000-0000-000000000002', '2.1', '2026-11-05', 'TRADE', 'X', 'BUY', 'VENUE_C',
                'UNMAPPED', 'MIC:MISSING_IDENTIFIER', 500, 101.25, NOW(), 0, encode(digest('8=FIX.4.4|35=8|', 'sha256'), 'hex'));
        RAISE EXCEPTION 'FAIL: unresolved execution venue back-filled';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN
        INSERT INTO execution_events (source_system, exec_id, source_event_id, registry_version, trade_date, exec_type, instrument_id,
            side, mapping_status, mapping_reason, executed_quantity, execution_price, execution_timestamp, execution_timestamp_ns, payload_hash)
        VALUES ('BROKER_A', 'E3', gen_random_uuid(), '2.0', '2026-11-05', 'TRADE', 'X', 'BUY', 'UNMAPPED', 'x', 1, 1, NOW(), 0, repeat('0', 64));
        RAISE EXCEPTION 'FAIL: execution without raw source accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL; END;
    BEGIN
        INSERT INTO execution_events (source_system, exec_id, source_event_id, registry_version, trade_date, exec_type, instrument_id,
            side, mapping_status, mapping_reason, executed_quantity, execution_timestamp, execution_timestamp_ns, payload_hash)
        VALUES ('BROKER_A', 'E4', '00000000-0000-0000-0000-000000000002', '2.0', '2026-11-05', 'TRADE_CANCEL', 'X', 'BUY',
                'UNMAPPED', 'x', 0, NOW(), 0, encode(digest('8=FIX.4.4|35=8|', 'sha256'), 'hex'));
        RAISE EXCEPTION 'FAIL: bust without ExecRefID accepted';
    EXCEPTION WHEN check_violation THEN NULL; END;
    BEGIN UPDATE execution_events SET executed_quantity = 1000; RAISE EXCEPTION 'FAIL: execution update allowed';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;

-- 5. maker-checker and immutable reference data
DO $$ BEGIN
    INSERT INTO mapping_change_requests (proposed_by, proposed_at, description, evidence, payload)
    VALUES ('refdata.analyst', NOW(), 'add alias', 'broker notes', '{}');
    BEGIN
        UPDATE mapping_change_requests SET status = 'APPROVED', decided_by = 'refdata.analyst', decided_at = NOW();
        RAISE EXCEPTION 'FAIL: proposer approved own change';
    EXCEPTION WHEN check_violation THEN NULL; END;
    UPDATE mapping_change_requests SET status = 'APPROVED', decided_by = 'refdata.controller', decided_at = NOW();
    BEGIN
        UPDATE venue_alias_mappings SET venue_id = 'VENUE_A' WHERE identifier = 'ABC';
        RAISE EXCEPTION 'FAIL: alias row edited in place';
    EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    UPDATE venue_alias_mappings SET superseded_in_version = '2.1' WHERE identifier = 'SMART' AND venue_id = 'VENUE_D';
    BEGIN
        UPDATE venue_alias_mappings SET superseded_in_version = '2.0' WHERE identifier = 'SMART' AND venue_id = 'VENUE_D';
        RAISE EXCEPTION 'FAIL: supersession changed twice';
    EXCEPTION WHEN insufficient_privilege OR check_violation THEN NULL; END;
END $$;

-- 6. bitemporal resolution: version and event time both matter
DO $$
DECLARE n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM resolve_identifier('ABC_ROUTER', 'ALIAS', 'BROKER_A', '2026-11-10Z', '2.0');
    IF n <> 0 THEN RAISE EXCEPTION 'FAIL: ABC_ROUTER resolved under 2.0'; END IF;
    SELECT count(*) INTO n FROM resolve_identifier('ABC_ROUTER', 'ALIAS', 'BROKER_A', '2026-11-10Z', '2.1');
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL: ABC_ROUTER not resolved under 2.1'; END IF;
    SELECT count(*) INTO n FROM resolve_identifier('ABC_ROUTER', 'ALIAS', 'BROKER_A', '2026-11-01Z', '2.1');
    IF n <> 0 THEN RAISE EXCEPTION 'FAIL: ABC_ROUTER resolved before it was valid'; END IF;
    SELECT count(*) INTO n FROM resolve_identifier('ABC_ROUTER', 'ALIAS', 'BROKER_B', '2026-11-10Z', '2.1');
    IF n <> 0 THEN RAISE EXCEPTION 'FAIL: broker-specific alias leaked to another broker'; END IF;
    SELECT count(DISTINCT venue_id) INTO n FROM resolve_identifier('SMART', 'ALIAS', 'BROKER_B', '2026-11-10Z', '2.0');
    IF n <> 2 THEN RAISE EXCEPTION 'FAIL: ambiguity under 2.0 not visible'; END IF;
    SELECT count(DISTINCT venue_id) INTO n FROM resolve_identifier('SMART', 'ALIAS', 'BROKER_B', '2026-11-10Z', '2.1');
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL: supersession not applied in 2.1'; END IF;
END $$;

-- 7. reconciled state cannot double count or carry busted quantity
DO $$
DECLARE run UUID;
BEGIN
    INSERT INTO reconciliation_runs (audit_execution_id, registry_version, economic_state_sha256)
    VALUES (gen_random_uuid(), '2.1', repeat('a', 64)) RETURNING run_id INTO run;
    INSERT INTO effective_executions VALUES (run, 'BROKER_A', 'E1', 'E1', ARRAY['E1'], 'ACTIVE', 500, 101.25, NULL,
                                             'UNMAPPED', 'C1', 'P1', 'COMPLETE');
    BEGIN
        INSERT INTO effective_executions VALUES (run, 'BROKER_A', 'E1', 'E1', ARRAY['E1'], 'ACTIVE', 500, 101.25, NULL,
                                                 'UNMAPPED', 'C1', 'P1', 'COMPLETE');
        RAISE EXCEPTION 'FAIL: duplicate execution counted twice';
    EXCEPTION WHEN unique_violation THEN NULL; END;
    BEGIN
        INSERT INTO effective_executions VALUES (run, 'BROKER_A', 'E9', 'E10', ARRAY['E9', 'E10'], 'BUSTED', 100, 1, NULL,
                                                 'UNMAPPED', 'C1', 'P1', 'COMPLETE');
        RAISE EXCEPTION 'FAIL: busted trade kept quantity';
    EXCEPTION WHEN check_violation THEN NULL; END;
END $$;

SELECT 'constraint checks passed' AS result;
