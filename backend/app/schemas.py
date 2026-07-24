from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

MISSING_TEXT_VALUES = {"", "na", "n/a", "none", "null", "-", "--"}


def is_missing_value(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in MISSING_TEXT_VALUES


def clean_optional_text(value: object) -> str | None:
    if is_missing_value(value):
        return None
    return str(value).strip()


class ExtractedCatalogItem(BaseModel):
    ingredient_name: str
    normalized_name: str | None = None
    price_per_unit: float | None = None
    currency: str = "INR"
    available_qty: float | None = None
    unit: str | None = None
    valid_until: datetime | None = None
    supplier_sku: str | None = None
    lead_time_days: int | None = None
    lead_time_text: str | None = None
    moq: float | None = None
    notes: str | None = None

    @field_validator("price_per_unit", mode="before")
    @classmethod
    def validate_price_per_unit(cls, v):
        if isinstance(v, bool):
            raise ValueError("price_per_unit cannot be boolean")
        if is_missing_value(v):
            return None
        return v

    @field_validator("available_qty", mode="before")
    @classmethod
    def validate_available_qty(cls, v):
        if isinstance(v, bool):
            return None
        if is_missing_value(v):
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    @field_validator("moq", mode="before")
    @classmethod
    def validate_moq(cls, v):
        if isinstance(v, bool):
            return None
        if is_missing_value(v):
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    @field_validator("lead_time_days", mode="before")
    @classmethod
    def validate_lead_time_days(cls, v):
        if isinstance(v, bool) or is_missing_value(v):
            return None
        if isinstance(v, str) and any(token in v.lower() for token in ("-", "to", "–")):
            return None
        return v

    @field_validator("unit", "supplier_sku", "lead_time_text", "notes", mode="before")
    @classmethod
    def validate_optional_text(cls, v):
        return clean_optional_text(v)

    @field_validator("currency", mode="before")
    @classmethod
    def validate_currency(cls, v):
        cleaned = clean_optional_text(v)
        return cleaned.upper() if cleaned else "INR"



class CatalogIngestionResult(BaseModel):
    supplier_name: str
    supplier_email: str
    subject: str | None = None
    received_at: datetime
    items: list[ExtractedCatalogItem]


class QueryPlan(BaseModel):
    operation: str = Field(pattern="^(supplier_compare|best_price|catalog_search|history_compare|supplier_activity|unrelated)$")
    normalized_name: str | None = None
    min_quantity: float | None = None
    unit: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = Field(default=10, ge=1, le=50)
    semantic_query: str | None = None


class ChatRequest(BaseModel):
    session_id: UUID | None = None
    message: str


class ChatResponse(BaseModel):
    answer: str
    rows: list[dict[str, Any]] = []


class SupplierSummary(BaseModel):
    id: UUID
    name: str
    email_domain: str
    last_email_date: datetime | None = None
    certifications: str | None = None
