"""走廊碳核算的 SQLite 表结构。

设计要点：
- 碳核算表与 ``transport_coordination`` 基础表建在同一个 SQLite 文件中，
  因此操作者与组织直接复用基础服务的 actors / organizations；
- 主数据（路段边界、车辆与能源类型、排放因子、基准方案、证据政策、目标）
  全部只追加版本，永不原位更新；
- 核算批次冻结时保存原始输入、主数据精确版本引用与凭证快照，
  重述与吊销只追加批次版本，已发布报告始终可按原输入复算。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from transport_coordination.storage import SCHEMA as BASE_SCHEMA

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ============ 版本化主数据 ============

-- 走廊下的路段边界（按版本保存，boundary 描述边界点，长度单位 km）
CREATE TABLE IF NOT EXISTS corridor_segments (
    segment_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    corridor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    length_km REAL NOT NULL CHECK(length_km > 0),
    boundary_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (segment_id, version)
);

-- 车辆与能源类型：能耗强度与（可选的）未匹配绿证时的并网/燃料排放因子
CREATE TABLE IF NOT EXISTS vehicle_energy_types (
    energy_type_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    energy_carrier TEXT NOT NULL
        CHECK(energy_carrier IN ('electricity','diesel','hydrogen','lng','other')),
    energy_intensity_kwh_per_km REAL,
    grid_ef_kg_per_kwh REAL,
    fuel_intensity_l_per_km REAL,
    fuel_ef_kg_per_l REAL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (energy_type_id, version)
);

-- 命名排放因子库（电网因子、柴油因子等），按 factor_key 取当前版本
CREATE TABLE IF NOT EXISTS emission_factors (
    factor_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    factor_key TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    value REAL NOT NULL CHECK(value >= 0),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (factor_id, version)
);

-- 基准线方案：基准车辆因子（kg/km）与载荷修正口径
CREATE TABLE IF NOT EXISTS baselines (
    baseline_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    corridor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    baseline_ef_kg_per_km REAL NOT NULL CHECK(baseline_ef_kg_per_km > 0),
    load_correction_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (baseline_id, version)
);

-- 证据政策：各类证据的有效期（天）与是否必需
CREATE TABLE IF NOT EXISTS evidence_policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    name TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    before_grace_days INTEGER NOT NULL DEFAULT 2 CHECK(before_grace_days >= 0),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);

-- 走廊减排目标与完成口径（verified：已独立核验；published：已对外发布）
CREATE TABLE IF NOT EXISTS corridor_targets (
    target_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    corridor_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    target_tco2 REAL NOT NULL CHECK(target_tco2 >= 0),
    metric TEXT NOT NULL CHECK(metric IN ('verified','published')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (target_id, version)
);

-- ============ 绿色电力凭证（先登记，后占用） ============
CREATE TABLE IF NOT EXISTS green_certificates (
    certificate_id TEXT PRIMARY KEY,
    batch_no TEXT NOT NULL,
    energy_kwh REAL NOT NULL CHECK(energy_kwh > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','occupied','withdrawn')),
    registered_by TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT
);

-- 凭证占用：matched 表示车队与站点双侧声明一致后的一次唯一分配，
-- 冻结时 reserved，发布时 settled，被重述/吊销取代时 released。
CREATE TABLE IF NOT EXISTS certificate_claims (
    claim_id TEXT PRIMARY KEY,
    certificate_id TEXT NOT NULL REFERENCES green_certificates(certificate_id),
    batch_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    claimant_type TEXT NOT NULL CHECK(claimant_type IN ('fleet','station','matched')),
    claimant_id TEXT NOT NULL,
    energy_kwh REAL NOT NULL CHECK(energy_kwh > 0),
    state TEXT NOT NULL CHECK(state IN ('reserved','settled','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(certificate_id, batch_id)
);

-- ============ 核算批次 ============
CREATE TABLE IF NOT EXISTS accounting_batches (
    batch_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    scope_key TEXT NOT NULL,                -- 批次族身份：走廊|周期起|周期止|口径标签
    scope_tag TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,     -- 族内版本：1 初版，其后为重述
    parent_batch_id TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('frozen','verified','published','restated','revoked')),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    baseline_id TEXT NOT NULL,
    baseline_version INTEGER NOT NULL,
    target_id TEXT,
    target_version INTEGER,
    master_versions_json TEXT NOT NULL,     -- 冻结时全部主数据精确版本引用
    certificate_snapshot_json TEXT NOT NULL,-- 凭证登记内容冻结快照（复算依据）
    frozen_by TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    verified_by TEXT,
    verified_at TEXT,
    verify_notes TEXT,
    published_at TEXT,
    restate_reason TEXT,
    revoke_reason TEXT,
    revoked_by TEXT,
    revoked_at TEXT,
    manifest_hash TEXT NOT NULL,           -- 全部冻结输入的规范摘要
    result_hash TEXT NOT NULL,             -- 输入 + 结果的规范摘要
    result_json TEXT NOT NULL,
    UNIQUE(scope_key, version)
);

-- 冻结的行程（含原始输入、校验状态、补证原因、分项结果）
CREATE TABLE IF NOT EXISTS batch_trips (
    uid TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES accounting_batches(batch_id),
    trip_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    energy_type_id TEXT NOT NULL,
    energy_type_version INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    logged_at TEXT,
    segment_legs_json TEXT NOT NULL,
    distance_km REAL NOT NULL,
    payload_json TEXT NOT NULL,
    payload_status TEXT NOT NULL CHECK(payload_status IN ('complete','pending')),
    energy_records_json TEXT NOT NULL,
    energy_status TEXT NOT NULL CHECK(energy_status IN ('complete','pending','conflict')),
    declarations_json TEXT NOT NULL,
    evidence_valid_json TEXT NOT NULL,
    claim_status TEXT NOT NULL CHECK(claim_status IN ('matched','missing','conflict')),
    verification_status TEXT NOT NULL
        CHECK(verification_status IN ('valid','pending_evidence')),
    pending_reasons_json TEXT NOT NULL,
    baseline_emissions_kg REAL NOT NULL,
    actual_emissions_kg REAL,               -- 证据不足时为 NULL，明确不是零排放
    reductions_kg REAL NOT NULL,
    calc_detail_json TEXT NOT NULL,
    UNIQUE(batch_id, trip_id)
);

-- 发布版本链：每个批次族每次发布/吊销一行，版本号最大即当前口径
CREATE TABLE IF NOT EXISTS published_versions (
    scope_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('initial','restatement','revocation')),
    reductions_tco2 REAL NOT NULL,
    result_hash TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published_by TEXT NOT NULL,
    PRIMARY KEY (scope_key, version)
);

-- 核验发现（独立核验人逐条登记，blocking 未解决不能通过核验）
CREATE TABLE IF NOT EXISTS verification_findings (
    finding_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES accounting_batches(batch_id),
    reviewer_id TEXT NOT NULL,
    code TEXT NOT NULL,
    trip_id TEXT,
    message TEXT NOT NULL,
    blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
    resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_certificate ON certificate_claims(certificate_id);
CREATE INDEX IF NOT EXISTS idx_claims_scope ON certificate_claims(scope_key, version);
CREATE INDEX IF NOT EXISTS idx_trips_batch ON batch_trips(batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_scope ON accounting_batches(scope_key, version);
CREATE INDEX IF NOT EXISTS idx_batches_corridor ON accounting_batches(corridor_id, period_start, period_end);
"""


class CarbonDatabase:
    """管理与基础服务同库的碳核算表，提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        # 先建基础表（organizations/actors/sites/request_receipts/audit_events 等）
        self.connection.executescript(BASE_SCHEMA)
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
