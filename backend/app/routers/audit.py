"""变更审计追踪接口：事件账本、版本链、乱序诊断、重放与归档。

列表接口返回的是审计事件流水（一条事件一行），而不是业务行快照；
版本链、生命周期结果清单和历史链是同一份事件账本的三种投影。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit import AuditService
from app.services.audit_ledger import STATUS_ORDER

router = APIRouter(prefix="/api/audit", tags=["变更审计追踪"])

service = AuditService()

LIST_FIELDS = [
    "记录编号",
    "事件序号",
    "版本序号",
    "变更对象",
    "变更类型",
    "变更前值",
    "变更后值",
    "操作人",
    "操作时间",
    "复核状态",
]
STATUSES = STATUS_ORDER


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按对象编号、事件编号、变更对象或操作人检索"),
    status: str | None = Query(default=None, description="已记录、已复核、已归档"),
    object_no: str | None = Query(default=None, description="按业务对象编号过滤，如 AUDI-0001"),
    change_type: str | None = Query(default=None, description="按变更类型过滤，如 参数调整"),
    field: str | None = Query(default=None, description="按变更对象过滤，如 负责人"),
    source_module: str | None = Query(default=None, description="来源模块编号：audit/catalog/retention/lifecycle"),
    archive_period: str | None = Query(default=None, description="归档期，格式 YYYY-MM"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按对象、变更类型、版本键与状态过滤事件流水。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        object_no=object_no,
        change_type=change_type,
        field=field,
        source_module=source_module,
        archive_period=archive_period,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats", response_model=dict)
def stats() -> dict[str, Any]:
    """账本统计：事件总数、版本链数、重放忽略与归并条数。"""
    return service.stats()


@router.get("/chains", response_model=dict)
def list_chains(object_no: str | None = Query(default=None, description="只看某个对象的版本链")) -> dict[str, Any]:
    """按对象 + 变更对象重建全部版本链；乱序事件也按事件序号排好。"""
    chains = service.chains(object_no)
    return {"items": chains, "total": len(chains)}


@router.get("/results", response_model=dict)
def lifecycle_results(
    module: str | None = Query(default=None, description="只看某个来源模块的生命周期结果"),
    object_no: str | None = Query(default=None, description="只看某个业务对象"),
) -> dict[str, Any]:
    """生命周期结果清单：每个对象一行，字段值为版本链最高版本，复核状态只前进。"""
    items = service.lifecycle_results(module=module, object_no=object_no)
    return {"items": items, "total": len(items)}


@router.get("/flow", response_model=dict)
def flow(object_no: str = Query(description="业务对象编号，如 AUDI-0001")) -> dict[str, Any]:
    """调用链视图：业务动作经过校验、审计写入、版本链重建到结果投影。"""
    if not object_no.strip():
        raise HTTPException(status_code=400, detail="业务对象编号不能为空")
    return service.flow(object_no)


@router.post("/replay", response_model=ActionResult)
def replay(payload: EntryPayload) -> ActionResult:
    """按事件重放同一事件：命中原幂等键时不追加副本。

    values 可带 event_id 与可选的 changes；带 changes 时用于验证“同幂等键不同结论”会被拒绝。
    """
    raw_id = payload.values.get("event_id") or payload.values.get("事件主键")
    try:
        event_id = int(raw_id)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="重放需要提供有效的 event_id")
    changes = payload.values.get("changes") or {}
    if not isinstance(changes, dict):
        return ActionResult(ok=False, message="changes 必须是字段对象")
    result = service.replay(event_id, changes)
    event = result.get("event")
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), entry=event)


@router.post("/events", response_model=ActionResult)
def ingest_event(payload: EntryPayload) -> ActionResult:
    """业务模块上报事件的标准入口：幂等键去重、归并窗口合并、版本槽位争抢顺延。"""
    result = service.ingest_event(payload.values)
    event = result.get("event")
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), entry=event)


@router.post("/archive", response_model=ActionResult)
def archive(payload: EntryPayload) -> ActionResult:
    """按月归档事件；归档后读取仍按原序列还原版本链。"""
    period = str(payload.values.get("archive_period") or payload.values.get("归档期") or "").strip()
    result = service.archive(period)
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""))


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出事件流水全量（含归档事件与全部版本链字段）。"""
    items, total = service.list_entries(page=1, size=100000)
    return {"module": "audit", "total": total, "items": items}


@router.get("/{object_no}/history", response_model=dict)
def history(object_no: str) -> dict[str, Any]:
    """对象完整历史：事件流水 + 字段版本链 + 对象事件链 + 生命周期结果。"""
    events = service.history(object_no)
    if not events:
        raise HTTPException(status_code=404, detail=f"业务对象 {object_no} 暂无审计事件")
    return {
        "业务对象编号": object_no,
        "事件流水": events,
        "版本链": service.chains(object_no),
        "对象事件链": service.object_chain(object_no),
        "生命周期结果": service.lifecycle_results(object_no=object_no),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条事件明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计事件 {entry_id} 不存在")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条变更事件，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="变更事件已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条变更事件执行标记复核、归档记录；复核状态只前进不回退。"""
    action = str(payload.values.get("action") or "").strip()
    operator = payload.values.get("operator")
    entry, message = service.run_action(entry_id, action, operator=str(operator) if operator else None)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
