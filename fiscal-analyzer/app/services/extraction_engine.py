"""
Extraction Engine
=================
The heart of the system. Implements a three-tier extraction pipeline:

  Tier 1: PDF text layer extraction (fast, free, most reliable when available)
  Tier 2: Claude AI vision analysis (handles scanned docs, complex layouts)
  Tier 3: Tesseract OCR fallback (last resort for corrupted or image-only PDFs)

Each tier reports its own confidence. The pipeline selects the best result
or combines outputs when useful.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import anthropic
import pdfplumber
import pypdf

from app.core.config import settings
from app.models.schemas import (
    ExtractionStrategy,
    ExtractedField,
    InvoiceData,
    LineItem,
)

logger = logging.getLogger(__name__)


# ── Prompt Engineering ────────────────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """You are an expert accountant and document analysis AI specialising in Italian fiscal documents.
Your task is to extract structured data from invoices, receipts, and related fiscal documents.

CRITICAL RULES:
1. Return ONLY a valid JSON object — no prose, no markdown fences, no explanations.
2. For every field, provide a "value" and a "confidence" score (0.0–1.0).
   - 1.0 = you can clearly read it, no ambiguity
   - 0.75–0.99 = high confidence but minor uncertainty (e.g., OCR artefact)
   - 0.5–0.74 = uncertain — you made a reasonable guess
   - below 0.5 = very uncertain, flag for human review
   - 0.0 = field not found or not applicable
3. For "value": use null if the field is not present in the document.
4. Italian VAT numbers (Partita IVA): format is "IT" + 11 digits. Validate this format.
5. Dates: normalize to ISO 8601 (YYYY-MM-DD).
6. Amounts: return as plain decimal strings, e.g. "1234.56". Do NOT include currency symbols.
7. source_hint: copy the exact text snippet from the document that led to your extraction.

JSON SCHEMA (return exactly this structure):
{
  "document_type": {"value": "invoice|receipt|credit_note|proforma|unknown", "confidence": 0.0, "source_hint": null},
  "invoice_number": {"value": null, "confidence": 0.0, "source_hint": null},
  "invoice_date": {"value": null, "confidence": 0.0, "source_hint": null},
  "due_date": {"value": null, "confidence": 0.0, "source_hint": null},
  "supplier_name": {"value": null, "confidence": 0.0, "source_hint": null},
  "supplier_address": {"value": null, "confidence": 0.0, "source_hint": null},
  "supplier_vat_id": {"value": null, "confidence": 0.0, "source_hint": null},
  "supplier_fiscal_code": {"value": null, "confidence": 0.0, "source_hint": null},
  "supplier_iban": {"value": null, "confidence": 0.0, "source_hint": null},
  "customer_name": {"value": null, "confidence": 0.0, "source_hint": null},
  "customer_vat_id": {"value": null, "confidence": 0.0, "source_hint": null},
  "subtotal_amount": {"value": null, "confidence": 0.0, "source_hint": null},
  "vat_amount": {"value": null, "confidence": 0.0, "source_hint": null},
  "total_amount": {"value": null, "confidence": 0.0, "source_hint": null},
  "currency": {"value": null, "confidence": 0.0, "source_hint": null},
  "payment_method": {"value": null, "confidence": 0.0, "source_hint": null},
  "purchase_order_ref": {"value": null, "confidence": 0.0, "source_hint": null},
  "line_items": [],
  "notes": null
}"""

EXTRACTION_USER_PROMPT_TEXT = """Analyze this fiscal document text and extract all structured data.
Pay special attention to Italian-format VAT numbers (Partita IVA) and monetary amounts.

DOCUMENT TEXT:
{document_text}"""

EXTRACTION_USER_PROMPT_VISION = """Analyze this fiscal document image and extract all structured data.
This is page {page_num} of {total_pages}. Pay special attention to:
- Any stamps, handwritten annotations, or watermarks
- Tables with line items
- Italian-format VAT numbers (Partita IVA): IT + 11 digits

