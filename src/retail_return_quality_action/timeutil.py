"""Date helpers with strict ISO parsing."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from .errors import DataError

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: str | date | None, *, field: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise DataError(f"{field} 必须是 YYYY-MM-DD 格式，实际为 {value!r}")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DataError(f"{field} 不是有效日期: {value!r}") from exc


def try_parse_date(value: str | None) -> date | None:
    try:
        return parse_date(value)
    except DataError:
        return None


def iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value.isoformat()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


