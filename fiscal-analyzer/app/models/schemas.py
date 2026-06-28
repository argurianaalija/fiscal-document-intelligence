"""
Fiscal Document Schemas
========================
Pydantic v2 models defining the complete data contract.

Design principles:
- Every extracted field carries its own confidence score
- Fields are Optional by default — partial extraction is better than failure
- The InvoiceData model is the single source of truth used by all downstream systems
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enums ─────────────────────────────────────────────────────────────────────

class DocumentType(str, Enum):
    INVOICE = "invoice"
    RECEIPT = "receipt"
    CREDIT_NOTE = "credit_note"
    PROFORMA = "proforma"
    UNKNOWN = "unknown"


class ExtractionStrategy(str, Enum):
    """Which extraction path was used — useful for debugging and auditing."""
    TEXT_LAYER = "text_layer"   # Direct PDF text extraction (fastest, most accurate)
    AI_VISION = "ai_vision"     # Claude multimodal analysis
    OCR_FALLBACK = "ocr_fallback"  # Tesseract OCR on rasterized pages


class ReviewStatus(str, Enum):
    AUTO_APPROVED = "auto_approved"   # All fields above confidence threshold
    NEEDS_REVIEW = "needs_review"     # One or more fields below threshold
    REJECTED = "rejected"             # Extraction failed entirely


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Extracted Field (with confidence) ─────────────────────────────────────────

class ExtractedField(BaseModel):
    """
    A single extracted value paired with the model's confidence.
    
    confidence: float in [0, 1]
        < 0.5  → likely wrong, do not use
        0.5–0.75 → uncertain, route to human review
        > 0.75 → reliable for automated processing
    """
    value: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    source_hint: Optional[str] = Field(
        None,
        description="Snippet of source text where this was found, for traceability"
    )

    @property
    def is_reliable(self) -> bool:
        return self.value is not None and self.confidence >= 0.75

    @property
    def needs_review(self) -> bool:
        return self.value is not None and self.confidence < 0.75


# ── Line Items ─────────────────────────────────────────────────────────────────

class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    vat_rate: Optional[Decimal] = Field(None, description="VAT rate as percentage, e.g. 22.0 for 22%")
    subtotal: Optional[Decimal] = None


# ── Core Invoice Data ──────────────────────────────────────────────────────────

class InvoiceData(BaseModel):
    """
    Structured representation of an extracted fiscal document.
    
    All monetary values are Decimal to avoid floating-point errors in accounting contexts.
    Italian-specific fields (partita IVA, codice fiscale) are included as first-class fields.
    """

    # ── Document identity ──────────────────────────────────────────────────
    document_type: ExtractedField = Field(default_factory=ExtractedField)
    invoice_number: ExtractedField = Field(default_factory=ExtractedField)
    invoice_date: ExtractedField = Field(default_factory=ExtractedField, description="ISO 8601 date string")
    due_date: ExtractedField = Field(default_factory=ExtractedField)

    # ── Supplier (Fornitore) ───────────────────────────────────────────────
    supplier_name: ExtractedField = Field(default_factory=ExtractedField)
    supplier_address: ExtractedField = Field(default_factory=ExtractedField)
    supplier_vat_id: ExtractedField = Field(
        default_factory=ExtractedField,
        description="Partita IVA — Italian format: IT + 11 digits"
    )
    supplier_fiscal_code: ExtractedField = Field(
        default_factory=ExtractedField,
        description="Codice Fiscale del fornitore"
    )
    supplier_iban: ExtractedField = Field(default_factory=ExtractedField)

    # ── Customer (Cliente) ─────────────────────────────────────────────────
    customer_name: ExtractedField = Field(default_factory=ExtractedField)
    customer_vat_id: ExtractedField = Field(default_factory=ExtractedField)

    # ── Financials ─────────────────────────────────────────────────────────
    subtotal_amount: ExtractedField = Field(default_factory=ExtractedField, description="Amount before VAT")
    vat_amount: ExtractedField = Field(default_factory=ExtractedField, description="Total VAT amount")
    total_amount: ExtractedField = Field(default_factory=ExtractedField, description="Total including VAT")
    currency: ExtractedField = Field(default_factory=ExtractedField)

    # ── Line items (optional — not always extractable) ─────────────────────
    line_items: list[LineItem] = Field(default_factory=list)

    # ── Additional metadata ────────────────────────────────────────────────
    payment_method: ExtractedField = Field(default_factory=ExtractedField)
    purchase_order_ref: ExtractedField = Field(default_factory=ExtractedField)
    notes: Optional[str] = None

    def overall_confidence(self) -> float:
        """Weighted average confidence across critical fields."""
        critical_fields = [
            (self.invoice_number, 1.0),
            (self.invoice_date, 1.0),
            (self.supplier_name, 1.5),
            (self.supplier_vat_id, 2.0),   # Most important for Italian accounting
            (self.total_amount, 2.0),
            (self.vat_amount, 1.5),
        ]
        total_weight = sum(w for _, w in critical_fields)
        weighted_sum = sum(f.confidence * w for f, w in critical_fields)
        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def fields_needing_review(self) -> list[str]:
        """Return names of fields with confidence below threshold."""
        review_fields = []
        for field_name, field_value in self.model_fields.items():
            val = getattr(self, field_name)
            if isinstance(val, ExtractedField) and val.needs_review:
                review_fields.append(field_name)
        return review_fields

    def to_flat_dict(self) -> dict:
        """
        Flatten to simple key→value dict for database insertion or CSV export.
        Drops confidence metadata — pure extracted values only.
        """
        result = {}
        for field_name in self.model_fields:
            val = getattr(self, field_name)
            if isinstance(val, ExtractedField):
                result[field_name] = val.value
            elif field_name == "line_items":
                result[field_name] = [item.model_dump() for item in val]
            else:
                result[field_name] = val
        return result


# ── API Request / Response models ──────────────────────────────────────────────

class SubmitDocumentResponse(BaseModel):
    job_id: str = Field(description="Use this to poll /jobs/{job_id} for results")
    status: JobStatus = JobStatus.QUEUED
    message: str = "Document queued for processing"
    estimated_seconds: int = Field(default=15, description="Rough processing time estimate")


class ExtractionResult(BaseModel):
    """Complete result returned when a job finishes."""
    job_id: str
    status: JobStatus
    document_name: str
    extraction_strategy: ExtractionStrategy
    page_count: int
    processing_time_ms: float
    overall_confidence: float
    review_status: ReviewStatus
    fields_needing_review: list[str]
    data: InvoiceData
    # Audit
    model_used: str
    tokens_used: Optional[int] = None
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    error: Optional[str] = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress_message: Optional[str] = None
    result: Optional[ExtractionResult] = None
    created_at: datetime
    updated_at: datetime
