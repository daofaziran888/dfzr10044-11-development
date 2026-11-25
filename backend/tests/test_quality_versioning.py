"""结构版本化质量校验端到端验收。

业务场景：
- 结构 V1 的 amount 按元保存，阈值上限 100；V2 换成以分保存的 amount_cents
- 含 70 元与 130 元的旧批在 V2 生效时仍按 V1 在途校验
- 70 元不能因读错量纲被隔离，130 元不能因更换字段无故通过
- 阈值口径变更后按同一套完整结构与规则复算通过/隔离明细
- 隔离复检可查初次判定版本与原输入；另一张湖表的同名版本必须拒绝
- V2 发布与按 V1 校验的在途批次确认发生冲突
- 复检 130 元行并通过原回炉接口重投，入湖台账中同一笔有效数据只出现一次
- 回执可追溯原批次与更正输入分别参与了哪次计算
"""
from __future__ import annotations

import pytest

from app.lake_store import lake_store
from app.services.quality_versioning import (
    QualityVersioningService,
    ReleaseConflictError,
    SchemaVersionError,
)
from app.services.ingest_batch import IngestBatchService


TABLE = "ods_payment_v"
OTHER_TABLE = "ods_other_table"


def _v1_schema() -> list[dict]:
    return [
        {"name": "order_id", "type": "STRING", "nullable": False},
        {"name": "amount", "type": "DECIMAL(18,2)", "nullable": False,
         "unit": "yuan", "factor": 1.0},
    ]


def _v2_schema() -> list[dict]:
    return [
        {"name": "order_id", "type": "STRING", "nullable": False},
        {"name": "amount_cents", "type": "BIGINT", "nullable": False,
         "unit": "cent", "factor": 100.0},
    ]


def _v1_rules() -> list[dict]:
    # V1 口径：amount 按元，上限 100
    return [
        {"规则编号": "QR-AMT-V1", "校验列": "amount", "校验类型": "RANGE",
         "阈值下限": 0, "阈值上限": 100, "违规处置": "隔离"},
    ]


def _v2_rules() -> list[dict]:
    # V2 口径：amount_cents 按分，上限 10000 分（=100 元），阈值随规则版本冻结
    return [
        {"规则编号": "QR-AMT-V2", "校验列": "amount_cents", "校验类型": "RANGE",
         "阈值下限": 0, "阈值上限": 10000, "违规处置": "隔离"},
    ]


@pytest.fixture
def svc() -> QualityVersioningService:
    lake_store.reset()
    service = QualityVersioningService()
    # V1 登记并生效
    service.register_schema(
        TABLE, "SCH-PAY-V1", _v1_schema(), reason="初始结构：amount 按元",
        activate=True,
    )
    # V2 登记（暂不生效）：amount_cents 由 amount 乘以 100 换算
    service.register_schema(
        TABLE, "SCH-PAY-V2", _v2_schema(),
        parent_version_id="SCH-PAY-V1",
        conversions={
            "amount_cents": {"source": "amount", "expr": "scale", "factor": 100.0},
        },
        reason="金额改为按分存储，amount -> amount_cents",
    )
    service.publish_rule_version(TABLE, "RV-PAY-1", "SCH-PAY-V1", _v1_rules())
    return service


def _old_batch() -> str:
    """一个含 70 元与 130 元的旧批，按 V1 结构采集。"""
    ingest = IngestBatchService()
    result = ingest.ingest_batch(
        TABLE, "OLTP_DB",
        [
            {"order_id": "ORD-70", "amount": 70},
            {"order_id": "ORD-130", "amount": 130},
        ],
        "2026-11-25T00:00:00", "2026-11-25T12:00:00",
        schema_version_id="SCH-PAY-V1",
    )
    return result["batch_id"]


