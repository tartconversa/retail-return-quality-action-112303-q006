"""Human-readable and machine-readable reporting.

Every report states the cut-off, how many orders were excluded as immature,
which records could not be traced to a batch, and which reason codes are
unknown.  That prevents a reader from treating missing data as "no problem".
"""

from __future__ import annotations

import json
from typing import Any

from .analysis import load_snapshot
from .config import Settings, load_settings
from .csvio import write_csv, write_text

LEVEL_LABELS = {"lot": "批次", "sku": "商品"}
GRADE_LABELS = {"none": "无需处理", "watch": "观察", "investigate": "调查", "escalate": "升级"}
REASON_LABELS = {
    "no_exposure": "无成熟暴露",
    "exposure_immature": "暴露未成熟",
    "context_only": "背景关注",
    "no_signal": "无异常",
    "strong_statistical_signal": "统计显著偏高",
    "adjusted_rate_excess": "分层后偏高",
    "repeat_evidence": "重复问题",
    "across_lots": "跨批次重复",
    "weak_signal": "弱信号",
    "inspection_attention": "质检需关注",
    "evidence_blocking": "证据未决",
    "unknown_share": "原因未知偏高",
    "traceability_gap": "批次追溯缺口",
    "information_signal": "商品说明信号",
    "statistical_context": "统计背景",
    "confirmed_inspection": "已确认质检",
    "no_supplier_data": "缺少供应商信息",
}
VERDICT_LABELS = {
    "improved": "改善",
    "worse": "恶化",
    "not_conclusive": "尚不能判断",
    "insufficient_data": "数据不足",
    "immature_exposure": "暴露尚未成熟",
}


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown_report(conn, *, snapshot_id: int | None = None, settings: Settings | None = None) -> str:
    settings = settings or load_settings()
    snapshot = load_snapshot(conn, snapshot_id)
    meta = snapshot["snapshot"]
    stats = snapshot["stats"]
    overall = stats["overall"]
    results = snapshot["results"]
    lots = {row["lot_id"]: dict(row) for row in conn.execute("SELECT * FROM lots")}
    products = {row["sku"]: dict(row) for row in conn.execute("SELECT * FROM products")}
    cases = {case["case_id"]: case for case in _cases(conn)}

    lines: list[str] = []
    lines.append("# 商品售后与质检风险报告")
    lines.append("")
    lines.append(f"- 分析截止日：{meta['as_of']}")
    lines.append(f"- 风险快照：#{meta['snapshot_id']}（配置摘要 `{meta['config_digest'][:12]}`，数据摘要 `{meta['data_digest'][:12]}`）")
    lines.append("- 输入资料为本地合成数据，不代表真实商品、企业或消费者。")
    lines.append("")

    lines.append("## 结论摘要")
    lines.append("")
    exposure = overall["exposure_units"]
    quality = overall["quality_units"]
    unknown = overall["unknown_units"]
    lines.append(
        f"成熟销售暴露 {exposure:,} 件，质量类退货 {quality} 件（粗率 {_fmt_pct(overall['quality_rate'])}），"
        f"原因未知 {unknown} 件（占已分类退货 {_fmt_pct(overall['unknown_share'])}）。"
        f"另有 {overall['immature_units']:,} 件销售因观察窗口未满而排除，不计入分母；"
        f"截止日之后成交的 {overall.get('future_units_excluded', 0):,} 件同样不参与本次比较。"
    )
    if overall.get("worst_case_rate") is not None:
        lines.append(
            f"若把原因未知的退货全部视为质量问题，质量退货率上界为 {_fmt_pct(overall['worst_case_rate'])}；"
            "该上界用于兜底，不替代分类复核。"
        )
    lines.append("")

    for level in ("lot", "sku"):
        rows = [r for r in results if r["level"] == level]
        flagged = [r for r in rows if r["proposed_level"] in ("investigate", "escalate", "watch")]
        lines.append(f"## {LEVEL_LABELS[level]}级信号")
        lines.append("")
        if not flagged:
            lines.append("没有需要关注的对象。")
            lines.append("")
            continue
        header = (
            "| 对象 | 建议等级 | 成熟暴露 | 质量退货 | 未知 | 期望 | SMR | q 值 | "
            "已确认质检权重 | 主要依据 | 当前案件 |"
        )
        sep = "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"
        lines.append(header)
        lines.append(sep)
        for row in sorted(flagged, key=_sort_key):
            reasons = _reason_text(row)
            case = _case_for(cases, level, row["entity_id"])
            lines.append(
                f"| {row['entity_id']} | {GRADE_LABELS.get(row['proposed_level'], row['proposed_level'])} "
                f"| {row['exposure_units']} | {row['quality_units']} | {row['unknown_units']} "
                f"| {_fmt(row['expected_units'])} | {_fmt(row['smr'])} | {_fmt(row['q_value'], 3)} "
                f"| {row['evidence_confirmed']} | {reasons} | {case} |"
            )
        lines.append("")

    lines.append("## 渠道与政策分层")
    lines.append("")
    lines.append(
        "退货率必须按销售暴露和渠道政策分层解读：不同渠道的退货窗口、退款政策不同，"
        "直接比较原始退货率会把渠道政策差异误读为产品缺陷。"
    )
    lines.append("")
    lines.append("| 分层（渠道/政策） | 成熟暴露 | 质量退货 | 未知 | 说明类 | 质量退货率 | 最坏情况率 | 未知占比 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in stats["strata"]:
        lines.append(
            f"| {row['channel']} / {row['policy_version']} | {row['exposure_units']} | {row['quality_units']} "
            f"| {row['unknown_units']} | {row['information_units']} | {_fmt_pct(row['quality_rate'])} "
            f"| {_fmt_pct(row['worst_case_rate'])} | {_fmt_pct(row['unknown_share'])} |"
        )
    lines.append("")
    het = overall.get("stratum_heterogeneity_p")
    if het is not None:
        lines.append(
            f"分层异质性检验 p = {_fmt(het, 4)}；"
            + ("分层间差异显著，未分层比较会混入渠道政策效应。" if het < 0.05 else "未见显著分层差异。")
        )
        lines.append("")

    lines.append("## 数据质量与未知范围")
    lines.append("")
    dq = overall["data_quality"]
    lines.append("| 指标 | 数量 | 说明 |")
    lines.append("| --- | ---: | --- |")
    rows = [
        ("订单缺少批次号", dq["orders_without_lot"], "无法归属到批次"),
        ("订单使用占位批次", dq["orders_unknown_lot_placeholder"], "批次号占位，不能对具体批次处置"),
        ("退货引用不存在订单", dq["returns_orphan"], "按不可追溯处理，不计入任何对象"),
        ("分析截止日之后的退货", dq["returns_after_cutoff"], "保留记录，不计入当前风险"),
        ("退货件数超过订单件数", dq["returns_over_order"], "分析时按订单件数封顶"),
        ("原因编码不在有效映射", dq["returns_reason_unmapped"], "按未知处理"),
        ("原因类别为未知", dq["returns_category_unknown"], "计入最坏情况率"),
        ("已修订退货", dq["returns_revised"], "按修订后分类重算"),
        ("超出政策窗口后到达的退货", dq["returns_late_outside_window"], "仍计入当前风险，但单独提示延迟"),
        ("质检已确认", dq["inspections_confirmed"], "可作为升级依据"),
        ("质检待审/争议", dq["inspections_unresolved"], "不能视为确认，也不视为无问题"),
    ]
    for label, count, note in rows:
        lines.append(f"| {label} | {count} | {note} |")
    lines.append("")
    lines.append(
        f"退款状态分布：已退款 {dq['refund_paid_units']} 件、待处理 {dq['refund_pending_units']} 件、"
        f"已拒绝 {dq['refund_rejected_units']} 件（按退货件数，已封顶）。"
    )
    lines.append("")

    lines.append("## 按商品/批次的处置状态")
    lines.append("")
    if cases:
        lines.append("| 案件 | 等级 | 状态 | 范围批次 | 动作 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for case in cases.values():
            actions = _actions_for_case(conn, case["case_id"])
            action_text = "；".join(
                f"{a['action_type']}={a['decision_state']}" for a in actions
            ) or "无"
            lines.append(
                f"| {case['case_id']} | {GRADE_LABELS.get(case['grade'] or '-', case['grade'] or '-')} "
                f"| {case['status']} | {', '.join(json.loads(case['scope_json']))} | {action_text} |"
            )
    else:
        lines.append("尚无人工立案记录。")
    lines.append("")

    checks = conn.execute("SELECT * FROM improvement_check ORDER BY check_id").fetchall()
    lines.append("## 改进核查")
    lines.append("")
    if checks:
        lines.append("| 案件 | 核查截止 | 前段暴露/事件 | 后段暴露/事件 | 比率 | 95% 区间 | p | 构成/率效应 | 结论 |")
        lines.append("| --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- |")
        for row in checks:
            ratio = "—" if row["rate_ratio"] is None else f"{row['rate_ratio']:.2f}"
            interval = (
                "—"
                if row["rate_ratio_lo"] is None
                else f"{row['rate_ratio_lo']:.2f}–{row['rate_ratio_hi']:.2f}"
            )
            try:
                mix = json.loads(row["mix_json"]) if "mix_json" in row.keys() and row["mix_json"] else {}
            except (ValueError, TypeError):
                mix = {}
            mix_text = (
                f"{mix['mix_effect']:+.3f} / {mix['rate_effect']:+.3f}" if mix else "—"
            )
            lines.append(
                f"| {row['case_id']} | {row['cutoff_at']} | {row['pre_exposure']}/{row['pre_events']} "
                f"| {row['post_exposure']}/{row['post_events']} | {ratio} | {interval} "
                f"| {_fmt(row['p_value'], 4)} | {mix_text} | {VERDICT_LABELS.get(row['verdict'], row['verdict'])} |"
            )
    else:
        lines.append("尚未进行改进核查。")
    lines.append("")

    if stats.get("notes"):
        lines.append("## 分析提示")
        lines.append("")
        for note in stats["notes"]:
            lines.append(f"- {note}")
        lines.append("")

    events = conn.execute(
        "SELECT * FROM refund_event ORDER BY recorded_at, event_id"
    ).fetchall()
    if events:
        lines.append("## 退款执行记录")
        lines.append("")
        lines.append("退款属于既成事实，复核改判不会改写已执行的退款。")
        lines.append("")
        lines.append("| 退货 | 原状态 | 新状态 | 操作人 | 角色 | 记录时间 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in events:
            lines.append(
                f"| {row['return_id']} | {row['old_state']} | {row['new_state']} | {row['actor']} "
                f"| {row['role']} | {row['recorded_at']} |"
            )
        lines.append("")

    lines.append("## 阅读须知")
    lines.append("")
    lines.append("- 建议等级由规则和统计证据生成，最终等级以质量负责人确认记录为准。")
    lines.append("- 商品说明换版只解释“说明理解类”退货，不能替代质量证据。")
    lines.append("- 已执行的退款与供应商处置保留当时决定；重新分析只更新当前风险与建议。")
    lines.append("- 原因为未知或批次追溯不完整的记录已单列，不能按零风险处理。")
    lines.append("")
    return "\n".join(lines)


def _sort_key(row: dict[str, Any]) -> tuple:
    order = {"escalate": 0, "investigate": 1, "watch": 2, "none": 3}
    return (order.get(row["proposed_level"], 9), -(row["smr"] or 0), row["entity_id"])


def _reason_text(row: dict[str, Any]) -> str:
    reasons = row.get("reasons_json") or []
    if not reasons:
        return "—"
    parts = []
    for reason in reasons:
        label = REASON_LABELS.get(reason.get("code"), reason.get("code"))
        detail = reason.get("text")
        parts.append(f"{label}：{detail}" if detail else label)
    return "；".join(parts)


def _case_for(cases: dict[str, dict], level: str, entity_id: str) -> str:
    for case in cases.values():
        if case["level"] == level and case["entity_id"] == entity_id:
            return f"{case['case_id']}（{case['status']}）"
    return "—"


def _cases(conn) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM case_record ORDER BY case_id")]


def _actions_for_case(conn, case_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM actions WHERE case_id=? ORDER BY action_id", (case_id,))]


def export_results(conn, *, snapshot_id: int | None = None, out_dir: str | None = None) -> dict[str, str]:
    """Export snapshot results, strata and case decisions as CSV files."""
    from pathlib import Path

    snapshot = load_snapshot(conn, snapshot_id)
    folder = Path(out_dir or ".")
    sid = snapshot["snapshot"]["snapshot_id"]
    results_path = write_csv(
        folder / f"risk_results_{sid}.csv",
        [
            "level", "entity_id", "proposed_level", "exposure_units", "immature_units",
            "quality_units", "unknown_units", "information_units", "expected_units", "smr",
            "p_value", "q_value", "crude_rate", "crude_lo", "crude_hi", "unknown_share",
            "worst_case_rate", "traceability_gap_share", "evidence_confirmed", "evidence_pending",
            "evidence_blocking", "reasons_json",
        ],
        snapshot["results"],
    )
    strata_path = write_csv(
        folder / f"strata_{sid}.csv",
        ["stratum", "channel", "policy_version", "exposure_units", "quality_units", "unknown_units",
         "information_units", "quality_rate", "worst_case_rate", "unknown_share"],
        snapshot["stats"]["strata"],
    )
    decisions = [
        dict(row)
        for row in conn.execute(
            "SELECT d.* FROM case_decision d ORDER BY d.decided_at, d.decision_id"
        )
    ]
    decisions_path = write_csv(
        folder / f"case_decisions_{sid}.csv",
        ["decision_id", "case_id", "actor", "role", "decision_type", "value", "rationale",
         "decided_at", "effective_at", "snapshot_id"],
        decisions,
    )
    manifesto_path = write_text(
        folder / f"manifest_{sid}.json",
        json.dumps(
            {
                "snapshot_id": sid,
                "as_of": snapshot["snapshot"]["as_of"],
                "config_digest": snapshot["snapshot"]["config_digest"],
                "data_digest": snapshot["snapshot"]["data_digest"],
                "overall": snapshot["stats"]["overall"],
                "notes": snapshot["stats"].get("notes", []),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return {
        "risk_results": str(results_path),
        "strata": str(strata_path),
        "case_decisions": str(decisions_path),
        "manifest": str(manifesto_path),
    }


def write_markdown_report(conn, path: str, *, snapshot_id: int | None = None, settings: Settings | None = None) -> str:
    content = markdown_report(conn, snapshot_id=snapshot_id, settings=settings)
    return str(write_text(path, content))


def decision_log(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT d.decision_id, d.case_id, c.level, c.entity_id, d.actor, d.role, d.decision_type,"
            " d.value, d.rationale, d.decided_at, d.effective_at, d.snapshot_id"
            " FROM case_decision d JOIN case_record c ON c.case_id=d.case_id"
            " ORDER BY d.decided_at, d.decision_id"
        )
    ]


def refund_state_history(conn) -> list[dict[str, Any]]:
    """Show refunds exactly as imported; reclassification must not rewrite them."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT r.return_id, r.order_id, r.returned_units, r.refund_state, r.review_state,"
            " o.lot_id, o.sku FROM returns r LEFT JOIN orders o ON o.order_id=r.order_id"
            " ORDER BY r.return_id"
        )
    ]
