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


# --------------------------------------------------------------------------- #
# Taxonomy — standardise Branch and derive Department from service category
# --------------------------------------------------------------------------- #
BRANCHES = ["Makati", "Cebu", "AMP", "TCF HR"]
DEPARTMENTS = ["Legal", "Accounting", "Audit", "HR"]

_DEFAULT_BRANCH_MAP = {
    "MAKATI": "Makati", "CEBU": "Cebu", "AMP": "AMP",
    "TCF HR": "TCF HR", "HR": "TCF HR",
}
# First matching keyword (checked in order) wins.
_DEFAULT_DEPT_KEYWORDS = {
    "Legal": ["legal", "proxy", "corporate secretary", "incorporation", "sec ",
              "registration", "permit", "transfer of shares", "change of officer",
              "amendment", "dissolution", "notariz", "license"],
    "Audit": ["audit"],
    "HR": ["hr", "payroll", "dole", "acr", "immigration", "visa", "work permit",
           "sss", "philhealth", "pag-ibig", "pagibig", "9(g)", "expat", "employee"],
    "Accounting": ["accounting", "account", "tax", "bookkeeping", "compilation",
                   "itr", "financial", "vat", "bir", "audit assistance", "advisory"],
}


def normalize_branch(cfg: dict, branch) -> str:
    tax = cfg.get("taxonomy", {}) or {}
    raw = {k.upper(): v for k, v in (tax.get("branch_map") or _DEFAULT_BRANCH_MAP).items()}
    s = str(branch or "").strip()
    if not s or s.lower() in ("nan", "none"):
        return "Unknown"
    return raw.get(s.upper(), s.title())


def department_for(cfg: dict, category) -> str:
    """Map a service category to a standard Department (Legal/Accounting/Audit/HR)."""
    tax = cfg.get("taxonomy", {}) or {}
    kw = tax.get("department_keywords") or _DEFAULT_DEPT_KEYWORDS
    default = tax.get("department_default", "Other")
    s = str(category or "").lower()
    for dept, words in kw.items():
        if any(w in s for w in words):
            return dept
    return default