class TestSchemaVersioning:
    def test_batch_keeps_schema_version_and_raw_input(self, svc):
        """批次保留采集时的结构版本，且原始输入不被转换覆盖。"""
        batch_id = _old_batch()
        br = lake_store.get_batch(batch_id)
        assert br.schema_version_id == "SCH-PAY-V1"
        assert br.rows[0]["amount"] == 70  # 仍是元，原输入保留

    def test_other_lake_table_same_version_id_rejected(self, svc):
        """另一张湖表登记同名结构版本必须拒绝。"""
        with pytest.raises(SchemaVersionError) as exc:
            svc.register_schema(OTHER_TABLE, "SCH-PAY-V1", _v1_schema())
        assert "同名版本" in str(exc.value) or "已被湖表" in str(exc.value)

    def test_convert_batch_v1_to_v2_scales_dimension(self, svc):
        """V1 元 -> V2 分：70 元转换为 7000 分，130 元转换为 13000 分。"""
        batch_id = _old_batch()
        result = svc.convert_batch(batch_id, "SCH-PAY-V2")
        assert result["success"]
        rows = result["converted_rows"]
        assert rows[0]["amount_cents"] == 7000
        assert rows[1]["amount_cents"] == 13000
        # 原始批次行未被改写
        assert lake_store.get_batch(batch_id).rows[0]["amount"] == 70

    def test_reject_foreign_schema_on_convert(self, svc):
        """转换时引用别的湖表的同名版本要拒绝。"""
        svc.register_schema(OTHER_TABLE, "SCH-OWN-V1", _v1_schema(), activate=True)
        batch_id = _old_batch()
        with pytest.raises(SchemaVersionError):
            svc.convert_row(TABLE, {"order_id": "x", "amount": 1},
                            "SCH-PAY-V1", "SCH-OWN-V1")


class TestInFlightEvaluation:
    def test_v1_evaluation_keeps_70_passed_and_130_isolated(self, svc):
        """在途批次按 V1 完整结构与规则复算：70 通过、130 隔离。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        assert view["status"] == "validating"
        rows = {r["row_key"]: r for r in view["rows"]}
        row70 = rows["order_id=ORD-70"]
        row130 = rows["order_id=ORD-130"]
        assert row70["verdict"] == "passed"
        assert row70["evaluated_values"]["amount"] == 70
        assert row130["verdict"] == "violated"
        assert row130["hit_rules"] == ["QR-AMT-V1"]

    def test_v2_publish_conflicts_with_inflight_v1_evaluation(self, svc):
        """V2 规则版本发布与按 V1 校验的在途批次冲突，必须拒绝。"""
        batch_id = _old_batch()
        svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        # 结构 V2 生效同样冲突
        with pytest.raises(ReleaseConflictError):
            svc.activate_schema(TABLE, "SCH-PAY-V2")
        # V2 规则版本发布也冲突
        with pytest.raises(ReleaseConflictError):
            svc.publish_rule_version(TABLE, "RV-PAY-2", "SCH-PAY-V2", _v2_rules())

    def test_confirm_after_conflict_window_uses_same_v1_snapshot(self, svc):
        """冲突解除后确认：仍用评估时冻结的 V1 结构/规则，结论不被新口径改写。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        # 在途期间 V2 发布被拒
        with pytest.raises(ReleaseConflictError):
            svc.publish_rule_version(TABLE, "RV-PAY-2", "SCH-PAY-V2", _v2_rules())
        # 确认在途评估（按 V1 复算）
        confirmed = svc.confirm_evaluation(view["evaluation_id"])
        assert confirmed["schema_version_id"] == "SCH-PAY-V1"
        assert confirmed["rule_version_id"] == "RV-PAY-1"
        assert confirmed["passed_rows"] == 1
        assert confirmed["isolated_rows"] == 1

        # 确认后在途锁释放，V2 结构与规则才能发布
        svc.activate_schema(TABLE, "SCH-PAY-V2")
        svc.publish_rule_version(TABLE, "RV-PAY-2", "SCH-PAY-V2", _v2_rules())

    def test_70_not_isolated_by_wrong_dimension_on_v2(self, svc):
        """量纲正确性：若直接拿 70（元）当分跟 10000 比会通过，但按链转换后
        70 元=7000 分仍在阈值内；关键是 70 不会被错误隔离，130 不会因换字段漏过。"""
        batch_id = _old_batch()
        # 先把 V1 在途评估确认掉，解除发布锁
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(view["evaluation_id"])
        svc.activate_schema(TABLE, "SCH-PAY-V2")
        svc.publish_rule_version(TABLE, "RV-PAY-2", "SCH-PAY-V2", _v2_rules())

        # 用 V2 结构与规则对同一旧批复算（原始输入仍是元，沿版本链转换）
        v2_view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V2", "RV-PAY-2")
        rows = {r["row_key"]: r for r in v2_view["rows"]}
        row70 = rows["order_id=ORD-70"]
        row130 = rows["order_id=ORD-130"]
        # 70 元 -> 7000 分 <= 10000，通过，不能因量纲被隔离
        assert row70["converted_input"]["amount_cents"] == 7000
        assert row70["verdict"] == "passed"
        # 130 元 -> 13000 分 > 10000，仍违规，不能因换字段无故通过
        assert row130["converted_input"]["amount_cents"] == 13000
        assert row130["verdict"] == "violated"
        assert row130["hit_rules"] == ["QR-AMT-V2"]

    def test_rule_version_bound_to_schema_cannot_cross_apply(self, svc):
        """规则版本与结构版本绑定，V1 规则不能拿去校验 V2 结构。"""
        batch_id = _old_batch()
        with pytest.raises(SchemaVersionError):
            svc.start_batch_evaluation(batch_id, "SCH-PAY-V2", "RV-PAY-1")


