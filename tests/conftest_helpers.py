"""Shared helpers for the test suite (standard library only)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DATA = ROOT / "data"
sys.path.insert(0, str(SRC))

from retail_return_quality_action import analysis, db, importer, reasons  # noqa: E402


def fresh_db(tmp_path: Path, name: str = "test.db"):
    conn = db.connect(tmp_path / name)
    return conn


def import_all(conn, directory: Path = DATA):
    results = importer.import_directory(conn, directory)
    reasons.seed_reviews_from_import(conn)
    return results


def copy_data(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DATA, target)
    return target
