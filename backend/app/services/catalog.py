"""数据资产盘点业务规则：动作流转同时写入审计事件账本。"""
from __future__ import annotations

from typing import Any

from app.services.audit_hooks import (
    apply_status_action,
    record_field_change,
    sync_business_row,
)
from app.services.audit_ledger import (
    STATUS_ORDER,
    attach_source_event,
    audit_ledger,
    normalize_time,
)
from app.store import store

MODULE = "catalog"
REQUIRED_FIELDS = ["资产编号", "表名称", "资产分类"]
# 业务状态只前进：草稿 → 已盘点 → 已复核 → 已归档
BUSINESS_STATUS_ORDER = ["草稿", "已盘点", "已复核", "已归档"]
REVIEW_ORDER = STATUS_ORDER
REVIEW_FIELD = "复核状态"
# 动作 → 业务行目标状态 + 审计复核状态 + 写入的字段
ACTION_RULES = {
    "提交复核": {"status": "已盘点", "review": "已记录", "field": "盘点状态"},
    "归档资产": {"status": "已复核", "review": "已复核", "field": "复核状态"},
    "废弃资产": {"status": "已归档", "review": "已归档", "field": "复核状态"},
}
NEGATIVE_ACTIONS = ["废弃资产"]


class CatalogService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("资产编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        enriched = [attach_source_event(row, MODULE) for row in rows]
        total = len(enriched)
        start = max(page - 1, 0) * size
        return enriched[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        enriched = attach_source_event(row, MODULE)
        object_no = str(row.get("资产编号") or "")
        enriched["事件版本链"] = audit_ledger.chains(object_no)
        enriched["事件流水"] = audit_ledger.history(object_no)
        enriched["生命周期结果"] = audit_ledger.lifecycle_results(object_no)
        return enriched

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({
            "业务域": values.get("业务域") or "",
            "负责人": values.get("负责人") or "",
            "数据等级": values.get("数据等级") or "",
            "更新频率": values.get("更新频率") or "",
            "盘点时间": normalize_time(values.get("盘点时间")),
        })
        entry["status"] = "草稿"
        entry["复核状态"] = REVIEW_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        record_field_change(
            module_key=MODULE,
            entry=entry,
            object_no=str(entry["资产编号"]),
            field="负责人",
            after=entry["负责人"],
            operator=str(values.get("操作人") or "值班管理员"),
            change_type="资产登记",
        )
        return attach_source_event(entry, MODULE), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"资产记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据资产盘点可执行范围"
        rule = ACTION_RULES[action]
        object_no = str(entry.get("资产编号") or "")
        current_status = str(entry.get("status") or "")
        if current_status in BUSINESS_STATUS_ORDER and BUSINESS_STATUS_ORDER.index(current_status) >= BUSINESS_STATUS_ORDER.index(rule["status"]):
            return attach_source_event(entry, MODULE), (
                f"资产记录当前状态已是「{current_status}」，动作「{action}」不会把状态倒回"
            )
        actor = str(entry.get("负责人") or "值班管理员")
        applied = apply_status_action(
            entry=entry,
            module_key=MODULE,
            object_no=object_no,
            field=rule["field"],
            before_value=current_status,
            after_value=rule["status"],
            target_review_status=rule["review"],
            action=action,
            actor=actor,
            status_order=BUSINESS_STATUS_ORDER,
        )
        if not applied["ok"]:
            return attach_source_event(entry, MODULE), applied["message"]
        sync_business_row(entry, object_no)
        return attach_source_event(entry, MODULE), applied["message"]
