"""Reason-code classification with versioned maps, aliases and revisions.

A return's *effective* reason is the latest applied revision effective on or
before the analysis date; otherwise it is the original code.  Codes are then
translated through the legacy alias bridge and the versioned reason map that
was valid when the return was received.  This matters because a code switch
("口径切换") can otherwise look like a change in the defect rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .errors import DataError


@dataclass(frozen=True)
class Classification:
    reason_code: str
    canonical_code: str | None
    category: str
    mapping_version: str | None
    via_alias: bool
    note: str = ""


class ReasonResolver:
    def __init__(self, conn):
        self.conn = conn
        self._map_rows = [
            dict(row) for row in conn.execute("SELECT * FROM reason_map ORDER BY reason_code, valid_from")
        ]
        self._alias_rows = [
            dict(row) for row in conn.execute("SELECT * FROM reason_alias ORDER BY alias_code, valid_from")
        ]

    # -- code translation -------------------------------------------------
    def _alias(self, code: str, at: date) -> tuple[str, bool]:
        matches = [
            row
            for row in self._alias_rows
            if row["alias_code"] == code
            and row["valid_from"] <= at.isoformat()
            and (not row["valid_to"] or row["valid_to"] >= at.isoformat())
        ]
        if not matches:
            return code, False
        matches.sort(key=lambda row: row["valid_from"])
        return matches[-1]["canonical_code"], True

    def _category_for(self, canonical: str, at: date) -> tuple[str | None, str | None]:
        matches = [
            row
            for row in self._map_rows
            if row["reason_code"] == canonical
            and row["valid_from"] <= at.isoformat()
            and (not row["valid_to"] or row["valid_to"] >= at.isoformat())
        ]
        if not matches:
            return None, None
        matches.sort(key=lambda row: row["valid_from"])
        row = matches[-1]
        return row["category"], row["mapping_version"]

    def classify(self, code: str, at: date) -> Classification:
        canonical, via_alias = self._alias(code, at)
        category, version = self._category_for(canonical, at)
        if category is None:
            return Classification(
                reason_code=code,
                canonical_code=canonical,
                category="unknown",
                mapping_version=version,
                via_alias=via_alias,
                note="原因编码未在有效映射中找到，按未知处理",
            )
        return Classification(code, canonical, category, version, via_alias)

    # -- effective reason after revisions ---------------------------------
    def effective_code(
        self, return_id: str, original_code: str, as_of: date | None
    ) -> tuple[str, str | None, date | None]:
        """Return ``(effective_code, review_id, effective_at)``.

        ``effective_at`` is the date the latest applied revision took effect.
        Classification should use that date rather than the return's receipt
        date: a revision may introduce a code from a newer catalogue that did
        not exist when the return was first recorded.
        """
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM return_revision WHERE return_id=? ORDER BY seq", (return_id,)
            )
        ]
        code = original_code
        applied: str | None = None
        applied_at: date | None = None
        for row in rows:
            if as_of is not None and row["effective_at"] > as_of.isoformat():
                continue
            code = row["new_reason_code"]
            applied = row["source_review_id"]
            applied_at = date.fromisoformat(row["effective_at"])
        return code, applied, applied_at

    def revisions(self, return_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM return_revision WHERE return_id=? ORDER BY seq", (return_id,)
            )
        ]


def validate_reason_code(conn, code: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM reason_map WHERE reason_code=? UNION SELECT 1 FROM reason_alias WHERE alias_code=? LIMIT 1",
        (code, code),
    ).fetchone()
    if not exists:
        raise DataError(f"原因编码 {code} 不在映射或别名表中")


def apply_reviews(conn, as_of: date | None = None) -> dict[str, int]:
    """Materialise imported ``return_review`` rows into ``return_revision``.

    Only ``reclassify`` reviews with a target code create a revision; re-running
    is idempotent because ``source_review_id`` is unique.
    """
    from .timeutil import utc_now

    resolver = ReasonResolver(conn)
    stats = {"applied": 0, "skipped": 0}
    reviews = conn.execute(
        "SELECT * FROM return_review ORDER BY return_id, reviewed_at, review_id"
    ).fetchall()
    for review in reviews:
        review = dict(review)
        if review["decision"] != "reclassify" or not review["new_reason_code"]:
            stats["skipped"] += 1
            continue
        current = conn.execute(
            "SELECT seq, new_reason_code FROM return_revision WHERE return_id=? ORDER BY seq DESC LIMIT 1",
            (review["return_id"],),
        ).fetchone()
        return_row = conn.execute(
            "SELECT reason_code FROM returns WHERE return_id=?", (review["return_id"],)
        ).fetchone()
        if return_row is None:
            stats["skipped"] += 1
            continue
        previous = current["new_reason_code"] if current else return_row["reason_code"]
        seq = (current["seq"] + 1) if current else 1
        try:
            conn.execute(
                "INSERT INTO return_revision(return_id, seq, source_review_id, old_reason_code,"
                " new_reason_code, effective_at, decided_by, rationale, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review["return_id"],
                    seq,
                    review["review_id"],
                    previous,
                    review["new_reason_code"],
                    review["reviewed_at"],
                    review["reviewer"],
                    review["note"],
                    utc_now(),
                ),
            )
            stats["applied"] += 1
        except Exception as exc:  # duplicate source review => already applied
            if "UNIQUE" not in str(exc):
                raise
            stats["skipped"] += 1
    conn.commit()
    # Touch the resolver so callers cannot accidentally use a stale cache.
    del resolver
    return stats


def apply_inspection_reviews(conn) -> dict[str, int]:
    """Append imported inspection reviews to the review ledger (idempotent)."""
    stats = {"applied": 0, "skipped": 0}
    rows = conn.execute(
        "SELECT * FROM inspection_review_import ORDER BY inspection_id, reviewed_at, review_id"
    ).fetchall()
    for row in rows:
        row = dict(row)
        existing = conn.execute(
            "SELECT 1 FROM inspection_review WHERE inspection_id=? AND reviewed_at=? AND reviewer=?",
            (row["inspection_id"], row["reviewed_at"], row["reviewer"]),
        ).fetchone()
        if existing:
            stats["skipped"] += 1
            continue
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM inspection_review WHERE inspection_id=?",
            (row["inspection_id"],),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO inspection_review(inspection_id, seq, review_state, reviewer, rationale, reviewed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["inspection_id"],
                seq,
                row["review_state"],
                row["reviewer"],
                row["rationale"],
                row["reviewed_at"],
            ),
        )
        stats["applied"] += 1
    conn.commit()
    return stats


def seed_reviews_from_import(conn) -> dict[str, int]:
    """Load legacy review files if the review ledgers are still empty."""
    return_stats = {"return_revisions": 0, "inspection_reviews": 0}
    if conn.execute("SELECT COUNT(*) FROM return_revision").fetchone()[0] == 0:
        return_stats["return_revisions"] = apply_reviews(conn)["applied"]
    if conn.execute("SELECT COUNT(*) FROM inspection_review").fetchone()[0] == 0:
        return_stats["inspection_reviews"] = apply_inspection_reviews(conn)["applied"]
    return return_stats
