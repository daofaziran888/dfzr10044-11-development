"""湖表存储引擎：文件注册表、分区清单、快照链、水位存储与批次数据。

本模块提供跨服务共享的不可变数据层：
- 文件注册：每个分区文件有唯一 file_id、所属分区、所属湖表、行数、字节数
- 快照链：每个湖表按时间线记录快照，含 file_ids、manifest、父快照指针
- 水位：每张湖表的已提交/待提交水位记录
- 批次数据：入湖批次的行级数据存储，含校验值
- 隔离区：被质量规则拦截的数据行，按批次隔离
- 写锁：并发提交冲突检测

设计原则：
- 所有写操作在提交前必须拿到全局锁
- 快照只在事务完整落湖后标记为可用
- 归并时先写入新文件清单，再原子地替换旧文件列表
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ----------------------------- 文件注册 -----------------------------

@dataclass
class PartitionFile:
    """分区文件：每个文件属于一个分区。"""
    file_id: str
    lake_table: str
    partition_key: str
    file_rows: int
    file_bytes: int
    file_manifest: str  # 文件内容摘要（MD5）
    created_at: float


# ----------------------------- 快照 -----------------------------

@dataclass
class LakeSnapshot:
    """湖表快照：记录某个时刻整个湖表的分区文件清单。"""
    snapshot_id: str
    lake_table: str
    parent_snapshot: str | None
    file_ids: list[str]
    added_rows: int
    deleted_rows: int
    manifest_checksum: str
    status: str  # generating | available | expired | rolled_back
    created_at: float


# ----------------------------- 水位 -----------------------------

@dataclass
class WatermarkRecord:
    """入湖水位追踪：记录已提交和待提交水位。"""
    lake_table: str
    field_name: str
    committed_value: str
    pending_value: str | None
    strategy: str  # append | merge | overwrite
    last_advanced_at: float


# ----------------------------- 结构版本 -----------------------------

@dataclass
class ColumnSpec:
    """结构版本中的列声明：名称、类型、量纲单位与可空性。

    unit 用于跨版本量纲换算（元 / 分）；factor 表示该列相对基准单位的倍率。
    """
    name: str
    type: str
    nullable: bool = True
    unit: str = ""
    factor: float = 1.0


@dataclass
class SchemaVersion:
    """结构版本：一张湖表某一版完整列结构，以及与父版本之间的字段转换映射。

    conversions 的键为新版本列名，值描述来源列与换算方式，例如：
    {"amount_cents": {"source": "amount", "expr": "scale", "factor": 100}}
    结构版本按 (湖表, 版本号) 唯一；另一张湖表的同名版本必须被拒绝。
    """
    version_id: str
    lake_table: str
    parent_version_id: str | None
    columns: list[ColumnSpec]
    conversions: dict[str, dict[str, Any]]
    status: str  # draft | active | superseded
    reason: str
    created_at: float

    def column(self, name: str) -> ColumnSpec | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None


# ----------------------------- 规则版本 -----------------------------

@dataclass
class RuleVersion:
    """规则版本：某次发布时点上完整、不可变的质量规则快照。

    阈值口径随版本冻结：校验时读取的是快照里的阈值，而不是规则模块的当前值。
    """
    rule_version_id: str
    lake_table: str
    schema_version_id: str
    rules: list[dict[str, Any]]
    status: str  # active | superseded
    created_at: float


# ----------------------------- 批次数据 -----------------------------

@dataclass
class BatchRecord:
    """入湖批次：一次入湖操作导出的一组数据行。

    schema_version_id 保留批次采集当时读到的结构版本，
    之后任何复算都必须能从批次查回这套结构与原始输入。
    """
    batch_id: str
    lake_table: str
    source_system: str
    rows: list[dict[str, Any]]  # 实际数据行（原始输入，永不被转换覆盖）
    row_count: int
    window_start: str
    window_end: str
    checksum: str
    snapshot_id: str | None  # 落湖后分配的 snap
    status: str  # pending | ingesting | ingested | failed
    created_at: float
    schema_version_id: str | None = None
    rule_version_id: str | None = None


# ----------------------------- 质量评估会话 -----------------------------

@dataclass
class RowEvaluation:
    """单行评估明细：原输入、按目标结构转换后的输入与判定结论。"""
    row_index: int
    row_key: str
    raw_input: dict[str, Any]
    converted_input: dict[str, Any]
    verdict: str  # passed | violated
    hit_rules: list[str]
    evaluated_values: dict[str, Any]  # 各规则实际读到的值（量纲转换后）


@dataclass
class EvaluationSession:
    """一次完整的按结构版本校验：冻结所用结构/规则版本与逐行复算轨迹。"""
    evaluation_id: str
    batch_id: str
    lake_table: str
    schema_version_id: str
    rule_version_id: str
    origin: str  # initial | recheck | retry
    source_evaluation_id: str | None
    status: str  # validating | confirmed
    row_evaluations: list[RowEvaluation]
    created_at: float
    confirmed_at: float | None = None


# ----------------------------- 隔离记录 -----------------------------

@dataclass
class QuarantineRecord:
    """行级隔离记录：保留初次判定版本、原输入与每次复检/回炉轨迹。"""
    quarantine_id: str
    batch_id: str
    lake_table: str
    row_key: str
    original_input: dict[str, Any]
    corrected_input: dict[str, Any] | None
    rule_id: str
    rule_name: str
    schema_version_id: str
    rule_version_id: str
    initial_evaluation_id: str
    status: str  # isolated | released | archived
    created_at: float
    evaluations: list[dict[str, Any]] = field(default_factory=list)


# ----------------------------- 幂等落湖台账 -----------------------------

@dataclass
class LakeLedgerEntry:
    """有效数据落湖台账：同一行主键在同一湖表只允许出现一次。"""
    row_key: str
    lake_table: str
    batch_id: str
    evaluation_id: str
    schema_version_id: str
    payload: dict[str, Any]
    created_at: float


# ----------------------------- 写锁 -----------------------------

class WriteLock:
    """全局写入锁：同一湖表同时只能有一个写入者提交。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def acquire(self, lake_table: str) -> threading.Lock:
        with self._lock:
            if lake_table not in self._locks:
                self._locks[lake_table] = threading.Lock()
            table_lock = self._locks[lake_table]
        table_lock.acquire()
        return table_lock

    def release(self, lake_table: str) -> None:
        with self._lock:
            lock = self._locks.get(lake_table)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


