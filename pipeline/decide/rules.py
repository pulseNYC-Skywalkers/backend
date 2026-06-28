"""Milestone 4 — Decide: config-driven, 3-state billing decision.

The model never decides a claim. A deterministic rule engine does, driven by a
biller-editable YAML (`config/rules_config.yaml`). A decision is a PURE FUNCTION
of (canonical_patient, rules_config):

    bill | do_not_bill | flag_for_review

Explicit precedence (so rules can't contradict each other):
    1. payer is bundled/not-billable        -> do_not_bill   (hard)
    2. coverage required but none active     -> do_not_bill   (hard)
    3. wound documentation incomplete/conflict -> flag_for_review
    4. payer is conditional (needs a human)  -> flag_for_review
    5. active diagnosis required but missing  -> flag_for_review
    6. everything checks out                  -> bill

Every decision carries templated, biller-readable reasons that cite the grounded
facts, plus the rules_version it was derived under.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from observability.metrics import get_logger
from store.dao import Store

log = get_logger("decide")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "rules_config.yaml"

PAYER_NAMES = {
    "MCB": "Medicare Part B", "MCA": "Medicare Part A",
    "MCD": "Medicaid", "HMO": "HMO / Managed Care",
}

_REQUIRED_CONFIG_KEYS = {
    "rules_version", "payers", "require_active_coverage",
    "require_active_diagnosis", "required_wound_fields", "min_wound_area_cm2",
}


class ConfigError(ValueError):
    """Raised when rules_config is malformed — fail loud, never silently mis-bill."""


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = yaml.safe_load(path.read_text())
    if not isinstance(cfg, dict):
        raise ConfigError("rules_config must be a mapping")
    missing = _REQUIRED_CONFIG_KEYS - set(cfg)
    if missing:
        raise ConfigError(f"rules_config missing keys: {sorted(missing)}")
    payers = cfg.get("payers") or {}
    for bucket in ("billable", "conditional", "not_billable"):
        if not isinstance(payers.get(bucket, []), list):
            raise ConfigError(f"payers.{bucket} must be a list")
    # A payer code in two buckets would be a contradictory rule.
    seen: dict[str, str] = {}
    for bucket in ("billable", "conditional", "not_billable"):
        for code in payers.get(bucket, []):
            if code in seen:
                raise ConfigError(f"payer {code} appears in both {seen[code]} and {bucket}")
            seen[code] = bucket
    return cfg


def _active_coverage(coverage: list[dict] | None) -> dict | None:
    for c in coverage or []:
        if c.get("effective_to") in (None, "", "null"):
            return c
    return None


def _active_diagnosis(diagnoses: list[dict] | None) -> dict | None:
    for d in diagnoses or []:
        if str(d.get("clinical_status", "")).lower() == "active":
            return d
    return None


def decide(canonical: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Pure function: (patient, config) -> decision. No I/O, no model."""
    reasons: list[str] = []
    eligibility: dict[str, Any] = {}
    payers = cfg["payers"]
    code = canonical.get("primary_payer_code")
    payer_name = PAYER_NAMES.get(code, code or "unknown")

    primary = canonical.get("primary_wound") or {}
    facts = primary.get("facts") or {}
    active_cov = _active_coverage(canonical.get("coverage"))
    active_dx = _active_diagnosis(canonical.get("diagnoses"))

    def _final(decision: str) -> dict[str, Any]:
        return {
            "internal_id": canonical["internal_id"],
            "patient_id": canonical.get("patient_id"),
            "facility_id": canonical.get("facility_id"),
            "decision": decision, "reasons": reasons,
            "eligibility": eligibility, "rules_version": cfg["rules_version"],
        }

    # 1) Hard exclusion: bundled / not-billable payer.
    eligibility["payer"] = code
    if code in payers.get("not_billable", []):
        reasons.append(f"Payer {code} ({payer_name}) is bundled/managed care — wound "
                       f"care is not separately billable.")
        return _final("do_not_bill")

    # 2) Hard exclusion: coverage required but none active.
    if cfg.get("require_active_coverage"):
        eligibility["active_coverage"] = bool(active_cov)
        if not active_cov:
            reasons.append("No active insurance coverage on file (all coverage has lapsed).")
            return _final("do_not_bill")
        reasons.append(f"Active {active_cov.get('payer_type', 'insurance')} coverage "
                       f"({active_cov.get('payer_name', code)}).")

    # 3) Documentation completeness / conflicts.
    required = cfg.get("required_wound_fields", [])
    missing = [f for f in required if f not in facts]
    eligibility["wound_fields_present"] = [f for f in required if f in facts]
    eligibility["wound_fields_missing"] = missing
    if canonical.get("needs_review") or missing:
        if missing:
            reasons.append(f"Wound documentation incomplete — missing {', '.join(missing)}.")
        for r in canonical.get("review_reasons") or []:
            reasons.append(f"Review: {r}")
        return _final("flag_for_review")

    # area / healed check
    area = primary.get("area_cm2", 0.0)
    eligibility["area_cm2"] = area
    if area <= cfg.get("min_wound_area_cm2", 0.0):
        reasons.append(f"Wound area {area} cm^2 is at/under the billable threshold "
                       f"(possibly healed).")
        return _final("flag_for_review")

    # describe the grounded wound for the biller
    wtype = facts.get("wound_type", {}).get("value")
    loc = facts.get("location", {}).get("value")
    l = facts.get("length_cm", {}).get("value")
    w = facts.get("width_cm", {}).get("value")
    reasons.append(f"Documented wound: {wtype}, {loc}, {l}x{w} cm "
                   f"(area {area} cm^2) — all required fields grounded to source.")

    # 4) Conditional payer -> human confirmation.
    if code in payers.get("conditional", []):
        reasons.append(f"Payer {code} ({payer_name}) is conditionally billable — biller "
                       f"confirmation required.")
        return _final("flag_for_review")

    # 5) Active diagnosis required.
    if cfg.get("require_active_diagnosis"):
        eligibility["active_diagnosis"] = bool(active_dx)
        if not active_dx:
            reasons.append("No active diagnosis on record — needs a billable ICD-10.")
            return _final("flag_for_review")
        reasons.append(f"Active diagnosis {active_dx.get('icd10_code')} "
                       f"({active_dx.get('icd10_description')}).")

    # 6) Billable.
    if code in payers.get("billable", []):
        reasons.append(f"Payer {code} ({payer_name}) is separately billable for wound care.")
    return _final("bill")


def run(store: Store | None = None, config_path: str | Path | None = None) -> dict[str, int]:
    own = store is None
    store = store or Store()
    cfg = load_config(config_path)
    log.info("decision rules loaded: version=%s", cfg["rules_version"])

    for canonical in store.all_canonical():
        store.upsert_decision(decide(canonical, cfg))

    counts = store.decision_counts()
    log.info("decide complete (rules=%s): %s", cfg["rules_version"], counts)
    if own:
        store.close()
    return counts


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Config-driven billing decision engine")
    ap.add_argument("--config", help="path to rules_config.yaml")
    run(config_path=ap.parse_args().config)