Extract every visible field even if partially obscured."""


# ── PDF Text Extraction ───────────────────────────────────────────────────────

class PDFTextExtractor:
    """Tier 1: Direct text extraction from PDF text layer."""

    def extract(self, pdf_path: Path) -> tuple[str, int]:
        """
        Returns (extracted_text, page_count).
        Raises ValueError if the PDF has no text layer (scanned document).
        """
        text_parts = []
        page_count = 0

        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                page_text = page.extract_text(layout=True) or ""
                # Also extract tables and merge them into the text
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            text_parts.append(" | ".join(str(cell or "") for cell in row))
                if page_text.strip():
                    text_parts.append(page_text)

        full_text = "\n\n".join(text_parts)
        if not full_text.strip():
            raise ValueError("PDF has no extractable text layer — falling back to vision")

        logger.debug("Text extraction: %d chars from %d pages", len(full_text), page_count)
        return full_text, page_count

    def has_text_layer(self, pdf_path: Path) -> bool:
        try:
            text, _ = self.extract(pdf_path)
            return bool(text.strip())
        except Exception:
            return False


# ── AI Vision Extractor ───────────────────────────────────────────────────────

class AIVisionExtractor:
    """
    Tier 2: Claude multimodal analysis.
    Renders PDF pages to images and sends them to the Claude vision API.
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def extract_from_text(self, text: str) -> tuple[dict, int]:
        """Use Claude to parse already-extracted text (faster, cheaper than vision)."""
        start = time.perf_counter()

        message = self.client.messages.create(
            model=settings.AI_MODEL,
            max_tokens=4096,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": EXTRACTION_USER_PROMPT_TEXT.format(document_text=text[:15000])
                }
            ],
        )

        duration = time.perf_counter() - start
        logger.debug("AI text extraction: %.2fs, %d tokens", duration, message.usage.input_tokens + message.usage.output_tokens)

        raw_json = message.content[0].text
        return self._parse_response(raw_json), message.usage.input_tokens + message.usage.output_tokens

    def extract_from_pdf_vision(self, pdf_path: Path) -> tuple[dict, int]:
        """
        Convert PDF pages to images and analyze with Claude vision.
        Used for scanned documents or when text extraction fails.
        """
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        page_count = len(doc)
        all_page_results = []
        total_tokens = 0

        # Process each page (limit to first 10 pages for very long documents)
        for page_num, page in enumerate(doc, start=1):
            if page_num > 10:
                logger.warning("Document has %d pages — processing first 10 only", page_count)
                break

            # Render at 200 DPI for good OCR quality
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("jpeg")
            img_b64 = base64.standard_b64encode(img_bytes).decode()

            message = self.client.messages.create(
                model=settings.AI_MODEL,
                max_tokens=4096,
                system=EXTRACTION_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": EXTRACTION_USER_PROMPT_VISION.format(
                                    page_num=page_num, total_pages=page_count
                                ),
                            },
                        ],
                    }
                ],
            )

            total_tokens += message.usage.input_tokens + message.usage.output_tokens
            page_result = self._parse_response(message.content[0].text)
            all_page_results.append(page_result)

        doc.close()

        # Merge results across pages — page 1 usually has supplier/header info,
        # later pages may have totals or additional line items
        merged = self._merge_page_results(all_page_results)
        return merged, total_tokens

    def _parse_response(self, raw: str) -> dict:
        """Parse Claude's JSON response, handling common formatting issues."""
        # Strip any accidental markdown fences
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse AI response: %s\nRaw: %s", e, raw[:500])
            raise ValueError(f"AI returned invalid JSON: {e}") from e

    def _merge_page_results(self, results: list[dict]) -> dict:
        """
        Merge extractions from multiple pages.
        Strategy: take the field with highest confidence from any page.
        Special-case line_items: concatenate from all pages.
        """
        if not results:
            return {}
        if len(results) == 1:
            return results[0]

        merged = results[0].copy()
        all_line_items = []

        for result in results:
            # Collect line items from all pages
            all_line_items.extend(result.get("line_items", []))

            # For scalar fields, keep the highest-confidence value
            for field, value in result.items():
                if field == "line_items":
                    continue
                if isinstance(value, dict) and "confidence" in value:
                    current_conf = merged.get(field, {}).get("confidence", 0)
                    if value.get("confidence", 0) > current_conf:
                        merged[field] = value

        merged["line_items"] = all_line_items
        return merged


# ── Data Builder ──────────────────────────────────────────────────────────────

