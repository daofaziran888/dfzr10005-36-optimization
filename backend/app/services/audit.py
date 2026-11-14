"""变更审计追踪业务规则：列表与动作都由审计事件账本派生。

审计模块本身不再单独维护一份“当前行”，因为同一个对象的重复事件一旦分表，
乱序到达就会让变更前值接不上。所有入口都写同一本账，列表、版本链和生命周期
结果都从同一事件链投影出来。
"""
from __future__ import annotations

from typing import Any

from app.services.audit_hooks import call_chain
from app.services.audit_ledger import STATUS_ORDER, audit_ledger, normalize_time

MODULE = "audit"
REQUIRED_FIELDS = ["记录编号", "变更对象", "变更类型"]
ACTION_RULES = {"标记复核": "已复核", "归档记录": "已归档"}
REVIEW_FIELD = "复核状态"


def _public_row(event: dict[str, Any]) -> dict[str, Any]:
    """把一条事件投影成变更记录行，保留事件序号链的全部字段。"""
    return {
        "id": event["id"],
        "status": event["复核状态"],
        "pending": event["复核状态"] != STATUS_ORDER[-1],
        "abnormal": bool(event["冲突后补版本"]),
        "记录编号": event["业务对象编号"],
        "变更对象": event["变更对象"],
        "变更类型": event["变更类型"],
        "变更前值": event["变更前值"],
        "变更后值": event["变更后值"],
        "操作人": event["操作人"],
        "操作时间": event["操作时间"],
        "复核状态": event["复核状态"],
        "复核人": event["复核人"],
        "事件编号": event["事件编号"],
        "事件序号": event["事件序号"],
        "版本序号": event["版本序号"],
        "父事件序号": event["父事件序号"],
        "父事件指纹": event["父事件指纹"],
        "稳定排序键": event["稳定排序键"],
        "本地序列号": event["本地序列号"],
        "来源模块编号": event["来源模块编号"],
        "来源模块": event["来源模块"],
        "入口标记": event["入口标记"],
        "业务对象主键": event["业务对象主键"],
        "版本键": event["版本键"],
        "幂等键": event["幂等键"],
        "幂等键摘要": event["幂等键摘要"],
        "写入进程标识": event["写入进程标识"],
        "落库批次号": event["落库批次号"],
        "落库批次戳": event["落库批次戳"],
        "校验码": event["校验码"],
        "落库时间": event["落库时间"],
        "归档期": event["归档期"],
        "已归档": event["已归档"],
        "归档批次": event["归档批次"],
        "合并计数": event["合并计数"],
        "重放次数": event["重放次数"],
        "冲突后补版本": event["冲突后补版本"],
        "补版本原因": event["补版本原因"],
        "到达时前序事件序号": event["到达时前序事件序号"],
        "到达时前序值": event["到达时前序值"],
        "到达即接续": event["到达即接续"],
        "断点标记": event["断点标记"],
        "摘要": event["摘要"],
        "摘要文本": event["摘要文本"],
    }


