"""表生命周期管理接口：策略维护、状态流转与生命周期结果清单。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit_ledger import audit_ledger
from app.services.lifecycle import LifecycleService

router = APIRouter(prefix="/api/lifecycle", tags=["表生命周期管理"])

service = LifecycleService()

LIST_FIELDS = ["策略编号", "表名称", "创建日期", "保留天数", "归档路径", "清理方式", "最后操作", "策略状态"]
STATUSES = ["草稿", "生效中", "已到期", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已到期、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤生命周期策略；每行带来源事件与生命周期结果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/results", response_model=dict)
def lifecycle_results() -> dict[str, Any]:
    """生命周期结果清单：每个对象一行，当前状态取版本链最高版本。"""
    items = audit_ledger.lifecycle_results()
    return {"items": items, "total": len(items)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条生命周期策略明细，附带事件流水与版本链。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"生命周期策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条生命周期策略；登记本身也会产生一条审计事件。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="生命周期策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条生命周期策略执行启用策略、归档表、删除表。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/history", response_model=dict)
def history(entry_id: int) -> dict[str, Any]:
    """策略完整历史：事件流水 + 版本链 + 生命周期结果。"""
    row = service.get_entry(entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"生命周期策略 {entry_id} 不存在或已归档")
    object_no = str(row.get("策略编号") or "")
    return {
        "业务对象编号": object_no,
        "事件流水": audit_ledger.history(object_no),
        "版本链": audit_ledger.chains(object_no),
        "生命周期结果": audit_ledger.lifecycle_results(object_no=object_no),
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出生命周期策略清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lifecycle", "total": total, "items": items}
