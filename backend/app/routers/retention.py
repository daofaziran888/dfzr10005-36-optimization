"""数据保留策略接口：保留规则维护、期限调整、启用/废止/归档与版本链查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit_ledger import audit_ledger
from app.services.retention import RetentionService

router = APIRouter(prefix="/api/retention", tags=["数据保留策略"])

service = RetentionService()

LIST_FIELDS = ["规则编号", "数据集范围", "保留期限", "过期动作", "例外清单", "合规依据", "执行周期", "规则状态"]
STATUSES = ["草稿", "生效中", "已到期", "已废止"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已到期、已废止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤保留规则；每行都带来源事件与版本链摘要。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条保留规则明细，附带事件流水与版本链。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"保留规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条保留规则；登记本身也会产生一条审计事件。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="保留规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条保留规则执行启用规则、废止规则、归档规则。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/duration", response_model=ActionResult)
def update_duration(entry_id: int, payload: EntryPayload) -> ActionResult:
    """调整保留期限；前值取自当前业务行，后值落审计后按版本链回写。"""
    raw = payload.values.get("保留期限") or payload.values.get("duration")
    if raw is None or not str(raw).strip():
        return ActionResult(ok=False, message="缺少必填字段：保留期限")
    operator = str(payload.values.get("操作人") or "值班管理员")
    entry, message = service.update_duration(entry_id, raw, operator=operator)
    if entry is None:
        return ActionResult(ok=False, message=message)
    ok = "已调整" in message
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/{entry_id}/history", response_model=dict)
def history(entry_id: int) -> dict[str, Any]:
    """保留规则的完整历史：事件流水 + 版本链 + 生命周期结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"保留规则 {entry_id} 不存在或已归档")
    object_no = str(entry.get("规则编号") or "")
    return {
        "业务对象编号": object_no,
        "事件流水": audit_ledger.history(object_no),
        "版本链": audit_ledger.chains(object_no),
        "生命周期结果": audit_ledger.lifecycle_results(object_no=object_no),
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出保留规则清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "retention", "total": total, "items": items}
