"""Load and expose configuration from config/settings.yaml."""
from __future__ import annotations

from pathlib import Path
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


def load_config(path: Path | str | None = None) -> dict:
    """Read settings.yaml and return it as a plain dict."""
    path = Path(path) if path else CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def target_for(cfg: dict, department: str) -> float:
    """Return the sales target for a department, falling back to the default."""
    targets = cfg.get("targets", {})
    by_dept = targets.get("by_department", {}) or {}
    return float(by_dept.get(department, targets.get("default_target", 0)))
