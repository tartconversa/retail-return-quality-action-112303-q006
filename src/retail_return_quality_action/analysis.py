"""Exposure-adjusted quality signal analysis.

Method summary
--------------
* **Cohort / maturity.**  Only orders sold at least ``min_maturity_days`` before
  the analysis cut-off are counted as exposure; younger orders are reported as
  ``immature`` and excluded from both numerator and denominator.  This prevents
  a sales ramp from mechanically lowering the observed return rate.
* **Stratification.**  Every entity is compared with a background built from
  the same ``channel × policy_version`` cells (indirect standardisation).  The
  background rate for a cell excludes the entity being scored and is smoothed
  with a configurable prior, so small cells cannot dominate.  A mix
  decomposition separates "more exposure in a high-return policy" from a
  genuine within-cell rate change.
* **Uncertainty.**  One-sided Poisson exceedance p-values, Benjamini-Hochberg
  q-values across entities of the same level, exact Poisson CIs for crude
  rates.  A worst-case rate adds unresolved ("unknown") returns to the
  numerator so that missing codes are never treated as zero risk.
* **Evidence.**  Inspection findings only count when confirmed; pending or
  disputed major/critical findings are *blocking* and prevent an escalation.
  Customer-service summaries corroborate or contradict the coded reason.
* **Repeat problems.**  A finding code confirmed in two or more lots of a SKU,
  the same code confirmed twice for one lot, or quality returns on two or more
  lots of a SKU are all treated as repetition - the actual trigger for
  supplier investigation rather than a single-batch anomaly.
* **Traceability.**  Placeholder or missing lot IDs, and lots whose own
  traceability state is partial/unknown, are reported as an explicit gap.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .config import Settings
from .csvio import canonical_json
from .errors import DataError
from .reasons import ReasonResolver
from .stats import bh_qvalues, chi2_sf, decompose_entity_mix, poisson_rate_ci, poisson_sf
from .timeutil import iso, parse_date, utc_now

UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class InspectionEvidence:
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    confirmed_weight: int = 0
    pending_weight: int = 0
    max_confirmed_severity: str | None = None
    repeat_codes: dict[str, list[str]] = field(default_factory=dict)
    repeat_within_lot: dict[str, list[str]] = field(default_factory=dict)
    blocking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "pending": self.pending,
            "rejected_count": len(self.rejected),
            "confirmed_weight": self.confirmed_weight,
            "pending_weight": self.pending_weight,
            "max_confirmed_severity": self.max_confirmed_severity,
            "repeat_codes": self.repeat_codes,
            "repeat_within_lot": self.repeat_within_lot,
            "blocking": self.blocking,
        }


@dataclass
class ServiceEvidence:
    confirmed: int = 0
    pending: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    corroborating: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "pending": self.pending,
            "conflicts": self.conflicts,
            "corroborating": self.corroborating,
        }


@dataclass
class EntityRisk:
    level: str
    entity_id: str
    exposure_units: int
    immature_units: int
    quality_units: int
    unknown_units: int
    information_units: int
    fit_units: int
    other_units: int
    late_return_units: int
    expected_units: float
    smr: float | None
    p_value: float
    q_value: float = 1.0
    crude_rate: float = 0.0
    crude_lo: float = 0.0
    crude_hi: float = 0.0
    unknown_share: float = 0.0
    worst_case_rate: float = 0.0
    traceability_gap_units: int = 0
    traceability_gap_share: float = 0.0
    info_expected: float = 0.0
    info_smr: float | None = None
    info_p_value: float = 1.0
    info_q_value: float = 1.0
    worst_case_p_value: float = 1.0
    worst_case_q_value: float = 1.0
    evidence: InspectionEvidence = field(default_factory=InspectionEvidence)
    service: ServiceEvidence = field(default_factory=ServiceEvidence)
    repeat: dict[str, Any] = field(default_factory=dict)
    mix: dict[str, float] = field(default_factory=dict)
    reasons: list[dict[str, str]] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    proposed_level: str = "none"
    exposure_days_mean: float = 0.0

    @property
    def evidence_confirmed(self) -> int:
        return self.evidence.confirmed_weight

    @property
    def evidence_pending(self) -> int:
        return self.evidence.pending_weight

    def add_reason(self, code: str, text: str) -> None:
        self.reasons.append({"code": code, "text": text})

    def as_row(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "entity_id": self.entity_id,
            "proposed_level": self.proposed_level,
            "exposure_units": self.exposure_units,
            "immature_units": self.immature_units,
            "quality_units": self.quality_units,
            "unknown_units": self.unknown_units,
            "information_units": self.information_units,
            "fit_units": self.fit_units,
            "other_units": self.other_units,
            "late_return_units": self.late_return_units,
            "expected_units": round(self.expected_units, 6),
            "smr": None if self.smr is None else round(self.smr, 6),
            "p_value": self.p_value,
            "q_value": self.q_value,
            "crude_rate": self.crude_rate,
            "crude_lo": self.crude_lo,
            "crude_hi": self.crude_hi,
            "unknown_share": self.unknown_share,
            "worst_case_rate": self.worst_case_rate,
            "traceability_gap_units": self.traceability_gap_units,
            "traceability_gap_share": self.traceability_gap_share,
            "info_expected": self.info_expected,
            "info_smr": self.info_smr,
            "info_p_value": self.info_p_value,
            "info_q_value": self.info_q_value,
            "evidence_confirmed": self.evidence_confirmed,
            "evidence_pending": self.evidence_pending,
            "evidence_blocking": int(self.evidence.blocking),
            "repeat_json": self.repeat,
            "mix_json": self.mix,
            "reasons_json": self.reasons,
            "scope_json": self.scope,
            "worst_case_p_value": self.worst_case_p_value,
            "worst_case_q_value": self.worst_case_q_value,
            "evidence_json": self.evidence.as_dict(),
            "service_json": self.service.as_dict(),
        }


@dataclass
class AnalysisResult:
    snapshot_id: int
    as_of: date
    config_digest: str
    data_digest: str
    entities: dict[str, list[EntityRisk]] = field(default_factory=dict)
    strata: list[dict[str, Any]] = field(default_factory=list)
    data_quality: dict[str, Any] = field(default_factory=dict)
    overall: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def get(self, level: str, entity_id: str) -> EntityRisk:
        for entity in self.entities.get(level, []):
            if entity.entity_id == entity_id:
                return entity
        raise DataError(f"分析结果中不存在 {level}:{entity_id}")


# ---------------------------------------------------------------------------
# Loading and effective facts
# ---------------------------------------------------------------------------


@dataclass
class EffectiveReturn:
    return_id: str
    order_id: str
    sku: str
    lot_id: str
    channel: str
    policy_version: str
    units: int
    raw_units: int
    sold_at: date
    received_at: date
    age_days: int
    code: str
    original_code: str
    review_id: str | None
    category: str
    classification_note: str
    reason_code_known: bool
    refund_state: str
    review_state: str
    late: bool
    over_order: bool
    order_exists: bool = True
    after_cutoff: bool = False


def _refund_state(conn, row: EffectiveReturn) -> str:
    """Current refund state: imported baseline plus any recorded executions."""
    record = conn.execute(
        "SELECT refund_state FROM refund_record WHERE return_id=?", (row.return_id,)
    ).fetchone()
    return record["refund_state"] if record else row.refund_state


def _data_digest(conn) -> str:
    tables = {
        "products": "SELECT * FROM products ORDER BY sku",
        "lots": "SELECT * FROM lots ORDER BY lot_id",
        "orders": "SELECT * FROM orders ORDER BY order_id",
        "returns": "SELECT * FROM returns ORDER BY return_id",
        "reason_map": "SELECT * FROM reason_map ORDER BY reason_code, mapping_version",
        "reason_alias": "SELECT * FROM reason_alias ORDER BY alias_code, scheme_version",
        "return_revision": "SELECT * FROM return_revision ORDER BY return_id, seq",
        "inspections": "SELECT * FROM inspections ORDER BY inspection_id",
        "inspection_review": "SELECT * FROM inspection_review ORDER BY inspection_id, seq",
        "customer_service": "SELECT * FROM customer_service ORDER BY summary_id",
        "copy_versions": "SELECT * FROM copy_versions ORDER BY copy_version_id",
        "actions": "SELECT action_id, lot_id, sku, owner_role, action_type, decision_state, effective_at,"
        " receipt_ref, supplier_ref, supersedes_action_id, verification FROM actions ORDER BY action_id",
        "refund_record": "SELECT * FROM refund_record ORDER BY return_id",
    }
    payload: dict[str, Any] = {}
    for name, sql in tables.items():
        payload[name] = [dict(row) for row in conn.execute(sql)]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _load_settings(settings: Settings | None) -> Settings:
    from .config import load_settings

    return settings or load_settings()


def _resolve_as_of(conn, as_of: str | date | None) -> date:
    if as_of is not None:
        return parse_date(as_of if isinstance(as_of, str) else iso(as_of), field="as_of")
    row = conn.execute("SELECT MAX(received_at) FROM returns").fetchone()
    if row is None or row[0] is None:
        raise DataError("资料中没有退货记录，无法推断分析日期，请显式提供 --as-of")
    return parse_date(row[0], field="as_of")


def _effective_returns(conn, resolver: ReasonResolver, as_of: date, settings: Settings) -> list[EffectiveReturn]:
    orders = {row["order_id"]: dict(row) for row in conn.execute("SELECT * FROM orders")}
    returns = [dict(row) for row in conn.execute("SELECT * FROM returns ORDER BY return_id")]
    late_days = int(settings.get("late_return_days", 30))
    result: list[EffectiveReturn] = []
    for row in returns:
        order = orders.get(row["order_id"])
        if order is None:
            order = {"order_id": row["order_id"], "sku": UNKNOWN, "lot_id": "", "channel": UNKNOWN,
                     "policy_version": UNKNOWN, "sold_at": None, "units": 0}
        sold_at = parse_date(order["sold_at"], field="sold_at") if order.get("sold_at") else None
        received_at = parse_date(row["received_at"], field="received_at")
        order_units = int(order.get("units") or 0)
        raw_units = int(row["returned_units"])
        units = min(raw_units, order_units) if order_units > 0 else raw_units
        over_order = order_units > 0 and raw_units > order_units
        age = (received_at - sold_at).days if sold_at else -1
        code, review_id, revision_at = resolver.effective_code(row["return_id"], row["reason_code"], as_of)
        # Classify by the versioned catalogue valid when the effective code was
        # assigned: revision date for revised returns, receipt date otherwise.
        classification = resolver.classify(code, revision_at or received_at)
        reason_known = classification.mapping_version is not None
        result.append(
            EffectiveReturn(
                return_id=row["return_id"],
                order_id=row["order_id"],
                sku=order.get("sku") or UNKNOWN,
                lot_id=order.get("lot_id") or "",
                channel=order.get("channel") or UNKNOWN,
                policy_version=order.get("policy_version") or UNKNOWN,
                units=units,
                raw_units=raw_units,
                sold_at=sold_at or received_at,
                received_at=received_at,
                age_days=age,
                code=code,
                original_code=row["reason_code"],
                review_id=review_id,
                category=classification.category,
                classification_note=classification.note,
                reason_code_known=reason_known,
                refund_state=row["refund_state"],
                review_state=row["review_state"],
                late=age > late_days,
                over_order=over_order,
                order_exists=order.get("sold_at") is not None,
                after_cutoff=received_at > as_of,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Exposure, strata and evidence
# ---------------------------------------------------------------------------


class _Cohort:
    def __init__(self, settings: Settings, as_of: date) -> None:
        self.settings = settings
        self.as_of = as_of
        self.default_window = int(settings.get("default_window_days", 30))
        self.buffer_days = int(settings.get("maturity_buffer_days", 0))
        self.policy_windows = {str(k): int(v) for k, v in settings.get("policy_windows_days", {}).items()}
        self.quality_categories = settings.quality_categories
        self.unknown_categories = settings.unknown_categories
        self.severity_weights = settings.severity_weights
        self.placeholder = set(settings.get("traceability", {}).get("placeholder_ids", []))
        self.unknown_states = set(settings.get("traceability", {}).get("unknown_states", []))
        self.min_unknown_share = float(settings.get("traceability", {}).get("min_unknown_share_watch", 0.3))
        self.min_trace_share = float(settings.get("traceability", {}).get("min_untraced_share_watch", 0.2))

    def maturity_days(self, policy_version: str | None) -> int:
        """Observation horizon for a policy version, including the buffer."""
        window = self.policy_windows.get(str(policy_version), self.default_window)
        return window + self.buffer_days

    def is_mature(self, sold_at: date, policy_version: str | None = None) -> bool:
        return (self.as_of - sold_at).days >= self.maturity_days(policy_version)

    def stratum(self, channel: str, policy: str) -> str:
        return f"{channel}/{policy}"

    def traceability_gap(self, lot_id: str, lots: dict[str, dict]) -> bool:
        if not lot_id or lot_id in self.placeholder:
            return True
        meta = lots.get(lot_id)
        if meta is None:
            return True
        return meta.get("traceability_state") in self.unknown_states

    def category_bucket(self, category: str) -> str:
        if category in self.quality_categories:
            return "quality"
        if category in self.unknown_categories:
            return "unknown"
        if category == "information":
            return "information"
        if category == "fit":
            return "fit"
        return "other"


def _inspection_evidence(conn, cohort: _Cohort) -> dict[str, InspectionEvidence]:
    """Build evidence per lot, including cross-lot repetition within a SKU."""
    inspections = [dict(row) for row in conn.execute("SELECT * FROM inspections ORDER BY inspected_at, inspection_id")]
    reviews: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT * FROM inspection_review ORDER BY inspection_id, seq"):
        reviews.setdefault(row["inspection_id"], []).append(dict(row))
    effective: list[dict[str, Any]] = []
    for row in inspections:
        if row["inspected_at"] > cohort.as_of.isoformat():
            continue
        state = row["review_state"]
        applied = None
        for review in reviews.get(row["inspection_id"], []):
            if review["reviewed_at"] <= cohort.as_of.isoformat():
                state = review["review_state"]
                applied = review
        effective.append({**row, "effective_state": state, "review": applied})

    # Per-lot code counts (confirmed only).  Repetition is scoped to the SKU:
    # the same finding in two lots only counts when those lots actually share a
    # product, otherwise identical generic codes would create false repeats.
    sku_lots: dict[str, set[str]] = {}
    lot_skus: dict[str, set[str]] = {}
    for order in conn.execute("SELECT DISTINCT sku, lot_id FROM orders"):
        if order["lot_id"]:
            sku_lots.setdefault(order["sku"], set()).add(order["lot_id"])
            lot_skus.setdefault(order["lot_id"], set()).add(order["sku"])

    per_lot: dict[str, InspectionEvidence] = {}
    confirmed: list[dict[str, Any]] = []
    for row in effective:
        lot = row["lot_id"]
        ev = per_lot.setdefault(lot, InspectionEvidence())
        weight = cohort.severity_weights.get(row["severity"], 1)
        item = {
            "inspection_id": row["inspection_id"],
            "finding_code": row["finding_code"],
            "severity": row["severity"],
            "sample_size": row["sample_size"],
            "inspected_at": row["inspected_at"],
            "state": row["effective_state"],
            "review_rationale": (row["review"] or {}).get("rationale"),
        }
        if row["effective_state"] == "confirmed":
            ev.confirmed.append(item)
            ev.confirmed_weight += weight
            confirmed.append({**row, "weight": weight})
            current = ev.max_confirmed_severity
            order = {"minor": 1, "major": 2, "critical": 3}
            if current is None or order[row["severity"]] > order[current]:
                ev.max_confirmed_severity = row["severity"]
        elif row["effective_state"] in ("pending", "disputed"):
            ev.pending.append(item)
            ev.pending_weight += weight
            if weight >= cohort.severity_weights.get("major", 2):
                ev.blocking = True
        else:
            ev.rejected.append(item)

    # Repetition within a lot.
    for lot, ev in per_lot.items():
        counts: dict[str, int] = {}
        for row in confirmed:
            if row["lot_id"] == lot:
                counts[row["finding_code"]] = counts.get(row["finding_code"], 0) + 1
        ev.repeat_within_lot = {code: [lot] for code, n in counts.items() if n >= 2}

    # Repetition across the lots of a SKU.
    for sku, lots in sku_lots.items():
        for code in {row["finding_code"] for row in confirmed if row["lot_id"] in lots}:
            code_lots = sorted({row["lot_id"] for row in confirmed if row["lot_id"] in lots and row["finding_code"] == code})
            if len(code_lots) >= 2:
                for lot in code_lots:
                    ev = per_lot.setdefault(lot, InspectionEvidence())
                    ev.repeat_codes[code] = [other for other in code_lots if other != lot]
    return per_lot


def _service_evidence(conn, cohort: _Cohort) -> tuple[dict[str, ServiceEvidence], dict[str, ServiceEvidence]]:
    """Corroboration/conflict between service narratives and coded reasons."""
    rows = [dict(row) for row in conn.execute("SELECT * FROM customer_service ORDER BY received_at")]
    returns = {row["return_id"]: dict(row) for row in conn.execute("SELECT * FROM returns")}
    orders = {row["order_id"]: dict(row) for row in conn.execute("SELECT * FROM orders")}
    resolver = ReasonResolver(conn)
    topic_category = {"quality": "quality", "information": "information", "fit": "fit", "other": "unknown"}
    damage_words = ("破损", "开裂", "损坏", "缺陷", "划痕", "破洞", "开线", "变形", "拉链")
    wording_words = ("说明", "描述", "尺码表", "颜色与", "页面")
    lot_ev: dict[str, ServiceEvidence] = {}
    sku_ev: dict[str, ServiceEvidence] = {}
    for row in rows:
        if row["received_at"] > cohort.as_of.isoformat():
            continue
        order = orders.get(row["order_id"] or "")
        if order is None:
            continue
        linked = returns.get(row["linked_return_id"] or "")
        topic_bucket = topic_category.get(row["topic"], "other")
        conflict = False
        if linked is not None:
            classification = resolver.classify(linked["reason_code"], parse_date(linked["received_at"]))
            coded = cohort.category_bucket(classification.category)
            text = row["summary_text"]
            if coded == "quality" and topic_bucket == "quality":
                pass
            elif coded != "quality" and any(word in text for word in damage_words):
                conflict = True
            elif coded == "quality" and any(word in text for word in wording_words):
                conflict = True
            elif coded != topic_bucket and topic_bucket != "unknown":
                conflict = True
        for target in (lot_ev.setdefault(order.get("lot_id") or "", ServiceEvidence()),
                       sku_ev.setdefault(order["sku"], ServiceEvidence())):
            if row["review_state"] == "confirmed":
                target.confirmed += 1
                if conflict:
                    target.conflicts.append(
                        {"summary_id": row["summary_id"], "note": "已确认客服摘要与退货编码类别不一致"}
                    )
                elif topic_bucket == "quality":
                    target.corroborating.append(row["summary_id"])
            elif row["review_state"] == "pending":
                target.pending += 1
                if conflict:
                    target.conflicts.append(
                        {"summary_id": row["summary_id"], "note": "待审客服描述提示与编码不一致"}
                    )
    return lot_ev, sku_ev


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------


def _score(
    entities: Iterable[str],
    exposure: dict[str, dict[str, int]],
    counts: dict[str, dict[str, dict[str, int]]],
    background_exposure: dict[str, int],
    background_counts: dict[str, dict[str, int]],
    settings: Settings,
    *,
    level: str,
    immature: dict[str, int] | None = None,
    trace_gap: dict[str, int] | None = None,
    late: dict[str, int] | None = None,
) -> list[EntityRisk]:
    smoothing = float(settings.get("expected_smoothing", 0.5))
    total_n = sum(background_exposure.values())
    total_k = sum(background_counts.get("quality", {}).values()) or 0
    pooled_rate = (total_k / total_n) if total_n else 0.0
    prior_rate = pooled_rate if pooled_rate > 0 else 1.0 / max(total_n, 1)
    results: list[EntityRisk] = []
    for entity in sorted(entities):
        strata = exposure.get(entity, {})
        n = sum(strata.values())
        sc = counts.get(entity, {})
        k = sum(sc.get("quality", {}).values())
        u = sum(sc.get("unknown", {}).values())
        info = sum(sc.get("information", {}).values())
        fit = sum(sc.get("fit", {}).values())
        other = sum(sc.get("other", {}).values())
        expected = 0.0
        expected_unknown = 0.0
        expected_info = 0.0
        for stratum, nls in strata.items():
            if nls <= 0:
                continue
            bg_n = background_exposure.get(stratum, 0) - nls
            bg_k = background_counts.get("quality", {}).get(stratum, 0) - sc.get("quality", {}).get(stratum, 0)
            bg_u = background_counts.get("unknown", {}).get(stratum, 0) - sc.get("unknown", {}).get(stratum, 0)
            bg_i = background_counts.get("information", {}).get(stratum, 0) - sc.get("information", {}).get(stratum, 0)
            prior_n = smoothing / prior_rate
            expected += nls * (bg_k + smoothing) / (bg_n + prior_n)
            expected_unknown += nls * (bg_k + bg_u + smoothing) / (bg_n + prior_n)
            expected_info += nls * (bg_i + smoothing) / (bg_n + prior_n)
        p_value = poisson_sf(k, expected) if expected > 0 else (0.0 if k > 0 else 1.0)
        worst_p = poisson_sf(k + u, expected_unknown) if expected_unknown > 0 else (0.0 if (k + u) > 0 else 1.0)
        if n > 0:
            crude, lo, hi = poisson_rate_ci(k, n)
        else:
            crude, lo, hi = 0.0, 0.0, 0.0
        smr = (k / expected) if expected > 0 else None
        info_smr = (info / expected_info) if expected_info > 0 else None
        info_p = poisson_sf(info, expected_info) if expected_info > 0 else (0.0 if info > 0 else 1.0)
        unknown_share = (u / (k + u)) if (k + u) > 0 else 0.0
        gap_units = (trace_gap or {}).get(entity, 0)
        risk = EntityRisk(
            level=level,
            entity_id=entity,
            exposure_units=n,
            immature_units=(immature or {}).get(entity, 0),
            quality_units=k,
            unknown_units=u,
            information_units=info,
            fit_units=fit,
            other_units=other,
            late_return_units=(late or {}).get(entity, 0),
            expected_units=expected,
            smr=smr,
            p_value=p_value,
            crude_rate=crude,
            crude_lo=lo,
            crude_hi=hi,
            unknown_share=unknown_share,
            worst_case_rate=((k + u) / n) if n else 0.0,
            traceability_gap_units=gap_units,
            traceability_gap_share=(gap_units / n) if n else 0.0,
            info_expected=expected_info,
            info_smr=info_smr,
            info_p_value=info_p,
            worst_case_p_value=worst_p,
        )
        results.append(risk)
    q = bh_qvalues([r.p_value for r in results])
    q_worst = bh_qvalues([r.worst_case_p_value for r in results])
    q_info = bh_qvalues([r.info_p_value for r in results])
    for risk, qv, qw, qi in zip(results, q, q_worst, q_info):
        risk.q_value = qv
        risk.worst_case_q_value = qw
        risk.info_q_value = qi
    return results


def _mix(entity: str, exposure: dict[str, dict[str, int]], counts: dict[str, dict[str, dict[str, int]]], background_exposure: dict[str, int], background_counts: dict[str, dict[str, int]]) -> dict[str, float]:
    entity_rows = {
        stratum: (float(n), float(counts.get(entity, {}).get("quality", {}).get(stratum, 0)))
        for stratum, n in exposure.get(entity, {}).items()
    }
    background_rows = {
        stratum: (float(n), float(background_counts.get("quality", {}).get(stratum, 0)))
        for stratum, n in background_exposure.items()
        if n > 0
    }
    if not entity_rows or not background_rows:
        return {}
    return decompose_entity_mix(entity_rows, background_rows)


def _repeat_summary(
    evidence: InspectionEvidence,
    return_lots: set[str],
    sku_lots: set[str],
    settings: Settings,
) -> dict[str, Any]:
    """Summarise repetition evidence and the lots it touches."""
    min_lots = int(settings.get("repeat", {}).get("min_lots", 2))
    finding_lots = {lot for ids in evidence.repeat_codes.values() for lot in ids}
    return {
        "finding_repeat_codes": evidence.repeat_codes,
        "within_lot_repeat_codes": evidence.repeat_within_lot,
        "quality_return_lots": sorted(return_lots),
        "sku_lots": sorted(sku_lots),
        "repeat_flag": bool(evidence.repeat_codes or evidence.repeat_within_lot or len(return_lots) >= min_lots),
        "return_repeat": len(return_lots) >= min_lots,
        "affected_lots": sorted(finding_lots | return_lots),
    }


def _grade(risk: EntityRisk, settings: Settings) -> None:
    """Assign a proposed investigation level from statistics + evidence.

    The ordering matters.  Strong statistics alone are not enough: an
    escalation additionally requires confirmed, repeated evidence and must not
    be blocked by unresolved major/critical findings.  Conversely, confirmed
    repetition can justify an investigation even when the return count is too
    small to reach significance - that is the "repeated problem" the quality
    team acts on.  Suggestions never overwrite a human decision; they are
    stored as ``proposed_level`` on the snapshot.
    """
    sig = settings["significance"]
    repeat_cfg = settings.get("repeat", {})
    trace_cfg = settings.get("traceability", {})
    fdr_q = float(sig.get("fdr_q", 0.10))
    watch_p = float(sig.get("watch_p", 0.20))
    escalate_q = float(sig.get("escalate_q", 0.05))
    smr_investigate = float(sig.get("smr_investigate", 1.5))
    smr_escalate = float(sig.get("smr_escalate", 2.0))
    smr_watch = float(sig.get("smr_watch", 1.5))
    min_events = int(sig.get("min_events", 2))
    min_weight = int(repeat_cfg.get("min_severity_weight", 3))
    unknown_share_cut = float(trace_cfg.get("min_unknown_share_watch", 0.30))
    trace_share_cut = float(trace_cfg.get("min_untraced_share_watch", 0.20))

    if risk.exposure_units <= 0:
        # Exposure that is still maturing is unknown, not clean: flag it for a
        # later review rather than silently marking the entity as fine.
        if risk.immature_units > 0:
            risk.proposed_level = "watch"
            risk.add_reason("exposure_immature", f"{risk.immature_units} 件销售尚未满观察窗口，暂不能评级")
        else:
            risk.proposed_level = "none"
            risk.add_reason("no_exposure", "没有成熟销售暴露，暂不评级")
        return

    smr = risk.smr or 0.0
    quality_events = risk.quality_units + risk.unknown_units
    evidence_weight = risk.evidence.confirmed_weight
    max_sev = risk.evidence.max_confirmed_severity
    severe_evidence = max_sev in ("major", "critical")
    repeat_confirm = bool(risk.evidence.repeat_codes or risk.evidence.repeat_within_lot)

    strong_stat = risk.q_value <= escalate_q and smr >= smr_escalate and risk.quality_units >= min_events
    invest_stat = risk.q_value <= fdr_q and smr >= smr_investigate and risk.quality_units >= min_events
    watch_stat = risk.p_value <= watch_p and smr >= smr_watch and quality_events >= min_events
    unknown_watch = risk.unknown_units >= 2 and risk.unknown_share >= unknown_share_cut
    trace_watch = risk.traceability_gap_share >= trace_share_cut and quality_events >= 2
    confirm_watch = evidence_weight >= min_weight and (severe_evidence or repeat_confirm) and quality_events >= 1
    info_signal = (
        risk.information_units >= min_events
        and risk.info_p_value <= watch_p
        and (risk.info_smr or 0.0) >= 2.0
    )

    if strong_stat and repeat_confirm and evidence_weight >= min_weight and not risk.evidence.blocking:
        risk.proposed_level = "escalate"
        risk.add_reason("strong_statistical_signal", f"SMR={smr:.2f}，校正后 q={risk.q_value:.3f}")
        risk.add_reason("repeat_evidence", "同一缺陷编码在多个批次或同批次多次质检中重复确认")
    elif repeat_confirm and evidence_weight >= min_weight and severe_evidence and not risk.evidence.blocking and (
        invest_stat or watch_stat or risk.quality_units >= min_events
    ):
        risk.proposed_level = "investigate"
        risk.add_reason("repeat_evidence", "已确认的同类质检发现在多批次或同批次重复出现")
        if invest_stat:
            risk.add_reason("adjusted_rate_excess", f"SMR={smr:.2f}，校正后 q={risk.q_value:.3f}")
        elif watch_stat:
            risk.add_reason("statistical_context", f"SMR={smr:.2f}，p={risk.p_value:.3f}（未通过多重检验校正）")
        else:
            risk.add_reason("statistical_context", f"退货数量不足以达到统计显著（SMR={smr:.2f}），依据为确认的重复质检证据")
    elif invest_stat and evidence_weight >= min_weight and not risk.evidence.blocking:
        risk.proposed_level = "investigate"
        risk.add_reason("adjusted_rate_excess", f"SMR={smr:.2f}，校正后 q={risk.q_value:.3f}")
        risk.add_reason("confirmed_inspection", f"已确认质检发现权重 {evidence_weight}，支持进一步调查")
    elif watch_stat or confirm_watch or unknown_watch or trace_watch or info_signal:
        risk.proposed_level = "watch"
        if watch_stat:
            risk.add_reason("weak_signal", f"SMR={smr:.2f}，p={risk.p_value:.3f}")
        if confirm_watch:
            risk.add_reason("inspection_attention", f"已确认质检权重 {evidence_weight}，最高严重度 {max_sev}")
        if unknown_watch:
            risk.add_reason("unknown_share", f"未知原因占已判定退货 {risk.unknown_share:.0%}，风险可能被低估")
        if trace_watch:
            risk.add_reason("traceability_gap", f"追溯不完整覆盖 {risk.traceability_gap_share:.0%} 的成熟暴露")
        if info_signal:
            risk.add_reason(
                "information_signal",
                f"说明理解类 SMR={risk.info_smr:.2f}，优先核对商品说明，不按质量缺陷处理",
            )
        if risk.evidence.blocking:
            risk.add_reason("evidence_blocking", "存在待审/争议的重大质检发现，未作为升级依据")
    else:
        risk.proposed_level = "none"
        risk.add_reason("no_signal", "按当前暴露、分层背景和已确认证据，未发现需要改进的重复问题")

    if risk.evidence.blocking and risk.proposed_level in ("investigate", "escalate"):
        # Defensive: an escalation must never rest on unverified findings.
        risk.proposed_level = "watch"
        risk.add_reason("evidence_blocking", "重大质检发现仍在待审/争议，降级为观察")
    if risk.unknown_share >= unknown_share_cut and risk.proposed_level == "none":
        risk.proposed_level = "watch"
        risk.reasons = [r for r in risk.reasons if r["code"] != "no_signal"]
        risk.add_reason("unknown_share", "未知原因占比偏高，暂不能判定为无问题")
    if risk.proposed_level != "none":
        risk.reasons = [r for r in risk.reasons if r["code"] != "no_signal"]
    if not risk.reasons and risk.proposed_level != "none":
        risk.add_reason("context_only", "存在背景性关注点，但统计与质检证据均未达到调查门槛")
    if not risk.reasons:
        risk.add_reason("no_signal", "按当前暴露、分层背景和已确认证据，未发现需要改进的重复问题")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze(
    conn,
    *,
    as_of: str | date | None = None,
    settings: Settings | None = None,
    levels: tuple[str, ...] = ("lot", "sku"),
    note: str | None = None,
    persist: bool = True,
) -> AnalysisResult:
    settings = _load_settings(settings)
    cut = _resolve_as_of(conn, as_of)
    resolver = ReasonResolver(conn)
    orders = [dict(row) for row in conn.execute("SELECT * FROM orders")]
    lots = {row["lot_id"]: dict(row) for row in conn.execute("SELECT * FROM lots")}
    products = {row["sku"]: dict(row) for row in conn.execute("SELECT * FROM products")}
    cohort = _Cohort(settings, cut)
    returns = _effective_returns(conn, resolver, cut, settings)

    valid_order_ids = {row["order_id"] for row in orders}
    data_quality = {
        "orders_total": len(orders),
        "orders_without_lot": sum(1 for row in orders if not row.get("lot_id")),
        "orders_unknown_lot_placeholder": sum(1 for row in orders if row.get("lot_id") in cohort.placeholder),
        "orders_lot_missing_metadata": sum(1 for row in orders if row.get("lot_id") and row.get("lot_id") not in lots),
        "returns_total": len(returns),
        "returns_orphan": sum(1 for row in returns if row.order_id not in valid_order_ids),
        "returns_after_cutoff": sum(1 for row in returns if row.after_cutoff),
        "returns_over_order": sum(1 for row in returns if row.over_order),
        "returns_late_outside_window": sum(1 for row in returns if row.late),
        "returns_revised": sum(1 for row in returns if row.review_id),
        "returns_reason_unmapped": sum(1 for row in returns if not row.reason_code_known),
        "returns_category_unknown": sum(1 for row in returns if row.category == UNKNOWN),
        "refund_paid_units": sum(row.units for row in returns if _refund_state(conn, row) == "paid"),
        "refund_pending_units": sum(row.units for row in returns if _refund_state(conn, row) == "pending"),
        "refund_rejected_units": sum(row.units for row in returns if _refund_state(conn, row) == "rejected"),
        "pending_review_units": sum(row.units for row in returns if row.review_state == "review"),
        "inspections_confirmed": conn.execute(
            "SELECT COUNT(*) FROM inspections WHERE review_state='confirmed'"
        ).fetchone()[0],
        "inspections_unresolved": conn.execute(
            "SELECT COUNT(*) FROM inspections WHERE review_state IN ('pending','disputed')"
        ).fetchone()[0],
    }

    # Exposure by entity and stratum.
    exposure: dict[str, dict[str, dict[str, int]]] = {"lot": {}, "sku": {}}
    immature: dict[str, dict[str, int]] = {"lot": {}, "sku": {}}
    trace_gap: dict[str, dict[str, int]] = {"lot": {}, "sku": {}}
    counts: dict[str, dict[str, dict[str, dict[str, int]]]] = {"lot": {}, "sku": {}}
    late: dict[str, dict[str, int]] = {"lot": {}, "sku": {}}
    sku_lots: dict[str, set[str]] = {}
    lot_skus: dict[str, set[str]] = {}
    immature_units_total = 0

    future_units_total = 0
    for order in orders:
        lot = order.get("lot_id") or ""
        sku = order.get("sku") or UNKNOWN
        sku_lots.setdefault(sku, set()).add(lot)
        lot_skus.setdefault(lot, set()).add(sku)
        stratum = cohort.stratum(order["channel"], order["policy_version"])
        sold_at = parse_date(order["sold_at"], field="sold_at")
        units = int(order["units"])
        if sold_at > cut:
            # Sales after the analysis date are outside the observation period.
            future_units_total += units
            continue
        for level, key in (("lot", lot), ("sku", sku)):
            exposure[level].setdefault(key, {})
            immature[level].setdefault(key, 0)
            trace_gap[level].setdefault(key, 0)
            late[level].setdefault(key, 0)
            counts[level].setdefault(key, {})
        if not cohort.is_mature(sold_at, order["policy_version"]):
            immature["lot"][lot] += units
            immature["sku"][sku] += units
            immature_units_total += units
            continue
        exposure["lot"][lot][stratum] = exposure["lot"][lot].get(stratum, 0) + units
        exposure["sku"][sku][stratum] = exposure["sku"][sku].get(stratum, 0) + units
        if cohort.traceability_gap(lot, lots):
            trace_gap["lot"][lot] += units
            trace_gap["sku"][sku] += units

    for row in returns:
        if not row.order_exists or row.after_cutoff:
            continue
        sold_at = row.sold_at
        for level, key in (("lot", row.lot_id), ("sku", row.sku)):
            counts[level].setdefault(key, {})
            late[level].setdefault(key, 0)
        if not cohort.is_mature(sold_at, row.policy_version):
            continue
        bucket = cohort.category_bucket(row.category)
        if not row.reason_code_known:
            bucket = "unknown"
        for level, key in (("lot", row.lot_id), ("sku", row.sku)):
            counts[level][key].setdefault(bucket, {})
            counts[level][key][bucket][cohort.stratum(row.channel, row.policy_version)] = (
                counts[level][key][bucket].get(cohort.stratum(row.channel, row.policy_version), 0) + row.units
            )
            if row.late:
                late[level][key] += row.units

    background_exposure: dict[str, int] = {}
    background_counts: dict[str, dict[str, int]] = {"quality": {}, "unknown": {}, "information": {}}
    # Background is the pooled cohort, independent of entity.
    for order in orders:
        sold_check = parse_date(order["sold_at"], field="sold_at")
        if sold_check > cut:
            continue
        if not cohort.is_mature(sold_check, order["policy_version"]):
            continue
        stratum = cohort.stratum(order["channel"], order["policy_version"])
        background_exposure[stratum] = background_exposure.get(stratum, 0) + int(order["units"])
    for row in returns:
        if not row.order_exists or row.after_cutoff or not cohort.is_mature(row.sold_at, row.policy_version):
            continue
        bucket = cohort.category_bucket(row.category)
        if not row.reason_code_known:
            bucket = "unknown"
        if bucket not in background_counts:
            continue
        stratum = cohort.stratum(row.channel, row.policy_version)
        background_counts[bucket][stratum] = background_counts[bucket].get(stratum, 0) + row.units

    lot_evidence = _inspection_evidence(conn, cohort)
    service_lot, service_sku = _service_evidence(conn, cohort)

    # Return-lot sets per SKU for repetition.
    sku_return_lots: dict[str, set[str]] = {sku: set() for sku in sku_lots}
    for row in returns:
        if not row.order_exists or row.after_cutoff or not cohort.is_mature(row.sold_at, row.policy_version):
            continue
        if cohort.category_bucket(row.category) != "quality" or not row.reason_code_known:
            continue
        sku_return_lots.setdefault(row.sku, set()).add(row.lot_id)

    result = AnalysisResult(
        snapshot_id=0,
        as_of=cut,
        config_digest=settings.digest,
        data_digest=_data_digest(conn),
        data_quality=data_quality,
        notes=[],
    )

    for level in levels:
        entity_ids = [key for key in exposure[level] if key not in ("",)]
        scored = _score(
            entity_ids,
            {key: value for key, value in exposure[level].items()},
            counts[level],
            background_exposure,
            background_counts,
            settings,
            level=level,
            immature=immature[level],
            trace_gap=trace_gap[level],
            late=late[level],
        )
        for risk in scored:
            if level == "lot":
                risk.evidence = lot_evidence.get(risk.entity_id, InspectionEvidence())
                risk.service = service_lot.get(risk.entity_id, ServiceEvidence())
                related_skus = lot_skus.get(risk.entity_id, set())
                related_lots: set[str] = set()
                for sku in related_skus:
                    related_lots |= sku_lots.get(sku, set())
                related_returns: set[str] = set()
                for sku in related_skus:
                    related_returns |= sku_return_lots.get(sku, set())
                risk.scope = {
                    "sku": sorted(related_skus),
                    "lots": [risk.entity_id],
                    "supplier_id": (lots.get(risk.entity_id) or {}).get("supplier_id"),
                    "unshipped_units": (lots.get(risk.entity_id) or {}).get("unshipped_units", 0),
                    "traceability_state": (lots.get(risk.entity_id) or {}).get("traceability_state", "unknown"),
                }
                risk.repeat = _repeat_summary(
                    risk.evidence,
                    related_returns,
                    related_lots,
                    settings,
                )
                # A lot's own quality returns make the problem a repeated one for
                # that lot; cross-lot return repetition is visible in the SKU row.
                lot_returns = {
                    row.lot_id for row in returns
                    if row.lot_id == risk.entity_id and row.order_exists
                    and cohort.is_mature(row.sold_at, row.policy_version)
                    and cohort.category_bucket(row.category) == "quality" and row.reason_code_known
                }
                risk.repeat["quality_return_lots"] = sorted(lot_returns)
                risk.repeat["return_repeat"] = False
                risk.repeat["related_quality_return_lots"] = sorted(related_returns)
                risk.repeat["repeat_flag"] = bool(
                    risk.evidence.repeat_codes or risk.evidence.repeat_within_lot or lot_returns
                )
                risk.mix = _mix(
                    risk.entity_id, exposure[level], counts[level], background_exposure, background_counts
                )
            else:
                risk.service = service_sku.get(risk.entity_id, ServiceEvidence())
                risk.scope = {
                    "sku": [risk.entity_id],
                    "lots": sorted(sku_lots.get(risk.entity_id, set())),
                    "product_category": (products.get(risk.entity_id) or {}).get("category"),
                    "active_copy_version": (products.get(risk.entity_id) or {}).get("active_copy_version"),
                }
                # SKU-level repeat unions evidence from all its lots.
                union = InspectionEvidence()
                for lot in sku_lots.get(risk.entity_id, set()):
                    ev = lot_evidence.get(lot, InspectionEvidence())
                    union.confirmed.extend(ev.confirmed)
                    union.pending.extend(ev.pending)
                    union.rejected.extend(ev.rejected)
                    union.confirmed_weight += ev.confirmed_weight
                    union.pending_weight += ev.pending_weight
                    union.blocking = union.blocking or ev.blocking
                    for code, ids in ev.repeat_codes.items():
                        union.repeat_codes.setdefault(code, [])
                        for other in ids:
                            if other not in union.repeat_codes[code]:
                                union.repeat_codes[code].append(other)
                    for code, ids in ev.repeat_within_lot.items():
                        union.repeat_within_lot.setdefault(code, [])
                        union.repeat_within_lot[code].extend(i for i in ids if i not in union.repeat_within_lot[code])
                    order = {"minor": 1, "major": 2, "critical": 3}
                    if ev.max_confirmed_severity and (
                        union.max_confirmed_severity is None
                        or order[ev.max_confirmed_severity] > order[union.max_confirmed_severity]
                    ):
                        union.max_confirmed_severity = ev.max_confirmed_severity
                risk.evidence = union
                risk.repeat = _repeat_summary(
                    union,
                    sku_return_lots.get(risk.entity_id, set()),
                    sku_lots.get(risk.entity_id, set()),
                    settings,
                )
                risk.mix = _mix(
                    risk.entity_id, exposure[level], counts[level], background_exposure, background_counts
                )
            _grade(risk, settings)
        result.entities[level] = scored

    # Stratum table: policy/channel exposure and crude quality rate.
    strata_rows: list[dict[str, Any]] = []
    for stratum in sorted(background_exposure):
        n = background_exposure[stratum]
        k = background_counts["quality"].get(stratum, 0)
        u = background_counts["unknown"].get(stratum, 0)
        i = background_counts["information"].get(stratum, 0)
        channel, _, policy = stratum.partition("/")
        strata_rows.append(
            {
                "stratum": stratum,
                "channel": channel,
                "policy_version": policy,
                "exposure_units": n,
                "quality_units": k,
                "unknown_units": u,
                "information_units": i,
                "quality_rate": (k / n) if n else 0.0,
                "worst_case_rate": ((k + u) / n) if n else 0.0,
                "unknown_share": (u / (k + u)) if (k + u) else 0.0,
            }
        )
    result.strata = strata_rows

    total_n = sum(background_exposure.values())
    total_k = sum(background_counts["quality"].values())
    total_u = sum(background_counts["unknown"].values())
    total_i = sum(background_counts["information"].values())
    result.overall = {
        "as_of": cut.isoformat(),
        "maturity_days": {policy: cohort.maturity_days(policy) for policy in sorted(cohort.policy_windows)}
        | {"default": cohort.maturity_days(None)},
        "exposure_units": total_n,
        "immature_units": immature_units_total,
        "future_units_excluded": future_units_total,
        "quality_units": total_k,
        "unknown_units": total_u,
        "information_units": total_i,
        "quality_rate": (total_k / total_n) if total_n else 0.0,
        "worst_case_rate": ((total_k + total_u) / total_n) if total_n else 0.0,
        "unknown_share": (total_u / (total_k + total_u)) if (total_k + total_u) else 0.0,
        "strata_count": len(background_exposure),
        "stratum_heterogeneity_p": _heterogeneity_p(background_exposure, background_counts["quality"]),
        "data_quality": data_quality,
    }
    if data_quality["returns_late_outside_window"]:
        result.notes.append(
            f"{data_quality['returns_late_outside_window']} 条退货在政策窗口之后才到达，已按收到日期计入当前风险并单独提示延迟。"
        )
    if result.overall["stratum_heterogeneity_p"] is not None and result.overall["stratum_heterogeneity_p"] < 0.05:
        result.notes.append(
            "渠道/政策分层之间质量退货率差异显著，直接比较商品或批次的历史退货率会混入渠道政策效应；"
            "本报告的分层期望值已控制该差异。"
        )

    if persist:
        result.snapshot_id = _persist_snapshot(conn, result, settings, note)
    return result


def _heterogeneity_p(exposure: dict[str, int], counts: dict[str, int]) -> float | None:
    cells = [(n, counts.get(stratum, 0)) for stratum, n in exposure.items() if n > 0]
    if len(cells) < 2:
        return None
    total_n = sum(n for n, _ in cells)
    total_k = sum(k for _, k in cells)
    rate = total_k / total_n
    chi2 = sum((k - n * rate) ** 2 / (n * rate) for n, k in cells if n * rate > 0)
    return chi2_sf(chi2, len(cells) - 1)


def _persist_snapshot(conn, result: AnalysisResult, settings: Settings, note: str | None) -> int:
    settings_payload = json.loads(json.dumps(settings.data, ensure_ascii=False, sort_keys=True))
    stats_payload = {"overall": result.overall, "strata": result.strata, "notes": result.notes}
    cur = conn.execute(
        "INSERT INTO risk_snapshot(created_at, as_of, config_digest, data_digest, settings_json, stats_json,"
        " notes, previous_snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utc_now(),
            result.as_of.isoformat(),
            result.config_digest,
            result.data_digest,
            canonical_json(settings_payload),
            canonical_json(stats_payload),
            note,
            conn.execute("SELECT MAX(snapshot_id) FROM risk_snapshot").fetchone()[0],
        ),
    )
    snapshot_id = int(cur.lastrowid)
    columns = [
        "level", "entity_id", "proposed_level", "exposure_units", "immature_units",
        "quality_units", "unknown_units", "information_units", "fit_units", "other_units",
        "late_return_units", "expected_units", "smr", "p_value", "q_value",
        "crude_rate", "crude_lo", "crude_hi", "unknown_share", "worst_case_rate",
        "traceability_gap_units", "traceability_gap_share", "info_expected", "info_smr",
        "info_p_value", "info_q_value", "evidence_confirmed", "evidence_pending",
        "evidence_blocking", "repeat_json", "mix_json", "reasons_json", "scope_json",
    ]
    sql = (
        "INSERT INTO risk_result(snapshot_id, " + ", ".join(columns) + ")"
        " VALUES (?," + ", ".join("?" for _ in columns) + ")"
    )
    for entities in result.entities.values():
        for risk in entities:
            row = risk.as_row()
            values = [snapshot_id]
            for name in columns:
                value = row[name]
                if name.endswith("_json"):
                    value = canonical_json(value)
                values.append(value)
            conn.execute(sql, values)
    conn.commit()
    return snapshot_id


def latest_snapshot_id(conn) -> int:
    value = conn.execute("SELECT MAX(snapshot_id) FROM risk_snapshot").fetchone()[0]
    if value is None:
        raise DataError("尚无风险快照，请先运行 analyze")
    return int(value)


def load_snapshot(conn, snapshot_id: int | None = None) -> dict[str, Any]:
    snapshot_id = snapshot_id or latest_snapshot_id(conn)
    row = conn.execute("SELECT * FROM risk_snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise DataError(f"风险快照不存在: {snapshot_id}")
    results = [dict(r) for r in conn.execute("SELECT * FROM risk_result WHERE snapshot_id=? ORDER BY level, entity_id", (snapshot_id,))]
    parsed = []
    for item in results:
        for key in ("repeat_json", "mix_json", "reasons_json", "scope_json"):
            item[key] = json.loads(item[key]) if item[key] else {}
        item["proposed_level"] = item["proposed_level"]
        parsed.append(item)
    return {
        "snapshot": dict(row),
        "stats": json.loads(row["stats_json"]),
        "results": parsed,
    }