# ----------------------------- 存储引擎 -----------------------------

class LakeStore:
    """湖表存储引擎主类。

    管理维度：
    - files: 分区文件注册表
    - snapshots: 快照链
    - watermarks: 水位
    - batches: 批次数据（保留采集时的结构版本）
    - schema_versions: 结构版本目录（含量纲与跨版本转换）
    - rule_versions: 不可变规则版本快照
    - evaluations: 按结构版本的校验/复算会话
    - quarantine: 行级隔离区（保留初次判定版本与原输入）
    - ledger: 有效数据幂等落湖台账
    - write_lock: 并发提交的控制锁
    """

    def __init__(self) -> None:
        # 文件注册表：lake_table -> {file_id -> PartitionFile}
        self._files: dict[str, dict[str, PartitionFile]] = {}

        # 快照链：lake_table -> list[LakeSnapshot]（时间序）
        self._snapshots: dict[str, list[LakeSnapshot]] = {}

        # 水位：lake_table -> WatermarkRecord
        self._watermarks: dict[str, WatermarkRecord] = {}

        # 批次数据：batch_id -> BatchRecord
        self._batches: dict[str, BatchRecord] = {}

        # 结构版本：(lake_table, version_id) -> SchemaVersion
        self._schema_versions: dict[tuple[str, str], SchemaVersion] = {}

        # 规则版本：rule_version_id -> RuleVersion
        self._rule_versions: dict[str, RuleVersion] = {}

        # 质量评估会话：evaluation_id -> EvaluationSession
        self._evaluations: dict[str, EvaluationSession] = {}

        # 行级隔离区：quarantine_id -> QuarantineRecord
        self._quarantine_records: dict[str, QuarantineRecord] = {}

        # 旧接口兼容存储：batch_id -> list[dict]（quarantine_rows 仍可使用）
        self._quarantine: dict[str, list[dict[str, Any]]] = {}

        # 幂等落湖台账：(lake_table, row_key) -> LakeLedgerEntry
        self._ledger: dict[tuple[str, str], LakeLedgerEntry] = {}

        # 写锁
        self._write_lock = WriteLock()

        # 归并中断恢复检查点：merge_id -> {partial_file_ids: [...], ...}
        self._merge_checkpoints: dict[str, dict[str, Any]] = {}

    # ---- 文件操作 ----

    def register_file(
        self, lake_table: str, partition_key: str, rows: int, bytes_: int, manifest: str | None = None
    ) -> PartitionFile:
        file_id = f"{lake_table}-p{partition_key}-{uuid.uuid4().hex[:8]}"
        checksum = manifest or hashlib.md5(f"{lake_table}:{partition_key}:{time.time()}".encode()).hexdigest()
        pf = PartitionFile(
            file_id=file_id,
            lake_table=lake_table,
            partition_key=partition_key,
            file_rows=rows,
            file_bytes=bytes_,
            file_manifest=checksum,
            created_at=time.time(),
        )
        self._files.setdefault(lake_table, {})[file_id] = pf
        return pf

    def list_files(self, lake_table: str, partition_key: str | None = None) -> list[PartitionFile]:
        files = list(self._files.get(lake_table, {}).values())
        if partition_key is not None:
            files = [f for f in files if f.partition_key == partition_key]
        return files

    def get_file(self, lake_table: str, file_id: str) -> PartitionFile | None:
        return self._files.get(lake_table, {}).get(file_id)

    def remove_files(self, lake_table: str, file_ids: list[str]) -> int:
        table_files = self._files.get(lake_table, {})
        removed = 0
        for fid in file_ids:
            if fid in table_files:
                del table_files[fid]
                removed += 1
        return removed

    def replace_file_list(
        self, lake_table: str, old_file_ids: list[str], new_files: list[PartitionFile]
    ) -> None:
        """原子替换：先注册新文件，再删除旧文件。保证归并不留半份清单。"""
        table_files = self._files.setdefault(lake_table, {})
        for nf in new_files:
            table_files[nf.file_id] = nf
        for fid in old_file_ids:
            table_files.pop(fid, None)

    # ---- 快照操作 ----

    def create_snapshot(
        self,
        lake_table: str,
        file_ids: list[str],
        parent_snapshot: str | None = None,
        added_rows: int = 0,
        deleted_rows: int = 0,
    ) -> LakeSnapshot:
        snap_id = f"snap-{lake_table}-{uuid.uuid4().hex[:8]}"
        manifest = hashlib.md5(
            ",".join(sorted(file_ids)).encode()
        ).hexdigest()
        snap = LakeSnapshot(
            snapshot_id=snap_id,
            lake_table=lake_table,
            parent_snapshot=parent_snapshot,
            file_ids=list(file_ids),
            added_rows=added_rows,
            deleted_rows=deleted_rows,
            manifest_checksum=manifest,
            status="generating",
            created_at=time.time(),
        )
        chain = self._snapshots.setdefault(lake_table, [])
        chain.append(snap)
        return snap

    def mark_snapshot_available(self, lake_table: str, snapshot_id: str) -> LakeSnapshot | None:
        for snap in self._snapshots.get(lake_table, []):
            if snap.snapshot_id == snapshot_id:
                snap.status = "available"
                return snap
        return None

    def mark_snapshot_expired(self, lake_table: str, snapshot_id: str) -> LakeSnapshot | None:
        for snap in self._snapshots.get(lake_table, []):
            if snap.snapshot_id == snapshot_id:
                snap.status = "expired"
                return snap
        return None

    def get_snapshot(self, lake_table: str, snapshot_id: str) -> LakeSnapshot | None:
        for snap in self._snapshots.get(lake_table, []):
            if snap.snapshot_id == snapshot_id:
                return snap
        return None

    def list_snapshots(self, lake_table: str) -> list[LakeSnapshot]:
        return list(self._snapshots.get(lake_table, []))

    def get_snapshot_at_time(self, lake_table: str, timestamp: float) -> LakeSnapshot | None:
        """时间旅行：返回 <=timestamp 的最近可用快照。"""
        candidates = [
            s for s in self._snapshots.get(lake_table, [])
            if s.status == "available" and s.created_at <= timestamp
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.created_at)

    # ---- 水位操作 ----

    def set_watermark(
        self, lake_table: str, field_name: str, committed: str,
        strategy: str = "append"
    ) -> WatermarkRecord:
        wm = WatermarkRecord(
            lake_table=lake_table,
            field_name=field_name,
            committed_value=committed,
            pending_value=None,
            strategy=strategy,
            last_advanced_at=time.time(),
        )
        self._watermarks[lake_table] = wm
        return wm

    def advance_watermark(self, lake_table: str, new_value: str) -> WatermarkRecord | None:
        wm = self._watermarks.get(lake_table)
        if wm is None:
            return None
        wm.pending_value = new_value
        return wm

    def commit_watermark(self, lake_table: str) -> WatermarkRecord | None:
        wm = self._watermarks.get(lake_table)
        if wm is None or wm.pending_value is None:
            return None
        wm.committed_value = wm.pending_value
        wm.pending_value = None
        wm.last_advanced_at = time.time()
        return wm

    def get_watermark(self, lake_table: str) -> WatermarkRecord | None:
        return self._watermarks.get(lake_table)

    def list_watermarks(self) -> list[WatermarkRecord]:
        return list(self._watermarks.values())

    # ---- 批次数据 ----

    def store_batch(
        self, batch_id: str, lake_table: str, source_system: str,
        rows: list[dict[str, Any]], window_start: str, window_end: str,
        schema_version_id: str | None = None,
    ) -> BatchRecord:
        checksum = hashlib.md5(
            str(sorted([str(r) for r in rows])).encode()
        ).hexdigest()
        br = BatchRecord(
            batch_id=batch_id,
            lake_table=lake_table,
            source_system=source_system,
            rows=list(rows),
            row_count=len(rows),
            window_start=window_start,
            window_end=window_end,
            checksum=checksum,
            snapshot_id=None,
            status="pending",
            created_at=time.time(),
            schema_version_id=schema_version_id,
        )
        self._batches[batch_id] = br
        return br

    def get_batch(self, batch_id: str) -> BatchRecord | None:
        return self._batches.get(batch_id)

    def list_batches(self, lake_table: str) -> list[BatchRecord]:
        return [b for b in self._batches.values() if b.lake_table == lake_table]

    def bind_batch_versions(
        self, batch_id: str, schema_version_id: str, rule_version_id: str | None = None
    ) -> BatchRecord | None:
        """把批次当时读到的结构版本（及规则版本）固定到批次记录上。"""
        br = self._batches.get(batch_id)
        if br is None:
            return None
        br.schema_version_id = schema_version_id
        if rule_version_id is not None:
            br.rule_version_id = rule_version_id
        return br

    def update_batch_status(self, batch_id: str, status: str, snapshot_id: str | None = None) -> BatchRecord | None:
        br = self._batches.get(batch_id)
        if br is None:
            return None
        br.status = status
        if snapshot_id is not None:
            br.snapshot_id = snapshot_id
        return br

    # ---- 结构版本目录 ----

    def register_schema_version(
        self,
        version_id: str,
        lake_table: str,
        columns: list[ColumnSpec],
        *,
        parent_version_id: str | None = None,
        conversions: dict[str, dict[str, Any]] | None = None,
        reason: str = "",
    ) -> SchemaVersion:
        """登记一个结构版本。

        版本号在全局范围唯一：另一张湖表登记同名版本必须拒绝，
        避免隔离复检时错拿别的湖表的同名结构。
        """
        for (other_table, other_id), existing in self._schema_versions.items():
            if other_id == version_id and other_table != lake_table:
                raise ValueError(
                    f"结构版本号 {version_id} 已被湖表 {other_table} 占用，"
                    f"湖表 {lake_table} 不得登记同名版本"
                )
        key = (lake_table, version_id)
        if key in self._schema_versions:
            raise ValueError(f"湖表 {lake_table} 已存在结构版本 {version_id}")
        sv = SchemaVersion(
            version_id=version_id,
            lake_table=lake_table,
            parent_version_id=parent_version_id,
            columns=list(columns),
            conversions=dict(conversions or {}),
            status="draft",
            reason=reason,
            created_at=time.time(),
        )
        self._schema_versions[key] = sv
        return sv

    def get_schema_version(self, lake_table: str, version_id: str) -> SchemaVersion | None:
        return self._schema_versions.get((lake_table, version_id))

    def list_schema_versions(self, lake_table: str | None = None) -> list[SchemaVersion]:
        versions = list(self._schema_versions.values())
        if lake_table is not None:
            versions = [v for v in versions if v.lake_table == lake_table]
        return sorted(versions, key=lambda v: v.created_at)

    def get_active_schema_version(self, lake_table: str) -> SchemaVersion | None:
        active = [
            v for v in self._schema_versions.values()
            if v.lake_table == lake_table and v.status == "active"
        ]
        return active[-1] if active else None

    def has_inflight_evaluation(self, lake_table: str) -> bool:
        """该湖表是否存在仍在 validating（在途校验、未确认）的评估会话。"""
        return any(
            ev.lake_table == lake_table and ev.status == "validating"
            for ev in self._evaluations.values()
        )

    def list_inflight_evaluations(self, lake_table: str) -> list[EvaluationSession]:
        return [
            ev for ev in self._evaluations.values()
            if ev.lake_table == lake_table and ev.status == "validating"
        ]

    def activate_schema_version(self, version_id: str, lake_table: str) -> SchemaVersion:
        """生效一个结构版本；与在途批次校验冲突时拒绝发布。

        旧的生效版本转为 superseded，新版本置为 active。
        """
        sv = self._schema_versions.get((lake_table, version_id))
        if sv is None:
            raise KeyError(f"湖表 {lake_table} 不存在结构版本 {version_id}")
        inflight = self.list_inflight_evaluations(lake_table)
        if inflight:
            ids = ", ".join(ev.evaluation_id for ev in inflight)
            raise RuntimeError(
                f"结构版本 {version_id} 发布冲突：湖表 {lake_table} 存在 "
                f"{len(inflight)} 个在途校验会话（{ids}）尚未确认"
            )
        for existing in self._schema_versions.values():
            if existing.lake_table == lake_table and existing.status == "active":
                existing.status = "superseded"
        sv.status = "active"
        return sv

    # ---- 规则版本（不可变快照） ----

    def create_rule_version(
        self,
        rule_version_id: str,
        lake_table: str,
        schema_version_id: str,
        rules: list[dict[str, Any]],
    ) -> RuleVersion:
        """发布一版不可变规则快照；与在途校验冲突时拒绝。"""
        if self.get_schema_version(lake_table, schema_version_id) is None:
            raise KeyError(
                f"规则版本必须绑定湖表 {lake_table} 已登记的结构版本，"
                f"未知结构版本 {schema_version_id}"
            )
        if rule_version_id in self._rule_versions:
            raise ValueError(f"规则版本 {rule_version_id} 已存在")
        inflight = self.list_inflight_evaluations(lake_table)
        if inflight:
            ids = ", ".join(ev.evaluation_id for ev in inflight)
            raise RuntimeError(
                f"规则版本 {rule_version_id} 发布冲突：湖表 {lake_table} 存在 "
                f"{len(inflight)} 个在途校验会话（{ids}）尚未确认"
            )
        rv = RuleVersion(
            rule_version_id=rule_version_id,
            lake_table=lake_table,
            schema_version_id=schema_version_id,
            rules=copy.deepcopy(rules),
            status="active",
            created_at=time.time(),
        )
        for existing in self._rule_versions.values():
            if existing.lake_table == lake_table and existing.status == "active":
                existing.status = "superseded"
        self._rule_versions[rule_version_id] = rv
        return rv

    def get_rule_version(self, rule_version_id: str) -> RuleVersion | None:
        return self._rule_versions.get(rule_version_id)

    def list_rule_versions(self, lake_table: str | None = None) -> list[RuleVersion]:
        versions = list(self._rule_versions.values())
        if lake_table is not None:
            versions = [v for v in versions if v.lake_table == lake_table]
        return sorted(versions, key=lambda v: v.created_at)

    def get_active_rule_version(self, lake_table: str) -> RuleVersion | None:
        active = [
            v for v in self._rule_versions.values()
            if v.lake_table == lake_table and v.status == "active"
        ]
        return active[-1] if active else None

    # ---- 质量评估会话 ----

    def save_evaluation(self, session: EvaluationSession) -> EvaluationSession:
        self._evaluations[session.evaluation_id] = session
        return session

    def get_evaluation(self, evaluation_id: str) -> EvaluationSession | None:
        return self._evaluations.get(evaluation_id)

    def list_evaluations(
        self, lake_table: str | None = None, batch_id: str | None = None
    ) -> list[EvaluationSession]:
        sessions = list(self._evaluations.values())
        if lake_table is not None:
            sessions = [s for s in sessions if s.lake_table == lake_table]
        if batch_id is not None:
            sessions = [s for s in sessions if s.batch_id == batch_id]
        return sorted(sessions, key=lambda s: s.created_at)

    def mark_evaluation_confirmed(self, evaluation_id: str) -> EvaluationSession | None:
        session = self._evaluations.get(evaluation_id)
        if session is None:
            return None
        session.status = "confirmed"
        session.confirmed_at = time.time()
        return session

    # ---- 行级隔离区 ----

    def add_quarantine_record(self, record: QuarantineRecord) -> QuarantineRecord:
        self._quarantine_records[record.quarantine_id] = record
        return record

    def get_quarantine_record(self, quarantine_id: str) -> QuarantineRecord | None:
        return self._quarantine_records.get(quarantine_id)

    def find_quarantine_record(
        self, batch_id: str, row_key: str
    ) -> QuarantineRecord | None:
        for record in self._quarantine_records.values():
            if record.batch_id == batch_id and record.row_key == row_key:
                return record
        return None

    def list_quarantine_records(
        self,
        lake_table: str | None = None,
        batch_id: str | None = None,
        status: str | None = None,
    ) -> list[QuarantineRecord]:
        records = list(self._quarantine_records.values())
        if lake_table is not None:
            records = [r for r in records if r.lake_table == lake_table]
        if batch_id is not None:
            records = [r for r in records if r.batch_id == batch_id]
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: r.created_at)

    def append_quarantine_evaluation(
        self, quarantine_id: str, evaluation: dict[str, Any]
    ) -> QuarantineRecord | None:
        record = self._quarantine_records.get(quarantine_id)
        if record is None:
            return None
        record.evaluations.append(copy.deepcopy(evaluation))
        return record

    def update_quarantine_status(
        self, quarantine_id: str, status: str,
        corrected_input: dict[str, Any] | None = None,
    ) -> QuarantineRecord | None:
        record = self._quarantine_records.get(quarantine_id)
        if record is None:
            return None
        record.status = status
        if corrected_input is not None:
            record.corrected_input = copy.deepcopy(corrected_input)
        return record

    # ---- 幂等落湖台账 ----

    def ledger_contains(self, lake_table: str, row_key: str) -> bool:
        return (lake_table, row_key) in self._ledger

    def ledger_get(self, lake_table: str, row_key: str) -> LakeLedgerEntry | None:
        return self._ledger.get((lake_table, row_key))

    def ledger_add(
        self,
        lake_table: str,
        row_key: str,
        batch_id: str,
        evaluation_id: str,
        schema_version_id: str,
        payload: dict[str, Any],
    ) -> LakeLedgerEntry:
        """台账写入：同一 (湖表, 行主键) 已存在时直接返回旧记录，保证只落一次。"""
        existing = self._ledger.get((lake_table, row_key))
        if existing is not None:
            return existing
        entry = LakeLedgerEntry(
            row_key=row_key,
            lake_table=lake_table,
            batch_id=batch_id,
            evaluation_id=evaluation_id,
            schema_version_id=schema_version_id,
            payload=copy.deepcopy(payload),
            created_at=time.time(),
        )
        self._ledger[(lake_table, row_key)] = entry
        return entry

    def ledger_list(self, lake_table: str | None = None) -> list[LakeLedgerEntry]:
        entries = list(self._ledger.values())
        if lake_table is not None:
            entries = [e for e in entries if e.lake_table == lake_table]
        return sorted(entries, key=lambda e: e.created_at)

    def reset(self) -> None:
        """清空全部运行态数据（测试隔离用）。"""
        self._files.clear()
        self._snapshots.clear()
        self._watermarks.clear()
        self._batches.clear()
        self._schema_versions.clear()
        self._rule_versions.clear()
        self._evaluations.clear()
        self._quarantine_records.clear()
        self._quarantine.clear()
        self._ledger.clear()
        self._merge_checkpoints.clear()

    # ---- 隔离区（旧接口，保持兼容） ----

    def quarantine_rows(
        self, batch_id: str, rule_id: str, rule_name: str,
        rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """将被质量规则拦截的行放入隔离区。"""
        quarantined = []
        for row in rows:
            qr = dict(row)
            qr["_quarantine_batch"] = batch_id
            qr["_quarantine_rule"] = rule_id
            qr["_quarantine_rule_name"] = rule_name
            qr["_quarantine_time"] = time.time()
            quarantined.append(qr)
        self._quarantine.setdefault(batch_id, []).extend(quarantined)
        return quarantined

    def list_quarantine(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        if batch_id is not None:
            return list(self._quarantine.get(batch_id, []))
        result: list[dict[str, Any]] = []
        for rows in self._quarantine.values():
            result.extend(rows)
        return result

    def release_quarantine(self, batch_id: str) -> list[dict[str, Any]]:
        """回炉重入：从隔离区取出数据行。"""
        rows = self._quarantine.pop(batch_id, [])
        cleaned: list[dict[str, Any]] = []
        for r in rows:
            cleaned_row = {k: v for k, v in r.items() if not k.startswith("_quarantine_")}
            cleaned.append(cleaned_row)
        return cleaned

    # ---- 归并检查点 ----

    def save_merge_checkpoint(self, merge_id: str, partial_file_ids: list[str]) -> None:
        self._merge_checkpoints[merge_id] = {
            "partial_file_ids": list(partial_file_ids),
            "saved_at": time.time(),
        }

    def get_merge_checkpoint(self, merge_id: str) -> dict[str, Any] | None:
        return self._merge_checkpoints.get(merge_id)

    def clear_merge_checkpoint(self, merge_id: str) -> None:
        self._merge_checkpoints.pop(merge_id, None)

    # ---- 并发写锁 ----

    def acquire_write_lock(self, lake_table: str) -> threading.Lock:
        return self._write_lock.acquire(lake_table)

    def release_write_lock(self, lake_table: str) -> None:
        self._write_lock.release(lake_table)

    # ---- 统计快照 ----

    def stats_snapshot(self, lake_table: str) -> dict[str, Any]:
        """生成湖表统计画像快照。"""
        files = self.list_files(lake_table)
        snapshots = self.list_snapshots(lake_table)
        wm = self.get_watermark(lake_table)
        total_rows = sum(f.file_rows for f in files)
        total_bytes = sum(f.file_bytes for f in files)
        partition_count = len(set(f.partition_key for f in files))
        return {
            "lake_table": lake_table,
            "file_count": len(files),
            "total_rows": total_rows,
            "total_bytes": total_bytes,
            "partition_count": partition_count,
            "available_snapshots": sum(1 for s in snapshots if s.status == "available"),
            "watermark": wm.committed_value if wm else None,
        }


# 全局单例
lake_store = LakeStore()