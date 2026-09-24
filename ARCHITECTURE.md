# CUBE QA Tool — Architecture

## Overview

The tool is a local Python application with a Streamlit front end. All
network requests, XML parsing, and analysis run on the Python backend.
The browser receives only rendered results.

---

## Component Map

```
┌─────────────────────────────────────────────────────────┐
│                    Streamlit UI (app.py)                 │
│  Input panel │ RM panel │ Progress │ Results │ Reports   │
└────────────────────────┬────────────────────────────────┘
                         │ calls
┌────────────────────────▼────────────────────────────────┐
│                  QARunner (qa_engine.py)                 │
│  Generator-based pipeline — yields PipelineEvent        │
│  Progress state separate from QA outcome                │
└─┬──────┬──────┬──────┬──────┬──────┬──────┬────────────┘
  │      │      │      │      │      │      │
  ▼      ▼      ▼      ▼      ▼      ▼      ▼
xml_   source_ rm_    checks/ routing report_ content_
parser retriever adapter  [1-12]          generator comparator
```

---

## Pipeline Stages (in order)

| Stage | Module | Description |
|-------|--------|-------------|
| 1 | qa_engine | Validate inputs, compute file hash |
| 2 | xml_parser | Secure parse + XSD validation |
| 3 | rm_adapter | Connect RM or load export (optional) |
| 4 | source_retriever | HTTP fetch or file extraction |
| 5 | qa_engine | Verify source identity vs. XML |
| 6 | content_comparator | Build section alignment |
| 7 | checks/[1-12] | Run all 12 business checks in order |
| 8 | qa_engine | Consolidate findings |
| 9 | routing | Compute Decision and routing |
| 10 | report_generator | Produce HTML, JSON, CSV |

Independent checks continue after upstream failures. A failed source
retrieval does not prevent detection of blank metadata (checks 1–3, 10–11).

---

## Data Models (`models.py`)

All models are plain dataclasses. Key types:

**ParsedXML** — complete parsed representation of the book XML, including all
metadata fields, section hierarchy, images, footnotes, and technical parse
results (well-formedness, XSD validity).

**SourceDocument** — extracted representation of the original source,
regardless of whether it arrived as a URL, uploaded PDF, HTML, or DOCX.
Preserves: origin URL or filename, retrieval time, file hash, text,
page_texts, headings, tables, images, footnotes, OCR status.

**CheckResult** — immutable result of one check: check_number, check_name,
qa_status (Pass/Fail/Unverified/N/A/Informational), execution_status
(Pending/Running/Completed/Failed to execute/Blocked), reason, findings
(list of CheckFinding with xpath, evidence, expected/observed values), routing.

**AuditReport** — the complete output: all check results, decision,
primary_route, secondary_routes, handover_note, schema results, provenance.

**Execution status is kept separate from QA outcome**. A check may
*execute successfully* (status = Completed) and still produce a QA Fail.
A check may fail to execute (status = Blocked) when a prerequisite is
missing — this does not count as a confirmed QA failure.

---

## XML Parsing Security (`xml_parser.py`)

```python
parser = etree.XMLParser(
    resolve_entities=False,   # disable XXE
    no_network=True,          # no external DTD/schema fetches
    recover=True,             # attempt recovery on malformed XML
)
```

External entity injection, billion laughs, and DTD network fetches are all
disabled. If recovery parsing is needed, the parse_errors list is populated
and downstream content checks are marked Blocked.

Schema validation uses the XSD supplied at run time. Schema-optional fields
can still be mandatory business fields (e.g. CubeBookId).

---

## Source Retrieval (`source_retriever.py`)

Supported inputs:
- Remote URL (HTTP/HTTPS only)
- Uploaded PDF (pdfminer extraction; pytesseract for scanned pages)
- Uploaded HTML/HTM (BeautifulSoup, navigation stripped)
- Uploaded DOCX (python-docx)
- Plain text

SSRF protection:
- Private IP ranges (RFC 1918, loopback, link-local) are blocked.
- `ALLOWED_INTERNAL_HOSTS` env var can whitelist specific internal RM hosts.
- Redirect destinations are validated against the same rules.

Scanned PDFs:
- pdfminer text extraction attempted first.
- If a page yields no text, pytesseract OCR is attempted (if installed).
- OCR confidence is recorded; comparisons on low-confidence pages are marked
  Unverified rather than silently approved.

---

## Check Design Principles

**Check 4 (Source URL)**:
URL syntax alone → not Pass. HTTP 200 alone → not Pass. A URL that points at
a homepage, search page, or wrong document is a FAIL regardless of HTTP status.
A URL that cannot be retrieved without evidence of geographic blocking → Unverified.

**Check 5 (Broken vs. Blocked)**:
A timeout or 403 is not evidence of geographic blocking. Only explicit
geo-blocking signals (known CDN/WAF responses, country-code messages) support
that diagnosis. Otherwise → Unverified.

**Check 7 (Content Fidelity)**:
Section alignment is performed before detailed comparison. The comparison
normalises harmless whitespace but preserves numbers, dates, punctuation,
and word boundaries. Similarity score alone is never the Pass criterion — a
99% similar document can still contain an incorrect obligation date.

