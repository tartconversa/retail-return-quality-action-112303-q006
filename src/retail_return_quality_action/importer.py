"""Idempotent import of source datasets into SQLite.

Design rules
------------
* Every run creates an ``import_batch`` audit row (even a no-op repeat).
* Rows are matched on their natural key and compared by a canonical hash, so a
  re-import of unchanged data changes nothing; changed descriptive fields are
  updated in place while immutable business facts are rejected and reported.
* Invalid rows are rejected individually and recorded in ``import_issue``.  A
  batch with any rejected row is marked ``partial`` but valid rows still land.
* Nothing is deleted.  Corrections are appended as revisions, which keeps the
  audit trail intact when the meaning of a return is later revised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .csvio import file_sha256, read_csv, row_hash
from .errors import DataError
from .timeutil import parse_date, utc_now

# ---------------------------------------------------------------------------
# Dataset specifications
# ---------------------------------------------------------------------------

IMMUTABLE = {"record_hash"}


@dataclass
class Column:
    name: str
    required: bool = True
    kind: str = "text"  # text | int | date | optional_text | optional_int
    choices: tuple[str, ...] | None = None


@dataclass
class DatasetSpec:
    key: str
    table: str
    source_name: str
    columns: list[Column]
    natural_key: tuple[str, ...]
    mutable: tuple[str, ...] = ()

    @property
    def header(self) -> list[str]:
        return [c.name for c in self.columns]


SPECS: list[DatasetSpec] = [
    DatasetSpec(
        key="products",
        table="products",
        source_name="products.csv",
        columns=[
            Column("sku"),
            Column("name", required=False),
            Column("category", required=False),
            Column("launched_on", kind="date", required=False),
            Column("active_copy_version", required=False),
        ],
        natural_key=("sku",),
        mutable=("name", "category", "active_copy_version"),
    ),
    DatasetSpec(
        key="lots",
        table="lots",
        source_name="lots.csv",
        columns=[
            Column("lot_id"),
            Column("supplier_id", required=False),
            Column("supplier_lot_ref", required=False),
            Column("produced_on", kind="date", required=False),
            Column("unshipped_units", kind="int", required=False),
            Column("traceability_state", choices=("complete", "partial", "unknown")),
            Column("note", required=False),
        ],
        natural_key=("lot_id",),
        mutable=("supplier_id", "supplier_lot_ref", "unshipped_units", "traceability_state", "note"),
    ),
    DatasetSpec(
        key="orders",
        table="orders",
        source_name="orders.csv",
        columns=[
            Column("order_id"),
            Column("sku"),
            Column("lot_id", required=False),
            Column("channel"),
            Column("sold_at", kind="date"),
            Column("units", kind="int"),
            Column("policy_version"),
        ],
        natural_key=("order_id",),
        mutable=(),
    ),
    DatasetSpec(
        key="returns",
        table="returns",
        source_name="returns.csv",
        columns=[
            Column("return_id"),
            Column("order_id"),
            Column("returned_units", kind="int"),
            Column("reason_code"),
            Column("received_at", kind="date"),
            Column("refund_state", choices=("pending", "paid", "rejected")),
            Column("review_state", choices=("review", "coded", "revised")),
        ],
        natural_key=("return_id",),
        mutable=(),
    ),
    DatasetSpec(
        key="reason_map",
        table="reason_map",
        source_name="reason_map.csv",
        columns=[
            Column("reason_code"),
            Column("category", choices=("quality", "information", "fit", "unknown", "logistics")),
            Column("valid_from", kind="date"),
            Column("valid_to", kind="date", required=False),
            Column("mapping_version"),
        ],
        natural_key=("reason_code", "mapping_version"),
        mutable=("category", "valid_to"),
    ),
    DatasetSpec(
        key="reason_aliases",
        table="reason_alias",
        source_name="reason_aliases.csv",
        columns=[
            Column("alias_code"),
            Column("canonical_code"),
            Column("scheme_version"),
            Column("valid_from", kind="date"),
            Column("valid_to", kind="date", required=False),
            Column("note", required=False),
        ],
        natural_key=("alias_code", "scheme_version"),
        mutable=("canonical_code", "valid_to", "note"),
    ),
    DatasetSpec(
        key="inspections",
        table="inspections",
        source_name="inspections.csv",
        columns=[
            Column("inspection_id"),
            Column("lot_id"),
            Column("sample_size", kind="int"),
            Column("finding_code"),
            Column("severity", choices=("minor", "major", "critical")),
            Column("review_state", choices=("pending", "confirmed", "disputed", "rejected")),
            Column("inspected_at", kind="date"),
        ],
        natural_key=("inspection_id",),
        mutable=("review_state",),
    ),
    DatasetSpec(
        key="actions",
        table="actions",
        source_name="actions.csv",
        columns=[
            Column("action_id"),
            Column("lot_id", required=False),
            Column("owner_role", choices=("quality", "product", "supply")),
            Column("action_type"),
            Column("decision_state"),
            Column("effective_at", kind="date", required=False),
            Column("receipt_ref", required=False),
        ],
        natural_key=("action_id",),
        mutable=(),
    ),
    DatasetSpec(
        key="receipts",
        table="receipts",
        source_name="receipts.csv",
        columns=[
            Column("receipt_ref"),
            Column("lot_id", required=False),
            Column("receipt_type"),
            Column("outcome"),
            Column("received_at", kind="date"),
            Column("channel", required=False),
            Column("note", required=False),
        ],
        natural_key=("receipt_ref",),
        mutable=(),
    ),
    DatasetSpec(
        key="service",
        table="customer_service",
        source_name="customer_service.csv",
        columns=[
            Column("summary_id"),
            Column("order_id"),
            Column("received_at", kind="date"),
            Column("topic"),
            Column("summary_text"),
            Column("review_state", choices=("pending", "confirmed", "rejected")),
            Column("linked_return_id", required=False),
        ],
        natural_key=("summary_id",),
        mutable=(),
    ),
    DatasetSpec(
        key="copy_versions",
        table="copy_versions",
        source_name="copy_versions.csv",
        columns=[
            Column("copy_version_id"),
            Column("sku"),
            Column("version"),
            Column("valid_from", kind="date"),
            Column("valid_to", kind="date", required=False),
            Column("change_type", required=False),
            Column("summary", required=False),
        ],
        natural_key=("copy_version_id",),
        mutable=("valid_to",),
    ),
    DatasetSpec(
        key="return_reviews",
        table="return_review",
        source_name="return_reviews.csv",
        columns=[
            Column("review_id"),
            Column("return_id"),
            Column("decision", choices=("reclassify", "confirm", "reject")),
            Column("new_reason_code", required=False),
            Column("reviewed_at", kind="date"),
            Column("reviewer"),
            Column("note", required=False),
        ],
        natural_key=("review_id",),
        mutable=(),
    ),
    DatasetSpec(
        key="inspection_reviews",
        table="inspection_review_import",
        source_name="inspection_reviews.csv",
        columns=[
            Column("review_id"),
            Column("inspection_id"),
            Column("review_state", choices=("confirmed", "rejected", "disputed", "pending")),
            Column("reviewer"),
            Column("rationale"),
            Column("reviewed_at", kind="date"),
        ],
        natural_key=("review_id",),
        mutable=(),
    ),
]

# Follow-up extracts share the orders/returns schema but arrive as separate
# files.  They are imported into the same tables so that improvement tracking
# sees a continuous sales history.
FOLLOWUP_SPECS: list[DatasetSpec] = [
    DatasetSpec(
        key="orders_followup",
        table="orders",
        source_name="followup_orders.csv",
        columns=[
            Column("order_id"),
            Column("sku"),
            Column("lot_id", required=False),
            Column("channel"),
            Column("sold_at", kind="date"),
            Column("units", kind="int"),
            Column("policy_version"),
        ],
        natural_key=("order_id",),
    ),
    DatasetSpec(
        key="returns_followup",
        table="returns",
        source_name="followup_returns.csv",
        columns=[
            Column("return_id"),
            Column("order_id"),
            Column("returned_units", kind="int"),
            Column("reason_code"),
            Column("received_at", kind="date"),
            Column("refund_state", choices=("pending", "paid", "rejected")),
            Column("review_state", choices=("review", "coded", "revised")),
        ],
        natural_key=("return_id",),
    ),
]

SPECS.extend(FOLLOWUP_SPECS)
SPEC_BY_KEY = {spec.key: spec for spec in SPECS}
SPEC_BY_FILE = {spec.source_name: spec for spec in SPECS}


def _convert(spec: DatasetSpec, column: Column, raw: str) -> Any:
    value = raw.strip()
    if value == "":
        if column.required:
            raise DataError(f"必填字段 {column.name} 为空")
        return None
    if column.kind == "int":
        try:
            number = int(value)
        except ValueError as exc:
            raise DataError(f"{column.name} 必须是整数，实际为 {value!r}") from exc
        if number < 0:
            raise DataError(f"{column.name} 不能为负数")
        return number
    if column.kind == "date":
        return parse_date(value, field=column.name)
    if column.choices and value not in column.choices:
        raise DataError(f"{column.name} 取值 {value!r} 不在允许集合 {column.choices}")
    return value


def _validate_row(spec: DatasetSpec, raw: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for column in spec.columns:
        payload[column.name] = _convert(spec, column, raw.get(column.name, ""))
    if spec.key == "orders":
        if payload["units"] <= 0:
            raise DataError("units 必须大于 0")
    if spec.key == "returns":
        if payload["returned_units"] <= 0:
            raise DataError("returned_units 必须大于 0")
    if spec.key == "inspections":
        if payload["sample_size"] <= 0:
            raise DataError("sample_size 必须大于 0")
    if spec.key == "lots":
        if payload["unshipped_units"] is None:
            payload["unshipped_units"] = 0
        if payload["unshipped_units"] < 0:
            raise DataError("unshipped_units 不能为负数")
    if spec.key in ("reason_map", "reason_aliases"):
        if payload["valid_to"] is not None and payload["valid_to"] < payload["valid_from"]:
            raise DataError("valid_to 不能早于 valid_from")
    if spec.key == "return_reviews":
        if payload["decision"] == "reclassify" and not payload.get("new_reason_code"):
            raise DataError("reclassify 复核必须给出 new_reason_code")
    if spec.key == "copy_versions":
        if payload["valid_to"] is not None and payload["valid_to"] < payload["valid_from"]:
            raise DataError("valid_to 不能早于 valid_from")
    return payload


def _hash_payload(spec: DatasetSpec, payload: dict[str, Any]) -> str:
    cleaned = {}
    for key, value in payload.items():
        if isinstance(value, date):
            cleaned[key] = value.isoformat()
        else:
            cleaned[key] = value
    return row_hash(cleaned)


def _insert_sql(spec: DatasetSpec, payload: dict[str, Any], extra: dict[str, Any]) -> tuple[str, list[Any]]:
    declared = [c.name for c in spec.columns]
    names = declared + [k for k in extra if k not in declared]
    values: list[Any] = []
    for name in names:
        value = payload.get(name) if name in payload else extra.get(name)
        values.append(value.isoformat() if isinstance(value, date) else value)
    placeholders = ", ".join("?" for _ in names)
    return f"INSERT INTO {spec.table} ({', '.join(names)}) VALUES ({placeholders})", values


@dataclass
class BatchResult:
    batch_id: int
    dataset: str
    status: str
    total: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "dataset": self.dataset,
            "status": self.status,
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "issues": self.issues,
        }


def _merge_extra(spec: DatasetSpec, payload: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {"record_hash": _hash_payload(spec, payload)}
    for column in spec.columns:
        if column.name in spec.natural_key:
            extra[column.name] = payload[column.name]
    if spec.key == "lots":
        extra["traceability_state"] = payload["traceability_state"]
    return extra


def import_dataset(
    conn,
    path: str | Path,
    *,
    dataset: str | None = None,
    strict: bool = False,
    note: str | None = None,
) -> BatchResult:
    """Import one CSV file.  ``strict`` aborts the batch on the first bad row."""
    source = Path(path)
    spec = SPEC_BY_KEY.get(dataset) if dataset else SPEC_BY_FILE.get(source.name)
    if spec is None:
        raise DataError(f"无法识别数据集: {dataset or source.name}")
    if not source.exists():
        raise DataError(f"源文件不存在: {source}")

    header, raw_rows, encoding = read_csv(source)
    missing = [name for name in spec.header if name not in header]
    if missing:
        raise DataError(f"{source.name} 缺少列: {', '.join(missing)}")
    extra_cols = [name for name in header if name not in spec.header]
    if extra_cols:
        raise DataError(f"{source.name} 包含未声明列: {', '.join(extra_cols)}")

    started = utc_now()
    batch_id = _start_batch(conn, spec, source, encoding, started, note)
    result = BatchResult(batch_id=batch_id, dataset=spec.key, status="completed")
    result.total = len(raw_rows)

    for index, raw in enumerate(raw_rows, start=2):
        record_key = raw.get(spec.natural_key[0], "")
        try:
            payload = _validate_row(spec, raw)
        except DataError as exc:
            result.rejected += 1
            issue = {
                "row_number": index,
                "severity": "error",
                "code": "invalid_row",
                "message": str(exc),
                "record_key": record_key,
                "raw": raw,
            }
            result.issues.append(issue)
            _record_issue(conn, batch_id, spec.key, issue)
            if strict:
                conn.rollback()
                raise DataError(f"第 {index} 行校验失败: {exc}") from exc
            continue

        try:
            outcome, changed_fields = _upsert_row(conn, spec, payload, batch_id)
        except DataError as exc:
            result.rejected += 1
            issue = {
                "row_number": index,
                "severity": "error",
                "code": "immutable_fact_conflict",
                "message": str(exc),
                "record_key": record_key,
                "raw": raw,
            }
            result.issues.append(issue)
            _record_issue(conn, batch_id, spec.key, issue)
            if strict:
                conn.rollback()
                raise DataError(f"第 {index} 行校验失败: {exc}") from exc
            continue
        if outcome == "inserted":
            result.inserted += 1
        elif outcome == "updated":
            result.updated += 1
        else:
            result.unchanged += 1
        if changed_fields:
            _record_issue(
                conn,
                batch_id,
                spec.key,
                {
                    "row_number": index,
                    "severity": "warning",
                    "code": "mutable_fields_updated",
                    "message": "可变更字段已更新: " + ", ".join(changed_fields),
                    "record_key": record_key,
                    "raw": raw,
                },
            )

    if result.inserted or result.updated:
        _post_import_checks(conn, spec, batch_id, result)
    if result.rejected:
        result.status = "partial"
    _finish_batch(conn, batch_id, result)
    conn.commit()
    return result


def _start_batch(conn, spec: DatasetSpec, source: Path, encoding: str, started: str, note: str | None) -> int:
    digest = file_sha256(source)
    cur = conn.execute(
        "INSERT INTO import_batch(dataset, source_name, sha256, encoding, imported_at, status, note)"
        " VALUES (?, ?, ?, ?, ?, 'running', ?)",
        (spec.key, source.name, digest, encoding, started, note),
    )
    return int(cur.lastrowid)


def _finish_batch(conn, batch_id: int, result: BatchResult) -> None:
    conn.execute(
        "UPDATE import_batch SET rows_total=?, rows_new=?, rows_updated=?, rows_unchanged=?,"
        " rows_rejected=?, status=?, note=COALESCE(note, ?) WHERE batch_id=?",
        (
            result.total,
            result.inserted,
            result.updated,
            result.unchanged,
            result.rejected,
            result.status,
            None,
            batch_id,
        ),
    )


def _record_issue(conn, batch_id: int, dataset: str, issue: dict[str, Any]) -> None:
    import json

    conn.execute(
        "INSERT INTO import_issue(batch_id, dataset, row_number, severity, code, message, record_key, raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            batch_id,
            dataset,
            issue.get("row_number"),
            issue["severity"],
            issue["code"],
            issue["message"],
            issue.get("record_key"),
            json.dumps(issue.get("raw"), ensure_ascii=False) if issue.get("raw") is not None else None,
        ),
    )


_MISSING = object()


def _upsert_row(conn, spec: DatasetSpec, payload: dict[str, Any], batch_id: int) -> tuple[str, list[str]]:
    extra = _merge_extra(spec, payload)
    key_where = " AND ".join(f"{name}=?" for name in spec.natural_key)
    key_values = [payload[name] for name in spec.natural_key]
    existing = conn.execute(f"SELECT * FROM {spec.table} WHERE {key_where}", key_values).fetchone()
    new_hash = extra["record_hash"]
    if existing is not None:
        if existing["record_hash"] == new_hash:
            conn.execute(
                f"UPDATE {spec.table} SET last_batch=? WHERE {key_where}",
                [batch_id, *key_values],
            )
            return "unchanged", []
        changed: list[str] = []
        assignments: list[str] = []
        values: list[Any] = []
        for name in spec.mutable:
            if name not in payload:
                continue
            current = existing[name] if name in existing.keys() else None
            desired = payload[name]
            current_norm = current.isoformat() if isinstance(current, date) else current
            desired_norm = desired.isoformat() if isinstance(desired, date) else desired
            if current_norm != desired_norm:
                changed.append(name)
                assignments.append(f"{name}=?")
                values.append(desired_norm)
        # Immutable business facts must not silently drift.
        immutable_changed = [
            c.name
            for c in spec.columns
            if c.name not in spec.mutable and c.name not in spec.natural_key
            and _norm(payload[c.name]) != _norm(existing[c.name] if c.name in existing.keys() else None)
            and c.name not in {"record_hash"}
        ]
        if immutable_changed:
            raise DataError(
                "不可变更的业务字段出现差异: " + ", ".join(immutable_changed)
                + "；请以修订记录方式处理，或确认是否为新记录"
            )
        if assignments:
            values.extend([new_hash, batch_id, *key_values])
            conn.execute(
                f"UPDATE {spec.table} SET {', '.join(assignments)}, record_hash=?, last_batch=?"
                f" WHERE {key_where}",
                values,
            )
            return "updated", changed
        conn.execute(
            f"UPDATE {spec.table} SET record_hash=?, last_batch=? WHERE {key_where}",
            [new_hash, batch_id, *key_values],
        )
        return "updated", []
    extra["first_batch"] = batch_id
    extra["last_batch"] = batch_id
    sql, values = _insert_sql(spec, payload, extra)
    conn.execute(sql, values)
    return "inserted", []


def _norm(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


def _post_import_checks(conn, spec: DatasetSpec, batch_id: int, result: BatchResult) -> None:
    """Record referential/consistency warnings without rejecting good rows."""
    if spec.key == "returns":
        orphans = conn.execute(
            "SELECT r.return_id FROM returns r LEFT JOIN orders o ON o.order_id=r.order_id"
            " WHERE o.order_id IS NULL AND r.last_batch=?",
            (batch_id,),
        ).fetchall()
        for row in orphans:
            _record_issue(
                conn,
                batch_id,
                spec.key,
                {
                    "row_number": None,
                    "severity": "warning",
                    "code": "orphan_return",
                    "message": "退货记录引用了不存在的订单，将在分析中按不可追溯处理",
                    "record_key": row["return_id"],
                    "raw": None,
                },
            )
        over = conn.execute(
            "SELECT r.return_id, r.returned_units, o.units, r.order_id FROM returns r"
            " JOIN orders o ON o.order_id=r.order_id"
            " WHERE r.returned_units > o.units AND r.last_batch=?",
            (batch_id,),
        ).fetchall()
        for row in over:
            _record_issue(
                conn,
                batch_id,
                spec.key,
                {
                    "row_number": None,
                    "severity": "warning",
                    "code": "returned_units_exceed_order",
                    "message": (
                        f"订单 {row['order_id']} 退货 {row['returned_units']} 件超过下单 {row['units']} 件，"
                        "分析时按订单件数封顶，并记入数据质量问题"
                    ),
                    "record_key": row["return_id"],
                    "raw": None,
                },
            )
        timeline = conn.execute(
            "SELECT r.return_id, r.received_at, o.sold_at FROM returns r JOIN orders o ON o.order_id=r.order_id"
            " WHERE r.received_at < o.sold_at AND r.last_batch=?",
            (batch_id,),
        ).fetchall()
        for row in timeline:
            _record_issue(
                conn,
                batch_id,
                spec.key,
                {
                    "row_number": None,
                    "severity": "warning",
                    "code": "return_before_sale",
                    "message": f"退货时间 {row['received_at']} 早于销售时间 {row['sold_at']}",
                    "record_key": row["return_id"],
                    "raw": None,
                },
            )
    if spec.key == "orders":
        unknown_lot = conn.execute(
            "SELECT lot_id FROM orders WHERE last_batch=? AND (lot_id IS NULL OR lot_id='')",
            (batch_id,),
        ).fetchall()
        for row in unknown_lot:
            _record_issue(
                conn,
                batch_id,
                spec.key,
                {
                    "row_number": None,
                    "severity": "warning",
                    "code": "order_without_lot",
                    "message": "订单缺少批次号，批次追溯不完整",
                    "record_key": None,
                    "raw": None,
                },
            )


def import_directory(conn, directory: str | Path, *, strict: bool = False) -> list[BatchResult]:
    """Import every recognised CSV present in ``directory`` in dependency order."""
    folder = Path(directory)
    if not folder.is_dir():
        raise DataError(f"资料目录不存在: {folder}")
    order = [
        "products",
        "lots",
        "orders",
        "returns",
        "reason_map",
        "reason_aliases",
        "inspections",
        "actions",
        "receipts",
        "service",
        "copy_versions",
        "inspection_reviews",
        "return_reviews",
        "orders_followup",
        "returns_followup",
    ]
    results: list[BatchResult] = []
    for key in order:
        spec = SPEC_BY_KEY[key]
        path = folder / spec.source_name
        if not path.exists():
            continue
        results.append(import_dataset(conn, path, dataset=key, strict=strict))
    return results
