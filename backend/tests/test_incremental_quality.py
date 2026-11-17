"""增量质量重算与导出复核的行为测试。

直接驱动服务层（不依赖 HTTP），覆盖：
回执幂等、迟到/重复结果不倒退、增量重算、未变化作业缓存复用、
版本依据冲突两份记录、已确认报告不可变、导出操作幂等、冻结快照隔离、
零指标/缺失字段可解释、正文解析侧清单回写、缓存复用可追溯。

运行：.venv/bin/python tests/test_incremental_quality.py
"""
from __future__ import annotations

import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.seed import SEED_ROWS  # noqa: E402
from app.services.contentextract import ContentextractService  # noqa: E402
from app.services.qualityreport import QualityreportService  # noqa: E402
from app.store import store  # noqa: E402

quality = QualityreportService()
content = ContentextractService()

PASSED: list[str] = []


def reset() -> None:
    store._tables = {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"[FAIL] {name} {detail}")
    PASSED.append(name)
    print(f"[ok] {name}")


def ingest(receipt: str, results: list[dict]) -> dict:
    outcome, message = quality.ingest_results({"回执编号": receipt, "结果": results})
    assert outcome is not None, message
    return outcome


def recalc(entry_id: int, op_key: str) -> dict:
    delivery, message = quality.recalc_and_export(entry_id, {"操作键": op_key})
    assert delivery is not None, message
    return delivery


# ---------------------------------------------------------------- 回执幂等
reset()
before = len(store.rows("crawlresult"))
first = ingest("RCPT-T1", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/e", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0002", "写入序号": 5},
])
second = ingest("RCPT-T1", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/e", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0002", "写入序号": 5},
])
check("回执首次受理写入结果", first["接收数"] == 1 and len(store.rows("crawlresult")) == before + 1)
check("并发/重复回执不多出第二份结果", second["重复回执"] is True and len(store.rows("crawlresult")) == before + 1)

# ------------------------------------------------------- 迟到/重复结果不倒退
reset()
late = ingest("RCPT-T2", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/a", "结果状态": "失败",
     "内容完整": "否", "提取版本": "EXT-0002", "写入序号": 1},
])
row_a = next(r for r in store.rows("crawlresult") if r["URL"] == "https://example.com/news/a")
check("迟到结果被忽略", late["忽略数"] == 1 and late["接收数"] == 0)
check("迟到结果不覆盖已有数据", row_a["结果状态"] == "成功" and row_a["提取版本"] == "EXT-0001")
newer = ingest("RCPT-T3", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/a", "结果状态": "失败",
     "内容完整": "否", "提取版本": "EXT-0002", "写入序号": 9},
])
row_a = next(r for r in store.rows("crawlresult") if r["URL"] == "https://example.com/news/a")
check("更大写入序号原地更新", newer["接收数"] == 1 and row_a["写入序号"] == 9 and row_a["提取版本"] == "EXT-0002")

# ------------------------------------------------------------- 增量重算基本流
reset()
d1 = recalc(1, "OP-Q1-V1")  # QUAL-0001 草稿，作业 CRAW-0003（4 条种子结果）
check("首版结论版本为 1", d1["结论版本"] == 1)
check("总URL数来自冻结快照", d1["质量结论"]["总URL数"] == 4)
check("成功/失败统计正确", d1["质量结论"]["成功URL数"] == 3 and d1["质量结论"]["失败URL数"] == 1)
check("内容完整率正确", d1["质量结论"]["内容完整率"] == 0.5)
check("字段覆盖率取自提取版本 EXT-0001", d1["质量结论"]["字段覆盖率"] == 0.8)
check("草稿报告行指标被刷新", store.find("qualityreport", 1)["总URL数"] == "4")
check("导出数据集与结论同一份交付对象", d1["交付编号"] and len(d1["导出数据集"]) == 1)
check("快照已冻结且水位正确", d1["快照"]["水位"] == 4 and len(store.rows("qualitysnapshot")) == 1)

# ------------------------------------------------- 未变化作业不重新扫描（缓存复用）
reset()
recalc(1, "OP-Q1-V1")
ingest("RCPT-T4", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/e", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0001", "写入序号": 5},
])
d2 = recalc(1, "OP-Q1-V2")
check("结论版本单调递增", d2["结论版本"] == 2)
check("受影响作业被重算", d2["质量结论"]["重算作业"] == ["CRAW-0003"])
record2 = d2["导出数据集"][0]
reused = [i for i in record2["来源URLs"] if i["指标来源"] == "缓存复用"]
fresh = [i for i in record2["来源URLs"] if i["指标来源"] == "本次重算"]
check("未变化 URL 复用缓存", len(reused) == 4 and len(fresh) == 1)
check("缓存复用可回到原始URL与提取版本", all(i["提取版本"] == "EXT-0001" and i["URL"] for i in reused))
check("复用记录不另起分支", record2["指标来源"] == "增量合并" and len(d2["导出数据集"]) == 1)

