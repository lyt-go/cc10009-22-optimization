"""采集质量业务规则：状态流转、字段校验与筛选口径都收在这里。

增量质量重算与导出复核的数据流（跟着数据流查找）：

采集质量回执(ingest) → 作业结果(crawlresult) → 冻结快照(qualitysnapshot)
→ 增量重算（只读受影响作业结果 + 结构化提取版本数据，未变化作业复用缓存）
→ 一份交付对象(qualitydelivery：新的质量结论 + 报告导出结果数据集)
→ 结果摘要写入正文解析侧清单、报告导出结果回写并更新该处记录(contentmanifest)。

关键约束：
- 已确认报告保持不可变，增量重算只能生成新版本（结论版本单调递增）；
- 重复结果和迟到结果不能造成版本倒退（回执幂等 + 写入序号单调）；
- 对报告导出结果的重复操作只生效一次（操作键幂等）；
- 质量重算与导出共用同一冻结快照，导出不能混入快照水位之后写入的数据；
- 缓存复用必须能回到原始URL与提取版本，复用记录不另起分支；
- 冲突留优先级：同一作业出现两个版本依据时，导出结果对应两份记录并优先复核。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from app.services.contentextract import ContentextractService
from app.store import store

MODULE = "qualityreport"
REQUIRED_FIELDS = ["报告编号", "作业编号", "总URL数"]
STATUS_ORDER = ["草稿", "生成中", "已确认", "已归档"]
ACTION_RULES = {"生成报告": "生成中", "确认报告": "已确认", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []
IMMUTABLE_STATUSES = {"已确认", "已归档"}  # 已确认报告保持不可变

RESULT_MODULE = "crawlresult"  # 作业结果
SNAPSHOT_MODULE = "qualitysnapshot"  # 冻结快照
DELIVERY_MODULE = "qualitydelivery"  # 交付对象：质量结论 + 报告导出结果数据集
RECEIPT_MODULE = "qualityreceipt"  # 采集质量回执登记

METRIC_FIELDS = ["总URL数", "成功URL数", "失败URL数", "内容完整率", "字段覆盖率"]

# 重算与回执都走同一把锁：并发回执不能多出第二份结果，并发重算不能写出重复版本。
_recalc_lock = threading.Lock()

_contentextract = ContentextractService()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class QualityreportService:
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
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"质量报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集质量可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"质量报告已{action}"

    # ------------------------------------------------------------------
    # 采集质量回执：写入作业结果，幂等且不允许版本倒退
    # ------------------------------------------------------------------

    def ingest_results(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """受理一批作业结果。

        - 同一回执编号只生效一次：并发/重复回执直接返回首次受理结果，不多出第二份；
        - 同一 URL 只接受写入序号更大的结果：重复结果和迟到结果被忽略，版本不倒退。
        """
        receipt_no = str(values.get("回执编号") or "").strip()
        results = values.get("结果")
        if not receipt_no:
            return None, "缺少回执编号，无法登记采集质量回执"
        if not isinstance(results, list) or not results:
            return None, "回执内没有作业结果"
        with _recalc_lock:
            for row in store.rows(RECEIPT_MODULE):
                if row.get("回执编号") == receipt_no:
                    outcome = copy.deepcopy(row["回执结果"])
                    outcome["重复回执"] = True
                    return outcome, f"回执 {receipt_no} 已受理过，本次不重复写入"
            rows = store.rows(RESULT_MODULE)
            applied, ignored, rejected = 0, 0, []
            for item in results:
                job_no = str((item or {}).get("作业编号") or "").strip()
                url = str((item or {}).get("URL") or "").strip()
                seq = _to_int((item or {}).get("写入序号"))
                if not job_no or not url or seq is None:
                    rejected.append(item)
                    continue
                existing = next(
                    (row for row in rows if row.get("作业编号") == job_no and row.get("URL") == url),
                    None,
                )
                if existing is not None and int(existing.get("写入序号", 0)) >= seq:
                    ignored += 1  # 重复结果或迟到结果：忽略，版本不倒退
                    continue
                record = {
                    "作业编号": job_no,
                    "URL": url,
                    "结果状态": str(item.get("结果状态") or "失败"),
                    "内容完整": str(item.get("内容完整") or "否"),
                    "提取版本": str(item.get("提取版本") or ""),
                    "写入序号": seq,
                    "回执编号": receipt_no,
                    "批次时间": _now(),
                }
                if existing is None:
                    record["id"] = _next_id(RESULT_MODULE)
                    rows.append(record)
                else:
                    existing.update(record)  # 同一 URL 的新序号结果原地覆盖旧结果
                applied += 1
            outcome = {
                "回执编号": receipt_no,
                "接收数": applied,
                "忽略数": ignored,
                "拒绝数": len(rejected),
                "受理时间": _now(),
                "重复回执": False,
            }
            store.rows(RECEIPT_MODULE).append(
                {"id": _next_id(RECEIPT_MODULE), "回执编号": receipt_no, "回执结果": copy.deepcopy(outcome)}
            )
            return outcome, f"回执 {receipt_no} 已受理：接收 {applied} 条，忽略 {ignored} 条迟到/重复结果"

    # ------------------------------------------------------------------
    # 增量重算 + 导出：共用一份冻结快照，产出一份交付对象
    # ------------------------------------------------------------------

    def recalc_and_export(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """对报告做一次增量质量重算并导出，产物是一份交付对象。

        只读取受影响作业的新结果与结构化提取版本数据；未变化作业不重新扫描，
        直接复用上一版本的缓存记录（可回到原始URL与提取版本）。
        """
        report = store.find(MODULE, entry_id)
        if report is None:
            return None, f"质量报告 {entry_id} 不存在或已归档"
        if report.get("status") == "已归档":
            return None, f"质量报告 {report.get('报告编号')} 已归档，不再增量重算"
        op_key = str(values.get("操作键") or "").strip()
        if not op_key:
            return None, "缺少操作键，无法保证报告导出结果只生效一次"
        report_no = str(report.get("报告编号"))
        with _recalc_lock:
            # 幂等：对报告导出结果的重复操作只生效一次
            for row in store.rows(DELIVERY_MODULE):
                if row.get("报告编号") == report_no and row.get("操作键") == op_key:
                    replay = copy.deepcopy(row)
                    replay["重复操作"] = True
                    return replay, f"操作 {op_key} 已生效过，报告导出结果不重复生成"

            jobs = [part.strip() for part in str(report.get("作业编号") or "").split(",") if part.strip()]
            if not jobs:
                return None, f"质量报告 {report_no} 未绑定作业编号，无法圈定重算范围"

            deliveries = self._deliveries_of(report_no)
            last = deliveries[-1] if deliveries else None
            last_version = int(last["结论版本"]) if last else 0
            last_watermark = int(last["快照"]["水位"]) if last else 0
            last_index: dict[str, int] = dict(last["快照"].get("结果索引", {})) if last else {}
            last_records = list(last["导出数据集"]) if last else []

            # 冻结快照：快照冻结之后写入的结果一律不进入本次重算与导出
            watermark = last_watermark
            scoped_rows: list[dict[str, Any]] = []
            for row in store.rows(RESULT_MODULE):
                if row.get("作业编号") not in jobs:
                    continue
                watermark = max(watermark, int(row.get("写入序号", 0)))
                scoped_rows.append(row)
            frozen_rows = copy.deepcopy(scoped_rows)
            result_index = {
                f"{row.get('作业编号')}|{row.get('URL')}": int(row.get("写入序号", 0))
                for row in frozen_rows
            }
            snapshot = {
                "id": _next_id(SNAPSHOT_MODULE),
                "快照编号": f"SNAP-{report_no}-{last_version + 1:04d}",
                "报告编号": report_no,
                "作业范围": jobs,
                "水位": watermark,
                "上一水位": last_watermark,
                "冻结时间": _now(),
                "结果明细": frozen_rows,
                "结果索引": result_index,
                "版本绑定": {row["URL"]: row.get("提取版本") for row in frozen_rows},
            }

            export_records: list[dict[str, Any]] = []
            job_metrics: dict[str, dict[str, Any]] = {}
            reused_jobs: list[str] = []
            recomputed_jobs: list[str] = []
            conflict_jobs: list[str] = []
            explanations: list[str] = []
            delivery_no = f"DELI-{report_no}-{last_version + 1:04d}"

            for job in jobs:
                # 受影响判定：新 URL，或同一 URL 的写入序号被推进（迟到结果若序号不变则不算变更）
                new_rows = [
                    row
                    for row in frozen_rows
                    if row.get("作业编号") == job
                    and last_index.get(f"{job}|{row.get('URL')}") != int(row.get("写入序号", 0))
                ]
                cached_items = [
                    item
                    for record in last_records
                    if record.get("作业编号") == job
                    for item in record.get("来源URLs", [])
                ]
                if not new_rows:
                    # 未变化作业不重新扫描：整份复用上一版本缓存
                    reused_jobs.append(job)
                    items = [dict(item, 指标来源="缓存复用") for item in cached_items]
                    if not items:
                        explanations.append(f"作业 {job} 在快照水位 {watermark} 内没有任何结果，指标按零形成结论")
                else:
                    recomputed_jobs.append(job)
                    changed_urls = {row["URL"] for row in new_rows}
                    kept = [dict(item, 指标来源="缓存复用") for item in cached_items if item.get("URL") not in changed_urls]
                    fresh = [self._item_from_row(row, explanations) for row in new_rows]
                    items = kept + fresh  # 复用未变化字段，只重算受影响的 URL

                bases = sorted({str(item.get("提取版本") or "") for item in items if item.get("提取版本")})
                has_fresh = any(item.get("指标来源") == "本次重算" for item in items)
                has_cache = any(item.get("指标来源") == "缓存复用" for item in items)
                conflict = has_fresh and has_cache and len(bases) > 1
                if conflict:
                    # 冲突边界：新抓取结果只影响部分URL，字段覆盖率缓存仍来自旧提取版本，
                    # 重算范围出现不同版本依据 → 报告导出结果对应两份记录，冲突留优先级。
                    conflict_jobs.append(job)
                    explanations.append(
                        f"作业 {job} 新结果基于 {bases[-1]}，缓存仍来自 {'、'.join(bases[:-1])}，"
                        "重算范围存在不同版本依据，报告导出结果按版本依据拆成两份记录并优先复核"
                    )
                    for basis in bases:
                        basis_items = [item for item in items if str(item.get("提取版本") or "") == basis]
                        export_records.append(
                            self._make_record(delivery_no, len(export_records) + 1, job, basis, basis_items, conflict=True)
                        )
                else:
                    if bases:
                        basis = bases[-1]
                    else:
                        basis = next(
                            (record.get("版本依据") for record in last_records if record.get("作业编号") == job),
                            None,
                        )
                    export_records.append(
                        self._make_record(delivery_no, len(export_records) + 1, job, basis, items, conflict=False)
                    )
                job_metrics[job] = self._aggregate(items)

            totals = self._aggregate(
                [item for record in export_records for item in record.get("来源URLs", [])]
            )
            if not recomputed_jobs:
                explanations.append("本批次无新增结果，指标沿用上一版本缓存，未重新扫描任何作业")
            if totals["总URL数"] == 0:
                explanations.append("总URL数为零：快照内没有可用结果，各指标按零形成可解释结论")

            conclusion = {
                "结论版本": last_version + 1,
                "总URL数": totals["总URL数"],
                "成功URL数": totals["成功URL数"],
                "失败URL数": totals["失败URL数"],
                "内容完整率": totals["内容完整率"],
                "字段覆盖率": totals["字段覆盖率"],
                "作业指标": job_metrics,
                "重算作业": recomputed_jobs,
                "复用作业": reused_jobs,
                "冲突作业": conflict_jobs,
                "解释": explanations,
            }
            delivery = {
                "id": _next_id(DELIVERY_MODULE),
                "交付编号": delivery_no,
                "报告编号": report_no,
                "结论版本": last_version + 1,
                "操作键": op_key,
                "快照": {
                    "快照编号": snapshot["快照编号"],
                    "水位": watermark,
                    "冻结时间": snapshot["冻结时间"],
                    "结果索引": result_index,
                },
                "质量结论": conclusion,
                "导出数据集": export_records,
                "重复操作": False,
                "生成时间": _now(),
            }
            store.rows(SNAPSHOT_MODULE).append(snapshot)
            store.rows(DELIVERY_MODULE).append(delivery)

            # 已确认报告保持不可变：只生成新版本，不改写报告行；未确认报告刷新当前指标。
            if report.get("status") not in IMMUTABLE_STATUSES:
                for field in METRIC_FIELDS:
                    report[field] = str(conclusion[field])
                report["报告生成时间"] = delivery["生成时间"]

            self._sync_manifest(delivery, snapshot)
            return copy.deepcopy(delivery), (
                f"质量报告 {report_no} 已生成第 {last_version + 1} 版结论并导出，"
                f"重算作业 {len(recomputed_jobs)} 个、复用作业 {len(reused_jobs)} 个"
            )

    # ------------------------------------------------------------------
    # 逐项复核：交付对象与导出数据集查询
    # ------------------------------------------------------------------

    def list_deliveries(self, entry_id: int) -> tuple[list[dict[str, Any]], str] | None:
        report = store.find(MODULE, entry_id)
        if report is None:
            return None
        report_no = str(report.get("报告编号"))
        summaries = [
            {
                "交付编号": row["交付编号"],
                "结论版本": row["结论版本"],
                "快照编号": row["快照"]["快照编号"],
                "水位": row["快照"]["水位"],
                "操作键": row["操作键"],
                "记录数": len(row["导出数据集"]),
                "冲突记录数": sum(1 for record in row["导出数据集"] if record.get("冲突")),
                "生成时间": row["生成时间"],
            }
            for row in self._deliveries_of(report_no)
        ]
        return summaries, report_no

    def get_delivery(self, entry_id: int, version: int) -> dict[str, Any] | None:
        report = store.find(MODULE, entry_id)
        if report is None:
            return None
        report_no = str(report.get("报告编号"))
        for row in self._deliveries_of(report_no):
            if int(row["结论版本"]) == version:
                return copy.deepcopy(row)
        return None

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------

    def _deliveries_of(self, report_no: str) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(DELIVERY_MODULE) if row.get("报告编号") == report_no]
        return sorted(rows, key=lambda row: int(row.get("结论版本", 0)))

    def _item_from_row(self, row: dict[str, Any], explanations: list[str]) -> dict[str, Any]:
        """把一条新作业结果转成导出明细项，字段覆盖率取自正文解析侧的冻结版本。"""
        version_no = str(row.get("提取版本") or "")
        coverage = self._coverage_of(version_no)
        if coverage is None:
            explanations.append(
                f"URL {row.get('URL')} 的提取版本 {version_no or '未标注'} 未提供字段覆盖结果，字段覆盖率按 0 计入"
            )
        return {
            "URL": row.get("URL"),
            "写入序号": int(row.get("写入序号", 0)),
            "提取版本": version_no,
            "结果状态": row.get("结果状态"),
            "内容完整": row.get("内容完整"),
            "字段覆盖率": coverage,
            "指标来源": "本次重算",
        }

    @staticmethod
    def _coverage_of(version_no: str) -> float | None:
        """读取正文解析侧可复用的字段覆盖结果；缺失时返回 None 由上层解释。"""
        if not version_no:
            return None
        fields = _contentextract.field_coverage(version_no)
        if not fields:
            return None
        if isinstance(fields, dict):
            return round(sum(1 for ok in fields.values() if ok) / len(fields), 4) if fields else None
        return None

    def _make_record(
        self,
        delivery_no: str,
        seq: int,
        job: str,
        basis: str | None,
        items: list[dict[str, Any]],
        *,
        conflict: bool,
    ) -> dict[str, Any]:
        metrics = self._aggregate(items)
        sources = {item.get("指标来源") for item in items}
        if not items:
            origin = "无结果"
        elif sources == {"本次重算"}:
            origin = "本次重算"
        elif sources == {"缓存复用"}:
            origin = "缓存复用"
        else:
            origin = "增量合并"
        return {
            "记录编号": f"{delivery_no}-R{seq:02d}",
            "作业编号": job,
            "版本依据": basis,
            "指标来源": origin,
            "冲突": conflict,
            "优先级": "高" if conflict else "普通",  # 冲突留优先级
            **metrics,
            "来源URLs": copy.deepcopy(items),  # 缓存复用可回到原始URL与提取版本
        }

    @staticmethod
    def _aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(items)
        success = sum(1 for item in items if item.get("结果状态") == "成功")
        complete = sum(1 for item in items if item.get("内容完整") == "是")
        coverage_values = [item.get("字段覆盖率") for item in items]
        coverage_known = [float(value) for value in coverage_values if value is not None]
        missing = len(coverage_values) - len(coverage_known)
        # 字段缺失按 0 计入，保证指标可解释而不是静默跳过
        coverage = round(sum(coverage_known) / total, 4) if total else 0
        return {
            "总URL数": total,
            "成功URL数": success,
            "失败URL数": total - success,
            "内容完整率": round(complete / total, 4) if total else 0,
            "字段覆盖率": coverage,
            "字段覆盖缺失数": missing,
        }

    def _sync_manifest(self, delivery: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """结果摘要写入正文解析侧清单，并把报告导出结果回写、更新该处记录。"""
        conclusion = delivery["质量结论"]
        versions = sorted(
            {
                str(item.get("提取版本"))
                for record in delivery["导出数据集"]
                for item in record.get("来源URLs", [])
                if item.get("提取版本")
            }
            | {str(v) for v in snapshot.get("版本绑定", {}).values() if v}
        )
        summary = {
            "报告编号": delivery["报告编号"],
            "结论版本": conclusion["结论版本"],
            "快照编号": snapshot["快照编号"],
            "水位": snapshot["水位"],
            "总URL数": conclusion["总URL数"],
            "内容完整率": conclusion["内容完整率"],
            "字段覆盖率": conclusion["字段覆盖率"],
            "生成时间": delivery["生成时间"],
        }
        export_info = {
            "交付编号": delivery["交付编号"],
            "操作键": delivery["操作键"],
            "记录数": len(delivery["导出数据集"]),
            "冲突记录数": sum(1 for record in delivery["导出数据集"] if record.get("冲突")),
            "导出时间": delivery["生成时间"],
        }
        for version_no in versions:
            _contentextract.write_result_summary(version_no, summary)
            _contentextract.writeback_export_result(version_no, export_info)
