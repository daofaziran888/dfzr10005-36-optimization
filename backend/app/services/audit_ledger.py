"""变更审计事件账本：幂等写入、乱序归并、版本链重建与生命周期结果。

设计要点（细节见 docs/audit-event-flow.md）：

* 版本键 = 业务对象编号 + 变更对象（字段级版本链），例如 AUDI-0001/参数阈值。
* 事件序号由来源模块给出，是版本链的排序主键；乱序到达时只影响写入顺序，不影响链上顺序。
* 归并窗口（默认 120 秒，按落库时间）内，同一版本槽位的重复事件合并为一条，不追加副本。
* 幂等键命中即视为重放：完全一致则直接返回原事件，任何结论字段不一致则拒绝写入。
* 同一版本槽位被后来的不同事件争抢时，后到者顺延为下一版本，绝不覆盖已复核结论。
* 复核状态按对象单调前进：已记录 → 已复核 → 已归档，旧事件晚到也不能把状态倒回。
* 每条事件保留本地序列号、父事件指纹、幂等键摘要、落库批次戳和校验码；
  稳定排序键 = 版本键 + 事件序号 + 本地序列号，同一秒到达的事件也能区分。
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.audit_seed import AUDIT_EVENT_SEED, SOURCE_MODULES

STATUS_ORDER = ["已记录", "已复核", "已归档"]
STATUS_RANK = {status: rank for rank, status in enumerate(STATUS_ORDER)}
DEFAULT_MERGE_WINDOW_SECONDS = 120

# 各模块业务对象编号所在的字段名
OBJECT_NO_FIELD = {
    "audit": "记录编号",
    "catalog": "资产编号",
    "retention": "规则编号",
    "lifecycle": "策略编号",
}

# 字段变更对应的变更类型
CHANGE_TYPE_BY_FIELD = {
    "负责人": "负责人调整",
    "参数阈值": "参数调整",
    "保留期限": "期限调整",
    "告警级别": "告警配置",
    "策略状态": "状态流转",
}


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(values: dict[str, Any]) -> str:
    return json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(values: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()


def normalize_time(value: Any, default: str | None = None) -> str:
    """把日期/时间统一成秒级 ISO 字符串，保证字符串比较等价于时间比较。"""
    text = _norm(value)
    if not text:
        return default or datetime.now().replace(microsecond=0).isoformat()
    text = text.replace("Z", "").replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return default or datetime.now().replace(microsecond=0).isoformat()
    return parsed.replace(microsecond=0).isoformat()


def _status_rank(status: str) -> int:
    return STATUS_RANK.get(_norm(status), 0)


class AuditLedger:
    """线程安全的内存事件账本；真实项目里这一段对应数据库表 + 唯一索引 + 事务。"""

    def __init__(self, *, seed: bool = True, merge_window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS) -> None:
        self._lock = threading.RLock()
        self.merge_window_seconds = merge_window_seconds
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[int, dict[str, Any]] = {}
        self._by_idem: dict[str, int] = {}
        self._chain_ids: dict[str, list[int]] = defaultdict(list)
        self._object_ids: dict[str, list[int]] = defaultdict(list)
        self._archived_ids: set[int] = set()
        self._archived_periods: dict[str, set[int]] = defaultdict(set)
        self._local_seq = 0
        self._event_id = 0
        self._batch_seq = 0
        self._lifecycle_results: list[dict[str, Any]] = []
        if seed:
            for row in AUDIT_EVENT_SEED:
                self.ingest(row, process_id=row.get("写入进程标识"), silent=True)

    # ------------------------------------------------------------------ 写入
    def ingest(
        self,
        payload: dict[str, Any],
        *,
        process_id: Any = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        """接收一条业务变更事件。返回 ok/追加/重放/合并/冲突与事件本身。"""
        values = self._normalize_payload(payload, process_id)
        missing = values.pop("_missing", [])
        if missing:
            return {"ok": False, "追加": False, "message": f"缺少必填字段：{'、'.join(missing)}"}

        with self._lock:
            idem_key = values.pop("_idem_key")
            existing = self._by_idem.get(idem_key)
            if existing is not None:
                original = self._by_id[existing]
                conflict = not self._same_conclusion(original, values)
                if conflict:
                    return {
                        "ok": False,
                        "追加": False,
                        "冲突": True,
                        "message": "同一幂等键携带了不同结论，已拒绝写入也不能覆盖原结论",
                        "event": self._public(original),
                        "差异字段": self._diff(original, values),
                    }
                original["重放次数"] += 1
                self._refresh_all()
                return {
                    "ok": True,
                    "追加": False,
                    "重放": True,
                    "message": "相同事件重放，命中幂等键，未追加副本",
                    "event": self._public(original),
                }

            object_events = self._object_event_dicts(values["业务对象编号"])
            requested_seq = values["事件序号"]
            if requested_seq <= 0:
                # 事件序号按业务对象单调分配，多个字段共用一条对象内事件链
                requested_seq = max((item["事件序号"] for item in object_events), default=0) + 1
            bumped = False
            merged: dict[str, Any] | None = None
            same_slot = next((item for item in object_events if item["事件序号"] == requested_seq), None)
            if same_slot is not None:
                if same_slot["版本键"] == values["版本键"] and self._mergeable(same_slot, values):
                    same_slot["合并计数"] += 1
                    same_slot["重放次数"] += 1
                    merged = same_slot
                else:
                    requested_seq = max((item["事件序号"] for item in object_events), default=0) + 1
                    bumped = True

            if merged is not None:
                self._refresh_all()
                if silent:
                    return {"ok": True, "追加": False, "合并": True, "event": self._public(merged)}
                return {
                    "ok": True,
                    "追加": False,
                    "合并": True,
                    "message": f"归并窗口内命中版本槽位 {merged['事件序号']}，已合并为同一条事件",
                    "event": self._public(merged),
                }

            parent = self._parent_for(values["版本键"], requested_seq)
            arrival_events = self._object_event_dicts(values["业务对象编号"])
            arrival_parent = max(
                (item for item in arrival_events if item["事件序号"] < requested_seq),
                key=lambda item: (item["本地序列号"], item["id"]),
                default=None,
            )
            arrival_before = arrival_parent["变更后值"] if arrival_parent else ""
            arrival_link = arrival_parent is not None and arrival_before == values["变更前值"]
            self._local_seq += 1
            self._event_id += 1
            self._batch_seq += 1
            db_time = values["落库时间"]
            batch_no = values["落库批次号"] or f"BATCH-{db_time[:10].replace('-', '')}-{self._batch_seq:04d}"
            idem_digest = hashlib.sha256(idem_key.encode("utf-8")).hexdigest()
            # 校验码只覆盖事件自身不可变的字段：父指纹是读取时投影，
            # 乱序事件补齐后父链会重排，不能让指纹因此失效。
            checksum_basis = {
                "本地序列号": self._local_seq,
                "版本键": values["版本键"],
                "事件序号": requested_seq,
                "变更前值": values["变更前值"],
                "变更后值": values["变更后值"],
                "操作人": values["操作人"],
                "操作时间": values["操作时间"],
                "复核状态": values["复核状态"],
                "幂等键摘要": idem_digest,
                "落库批次号": batch_no,
            }
            event = {
                "id": self._event_id,
                "事件编号": f"EVT-{self._event_id:06d}",
                "本地序列号": self._local_seq,
                "来源模块编号": values["来源模块编号"],
                "来源模块": SOURCE_MODULES[values["来源模块编号"]]["名称"],
                "入口标记": SOURCE_MODULES[values["来源模块编号"]]["入口标记"],
                "业务对象编号": values["业务对象编号"],
                "业务对象主键": values["业务对象主键"],
                "版本键": values["版本键"],
                "事件序号": requested_seq,
                "父事件序号": parent["事件序号"] if parent else 0,
                "父事件指纹": parent["校验码"] if parent else "ROOT",
                "幂等键": idem_key,
                "幂等键摘要": idem_digest,
                "写入进程标识": values["写入进程标识"],
                "落库批次号": batch_no,
                "落库批次戳": f"{db_time}@{batch_no}",
                "校验码": "CK-" + _digest(checksum_basis)[:32],
                "变更对象": values["变更对象"],
                "变更类型": values["变更类型"],
                "变更前值": values["变更前值"],
                "变更后值": values["变更后值"],
                "操作人": values["操作人"],
                "操作时间": values["操作时间"],
                "复核状态": values["复核状态"],
                "复核人": values["复核人"],
                "落库时间": db_time,
                "归档期": values["操作时间"][:7],
                "到达时前序事件序号": arrival_parent["事件序号"] if arrival_parent else 0,
                "到达时前序值": arrival_before,
                "到达即接续": arrival_link,
                "断点标记": "" if arrival_link or arrival_parent is None else "乱序到达：变更前值未接上到达时最新版本，已按事件序号归位",
                "合并计数": 1,
                "重放次数": 0,
                "冲突后补版本": bumped,
                "补版本原因": "版本槽位已被占用，后到事件顺延为下一版本" if bumped else "",
                "稳定排序键": "",
                "版本序号": 0,
                "摘要": {},
                "已归档": False,
            }
            self._events.append(event)
            self._by_id[event["id"]] = event
            self._by_idem[idem_key] = event["id"]
            self._chain_ids[event["版本键"]].append(event["id"])
            self._object_ids[event["业务对象编号"]].append(event["id"])
            self._refresh_chain(event["版本键"])
            self._refresh_object_state(event["业务对象编号"])
            if silent:
                return {"ok": True, "追加": True, "event": self._public(event)}
            message = "事件已写入审计账本"
            if bumped:
                message = f"版本槽位已被占用，后到事件不覆盖原结论，已顺延为事件序号 {requested_seq}"
            return {"ok": True, "追加": True, "重放": False, "合并": False, "event": self._public(event), "message": message}

    def replay(self, event_id: int, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        """按事件主键重放；changes 用于故意制造“同一幂等键不同结论”的冲突。"""
        with self._lock:
            original = self._by_id.get(int(event_id))
            if original is None:
                return {"ok": False, "追加": False, "message": f"事件 {event_id} 不存在"}
            payload = {
                "来源模块编号": original["来源模块编号"],
                "业务对象编号": original["业务对象编号"],
                "业务对象主键": original["业务对象主键"],
                "变更对象": original["变更对象"],
                "变更类型": original["变更类型"],
                "变更前值": original["变更前值"],
                "变更后值": original["变更后值"],
                "操作人": original["操作人"],
                "操作时间": original["操作时间"],
                "复核状态": original["复核状态"],
                "复核人": original["复核人"],
                "事件序号": original["事件序号"],
                "落库时间": original["落库时间"],
                "写入进程标识": original["写入进程标识"],
                "落库批次号": original["落库批次号"],
                "幂等键": original["幂等键"],
            }
            payload.update(changes or {})
            return self.ingest(payload, process_id=payload.get("写入进程标识"))

    # ------------------------------------------------------------------ 归档
    def archive(self, period: str) -> dict[str, Any]:
        """把某个操作月份的事件移入归档区；读取时仍与在线事件一起重建版本链。"""
        period = _norm(period)
        if not period:
            return {"ok": False, "message": "归档月份不能为空"}
        with self._lock:
            moved = 0
            for event in self._events:
                if event["归档期"] == period and event["id"] not in self._archived_ids:
                    self._archived_ids.add(event["id"])
                    self._archived_periods[period].add(event["id"])
                    moved += 1
            self._refresh_all()
            return {
                "ok": True,
                "message": f"归档期 {period} 已归档 {moved} 条事件，版本链仍可按原序列读取",
                "归档期": period,
                "归档条数": moved,
            }

    def archive_event(self, event_id: int) -> dict[str, Any]:
        """归档单条事件（复核动作“归档记录”用），不影响同月其他对象。"""
        with self._lock:
            event = self._by_id.get(int(event_id))
            if event is None:
                return {"ok": False, "message": f"事件 {event_id} 不存在"}
            if event["id"] in self._archived_ids:
                return {"ok": True, "message": f"事件 {event['事件编号']} 已在归档区"}
            self._archived_ids.add(event["id"])
            self._archived_periods[event["归档期"]].add(event["id"])
            self._refresh_all()
            return {
                "ok": True,
                "message": f"事件 {event['事件编号']} 已归档，版本链仍保留",
                "归档期": event["归档期"],
                "归档条数": 1,
            }

    # ------------------------------------------------------------------ 读取
    def list_events(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        module: str | None = None,
        object_no: str | None = None,
        archive_period: str | None = None,
        include_archived: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [
                event
                for event in self._events
                if include_archived or event["id"] not in self._archived_ids
            ]
            if object_no:
                rows = [event for event in rows if event["业务对象编号"] == object_no]
            if archive_period:
                rows = [event for event in rows if event["归档期"] == archive_period]
            if module:
                rows = [event for event in rows if event["来源模块编号"] == module]
            if status:
                rows = [event for event in rows if event["复核状态"] == status]
            if keyword:
                needle = keyword.strip()
                rows = [
                    event for event in rows
                    if needle in event["业务对象编号"]
                    or needle in event["事件编号"]
                    or needle in event["变更对象"]
                    or needle in event["变更类型"]
                    or needle in event["操作人"]
                    or needle in event["来源模块"]
                ]
            rows = sorted(rows, key=self._ledger_sort_key)
            return [self._public(event) for event in rows], len(rows)

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self._lock:
            event = self._by_id.get(int(event_id))
            return self._public(event) if event else None

    def find_by_idem(self, idem_key: str) -> dict[str, Any] | None:
        with self._lock:
            event_id = self._by_idem.get(_norm(idem_key))
            return self._public(self._by_id[event_id]) if event_id else None

    def chains(self, object_no: str | None = None) -> list[dict[str, Any]]:
        """按对象 + 变更对象重建版本链；归档事件也参与排序。"""
        with self._lock:
            chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for event in self._events:
                if object_no and event["业务对象编号"] != object_no:
                    continue
                chains[event["版本键"]].append(event)
            result: list[dict[str, Any]] = []
            for version_key, events in chains.items():
                ordered = sorted(events, key=self._chain_sort_key)
                source = ordered[0]
                review_status = max((event["复核状态"] for event in ordered), key=_status_rank)
                reviewer = next(
                    (event["复核人"] for event in reversed(ordered) if event["复核状态"] == review_status),
                    "",
                )
                versions = [self._version_view(event) for event in ordered]
                chain_digest = hashlib.sha256(
                    "|".join(event["校验码"] for event in ordered).encode("utf-8")
                ).hexdigest()
                result.append({
                    "业务对象编号": source["业务对象编号"],
                    "业务对象主键": source["业务对象主键"],
                    "变更对象": source["变更对象"],
                    "版本键": version_key,
                    "来源模块": source["来源模块"],
                    "来源模块编号": source["来源模块编号"],
                    "入口标记": source["入口标记"],
                    "版本数": len(ordered),
                    "事件数": sum(1 + event["合并计数"] - 1 + event["重放次数"] for event in ordered),
                    "复核状态": review_status,
                    "复核人": reviewer,
                    "链指纹": "CHAIN-" + chain_digest[:32],
                    "版本链": versions,
                })
            result.sort(key=lambda item: (item["业务对象编号"], item["变更对象"], item["版本键"]))
            return result

    def object_chains(self, object_no: str) -> list[dict[str, Any]]:
        """对象级事件链：把所有字段版本链按事件序号归并，用于还原对象完整历史。"""
        with self._lock:
            events = self._object_event_dicts(object_no)
            if not events:
                return []
            ordered = sorted(events, key=self._ledger_sort_key)
            links: list[dict[str, Any]] = []
            previous: dict[str, Any] | None = None
            for event in ordered:
                digest = hashlib.sha256(
                    f"{event['业务对象编号']}|{event['事件序号']}|{event['校验码']}".encode("utf-8")
                ).hexdigest()
                self._version_view(event)
                # 对象事件链的父节点是上一个对象内事件（跨字段），与字段版本链的父节点不同
                object_parent_no = previous["事件序号"] if previous else 0
                object_parent_fp = previous["校验码"] if previous else "ROOT"
                links.append({
                    "事件编号": event["事件编号"],
                    "事件序号": event["事件序号"],
                    "本地序列号": event["本地序列号"],
                    "变更对象": event["变更对象"],
                    "变更类型": event["变更类型"],
                    "变更前值": event["变更前值"],
                    "变更后值": event["变更后值"],
                    "操作人": event["操作人"],
                    "操作时间": event["操作时间"],
                    "复核状态": event["复核状态"],
                    "父事件序号": object_parent_no,
                    "父事件指纹": object_parent_fp,
                    "稳定排序键": event["稳定排序键"],
                    "校验码": event["校验码"],
                    "链指纹": "NODE-" + digest[:32],
                })
                previous = event
            return links

    def history(self, object_no: str) -> list[dict[str, Any]]:
        """对象完整历史：按对象内事件序号排序，含归档事件。"""
        with self._lock:
            rows = [
                self._version_view(event)
                for event in self._events
                if event["业务对象编号"] == object_no
            ]
            return sorted(rows, key=self._ledger_sort_key)

    def latest_for_object(self, object_no: str) -> dict[str, Any] | None:
        with self._lock:
            events = [self._by_id[event_id] for event_id in self._object_ids.get(object_no, [])]
            if not events:
                return None
            return self._public(max(events, key=lambda item: (item["操作时间"], item["事件序号"], item["本地序列号"])))

    def lifecycle_results(self, object_no: str | None = None) -> list[dict[str, Any]]:
        """当前状态清单：每个对象一行，字段值取版本链最高版本，复核状态只前进不倒退。"""
        with self._lock:
            self._rebuild_lifecycle_results()
            rows = [
                copy.deepcopy(row)
                for row in self._lifecycle_results
                if not object_no or row["业务对象编号"] == object_no
            ]
            return sorted(rows, key=lambda row: row["业务对象编号"])

    def lifecycle_results_by_module(self, module_key: str) -> list[dict[str, Any]]:
        rows = self.lifecycle_results()
        return [row for row in rows if row["来源模块编号"] == module_key]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._rebuild_lifecycle_results()
            total = len(self._events)
            by_module = defaultdict(int)
            by_status = defaultdict(int)
            for event in self._events:
                by_module[event["来源模块"]] += 1
                by_status[event["复核状态"]] += 1
            return {
                "事件总数": total,
                "对象数": len(self._object_ids),
                "版本链数": len(self._chain_ids),
                "归档事件数": len(self._archived_ids),
                "重放忽略次数": sum(event["重放次数"] for event in self._events),
                "合并归并条数": sum(event["合并计数"] - 1 for event in self._events),
                "按来源模块": dict(sorted(by_module.items())),
                "按复核状态": {status: by_status.get(status, 0) for status in STATUS_ORDER},
            }

    # ------------------------------------------------------------------ 内部
    def _normalize_payload(self, payload: dict[str, Any], process_id: Any) -> dict[str, Any]:
        module_key = _norm(
            payload.get("来源模块编号") or payload.get("来源模块键") or payload.get("来源模块")
        )
        field = _norm(payload.get("变更对象"))
        change_type = _norm(payload.get("变更类型")) or CHANGE_TYPE_BY_FIELD.get(field, "字段变更")
        object_no = _norm(payload.get("业务对象编号"))
        required = {
            "来源模块编号": module_key,
            "业务对象编号": object_no,
            "变更对象": field,
            "变更类型": change_type,
            "操作人": _norm(payload.get("操作人")),
            "操作时间": _norm(payload.get("操作时间")),
        }
        missing = [name for name, value in required.items() if not value]
        if module_key and module_key not in SOURCE_MODULES:
            missing.append("来源模块编号(未知模块)")
        before = _norm(payload.get("变更前值"))
        after = _norm(payload.get("变更后值"))
        operation_time = normalize_time(payload.get("操作时间"))
        db_time = normalize_time(payload.get("落库时间"), default=operation_time)
        review_status = _norm(payload.get("复核状态")) or STATUS_ORDER[0]
        if review_status not in STATUS_ORDER:
            missing.append("复核状态")
        operator = _norm(payload.get("操作人"))
        idem_key = _norm(payload.get("幂等键"))
        if not idem_key:
            idem_basis = {
                "来源模块编号": module_key,
                "业务对象编号": object_no,
                "变更对象": field,
                "变更类型": change_type,
                "变更前值": before,
                "变更后值": after,
                "操作人": operator,
                "操作时间": operation_time,
                "事件序号": payload.get("事件序号") or 0,
            }
            idem_key = f"{module_key}-{object_no}-{field}-{_digest(idem_basis)[:12]}"
        try:
            event_seq = int(payload.get("事件序号") or 0)
        except (TypeError, ValueError):
            event_seq = 0
        try:
            object_pk = int(payload.get("业务对象主键") or 0)
        except (TypeError, ValueError):
            object_pk = 0
        return {
            "来源模块编号": module_key,
            "业务对象编号": object_no,
            "业务对象主键": object_pk,
            "变更对象": field,
            "变更类型": change_type,
            "变更前值": before,
            "变更后值": after,
            "操作人": operator,
            "操作时间": operation_time,
            "复核状态": review_status,
            "复核人": _norm(payload.get("复核人")) or operator,
            "事件序号": event_seq,
            "落库时间": db_time,
            "落库批次号": _norm(payload.get("落库批次号")),
            "写入进程标识": _norm(process_id) or _norm(payload.get("写入进程标识")) or "api-unknown",
            "版本键": f"{object_no}|{field}",
            "_idem_key": idem_key,
            "_missing": missing,
        }

    @staticmethod
    def _same_conclusion(original: dict[str, Any], values: dict[str, Any]) -> bool:
        return all(
            _norm(original[field]) == _norm(values[field])
            for field in (
                "业务对象编号",
                "变更对象",
                "变更类型",
                "变更前值",
                "变更后值",
                "操作人",
                "操作时间",
                "复核状态",
            )
        )

    @staticmethod
    def _diff(original: dict[str, Any], values: dict[str, Any]) -> list[str]:
        fields = ["业务对象编号", "变更对象", "变更类型", "变更前值", "变更后值", "操作人", "操作时间", "复核状态"]
        return [field for field in fields if _norm(original[field]) != _norm(values[field])]

    def _mergeable(self, existing: dict[str, Any], values: dict[str, Any]) -> bool:
        """同一版本槽位、同一结论、且落库时间落在归并窗口内 → 合并而不是追加。"""
        if not (
            existing["变更前值"] == values["变更前值"]
            and existing["变更后值"] == values["变更后值"]
            and existing["操作人"] == values["操作人"]
            and existing["操作时间"] == values["操作时间"]
            and existing["复核状态"] == values["复核状态"]
        ):
            return False
        existing_time = datetime.fromisoformat(existing["落库时间"])
        incoming_time = datetime.fromisoformat(values["落库时间"])
        return abs((incoming_time - existing_time).total_seconds()) <= self.merge_window_seconds

    def _chain_event_dicts(self, version_key: str) -> list[dict[str, Any]]:
        return [self._by_id[event_id] for event_id in self._chain_ids.get(version_key, [])]

    def _object_event_dicts(self, object_no: str) -> list[dict[str, Any]]:
        return [self._by_id[event_id] for event_id in self._object_ids.get(object_no, [])]

    def _parent_for(self, version_key: str, event_seq: int) -> dict[str, Any] | None:
        candidates = [event for event in self._chain_event_dicts(version_key) if event["事件序号"] < event_seq]
        return max(candidates, key=self._chain_sort_key) if candidates else None

    @staticmethod
    def _chain_sort_key(event: dict[str, Any]) -> tuple[Any, ...]:
        return (event["事件序号"], event["操作时间"], event["本地序列号"], event["id"])

    @staticmethod
    def _ledger_sort_key(event: dict[str, Any]) -> tuple[Any, ...]:
        """账本稳定顺序：对象 → 对象内事件序号 → 本地序列号 → 主键。

        完全不看操作时间/落库时间，因此同一秒到达、跨月归档或刷新页面都不会改变顺序。
        """
        return (event["业务对象编号"], event["事件序号"], event["本地序列号"], event["id"])

    def _refresh_all(self) -> None:
        for version_key in list(self._chain_ids):
            self._refresh_chain(version_key)
        for object_no in list(self._object_ids):
            self._refresh_object_state(object_no)
        self._rebuild_lifecycle_results()

    def _refresh_chain(self, version_key: str) -> None:
        ordered = sorted(self._chain_event_dicts(version_key), key=self._chain_sort_key)
        chain_fingerprint = hashlib.sha256(
            "|".join(event["校验码"] for event in ordered).encode("utf-8")
        ).hexdigest()
        parent: dict[str, Any] | None = None
        for version, event in enumerate(ordered, start=1):
            event["版本序号"] = version
            event["父事件序号"] = parent["事件序号"] if parent else 0
            event["父事件指纹"] = parent["校验码"] if parent else "ROOT"
            event["稳定排序键"] = (
                f"{event['业务对象编号']}#{event['变更对象']}#"
                f"{event['事件序号']:04d}#{event['本地序列号']:06d}"
            )
            seal_basis = {
                "对象编号": event["业务对象编号"],
                "变更对象": event["变更对象"],
                "版本序号": version,
                "原值": event["变更前值"],
                "新值": event["变更后值"],
                "复核人": event["复核人"],
                "落库时间": event["落库时间"],
                "入口标记": event["入口标记"],
                "来源模块": event["来源模块"],
                "校验码": event["校验码"],
                "合并计数": event["合并计数"],
                "重放次数": event["重放次数"],
                "链指纹": chain_fingerprint,
            }
            seal = "SEAL-" + _digest(seal_basis)[:32]
            event["摘要"] = {
                "对象编号": event["业务对象编号"],
                "版本序号": version,
                "事件序号": event["事件序号"],
                "原值": event["变更前值"],
                "新值": event["变更后值"],
                "复核人": event["复核人"],
                "复核状态": event["复核状态"],
                "落库时间": event["落库时间"],
                "入口标记": event["入口标记"],
                "来源模块": event["来源模块"],
                "合并次数": event["合并计数"],
                "重放次数": event["重放次数"],
                "只读": True,
                "派生自审计": True,
                "只读校验戳": seal,
            }
            event["摘要文本"] = (
                f"{event['业务对象编号']} v{version}（事件序号 {event['事件序号']}）："
                f"{event['变更前值']} → {event['变更后值']}｜复核人 {event['复核人']}"
                f"｜落库 {event['落库时间']}｜{event['入口标记']}"
            )
            parent = event

    def _refresh_object_state(self, object_no: str) -> None:
        """对象状态不单独存储，统一从事件重算，避免业务行和账本各存一份。"""
        if not self._object_ids.get(object_no):
            return
        self._rebuild_lifecycle_results()

    def _rebuild_lifecycle_results(self) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in self._events:
            grouped[event["业务对象编号"]].append(event)
        results: list[dict[str, Any]] = []
        for object_no, events in grouped.items():
            order = sorted(events, key=lambda item: (item["操作时间"], item["事件序号"], item["本地序列号"]))
            fields: list[dict[str, Any]] = []
            field_chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for event in events:
                field_chains[event["变更对象"]].append(event)
            for field, chain in field_chains.items():
                latest = max(chain, key=self._chain_sort_key)
                fields.append({
                    "变更对象": field,
                    "当前值": latest["变更后值"],
                    "原值": latest["变更前值"],
                    "版本序号": latest["版本序号"],
                    "事件序号": latest["事件序号"],
                    "复核状态": latest["复核状态"],
                    "来源事件": latest["事件编号"],
                    "操作时间": latest["操作时间"],
                })
            fields.sort(key=lambda item: item["变更对象"])
            review_status = STATUS_ORDER[0]
            reviewer = ""
            for event in order:
                if _status_rank(event["复核状态"]) >= _status_rank(review_status):
                    review_status = event["复核状态"]
                    reviewer = event["复核人"]
            last_db_time = max(event["落库时间"] for event in events)
            archived_periods = sorted({event["归档期"] for event in events if event["id"] in self._archived_ids})
            source = events[0]
            result_row = {
                "业务对象编号": object_no,
                "业务对象主键": source["业务对象主键"],
                "来源模块编号": source["来源模块编号"],
                "来源模块": source["来源模块"],
                "入口标记": source["入口标记"],
                "当前字段值": {item["变更对象"]: item["当前值"] for item in fields},
                "字段状态": fields,
                "复核状态": review_status,
                "复核人": reviewer,
                "最后操作时间": order[-1]["操作时间"],
                "最后落库时间": last_db_time,
                "版本链数": len(field_chains),
                "事件数": len(events),
                "归档期": archived_periods,
                "当前来源事件": order[-1]["事件编号"],
            }
            seal_basis = {
                "业务对象编号": object_no,
                "当前字段值": result_row["当前字段值"],
                "复核状态": review_status,
                "事件数": len(events),
                "最后落库时间": last_db_time,
            }
            result_row["结果校验戳"] = "RES-" + _digest(seal_basis)[:32]
            results.append(result_row)
        self._lifecycle_results = results

    def _public(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(event)
        payload["已归档"] = event["id"] in self._archived_ids
        payload.setdefault("归档批次", "ARCH-" + event["归档期"] if payload["已归档"] else "")
        return payload

    def _version_view(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(event)
        payload["已归档"] = event["id"] in self._archived_ids
        payload["归档批次"] = "ARCH-" + event["归档期"] if payload["已归档"] else ""
        return payload


audit_ledger = AuditLedger()


def attach_source_event(row: dict[str, Any], module_key: str) -> dict[str, Any]:
    """给业务模块列表行补上来源事件、版本链摘要与派生复核状态。"""
    enriched = dict(row)
    object_no = _norm(row.get(OBJECT_NO_FIELD.get(module_key, "")))
    if not object_no:
        return enriched
    events = audit_ledger.history(object_no)
    if not events:
        enriched.setdefault("来源事件", "—")
        enriched.setdefault("来源事件序号", 0)
        enriched.setdefault("版本链", [])
        return enriched
    latest = max(events, key=lambda item: (item["操作时间"], item["事件序号"], item["本地序列号"]))
    chains = audit_ledger.chains(object_no)
    enriched["来源模块编号"] = latest["来源模块编号"]
    enriched["来源模块"] = latest["来源模块"]
    enriched["入口标记"] = latest["入口标记"]
    enriched["来源事件"] = latest["事件编号"]
    enriched["来源事件序号"] = latest["事件序号"]
    enriched["来源事件版本"] = latest["版本序号"]
    enriched["来源事件摘要"] = latest["摘要文本"]
    enriched["最后落库时间"] = latest["落库时间"]
    enriched["历史事件数"] = len(events)
    enriched["版本链"] = [
        {
            "变更对象": chain["变更对象"],
            "版本数": chain["版本数"],
            "复核状态": chain["复核状态"],
            "版本序号": event["版本序号"],
            "当前值": chain["版本链"][-1]["变更后值"],
            "摘要": chain["版本链"][-1]["摘要文本"],
        }
        for chain in chains
        for event in [chain["版本链"][-1]]
    ]
    result = next(
        (row for row in audit_ledger.lifecycle_results(object_no)),
        None,
    )
    if result:
        enriched["复核状态"] = result["复核状态"]
        enriched["复核人"] = result["复核人"]
        enriched["生命周期结果"] = result
        # 业务行字段跟随版本链最高版本：即使事件是别的模块写进来的，
        # 资产/保留/生命周期页面看到的当前值也和审计结果清单一致。
        for field, current in result["当前字段值"].items():
            if field in enriched:
                enriched[field] = current
        enriched["当前字段值"] = result["当前字段值"]
    return enriched
