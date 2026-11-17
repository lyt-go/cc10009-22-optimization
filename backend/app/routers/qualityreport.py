"""采集质量接口：维护质量报告，覆盖生成报告、确认报告、归档报告等动作。

增量质量重算与导出复核：
- POST /ingest                采集质量回执：写入作业结果，回执编号幂等
- POST /{entry_id}/recalc     增量重算并导出：冻结快照 → 新质量结论 + 报告导出结果数据集
- GET  /{entry_id}/deliveries 交付对象列表（逐项复核入口）
- GET  /{entry_id}/deliveries/{version} 单个交付对象明细

注意：静态路径（/export、/ingest）必须声明在 /{entry_id} 之前，否则会被参数路由抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.qualityreport import QualityreportService

router = APIRouter(prefix="/api/qualityreport", tags=["采集质量"])

service = QualityreportService()

LIST_FIELDS = ["报告编号", "作业编号", "总URL数", "成功URL数", "失败URL数", "内容完整率", "字段覆盖率", "报告生成时间"]
STATUSES = ["草稿", "生成中", "已确认", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、生成中、已确认、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号与状态过滤采集质量列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集质量清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "qualityreport", "total": total, "items": items}


@router.post("/ingest", response_model=ActionResult)
def ingest_results(payload: EntryPayload) -> ActionResult:
    """采集质量回执：把一批作业结果写入结果池。

    同一回执编号只生效一次（并发回执不多出第二份结果）；同一 URL 只接受
    写入序号更大的结果，重复与迟到结果不会造成版本倒退。
    """
    outcome, message = service.ingest_results(payload.values)
    if outcome is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=outcome)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量报告 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条质量报告，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="质量报告已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条质量报告执行生成报告、确认报告、归档报告；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/recalc", response_model=ActionResult)
def recalc_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """增量质量重算并导出：只读取受影响作业结果与结构化提取版本数据。

    未变化作业不重新扫描、复用缓存字段；产物是一份交付对象（新的质量结论 +
    报告导出结果数据集），同一操作键重复提交只生效一次。
    """
    delivery, message = service.recalc_and_export(entry_id, payload.values)
    if delivery is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=delivery)


@router.get("/{entry_id}/deliveries")
def list_deliveries(entry_id: int) -> dict[str, Any]:
    """列出报告的全部交付对象摘要，供逐项复核。"""
    result = service.list_deliveries(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"质量报告 {entry_id} 不存在或已归档")
    items, report_no = result
    return {"报告编号": report_no, "total": len(items), "items": items}


@router.get("/{entry_id}/deliveries/{version}")
def get_delivery(entry_id: int, version: int) -> dict[str, Any]:
    """读取某一版交付对象：质量结论 + 报告导出结果数据集（含来源URL与提取版本）。"""
    delivery = service.get_delivery(entry_id, version)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"质量报告 {entry_id} 的第 {version} 版交付对象不存在")
    return delivery
