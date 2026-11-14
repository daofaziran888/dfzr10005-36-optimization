"""表生命周期管理业务规则：策略状态流转写审计，可作为其他模块的生命周期结果入口。"""
from __future__ import annotations

from typing import Any

from app.services.audit_hooks import apply_status_action, record_change, sync_business_row
from app.services.audit_ledger import (
    STATUS_ORDER,
    attach_source_event,
    audit_ledger,
    normalize_time,
)
from app.store import store

MODULE = "lifecycle"
REQUIRED_FIELDS = ["策略编号", "表名称", "创建日期"]
BUSINESS_STATUS_ORDER = ["草稿", "生效中", "已到期", "已归档"]
REVIEW_FIELD = "复核状态"
STATUS_FIELD = "策略状态"
ACTION_RULES = {
    "启用策略": {"status": "生效中", "review": "已记录"},
    "归档表": {"status": "已到期", "review": "已复核"},
    "删除表": {"status": "已归档", "review": "已归档"},
}
NEGATIVE_ACTIONS = ["删除表"]


class LifecycleService:
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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
        object_no = str(row.get("策略编号") or "")
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
            "保留天数": values.get("保留天数") or "",
            "归档路径": values.get("归档路径") or "",
            "清理方式": values.get("清理方式") or "",
        })
        entry["status"] = "草稿"
        entry["复核状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        record_change(
            module_key=MODULE,
            object_no=str(entry["策略编号"]),
            object_pk=int(entry["id"]),
            field=STATUS_FIELD,
            before="",
            after=str(entry["status"]),
            operator=str(values.get("操作人") or "值班管理员"),
            change_type="策略登记",
        )
        return attach_source_event(entry, MODULE), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"生命周期策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表生命周期管理可执行范围"
        rule = ACTION_RULES[action]
        object_no = str(entry.get("策略编号") or "")
        current_status = str(entry.get("status") or "")
        if current_status in BUSINESS_STATUS_ORDER and BUSINESS_STATUS_ORDER.index(current_status) >= BUSINESS_STATUS_ORDER.index(rule["status"]):
            return attach_source_event(entry, MODULE), (
                f"生命周期策略当前状态已是「{current_status}」，动作「{action}」不会把状态倒回"
            )
        applied = apply_status_action(
            entry=entry,
            module_key=MODULE,
            object_no=object_no,
            field=STATUS_FIELD,
            before_value=current_status,
            after_value=rule["status"],
            target_review_status=rule["review"],
            action=action,
            actor="值班管理员",
            status_order=BUSINESS_STATUS_ORDER,
        )
        if not applied["ok"]:
            return attach_source_event(entry, MODULE), applied["message"]
        sync_business_row(entry, object_no)
        return attach_source_event(entry, MODULE), applied["message"]
