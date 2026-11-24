"""CSV input/output with explicit encodings and atomic writes."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

from .errors import DataError

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


def read_text(path: str | Path) -> tuple[str, str]:
    file_path = Path(path)
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            return file_path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
        except OSError as exc:
            raise DataError(f"无法读取文件 {file_path}: {exc}") from exc
    raise DataError(f"文件编码无法识别（尝试 {', '.join(ENCODINGS)}）: {file_path}: {last_error}")


def read_csv(path: str | Path) -> tuple[list[str], list[dict[str, str]], str]:
    """Read a CSV file, returning header, rows (strings) and encoding.

    Header and values are stripped of surrounding whitespace and BOM.  Missing
    trailing cells become empty strings.  Extra cells are rejected because they
    almost always indicate a column-count problem.
    """
    file_path = Path(path)
    text, encoding = read_text(file_path)
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream)
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise DataError(f"CSV 文件为空: {file_path}") from exc
    header = [cell.strip().lstrip("﻿") for cell in raw_header]
    if not header or any(not cell for cell in header):
        raise DataError(f"CSV 表头包含空列名: {file_path}")
    if len(set(header)) != len(header):
        raise DataError(f"CSV 表头存在重复列名: {file_path}")
    rows: list[dict[str, str]] = []
    for line_no, cells in enumerate(reader, start=2):
        if not cells or all(not c.strip() for c in cells):
            continue
        if len(cells) != len(header):
            raise DataError(
                f"{file_path} 第 {line_no} 行列数为 {len(cells)}，表头为 {len(header)} 列"
            )
        rows.append({name: cells[i].strip() for i, name in enumerate(header)})
    return header, rows, encoding


def write_csv(path: str | Path, header: Iterable[str], rows: Iterable[Mapping[str, object]]) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=file_path.name + ".", dir=str(file_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(header), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _stringify(v) for k, v in row.items()})
        os.replace(tmp_name, file_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return file_path


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def write_text(path: str | Path, content: str) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=file_path.name + ".", dir=str(file_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        os.replace(tmp_name, file_path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return file_path


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def row_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


