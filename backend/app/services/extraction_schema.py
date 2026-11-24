"""Pydantic models for what the LLM must return (validated on every response)."""
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldOut(_Strict):
    value: str | None
    confidence: float = Field(ge=0, le=1)
    source_snippet: str | None


class LineItemOut(_Strict):
    description: str | None
    quantity: str | None
    unit_price: str | None
    line_total: str | None
    confidence: float = Field(ge=0, le=1)
    source_snippet: str | None

    @field_validator("quantity", "unit_price", "line_total")
    @classmethod
    def _plain_decimal(cls, v: str | None) -> str | None:
        if v is not None and not _DECIMAL.match(v):
            raise ValueError(f"must be a plain decimal like 1250.50, got {v!r}")
        return v


class LLMExtraction(_Strict):
    invoice_number: FieldOut
    invoice_date: FieldOut
    supplier_name: FieldOut
    customer_name: FieldOut
    subtotal: FieldOut
    tax: FieldOut
    total: FieldOut
    currency: FieldOut
    line_items: list[LineItemOut]
    multiple_invoices_detected: bool

    @field_validator("subtotal", "tax", "total")
    @classmethod
    def _money_plain_decimal(cls, f: FieldOut) -> FieldOut:
        if f.value is not None and not _DECIMAL.match(f.value):
            raise ValueError(f"must be a plain decimal like 1250.50, got {f.value!r}")
        return f


SCALAR_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                 "subtotal", "tax", "total", "currency")
MONEY_FIELDS = ("subtotal", "tax", "total")
