"""Request validation for the JSON API."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...data.masterformat import is_specifiable, normalize_code
from ...models.scope import SCOPE_STATUSES, SECTION_KEYS


class ScopeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    division_code: str = Field(..., description="CSI MasterFormat division, e.g. '21'.")
    trade_name: str | None = Field(None, max_length=160)
    title: str = Field("Scope of Work", max_length=255)
    exhibit_label: str = Field("EXHIBIT B", max_length=60)
    project_id: str | None = None
    bid_package_id: str | None = None
    clause_ids: list[str] = Field(default_factory=list)
    spec_section_ids: list[str] = Field(default_factory=list)
    enabled_sections: list[str] | None = None
    numbering_scheme: str = "legal"
    template_id: str | None = None
    base_bid_amount: Decimal | None = None
    currency: str = Field("USD", min_length=3, max_length=3)
    # When true, ignore clause_ids and take the library defaults for the
    # division -- the one-call "just generate me a scope" path.
    use_defaults: bool = False

    @field_validator("division_code")
    @classmethod
    def _check_division(cls, value: str) -> str:
        code = normalize_code(value)
        if not code or not is_specifiable(code):
            raise ValueError(
                f"{value!r} is not a specifiable CSI division. Reserved and "
                "unknown numbers are rejected."
            )
        return code

    @field_validator("numbering_scheme")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        if value not in ("legal", "outline"):
            raise ValueError("numbering_scheme must be 'legal' or 'outline'.")
        return value

    @field_validator("enabled_sections")
    @classmethod
    def _check_sections(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [key for key in value if key not in SECTION_KEYS]
        if unknown:
            raise ValueError(f"Unknown section keys: {', '.join(unknown)}")
        return value

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must be a three-letter code such as USD.")
        return value.upper()


class ScopeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, max_length=255)
    exhibit_label: str | None = Field(None, max_length=60)
    trade_name: str | None = Field(None, max_length=160)
    status: str | None = None
    base_bid_amount: Decimal | None = None
    alternates_amount: Decimal | None = None
    adjustments_amount: Decimal | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str | None) -> str | None:
        if value is not None and value not in SCOPE_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(SCOPE_STATUSES)}")
        return value


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., max_length=255)
    number: str | None = Field(None, max_length=80)
    address: str | None = Field(None, max_length=255)
    city: str | None = Field(None, max_length=120)
    state: str | None = Field(None, max_length=60)
    postal_code: str | None = Field(None, max_length=20)
    owner_name: str | None = Field(None, max_length=255)
    architect_name: str | None = Field(None, max_length=255)
    contractor_name: str | None = Field(None, max_length=255)
    delivery_method: str | None = Field(None, max_length=60)


class BidPackageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(..., max_length=60)
    name: str = Field(..., max_length=255)
    division_code: str | None = None
    trade_name: str | None = Field(None, max_length=160)
    subcontractor_name: str | None = Field(None, max_length=255)
    base_bid_amount: Decimal | None = None

    @field_validator("division_code")
    @classmethod
    def _check_division(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = normalize_code(value)
        if not code:
            raise ValueError(f"{value!r} is not a valid CSI division.")
        return code
