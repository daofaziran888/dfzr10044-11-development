"""按结构版本的质量校验与转换服务。

核心能力：
- 结构版本目录登记/生效，跨版本量纲转换（元 -> 分等）
- 规则版本化：每次发布冻结完整规则与阈值快照，在途校验期间发布新版本会冲突
- 按批次绑定的结构版本做校验与转换，逐行保留原输入、转换值与命中规则
- 确认时通过行进幂等落湖台账（同一行主键只出现一次），违规行进行级隔离区
- 隔离复检：查回初次判定版本与原输入，用同一套完整结构与规则复算
- 回炉重入：接受更正输入，走同一评估链路，回执可追溯原批次与更正输入各参与了哪次计算
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from app.lake_store import (
    ColumnSpec,
    EvaluationSession,
    LakeStore,
    QuarantineRecord,
    RowEvaluation,
    SchemaVersion,
    lake_store,
)


class SchemaVersionError(ValueError):
    """结构版本登记/读取错误（含跨湖表同名版本拒绝）。"""


class ReleaseConflictError(RuntimeError):
    """新版本发布与在途批次校验冲突。"""


def row_identity_key(row: dict[str, Any]) -> str:
    """生成稳定的行主键：优先业务主键，否则取整行内容指纹。"""
    for key_field in ("order_id", "record_id", "id"):
        value = row.get(key_field)
        if value not in (None, ""):
            return f"{key_field}={value}"
    digest = hashlib.md5(
        str(sorted(row.items(), key=lambda kv: str(kv[0]))).encode()
    ).hexdigest()
    return f"fingerprint={digest[:16]}"


def _to_number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value))


class QualityVersioningService:
    """结构版本化校验领域服务（无状态，全部状态落在 lake_store）。"""

    def __init__(self, store: LakeStore | None = None) -> None:
        self.store = store or lake_store

    # ------------------------------------------------------------------
    # 结构版本
    # ------------------------------------------------------------------

    def register_schema(
        self,
        lake_table: str,
        version_id: str,
        columns: list[dict[str, Any]],
        *,
        parent_version_id: str | None = None,
        conversions: dict[str, dict[str, Any]] | None = None,
        reason: str = "",
        activate: bool = False,
    ) -> dict[str, Any]:
        """登记（可选直接生效）一个结构版本。"""
        specs = [
            ColumnSpec(
                name=str(col["name"]),
                type=str(col.get("type", "STRING")),
                nullable=bool(col.get("nullable", True)),
                unit=str(col.get("unit", "")),
                factor=float(col.get("factor", 1.0)),
            )
            for col in columns
        ]
        try:
            sv = self.store.register_schema_version(
                version_id,
                lake_table,
                specs,
                parent_version_id=parent_version_id,
                conversions=conversions,
                reason=reason,
            )
        except ValueError as exc:
            raise SchemaVersionError(str(exc)) from exc
        if activate:
            self.activate_schema(lake_table, version_id)
        return self._schema_view(sv)

    def activate_schema(self, lake_table: str, version_id: str) -> dict[str, Any]:
        try:
            sv = self.store.activate_schema_version(version_id, lake_table)
        except KeyError as exc:
            raise SchemaVersionError(str(exc)) from exc
        except RuntimeError as exc:
            raise ReleaseConflictError(str(exc)) from exc
        return self._schema_view(sv)

    def list_schemas(self, lake_table: str | None = None) -> list[dict[str, Any]]:
        return [self._schema_view(sv) for sv in self.store.list_schema_versions(lake_table)]

    def _schema_view(self, sv: SchemaVersion) -> dict[str, Any]:
        return {
            "version_id": sv.version_id,
            "lake_table": sv.lake_table,
            "parent_version_id": sv.parent_version_id,
            "status": sv.status,
            "reason": sv.reason,
            "columns": [
                {
                    "name": col.name,
                    "type": col.type,
                    "nullable": col.nullable,
                    "unit": col.unit,
                    "factor": col.factor,
                }
                for col in sv.columns
            ],
            "conversions": sv.conversions,
        }

    # ------------------------------------------------------------------
    # 规则版本
    # ------------------------------------------------------------------

    def publish_rule_version(
        self,
        lake_table: str,
        rule_version_id: str,
        schema_version_id: str,
        rules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """发布不可变规则版本；存在在途校验时抛出 ReleaseConflictError。"""
        try:
            rv = self.store.create_rule_version(
                rule_version_id, lake_table, schema_version_id, rules
            )
        except KeyError as exc:
            raise SchemaVersionError(str(exc)) from exc
        except RuntimeError as exc:
            raise ReleaseConflictError(str(exc)) from exc
        return self._rule_view(rv)

    def list_rule_versions(self, lake_table: str | None = None) -> list[dict[str, Any]]:
        return [self._rule_view(rv) for rv in self.store.list_rule_versions(lake_table)]

    def _rule_view(self, rv) -> dict[str, Any]:
        return {
            "rule_version_id": rv.rule_version_id,
            "lake_table": rv.lake_table,
            "schema_version_id": rv.schema_version_id,
            "status": rv.status,
            "rules": rv.rules,
        }

    # ------------------------------------------------------------------
    # 跨版本转换
    # ------------------------------------------------------------------

    def _resolve_version_chain(
        self, lake_table: str, from_version_id: str, to_version_id: str
    ) -> list[SchemaVersion]:
        """解析 from -> to 的结构版本链（含两端），链不通则报错。"""
        if from_version_id == to_version_id:
            same = self.store.get_schema_version(lake_table, from_version_id)
            if same is None:
                raise SchemaVersionError(
                    f"湖表 {lake_table} 不存在结构版本 {from_version_id}，"
                    f"拒绝使用其他湖表的同名版本"
                )
            return [same]
        target = self.store.get_schema_version(lake_table, to_version_id)
        if target is None:
            raise SchemaVersionError(
                f"湖表 {lake_table} 不存在结构版本 {to_version_id}，"
                f"拒绝使用其他湖表的同名版本"
            )
        chain: list[SchemaVersion] = []
        cursor: SchemaVersion | None = target
        seen: set[str] = set()
        while cursor is not None:
            if cursor.version_id in seen:
                raise SchemaVersionError("结构版本链存在环，无法解析转换路径")
            seen.add(cursor.version_id)
            chain.append(cursor)
            if cursor.version_id == from_version_id:
                chain.reverse()
                return chain
            if not cursor.parent_version_id:
                break
            cursor = self.store.get_schema_version(lake_table, cursor.parent_version_id)
        raise SchemaVersionError(
            f"结构版本 {from_version_id} 不是 {to_version_id} 的祖先，"
            f"无法沿版本链转换湖表 {lake_table} 的批次"
        )

    def convert_row(
        self,
        lake_table: str,
        row: dict[str, Any],
        from_version_id: str,
        to_version_id: str,
    ) -> dict[str, Any]:
        """把一行原始输入沿结构版本链逐版转换到目标结构。

        - 目标列在 conversions 里声明了 source 时按 expr 换算（scale 乘倍率）
        - 未声明转换的同名列直接沿用
        - 量纲转换保留在 converted 输入里，原始输入永不被覆盖
        """
        if from_version_id == to_version_id:
            sv = self.store.get_schema_version(lake_table, from_version_id)
            if sv is None:
                raise SchemaVersionError(
                    f"湖表 {lake_table} 不存在结构版本 {from_version_id}"
                )
            return {col.name: row.get(col.name) for col in sv.columns}

        chain = self._resolve_version_chain(lake_table, from_version_id, to_version_id)
        current = dict(row)
        for sv in chain[1:]:
            converted: dict[str, Any] = {}
            for col in sv.columns:
                mapping = sv.conversions.get(col.name)
                if mapping and mapping.get("source") in current:
                    source_value = current[mapping["source"]]
                    expr = mapping.get("expr", "identity")
                    if expr == "scale":
                        factor = float(mapping.get("factor", col.factor or 1.0))
                        num = _to_number(source_value)
                        converted[col.name] = num * factor if num is not None else None
                    elif expr == "identity":
                        converted[col.name] = source_value
                    elif expr == "constant":
                        converted[col.name] = mapping.get("value")
                    else:
                        raise SchemaVersionError(f"不支持的转换表达式：{expr}")
                elif col.name in current:
                    converted[col.name] = current[col.name]
                else:
                    converted[col.name] = None
            current = converted
        return current

    def convert_batch(
        self, batch_id: str, to_version_id: str
    ) -> dict[str, Any]:
        """把整个批次从其绑定结构版本转换到目标结构版本（只读，不改原输入）。"""
        br = self.store.get_batch(batch_id)
        if br is None:
            return {"success": False, "error": "batch not found"}
        if not br.schema_version_id:
            return {"success": False, "error": "批次未保留结构版本，无法转换"}
        converted_rows = [
            self.convert_row(
                br.lake_table, row, br.schema_version_id, to_version_id
            )
            for row in br.rows
        ]
        return {
            "success": True,
            "batch_id": batch_id,
            "lake_table": br.lake_table,
            "from_schema_version_id": br.schema_version_id,
            "to_schema_version_id": to_version_id,
            "row_count": len(converted_rows),
            "converted_rows": converted_rows,
        }

    # ------------------------------------------------------------------
    # 规则评估
    # ------------------------------------------------------------------

    def _evaluate_row(
        self,
        index: int,
        raw_row: dict[str, Any],
        converted_row: dict[str, Any],
        rules: list[dict[str, Any]],
    ) -> RowEvaluation:
        hit_rules: list[str] = []
        evaluated_values: dict[str, Any] = {}
        for rule in rules:
            rule_id = str(rule.get("规则编号", rule.get("rule_id", "")))
            col = str(rule.get("校验列", rule.get("column", "")))
            rule_type = str(rule.get("校验类型", rule.get("rule_type", "")))
            if col not in converted_row:
                continue
            value = converted_row[col]
            evaluated_values[col] = value
            if rule_type == "NOT_NULL":
                if value is None or str(value).strip() == "":
                    hit_rules.append(rule_id)
            elif rule_type == "RANGE":
                num = _to_number(value)
                if num is None:
                    hit_rules.append(rule_id)
                    continue
                lo = rule.get("阈值下限", rule.get("min"))
                hi = rule.get("阈值上限", rule.get("max"))
                lo_num = _to_number(lo)
                hi_num = _to_number(hi)
                if (lo_num is not None and num < lo_num) or (
                    hi_num is not None and num > hi_num
                ):
                    hit_rules.append(rule_id)
        return RowEvaluation(
            row_index=index,
            row_key=row_identity_key(raw_row),
            raw_input=dict(raw_row),
            converted_input=dict(converted_row),
            verdict="violated" if hit_rules else "passed",
            hit_rules=hit_rules,
            evaluated_values=evaluated_values,
        )

    def _run_evaluation(
        self,
        *,
        batch_id: str,
        rows: list[dict[str, Any]],
        schema_version_id: str,
        rule_version_id: str,
        origin: str,
        source_evaluation_id: str | None,
        from_schema_version_id: str | None = None,
    ) -> EvaluationSession:
        br = self.store.get_batch(batch_id)
        if br is None:
            raise SchemaVersionError("batch not found")
        rv = self.store.get_rule_version(rule_version_id)
        if rv is None:
            raise SchemaVersionError(f"规则版本 {rule_version_id} 不存在")
        target_schema = self.store.get_schema_version(br.lake_table, schema_version_id)
        if target_schema is None:
            raise SchemaVersionError(
                f"湖表 {br.lake_table} 不存在结构版本 {schema_version_id}，"
                f"拒绝引用其他湖表的同名版本"
            )
        if rv.schema_version_id != schema_version_id:
            raise SchemaVersionError(
                f"规则版本 {rule_version_id} 绑定的是结构 "
                f"{rv.schema_version_id}，不能用于结构 {schema_version_id} 的校验"
            )
        # 源结构优先取显式传入（复检/回炉沿用隔离记录固定版本），
        # 其次取批次采集版本，最后与目标版本相同。
        source_schema = (
            from_schema_version_id
            or br.schema_version_id
            or schema_version_id
        )
        if self.store.get_schema_version(br.lake_table, source_schema) is None:
            raise SchemaVersionError(
                f"批次原结构版本 {source_schema} 在湖表 {br.lake_table} 上不存在"
            )

        evaluations: list[RowEvaluation] = []
        for index, raw_row in enumerate(rows):
            converted = self.convert_row(
                br.lake_table, raw_row, source_schema, schema_version_id
            )
            evaluations.append(
                self._evaluate_row(index, raw_row, converted, rv.rules)
            )

        session = EvaluationSession(
            evaluation_id=f"eval-{uuid.uuid4().hex[:10]}",
            batch_id=batch_id,
            lake_table=br.lake_table,
            schema_version_id=schema_version_id,
            rule_version_id=rule_version_id,
            origin=origin,
            source_evaluation_id=source_evaluation_id,
            status="validating",
            row_evaluations=evaluations,
            created_at=time.time(),
        )
        return self.store.save_evaluation(session)

    def start_batch_evaluation(
        self, batch_id: str, schema_version_id: str, rule_version_id: str
    ) -> dict[str, Any]:
        """对在途批次启动按结构版本的校验，会话处于 validating，阻塞冲突发布。"""
        br = self.store.get_batch(batch_id)
        if br is None:
            return {"success": False, "error": "batch not found"}
        # 固定批次当时读到的结构版本（若尚未固定）
        if not br.schema_version_id:
            self.store.bind_batch_versions(batch_id, schema_version_id)
        session = self._run_evaluation(
            batch_id=batch_id,
            rows=br.rows,
            schema_version_id=schema_version_id,
            rule_version_id=rule_version_id,
            origin="initial",
            source_evaluation_id=None,
        )
        return self._evaluation_view(session)

    def confirm_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        """确认评估：通过行进幂等台账，违规行进隔离区；会话转 confirmed 释放发布锁。"""
        session = self.store.get_evaluation(evaluation_id)
        if session is None:
            return {"success": False, "error": "evaluation not found"}
        br = self.store.get_batch(session.batch_id)
        if br is None:
            return {"success": False, "error": "batch not found"}

        passed: list[dict[str, Any]] = []
        isolated: list[dict[str, Any]] = []
        ledger_added = 0
        ledger_duplicated = 0

        for row_eval in session.row_evaluations:
            if row_eval.verdict == "passed":
                if self.store.ledger_contains(session.lake_table, row_eval.row_key):
                    ledger_duplicated += 1
                    continue
                self.store.ledger_add(
                    session.lake_table,
                    row_eval.row_key,
                    session.batch_id,
                    session.evaluation_id,
                    session.schema_version_id,
                    row_eval.converted_input,
                )
                ledger_added += 1
                passed.append({
                    "row_key": row_eval.row_key,
                    "payload": row_eval.converted_input,
                })
            else:
                existing = self.store.find_quarantine_record(
                    session.batch_id, row_eval.row_key
                )
                if existing is not None:
                    # 同一批次同一行已隔离，追加本次判定轨迹而不是重复建单
                    record = existing
                else:
                    record = QuarantineRecord(
                        quarantine_id=f"quar-{uuid.uuid4().hex[:10]}",
                        batch_id=session.batch_id,
                        lake_table=session.lake_table,
                        row_key=row_eval.row_key,
                        original_input=dict(row_eval.raw_input),
                        corrected_input=None,
                        rule_id=",".join(row_eval.hit_rules),
                        rule_name="版本化质量规则命中",
                        schema_version_id=session.schema_version_id,
                        rule_version_id=session.rule_version_id,
                        initial_evaluation_id=session.evaluation_id,
                        status="isolated",
                        created_at=time.time(),
                    )
                    self.store.add_quarantine_record(record)
                self.store.append_quarantine_evaluation(record.quarantine_id, {
                    "evaluation_id": session.evaluation_id,
                    "origin": session.origin,
                    "schema_version_id": session.schema_version_id,
                    "rule_version_id": session.rule_version_id,
                    "verdict": row_eval.verdict,
                    "hit_rules": row_eval.hit_rules,
                    "raw_input": row_eval.raw_input,
                    "converted_input": row_eval.converted_input,
                    "evaluated_values": row_eval.evaluated_values,
                })
                isolated.append({
                    "quarantine_id": record.quarantine_id,
                    "row_key": row_eval.row_key,
                    "hit_rules": row_eval.hit_rules,
                })

        self.store.mark_evaluation_confirmed(evaluation_id)
        self.store.bind_batch_versions(
            session.batch_id, session.schema_version_id, session.rule_version_id
        )
        return {
            "success": True,
            "evaluation_id": evaluation_id,
            "batch_id": session.batch_id,
            "status": "confirmed",
            "schema_version_id": session.schema_version_id,
            "rule_version_id": session.rule_version_id,
            "total_rows": len(session.row_evaluations),
            "passed_rows": len(passed),
            "isolated_rows": len(isolated),
            "ledger_added": ledger_added,
            "ledger_duplicated": ledger_duplicated,
            "passed": passed,
            "isolated": isolated,
        }

    # ------------------------------------------------------------------
    # 隔离复检与回炉
    # ------------------------------------------------------------------

    def get_quarantine_detail(self, quarantine_id: str) -> dict[str, Any]:
        """隔离详情：初次判定版本、原输入、更正输入与历次计算轨迹。"""
        record = self.store.get_quarantine_record(quarantine_id)
        if record is None:
            return {"success": False, "error": "quarantine record not found"}
        return {
            "success": True,
            "quarantine_id": record.quarantine_id,
            "batch_id": record.batch_id,
            "lake_table": record.lake_table,
            "row_key": record.row_key,
            "status": record.status,
            "rule_id": record.rule_id,
            "original_input": record.original_input,
            "corrected_input": record.corrected_input,
            "initial_evaluation_id": record.initial_evaluation_id,
            "schema_version_id": record.schema_version_id,
            "rule_version_id": record.rule_version_id,
            "evaluation_history": record.evaluations,
        }

    def recheck_quarantine(
        self,
        quarantine_id: str,
        *,
        schema_version_id: str | None = None,
        rule_version_id: str | None = None,
        corrected_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """用同一套完整结构与规则复算隔离行（不落湖、不改状态），返回复检结论。

        默认沿用隔离记录上固定的初次判定版本；也可显式指定新版本做口径对比。
        """
        record = self.store.get_quarantine_record(quarantine_id)
        if record is None:
            return {"success": False, "error": "quarantine record not found"}
        schema_id = schema_version_id or record.schema_version_id
        rule_id = rule_version_id or record.rule_version_id
        # 跨湖表同名版本防御：目标结构必须属于隔离记录所在湖表
        target_schema = self.store.get_schema_version(record.lake_table, schema_id)
        if target_schema is None:
            return {
                "success": False,
                "error": (
                    f"结构版本 {schema_id} 不属于湖表 {record.lake_table}，"
                    f"拒绝使用另一张湖表的同名版本复检"
                ),
            }
        rv = self.store.get_rule_version(rule_id)
        if rv is None:
            return {"success": False, "error": f"规则版本 {rule_id} 不存在"}

        input_row = corrected_input if corrected_input is not None else record.original_input
        # 默认按隔离记录固定的初次判定版本复算；显式指定新版本时，
        # 原输入仍以批次采集结构为源，沿版本链转换到目标结构。
        if schema_version_id is None or schema_version_id == record.schema_version_id:
            source_schema = record.schema_version_id
        else:
            br = self.store.get_batch(record.batch_id)
            source_schema = br.schema_version_id if br else record.schema_version_id
        converted = self.convert_row(
            record.lake_table, input_row, source_schema, schema_id
        )
        row_eval = self._evaluate_row(0, input_row, converted, rv.rules)

        session = self.store.save_evaluation(EvaluationSession(
            evaluation_id=f"eval-{uuid.uuid4().hex[:10]}",
            batch_id=record.batch_id,
            lake_table=record.lake_table,
            schema_version_id=schema_id,
            rule_version_id=rule_id,
            origin="recheck",
            source_evaluation_id=record.initial_evaluation_id,
            status="validating",
            row_evaluations=[row_eval],
            created_at=time.time(),
        ))
        self.store.append_quarantine_evaluation(quarantine_id, {
            "evaluation_id": session.evaluation_id,
            "origin": "recheck",
            "schema_version_id": schema_id,
            "rule_version_id": rule_id,
            "verdict": row_eval.verdict,
            "hit_rules": row_eval.hit_rules,
            "raw_input": row_eval.raw_input,
            "converted_input": row_eval.converted_input,
            "evaluated_values": row_eval.evaluated_values,
            "corrected": corrected_input is not None,
        })
        return {
            "success": True,
            "quarantine_id": quarantine_id,
            "evaluation_id": session.evaluation_id,
            "initial_evaluation_id": record.initial_evaluation_id,
            "schema_version_id": schema_id,
            "rule_version_id": rule_id,
            "input_used": "corrected_input" if corrected_input is not None else "original_input",
            "verdict": row_eval.verdict,
            "hit_rules": row_eval.hit_rules,
            "raw_input": row_eval.raw_input,
            "converted_input": row_eval.converted_input,
            "evaluated_values": row_eval.evaluated_values,
        }

    def retry_quarantine(
        self, quarantine_id: str, corrected_input: dict[str, Any]
    ) -> dict[str, Any]:
        """通过原回炉接口重试：更正输入走同一评估链路，通过则幂等落湖并释放隔离。

        回执同时给出原批次与更正输入分别参与的评估编号，保证可追溯。
        """
        record = self.store.get_quarantine_record(quarantine_id)
        if record is None:
            return {"success": False, "error": "quarantine record not found"}
        if record.status == "released":
            return {"success": False, "error": "该隔离记录已回炉，禁止重复重投"}

        br = self.store.get_batch(record.batch_id)
        if br is None:
            return {"success": False, "error": "source batch not found"}

        # 回炉必须沿用隔离记录固定的初次判定结构与规则，
        # 不受批次后续被其他评估更新绑定版本的影响。
        session = self._run_evaluation(
            batch_id=record.batch_id,
            rows=[corrected_input],
            schema_version_id=record.schema_version_id,
            rule_version_id=record.rule_version_id,
            origin="retry",
            source_evaluation_id=record.initial_evaluation_id,
            from_schema_version_id=record.schema_version_id,
        )
        row_eval = session.row_evaluations[0]
        self.store.append_quarantine_evaluation(quarantine_id, {
            "evaluation_id": session.evaluation_id,
            "origin": "retry",
            "schema_version_id": session.schema_version_id,
            "rule_version_id": session.rule_version_id,
            "verdict": row_eval.verdict,
            "hit_rules": row_eval.hit_rules,
            "raw_input": row_eval.raw_input,
            "converted_input": row_eval.converted_input,
            "evaluated_values": row_eval.evaluated_values,
            "corrected": True,
        })

        result: dict[str, Any] = {
            "success": True,
            "quarantine_id": quarantine_id,
            "batch_id": record.batch_id,
            "evaluation_id": session.evaluation_id,
            "initial_evaluation_id": record.initial_evaluation_id,
            "original_input": record.original_input,
            "corrected_input": corrected_input,
            "schema_version_id": session.schema_version_id,
            "rule_version_id": session.rule_version_id,
            "verdict": row_eval.verdict,
            "hit_rules": row_eval.hit_rules,
            "converted_input": row_eval.converted_input,
        }

        if row_eval.verdict != "passed":
            self.store.mark_evaluation_confirmed(session.evaluation_id)
            result.update({
                "released": False,
                "message": "更正输入复算仍未通过，隔离记录保持隔离",
            })
            return result

        row_key = row_identity_key(corrected_input)
        already_in_ledger = self.store.ledger_contains(record.lake_table, row_key)
        entry = self.store.ledger_add(
            record.lake_table,
            row_key,
            record.batch_id,
            session.evaluation_id,
            session.schema_version_id,
            row_eval.converted_input,
        )
        self.store.mark_evaluation_confirmed(session.evaluation_id)
        self.store.update_quarantine_status(
            quarantine_id, "released", corrected_input=corrected_input
        )
        result.update({
            "released": True,
            "row_key": row_key,
            "ledger_duplicated": already_in_ledger,
            "ledger_entry": {
                "row_key": entry.row_key,
                "batch_id": entry.batch_id,
                "evaluation_id": entry.evaluation_id,
                "schema_version_id": entry.schema_version_id,
            },
            "message": "更正输入复算通过，已幂等落湖并释放隔离",
        })
        return result

    # ------------------------------------------------------------------
    # 台账与查询
    # ------------------------------------------------------------------

    def list_ledger(self, lake_table: str | None = None) -> dict[str, Any]:
        entries = self.store.ledger_list(lake_table)
        return {
            "lake_table": lake_table,
            "total_valid_rows": len(entries),
            "items": [
                {
                    "row_key": e.row_key,
                    "lake_table": e.lake_table,
                    "batch_id": e.batch_id,
                    "evaluation_id": e.evaluation_id,
                    "schema_version_id": e.schema_version_id,
                    "payload": e.payload,
                }
                for e in entries
            ],
        }

    def list_evaluations(
        self, lake_table: str | None = None, batch_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            self._evaluation_view(s)
            for s in self.store.list_evaluations(lake_table, batch_id)
        ]

    def list_quarantine(
        self,
        lake_table: str | None = None,
        batch_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        records = self.store.list_quarantine_records(lake_table, batch_id, status)
        return [
            {
                "quarantine_id": r.quarantine_id,
                "batch_id": r.batch_id,
                "lake_table": r.lake_table,
                "row_key": r.row_key,
                "rule_id": r.rule_id,
                "status": r.status,
                "schema_version_id": r.schema_version_id,
                "rule_version_id": r.rule_version_id,
                "initial_evaluation_id": r.initial_evaluation_id,
                "original_input": r.original_input,
                "corrected_input": r.corrected_input,
                "evaluation_count": len(r.evaluations),
            }
            for r in records
        ]

    def _evaluation_view(self, session: EvaluationSession) -> dict[str, Any]:
        passed = sum(1 for e in session.row_evaluations if e.verdict == "passed")
        violated = len(session.row_evaluations) - passed
        return {
            "success": True,
            "evaluation_id": session.evaluation_id,
            "batch_id": session.batch_id,
            "lake_table": session.lake_table,
            "schema_version_id": session.schema_version_id,
            "rule_version_id": session.rule_version_id,
            "origin": session.origin,
            "source_evaluation_id": session.source_evaluation_id,
            "status": session.status,
            "total_rows": len(session.row_evaluations),
            "passed_rows": passed,
            "violated_rows": violated,
            "rows": [
                {
                    "row_index": e.row_index,
                    "row_key": e.row_key,
                    "verdict": e.verdict,
                    "hit_rules": e.hit_rules,
                    "raw_input": e.raw_input,
                    "converted_input": e.converted_input,
                    "evaluated_values": e.evaluated_values,
                }
                for e in session.row_evaluations
            ],
        }


# 模块级单例，供路由直接使用
quality_versioning_service = QualityVersioningService()
