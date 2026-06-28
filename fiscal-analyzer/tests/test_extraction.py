"""
Test Suite
==========
Tests for the extraction engine and API endpoints.
Run with: pytest tests/ -v
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import ExtractedField, InvoiceData
from app.services.extraction_engine import InvoiceDataBuilder


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "dev-key-change-me"}


@pytest.fixture
def sample_ai_response():
    """Realistic AI response for a simple Italian invoice."""
    return {
        "document_type": {"value": "invoice", "confidence": 0.98, "source_hint": "FATTURA"},
        "invoice_number": {"value": "FT/2024/001234", "confidence": 0.99, "source_hint": "N. Fattura: FT/2024/001234"},
        "invoice_date": {"value": "2024-03-15", "confidence": 0.97, "source_hint": "Data: 15/03/2024"},
        "due_date": {"value": "2024-04-15", "confidence": 0.90, "source_hint": "Scadenza: 15/04/2024"},
        "supplier_name": {"value": "Acme S.r.l.", "confidence": 0.99, "source_hint": "Acme S.r.l."},
        "supplier_address": {"value": "Via Roma 1, 20121 Milano MI", "confidence": 0.92, "source_hint": "Via Roma 1, 20121 Milano MI"},
        "supplier_vat_id": {"value": "IT12345678901", "confidence": 0.99, "source_hint": "P.IVA: IT12345678901"},
        "supplier_fiscal_code": {"value": "ACMESRL80A01F205Z", "confidence": 0.85, "source_hint": "C.F.: ACMESRL80A01F205Z"},
        "supplier_iban": {"value": "IT60X0542811101000000123456", "confidence": 0.88, "source_hint": "IBAN: IT60X0542811101000000123456"},
        "customer_name": {"value": "Beta S.p.A.", "confidence": 0.96, "source_hint": "Spett.le Beta S.p.A."},
        "customer_vat_id": {"value": "IT98765432109", "confidence": 0.94, "source_hint": "P.IVA Cliente: IT98765432109"},
        "subtotal_amount": {"value": "1000.00", "confidence": 0.99, "source_hint": "Imponibile: € 1.000,00"},
        "vat_amount": {"value": "220.00", "confidence": 0.99, "source_hint": "IVA 22%: € 220,00"},
        "total_amount": {"value": "1220.00", "confidence": 0.99, "source_hint": "Totale: € 1.220,00"},
        "currency": {"value": "EUR", "confidence": 1.0, "source_hint": "€"},
        "payment_method": {"value": "Bonifico bancario", "confidence": 0.87, "source_hint": "Pagamento: Bonifico bancario"},
        "purchase_order_ref": {"value": None, "confidence": 0.0, "source_hint": None},
        "line_items": [
            {
                "description": "Sviluppo software custom",
                "quantity": "1",
                "unit_price": "1000.00",
                "vat_rate": "22.00",
                "subtotal": "1000.00"
            }
        ],
        "notes": None,
    }


# ── Unit Tests: Data Builder ───────────────────────────────────────────────────

class TestInvoiceDataBuilder:
    def setup_method(self):
        self.builder = InvoiceDataBuilder()

    def test_build_complete_invoice(self, sample_ai_response):
        data = self.builder.build(sample_ai_response)

        assert data.supplier_vat_id.value == "IT12345678901"
        assert data.supplier_vat_id.confidence == 0.99
        assert data.total_amount.value == "1220.00"
        assert data.invoice_number.value == "FT/2024/001234"
        assert len(data.line_items) == 1
        assert data.line_items[0].description == "Sviluppo software custom"

    def test_overall_confidence_high_for_complete_doc(self, sample_ai_response):
        data = self.builder.build(sample_ai_response)
        assert data.overall_confidence() >= 0.95

    def test_missing_field_returns_empty_extracted_field(self):
        data = self.builder.build({})
        assert data.supplier_vat_id.value is None
        assert data.supplier_vat_id.confidence == 0.0

    def test_fields_needing_review_detects_low_confidence(self):
        response = {
            "supplier_vat_id": {"value": "IT12345678901", "confidence": 0.4, "source_hint": None},
            "total_amount": {"value": "500.00", "confidence": 0.3, "source_hint": None},
        }
        data = self.builder.build(response)
        review_fields = data.fields_needing_review()
        assert "supplier_vat_id" in review_fields
        assert "total_amount" in review_fields

    def test_to_flat_dict_returns_values_only(self, sample_ai_response):
        data = self.builder.build(sample_ai_response)
        flat = data.to_flat_dict()
        assert flat["supplier_vat_id"] == "IT12345678901"
        assert flat["total_amount"] == "1220.00"
        # No confidence metadata in flat dict
        assert "confidence" not in str(flat)

    def test_extracted_field_is_reliable(self):
        field = ExtractedField(value="IT12345678901", confidence=0.99)
        assert field.is_reliable is True

    def test_extracted_field_needs_review(self):
        field = ExtractedField(value="IT12345?78901", confidence=0.45)
        assert field.needs_review is True
        assert field.is_reliable is False

    def test_null_value_is_not_reliable(self):
        field = ExtractedField(value=None, confidence=0.0)
        assert field.is_reliable is False
        assert field.needs_review is False  # None != uncertain


# ── Integration Tests: API ─────────────────────────────────────────────────────

class TestHealthEndpoints:
    def test_health_check(self, client):
        response = client.get("/health/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data

    def test_readiness_check(self, client):
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert "ready" in data
        assert "checks" in data


class TestDocumentEndpoints:
    def test_analyze_requires_api_key(self, client):
        response = client.post("/api/v1/documents/analyze")
        assert response.status_code == 401

    def test_analyze_rejects_non_pdf(self, client, auth_headers):
        response = client.post(
            "/api/v1/documents/analyze",
            files={"file": ("test.txt", b"not a pdf", "text/plain")},
            headers=auth_headers,
        )
        assert response.status_code == 415

    @patch("app.services.extraction_engine.AIVisionExtractor.extract_from_text")
    @patch("app.services.extraction_engine.PDFTextExtractor.extract")
    def test_analyze_pdf_success(self, mock_extract, mock_ai, client, auth_headers, sample_ai_response):
        # Mock PDF text extraction
        mock_extract.return_value = ("Fattura n. FT/2024/001234\nTotale: € 1.220,00", 1)
        # Mock AI response
        mock_ai.return_value = (sample_ai_response, 1250)

        # Create a minimal valid PDF bytes
        minimal_pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n%%EOF"

        response = client.post(
            "/api/v1/documents/analyze",
            files={"file": ("fattura.pdf", minimal_pdf, "application/pdf")},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["data"]["supplier_vat_id"]["value"] == "IT12345678901"
        assert data["data"]["total_amount"]["value"] == "1220.00"
        assert data["overall_confidence"] >= 0.90
        assert data["tokens_used"] == 1250

    def test_jobs_404_for_unknown_job(self, client):
        response = client.get("/api/v1/jobs/nonexistent-job-id")
        assert response.status_code == 404

    def test_async_submit_returns_job_id(self, client, auth_headers):
        """Verify async endpoint immediately returns a job_id without blocking."""
        minimal_pdf = b"%PDF-1.4\n%%EOF"

        with patch("app.api.routes.documents._process_job"):
            response = client.post(
                "/api/v1/documents/analyze/async",
                files={"file": ("test.pdf", minimal_pdf, "application/pdf")},
                headers=auth_headers,
            )

        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "queued"
        assert len(data["job_id"]) == 36  # UUID format
