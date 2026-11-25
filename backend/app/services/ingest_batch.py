"""入湖批次领域服务：批次入库、去重、快照绑定与校验值对比。"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.lake_store import lake_store


class IngestBatchService(BaseModuleService):
    MODULE = "ingest_batch"
    ENTITY = "入湖批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次编号", "所属湖表", "来源系统", "批次行数", "采集窗口", "落湖快照", "完整校验值", "入湖时间"]
    REQUIRED_FIELDS = ["批次编号", "所属湖表", "来源系统"]
    STATUS_ORDER = ["待入湖", "入湖中", "已入湖", "入湖失败"]
    ACTION_RULES = {"启动入湖": "入湖中", "确认入湖": "已入湖", "标记失败": "入湖失败"}
    NEGATIVE_ACTIONS = []

    def ingest_batch(self, lake_table: str, source_system: str,
                     rows: list[dict[str, Any]], window_start: str,
                     window_end: str,
                     schema_version_id: str | None = None) -> dict[str, Any]:
        """接收入湖批次数据：去重、存储、并与快照绑定。

        schema_version_id 固定批次采集当时读到的结构版本，后续校验/复检/回炉
        都能从批次查回同一套完整结构。
        """
        batch_id = f"batch-{lake_table}-{uuid.uuid4().hex[:6]}"
        # 先去重再存储
        seen = set()
        deduped: list[dict[str, Any]] = []
        for r in rows:
            key = hashlib.md5(str(sorted(r.items())).encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        br = lake_store.store_batch(batch_id, lake_table, source_system,
                                    deduped, window_start, window_end,
                                    schema_version_id=schema_version_id)
        lake_store.update_batch_status(batch_id, "ingesting")
        return {
            "batch_id": batch_id,
            "lake_table": lake_table,
            "row_count": len(deduped),
            "duplicates_removed": len(rows) - len(deduped),
            "checksum": br.checksum,
            "schema_version_id": br.schema_version_id,
            "window": f"{window_start} ~ {window_end}",
        }

    def verify_batch_integrity(self, batch_id: str) -> dict[str, Any]:
        """校验批次完整性：重新计算校验值并对比。"""
        br = lake_store.get_batch(batch_id)
        if br is None:
            return {"success": False, "error": "batch not found"}
        recalc = hashlib.md5(
            str(sorted([str(r) for r in br.rows])).encode()
        ).hexdigest()
        return {
            "batch_id": batch_id,
            "stored_checksum": br.checksum,
            "recalculated_checksum": recalc,
            "integrity_ok": recalc == br.checksum,
        }

    def promote_to_ingested(self, batch_id: str, snapshot_id: str) -> dict[str, Any]:
        """操作已入湖：绑定快照并标记已入湖。"""
        br = lake_store.update_batch_status(batch_id, "ingested", snapshot_id)
        if br is None:
            return {"success": False, "error": "batch not found"}
        return {
            "batch_id": batch_id,
            "status": "ingested",
            "snapshot_id": snapshot_id,
        }