class TestQuarantineRecheckAndRetry:
    def test_quarantine_detail_keeps_initial_version_and_raw_input(self, svc):
        """隔离详情可查初次判定版本与原输入。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        confirmed = svc.confirm_evaluation(view["evaluation_id"])
        quar_id = next(
            item["quarantine_id"] for item in confirmed["isolated"]
            if item["row_key"] == "order_id=ORD-130"
        )
        detail = svc.get_quarantine_detail(quar_id)
        assert detail["success"]
        assert detail["schema_version_id"] == "SCH-PAY-V1"
        assert detail["rule_version_id"] == "RV-PAY-1"
        assert detail["initial_evaluation_id"] == view["evaluation_id"]
        assert detail["original_input"] == {"order_id": "ORD-130", "amount": 130}
        assert detail["status"] == "isolated"

    def test_recheck_reproduces_violation_with_same_structure_and_rules(self, svc):
        """复检用同一套完整结构与规则复算，130 元仍违规。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(view["evaluation_id"])
        quar = svc.list_quarantine(batch_id=batch_id)[0]
        recheck = svc.recheck_quarantine(quar["quarantine_id"])
        assert recheck["verdict"] == "violated"
        assert recheck["hit_rules"] == ["QR-AMT-V1"]
        # 复检默认沿用初次判定版本
        assert recheck["schema_version_id"] == "SCH-PAY-V1"
        assert recheck["rule_version_id"] == "RV-PAY-1"
        assert recheck["input_used"] == "original_input"

    def test_recheck_rejects_other_lake_table_same_named_version(self, svc):
        """隔离复检使用另一张湖表的同名版本必须拒绝。"""
        # 在另一张湖表登记一个本表不存在的版本号，模拟跨表同名结构
        svc.register_schema(OTHER_TABLE, "SCH-FOREIGN-V9", _v2_schema(),
                            activate=True)
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(view["evaluation_id"])
        quar = svc.list_quarantine(batch_id=batch_id)[0]
        rejected = svc.recheck_quarantine(
            quar["quarantine_id"], schema_version_id="SCH-FOREIGN-V9"
        )
        assert rejected["success"] is False
        assert "另一张湖表" in rejected["error"] or "不属于湖表" in rejected["error"]

    def test_retry_corrected_130_row_lands_once_in_lake(self, svc):
        """复检 130 元行后通过原回炉接口重投更正输入，台账中同一笔只出现一次。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        confirmed = svc.confirm_evaluation(view["evaluation_id"])
        quar_id = next(
            item["quarantine_id"] for item in confirmed["isolated"]
            if item["row_key"] == "order_id=ORD-130"
        )

        ledger_before = svc.list_ledger(TABLE)
        keys_before = {item["row_key"] for item in ledger_before["items"]}
        assert "order_id=ORD-130" not in keys_before
        assert "order_id=ORD-70" in keys_before  # 70 元确认时已落湖

        # 先复检：把 130 元更正为 80 元，复算应通过
        recheck = svc.recheck_quarantine(
            quar_id, corrected_input={"order_id": "ORD-130", "amount": 80}
        )
        assert recheck["verdict"] == "passed"
        assert recheck["input_used"] == "corrected_input"

        # 通过原回炉接口重试（同一入口，同一套结构与规则）
        retry = svc.retry_quarantine(
            quar_id, corrected_input={"order_id": "ORD-130", "amount": 80}
        )
        assert retry["released"] is True
        assert retry["verdict"] == "passed"
        assert retry["initial_evaluation_id"] == view["evaluation_id"]
        assert retry["evaluation_id"] != view["evaluation_id"]

        # 台账唯一性：ORD-130 只出现一次
        ledger = svc.list_ledger(TABLE)
        keys = [item["row_key"] for item in ledger["items"]]
        assert keys.count("order_id=ORD-130") == 1
        assert sorted(keys) == ["order_id=ORD-130", "order_id=ORD-70"]
        assert ledger["total_valid_rows"] == 2

        # 重复重投被禁止（隔离记录已释放）
        again = svc.retry_quarantine(
            quar_id, corrected_input={"order_id": "ORD-130", "amount": 80}
        )
        assert again["success"] is False

        # 隔离记录已释放；底层幂等台账保证同一行不会产生第二条
        assert lake_store.ledger_contains(TABLE, "order_id=ORD-130")
        entry = lake_store.ledger_get(TABLE, "order_id=ORD-130")
        # 台账记录指向回炉评估，而非初次评估
        assert entry.evaluation_id == retry["evaluation_id"]

    def test_retry_still_violated_keeps_quarantine(self, svc):
        """更正后仍超阈值：回炉复算不通过，隔离记录保持隔离且不进台账。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(view["evaluation_id"])
        quar = svc.list_quarantine(batch_id=batch_id)[0]
        retry = svc.retry_quarantine(
            quar["quarantine_id"],
            corrected_input={"order_id": "ORD-130", "amount": 999},
        )
        assert retry["released"] is False
        assert retry["verdict"] == "violated"
        record = lake_store.get_quarantine_record(quar["quarantine_id"])
        assert record.status == "isolated"
        assert not lake_store.ledger_contains(TABLE, "order_id=ORD-130")

    def test_receipt_traces_original_and_corrected_evaluations(self, svc):
        """回执可查回原批次和更正输入分别参与了哪次计算。"""
        batch_id = _old_batch()
        view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(view["evaluation_id"])
        quar = svc.list_quarantine(batch_id=batch_id)[0]

        retry = svc.retry_quarantine(
            quar["quarantine_id"],
            corrected_input={"order_id": "ORD-130", "amount": 80},
        )
        # 回炉回执同时携带初次评估与本次评估
        assert retry["initial_evaluation_id"] == view["evaluation_id"]
        assert retry["evaluation_id"]
        assert retry["batch_id"] == batch_id
        assert retry["original_input"] == {"order_id": "ORD-130", "amount": 130}
        assert retry["corrected_input"] == {"order_id": "ORD-130", "amount": 80}

        # 隔离详情的评估历史完整记录 initial / recheck / retry 各次计算
        detail = svc.get_quarantine_detail(quar["quarantine_id"])
        origins = [item["origin"] for item in detail["evaluation_history"]]
        assert "initial" in origins
        assert "retry" in origins
        initial_item = next(
            item for item in detail["evaluation_history"] if item["origin"] == "initial"
        )
        retry_item = next(
            item for item in detail["evaluation_history"] if item["origin"] == "retry"
        )
        # 原输入参与初次计算，更正输入参与回炉计算
        assert initial_item["raw_input"]["amount"] == 130
        assert retry_item["raw_input"]["amount"] == 80
        assert retry_item["corrected"] is True

        # 批次维度也能查回全部评估会话
        batch_evals = svc.list_evaluations(batch_id=batch_id)
        eval_ids = {e["evaluation_id"] for e in batch_evals}
        assert view["evaluation_id"] in eval_ids
        assert retry["evaluation_id"] in eval_ids


