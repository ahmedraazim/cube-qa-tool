# CUBE New-Book QA Automation Tool

A complete, runnable Python/Streamlit application that automates CUBE's New Book
QA workflow. It performs all 12 business checks against a captured XML file,
generates full audit reports, and provides actionable routing decisions.

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- pip

Optional but recommended for full functionality:
- `tesseract-ocr` (for scanned PDF sources)
- `playwright` (for JavaScript-heavy source URLs)
- Anthropic API key (for translation quality checks)

### 2. Install dependencies

```bash
cd cube_qa
pip install -r requirements.txt
```

> **Note**: `streamlit`, `langdetect`, `pytesseract`, and `anthropic` are
> optional — the tool runs without them, with affected checks showing
> "Unverified" where the capability is absent.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in any credentials you have
```

### 4. Launch the Streamlit UI

```bash
streamlit run app.py
```

Then open `http://localhost:8501` in your browser.

### 5. Run from the command line (no Streamlit required)

```bash
python -c "
from pathlib import Path
from modules.qa_engine import QARunner
from modules.report_generator import generate_html

runner = QARunner(
    xml_path=Path('sample_data/DE--DE--REG--22A6E807-6910-42AB-A752-C1DF1943EEF2_v1.xml'),
    xsd_path=Path('sample_data/schema.xsd.txt'),
)
for event in runner.run():
    print(f'[{event.status}] {event.stage}: {event.message[:80]}')
"
```

---

## Running the Tests

```bash
cd cube_qa
python -m unittest tests.test_qa_checks -v
```

All 43 tests cover the 16 acceptance criteria from §13 of the requirements.
They use real check functions with controlled fixtures — no hardcoded results.

**Expected output**: 43 tests, 0 failures.

---

## Project Layout

```
cube_qa/
├── app.py                        # Streamlit UI entry point
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment variable template
├── config.yaml                   # QA rules configuration
├── modules/
│   ├── models.py                 # Shared data models (ParsedXML, CheckResult, …)
│   ├── config_loader.py          # YAML + env var configuration
│   ├── xml_parser.py             # Secure XML parsing (XXE-disabled) + XSD validation
│   ├── source_retriever.py       # HTTP retrieval with SSRF protection
│   ├── content_comparator.py     # Text diff, number/date fidelity, section alignment
│   ├── qa_engine.py              # 10-stage pipeline orchestrator
│   ├── rm_adapter.py             # Read-only RM integration adapter
│   ├── report_generator.py       # HTML, JSON, CSV report generation
│   ├── routing.py                # Decision and routing logic
│   └── checks/
│       ├── check_01_book_name.py
│       ├── check_02_citation.py
│       ├── check_03_issuance_type.py
│       ├── check_04_05_source_url.py
│       ├── check_06_toc_structure.py
│       ├── check_07_content_fidelity.py
│       └── check_08_12.py        # Checks 8–12
├── sample_data/
│   ├── DE--DE--REG--22A6E807-…_v1.xml   # Sample book XML
│   ├── schema.xsd.txt                    # XSD schema
│   ├── sample_audit_report.html          # Real audit output
│   ├── sample_audit_report.json
│   └── sample_audit_report.csv
└── tests/
    └── test_qa_checks.py          # 43 acceptance tests
```

---

## The 12 Business Checks

| # | Check | Routes To |
|---|-------|-----------|
| 1 | Book name — English, allowed characters | Data Management |
| 2 | Citation — not blank, correct fallback | Data Management |
| 3 | Issuance Type — vocabulary + title match | Data Management |
| 4 | Source URL — presence and validity | Research |
| 5 | Source URL — broken vs. geo-blocked | Research |
| 6 | TOC / structure — hierarchy comparison | Data Management |
| 7 | Content fidelity — word-for-word comparison | Research |
| 8 | Translation sanity — coherence check | Research |
| 9 | Styling exclusion — informational only | (never fails) |
| 10 | Placeholder metadata — DEFAULT detection | Data Management |
| 11 | Issuance Date — source-verified match | Research |
| 12 | Final sweep — images, footnotes, duplicates | Research |

---

## RM Integration

The RM adapter interface is defined in `modules/rm_adapter.py`. The live
integration is **explicitly unconfigured** — no RM API documentation or
credentials were available during development.

Supported modes:
1. **XML-only** (always available) — checks metadata extracted directly from XML.
2. **RM export JSON** — upload a JSON export from RM as a fallback.
3. **Live RM** (unconfigured) — adapter interface ready; implement `connect()`
   when API documentation is available.

The adapter contains **no write operations**. It cannot submit, approve, reject,
publish, or route anything in RM.

---

## Configuration

Key settings in `config.yaml`:

```yaml
qa:
  allowed_book_name_chars: "a-zA-Z0-9 .,/()\\-'"
  issuance_type_vocabulary:
    - Regulation
    - Notice
    - Circular
    - Resolution
    # …add more as needed

source_retrieval:
  timeout_seconds: 30
  max_retries: 2
  max_file_size_mb: 50

ai:
  enabled: false          # set true if ANTHROPIC_API_KEY is set
  model: claude-sonnet-4-6
```

Override any value via environment variables (see `.env.example`).

---

## Sample Audit Results

Running the tool on `DE--DE--REG--22A6E807-6910-42AB-A752-C1DF1943EEF2_v1.xml`
produces the following findings (source URL could not be retrieved in this
environment — remote BCB.gov.br access is network-restricted here):

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | Book Name | Unverified | `langdetect` not installed; cannot confirm language |
| 2 | Citation | **Pass** | `"Document Title as Citation"` — accepted fallback |
| 3 | Issuance Type | **FAIL** | `<IssuanceType>` is blank |
| 4 | Source URL Validity | Unverified | URL present but retrieval blocked in this environment |
| 5 | Broken vs. Blocked | Unverified | Retrieval failed; cannot distinguish broken vs. geo-blocked |
| 6 | TOC / Structure | Unverified | Source not retrieved; cannot compare structure |
| 7 | Content Fidelity | Unverified | Source not retrieved; comparison impossible |
| 8 | Translation Sanity | Unverified | No translation document provided |
| 9 | Styling Exclusion | Informational | No styling failures applicable |
| 10 | Placeholder Metadata | **FAIL** | `issuing-body="DEFAULT"`, `country-of-issue="DEFAULT"` |
| 11 | Issuance Date | **FAIL** | `IssueDateStatus="Date Replaced By Capture Date"` |
| 12 | Final Sweep | Pass | No images, footnotes, or duplication issues detected |

**Decision: FAIL**
**Primary Route: Data Management** (Issuance Type + Placeholder Metadata)
**Secondary Route: Research** (Issuance Date)

See `sample_data/sample_audit_report.html` for the full formatted report.

---

## Known Limitations

See `ARCHITECTURE.md` for a full list. Key ones:

1. **Source retrieval** requires network access — URLs that are geo-restricted
   or require authentication show as Unverified.
2. **Language detection** requires `langdetect` — Check 1 is Unverified without it.
3. **OCR** requires `tesseract-ocr` system binary — scanned sources need it.
4. **Translation check** requires Anthropic API key — Check 8 is Unverified without it.
5. **RM live integration** is unimplemented — export-based mode available.

---

## Security Notes

- XML is parsed with XXE disabled (`resolve_entities=False, no_network=True`).
- Source fetching blocks RFC 1918 private addresses; only `ALLOWED_INTERNAL_HOSTS`
  may bypass this restriction.
- No credentials appear in reports or logs.
- No uploaded file is executed.
- AI calls are opt-in and configurable.
