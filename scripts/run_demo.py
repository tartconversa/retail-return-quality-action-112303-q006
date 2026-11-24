"""End-to-end demonstration on the bundled synthetic data.

The script builds a throw-away database, imports the source files, applies the
review ledger, runs a baseline analysis, walks one investigation case through
all three roles (quality / product / supply), records a post-remediation
improvement check, and writes a Chinese Markdown report plus CSV exports.

Usage::

    python scripts/run_demo.py --workdir /tmp/rrqa-demo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retail_return_quality_action import analysis, db, importer, reasons, report, workflow  # noqa: E402
from retail_return_quality_action.config import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=str(ROOT / "var" / "demo"))
    parser.add_argument("--as-of", default="2026-11-25")
    parser.add_argument("--cutoff", default="2027-01-31")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "app.db"
    if db_path.exists():
        db_path.unlink()

    settings = load_settings()
    conn = db.connect(db_path)
    print("== 导入资料 ==")
    for batch in importer.import_directory(conn, ROOT / "data"):
        print(f"  {batch.dataset:18s} 新增 {batch.inserted:3d} 更新 {batch.updated:2d} "
              f"未变 {batch.unchanged:3d} 拒绝 {batch.rejected}")
    print("== 应用复核台账 ==")
    print(" ", reasons.seed_reviews_from_import(conn))

    print("== 基线分析 ==")
    result = analysis.analyze(conn, as_of=args.as_of, settings=settings, note="基线")
    print(f"  快照 #{result.snapshot_id}；成熟暴露 {result.overall['exposure_units']} 件；"
          f"质量退货 {result.overall['quality_units']}；未知 {result.overall['unknown_units']}")
    for level in ("lot", "sku"):
        for risk in sorted(result.entities[level], key=lambda r: -(r.smr or 0)):
            if risk.proposed_level in ("investigate", "escalate"):
                print(f"  [{level}] {risk.entity_id}: {risk.proposed_level} "
                      f"SMR={risk.smr:.2f} q={risk.q_value:.3f}")

    print("== 质量负责人立案并确认等级 ==")
    case = workflow.open_case(
        conn, "lot", "LOT-006", actor="QI-01",
        rationale="同一材料的两个批次重复出现已确认关键质检缺陷，暴露校正后退货率显著偏高",
    )
    workflow.record_grade(
        conn, case["case_id"], grade="escalate", actor="QI-01",
        rationale="SMR 约 3 倍且 q<0.10，两个批次均确认 finish 关键缺陷",
    )
    print(f"  案件 {case['case_id']} -> escalate")

    print("== 三个角色分别决定 ==")
    hold = workflow.record_decision(
        conn, case["case_id"], decision="hold-shipment", actor="SC-01", role="supply",
        rationale="暂停 LOT-006 未发货批次并向渠道确认", lot_id="LOT-006",
    )["action_id"]
    workflow.record_decision(
        conn, case["case_id"], decision="request-investigation", actor="QI-01", role="quality",
        rationale="要求供应商提交根因分析与整改计划",
    )
    workflow.record_decision(
        conn, case["case_id"], decision="revise-copy", actor="PD-02", role="product",
        rationale="补充材质与保养说明，减少说明理解类退货",
    )
    print("  已记录 supply / quality / product 决定")

    print("== 执行供应链动作（需要回执） ==")
    workflow.transition_action(conn, hold, to_state="confirmed", actor="SC-01", role="supply")
    workflow.transition_action(conn, hold, to_state="in_progress", actor="SC-01", role="supply")
    try:
        workflow.transition_action(conn, hold, to_state="complete", actor="SC-01", role="supply")
    except Exception as exc:  # noqa: BLE001 - demonstration of the guard
        print(f"  拒绝无回执完成: {exc}")
    workflow.register_receipt(
        conn, receipt_ref="RCP-DEMO-1", receipt_type="hold-ack", outcome="accepted",
        received_at="2026-11-27", lot_id="LOT-006", channel="store", actor="SC-01",
    )
    workflow.transition_action(
        conn, hold, to_state="complete", actor="SC-01", role="supply", receipt_ref="RCP-DEMO-1"
    )
    print("  渠道回执已登记，暂停发货完成")

    print("== 改进核查 ==")
    check = workflow.record_improvement_check(
        conn, case["case_id"], actor="QI-01", cutoff=args.cutoff, anchor="2026-11-27",
    )
    print(f"  前段 {check['pre_events']}/{check['pre_exposure']}，后段 {check['post_events']}/{check['post_exposure']}，"
          f"结论={check['verdict']}，p={check['p_value']:.2e}")
    mix = json.loads(check.get("mix_json") or "{}")
    if mix:
        print(f"  构成效应 {mix['mix_effect']:+.3f}，率效应 {mix['rate_effect']:+.3f}")

    print("== 登记退款执行（既成事实） ==")
    pending = conn.execute(
        "SELECT r.return_id FROM returns r JOIN orders o USING(order_id)"
        " WHERE o.lot_id='LOT-006' AND r.refund_state='pending' LIMIT 1"
    ).fetchone()
    if pending:
        workflow.record_refund_state(
            conn, pending["return_id"], state="paid", actor="CS-01", role="quality",
            amount_units=1, note="按售后政策执行退款",
        )
        print(f"  {pending['return_id']} 退款已执行并留痕")

    print("== 重新分析（更正后重算当前风险） ==")
    later = analysis.analyze(conn, as_of=args.cutoff, settings=settings, note="复核后")
    before = result.get("lot", "LOT-006")
    after = later.get("lot", "LOT-006")
    print(f"  LOT-006 质量退货 {before.quality_units} -> {after.quality_units}，"
          f"建议等级 {before.proposed_level} -> {after.proposed_level}")
    refunds = conn.execute(
        "SELECT refund_state, COUNT(*) FROM returns GROUP BY refund_state ORDER BY refund_state"
    ).fetchall()
    executions = conn.execute("SELECT COUNT(*) FROM refund_event").fetchone()[0]
    print("  原始退款记录保持不变:", {row[0]: row[1] for row in refunds},
          f"追加执行事件 {executions} 条")

    report_path = workdir / "报告.md"
    report.write_markdown_report(conn, str(report_path), snapshot_id=later.snapshot_id, settings=settings)
    exports = report.export_results(conn, snapshot_id=later.snapshot_id, out_dir=str(workdir))
    print(f"== 输出 ==\n  报告: {report_path}")
    for name, path in exports.items():
        print(f"  {name}: {path}")

    manifest = json.loads(Path(exports["manifest"]).read_text(encoding="utf-8"))
    print("  快照摘要:", manifest["snapshot_id"], manifest["config_digest"][:12], manifest["data_digest"][:12])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
