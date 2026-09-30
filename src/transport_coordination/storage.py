"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 走廊碳核算：配置版本化 ---------------------------------------------------
CREATE TABLE IF NOT EXISTS carbon_corridors (
    corridor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS carbon_boundary_versions (
    corridor_id TEXT NOT NULL REFERENCES carbon_corridors(corridor_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    segments_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(corridor_id, version)
);
CREATE TABLE IF NOT EXISTS carbon_target_versions (
    corridor_id TEXT NOT NULL REFERENCES carbon_corridors(corridor_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    target_reduction_t REAL NOT NULL CHECK(target_reduction_t >= 0),
    methodology TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(corridor_id, version)
);
CREATE TABLE IF NOT EXISTS vehicle_type_versions (
    vehicle_type_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    energy_type TEXT NOT NULL,
    consumption_rate REAL NOT NULL CHECK(consumption_rate >= 0),
    rated_payload_t REAL NOT NULL CHECK(rated_payload_t >= 0),
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(vehicle_type_id, version)
);
CREATE TABLE IF NOT EXISTS emission_factor_versions (
    factor_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    value REAL NOT NULL CHECK(value >= 0),
    unit TEXT NOT NULL,
    source TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(factor_key, version)
);
CREATE TABLE IF NOT EXISTS baseline_versions (
    baseline_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    corridor_id TEXT NOT NULL REFERENCES carbon_corridors(corridor_id),
    vehicle_class TEXT NOT NULL,
    fuel_intensity_l_per_km REAL NOT NULL CHECK(fuel_intensity_l_per_km >= 0),
    factor_key TEXT NOT NULL,
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(baseline_id, version)
);
CREATE TABLE IF NOT EXISTS evidence_policy_versions (
    evidence_type TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    validity_days INTEGER NOT NULL CHECK(validity_days >= 0),
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(evidence_type, version)
);
-- 走廊碳核算：运营输入（行程、载荷、补能、凭证） ---------------------------
CREATE TABLE IF NOT EXISTS carbon_trips (
    trip_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES carbon_corridors(corridor_id),
    segment_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    vehicle_type_id TEXT NOT NULL,
    distance_km REAL NOT NULL CHECK(distance_km > 0),
    occurred_at TEXT NOT NULL,
    payload_t REAL,
    payload_status TEXT NOT NULL CHECK(payload_status IN ('absent', 'reported')),
    conflict_flags_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS carbon_energy_events (
    event_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES carbon_trips(trip_id),
    station_id TEXT,
    source_status TEXT NOT NULL CHECK(source_status IN ('unknown', 'reported')),
    amount REAL NOT NULL CHECK(amount > 0),
    occurred_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS carbon_certificates (
    certificate_id TEXT PRIMARY KEY,
    claimant_type TEXT NOT NULL CHECK(claimant_type IN ('fleet', 'station')),
    claimant_id TEXT NOT NULL,
    kwh_total REAL NOT NULL CHECK(kwh_total > 0),
    generation_start TEXT NOT NULL,
    generation_end TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'withdrawn')),
    withdrawn_at TEXT,
    withdrawal_reason TEXT,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS carbon_claims (
    claim_id TEXT PRIMARY KEY,
    certificate_id TEXT NOT NULL REFERENCES carbon_certificates(certificate_id),
    energy_event_id TEXT NOT NULL REFERENCES carbon_energy_events(event_id),
    claimant_type TEXT NOT NULL CHECK(claimant_type IN ('fleet', 'station')),
    claimant_id TEXT NOT NULL,
    kwh REAL NOT NULL CHECK(kwh > 0),
    status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected_conflict', 'voided')),
    conflict_code TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS claim_one_accepted_per_event
    ON carbon_claims(energy_event_id) WHERE status = 'accepted';
-- 凭证占用：held=待核验冻结占用，committed=随批次发布锁定，
-- released=核验驳回释放，superseded=被同族新版本替代
CREATE TABLE IF NOT EXISTS carbon_allocations (
    allocation_id TEXT PRIMARY KEY,
    family_batch_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    certificate_id TEXT NOT NULL,
    energy_event_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    kwh REAL NOT NULL CHECK(kwh > 0),
    claimant_type TEXT NOT NULL,
    claimant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('held', 'committed', 'released', 'superseded')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alloc_certificate_idx
    ON carbon_allocations(certificate_id, status);
CREATE TABLE IF NOT EXISTS carbon_evidence_requests (
    request_id TEXT PRIMARY KEY,
    family_batch_id TEXT,
    batch_id TEXT,
    version INTEGER,
    trip_id TEXT,
    code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    resolution_note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS evidence_open_unique
    ON carbon_evidence_requests(batch_id, version, trip_id, code)
    WHERE status = 'open';
-- 批次族：同一 batch_id 的多个版本构成重述/吊销链
CREATE TABLE IF NOT EXISTS carbon_batches (
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    corridor_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    baseline_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen', 'verified', 'rejected',
                                         'published', 'restated', 'revoked')),
    revision_kind TEXT NOT NULL CHECK(revision_kind IN ('original', 'late_data',
                                                        'factor_revision',
                                                        'certificate_revocation')),
    change_note TEXT NOT NULL,
    input_snapshot_json TEXT,
    snapshot_hash TEXT,
    result_json TEXT,
    result_hash TEXT,
    formula_version TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_by TEXT,
    frozen_at TEXT,
    submitted_at TEXT,
    verified_by TEXT,
    verified_at TEXT,
    verification_decision TEXT,
    verification_note TEXT,
    published_by TEXT,
    published_at TEXT,
    PRIMARY KEY(batch_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS batch_open_version
    ON carbon_batches(batch_id) WHERE status IN ('draft', 'frozen', 'verified');
-- 行程跨批次族锁定，冻结即占位，发布后锁定
CREATE TABLE IF NOT EXISTS carbon_trip_locks (
    trip_id TEXT PRIMARY KEY,
    family_batch_id TEXT NOT NULL,
    active_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('held', 'committed'))
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