class TestThresholdRejudge:
    def test_threshold_change_rejudge_uses_frozen_snapshots(self, svc):
        """阈值口径变更后重判：通过与隔离明细都用各自冻结的完整结构与规则复算。"""
        batch_id = _old_batch()
        # V1 在途评估并确认
        v1_view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(v1_view["evaluation_id"])
        # 发布 V2（新口径：分，上限 10000）
        svc.activate_schema(TABLE, "SCH-PAY-V2")
        svc.publish_rule_version(TABLE, "RV-PAY-2", "SCH-PAY-V2", _v2_rules())

        # 用 V2 对同一旧批重判
        v2_view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V2", "RV-PAY-2")
        v2_confirmed = svc.confirm_evaluation(v2_view["evaluation_id"])
        # 70 在 V1 已落湖，V2 复算同样通过但台账去重不重复落
        assert v2_confirmed["ledger_duplicated"] == 1
        assert v2_confirmed["ledger_added"] == 0
        # 130 在两套口径下都违规，隔离单复用而非新建
        assert v2_confirmed["isolated_rows"] == 1
        quar = svc.list_quarantine(batch_id=batch_id)[0]
        detail = svc.get_quarantine_detail(quar["quarantine_id"])
        versions_used = {
            item["schema_version_id"] for item in detail["evaluation_history"]
        }
        assert versions_used == {"SCH-PAY-V1", "SCH-PAY-V2"}

        # 台账中有效数据仍然只有两笔中的通过笔（70），且只出现一次
        ledger = svc.list_ledger(TABLE)
        assert ledger["total_valid_rows"] == 1
        assert ledger["items"][0]["row_key"] == "order_id=ORD-70"

    def test_retry_uses_frozen_v1_even_after_v2_rejudge(self, svc):
        """批次被 V2 重判、绑定版本更新后，回炉仍按隔离记录的 V1 结构规则复算。"""
        batch_id = _old_batch()
        v1_view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V1", "RV-PAY-1")
        svc.confirm_evaluation(v1_view["evaluation_id"])
        quar = svc.list_quarantine(batch_id=batch_id)[0]

        # V2 发布并对同一批次重判，批次绑定版本被带到 V2
        svc.activate_schema(TABLE, "SCH-PAY-V2")
        svc.publish_rule_version(TABLE, "RV-PAY-2", "SCH-PAY-V2", _v2_rules())
        v2_view = svc.start_batch_evaluation(batch_id, "SCH-PAY-V2", "RV-PAY-2")
        svc.confirm_evaluation(v2_view["evaluation_id"])
        assert lake_store.get_batch(batch_id).schema_version_id == "SCH-PAY-V2"

        # 回炉仍读取隔离记录固定的 V1：更正为 80 元按 V1 口径通过
        retry = svc.retry_quarantine(
            quar["quarantine_id"],
            corrected_input={"order_id": "ORD-130", "amount": 80},
        )
        assert retry["released"] is True
        assert retry["schema_version_id"] == "SCH-PAY-V1"
        assert retry["rule_version_id"] == "RV-PAY-1"
        # V1 结构没有 amount_cents，转换结果保持 V1 字段
        assert "amount" in retry["converted_input"]
        assert "amount_cents" not in retry["converted_input"]
