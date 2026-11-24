"""Deterministically generate the local synthetic retail dataset.

All identifiers and values are fictitious.  The generator is seeded and
asserts its own row counts, so the bundled CSVs can be reproduced exactly.

Planted scenarios
-----------------
===============  ==========================================================
Scenario          How it appears in the data
===============  ==========================================================
LOT-006           Real exposure-adjusted excess of quality returns,
                  repeated confirmed critical inspection findings, and a
                  clean post-remediation period -> escalate/investigate.
LOT-002           Apparent excess, but its major/critical findings are
                  still pending or disputed -> evidence blocking, no
                  escalation on unverified findings.
LOT-008           Partial traceability plus a pending critical finding ->
                  watch, cannot be fully traced.
LOT-004           Highest share of unresolved reason codes -> worst-case
                  risk must be reported, not assumed zero.
LOT-000           Placeholder lot id with no upstream reference -> batch
                  action impossible; cannot hold a real batch.
LOT-003 (SKU-003) Information ("商品说明") returns around a copy version
                  change -> product copy action, not quality escalation.
2026-11-15        Reason catalogue switch: legacy labels before, R-2x
                  codes after; aliases bridge the two schemes.
===============  ==========================================================

Run from the repository root::

    python scripts/generate_data.py
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED = 20261124
CUTOFF = date(2026, 11, 25)
CATALOG_SWITCH = date(2026, 11, 15)

CHANNELS = ["web", "marketplace", "store"]
POLICIES = ["POL-0", "POL-1", "POL-2", "POL-3"]

PRODUCTS = [
    ("SKU-000", "占位商品（编码缺失）", "unknown", "2026-09-01", "v1"),
    ("SKU-001", "轻量羽绒夹克", "outerwear", "2026-09-05", "v1"),
    ("SKU-002", "通勤双肩包", "bags", "2026-09-06", "v1"),
    ("SKU-003", "直筒休闲裤", "bottoms", "2026-09-07", "v2"),
    ("SKU-004", "羊毛混纺围巾", "accessories", "2026-09-08", "v1"),
    ("SKU-005", "针织开衫", "knitwear", "2026-09-09", "v1"),
    ("SKU-006", "防泼水徒步鞋", "footwear", "2026-09-10", "v1"),
    ("SKU-007", "纯棉印花T恤", "tops", "2026-09-11", "v2"),
    ("SKU-008", "亚麻衬衫", "tops", "2026-09-12", "v1"),
    ("SKU-009", "软壳长裤", "bottoms", "2026-09-13", "v1"),
    ("SKU-010", "保温随行杯", "home", "2026-09-14", "v1"),
    ("SKU-011", "儿童雨衣", "outerwear", "2026-09-15", "v1"),
    ("SKU-012", "折叠收纳箱", "home", "2026-09-16", "v1"),
    ("SKU-013", "羊毛袜（三双装）", "accessories", "2026-09-17", "v1"),
    ("SKU-014", "绗缝马甲", "outerwear", "2026-09-18", "v1"),
    ("SKU-015", "棉质睡袋", "home", "2026-09-19", "v1"),
    ("SKU-016", "帆布托特包", "bags", "2026-09-20", "v1"),
    ("SKU-017", "运动腰包", "accessories", "2026-09-21", "v1"),
    ("SKU-018", "防晒渔夫帽", "accessories", "2026-09-22", "v1"),
    ("SKU-019", "速干短袖", "tops", "2026-09-23", "v1"),
]

LOT_META = {
    "LOT-000": dict(supplier="", ref="", produced="2026-01-01", unshipped=0, state="unknown",
                    note="批次号占位：上游系统未回传实际批次，相关订单无法追溯到具体生产批次"),
    "LOT-001": dict(supplier="SUP-01", ref="SL-2609-A", produced="2026-09-12", unshipped=6, state="complete", note=""),
    "LOT-002": dict(supplier="SUP-02", ref="SL-2609-B", produced="2026-09-18", unshipped=18, state="complete", note=""),
    "LOT-003": dict(supplier="SUP-03", ref="SL-2609-C", produced="2026-09-22", unshipped=12, state="complete", note=""),
    "LOT-004": dict(supplier="SUP-04", ref="", produced="2026-09-26", unshipped=0, state="partial",
                    note="供应商批次号缺失，仅有生产周"),
    "LOT-005": dict(supplier="SUP-05", ref="SL-2610-A", produced="2026-10-01", unshipped=8, state="complete", note=""),
    "LOT-006": dict(supplier="SUP-01", ref="SL-2610-B", produced="2026-10-03", unshipped=24, state="complete", note=""),
    "LOT-007": dict(supplier="SUP-02", ref="SL-2610-C", produced="2026-10-06", unshipped=10, state="complete", note=""),
    "LOT-008": dict(supplier="SUP-03", ref="", produced="2026-10-09", unshipped=9, state="partial",
                    note="生产批次号与入库记录不一致"),
    "LOT-009": dict(supplier="SUP-04", ref="SL-2610-D", produced="2026-10-12", unshipped=0, state="complete", note=""),
    "LOT-010": dict(supplier="SUP-05", ref="SL-2610-E", produced="2026-10-15", unshipped=14, state="complete", note=""),
    "LOT-011": dict(supplier="SUP-01", ref="SL-2610-F", produced="2026-10-18", unshipped=11, state="complete", note=""),
    "LOT-012": dict(supplier="SUP-02", ref="", produced="2026-10-21", unshipped=7, state="partial",
                    note="代工厂换线，批次映射待补"),
    "LOT-013": dict(supplier="SUP-03", ref="SL-2610-G", produced="2026-10-24", unshipped=5, state="complete", note=""),
    "LOT-014": dict(supplier="SUP-04", ref="SL-2610-H", produced="2026-10-27", unshipped=13, state="complete", note=""),
}

LOT_SKUS = {
    "LOT-000": ["SKU-000"],
    "LOT-001": ["SKU-001", "SKU-012"],
    "LOT-002": ["SKU-002"],
    "LOT-003": ["SKU-003", "SKU-013"],
    "LOT-004": ["SKU-004", "SKU-014"],
    "LOT-005": ["SKU-005", "SKU-015"],
    "LOT-006": ["SKU-006", "SKU-016"],
    "LOT-007": ["SKU-007", "SKU-017"],
    "LOT-008": ["SKU-008", "SKU-018"],
    "LOT-009": ["SKU-009", "SKU-019"],
    "LOT-010": ["SKU-010"],
    "LOT-011": ["SKU-006"],
    "LOT-012": ["SKU-002"],
    "LOT-013": ["SKU-003"],
    "LOT-014": ["SKU-011"],
}

LOT_ORDERS = {
    "LOT-006": 16, "LOT-002": 14, "LOT-008": 9, "LOT-004": 9, "LOT-000": 9,
    "LOT-003": 8, "LOT-013": 8, "LOT-012": 6, "LOT-007": 6, "LOT-010": 6,
    "LOT-011": 6, "LOT-001": 6, "LOT-005": 6, "LOT-009": 6, "LOT-014": 5,
}

# How strongly each lot leans to POL-2 (highest return-policy window).
LOT_POLICY_BIAS = {
    "LOT-006": 3, "LOT-002": 3, "LOT-008": 2, "LOT-004": 1, "LOT-003": 1, "LOT-013": 1,
}

QUALITY, INFORMATION, FIT, UNKNOWN = "quality", "information", "fit", "unknown"
LEGACY = {QUALITY: "damaged", INFORMATION: "description", FIT: "fit", UNKNOWN: "other"}
NEW = {QUALITY: "R-21", INFORMATION: "R-22", FIT: "R-23", UNKNOWN: "R-24"}

# (lot, number of returns, dominant bucket, lag range)
LOT_RETURNS = {
    "LOT-006": (14, QUALITY, (8, 24)),
    "LOT-002": (10, QUALITY, (9, 26)),
    "LOT-008": (5, QUALITY, (10, 28)),
    "LOT-004": (5, "mixed", (11, 30)),
    "LOT-000": (4, "mixed", (12, 30)),
    "LOT-003": (4, INFORMATION, (6, 22)),
    "LOT-013": (4, INFORMATION, (7, 24)),
    "LOT-007": (3, INFORMATION, (8, 25)),
    "LOT-010": (3, UNKNOWN, (10, 28)),
    "LOT-012": (4, "mixed", (9, 27)),
    "LOT-011": (4, QUALITY, (10, 26)),
    "LOT-001": (1, FIT, (8, 24)),
    "LOT-005": (1, FIT, (9, 25)),
    "LOT-009": (2, FIT, (10, 27)),
    "LOT-014": (1, INFORMATION, (8, 26)),
}

BASE_SALE = date(2026, 9, 28)


def _stratum_for(lot: str, index: int) -> tuple[str, str]:
    bias = LOT_POLICY_BIAS.get(lot, 0)
    channel = CHANNELS[index % 3]
    if bias:
        policy = "POL-2" if index % (4 - min(bias, 3)) == 0 else POLICIES[index % 4]
    else:
        policy = POLICIES[(index + (1 if lot > "LOT-007" else 0)) % 4]
    return channel, policy


def build_orders() -> list[dict]:
    rows: list[dict] = []
    counter = 0
    for lot, count in LOT_ORDERS.items():
        skus = LOT_SKUS[lot]
        for index in range(count):
            counter += 1
            sku = skus[index % len(skus)]
            channel, policy = _stratum_for(lot, index)
            if lot == "LOT-000":
                channel, policy = "web", "POL-0"
            sold = BASE_SALE + timedelta(days=(index * 7 + int(lot[-2:]) * 3) % 30)
            if index == count - 1:
                sold = date(2026, 11, 2) + timedelta(days=int(lot[-2:]) % 6)
            units = 1 + (index * 3 + int(lot[-2:])) % 4
            rows.append(dict(order_id=f"ORD-{counter:04d}", sku=sku, lot_id=lot, channel=channel,
                             sold_at=sold.isoformat(), units=units, policy_version=policy))
    return rows


def build_returns(orders: list[dict]) -> list[dict]:
    by_lot: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        by_lot[order["lot_id"]].append(order)
    rng = random.Random(SEED)
    returns: list[dict] = []
    counter = 0
    for lot, (count, bucket, (lag_lo, lag_hi)) in LOT_RETURNS.items():
        pool = sorted(by_lot[lot], key=lambda o: (o["sold_at"], o["order_id"]))
        rng.shuffle(pool)
        for n in range(count):
            order = pool[n]
            counter += 1
            if bucket == "mixed":
                bucket_for_row = [UNKNOWN, INFORMATION, QUALITY, UNKNOWN, FIT][n % 5]
            else:
                bucket_for_row = bucket if n < count - 1 else (INFORMATION if bucket == QUALITY else FIT)
            lag = lag_lo + (n * 3 + counters(lot)) % max(1, lag_hi - lag_lo)
            received = date.fromisoformat(order["sold_at"]) + timedelta(days=lag)
            if received > CUTOFF:
                received = CUTOFF - timedelta(days=n % 4)
            code = NEW[bucket_for_row] if received >= CATALOG_SWITCH else LEGACY[bucket_for_row]
            if counter % 7 == 0:
                refund, review = "pending", "review"
            elif counter % 5 == 0:
                refund, review = "rejected", "revised"
            else:
                refund, review = "paid", "coded"
            returns.append(dict(return_id=f"RET-{counter:04d}", order_id=order["order_id"],
                                returned_units=1, reason_code=code, received_at=received.isoformat(),
                                refund_state=refund, review_state=review))
    # Deliberate data-quality examples: two over-order rows.
    for return_id in ("RET-0003", "RET-0010"):
        row = next(r for r in returns if r["return_id"] == return_id)
        row["returned_units"] = 3
        row["refund_state"] = "paid"
        row["review_state"] = "coded"
    assert len(returns) == 65, len(returns)
    return returns


def counters(lot: str) -> int:
    return int(lot[-2:]) % 5


INSPECTION_PLAN = {
    "LOT-006": [("finish", "critical", "confirmed"), ("seam", "critical", "confirmed"),
                ("finish", "major", "confirmed"), ("label", "minor", "pending")],
    "LOT-011": [("finish", "critical", "confirmed"), ("seam", "major", "confirmed")],
    "LOT-002": [("label", "critical", "pending"), ("finish", "critical", "disputed"),
                ("seam", "major", "pending")],
    "LOT-012": [("packaging", "minor", "confirmed"), ("label", "major", "pending")],
    "LOT-008": [("seam", "critical", "pending"), ("finish", "major", "confirmed"),
                ("finish", "major", "confirmed")],
    "LOT-004": [("seam", "major", "disputed"), ("packaging", "minor", "confirmed")],
    "LOT-000": [("label", "minor", "confirmed"), ("label", "minor", "confirmed"),
                ("finish", "major", "pending")],
    "LOT-003": [("finish", "minor", "confirmed"), ("packaging", "major", "pending")],
    "LOT-013": [("finish", "minor", "confirmed"), ("seam", "minor", "confirmed")],
    "LOT-001": [("seam", "minor", "confirmed"), ("label", "minor", "confirmed")],
    "LOT-005": [("finish", "minor", "confirmed")],
    "LOT-007": [("packaging", "minor", "confirmed"), ("label", "minor", "confirmed")],
    "LOT-009": [("finish", "minor", "confirmed")],
    "LOT-010": [("label", "minor", "confirmed")],
    "LOT-014": [("seam", "minor", "confirmed")],
}

INSPECTION_ORDER = ["LOT-001", "LOT-002", "LOT-003", "LOT-004", "LOT-005", "LOT-006", "LOT-007",
                    "LOT-008", "LOT-009", "LOT-010", "LOT-011", "LOT-012", "LOT-013", "LOT-014", "LOT-000"]


def build_inspections() -> list[dict]:
    rows: list[dict] = []
    day = date(2026, 10, 30)
    number = 0
    for lot in INSPECTION_ORDER:
        for code, severity, state in INSPECTION_PLAN[lot]:
            number += 1
            rows.append(dict(inspection_id=f"IN-{number:03d}", lot_id=lot, sample_size=5 + number % 18,
                             finding_code=code, severity=severity, review_state=state,
                             inspected_at=(day + timedelta(days=(number - 1) % 26)).isoformat()))
    clean = ["LOT-001", "LOT-005", "LOT-007", "LOT-009", "LOT-010", "LOT-014", "LOT-003", "LOT-012"]
    index = 0
    while number < 45:
        number += 1
        lot = clean[index % len(clean)]
        rows.append(dict(inspection_id=f"IN-{number:03d}", lot_id=lot, sample_size=6 + number % 15,
                         finding_code=["finish", "packaging", "label", "seam"][number % 4],
                         severity="minor", review_state="confirmed",
                         inspected_at=(day + timedelta(days=(number - 1) % 26)).isoformat()))
        index += 1
    assert len(rows) == 45
    return rows


def build_return_reviews(returns: list[dict]) -> list[dict]:
    by_id = {row["return_id"]: row for row in returns}
    plan = [
        ("RR-001", "RET-0002", "reclassify", "R-21", "2026-11-18", "QI-02",
         "客服照片显示开箱即破损，原‘说明理解’改判为质量类"),
        ("RR-002", "RET-0019", "reclassify", "R-21", "2026-11-20", "QI-03",
         "仓库复检确认包装挤压造成破损"),
        ("RR-003", "RET-0008", "reclassify", "R-22", "2026-11-17", "QI-02",
         "复核为页面说明缺失导致的理解偏差，非质量缺陷"),
        ("RR-004", "RET-0031", "confirm", "", "2026-11-19", "QI-01", "维持质量分类"),
        ("RR-005", "RET-0040", "confirm", "", "2026-11-20", "QI-02", "维持尺码分类"),
        ("RR-006", "RET-0006", "reclassify", "R-21", "2026-12-02", "QI-03",
         "补充照片确认鞋面开胶，复核后维持质量编码口径"),
        ("RR-007", "RET-0013", "reclassify", "R-21", "2026-12-04", "QI-01",
         "拆包复检确认破损，由尺码改判质量"),
        ("RR-008", "RET-0026", "reclassify", "R-22", "2026-12-06", "QI-02",
         "复核确认是说明文字问题，转说明类"),
        ("RR-009", "RET-0037", "reclassify", "R-22", "2026-12-08", "QI-03",
         "复核确认颜色描述差异，转说明类"),
        ("RR-010", "RET-0050", "confirm", "", "2026-11-21", "QI-01", "维持质量分类"),
    ]
    rows = []
    for review_id, return_id, decision, new_code, reviewed, reviewer, note in plan:
        if return_id not in by_id:
            raise SystemExit(f"review references unknown return {return_id}")
        rows.append(dict(review_id=review_id, return_id=return_id, decision=decision,
                         new_reason_code=new_code, reviewed_at=reviewed, reviewer=reviewer, note=note))
    return rows


def build_inspection_reviews(inspections: list[dict]) -> list[dict]:
    by_id = {row["inspection_id"]: row for row in inspections}
    targets = [
        ("IN-004", "confirmed", "QI-02", "复检确认标签问题，维持发现", "2026-11-19"),
        ("IN-006", "rejected", "QI-03", "样本外复检未复现，撤销该项发现", "2026-11-20"),
        ("IN-010", "rejected", "QI-01", "复检未复现，撤销", "2026-11-21"),
        ("IN-014", "confirmed", "QI-02", "复检确认缝线强度不足", "2026-11-22"),
        ("IN-019", "rejected", "QI-03", "复检未复现，撤销", "2026-11-23"),
        ("IN-024", "rejected", "QI-01", "复检未复现，撤销", "2026-11-23"),
        ("IN-029", "rejected", "QI-02", "复检未复现，撤销", "2026-11-24"),
        ("IN-034", "rejected", "QI-03", "复检未复现，撤销", "2026-11-24"),
        ("IN-039", "confirmed", "QI-01", "复检确认包装问题，维持发现", "2026-11-24"),
        ("IN-041", "rejected", "QI-02", "复检未复现，撤销", "2026-11-24"),
    ]
    rows = []
    for idx, (inspection_id, state, reviewer, rationale, reviewed) in enumerate(targets, start=1):
        if inspection_id not in by_id:
            raise SystemExit(f"review references unknown inspection {inspection_id}")
        rows.append(dict(review_id=f"IR-{idx:03d}", inspection_id=inspection_id, review_state=state,
                         reviewer=reviewer, rationale=rationale, reviewed_at=reviewed))
    return rows


def build_actions() -> list[dict]:
    plan = [
        ("LOT-006", "quality", "request-investigation", "confirmed", "2026-11-26", ""),
        ("LOT-006", "product", "revise-copy", "proposed", "2026-11-28", ""),
        ("LOT-006", "supply", "hold-shipment", "in_progress", "2026-11-27", "RC-30"),
        ("LOT-006", "product", "change-packaging", "proposed", "2026-11-29", ""),
        ("LOT-002", "quality", "monitor", "confirmed", "2026-11-24", ""),
        ("LOT-002", "product", "change-packaging", "proposed", "2026-11-23", ""),
        ("LOT-002", "supply", "hold-shipment", "confirmed", "2026-11-24", "RC-31"),
        ("LOT-008", "quality", "monitor", "confirmed", "2026-11-24", ""),
        ("LOT-008", "product", "monitor", "accepted", "2026-11-21", ""),
        ("LOT-008", "supply", "hold-shipment", "confirmed", "2026-11-24", "RC-32"),
        ("LOT-004", "quality", "monitor", "confirmed", "2026-11-23", ""),
        ("LOT-004", "product", "monitor", "accepted", "2026-11-21", ""),
        ("LOT-004", "supply", "hold-shipment", "proposed", "2026-11-23", ""),
        ("LOT-000", "quality", "monitor", "proposed", "2026-11-25", ""),
        ("LOT-000", "product", "monitor", "proposed", "2026-11-22", ""),
        ("LOT-000", "supply", "no-action", "proposed", "2026-11-25", ""),
        ("LOT-003", "product", "revise-copy", "complete", "2026-10-20", "RC-22"),
        ("LOT-003", "product", "monitor", "complete", "2026-11-18", ""),
        ("LOT-003", "supply", "no-action", "complete", "2026-11-19", ""),
        ("LOT-013", "product", "no-change", "cancelled", "2026-11-10", ""),
        ("LOT-013", "product", "monitor", "complete", "2026-11-18", ""),
        ("LOT-013", "supply", "no-action", "complete", "2026-11-19", ""),
        ("LOT-001", "quality", "close-case", "complete", "2026-11-15", ""),
        ("LOT-001", "supply", "no-action", "complete", "2026-11-16", ""),
        ("LOT-005", "quality", "close-case", "complete", "2026-11-14", ""),
        ("LOT-005", "supply", "no-action", "complete", "2026-11-15", ""),
        ("LOT-007", "quality", "close-case", "complete", "2026-11-13", ""),
        ("LOT-007", "supply", "no-action", "complete", "2026-11-14", ""),
        ("LOT-009", "quality", "close-case", "complete", "2026-11-12", ""),
        ("LOT-009", "supply", "no-action", "complete", "2026-11-13", ""),
        ("LOT-010", "supply", "supplier-fix", "complete", "2026-11-08", "RC-11"),
        ("LOT-010", "quality", "close-case", "complete", "2026-11-09", ""),
        ("LOT-011", "supply", "supplier-fix", "in_progress", "2026-11-22", "RC-17"),
        ("LOT-011", "quality", "request-investigation", "confirmed", "2026-11-22", ""),
        ("LOT-012", "supply", "release-hold", "cancelled", "2026-11-11", ""),
        ("LOT-012", "quality", "monitor", "confirmed", "2026-11-20", ""),
        ("LOT-014", "supply", "no-action", "complete", "2026-11-09", ""),
        ("LOT-014", "quality", "close-case", "complete", "2026-11-10", ""),
        ("LOT-006", "quality", "close-case", "superseded", "2026-10-15", ""),
    ]
    assert len(plan) == 39
    return [dict(action_id=f"AC-{i:03d}", lot_id=lot, owner_role=role, action_type=action,
                 decision_state=state, effective_at=effective, receipt_ref=receipt)
            for i, (lot, role, action, state, effective, receipt) in enumerate(plan, start=1)]


def build_receipts() -> list[dict]:
    return [
        dict(receipt_ref="RC-11", lot_id="LOT-010", receipt_type="supplier-response", outcome="accepted",
             received_at="2026-11-12", channel="supply", note="供应商提交根因分析"),
        dict(receipt_ref="RC-17", lot_id="LOT-011", receipt_type="supplier-response", outcome="partial",
             received_at="2026-11-22", channel="supply", note="供应商仅提交临时筛选方案"),
        dict(receipt_ref="RC-22", lot_id="LOT-003", receipt_type="channel-publish", outcome="accepted",
             received_at="2026-10-21", channel="product", note="新版商品说明已发布"),
        dict(receipt_ref="RC-30", lot_id="LOT-006", receipt_type="hold-ack", outcome="accepted",
             received_at="2026-11-27", channel="store", note="渠道确认 LOT-006 未发货批次已暂停"),
        dict(receipt_ref="RC-31", lot_id="LOT-002", receipt_type="hold-ack", outcome="accepted",
             received_at="2026-11-24", channel="marketplace", note="渠道确认 LOT-002 暂停发货"),
        dict(receipt_ref="RC-32", lot_id="LOT-008", receipt_type="hold-ack", outcome="partial",
             received_at="2026-11-24", channel="web", note="部分渠道确认，部分待回执"),
        dict(receipt_ref="RC-40", lot_id="", receipt_type="hold-ack", outcome="accepted",
             received_at="2026-11-03", channel="marketplace", note="历史汇总回执，未落到具体批次"),
        dict(receipt_ref="RC-41", lot_id="", receipt_type="supplier-response", outcome="accepted",
             received_at="2026-11-09", channel="supply", note="历史供应商回复汇总"),
        dict(receipt_ref="RC-50", lot_id="LOT-006", receipt_type="supplier-response", outcome="accepted",
             received_at="2026-12-15", channel="supply", note="供应商提交 8D 整改报告与首批复检记录"),
        dict(receipt_ref="RC-51", lot_id="LOT-006", receipt_type="channel-publish", outcome="accepted",
             received_at="2026-12-18", channel="product", note="新版商品说明已发布确认"),
        dict(receipt_ref="RC-52", lot_id="LOT-006", receipt_type="hold-release", outcome="accepted",
             received_at="2027-02-15", channel="marketplace", note="整改验证通过后恢复发货"),
    ]


def build_service(returns: list[dict]) -> list[dict]:
    quality_returns = [row for row in returns]
    plan = [
        ("quality", "买家反馈开箱即发现鞋面开胶，已提供照片", "confirmed"),
        ("quality", "门店反馈同批次多双出现开胶", "confirmed"),
        ("quality", "买家称缝线开裂，附照片", "confirmed"),
        ("quality", "买家称鞋底断裂", "confirmed"),
        ("quality", "买家称背包拉链损坏", "confirmed"),
        ("fit", "买家称尺码偏小，要求换大一码", "confirmed"),
        ("information", "买家称页面尺寸表与实物不符", "confirmed"),
        ("other", "买家仅填写“其他”", "pending"),
        ("other", "买家未接听回访电话", "pending"),
        ("information", "买家称颜色与页面差异大", "confirmed"),
        ("information", "买家称包装内说明缺失", "pending"),
        ("fit", "买家称腰围偏紧", "pending"),
        ("quality", "买家称面料有破洞", "pending"),
        ("other", "买家称不想要了，未描述商品问题", "pending"),
        ("information", "买家称尺码表与实物不符", "confirmed"),
        ("quality", "买家称开箱破损，外包装完好", "confirmed"),
        ("fit", "买家称版型偏窄", "confirmed"),
        ("information", "买家称颜色描述不准确", "confirmed"),
    ]
    picks = quality_returns[:18]
    rows = []
    for idx, ((topic, text, state), ret) in enumerate(zip(plan, picks), start=1):
        rows.append(dict(summary_id=f"CS-{idx:03d}", order_id=ret["order_id"],
                         received_at=(date.fromisoformat(ret["received_at"]) + timedelta(days=1)).isoformat(),
                         topic=topic, summary_text=text, review_state=state,
                         linked_return_id=ret["return_id"]))
    return rows


def build_copy_versions() -> list[dict]:
    plan = [
        ("CP-SKU003-V1", "SKU-003", "v1", "2026-08-01", "2026-10-19", "initial", "初版商品说明与尺码表"),
        ("CP-SKU003-V2", "SKU-003", "v2", "2026-10-20", "", "size-guide", "补充实测尺码与试穿建议"),
        ("CP-SKU007-V1", "SKU-007", "v1", "2026-08-01", "2026-10-17", "initial", "初版商品说明"),
        ("CP-SKU007-V2", "SKU-007", "v2", "2026-10-18", "", "color", "颜色描述改为实拍色并补充色差提示"),
        ("CP-SKU002-V1", "SKU-002", "v1", "2026-08-01", "", "initial", "初版商品说明"),
        ("CP-SKU006-V1", "SKU-006", "v1", "2026-08-01", "", "initial", "初版商品说明"),
        ("CP-SKU010-V1", "SKU-010", "v1", "2026-08-01", "", "initial", "初版商品说明"),
        ("CP-SKU014-V1", "SKU-014", "v1", "2026-08-01", "", "initial", "初版商品说明"),
        ("CP-SKU018-V1", "SKU-018", "v1", "2026-08-01", "", "initial", "初版商品说明"),
    ]
    return [dict(copy_version_id=c, sku=s, version=v, valid_from=f, valid_to=t, change_type=ct, summary=sm)
            for c, s, v, f, t, ct, sm in plan]


def build_followups() -> tuple[list[dict], list[dict]]:
    orders: list[dict] = []
    returns: list[dict] = []
    number = 0

    def add(sku, lot, channel, policy, sold, units, demos):
        nonlocal number
        number += 1
        oid = f"ORD-{200 + number:04d}"
        orders.append(dict(order_id=oid, sku=sku, lot_id=lot, channel=channel,
                           sold_at=sold.isoformat(), units=units, policy_version=policy))
        for j, (code, lag, refund, review) in enumerate(demos):
            returns.append(dict(return_id=f"RET-{300 + number * 3 + j:04d}", order_id=oid,
                                returned_units=1, reason_code=code,
                                received_at=(sold + timedelta(days=lag)).isoformat(),
                                refund_state=refund, review_state=review))

    for k in range(10):
        add("SKU-006", "LOT-006", "store", "POL-2", date(2026, 12, 2) + timedelta(days=3 * k), 2, [])
    for k in range(4):
        add("SKU-016", "LOT-006", "store", "POL-2", date(2026, 12, 8) + timedelta(days=4 * k), 2, [])
    for k in range(6):
        add("SKU-003", "LOT-003", CHANNELS[k % 3], "POL-3",
            date(2026, 12, 3) + timedelta(days=3 * k), 2,
            [("R-23", 9, "paid", "coded")] if k in (1, 4) else [])
    for k in range(4):
        add("SKU-003", "LOT-013", CHANNELS[k % 3], "POL-3", date(2026, 12, 5) + timedelta(days=3 * k), 2, [])
    for k in range(4):
        add("SKU-002", "LOT-002", CHANNELS[k % 3], "POL-2", date(2026, 12, 4) + timedelta(days=4 * k), 2,
            [("R-21", 10, "paid", "coded")] if k in (1, 3) else [])
    for k in range(3):
        add("SKU-008", "LOT-008", CHANNELS[k % 3], "POL-1", date(2026, 12, 6) + timedelta(days=5 * k), 2,
            [("R-21", 11, "paid", "coded")] if k == 1 else [])
    return orders, returns


def write_csv(name: str, header: list[str], rows: list[dict]) -> None:
    with (DATA / name).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    orders = build_orders()
    returns = build_returns(orders)
    inspections = build_inspections()
    assert len(orders) == 120, len(orders)

    write_csv("products.csv", ["sku", "name", "category", "launched_on", "active_copy_version"],
              [dict(zip(["sku", "name", "category", "launched_on", "active_copy_version"], p)) for p in PRODUCTS])
    write_csv("lots.csv", ["lot_id", "supplier_id", "supplier_lot_ref", "produced_on", "unshipped_units",
                           "traceability_state", "note"],
              [dict(lot_id=lot, supplier_id=m["supplier"], supplier_lot_ref=m["ref"], produced_on=m["produced"],
                    unshipped_units=m["unshipped"], traceability_state=m["state"], note=m["note"])
               for lot, m in LOT_META.items()])
    write_csv("orders.csv", ["order_id", "sku", "lot_id", "channel", "sold_at", "units", "policy_version"], orders)
    write_csv("returns.csv", ["return_id", "order_id", "returned_units", "reason_code", "received_at",
                              "refund_state", "review_state"], returns)

    reason_map = [
        ("R-01", "fit", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-02", "information", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-03", "unknown", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-04", "quality", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-05", "quality", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-06", "information", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-07", "fit", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-08", "unknown", "2026-01-01", "2026-11-14", "MAP-1"),
        ("R-21", "quality", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-22", "information", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-23", "fit", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-24", "unknown", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-25", "quality", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-26", "information", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-27", "fit", "2026-11-15", "2027-12-31", "MAP-2"),
        ("R-28", "unknown", "2026-11-15", "2027-12-31", "MAP-2"),
    ]
    write_csv("reason_map.csv", ["reason_code", "category", "valid_from", "valid_to", "mapping_version"],
              [dict(zip(["reason_code", "category", "valid_from", "valid_to", "mapping_version"], row))
               for row in reason_map])
    aliases = [
        ("damaged", "R-04", "LEGACY-LABEL-1", "2026-01-01", "2027-12-31", "旧标签兼容：损毁类"),
        ("description", "R-02", "LEGACY-LABEL-1", "2026-01-01", "2027-12-31", "旧标签兼容：说明理解类，不作为质量证据"),
        ("fit", "R-01", "LEGACY-LABEL-1", "2026-01-01", "2027-12-31", "旧标签兼容：尺码类"),
        ("other", "R-03", "LEGACY-LABEL-1", "2026-01-01", "2027-12-31", "旧标签兼容：其他/原因未知"),
    ]
    write_csv("reason_aliases.csv", ["alias_code", "canonical_code", "scheme_version", "valid_from", "valid_to", "note"],
              [dict(zip(["alias_code", "canonical_code", "scheme_version", "valid_from", "valid_to", "note"], a))
               for a in aliases])
    write_csv("inspections.csv", ["inspection_id", "lot_id", "sample_size", "finding_code", "severity",
                                  "review_state", "inspected_at"], inspections)
    write_csv("actions.csv", ["action_id", "lot_id", "owner_role", "action_type", "decision_state",
                              "effective_at", "receipt_ref"], build_actions())
    write_csv("return_reviews.csv", ["review_id", "return_id", "decision", "new_reason_code", "reviewed_at",
                                     "reviewer", "note"], build_return_reviews(returns))
    write_csv("inspection_reviews.csv", ["review_id", "inspection_id", "review_state", "reviewer",
                                         "rationale", "reviewed_at"], build_inspection_reviews(inspections))
    write_csv("receipts.csv", ["receipt_ref", "lot_id", "receipt_type", "outcome", "received_at",
                               "channel", "note"], build_receipts())
    write_csv("customer_service.csv", ["summary_id", "order_id", "received_at", "topic", "summary_text",
                                       "review_state", "linked_return_id"], build_service(returns))
    write_csv("copy_versions.csv", ["copy_version_id", "sku", "version", "valid_from", "valid_to",
                                    "change_type", "summary"], build_copy_versions())
    follow_orders, follow_returns = build_followups()
    write_csv("followup_orders.csv", ["order_id", "sku", "lot_id", "channel", "sold_at", "units", "policy_version"],
              follow_orders)
    write_csv("followup_returns.csv", ["return_id", "order_id", "returned_units", "reason_code", "received_at",
                                       "refund_state", "review_state"], follow_returns)
    print(f"orders={len(orders)} returns={len(returns)} inspections={len(inspections)} "
          f"actions={len(build_actions())} followup_orders={len(follow_orders)}")


if __name__ == "__main__":
    main()
