"""Command line interface.

Typical flow::

    python -m retail_return_quality_action init
    python -m retail_return_quality_action import --dir data
    python -m retail_return_quality_action review apply
    python -m retail_return_quality_action analyze --as-of 2026-11-25
    python -m retail_return_quality_action report --out reports/report.md
    python -m retail_return_quality_action open-case --level lot --id LOT-006 --actor QI-01 --reason ...
    python -m retail_return_quality_action grade CASE-0001 --grade escalate --actor QI-01 --reason ...
    python -m retail_return_quality_action decide CASE-0001 --decision hold-shipment \
        --role supply --actor SC-01 --lot LOT-006 --reason ...
    python -m retail_return_quality_action action ACT-0001 --to confirmed --role supply --actor SC-01
    python -m retail_return_quality_action check CASE-0001 --cutoff 2027-01-31 --anchor 2026-11-27
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import analysis, db, importer, reasons, report, workflow
from .config import load_settings
from .errors import AppError
from .timeutil import iso

DEFAULT_DB = "var/app.db"


def _connect(args) -> Any:
    return db.connect(args.db)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _emit_batches(results) -> None:
    _print([result.as_dict() for result in results])


def _emit_analysis(result: analysis.AnalysisResult, top: int = 0) -> None:
    payload: dict[str, Any] = {
        "snapshot_id": result.snapshot_id,
        "as_of": result.as_of.isoformat(),
        "overall": result.overall,
        "notes": result.notes,
        "strata": result.strata,
    }
    for level, entities in result.entities.items():
        ranked = sorted(
            entities,
            key=lambda e: ({"escalate": 0, "investigate": 1, "watch": 2, "none": 3}.get(e.proposed_level, 9),
                           -(e.smr or 0)),
        )
        items = ranked[:top] if top else ranked
        payload[level] = [risk.as_row() for risk in items]
    _print(payload)


def cmd_init(args) -> int:
    conn = _connect(args)
    settings = load_settings(args.config)
    conn.execute("SELECT 1")
    conn.close()
    _print({"db": args.db, "config": settings.source_path, "config_digest": settings.digest[:12]})
    return 0


def cmd_import(args) -> int:
    conn = _connect(args)
    results = []
    if args.dir:
        results = importer.import_directory(conn, args.dir, strict=args.strict)
    else:
        results = [importer.import_dataset(conn, args.file, dataset=args.dataset, strict=args.strict)]
    conn.close()
    _emit_batches(results)
    rejected = sum(r.rejected for r in results)
    return 1 if rejected and args.strict else 0


def cmd_review(args) -> int:
    conn = _connect(args)
    if args.action in ("apply", "all"):
        result = reasons.seed_reviews_from_import(conn)
        _print({"applied": result})
    if args.action == "list":
        rows = [
            dict(row) for row in conn.execute(
                "SELECT r.return_id, r.seq, r.old_reason_code, r.new_reason_code, r.effective_at,"
                " r.decided_by, r.rationale FROM return_revision r ORDER BY r.return_id, r.seq"
            )
        ]
        _print(rows)
    conn.close()
    return 0


def cmd_analyze(args) -> int:
    conn = _connect(args)
    settings = load_settings(args.config)
    result = analysis.analyze(
        conn,
        as_of=args.as_of,
        settings=settings,
        note=args.note,
        persist=not args.dry_run,
    )
    _emit_analysis(result, top=args.top)
    conn.close()
    return 0


def cmd_show(args) -> int:
    conn = _connect(args)
    snapshot = analysis.load_snapshot(conn, args.snapshot)
    results = [r for r in snapshot["results"] if r["level"] == args.level]
    if args.id:
        results = [r for r in results if r["entity_id"] == args.id]
    _print({"snapshot": snapshot["snapshot"], "results": results})
    conn.close()
    return 0


def cmd_report(args) -> int:
    conn = _connect(args)
    settings = load_settings(args.config)
    if args.out:
        path = report.write_markdown_report(conn, args.out, snapshot_id=args.snapshot, settings=settings)
        exported = report.export_results(
            conn, snapshot_id=args.snapshot, out_dir=str(Path(args.out).parent)
        )
        _print({"markdown": path, "exports": exported})
    else:
        print(report.markdown_report(conn, snapshot_id=args.snapshot, settings=settings))
    conn.close()
    return 0


def cmd_open_case(args) -> int:
    conn = _connect(args)
    case = workflow.open_case(
        conn, args.level, args.id, actor=args.actor, rationale=args.reason,
        snapshot_id=args.snapshot, scope=args.scope,
    )
    _print(case)
    conn.close()
    return 0


def cmd_grade(args) -> int:
    conn = _connect(args)
    case = workflow.record_grade(
        conn, args.case_id, grade=args.grade, actor=args.actor, rationale=args.reason, scope=args.scope
    )
    _print(case)
    conn.close()
    return 0


def cmd_decide(args) -> int:
    conn = _connect(args)
    result = workflow.record_decision(
        conn, args.case_id, decision=args.decision, actor=args.actor, role=args.role,
        rationale=args.reason, lot_id=args.lot, supplier_ref=args.supplier_ref,
    )
    _print(result)
    conn.close()
    return 0


def cmd_action(args) -> int:
    conn = _connect(args)
    action = workflow.transition_action(
        conn, args.action_id, to_state=args.to, actor=args.actor, role=args.role,
        receipt_ref=args.receipt, note=args.note,
    )
    _print(action)
    conn.close()
    return 0


def cmd_receipt(args) -> int:
    conn = _connect(args)
    receipt = workflow.register_receipt(
        conn, receipt_ref=args.receipt_ref, receipt_type=args.type, outcome=args.outcome,
        received_at=args.received_at, lot_id=args.lot, channel=args.channel, note=args.note,
        actor=args.actor,
    )
    _print(receipt)
    conn.close()
    return 0


def cmd_close(args) -> int:
    conn = _connect(args)
    case = workflow.close_case(
        conn, args.case_id, actor=args.actor, role=args.role, outcome=args.outcome, rationale=args.reason
    )
    _print(case)
    conn.close()
    return 0


def cmd_reopen(args) -> int:
    conn = _connect(args)
    case = workflow.reopen_case(conn, args.case_id, actor=args.actor, role=args.role, rationale=args.reason)
    _print(case)
    conn.close()
    return 0


def cmd_check(args) -> int:
    conn = _connect(args)
    check = workflow.record_improvement_check(
        conn, args.case_id, actor=args.actor, cutoff=args.cutoff, anchor=args.anchor,
        pre_days=args.pre_days, post_days=args.post_days, note=args.note,
    )
    _print(check)
    conn.close()
    return 0


def cmd_cases(args) -> int:
    conn = _connect(args)
    cases = workflow.list_cases(conn, status=args.status)
    for case in cases:
        case["timeline"] = workflow.case_timeline(conn, case["case_id"])
        case["improvement_checks"] = workflow.case_improvement_checks(conn, case["case_id"])
    _print(cases)
    conn.close()
    return 0


def cmd_refund(args) -> int:
    conn = _connect(args)
    event = workflow.record_refund_state(
        conn, args.return_id, state=args.state, actor=args.actor, role=args.role,
        amount_units=args.amount, note=args.note,
    )
    _print(event)
    conn.close()
    return 0


def cmd_decisions(args) -> int:
    conn = _connect(args)
    _print(report.decision_log(conn))
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retail-return-quality-action", description="商品售后与质检风险分析")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 数据库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--config", default=None, help="分析配置 JSON 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化数据库")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="导入 CSV 资料")
    p.add_argument("--dir", help="资料目录，按依赖顺序导入全部可识别文件")
    p.add_argument("--file", help="单个 CSV 文件")
    p.add_argument("--dataset", help="显式指定数据集名称（与 --file 搭配）")
    p.add_argument("--strict", action="store_true", help="遇到校验错误立即中止")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("review", help="退货原因复核台账")
    p.add_argument("action", choices=["apply", "list", "all"])
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("analyze", help="计算暴露校正风险并保存快照")
    p.add_argument("--as-of", help="分析截止日 YYYY-MM-DD（默认取最新退货日期）")
    p.add_argument("--top", type=int, default=0, help="每级只输出前 N 条")
    p.add_argument("--dry-run", action="store_true", help="只计算不落库")
    p.add_argument("--note", help="快照备注")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("show", help="查看快照结果")
    p.add_argument("--snapshot", type=int)
    p.add_argument("--level", choices=["lot", "sku"], default="lot")
    p.add_argument("--id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("report", help="生成中文报告与导出文件")
    p.add_argument("--snapshot", type=int)
    p.add_argument("--out", help="Markdown 输出路径；同时导出 CSV 与清单")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("open-case", help="为商品或批次立案")
    p.add_argument("--level", choices=["lot", "sku"], required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--snapshot", type=int)
    p.add_argument("--scope", nargs="*", help="范围内的批次列表")
    p.set_defaults(func=cmd_open_case)

    p = sub.add_parser("grade", help="质量负责人确认问题等级")
    p.add_argument("case_id")
    p.add_argument("--grade", choices=["none", "watch", "investigate", "escalate"], required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--scope", nargs="*")
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("decide", help="记录商品或供应链决定")
    p.add_argument("case_id")
    p.add_argument("--decision", required=True)
    p.add_argument("--role", choices=["quality", "product", "supply"], required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--lot")
    p.add_argument("--supplier-ref")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("action", help="推进动作状态")
    p.add_argument("action_id")
    p.add_argument("--to", required=True)
    p.add_argument("--role", choices=["quality", "product", "supply"], required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--receipt")
    p.add_argument("--note")
    p.set_defaults(func=cmd_action)

    p = sub.add_parser("receipt", help="登记回执")
    p.add_argument("--receipt-ref", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--received-at", required=True)
    p.add_argument("--lot")
    p.add_argument("--channel")
    p.add_argument("--note")
    p.add_argument("--actor", required=True)
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("close", help="关闭案件")
    p.add_argument("case_id")
    p.add_argument("--role", default="quality", choices=["quality"])
    p.add_argument("--actor", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reopen", help="重新打开案件")
    p.add_argument("case_id")
    p.add_argument("--role", default="quality", choices=["quality"])
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("check", help="记录改进核查")
    p.add_argument("case_id")
    p.add_argument("--cutoff", required=True, help="核查截止日 YYYY-MM-DD")
    p.add_argument("--anchor", help="措施生效日 YYYY-MM-DD（默认取最近完成动作）")
    p.add_argument("--pre-days", type=int)
    p.add_argument("--post-days", type=int)
    p.add_argument("--actor", default="QI-01")
    p.add_argument("--note")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("cases", help="列出案件与时间线")
    p.add_argument("--status")
    p.set_defaults(func=cmd_cases)

    p = sub.add_parser("refund", help="登记退款执行状态（追加事件）")
    p.add_argument("return_id")
    p.add_argument("--state", required=True, choices=["pending", "paid", "rejected"])
    p.add_argument("--actor", required=True)
    p.add_argument("--role", default="quality", choices=["quality", "product", "supply"])
    p.add_argument("--amount", type=int)
    p.add_argument("--note")
    p.set_defaults(func=cmd_refund)

    p = sub.add_parser("decisions", help="列出全部决定与时间")
    p.set_defaults(func=cmd_decisions)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AppError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Output was truncated by a downstream consumer (e.g. ``| head``).
        try:
            sys.stdout.close()
        finally:
            return 0
