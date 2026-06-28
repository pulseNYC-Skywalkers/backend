"""Versioned EHR API adapter (PCC contract v1, per API.md).

Everything that knows the *shape* of the upstream API lives here:
  * URL construction (query-param endpoints, fixed facility constants)
  * the two-layer identity model: string `patient_id` (FA-001) for
    diagnoses/coverage vs integer `id` for notes/assessments
  * on-arrival schema validation: missing/invalid required fields -> quarantine;
    unknown fields / contract drift -> warn (don't fail silently)

A contract change (renamed field, new endpoint) is handled by adding a new
adapter version and flipping EHR_ADAPTER_VERSION — not by touching the runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, ValidationError

# Patient-scoped resources keyed by STRING patient_id.
STRING_ID_RESOURCES = {"diagnoses", "coverage"}
# Patient-scoped resources keyed by INTEGER internal id.
INT_ID_RESOURCES = {"notes", "assessments"}
PATIENT_SUBRESOURCES = ("diagnoses", "coverage", "notes", "assessments")


# --- on-arrival schemas (required fields only are strict) ----------------------
class _Rec(BaseModel):
    # extra='allow' so forward-compatible: unknown fields don't crash us; we
    # detect and warn about them separately (contract-drift signal). All
    # documented fields are declared (optional ones included) so that only
    # genuinely *unexpected* fields trip the drift warning.
    model_config = ConfigDict(extra="allow")


class PatientRec(_Rec):
    id: int
    facility_id: int
    patient_id: str
    first_name: str | None = None
    last_name: str | None = None
    birth_date: str | None = None
    gender: str | None = None
    primary_payer_code: str | None = None
    last_modified_at: str | None = None
    is_new_admission: bool | None = None


class DiagnosisRec(_Rec):
    id: int
    patient_id: str
    icd10_code: str | None = None
    icd10_description: str | None = None
    clinical_status: str | None = None
    onset_date: str | None = None
    last_modified_at: str | None = None


class CoverageRec(_Rec):
    id: int
    patient_id: str
    payer_name: str | None = None
    payer_code: str | None = None
    payer_type: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    last_modified_at: str | None = None


class NoteRec(_Rec):
    id: int
    patient_id: int
    org_id: str | None = None
    pcc_note_id: int | None = None
    note_type: str | None = None
    effective_date: str | None = None
    note_text: str | None = None
    created_by: str | None = None
    note_label: str | None = None
    sync_version: int | None = None
    is_current: bool | None = None


class AssessmentRec(_Rec):
    id: int
    patient_id: int
    org_id: str | None = None
    pcc_assessment_id: int | None = None
    assessment_type: str | None = None
    status: str | None = None
    assessment_date: str | None = None
    completion_date: str | None = None
    template_id: int | None = None
    assessment_type_description: str | None = None
    raw_json: str | None = None
    sync_version: int | None = None
    is_current: bool | None = None


_MODELS: dict[str, type[_Rec]] = {
    "patients": PatientRec,
    "diagnoses": DiagnosisRec,
    "coverage": CoverageRec,
    "notes": NoteRec,
    "assessments": AssessmentRec,
}


@dataclass
class ValidationResult:
    ok: bool                       # False -> quarantine the whole payload
    is_empty: bool                 # True  -> 200 but no records (no_data)
    n_records: int
    records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PccAdapterV1:
    version = "pcc_v1"

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    # --- URL construction --------------------------------------------------
    def patients_url(self, facility_id: int, since: str | None = None) -> str:
        q = {"facility_id": facility_id}
        if since:
            q["since"] = since
        return f"{self.base}/pcc/patients?{urlencode(q)}"

    def subresource_url(self, resource_type: str, key: str | int,
                        since: str | None = None) -> str:
        q: dict[str, Any] = {"patient_id": key}
        if since and resource_type in ("notes", "assessments"):
            q["since"] = since
        return f"{self.base}/pcc/{resource_type}?{urlencode(q)}"

    def health_url(self) -> str:
        return f"{self.base}/health"

    # --- identity model ----------------------------------------------------
    @staticmethod
    def patient_keys(patient_rec: dict[str, Any]) -> dict[str, Any]:
        """Return both identifiers + the right key per subresource."""
        sid = patient_rec["patient_id"]   # string, e.g. FA-001
        iid = patient_rec["id"]           # int internal id
        return {
            "patient_id_str": sid,
            "internal_id": iid,
            "diagnoses": sid,
            "coverage": sid,
            "notes": iid,
            "assessments": iid,
        }

    # --- validation --------------------------------------------------------
    def validate(self, resource_type: str, payload: Any) -> ValidationResult:
        model = _MODELS.get(resource_type)
        if model is None:
            return ValidationResult(True, False, 0, [], [], [f"no schema for {resource_type}"])

        # Contract requires a JSON array for these endpoints.
        if payload is None:
            return ValidationResult(True, True, 0, [], [], ["null body"])
        if not isinstance(payload, list):
            return ValidationResult(
                False, False, 0, [],
                errors=[f"expected JSON array, got {type(payload).__name__}"],
            )
        if len(payload) == 0:
            return ValidationResult(True, True, 0, [], [], [])

        records: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []
        known = set(model.model_fields.keys())

        for i, item in enumerate(payload):
            if not isinstance(item, dict):
                errors.append(f"record[{i}] is not an object")
                continue
            try:
                model(**item)  # validates required fields + types
            except ValidationError as exc:
                errors.append(f"record[{i}]: {exc.errors()[0].get('msg','invalid')}")
                continue
            unknown = set(item.keys()) - known
            if unknown:
                # Contract drift: surface it, but keep the record (forward-compat).
                warnings.append(f"record[{i}] unknown fields: {sorted(unknown)}")
            records.append(item)

        # If every record failed validation, quarantine the payload.
        ok = len(records) > 0 or len(errors) == 0
        return ValidationResult(
            ok=ok, is_empty=False, n_records=len(records),
            records=records, errors=errors, warnings=warnings,
        )


def get_adapter(version: str, base_url: str) -> PccAdapterV1:
    if version in ("pcc_v1", PccAdapterV1.version):
        return PccAdapterV1(base_url)
    raise ValueError(f"unknown EHR adapter version: {version!r}")
