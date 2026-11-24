"""SQLite persistence.

The application keeps a single local SQLite database.  It stores both the
imported source records (immutable, hash-stamped) and the decisions taken by
the quality, product and supply roles.  Facts are never overwritten by
re-import; corrections are appended as revisions.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batch (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    source_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    encoding TEXT,
    imported_at TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_new INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_unchanged INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS import_issue (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES import_batch(batch_id) ON DELETE CASCADE,
    dataset TEXT NOT NULL,
    row_number INTEGER,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    record_key TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    lot_id TEXT,
    channel TEXT NOT NULL,
    sold_at TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0),
    policy_version TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT,
    category TEXT,
    launched_on TEXT,
    active_copy_version TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    supplier_id TEXT,
    supplier_lot_ref TEXT,
    produced_on TEXT,
    unshipped_units INTEGER NOT NULL DEFAULT 0,
    traceability_state TEXT NOT NULL,
    note TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS returns (
    return_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    returned_units INTEGER NOT NULL CHECK (returned_units > 0),
    reason_code TEXT NOT NULL,
    received_at TEXT NOT NULL,
    refund_state TEXT NOT NULL,
    review_state TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reason_map (
    reason_code TEXT NOT NULL,
    mapping_version TEXT NOT NULL,
    category TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL,
    PRIMARY KEY (reason_code, mapping_version)
);

CREATE TABLE IF NOT EXISTS reason_alias (
    alias_code TEXT NOT NULL,
    scheme_version TEXT NOT NULL,
    canonical_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    note TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL,
    PRIMARY KEY (alias_code, scheme_version)
);

CREATE TABLE IF NOT EXISTS return_review (
    review_id TEXT PRIMARY KEY,
    return_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    new_reason_code TEXT,
    reviewed_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'import',
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS return_revision (
    return_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    source_review_id TEXT NOT NULL UNIQUE,
    old_reason_code TEXT NOT NULL,
    new_reason_code TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    rationale TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (return_id, seq)
);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    sample_size INTEGER NOT NULL CHECK (sample_size > 0),
    finding_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    review_state TEXT NOT NULL,
    inspected_at TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_review_import (
    review_id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL,
    review_state TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_review (
    inspection_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    review_state TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (inspection_id, seq)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    lot_id TEXT,
    sku TEXT,
    case_id TEXT,
    owner_role TEXT NOT NULL,
    action_type TEXT NOT NULL,
    decision_state TEXT NOT NULL,
    raw_state TEXT,
    effective_at TEXT,
    receipt_ref TEXT,
    supplier_ref TEXT,
    source TEXT NOT NULL DEFAULT 'app',
    note TEXT,
    created_by TEXT,
    created_at TEXT,
    completed_at TEXT,
    supersedes_action_id TEXT,
    verification TEXT NOT NULL DEFAULT 'unverified',
    flags_json TEXT NOT NULL DEFAULT '[]',
    record_hash TEXT,
    first_batch INTEGER,
    last_batch INTEGER
);

CREATE TABLE IF NOT EXISTS action_event (
    action_id TEXT NOT NULL REFERENCES actions(action_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    event_at TEXT NOT NULL,
    receipt_ref TEXT,
    note TEXT,
    PRIMARY KEY (action_id, seq)
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_ref TEXT PRIMARY KEY,
    lot_id TEXT,
    receipt_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    received_at TEXT NOT NULL,
    channel TEXT,
    note TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS refund_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    amount_units INTEGER,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS customer_service (
    summary_id TEXT PRIMARY KEY,
    order_id TEXT,
    received_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    review_state TEXT NOT NULL,
    linked_return_id TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS copy_versions (
    copy_version_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    version TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    change_type TEXT,
    summary TEXT,
    record_hash TEXT NOT NULL,
    first_batch INTEGER NOT NULL,
    last_batch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_snapshot (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    data_digest TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    notes TEXT,
    previous_snapshot_id INTEGER REFERENCES risk_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS risk_result (
    snapshot_id INTEGER NOT NULL REFERENCES risk_snapshot(snapshot_id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    proposed_level TEXT NOT NULL,
    exposure_units INTEGER NOT NULL,
    immature_units INTEGER NOT NULL,
    quality_units INTEGER NOT NULL,
    unknown_units INTEGER NOT NULL,
    information_units INTEGER NOT NULL,
    fit_units INTEGER NOT NULL DEFAULT 0,
    other_units INTEGER NOT NULL DEFAULT 0,
    late_return_units INTEGER NOT NULL DEFAULT 0,
    expected_units REAL NOT NULL,
    smr REAL,
    p_value REAL NOT NULL,
    q_value REAL NOT NULL,
    crude_rate REAL,
    crude_lo REAL,
    crude_hi REAL,
    unknown_share REAL NOT NULL,
    worst_case_rate REAL,
    traceability_gap_units INTEGER NOT NULL,
    traceability_gap_share REAL NOT NULL,
    info_expected REAL,
    info_smr REAL,
    info_p_value REAL,
    info_q_value REAL,
    evidence_confirmed INTEGER NOT NULL,
    evidence_pending INTEGER NOT NULL,
    evidence_blocking INTEGER NOT NULL,
    repeat_json TEXT NOT NULL,
    mix_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, level, entity_id)
);

CREATE TABLE IF NOT EXISTS case_record (
    case_id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    status TEXT NOT NULL,
    proposed_level TEXT NOT NULL,
    grade TEXT,
    opened_at TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES risk_snapshot(snapshot_id),
    scope_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    closed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_case_active
    ON case_record(level, entity_id)
    WHERE status IN ('open', 'investigating', 'reopened');

CREATE TABLE IF NOT EXISTS case_decision (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES case_record(case_id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    value TEXT,
    rationale TEXT,
    decided_at TEXT NOT NULL,
    effective_at TEXT,
    snapshot_id INTEGER REFERENCES risk_snapshot(snapshot_id),
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS improvement_check (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES case_record(case_id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    cutoff_at TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    pre_from TEXT NOT NULL,
    pre_to TEXT NOT NULL,
    post_from TEXT NOT NULL,
    post_to TEXT NOT NULL,
    pre_exposure INTEGER NOT NULL,
    pre_events INTEGER NOT NULL,
    post_exposure INTEGER NOT NULL,
    post_events INTEGER NOT NULL,
    pre_rate REAL,
    post_rate REAL,
    rate_ratio REAL,
    rate_ratio_lo REAL,
    rate_ratio_hi REAL,
    p_value REAL,
    verdict TEXT NOT NULL,
    method TEXT NOT NULL,
    note TEXT,
    snapshot_id INTEGER REFERENCES risk_snapshot(snapshot_id),
    mix_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS refund_record (
    return_id TEXT PRIMARY KEY,
    refund_state TEXT NOT NULL,
    amount_units INTEGER,
    refunded_at TEXT,
    last_seen_batch INTEGER NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS ix_risk_result_level ON risk_result(snapshot_id, level, proposed_level);
CREATE INDEX IF NOT EXISTS ix_import_issue_batch ON import_issue(batch_id);
CREATE INDEX IF NOT EXISTS ix_revision_return ON return_revision(return_id, effective_at);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and initialise) the application database."""
    db_path = str(path)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit immediate transaction for read-modify-write sequences."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))