class AuditService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        object_no: str | None = None,
        change_type: str | None = None,
        field: str | None = None,
        source_module: str | None = None,
        archive_period: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        events, total = audit_ledger.list_events(
            keyword=keyword,
            status=status,
            module=source_module,
            archive_period=archive_period,
        )
        if object_no:
            events = [event for event in events if object_no in event["业务对象编号"]]
        if change_type:
            events = [event for event in events if change_type in event["变更类型"]]
        if field:
            events = [event for event in events if field in event["变更对象"]]
        total = len(events)
        start = max(page - 1, 0) * size
        return [_public_row(event) for event in events[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        event = audit_ledger.get_event(entry_id)
        if event is None:
            return None
        row = _public_row(event)
        object_no = event["业务对象编号"]
        row["事件版本链"] = audit_ledger.chains(object_no)
        row["对象事件链"] = audit_ledger.object_chains(object_no)
        row["生命周期结果"] = audit_ledger.lifecycle_results(object_no=object_no)
        return row

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        object_no = str(values.get("记录编号")).strip()
        object_pk = int(values.get("业务对象主键") or self._next_object_pk())
        result = audit_ledger.ingest(
            {
                "来源模块编号": "audit",
                "业务对象编号": object_no,
                "业务对象主键": object_pk,
                "变更对象": values.get("变更对象"),
                "变更类型": values.get("变更类型"),
                "变更前值": values.get("变更前值"),
                "变更后值": values.get("变更后值"),
                "操作人": values.get("操作人") or "值班管理员",
                "操作时间": normalize_time(values.get("操作时间")),
                "复核状态": values.get("复核状态") or STATUS_ORDER[0],
                "复核人": values.get("复核人"),
                "事件序号": values.get("事件序号") or 0,
                "落库批次号": values.get("落库批次号"),
            },
            process_id=values.get("写入进程标识") or "audit-create",
        )
        if not result.get("ok"):
            return None, [result.get("message") or "审计写入失败"]
        return _public_row(result["event"]), []

    def run_action(self, entry_id: int, action: str, *, operator: str | None = None) -> tuple[dict[str, Any] | None, str]:
        event = audit_ledger.get_event(entry_id)
        if event is None:
            return None, f"变更记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于变更审计追踪可执行范围"
        target = ACTION_RULES[action]
        object_no = event["业务对象编号"]
        current = next((row for row in audit_ledger.lifecycle_results(object_no)), None)
        before = current["复核状态"] if current else STATUS_ORDER[0]
        if STATUS_ORDER.index(before) >= STATUS_ORDER.index(target):
            latest_event = audit_ledger.latest_for_object(object_no)
            message = f"对象 {object_no} 当前复核状态已是「{before}」，后到动作不会把状态倒回"
            return (_public_row(latest_event) if latest_event else None), message
        result = audit_ledger.ingest(
            {
                "来源模块编号": "audit",
                "业务对象编号": object_no,
                "业务对象主键": event["业务对象主键"],
                "变更对象": REVIEW_FIELD,
                "变更类型": "复核",
                "变更前值": before,
                "变更后值": target,
                "操作人": operator or event["操作人"],
                "操作时间": normalize_time(None),
                "复核状态": target,
                "复核人": operator or event["复核人"],
            },
            process_id="audit-review",
        )
        if not result.get("ok"):
            return None, result.get("message") or "审计写入失败"
        review_event = result["event"]
        if target == STATUS_ORDER[-1]:
            # 归档只针对本次复核事件，避免把同月其他对象的一并锁走
            audit_ledger.archive_event(review_event["id"])
        return _public_row(audit_ledger.get_event(review_event["id"])), f"变更记录已{action}"

    def chains(self, object_no: str | None = None) -> list[dict[str, Any]]:
        return audit_ledger.chains(object_no)

    def history(self, object_no: str) -> list[dict[str, Any]]:
        return audit_ledger.history(object_no)

    def object_chain(self, object_no: str) -> list[dict[str, Any]]:
        return audit_ledger.object_chains(object_no)

    def lifecycle_results(self, module: str | None = None, object_no: str | None = None) -> list[dict[str, Any]]:
        rows = audit_ledger.lifecycle_results(object_no)
        if module:
            rows = [row for row in rows if row["来源模块编号"] == module]
        return rows

    def flow(self, object_no: str) -> dict[str, Any]:
        return call_chain(MODULE, object_no)

    def stats(self) -> dict[str, Any]:
        return audit_ledger.stats()

    def replay(self, event_id: int, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        return audit_ledger.replay(event_id, changes)

    def ingest_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return audit_ledger.ingest(payload, process_id=payload.get("写入进程标识"))

    def archive(self, period: str) -> dict[str, Any]:
        return audit_ledger.archive(period)

    @staticmethod
    def _next_object_pk() -> int:
        rows = audit_ledger.lifecycle_results()
        return max((int(row["业务对象主键"]) for row in rows), default=0) + 1