# 多作业报告：有新增的作业重算，无新增的作业整份复用
reset()
entry, missing = quality.create_entry({"报告编号": "QUAL-T1", "作业编号": "CRAW-0003,CRAW-0001", "总URL数": "0"})
assert not missing
recalc(entry["id"], "OP-T1-V1")  # 两个作业都进入缓存
ingest("RCPT-T5", [
    {"作业编号": "CRAW-0001", "URL": "https://sample.com/list/z", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0001", "写入序号": 3},
])
d2 = recalc(entry["id"], "OP-T1-V2")
check("多作业报告只重算受影响作业", d2["质量结论"]["重算作业"] == ["CRAW-0001"])
check("未变化作业不重新扫描", d2["质量结论"]["复用作业"] == ["CRAW-0003"])
rec_c3 = next(r for r in d2["导出数据集"] if r["作业编号"] == "CRAW-0003")
check("未变化作业整份复用缓存", rec_c3["指标来源"] == "缓存复用" and rec_c3["总URL数"] == 4)
check("整份复用仍可回到原始URL与提取版本",
      all(i["提取版本"] == "EXT-0001" and i["URL"] for i in rec_c3["来源URLs"]))

# ------------------------------------------- 版本依据冲突：导出结果对应两份记录
reset()
recalc(1, "OP-Q1-V1")  # 全部 EXT-0001 进入缓存
ingest("RCPT-T6", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/e", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0002", "写入序号": 5},
])
d2 = recalc(1, "OP-Q1-V2")
conflict_records = [r for r in d2["导出数据集"] if r["作业编号"] == "CRAW-0003"]
check("冲突边界下导出结果对应两份记录", len(conflict_records) == 2)
bases = sorted(r["版本依据"] for r in conflict_records)
check("两份记录各依一个提取版本", bases == ["EXT-0001", "EXT-0002"])
check("冲突留优先级", all(r["冲突"] and r["优先级"] == "高" for r in conflict_records))
new_basis = next(r for r in conflict_records if r["版本依据"] == "EXT-0002")
old_basis = next(r for r in conflict_records if r["版本依据"] == "EXT-0001")
check("新依据记录来自本次重算", new_basis["指标来源"] == "本次重算" and new_basis["总URL数"] == 1)
check("旧依据记录来自缓存复用", old_basis["指标来源"] == "缓存复用" and old_basis["总URL数"] == 4)
check("冲突被写进可解释结论", any("版本依据" in e for e in d2["质量结论"]["解释"]))
check("汇总指标仍覆盖全部 URL", d2["质量结论"]["总URL数"] == 5)

# ------------------------------------------------------- 已确认报告保持不可变
reset()
confirmed_before = dict(store.find("qualityreport", 3))
d1 = recalc(3, "OP-Q3-V1")
confirmed_after = store.find("qualityreport", 3)
check("已确认报告可生成新版本", d1["结论版本"] == 1)
check("已确认报告行不被改写", confirmed_after == confirmed_before)
check("已确认报告状态不变", confirmed_after["status"] == "已确认")

# ------------------------------------------------------- 导出重复操作只生效一次
reset()
d1 = recalc(1, "OP-Q1-SAME")
d1_again = recalc(1, "OP-Q1-SAME")
check("重复操作返回同一份交付对象", d1_again["交付编号"] == d1["交付编号"] and d1_again["重复操作"] is True)
check("重复操作不产生新版本", len(store.rows("qualitydelivery")) == 1 and len(store.rows("qualitysnapshot")) == 1)

# ------------------------------------------------------- 冻结快照隔离后续写入
reset()
d1 = recalc(1, "OP-Q1-V1")
ingest("RCPT-T7", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/f", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0002", "写入序号": 6},
])
snapshot = store.rows("qualitysnapshot")[0]
check("快照水位之后写入不进快照", snapshot["水位"] == 4 and len(snapshot["结果明细"]) == 4)
urls_v1 = {i["URL"] for r in d1["导出数据集"] for i in r["来源URLs"]}
check("已导出数据集不混入后续写入", "https://example.com/news/f" not in urls_v1)
d2 = recalc(1, "OP-Q1-V2")
urls_v2 = {i["URL"] for r in d2["导出数据集"] for i in r["来源URLs"]}
check("下一版本才看到新结果", "https://example.com/news/f" in urls_v2 and d2["快照"]["水位"] == 6)

