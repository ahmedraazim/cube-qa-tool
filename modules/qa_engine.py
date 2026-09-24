"""
QA Engine — orchestrates the full 12-check pipeline.

Pipeline order:
1. Validate inputs
2. Parse XML and validate schema
3. Retrieve RM metadata (if configured)
4. Retrieve or extract source
5. Verify source identity and version
6. Build source-to-XML section alignment
7. Execute checks 1–12
8. Consolidate findings
9. Calculate decision and routing
10. Generate reports
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from .checks import (
    check_01_book_name,
    check_02_citation,
    check_03_issuance_type,
    check_04_05_source_url,
    check_06_toc_structure,
    check_07_content_fidelity,
    check_08_12,
)
from .models import (
    AuditReport, CheckResult, Decision, ExecutionStatus,
    ParsedXML, QAStatus, RMMetadata, Route, SourceDocument,
)
from .rm_adapter import RMAdapter, RMConnectionState
from .routing import calculate_decision_and_routing
from .xml_parser import full_parse


# ─── Progress event ───────────────────────────────────────────────────────────

class PipelineEvent:
    def __init__(self, stage: str, status: str, message: str = "", detail: Any = None):
        self.stage = stage
        self.status = status  # pending | running | completed | failed | blocked
        self.message = message
        self.detail = detail


# ─── QA Run ────────────────────────────────────────────────────────────────────

class QARunner:
    """
    Runs the complete QA pipeline.
    Call run() which is a generator yielding PipelineEvent objects,
    with the final event carrying the AuditReport in detail.
    """

    def __init__(
        self,
        xml_path: Path,
        xsd_path: Optional[Path] = None,
        source_path: Optional[Path] = None,
        source_url: Optional[str] = None,
        native_path: Optional[Path] = None,
        translation_path: Optional[Path] = None,
        rm_review_url: Optional[str] = None,
        rm_export_path: Optional[Path] = None,
        ai_client=None,
    ) -> None:
        self.xml_path = xml_path
        self.xsd_path = xsd_path
        self.source_path = source_path
        self.source_url = source_url
        self.native_path = native_path
        self.translation_path = translation_path
        self.rm_review_url = rm_review_url
        self.rm_export_path = rm_export_path
        self.ai_client = ai_client
        self.run_id = str(uuid.uuid4())

    def run(self) -> Generator[PipelineEvent, None, None]:
        """Generator that yields progress events and ultimately the AuditReport."""
        from . import source_retriever as sr

        # ── Stage 1: Validate inputs ─────────────────────────────────────────
        yield PipelineEvent("1. Input Validation", "running")
        if not self.xml_path.exists():
            yield PipelineEvent("1. Input Validation", "failed", f"XML file not found: {self.xml_path}")
            return
        xml_bytes = self.xml_path.read_bytes()
        xml_hash = hashlib.sha256(xml_bytes).hexdigest()
        yield PipelineEvent("1. Input Validation", "completed", f"XML file loaded ({len(xml_bytes)} bytes, sha256: {xml_hash[:12]}…)")

        # ── Stage 2: Parse XML ───────────────────────────────────────────────
        yield PipelineEvent("2. XML Parse & Schema Validation", "running")
        parsed_xml = full_parse(self.xml_path, self.xsd_path)

        if not parsed_xml.is_well_formed:
            msg = f"XML is malformed: {'; '.join(parsed_xml.parse_errors)}"
            yield PipelineEvent("2. XML Parse & Schema Validation", "failed", msg)
            # Still return a report with all checks Blocked
            check_results = _blocked_all_checks("XML parse failed: " + msg)
            report = _build_report(
                run_id=self.run_id,
                xml_filename=self.xml_path.name,
                xml_hash=xml_hash,
                parsed_xml=parsed_xml,
                source_doc=None,
                rm_meta=None,
                rm_state=None,
                check_results=check_results,
            )
            yield PipelineEvent("COMPLETE", "failed", "Audit complete with XML parse errors.", report)
            return

        schema_msg = ""
        if parsed_xml.xsd_valid is True:
            schema_msg = "XSD validation passed."
        elif parsed_xml.xsd_valid is False:
            schema_msg = f"XSD validation FAILED: {'; '.join(parsed_xml.xsd_errors[:3])}"
        elif self.xsd_path:
            schema_msg = "XSD validation did not run (schema load error)."
        else:
            schema_msg = "No XSD supplied — schema validation skipped."
        yield PipelineEvent("2. XML Parse & Schema Validation", "completed", schema_msg)

        # ── Stage 3: RM metadata ─────────────────────────────────────────────
        yield PipelineEvent("3. RM Metadata", "running")
        rm_adapter = RMAdapter()
        rm_state: Optional[RMConnectionState] = None
        rm_meta: Optional[RMMetadata] = None

        if self.rm_export_path and self.rm_export_path.exists():
            rm_state = rm_adapter.load_export(self.rm_export_path)
            if rm_state.is_connected:
                rm_meta = rm_state.metadata
                yield PipelineEvent("3. RM Metadata", "completed", f"Loaded from RM export: {self.rm_export_path.name}")
            else:
                yield PipelineEvent("3. RM Metadata", "failed", rm_state.error or "Export load failed")
        elif self.rm_review_url:
            yield PipelineEvent("3. RM Metadata", "running",
                f"Scanning RDM page with Selenium: {self.rm_review_url[:70]}")
            rm_state = rm_adapter.connect(self.rm_review_url)
            if rm_state.is_connected:
                rm_meta = rm_state.metadata
                fields_found = len(rm_state.metadata.raw_data or {})
                yield PipelineEvent("3. RM Metadata", "completed",
                    f"RDM page scanned — {fields_found} field(s) extracted.")
            else:
                yield PipelineEvent("3. RM Metadata", "failed",
                    rm_state.error or "RDM scan failed — continuing with XML only.")
        else:
            yield PipelineEvent("3. RM Metadata", "completed", "No RM URL or export provided — XML-only mode.")

        # ── Stage 4: Source retrieval ────────────────────────────────────────
        yield PipelineEvent("4. Source Retrieval", "running")
        source_doc: Optional[SourceDocument] = None
        native_doc: Optional[SourceDocument] = None
        translation_doc: Optional[SourceDocument] = None

        if self.source_path and self.source_path.exists():
            source_doc = sr.load_file(self.source_path)
            yield PipelineEvent("4. Source Retrieval", "completed",
                f"Loaded from file: {self.source_path.name} ({source_doc.extraction_method})")
        elif self.source_url:
            source_doc = sr.fetch_url(self.source_url)
            if source_doc.retrieval_error:
                yield PipelineEvent("4. Source Retrieval", "failed",
                    f"Fetch failed: {source_doc.retrieval_error} — continuing without source.")
            else:
                yield PipelineEvent("4. Source Retrieval", "completed",
                    f"Fetched URL (HTTP {source_doc.retrieval_status_code}, {source_doc.extraction_method})")
        else:
            # Try the URL from XML
            # Only use requested_urlname as URL if it looks like an actual URL
            def _is_url(s):
                return bool(s and s.strip().lower().startswith(("http://", "https://")))

            primary_url = (
                parsed_xml.url_link if _is_url(parsed_xml.url_link)
                else parsed_xml.requested_url if _is_url(parsed_xml.requested_url)
                else parsed_xml.requested_urlname if _is_url(parsed_xml.requested_urlname)
                else None
            )
            if primary_url:
                primary_url = primary_url.strip()
                yield PipelineEvent("4. Source Retrieval", "running",
                    f"Fetching source URL: {primary_url[:80]}")
                source_doc = sr.fetch_url_smart(primary_url)
                if source_doc.retrieval_error and not source_doc.text:
                    yield PipelineEvent("4. Source Retrieval", "failed",
                        f"Fetch failed: {source_doc.retrieval_error}")
                else:
                    method = source_doc.extraction_method or "unknown"
                    chars = len(source_doc.text or "")
                    warnings = source_doc.extraction_warnings or []
                    js_warn = any("javascript" in w.lower() for w in warnings)
                    if js_warn:
                        yield PipelineEvent("4. Source Retrieval", "running",
                            f"JS-rendered page detected. Trying Playwright + PDF discovery...")
                    yield PipelineEvent("4. Source Retrieval", "completed",
                        f"Source retrieved (HTTP {source_doc.retrieval_status_code}, "                        f"{method}, {chars:,} chars extracted)")
            else:
                yield PipelineEvent("4. Source Retrieval", "completed",
                    "No source URL found in XML — upload a source file to enable content checks.")

        if self.native_path and self.native_path.exists():
            native_doc = sr.load_file(self.native_path)
            yield PipelineEvent("4. Source Retrieval", "completed",
                f"Native document loaded: {self.native_path.name}")

        if self.translation_path and self.translation_path.exists():
            translation_doc = sr.load_file(self.translation_path)
            yield PipelineEvent("4. Source Retrieval", "completed",
                f"Translation document loaded: {self.translation_path.name}")

        # ── Stage 5: Source identity ─────────────────────────────────────────
        yield PipelineEvent("5. Source Identity Verification", "running")
        source_identity = _verify_source_identity(parsed_xml, source_doc)
        yield PipelineEvent("5. Source Identity Verification", "completed", source_identity)

        # ── Stages 6–7: Section alignment (done inside check 6) ───────────────
        yield PipelineEvent("6. Section Alignment", "running")
        yield PipelineEvent("6. Section Alignment", "completed", "Alignment performed within check 6.")

        # ── Stage 7: Execute checks ──────────────────────────────────────────
        check_kwargs = dict(
            parsed_xml=parsed_xml,
            source_doc=source_doc,
            native_doc=native_doc,
            translation_doc=translation_doc,
            rm_meta=rm_meta,
            ai_client=self.ai_client,
        )

        check_runners: List[Tuple[str, Callable]] = [
            ("Check 1: Book Name", lambda: check_01_book_name.run(**check_kwargs)),
            ("Check 2: Citation", lambda: check_02_citation.run(**check_kwargs)),
            ("Check 3: Issuance Type", lambda: check_03_issuance_type.run(**check_kwargs)),
            ("Check 4: Source URL Validity", lambda: check_04_05_source_url.run_check_04(**check_kwargs)),
            ("Check 5: Broken vs. Blocked", lambda: check_04_05_source_url.run_check_05(**check_kwargs)),
            ("Check 6: TOC / Structure", lambda: check_06_toc_structure.run(**check_kwargs)),
            ("Check 7: Content Fidelity", lambda: check_07_content_fidelity.run(**check_kwargs)),
            ("Check 8: Translation Sanity", lambda: check_08_12.run_check_08(**check_kwargs)),
            ("Check 9: Styling Exclusion", lambda: check_08_12.run_check_09(**check_kwargs)),
            ("Check 10: Placeholder Metadata", lambda: check_08_12.run_check_10(**check_kwargs)),
            ("Check 11: Issuance Date", lambda: check_08_12.run_check_11(**check_kwargs)),
            ("Check 12: Final Sweep", lambda: check_08_12.run_check_12(**check_kwargs)),
        ]

        check_results: List[CheckResult] = []
        for name, runner in check_runners:
            yield PipelineEvent(name, "running")
            try:
                result = runner()
                check_results.append(result)
                yield PipelineEvent(name, "completed",
                    f"{result.qa_status.value} — {result.reason[:80]}", result)
            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                failed_result = CheckResult(
                    check_number=len(check_results) + 1,
                    check_name=name,
                    qa_status=QAStatus.UNVERIFIED,
                    execution_status=ExecutionStatus.FAILED_TO_EXECUTE,
                    reason=f"Check threw an exception: {exc}",
                    verification_limitations=[tb],
                )
                check_results.append(failed_result)
                yield PipelineEvent(name, "failed_to_execute", str(exc))

        # ── Stage 8: Consolidate ─────────────────────────────────────────────
        yield PipelineEvent("8. Consolidation", "running")
        report = _build_report(
            run_id=self.run_id,
            xml_filename=self.xml_path.name,
            xml_hash=xml_hash,
            parsed_xml=parsed_xml,
            source_doc=source_doc,
            rm_meta=rm_meta,
            rm_state=rm_state,
            check_results=check_results,
        )
        yield PipelineEvent("8. Consolidation", "completed",
            f"Decision: {report.overall_decision.value} | Route: {report.primary_route.value}")

        yield PipelineEvent("COMPLETE", "completed", "Audit complete.", report)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _verify_source_identity(parsed_xml: ParsedXML, source_doc: Optional[SourceDocument]) -> str:
    """Return a description of the source identity check."""
    if source_doc is None:
        return "No source document to verify."
    if source_doc.retrieval_error:
        return f"Source retrieval failed — identity unverifiable: {source_doc.retrieval_error}"

    notes = []
    if source_doc.document_date:
        notes.append(f"Document date from source: {source_doc.document_date}")
    if source_doc.document_version:
        notes.append(f"Document version from source: {source_doc.document_version}")
    if source_doc.file_hash:
        notes.append(f"Source file SHA-256: {source_doc.file_hash[:12]}…")
    notes.append(f"Extraction method: {source_doc.extraction_method}")
    if source_doc.extraction_warnings:
        notes.append(f"Warnings: {'; '.join(source_doc.extraction_warnings[:2])}")

    return " | ".join(notes) if notes else "Source loaded but identity details not available."


def _blocked_all_checks(reason: str) -> List[CheckResult]:
    from .models import CheckResult
    names = [
        "Book Name", "Citation", "Issuance Type",
        "Source URL — Presence and Validity", "Source URL — Broken vs. Blocked",
        "TOC / Structure", "Content Fidelity", "Translation Sanity",
        "Styling Exclusion", "Placeholder Metadata", "Issuance Date", "Final Sweep",
    ]
    return [
        CheckResult(
            check_number=i + 1,
            check_name=name,
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.BLOCKED,
            reason=reason,
        )
        for i, name in enumerate(names)
    ]


def _build_report(
    run_id: str,
    xml_filename: str,
    xml_hash: str,
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument],
    rm_meta: Optional[RMMetadata],
    rm_state,
    check_results: List[CheckResult],
) -> AuditReport:
    decision, primary_route, secondary_routes, explanation, handover = \
        calculate_decision_and_routing(check_results, parsed_xml)

    confirmed_failures = sum(1 for r in check_results if r.is_confirmed_failure)
    unresolved = sum(1 for r in check_results if r.is_unresolved)

    return AuditReport(
        run_id=run_id,
        run_timestamp=datetime.utcnow(),
        xml_filename=xml_filename,
        xml_hash=xml_hash,
        source_identity=source_doc.origin if source_doc else None,
        source_hash=source_doc.file_hash if source_doc else None,
        parsed_xml=parsed_xml,
        source_document=source_doc,
        rm_metadata=rm_meta,
        check_results=check_results,
        overall_decision=decision,
        primary_route=primary_route,
        secondary_routes=secondary_routes,
        routing_explanation=explanation,
        handover_note=handover,
        confirmed_failures=confirmed_failures,
        unresolved_checks=unresolved,
        schema_validation_passed=parsed_xml.xsd_valid,
        schema_errors=parsed_xml.xsd_errors,
    )
