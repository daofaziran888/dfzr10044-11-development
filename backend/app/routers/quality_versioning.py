"""结构版本化质量校验路由：结构/规则版本、按版本校验转换、隔离复检与回炉。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.quality_versioning import (
    QualityVersioningService,
    ReleaseConflictError,
    SchemaVersionError,
    quality_versioning_service,
)

router = APIRouter(prefix="/api/quality-versioning", tags=["结构版本化质量校验"])


def _handle(error: Exception) -> HTTPException:
    if isinstance(error, ReleaseConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SchemaVersionError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))


class ColumnPayload(BaseModel):
    name: str
    type: str = "STRING"
    nullable: bool = True
    unit: str = ""
    factor: float = 1.0


class SchemaRegisterPayload(BaseModel):
    lake_table: str
    version_id: str
    columns: list[ColumnPayload]
    parent_version_id: str | None = None
    conversions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reason: str = ""
    activate: bool = False


class SchemaActivatePayload(BaseModel):
    lake_table: str
    version_id: str


class RuleVersionPayload(BaseModel):
    lake_table: str
    rule_version_id: str
    schema_version_id: str
    rules: list[dict[str, Any]]


class EvaluationPayload(BaseModel):
    batch_id: str
    schema_version_id: str
    rule_version_id: str


class ConvertPayload(BaseModel):
    batch_id: str
    to_schema_version_id: str


class BatchIngestPayload(BaseModel):
    lake_table: str
    source_system: str
    rows: list[dict[str, Any]]
    window_start: str
    window_end: str
    schema_version_id: str


class RecheckPayload(BaseModel):
    schema_version_id: str | None = None
    rule_version_id: str | None = None
    corrected_input: dict[str, Any] | None = None


class RetryPayload(BaseModel):
    corrected_input: dict[str, Any]


def _service() -> QualityVersioningService:
    return quality_versioning_service


@router.post("/schemas")
def register_schema(payload: SchemaRegisterPayload) -> dict[str, Any]:
    try:
        return _service().register_schema(
            payload.lake_table,
            payload.version_id,
            [col.model_dump() for col in payload.columns],
            parent_version_id=payload.parent_version_id,
            conversions=payload.conversions,
            reason=payload.reason,
            activate=payload.activate,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转 HTTP 错误
        raise _handle(exc) from exc


@router.post("/schemas/activate")
def activate_schema(payload: SchemaActivatePayload) -> dict[str, Any]:
    try:
        return _service().activate_schema(payload.lake_table, payload.version_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/schemas")
def list_schemas(lake_table: str | None = None) -> dict[str, Any]:
    return {"items": _service().list_schemas(lake_table)}


@router.post("/batches")
def ingest_batch(payload: BatchIngestPayload) -> dict[str, Any]:
    """按结构版本接收批次：结构必须属于该湖表，批次固定采集时读到的版本。"""
    from app.services.ingest_batch import IngestBatchService

    service = _service()
    schema = service.store.get_schema_version(
        payload.lake_table, payload.schema_version_id
    )
    if schema is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"结构版本 {payload.schema_version_id} 不属于湖表 "
                f"{payload.lake_table}，拒绝按其他湖表的同名版本接收批次"
            ),
        )
    result = IngestBatchService().ingest_batch(
        payload.lake_table,
        payload.source_system,
        payload.rows,
        payload.window_start,
        payload.window_end,
        schema_version_id=payload.schema_version_id,
    )
    return result


@router.post("/rule-versions")
def publish_rule_version(payload: RuleVersionPayload) -> dict[str, Any]:
    try:
        return _service().publish_rule_version(
            payload.lake_table,
            payload.rule_version_id,
            payload.schema_version_id,
            payload.rules,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/rule-versions")
def list_rule_versions(lake_table: str | None = None) -> dict[str, Any]:
    return {"items": _service().list_rule_versions(lake_table)}


@router.post("/batches/convert")
def convert_batch(payload: ConvertPayload) -> dict[str, Any]:
    result = _service().convert_batch(payload.batch_id, payload.to_schema_version_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/evaluations")
def start_evaluation(payload: EvaluationPayload) -> dict[str, Any]:
    try:
        result = _service().start_batch_evaluation(
            payload.batch_id, payload.schema_version_id, payload.rule_version_id
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/evaluations/{evaluation_id}/confirm")
def confirm_evaluation(evaluation_id: str) -> dict[str, Any]:
    result = _service().confirm_evaluation(evaluation_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/evaluations")
def list_evaluations(
    lake_table: str | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    return {"items": _service().list_evaluations(lake_table, batch_id)}


@router.get("/quarantine")
def list_quarantine(
    lake_table: str | None = None,
    batch_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {"items": _service().list_quarantine(lake_table, batch_id, status)}


@router.get("/quarantine/{quarantine_id}")
def quarantine_detail(quarantine_id: str) -> dict[str, Any]:
    result = _service().get_quarantine_detail(quarantine_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@router.post("/quarantine/{quarantine_id}/recheck")
def recheck_quarantine(quarantine_id: str, payload: RecheckPayload) -> dict[str, Any]:
    try:
        result = _service().recheck_quarantine(
            quarantine_id,
            schema_version_id=payload.schema_version_id,
            rule_version_id=payload.rule_version_id,
            corrected_input=payload.corrected_input,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/quarantine/{quarantine_id}/retry")
def retry_quarantine(quarantine_id: str, payload: RetryPayload) -> dict[str, Any]:
    try:
        result = _service().retry_quarantine(quarantine_id, payload.corrected_input)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/ledger")
def ledger(lake_table: str | None = None) -> dict[str, Any]:
    return _service().list_ledger(lake_table)
