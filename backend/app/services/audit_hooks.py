"""业务动作 → 审计事件的调用链钩子。

业务模块的 run_action 只做三件事：
1. 校验动作与前置状态；
2. 改业务行；
3. 通过 record_change 把变更写入审计账本。

审计账本负责幂等、归并、版本链和复核状态单调前进；业务行只保留当前状态，
历史链统一从账本读取，避免“归档后只看得到最后状态”。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.audit_ledger import (
    CHANGE_TYPE_BY_FIELD,
    STATUS_ORDER,
    audit_ledger,
    normalize_time,
)


def now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def record_change(
    *,
    module_key: str,
    object_no: str,
    object_pk: int,
    field: str,
    before: Any,
    after: Any,
    operator: str,
    review_status: str = STATUS_ORDER[0],
    reviewer: str | None = None,
    change_type: str | None = None,
    process_id: str | None = None,
) -> dict[str, Any]:
    """把一次业务变更写进审计账本；返回账本的写入结论。"""
    return audit_ledger.ingest(
        {
            "来源模块编号": module_key,
            "业务对象编号": object_no,
            "业务对象主键": object_pk,
            "变更对象": field,
            "变更类型": change_type or CHANGE_TYPE_BY_FIELD.get(field, "字段变更"),
            "变更前值": before,
            "变更后值": after,
            "操作人": operator,
            "操作时间": now_text(),
            "复核状态": review_status,
            "复核人": reviewer or operator,
        },
        process_id=process_id or f"{module_key}-action",
    )


def record_field_change(
    *,
    module_key: str,
    entry: dict[str, Any],
    object_no: str,
    field: str,
    after: Any,
    operator: str,
    change_type: str | None = None,
) -> dict[str, Any]:
    """字段更新：前值取业务行旧值，落审计后再回写业务行。"""
    before = entry.get(field, "")
    result = record_change(
        module_key=module_key,
        object_no=object_no,
        object_pk=int(entry.get("id") or 0),
        field=field,
        before=before,
        after=after,
        operator=operator,
        change_type=change_type,
    )
    if result.get("ok"):
        entry[field] = after
    return result


def apply_status_action(
    *,
    entry: dict[str, Any],
    module_key: str,
    object_no: str,
    field: str,
    before_value: str,
    after_value: str,
    target_review_status: str,
    action: str,
    actor: str,
    status_order: list[str] | None = None,
) -> dict[str, Any]:
    """状态流转统一入口：先写账本、再回写业务行。

    账本写入被拒绝（幂等冲突）时业务行不变，避免“审计没落、业务先改”的断链。
    业务状态与复核状态都只允许沿各自的状态序列前进，后到的旧事件不能倒回。
    """
    result = record_change(
        module_key=module_key,
        object_no=object_no,
        object_pk=int(entry.get("id") or 0),
        field=field,
        before=before_value,
        after=after_value,
        operator=actor,
        review_status=target_review_status,
        change_type="状态流转",
        process_id=f"{module_key}-action",
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "entry": entry,
            "message": result.get("message") or "审计写入失败",
            "written": result,
        }
    derived = next((row for row in audit_ledger.lifecycle_results(object_no)), None)
    if derived is not None:
        entry["复核状态"] = derived["复核状态"]
        entry["复核人"] = derived["复核人"]
        entry["最后落库时间"] = derived["最后落库时间"]
    entry["status"] = _advance(entry.get("status"), after_value, status_order)
    entry["pending"] = entry["status"] != (status_order or STATUS_ORDER)[-1]
    entry["abnormal"] = action.startswith("删除") or action.startswith("废弃")
    entry["最后操作"] = normalize_time(now_text())
    event = result["event"]
    return {
        "ok": True,
        "entry": entry,
        "message": f"已{action}",
        "written": result,
        "来源事件": event["事件编号"],
        "事件序号": event["事件序号"],
    }


def _advance(current: Any, target: str, status_order: list[str] | None = None) -> str:
    """业务状态只前进不回退；不在序列里的状态原样返回。"""
    order = status_order or STATUS_ORDER
    text = str(current or "")
    if text not in order or target not in order:
        return text or target
    return max(text, target, key=order.index)


def sync_business_row(entry: dict[str, Any], object_no: str) -> None:
    """把账本投影出的复核状态与最新落库时间同步回业务行。"""
    derived = next((row for row in audit_ledger.lifecycle_results(object_no)), None)
    if derived is None:
        return
    entry["复核状态"] = derived["复核状态"]
    entry["复核人"] = derived["复核人"]
    entry["最后落库时间"] = derived["最后落库时间"]
    entry["字段版本"] = derived["字段状态"]


def call_chain(module_key: str, object_no: str) -> dict[str, Any]:
    """给前端调试面板用的调用链视图：业务动作 → 校验 → 账本写入 → 版本链。"""
    events = audit_ledger.history(object_no)
    return {
        "来源模块编号": module_key,
        "业务对象编号": object_no,
        "环节": [
            {"序号": 1, "环节": "业务动作", "说明": f"{module_key} 模块发起变更", "输入": "动作 + 业务对象"},
            {"序号": 2, "环节": "业务校验", "说明": "检查必填字段和状态前置条件", "输入": "当前业务行"},
            {"序号": 3, "环节": "审计写入", "说明": "幂等键去重 → 归并窗口 → 版本槽位分配", "输入": "变更前后值"},
            {"序号": 4, "环节": "版本链重建", "说明": "按事件序号排序，父链与稳定排序键落定", "输入": "事件集合"},
            {"序号": 5, "环节": "结果投影", "说明": "当前状态写生命周期结果清单，历史链保留", "输入": "版本链"},
        ],
        "事件数": len(events),
        "版本链": audit_ledger.chains(object_no),
        "对象事件链": audit_ledger.object_chains(object_no),
    }
