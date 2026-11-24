"""Case workflow: grading, cross-role decisions, actions and improvement checks.

Permission and state rules
--------------------------
* The quality owner grades a case (``none``/``watch``/``investigate``/
  ``escalate``) and records the in-scope lots.  A grade can only be changed by
  the quality role and never silently: every change is a new decision row.
* Product decisions (copy, packaging) belong to the product role; supply
  decisions (hold, supplier remediation, release) belong to the supply role.
  A role cannot write another role's decision.
* Actions that affect the outside world (holds, supplier fixes, copy releases)
  need a receipt before they can be completed.  A rejected or partial receipt
  keeps the action open and is reported, not treated as success.
* A closed case can be reopened, but the original grade and decisions remain in
  the timeline.  Re-analysis never rewrites recorded decisions.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from .analysis import load_snapshot
from .config import Settings, load_settings
from .csvio import canonical_json
from .errors import NotFoundError, PermissionDenied, WorkflowError
from .reasons import ReasonResolver
from .stats import decompose_rate_change, rate_ratio_ci
from .timeutil import iso, parse_date, utc_now

ROLE_QUALITY = "quality"
ROLE_PRODUCT = "product"
ROLE_SUPPLY = "supply"

CASE_OPEN = "open"
CASE_INVESTIGATING = "investigating"
CASE_CLOSED = "closed"
CASE_REOPENED = "reopened"
ACTIVE_CASE_STATES = (CASE_OPEN, CASE_INVESTIGATING, CASE_REOPENED)

ACTION_LABELS = {
    "request-investigation": "要求供应商调查",
    "close-case": "关闭案件",
    "monitor": "持续观察",
    "revise-copy": "更新商品说明",
    "change-packaging": "更新包装",
    "no-change": "维持现状",
    "hold-shipment": "暂停未发货批次",
    "supplier-fix": "要求供应商整改",
    "release-hold": "解除暂停发货",
    "no-action": "不采取供应链措施",
}

GRADE_LABELS = {"none": "无需处理", "watch": "观察", "investigate": "调查", "escalate": "升级"}


def _settings(settings: Settings | None) -> Settings:
    return settings or load_settings()


def _action_category(settings: Settings, action_type: str) -> str:
    mapping = settings["decisions"].get("action_categories", {})
    return mapping.get(action_type, "other")


def _role_for_action(settings: Settings, action_type: str) -> str:
    if action_type in settings["decisions"]["quality_actions"]:
        return ROLE_QUALITY
    if action_type in settings["decisions"]["product_actions"]:
        return ROLE_PRODUCT
    if action_type in settings["decisions"]["supply_actions"]:
        return ROLE_SUPPLY
    raise WorkflowError(f"未知动作类型: {action_type}")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _case_row(conn, case_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM case_record WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"案件不存在: {case_id}")
    return dict(row)


def find_case(conn, level: str, entity_id: str, *, include_closed: bool = False) -> dict[str, Any] | None:
    sql = "SELECT * FROM case_record WHERE level=? AND entity_id=?"
    if not include_closed:
        sql += " AND status IN ('open','investigating','reopened')"
    sql += " ORDER BY opened_at DESC, case_id DESC LIMIT 1"
    row = conn.execute(sql, (level, entity_id)).fetchone()
    return dict(row) if row else None


def open_case(
    conn,
    level: str,
    entity_id: str,
    *,
    snapshot_id: int | None = None,
    actor: str,
    rationale: str,
    settings: Settings | None = None,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """Open (or return) the active case for an entity at a snapshot."""
    settings = _settings(settings)
    if level not in ("lot", "sku"):
        raise WorkflowError("level 必须是 lot 或 sku")
    if not actor:
        raise WorkflowError("必须记录操作人")
    snapshot_id = snapshot_id or _latest_snapshot(conn)
    existing = find_case(conn, level, entity_id)
    if existing:
        return existing
    snapshot = load_snapshot(conn, snapshot_id)
    result = next(
        (r for r in snapshot["results"] if r["level"] == level and r["entity_id"] == entity_id),
        None,
    )
    if result is None:
        raise NotFoundError(f"快照 {snapshot_id} 中没有 {level}:{entity_id}")
    if not rationale.strip():
        raise WorkflowError("开案必须说明理由")
    now = utc_now()
    case_id = _next_case_id(conn)
    resolved_scope = scope if scope is not None else _default_scope(snapshot, level, entity_id)
    conn.execute(
        "INSERT INTO case_record(case_id, level, entity_id, status, proposed_level, grade, opened_at,"
        " opened_by, snapshot_id, scope_json, summary) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            case_id,
            level,
            entity_id,
            CASE_OPEN,
            result["proposed_level"],
            now,
            actor,
            snapshot_id,
            canonical_json(resolved_scope),
            rationale,
        ),
    )
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id, payload_json) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
        (case_id, actor, ROLE_QUALITY, result["proposed_level"], rationale, now, now, snapshot_id,
         canonical_json({"scope": resolved_scope})),
    )
    conn.commit()
    return _case_row(conn, case_id)


def _latest_snapshot(conn) -> int:
    row = conn.execute("SELECT MAX(snapshot_id) FROM risk_snapshot").fetchone()
    if row is None or row[0] is None:
        raise WorkflowError("尚无风险快照，请先运行 analyze")
    return int(row[0])


def _next_case_id(conn) -> str:
    row = conn.execute("SELECT COUNT(*) FROM case_record").fetchone()
    return f"CASE-{int(row[0]) + 1:04d}"


def _default_scope(snapshot: dict[str, Any], level: str, entity_id: str) -> list[str]:
    result = next(r for r in snapshot["results"] if r["level"] == level and r["entity_id"] == entity_id)
    scope = result.get("scope_json") or {}
    if level == "lot":
        return [entity_id]
    return list(scope.get("lots") or [])


def record_grade(
    conn,
    case_id: str,
    *,
    grade: str,
    actor: str,
    rationale: str,
    role: str = ROLE_QUALITY,
    scope: list[str] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = _settings(settings)
    if role != ROLE_QUALITY:
        raise PermissionDenied("只有质量负责人可以确认问题等级")
    if grade not in settings["decisions"]["levels"]:
        raise WorkflowError(f"等级必须是 {', '.join(settings['decisions']['levels'])}")
    if not rationale.strip():
        raise WorkflowError("确认等级必须写明依据")
    case = _case_row(conn, case_id)
    if case["status"] == CASE_CLOSED:
        raise WorkflowError("已关闭案件不能改等级，请先重新打开")
    if case["grade"] == grade and scope is None:
        raise WorkflowError("等级未变化")
    now = utc_now()
    if scope is not None:
        _validate_scope(conn, case["level"], case["entity_id"], scope)
        conn.execute("UPDATE case_record SET scope_json=? WHERE case_id=?", (canonical_json(scope), case_id))
    conn.execute(
        "UPDATE case_record SET grade=?, status=? WHERE case_id=?",
        (grade, CASE_INVESTIGATING, case_id),
    )
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id, payload_json) VALUES (?, ?, ?, 'grade', ?, ?, ?, ?, ?, ?)",
        (case_id, actor, role, grade, rationale, now, now, case["snapshot_id"],
         canonical_json({"scope": scope if scope is not None else json.loads(case["scope_json"])})),
    )
    conn.commit()
    return _case_row(conn, case_id)


def _validate_scope(conn, level: str, entity_id: str, scope: list[str]) -> None:
    if not isinstance(scope, list):
        raise WorkflowError("scope 必须是批次列表")
    for lot in scope:
        if level == "lot" and lot != entity_id:
            raise WorkflowError("批次级案件的范围内只能包含该批次")
        if not conn.execute("SELECT 1 FROM lots WHERE lot_id=?", (lot,)).fetchone():
            raise WorkflowError(f"范围内批次不存在: {lot}")


def record_decision(
    conn,
    case_id: str,
    *,
    decision: str,
    actor: str,
    role: str,
    rationale: str,
    lot_id: str | None = None,
    supplier_ref: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Record a product or supply decision and create the corresponding action."""
    settings = _settings(settings)
    case = _case_row(conn, case_id)
    if case["status"] == CASE_CLOSED:
        raise WorkflowError("案件已关闭，不能追加决定")
    owner_role = _role_for_action(settings, decision)
    if role != owner_role:
        raise PermissionDenied(
            f"{decision} 属于 {owner_role} 角色，当前角色 {role} 不能代为决定"
        )
    if not rationale.strip():
        raise WorkflowError("业务决定必须写明理由")
    allowed = (
        settings["decisions"]["product_actions"]
        if role == ROLE_PRODUCT
        else settings["decisions"]["quality_actions"]
        if role == ROLE_QUALITY
        else settings["decisions"]["supply_actions"]
    )
    if decision not in allowed:
        raise WorkflowError(f"{role} 不能执行动作 {decision}")
    if lot_id is not None:
        scope_lots = json.loads(case["scope_json"])
        if lot_id not in scope_lots:
            raise WorkflowError(f"批次 {lot_id} 不在案件范围内: {', '.join(scope_lots)}")
        _validate_scope(conn, case["level"], case["entity_id"], [lot_id])
    now = utc_now()
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id, payload_json) VALUES (?, ?, ?, 'action', ?, ?, ?, ?, ?, ?)",
        (case_id, actor, role, decision, rationale, now, now, case["snapshot_id"],
         canonical_json({"lot_id": lot_id, "supplier_ref": supplier_ref})),
    )
    action_id = _create_action(
        conn,
        case=case,
        action_type=decision,
        owner_role=role,
        actor=actor,
        rationale=rationale,
        lot_id=lot_id,
        supplier_ref=supplier_ref,
        settings=settings,
    )
    conn.commit()
    return {"case": _case_row(conn, case_id), "action_id": action_id}