# ------------------------------------------- 指标为零或字段缺失时仍形成可解释结论
reset()
ingest("RCPT-T8", [
    {"作业编号": "CRAW-0003", "URL": "https://example.com/news/g", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0003", "写入序号": 5},
])
d1 = recalc(1, "OP-Q1-V1")
check("缺失字段覆盖被解释", any("未提供字段覆盖结果" in e for e in d1["质量结论"]["解释"]))
item_g = next(i for r in d1["导出数据集"] for i in r["来源URLs"] if i["URL"].endswith("/g"))
check("缺失字段覆盖按 0 计入且可追溯", item_g["字段覆盖率"] is None and item_g["提取版本"] == "EXT-0003")
check("汇总字段覆盖率仍给出数值", d1["质量结论"]["字段覆盖率"] == round(0.8 * 4 / 5, 4))
d2 = recalc(2, "OP-Q2-V1")  # CRAW-0002 没有任何结果
check("零指标作业形成可解释结论", any("指标按零" in e for e in d2["质量结论"]["解释"]))
rec_empty = next(r for r in d2["导出数据集"] if r["作业编号"] == "CRAW-0002")
check("零指标记录可复核", rec_empty["总URL数"] == 0 and rec_empty["指标来源"] == "无结果")

# ------------------------------------------------- 正文解析侧清单：摘要写入与回写
reset()
d1 = recalc(1, "OP-Q1-V1")
mani = {r["提取版本号"]: r for r in content.manifest_list()}
check("结果摘要写入正文解析侧清单", mani["EXT-0001"]["结果摘要"]["结论版本"] == 1)
check("报告导出结果回写清单", mani["EXT-0001"]["报告导出结果"]["交付编号"] == d1["交付编号"])
check("清单记录有更新时间", bool(mani["EXT-0001"]["更新时间"]))
d2 = recalc(1, "OP-Q1-V2")
mani_after = {r["提取版本号"]: r for r in content.manifest_list()}
check("回写更新该处记录而非追加", len([r for r in content.manifest_list() if r["提取版本号"] == "EXT-0001"]) == 1)
check("清单记录随新版本更新", mani_after["EXT-0001"]["结果摘要"]["结论版本"] == 2)
check("清单可供下游核对导出结果", mani_after["EXT-0001"]["报告导出结果"]["交付编号"] == d2["交付编号"])

# ------------------------------------------------------------- 逐项复核接口
reset()
recalc(1, "OP-Q1-V1")
recalc(1, "OP-Q1-V2")
summaries, report_no = quality.list_deliveries(1)
check("交付对象列表可逐项复核", report_no == "QUAL-0001" and [s["结论版本"] for s in summaries] == [1, 2])
full = quality.get_delivery(1, 2)
check("单版交付对象含结论与数据集", full["质量结论"]["结论版本"] == 2 and full["导出数据集"])
check("版本绑定关系可追溯", all(i["提取版本"] for r in full["导出数据集"] for i in r["来源URLs"]))

# ------------------------------------------------------------- 并发幂等
import threading

reset()
results_pool = [
    {"作业编号": "CRAW-0003", "URL": f"https://example.com/news/t{i}", "结果状态": "成功",
     "内容完整": "是", "提取版本": "EXT-0002", "写入序号": 10 + i}
    for i in range(4)
]
before = len(store.rows("crawlresult"))
threads = [threading.Thread(target=ingest, args=("RCPT-CONCURRENT", results_pool)) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发回执只生效一次", len(store.rows("crawlresult")) == before + 4)
receipts = [r for r in store.rows("qualityreceipt") if r["回执编号"] == "RCPT-CONCURRENT"]
check("并发回执只登记一份结果", len(receipts) == 1 and receipts[0]["回执结果"]["接收数"] == 4)

reset()
recalc(1, "OP-CONCURRENT-1")
deliveries_before = len(store.rows("qualitydelivery"))
threads = [threading.Thread(target=recalc, args=(1, "OP-CONCURRENT-2")) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发重算同一操作键只生成一份交付对象", len(store.rows("qualitydelivery")) == deliveries_before + 1)
versions = [d["结论版本"] for d in store.rows("qualitydelivery")]
check("并发下结论版本不倒退不重复", versions == sorted(versions) and len(set(versions)) == len(versions))

print(f"\n全部 {len(PASSED)} 项检查通过")