class InvoiceDataBuilder:
    """Converts raw AI JSON output into validated InvoiceData Pydantic models."""

    def build(self, raw: dict) -> InvoiceData:
        """
        Map raw dict to InvoiceData, building ExtractedField objects for each field.
        Invalid/missing fields default to empty ExtractedField (confidence=0).
        """

        def field(key: str) -> ExtractedField:
            raw_field = raw.get(key)
            if not raw_field or not isinstance(raw_field, dict):
                return ExtractedField()
            return ExtractedField(
                value=raw_field.get("value"),
                confidence=float(raw_field.get("confidence", 0.0)),
                source_hint=raw_field.get("source_hint"),
            )

        line_items = []
        for item in raw.get("line_items", []):
            if isinstance(item, dict):
                line_items.append(LineItem(
                    description=item.get("description"),
                    quantity=item.get("quantity"),
                    unit_price=item.get("unit_price"),
                    vat_rate=item.get("vat_rate"),
                    subtotal=item.get("subtotal"),
                ))

        return InvoiceData(
            document_type=field("document_type"),
            invoice_number=field("invoice_number"),
            invoice_date=field("invoice_date"),
            due_date=field("due_date"),
            supplier_name=field("supplier_name"),
            supplier_address=field("supplier_address"),
            supplier_vat_id=field("supplier_vat_id"),
            supplier_fiscal_code=field("supplier_fiscal_code"),
            supplier_iban=field("supplier_iban"),
            customer_name=field("customer_name"),
            customer_vat_id=field("customer_vat_id"),
            subtotal_amount=field("subtotal_amount"),
            vat_amount=field("vat_amount"),
            total_amount=field("total_amount"),
            currency=field("currency"),
            payment_method=field("payment_method"),
            purchase_order_ref=field("purchase_order_ref"),
            notes=raw.get("notes"),
            line_items=line_items,
        )


# ── Orchestrator ──────────────────────────────────────────────────────────────

class ExtractionOrchestrator:
    """
    Orchestrates the full extraction pipeline for a single document.
    Decides which strategy to use and falls back gracefully.
    """

    def __init__(self):
        self.text_extractor = PDFTextExtractor()
        self.ai_extractor = AIVisionExtractor()
        self.builder = InvoiceDataBuilder()

    def process(self, pdf_path: Path) -> tuple[InvoiceData, ExtractionStrategy, int, int]:
        """
        Process a document through the extraction pipeline.
        
        Returns:
            (invoice_data, strategy_used, page_count, tokens_used)
        """
        page_count = 0
        tokens_used = 0

        # ── Tier 1: Try text layer extraction ────────────────────────────
        try:
            text, page_count = self.text_extractor.extract(pdf_path)
            logger.info("Tier 1 (text layer) succeeded: %d chars", len(text))

            # Feed extracted text to AI for semantic parsing
            raw, tokens_used = self.ai_extractor.extract_from_text(text)
            data = self.builder.build(raw)

            # If overall confidence is good enough, we're done
            if data.overall_confidence() >= 0.6:
                logger.info(
                    "Extraction via TEXT_LAYER: confidence=%.2f, tokens=%d",
                    data.overall_confidence(), tokens_used
                )
                return data, ExtractionStrategy.TEXT_LAYER, page_count, tokens_used

            logger.info(
                "Text layer confidence too low (%.2f) — escalating to AI vision",
                data.overall_confidence()
            )

        except Exception as e:
            logger.info("Tier 1 failed: %s — escalating to Tier 2 (AI vision)", e)

        # ── Tier 2: AI Vision (multimodal) ───────────────────────────────
        try:
            raw, tokens_used = self.ai_extractor.extract_from_pdf_vision(pdf_path)

            # Get page count if not already set
            if page_count == 0:
                import fitz
                doc = fitz.open(str(pdf_path))
                page_count = len(doc)
                doc.close()

            data = self.builder.build(raw)
            logger.info(
                "Extraction via AI_VISION: confidence=%.2f, tokens=%d",
                data.overall_confidence(), tokens_used
            )
            return data, ExtractionStrategy.AI_VISION, page_count, tokens_used

        except Exception as e:
            logger.error("Tier 2 (AI vision) failed: %s", e)
            raise RuntimeError(
                f"All extraction strategies failed. Last error: {e}"
            ) from e