def _next_action_id(conn) -> str:
    row = conn.execute("SELECT COUNT(*) FROM actions WHERE action_id LIKE 'ACT-%'").fetchone()
    return f"ACT-{int(row[0]) + 1:04d}"


def _create_action(
    conn,
    *,
    case: dict[str, Any],
    action_type: str,
    owner_role: str,
    actor: str,
    rationale: str,
    lot_id: str | None,
    supplier_ref: str | None,
    settings: Settings,
) -> str:
    action_id = _next_action_id(conn)
    now = utc_now()
    state = "proposed"
    conn.execute(
        "INSERT INTO actions(action_id, lot_id, sku, case_id, owner_role, action_type, decision_state,"
        " raw_state, effective_at, receipt_ref, supplier_ref, source, note, created_by, created_at,"
        " verification, flags_json, record_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'app', ?, ?, ?, 'unverified', '[]', NULL)",
        (
            action_id,
            lot_id,
            case["entity_id"] if case["level"] == "sku" else None,
            case["case_id"],
            owner_role,
            action_type,
            state,
            state,
            None,
            supplier_ref,
            rationale,
            actor,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO action_event(action_id, seq, from_state, to_state, actor, role, event_at, note)"
        " VALUES (?, 1, NULL, ?, ?, ?, ?, ?)",
        (action_id, state, actor, owner_role, now, rationale),
    )
    return action_id


def transition_action(
    conn,
    action_id: str,
    *,
    to_state: str,
    actor: str,
    role: str,
    receipt_ref: str | None = None,
    note: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Advance an action.  External actions cannot complete without a receipt."""
    settings = _settings(settings)
    row = conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"动作不存在: {action_id}")
    action = dict(row)
    if action["source"] == "import":
        raise WorkflowError("导入的历史动作只读；请新建应用内动作")
    if action["owner_role"] != role:
        raise PermissionDenied(f"动作 {action_id} 属于 {action['owner_role']}，当前角色 {role} 不能操作")
    allowed_states = settings["decisions"]["execution_states"]
    if to_state not in allowed_states:
        raise WorkflowError(f"状态必须是 {', '.join(allowed_states)}")
    transitions = {
        "proposed": {"confirmed", "in_progress", "cancelled", "superseded"},
        "confirmed": {"in_progress", "complete", "cancelled", "superseded"},
        "in_progress": {"complete", "cancelled"},
        "complete": set(),
        "cancelled": set(),
        "superseded": set(),
    }
    current = action["decision_state"]
    if to_state not in transitions.get(current, set()):
        raise WorkflowError(f"不允许从 {current} 直接变更为 {to_state}")
    if to_state == "complete" and action["action_type"] in settings["decisions"]["receipt_required_for"]:
        receipt_ref = receipt_ref or action["receipt_ref"]
        if not receipt_ref:
            raise WorkflowError(f"{ACTION_LABELS.get(action['action_type'], action['action_type'])} 完成前必须登记回执")
        receipt = conn.execute("SELECT * FROM receipts WHERE receipt_ref=?", (receipt_ref,)).fetchone()
        if receipt is None:
            raise WorkflowError(f"回执 {receipt_ref} 不存在")
        if receipt["outcome"] in ("rejected", "cancelled"):
            raise WorkflowError(f"回执 {receipt_ref} 结果为 {receipt['outcome']}，不能视为完成")
        if receipt["outcome"] == "partial":
            raise WorkflowError(f"回执 {receipt_ref} 仅为部分完成，请保持进行中")
        if receipt["lot_id"] and action["lot_id"] and receipt["lot_id"] != action["lot_id"]:
            raise WorkflowError("回执对应批次与动作批次不一致")
    now = utc_now()
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM action_event WHERE action_id=?", (action_id,)).fetchone()[0]
    conn.execute(
        "UPDATE actions SET decision_state=?, effective_at=?, receipt_ref=COALESCE(?, receipt_ref),"
        " completed_at=CASE WHEN ?='complete' THEN ? ELSE completed_at END,"
        " verification=CASE WHEN ?='complete' THEN 'verified' ELSE verification END WHERE action_id=?",
        (to_state, now, receipt_ref, to_state, now, to_state, action_id),
    )
    conn.execute(
        "INSERT INTO action_event(action_id, seq, from_state, to_state, actor, role, event_at, receipt_ref, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, seq, current, to_state, actor, role, now, receipt_ref, note),
    )
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id, payload_json) VALUES (?, ?, ?, 'action_transition', ?, ?, ?, ?, NULL, ?)",
        (action["case_id"], actor, role, f"{current}->{to_state}", note or "", now, now,
         canonical_json({"action_id": action_id, "receipt_ref": receipt_ref})),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone())


def register_receipt(
    conn,
    *,
    receipt_ref: str,
    receipt_type: str,
    outcome: str,
    received_at: str,
    lot_id: str | None = None,
    channel: str | None = None,
    note: str | None = None,
    actor: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = _settings(settings)
    if outcome not in settings["decisions"]["receipt_states"]:
        raise WorkflowError(f"回执结果必须是 {', '.join(settings['decisions']['receipt_states'])}")
    parse_date(received_at, field="received_at")
    if lot_id and not conn.execute("SELECT 1 FROM lots WHERE lot_id=?", (lot_id,)).fetchone():
        raise WorkflowError(f"批次不存在: {lot_id}")
    try:
        conn.execute(
            "INSERT INTO receipts(receipt_ref, lot_id, receipt_type, outcome, received_at, channel, note,"
            " record_hash, first_batch, last_batch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (receipt_ref, lot_id, receipt_type, outcome, received_at, channel, note, ""),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise WorkflowError(f"回执编号已存在: {receipt_ref}") from exc
        raise
    conn.commit()
    return dict(conn.execute("SELECT * FROM receipts WHERE receipt_ref=?", (receipt_ref,)).fetchone())


def close_case(
    conn,
    case_id: str,
    *,
    actor: str,
    role: str,
    outcome: str,
    rationale: str,
) -> dict[str, Any]:
    if role != ROLE_QUALITY:
        raise PermissionDenied("只有质量负责人可以关闭案件")
    case = _case_row(conn, case_id)
    if case["status"] == CASE_CLOSED:
        raise WorkflowError("案件已经关闭")
    if not rationale.strip():
        raise WorkflowError("关闭案件必须写明结论")
    open_actions = conn.execute(
        "SELECT action_id, action_type, decision_state FROM actions WHERE case_id=? AND decision_state"
        " NOT IN ('complete','cancelled','superseded') AND action_type != 'monitor'",
        (case_id,),
    ).fetchall()
    if open_actions:
        details = ", ".join(f"{row['action_id']}({row['action_type']}/{row['decision_state']})" for row in open_actions)
        raise WorkflowError(f"仍有未完成动作，不能关闭案件: {details}")
    now = utc_now()
    conn.execute("UPDATE case_record SET status=?, closed_at=?, summary=? WHERE case_id=?",
                 (CASE_CLOSED, now, outcome + "：" + rationale, case_id))
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id) VALUES (?, ?, ?, 'close', ?, ?, ?, ?, NULL)",
        (case_id, actor, role, outcome, rationale, now, now),
    )
    conn.commit()
    return _case_row(conn, case_id)


def reopen_case(conn, case_id: str, *, actor: str, role: str, rationale: str) -> dict[str, Any]:
    if role != ROLE_QUALITY:
        raise PermissionDenied("只有质量负责人可以重新打开案件")
    case = _case_row(conn, case_id)
    if case["status"] != CASE_CLOSED:
        raise WorkflowError("只有已关闭案件可以重新打开")
    if not rationale.strip():
        raise WorkflowError("重新打开必须写明理由")
    now = utc_now()
    conn.execute("UPDATE case_record SET status=?, closed_at=NULL WHERE case_id=?", (CASE_REOPENED, case_id))
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id) VALUES (?, ?, ?, 'reopen', ?, ?, ?, ?, NULL)",
        (case_id, actor, role, CASE_REOPENED, rationale, now, now),
    )
    conn.commit()
    return _case_row(conn, case_id)


def record_refund_state(
    conn,
    return_id: str,
    *,
    state: str,
    actor: str,
    role: str = "quality",
    amount_units: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Append a refund state event.

    Refunds are execution facts.  They can be recorded once when they happen
    and are never recalculated by a later re-analysis; a reclassification of
    the return reason does not rewrite the refund that was already paid.
    """
    row = conn.execute("SELECT * FROM returns WHERE return_id=?", (return_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"退货记录不存在: {return_id}")
    if state not in ("pending", "paid", "rejected"):
        raise WorkflowError("退款状态必须是 pending/paid/rejected")
    latest = conn.execute(
        "SELECT new_state FROM refund_event WHERE return_id=? ORDER BY event_id DESC LIMIT 1",
        (return_id,),
    ).fetchone()
    current = latest["new_state"] if latest else row["refund_state"]
    if current == state:
        raise WorkflowError(f"退款状态已经是 {state}")
    now = utc_now()
    conn.execute(
        "INSERT INTO refund_event(return_id, old_state, new_state, amount_units, recorded_at, actor, role, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (return_id, current, state, amount_units, now, actor, role, note),
    )
    existing = conn.execute("SELECT 1 FROM refund_record WHERE return_id=?", (return_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE refund_record SET refund_state=?, amount_units=COALESCE(?, amount_units),"
            " refunded_at=?, note=COALESCE(?, note) WHERE return_id=?",
            (state, amount_units, now, note, return_id),
        )
    else:
        conn.execute(
            "INSERT INTO refund_record(return_id, refund_state, amount_units, refunded_at, last_seen_batch, note)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (return_id, state, amount_units, now, note),
        )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM refund_event WHERE event_id=(SELECT MAX(event_id) FROM refund_event)"
        ).fetchone()
    )


def refund_events(conn, return_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM refund_event WHERE return_id=? ORDER BY event_id", (return_id,)
        )
    ]


# ---------------------------------------------------------------------------
# Improvement tracking
# ---------------------------------------------------------------------------


def _entity_entities(conn, level: str, entity_id: str) -> tuple[set[str], set[str]]:
    if level == "lot":
        rows = conn.execute(
            "SELECT DISTINCT sku, lot_id FROM orders WHERE lot_id=?", (entity_id,)
        ).fetchall()
        return {row["lot_id"] for row in rows}, {row["sku"] for row in rows}
    lots = {row["lot_id"] for row in conn.execute("SELECT DISTINCT lot_id FROM orders WHERE sku=?", (entity_id,))}
    return lots, {entity_id}


def _effective_quality_counts(
    conn,
    lot_ids: set[str],
    start: date,
    end: date,
    settings: Settings,
    cutoff: date,
) -> tuple[int, int, int, int, dict[str, tuple[float, float]]]:
    """Return ``(exposure, quality, unknown, immature)`` for orders sold in window.

    Exposure is the units sold in ``[start, end]`` whose observation horizon
    ends on or before ``cutoff``.  Events are returns linked to those same
    orders (received on or before ``cutoff``), which keeps numerator and
    denominator on the same cohort.
    """
    resolver = ReasonResolver(conn)
    policy_windows = {str(k): int(v) for k, v in settings.get("policy_windows_days", {}).items()}
    default_window = int(settings.get("default_window_days", 30))
    buffer_days = int(settings.get("maturity_buffer_days", 0))
    quality_categories = settings.quality_categories
    unknown_categories = settings.unknown_categories
    order_units: dict[str, int] = {}
    stratum_of: dict[str, str] = {}
    exposure = 0
    immature = 0
    for order in conn.execute("SELECT * FROM orders"):
        order = dict(order)
        if order["lot_id"] not in lot_ids:
            continue
        sold = parse_date(order["sold_at"], field="sold_at")
        if not (start <= sold <= end):
            continue
        horizon = policy_windows.get(str(order["policy_version"]), default_window) + buffer_days
        units = int(order["units"])
        if (cutoff - sold).days < horizon:
            immature += units
            continue
        exposure += units
        order_units[order["order_id"]] = units
        stratum_of[order["order_id"]] = f"{order['channel']}/{order['policy_version']}"
    quality = 0
    unknown = 0
    strata: dict[str, list[float]] = {}
    for row in conn.execute("SELECT * FROM returns"):
        row = dict(row)
        if row["order_id"] not in order_units:
            continue
        received = parse_date(row["received_at"], field="received_at")
        if received > cutoff:
            continue
        code, _, revision_at = resolver.effective_code(row["return_id"], row["reason_code"], cutoff)
        classification = resolver.classify(code, revision_at or received)
        units = min(int(row["returned_units"]), order_units[row["order_id"]])
        if classification.category in quality_categories:
            quality += units
            bucket = strata.setdefault(stratum_of[row["order_id"]], [0.0, 0.0])
            bucket[1] += units
        elif classification.category in unknown_categories:
            unknown += units
    for oid, stratum in stratum_of.items():
        strata.setdefault(stratum, [0.0, 0.0])[0] += float(order_units[oid])
    return exposure, quality, unknown, immature, {k: (v[0], v[1]) for k, v in strata.items()}


def record_improvement_check(
    conn,
    case_id: str,
    *,
    actor: str,
    cutoff: str | date,
    anchor: str | date | None = None,
    pre_days: int | None = None,
    post_days: int | None = None,
    settings: Settings | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Compare quality return rates before and after the intervention anchor."""
    settings = _settings(settings)
    case = _case_row(conn, case_id)
    cfg = settings["improvement"]
    cutoff_date = parse_date(cutoff if isinstance(cutoff, str) else iso(cutoff), field="cutoff")
    anchor_date = _resolve_anchor(conn, case, anchor)
    pre_days = int(pre_days or cfg.get("pre_days", 30))
    post_days = int(post_days or cfg.get("post_days", 30))
    if pre_days < 1 or post_days < 1:
        raise WorkflowError("pre_days/post_days 必须为正整数")
    lots, _ = _entity_entities(conn, case["level"], case["entity_id"])
    if not lots:
        raise WorkflowError("案件范围内没有可追溯批次，无法进行改进核查")
    pre_from = anchor_date - timedelta(days=pre_days)
    pre_to = anchor_date - timedelta(days=1)
    post_from = anchor_date
    post_to = min(cutoff_date, anchor_date + timedelta(days=post_days))
    if post_to < post_from:
        raise WorkflowError("核查截止日早于措施生效日")
    pre_exposure, pre_events, pre_unknown, pre_immature, pre_strata = _effective_quality_counts(
        conn, lots, pre_from, pre_to, settings, cutoff_date
    )
    post_exposure, post_events, post_unknown, post_immature, post_strata = _effective_quality_counts(
        conn, lots, post_from, post_to, settings, cutoff_date
    )
    mix: dict[str, float] = {}
    if pre_strata and post_strata:
        mix = decompose_rate_change(pre_strata, post_strata)
    pre_rate = (pre_events / pre_exposure) if pre_exposure else 0.0
    post_rate = (post_events / post_exposure) if post_exposure else 0.0
    min_post = int(cfg.get("min_post_exposure_units", 20))
    min_pre_events = int(cfg.get("min_pre_events", 2))
    alpha = float(cfg.get("alpha", 0.10))
    verdict = "inconclusive"
    ratio = lo = hi = None
    p_value = None
    if post_immature > 0:
        verdict = "immature_exposure"
    elif post_exposure < min_post:
        verdict = "insufficient_data"
    elif pre_events < min_pre_events:
        verdict = "insufficient_data"
    else:
        ratio, lo, hi, p_value = rate_ratio_ci(post_events, post_exposure, pre_events, pre_exposure, alpha=alpha)
        if lo > 1.0 and p_value <= alpha:
            verdict = "worse"
        elif hi < 1.0 and p_value <= alpha:
            verdict = "improved"
        else:
            verdict = "not_conclusive"
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO improvement_check(case_id, level, entity_id, metric, cutoff_at, checked_at, pre_from,"
        " pre_to, post_from, post_to, pre_exposure, pre_events, post_exposure, post_events, pre_rate,"
        " post_rate, rate_ratio, rate_ratio_lo, rate_ratio_hi, p_value, verdict, method, note, snapshot_id,"
        " mix_json)"
        " VALUES (?, ?, ?, 'quality_returns', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case_id,
            case["level"],
            case["entity_id"],
            cutoff_date.isoformat(),
            now,
            pre_from.isoformat(),
            pre_to.isoformat(),
            post_from.isoformat(),
            post_to.isoformat(),
            pre_exposure,
            pre_events,
            post_exposure,
            post_events,
            pre_rate,
            post_rate,
            ratio,
            lo,
            hi,
            p_value,
            verdict,
            "exact_binomial_rate_ratio",
            note,
            case["snapshot_id"],
            canonical_json(mix),
        ),
    )
    conn.execute(
        "INSERT INTO case_decision(case_id, actor, role, decision_type, value, rationale, decided_at,"
        " effective_at, snapshot_id, payload_json) VALUES (?, ?, 'quality', 'improvement_check', ?, ?, ?, ?, ?, ?)",
        (case_id, actor, verdict, note or "", now, now, case["snapshot_id"],
         canonical_json({"check_id": int(cur.lastrowid), "anchor": anchor_date.isoformat(),
                         "pre_events": pre_events, "post_events": post_events})),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM improvement_check WHERE check_id=?", (cur.lastrowid,)).fetchone())


def _resolve_anchor(conn, case: dict[str, Any], anchor: str | date | None) -> date:
    if anchor is not None:
        return parse_date(anchor if isinstance(anchor, str) else iso(anchor), field="anchor")
    row = conn.execute(
        "SELECT MAX(COALESCE(effective_at, created_at)) FROM actions WHERE case_id=? AND decision_state='complete'",
        (case["case_id"],),
    ).fetchone()
    if row is None or row[0] is None:
        raise WorkflowError("找不到已完成的整改动作，请显式提供 anchor 生效日期")
    raw = row[0]
    return parse_date(raw[:10], field="anchor")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_cases(conn, *, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM case_record"
    params: tuple = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY opened_at DESC, case_id DESC"
    cases = [dict(row) for row in conn.execute(sql, params)]
    for case in cases:
        case["actions"] = [
            dict(row) for row in conn.execute(
                "SELECT action_id, action_type, decision_state, owner_role, receipt_ref, lot_id"
                " FROM actions WHERE case_id=? ORDER BY action_id",
                (case["case_id"],),
            )
        ]
        case["scope"] = json.loads(case["scope_json"])
    return cases


def case_timeline(conn, case_id: str) -> list[dict[str, Any]]:
    _case_row(conn, case_id)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM case_decision WHERE case_id=? ORDER BY decided_at, decision_id", (case_id,)
        )
    ]


def case_actions(conn, case_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM actions WHERE case_id=? ORDER BY action_id", (case_id,))
    ]


def case_improvement_checks(conn, case_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM improvement_check WHERE case_id=? ORDER BY checked_at, check_id", (case_id,)
        )
    ]