**Check 8 (Translation)**:
The English title alone is insufficient. The check requires actual translation
content (uploaded translation document or AI-assisted evaluation). Without it
→ Unverified.

**Check 9 (Styling)**:
Bold, italic, capitalisation, bullet appearance → Informational only.
Never produces a Fail. Missing substantive list text or wrong reading order
are content defects handled by check 7.

**Check 11 (Issuance Date)**:
IssueDateStatus = "Date Replaced By Capture Date" is a confirmed failure with
strong evidence. It is not treated as a potential data entry issue — capture
date substitution is a known system behaviour that requires Research correction.

---

## Routing Logic (`routing.py`)

```
Decision = FAIL     if any confirmed_failures (qa_status == FAIL)
Decision = HOLD     elif any unresolved required checks (Unverified / Blocked)
Decision = APPROVE  elif all required checks satisfactorily verified
Decision = BLOCKED  if XML is unreadable (execution prerequisite failed)
```

**Primary route** = route of the first-blocking or most-critical failure,
not the route with the most failures.

**Secondary routes** = all other distinct routing assignments, preserved in
full. A failing audit always lists every affected route.

**Handover note** — auto-generated but not auto-submitted. It names every
confirmed failure, its check number, and a brief corrective direction.

Route assignments:
- **Research**: Checks 4, 5, 7, 8, 11, 12 (source integrity / dating)
- **Data Management**: Checks 1, 2, 3, 6, 10 (metadata / structure)
- Check 9 never routes (informational only)

---

## RM Adapter (`rm_adapter.py`)

**No write operations exist.** The adapter cannot approve, reject, publish,
route, comment on, or modify any book in RM.

The live integration is **explicitly unconfigured** (no RM API documentation
was available). The `connect()` method is an interface stub.

Supported modes:
1. `load_export(path)` — parse a JSON export of RM metadata.
2. Live (stub) — returns `RMConnectionState(connected=False)`.

The RM connection panel shows:
- Connection status
- Authentication status
- Metadata provenance (Live RM / Exported RM / XML only)
- A side-by-side XML vs. RM vs. Source comparison table

Conflicts between RM and XML values are always displayed. Neither silently
overwrites the other.

---

## Report Generator (`report_generator.py`)

Three formats:

**HTML** — styled audit report with:
- Summary banner (decision, route, counts)
- Full 12-row check table with status badges
- Per-check detail cards with evidence excerpts, XPaths, and corrections
- Schema validation section
- Handover note with copy affordance

**JSON** — machine-readable full report. Suitable for CI pipelines or
integration with other tooling. Every field from the data model is present.

**CSV** — one row per check. Compatible with spreadsheet review workflows.

---

## Known Limitations

1. **Live RM integration** is unimplemented. An adapter interface is ready.
   When RM API documentation becomes available, implement `connect()` in
   `rm_adapter.py` and populate `RMConnectionState` from the live response.

2. **Language detection** (`langdetect`) is unavailable in this build
   environment. Check 1 returns Unverified. The check logic is complete and
   will activate when the library is installable.

3. **Anthropic API** is not available from this environment. Check 8
   (Translation Sanity) AI path returns Unverified. The check still performs
   deterministic heuristics (non-English body with `language="en"` label).

4. **Source URL geo-blocking** cannot be verified without a VPN or alternative
   network path. The tool reports Unverified and does not fabricate a
   geographic-block diagnosis.

5. **Playwright** (JavaScript-rendered sources) is not installed. Sources
   that require JavaScript rendering fall back to plain HTTP fetch, which may
   miss dynamically loaded content.

6. **RM field semantics**: Fields such as "Duplicate" and "MisMatch" visible
   in RM screenshots have not been documented. The adapter does not attempt
   to interpret them until semantics are confirmed.

---

## Hardest Check to Automate

**Check 7 — Content Fidelity** is the hardest.

The challenge is not text comparison per se — it is knowing when a difference
*matters*. The checklist explicitly excludes styling differences (handled by
check 9), but there is a large grey zone: presentation normalisation that is
harmless in one context (whitespace, soft hyphens) can be meaningful in
another (line numbers in statutes, structured tables in financial regulations).

The key design decision here was to separate the comparison into two passes:
1. A structural alignment pass that maps source sections to XML sections,
   tolerating heading reformulations.
2. A content comparison pass that normalises only demonstrably harmless
   differences (whitespace, Unicode normalisation) while preserving numbers,
   dates, punctuation, and word boundaries.

The still-unsolved part: paragraph reordering within a section is flagged, but
without semantic understanding of the document domain it is impossible to know
whether a reordering is a legitimate editorial restructuring or a capture error.
The tool correctly flags it as Unverified rather than auto-approving or
auto-rejecting.

Similarity scores (cosine, Levenshtein) were deliberately excluded from the
Pass criterion for the reason stated in the requirements: a document can be 97%
similar to the source and still contain an incorrect capital threshold or
wrong effective date. Every substantive discrepancy must be individually
surfaced.
