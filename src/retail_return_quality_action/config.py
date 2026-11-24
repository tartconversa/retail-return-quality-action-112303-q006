"""Configuration loading and validation.

The application keeps business thresholds outside the code so that the quality
organisation can tune them without a release.  Every digest is recorded on a
risk snapshot, making it possible to explain which rules produced a decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "analysis.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "policy_windows_days": {"POL-0": 7, "POL-1": 14, "POL-2": 21, "POL-3": 30},
    "default_window_days": 30,
    "maturity_buffer_days": 7,
    "quality_categories": ["quality"],
    "unknown_categories": ["unknown"],
    "expected_smoothing": 0.5,
    "significance": {
        "fdr_q": 0.10,
        "watch_p": 0.20,
        "escalate_q": 0.10,
        "smr_investigate": 1.5,
        "smr_escalate": 2.0,
        "min_events": 2,
        "lot_investigate_p": 0.20,
        "lot_investigate_smr": 1.75,
        "lot_investigate_events": 4,
        "lot_investigate_smr": 1.75,
        "lot_investigate_events": 4,
        "smr_watch": 1.5,
    },
    "repeat": {
        "min_units": 2,
        "min_lots": 2,
        "min_severity_weight": 3,
        "qualifying_lot_smr": 1.3,
        "qualifying_lot_p": 0.30,
    },
    "severity_weights": {"minor": 1, "major": 2, "critical": 3},
    "traceability": {
        "placeholder_ids": ["LOT-000", "SKU-000"],
        "unknown_states": ["unknown", "partial"],
        "min_unknown_share_watch": 0.30,
        "min_untraced_share_watch": 0.20,
    },
    "improvement": {
        "min_post_exposure_units": 20,
        "alpha": 0.10,
        "min_pre_events": 2,
        "pre_days": 60,
        "post_days": 60,
    },
    "decisions": {
        "levels": ["none", "watch", "investigate", "escalate"],
        "quality_roles": ["quality"],
        "product_actions": ["revise-copy", "change-packaging", "no-change", "monitor"],
        "supply_actions": ["hold-shipment", "supplier-fix", "release-hold", "no-action"],
        "quality_actions": ["request-investigation", "close-case", "monitor"],
        "execution_states": ["proposed", "confirmed", "in_progress", "complete", "cancelled", "superseded"],
        "receipt_states": ["accepted", "complete", "rejected", "partial", "cancelled"],
        "receipt_required_for": [
            "hold-shipment",
            "supplier-fix",
            "release-hold",
            "revise-copy",
            "change-packaging",
        ],
        "action_categories": {
            "revise-copy": "product_copy",
            "change-packaging": "product_packaging",
            "no-change": "product_copy",
            "monitor": "quality_monitor",
            "request-investigation": "quality_investigation",
            "close-case": "quality_closure",
            "hold-shipment": "supply_hold",
            "supplier-fix": "supplier_remediation",
            "release-hold": "supply_release",
            "no-action": "supply_no_action",
        },
    },
    "late_return_days": 30,
    "report": {"top_n": 10, "rate_precision": 4},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class Settings:
    data: dict[str, Any]
    source_path: str | None = None
    digest: str = field(default="")

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def policy_windows(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in self.data["policy_windows_days"].items()}

    @property
    def quality_categories(self) -> set[str]:
        return set(self.data["quality_categories"])

    @property
    def unknown_categories(self) -> set[str]:
        return set(self.data["unknown_categories"])

    @property
    def severity_weights(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in self.data["severity_weights"].items()}

def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    source: str | None = None
    if config_path.exists():
        source = str(config_path)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"无法读取配置 {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("配置文件顶层必须是 JSON 对象")
    elif path is not None:
        raise ConfigError(f"配置文件不存在: {config_path}")

    data = _deep_merge(_DEFAULT_CONFIG, raw)
    _validate(data)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Settings(data=data, source_path=source, digest=digest)


def _validate(data: dict[str, Any]) -> None:
    required = [
        "policy_windows_days",
        "default_window_days",
        "maturity_buffer_days",
        "quality_categories",
        "unknown_categories",
        "significance",
        "repeat",
        "severity_weights",
        "traceability",
        "improvement",
        "decisions",
    ]
    for key in required:
        if key not in data:
            raise ConfigError(f"配置缺少字段: {key}")
    if not isinstance(data["policy_windows_days"], dict) or not data["policy_windows_days"]:
        raise ConfigError("policy_windows_days 必须是非空对象")
    for key, value in data["policy_windows_days"].items():
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"policy_windows_days.{key} 必须是正整数")
    for key in ("default_window_days", "maturity_buffer_days"):
        if not isinstance(data[key], int) or data[key] < 0:
            raise ConfigError(f"{key} 必须是非负整数")
    if data["default_window_days"] <= 0:
        raise ConfigError("default_window_days 必须为正整数")
    if not isinstance(data["quality_categories"], list) or not data["quality_categories"]:
        raise ConfigError("quality_categories 必须是非空数组")
    if not isinstance(data["unknown_categories"], list) or not data["unknown_categories"]:
        raise ConfigError("unknown_categories 必须是非空数组")
    overlap = set(data["quality_categories"]) & set(data["unknown_categories"])
    if overlap:
        raise ConfigError(f"质量与未知原因分类不能重叠: {sorted(overlap)}")
    sev = data["severity_weights"]
    if not isinstance(sev, dict) or not {"minor", "major", "critical"} <= set(sev):
        raise ConfigError("severity_weights 必须包含 minor/major/critical")
    levels = data["decisions"].get("levels", [])
    if levels != ["none", "watch", "investigate", "escalate"]:
        raise ConfigError("decisions.levels 必须依次为 none/watch/investigate/escalate")
    for key in ("quality_actions", "product_actions", "supply_actions"):
        if not data["decisions"].get(key):
            raise ConfigError(f"decisions.{key} 不能为空")
    if not data["decisions"].get("receipt_states"):
        raise ConfigError("decisions.receipt_states 不能为空")
    for key in ("fdr_q", "watch_p", "escalate_q", "smr_investigate", "smr_escalate"):
        value = data["significance"].get(key)
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"significance.{key} 必须是非负数")
    if not 0 < float(data["significance"]["fdr_q"]) <= 1:
        raise ConfigError("significance.fdr_q 必须在 (0,1]")
    if not 0 < float(data["significance"]["watch_p"]) <= 1:
        raise ConfigError("significance.watch_p 必须在 (0,1]")
    if int(data["improvement"].get("min_post_exposure_units", 0)) < 1:
        raise ConfigError("improvement.min_post_exposure_units 必须为正整数")
    if int(data["improvement"].get("pre_days", 0)) < 1 or int(data["improvement"].get("post_days", 0)) < 1:
        raise ConfigError("improvement.pre_days/post_days 必须为正整数")
