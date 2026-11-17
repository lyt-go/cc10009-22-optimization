"""内容提取业务规则：状态流转、字段校验与筛选口径都收在这里。

正文解析侧同时向采集质量提供两类可复用数据：
- 结构化提取版本数据（extractversion）：按提取版本冻结的字段覆盖结果，质量重算与
  报告导出读取同一份，保证字段覆盖率与内容完整率来自同一版本依据；
- 正文解析侧清单（contentmanifest）：质量侧把结果摘要与报告导出结果回写到这里，
  供下游逐项核对。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "contentextract"
REQUIRED_FIELDS = ["提取器编号", "内容类型", "正文选择器"]
STATUS_ORDER = ["草稿", "提取器评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交提取器评审": "提取器评审中", "发布提取器": "已发布", "废弃提取器": "已废弃"}
NEGATIVE_ACTIONS = []

VERSION_MODULE = "extractversion"  # 结构化提取版本数据
MANIFEST_MODULE = "contentmanifest"  # 正文解析侧清单


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class ContentextractService:
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
            rows = [row for row in rows if keyword in str(row.get("提取器编号", ""))]
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
            return None, f"提取器 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于内容提取可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"提取器已{action}"

    # ---- 正文解析侧：结构化提取版本数据 ----

    def extraction_versions(self) -> list[dict[str, Any]]:
        """列出全部结构化提取版本数据，供质量侧与下游核对。"""
        return store.rows(VERSION_MODULE)

    def version_info(self, version_no: str) -> dict[str, Any] | None:
        for row in store.rows(VERSION_MODULE):
            if row.get("提取版本号") == version_no:
                return row
        return None

    def field_coverage(self, version_no: str) -> dict[str, Any] | None:
        """可复用的字段覆盖结果：按提取版本冻结，重算与导出读同一份，不另起分支。"""
        row = self.version_info(version_no)
        if row is None:
            return None
        return row.get("字段覆盖结果")

    # ---- 正文解析侧清单：结果摘要与报告导出结果的落点 ----

    def manifest_list(self) -> list[dict[str, Any]]:
        return store.rows(MANIFEST_MODULE)

    def _manifest_row(self, version_no: str) -> dict[str, Any]:
        rows = store.rows(MANIFEST_MODULE)
        for row in rows:
            if row.get("提取版本号") == version_no:
                return row
        row = {
            "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
            "清单编号": f"MANI-{version_no}",
            "提取版本号": version_no,
            "结果摘要": None,
            "报告导出结果": None,
            "更新时间": None,
        }
        rows.append(row)
        return row

    def write_result_summary(self, version_no: str, summary: dict[str, Any]) -> dict[str, Any]:
        """结果摘要写入正文解析侧清单，供下游核对报告导出结果。"""
        row = self._manifest_row(version_no)
        row["结果摘要"] = dict(summary)
        row["更新时间"] = _now()
        return row

    def writeback_export_result(self, version_no: str, export_info: dict[str, Any]) -> dict[str, Any]:
        """报告导出结果回写到正文解析侧清单，并更新该处记录（原地更新，不重复追加）。"""
        row = self._manifest_row(version_no)
        row["报告导出结果"] = dict(export_info)
        row["更新时间"] = _now()
        return row
