# 🧾 Fiscal Document Intelligence

> Automated data extraction from Italian invoices and receipts — built for accountancy firms tired of manual data entry.

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![Claude AI](https://img.shields.io/badge/Claude-Sonnet_4.6-orange?logo=anthropic)](https://anthropic.com)
[![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

---

## The Problem

Accountants in Italy manually type data from hundreds of invoices every month — supplier name, VAT number (*Partita IVA*), totals, dates. It's slow, error-prone, and expensive.

This microservice eliminates that entirely.

---

## What It Does

Upload a PDF invoice → get back structured JSON data, ready to insert into any database or ERP system.

```bash
curl -X POST http://localhost:8000/api/v1/documents/analyze \
  -H "X-API-Key: your-key" \
  -F "file=@fattura.pdf"
```

```json
{
  "status": "completed",
  "overall_confidence": 0.97,
  "review_status": "auto_approved",
  "processing_time_ms": 2341,
  "data": {
    "supplier_name":   { "value": "Acme S.r.l.",        "confidence": 0.99 },
    "supplier_vat_id": { "value": "IT12345678901",      "confidence": 0.99 },
    "invoice_number":  { "value": "FT/2024/001234",     "confidence": 0.99 },
    "invoice_date":    { "value": "2024-03-15",         "confidence": 0.97 },
    "total_amount":    { "value": "1220.00",            "confidence": 0.99 },
    "vat_amount":      { "value": "220.00",             "confidence": 0.99 },
    "currency":        { "value": "EUR",                "confidence": 1.00 }
  }
}
```

---

## How It Works

The system uses a **three-tier extraction pipeline** — it automatically chooses the best strategy for each document:

```
PDF uploaded
     │
     ▼
┌─────────────────────┐
│ Tier 1: Text Layer  │  Fast. Reads the PDF's built-in text directly.
│ (pdfplumber)        │  Works for digital-native invoices.
└──────────┬──────────┘
           │ fails or low confidence
           ▼
┌─────────────────────┐
│ Tier 2: AI Vision   │  Sends page images to Claude AI.
│ (Claude Sonnet)     │  Handles scanned documents, complex layouts,
└──────────┬──────────┘  stamps, handwritten annotations.
           │ fails
           ▼
┌─────────────────────┐
│ Tier 3: OCR         │  Tesseract as last resort.
│ (Tesseract)         │  Ensures no document is ever unprocessable.
└─────────────────────┘
           │
           ▼
   Confidence scoring
   per field (0.0–1.0)
           │
     ┌─────┴──────┐
     ▼            ▼
confidence     confidence
  ≥ 0.75         < 0.75
     │              │
Auto-approved   Flagged for
→ straight      human review
  to database
```

Every extracted field carries its own confidence score. Low-confidence fields are automatically routed to a human review queue — the system never silently inserts bad data.

---

## Key Features

| Feature | Details |
|---|---|
| **Multi-strategy extraction** | Text layer → AI vision → OCR fallback |
| **Per-field confidence scoring** | Every value has a `confidence` score from 0 to 1 |
| **Human review routing** | Fields below threshold are flagged automatically |
| **Async job processing** | Submit and poll — no HTTP timeouts on large documents |
| **Italian VAT validation** | Partita IVA format enforced (IT + 11 digits) |
| **Full audit trail** | Model used, tokens consumed, processing time logged per document |
| **Docker-ready** | Dockerfile + docker-compose included |
| **Interactive API docs** | Auto-generated Swagger UI at `/docs` |

---

## Tech Stack

- **[FastAPI](https://fastapi.tiangolo.com)** — async Python web framework
- **[Claude Sonnet (Anthropic)](https://anthropic.com)** — multimodal AI for document understanding
- **[pdfplumber](https://github.com/jsvine/pdfplumber)** — PDF text and table extraction
- **[PyMuPDF](https://pymupdf.readthedocs.io)** — PDF-to-image rendering for vision analysis
- **[Pydantic v2](https://docs.pydantic.dev)** — data validation and typed schemas
- **Docker + docker-compose** — containerised deployment

---

## Quick Start

**Requirements:** Python 3.12+, an [Anthropic API key](https://console.anthropic.com)

```bash
# 1. Clone the repo
git clone https://github.com/argurianaalija/fiscal-document-intelligence.git
cd fiscal-document-intelligence

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Open .env and add your ANTHROPIC_API_KEY

# 4. Run
uvicorn app.main:app --reload

# 5. Open the interactive docs
open http://localhost:8000/docs
```

### Docker

```bash
cd docker
docker-compose up
```

---

## API Reference

### `POST /api/v1/documents/analyze` — Synchronous
Upload a document and get results immediately. Best for testing and small documents.

### `POST /api/v1/documents/analyze/async` — Async (recommended for production)
Returns a `job_id` immediately. Poll the result when ready — no timeouts.

### `GET /api/v1/jobs/{job_id}` — Poll job status
```json
{ "status": "completed", "result": { ... } }
```

### `GET /health` — Health check

Full interactive documentation available at `/docs` when the server is running.

---

## Project Structure

```
fiscal-document-intelligence/
├── app/
│   ├── main.py                      # FastAPI app and middleware
│   ├── api/routes/
│   │   ├── documents.py             # Upload and extraction endpoints
│   │   ├── jobs.py                  # Async job polling
│   │   └── health.py               # Health and readiness checks
│   ├── core/
│   │   ├── config.py                # Environment-based configuration (12-factor)
│   │   ├── security.py              # API key authentication
│   │   └── logging.py              # Structured logging
│   ├── models/
│   │   └── schemas.py               # All Pydantic models and enums
│   └── services/
│       └── extraction_engine.py     # Multi-tier extraction pipeline
├── tests/
│   └── test_extraction.py           # Unit and integration tests
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Author

**Alija Arguriana** · [github.com/argurianaalija](https://github.com/argurianaalija)

---

## License

MIT
