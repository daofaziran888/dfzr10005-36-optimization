"""数据保留策略业务规则：期限调整和状态流转都经过审计事件账本。"""
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

MODULE = "retention"
REQUIRED_FIELDS = ["规则编号", "数据集范围", "保留期限"]
BUSINESS_STATUS_ORDER = ["草稿", "生效中", "已到期", "已废止"]
REVIEW_FIELD = "复核状态"
DURATION_FIELD = "保留期限"
ACTION_RULES = {
    "启用规则": {"status": "生效中", "review": "已记录", "field": "规则状态"},
    "废止规则": {"status": "已废止", "review": "已复核", "field": "复核状态"},
    "归档规则": {"status": "已到期", "review": "已归档", "field": "复核状态"},
}
NEGATIVE_ACTIONS = ["废止规则"]


class RetentionService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
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
        object_no = str(row.get("规则编号") or "")
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
            "过期动作": values.get("过期动作") or "",
            "例外清单": values.get("例外清单") or "",
            "合规依据": values.get("合规依据") or "",
            "执行周期": values.get("执行周期") or "",
        })
        entry["status"] = "草稿"
        entry["复核状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        record_field_change(
            module_key=MODULE,
            entry=entry,
            object_no=str(entry["规则编号"]),
            field=DURATION_FIELD,
            after=entry["保留期限"],
            operator=str(values.get("操作人") or "值班管理员"),
            change_type="期限登记",
        )
        return attach_source_event(entry, MODULE), []

    def update_duration(self, entry_id: int, new_duration: Any, *, operator: str) -> tuple[dict[str, Any] | None, str]:
        """调整保留期限：前值取业务行旧值，落账后按版本链回写当前值。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"保留规则 {entry_id} 不存在或已归档"
        object_no = str(entry.get("规则编号") or "")
        result = record_field_change(
            module_key=MODULE,
            entry=entry,
            object_no=object_no,
            field=DURATION_FIELD,
            after=new_duration,
            operator=operator,
            change_type="期限调整",
        )
        if not result.get("ok"):
            return attach_source_event(entry, MODULE), result.get("message") or "审计写入失败"
        sync_business_row(entry, object_no)
        event = result["event"]
        return attach_source_event(entry, MODULE), f"保留期限已调整为 {new_duration}（事件序号 {event['事件序号']}）"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"保留规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据保留策略可执行范围"
        rule = ACTION_RULES[action]
        object_no = str(entry.get("规则编号") or "")
        current_status = str(entry.get("status") or "")
        if current_status in BUSINESS_STATUS_ORDER and BUSINESS_STATUS_ORDER.index(current_status) >= BUSINESS_STATUS_ORDER.index(rule["status"]):
            return attach_source_event(entry, MODULE), (
                f"保留规则当前状态已是「{current_status}」，动作「{action}」不会把状态倒回"
            )
        applied = apply_status_action(
            entry=entry,
            module_key=MODULE,
            object_no=object_no,
            field=rule["field"],
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
