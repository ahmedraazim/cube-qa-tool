"""
Shared data models for CUBE QA.
All dataclasses are immutable-by-convention; use dataclasses.replace() to derive variants.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ─── Enumerations ────────────────────────────────────────────────────────────

class QAStatus(str, Enum):
    """Business QA outcome for a check."""
    PASS = "Pass"
    FAIL = "Fail"
    UNVERIFIED = "Unverified"
    NA = "N/A"
    INFORMATIONAL = "Informational"


class ExecutionStatus(str, Enum):
    """Pipeline execution state for a check."""
    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED_TO_EXECUTE = "Failed to execute"
    BLOCKED = "Blocked"


class Decision(str, Enum):
    APPROVE = "APPROVE FOR PUBLISH"
    FAIL = "FAIL"
    HOLD = "HOLD — VERIFICATION INCOMPLETE"
    BLOCKED = "BLOCKED — INVALID INPUT"


class Route(str, Enum):
    RESEARCH = "Research"
    DATA_MANAGEMENT = "Data Management"
    NONE = "None"


class MetadataProvenance(str, Enum):
    LIVE_RM = "Live RM"
    EXPORTED_RM = "Exported RM data"
    XML_ONLY = "XML only"


# ─── XML Data ─────────────────────────────────────────────────────────────────

@dataclass
class XMLSection:
    level_id: str
    parent_id: Optional[str]
    title: str
    num: int
    link: Optional[str]
    paragraphs: List[Dict[str, str]]   # [{"id": ..., "text": ...}]
    children: List["XMLSection"] = field(default_factory=list)
    depth: int = 0

    @property
    def xpath(self) -> str:
        return f"//level[@id='{self.level_id}']"


@dataclass
class ParsedXML:
    doc_id: str
    cube_book_id: Optional[str]
    content_title: Optional[str]
    content_number: Optional[str]
    issue_date: Optional[str]
    issuing_body: Optional[str]
    issuing_body_id: Optional[str]
    compliance_date: Optional[str]
    country_of_issue: Optional[str]
    country_of_issue_id: Optional[str]
    translation_type: Optional[str]
    translation_source: Optional[str]
    requested_url: Optional[str]
    requested_urlname: Optional[str]
    document_type: Optional[str]
    url_link: Optional[str]
    citation: Optional[str]
    issuance_type: Optional[str]
    issue_date_status: Optional[str]
    compliance_date_status: Optional[str]
    book_load_type: Optional[str]
    version: Optional[str]
    language: Optional[str]
    update_guid: Optional[str]
    book_is_pdf: Optional[str]
    capture_date: Optional[str]
    metadata_overwrite: Optional[str]
    body_title: Optional[str]
    sections: List[XMLSection] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    footnotes: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    raw_xml: str = ""
    is_well_formed: bool = True
    parse_errors: List[str] = field(default_factory=list)
    xsd_valid: Optional[bool] = None
    xsd_errors: List[str] = field(default_factory=list)

    @property
    def all_url_candidates(self) -> List[str]:
        urls = []
        for u in [self.url_link, self.requested_urlname, self.requested_url]:
            if u and u.strip():
                urls.append(u.strip())
        # Also from sections
        for sec in self.sections:
            if sec.link:
                urls.append(sec.link)
        return list(dict.fromkeys(urls))  # deduplicate preserving order

    @property
    def flat_sections(self) -> List[XMLSection]:
        """DFS-ordered flat list of all sections."""
        result = []
        def _walk(secs):
            for s in secs:
                result.append(s)
                _walk(s.children)
        _walk(self.sections)
        return result


# ─── Source Evidence ──────────────────────────────────────────────────────────

@dataclass
class SourceDocument:
    origin: str                       # URL or filename
    retrieval_time: Optional[datetime]
    content_type: Optional[str]       # MIME type
    file_hash: Optional[str]          # SHA-256 of raw bytes
    text: Optional[str]               # Extracted plain text
    page_texts: List[Dict[str, Any]] = field(default_factory=list)  # per-page
    headings: List[str] = field(default_factory=list)
    tables: List[List[List[str]]] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    footnotes: List[str] = field(default_factory=list)
    document_date: Optional[str] = None
    document_version: Optional[str] = None
    extraction_method: str = "unknown"
    extraction_warnings: List[str] = field(default_factory=list)
    retrieval_status_code: Optional[int] = None
    retrieval_error: Optional[str] = None
    ocr_used: bool = False
    ocr_confidence: Optional[float] = None
    is_complete: bool = True

    @staticmethod
    def compute_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


# ─── RM Metadata ─────────────────────────────────────────────────────────────

@dataclass
class RMMetadata:
    book_name: Optional[str] = None
    citation: Optional[str] = None
    issuance_type: Optional[str] = None
    issuance_date: Optional[str] = None
    effective_date: Optional[str] = None
    compliance_date: Optional[str] = None
    jurisdiction: Optional[str] = None
    issuing_body: Optional[str] = None
    language: Optional[str] = None
    source_url: Optional[str] = None
    has_translation_tab: Optional[bool] = None
    has_native_tab: Optional[bool] = None
    toc_sections: List[str] = field(default_factory=list)
    retrieval_time: Optional[datetime] = None
    provenance: MetadataProvenance = MetadataProvenance.XML_ONLY
    connection_error: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)


# ─── Check Results ────────────────────────────────────────────────────────────

@dataclass
class CheckFinding:
    """A single discrepancy within a check."""
    xml_id: Optional[str]
    parent_section_id: Optional[str]
    xpath: Optional[str]
    source_location: Optional[str]     # page/section/URL
    observed: str
    expected: str
    excerpt_xml: Optional[str] = None
    excerpt_source: Optional[str] = None
    severity: str = "error"            # error | warning | info


@dataclass
class CheckResult:
    check_number: int
    check_name: str
    qa_status: QAStatus
    execution_status: ExecutionStatus
    reason: str
    findings: List[CheckFinding] = field(default_factory=list)
    method: str = ""
    coverage: str = ""
    recommended_correction: Optional[str] = None
    routing: Optional[Route] = None
    verification_limitations: List[str] = field(default_factory=list)
    informational_notes: List[str] = field(default_factory=list)

    @property
    def is_confirmed_failure(self) -> bool:
        return (
            self.qa_status == QAStatus.FAIL
            and self.execution_status == ExecutionStatus.COMPLETED
        )

    @property
    def is_unresolved(self) -> bool:
        return self.qa_status in (QAStatus.UNVERIFIED,) or \
               self.execution_status in (ExecutionStatus.BLOCKED, ExecutionStatus.FAILED_TO_EXECUTE)


# ─── Audit Report ─────────────────────────────────────────────────────────────

@dataclass
class AuditReport:
    run_id: str
    run_timestamp: datetime
    xml_filename: str
    xml_hash: str
    source_identity: Optional[str]
    source_hash: Optional[str]
    parsed_xml: ParsedXML
    source_document: Optional[SourceDocument]
    rm_metadata: Optional[RMMetadata]
    check_results: List[CheckResult]
    overall_decision: Decision
    primary_route: Route
    secondary_routes: List[Route]
    routing_explanation: str
    handover_note: str
    confirmed_failures: int
    unresolved_checks: int
    schema_validation_passed: Optional[bool]
    schema_errors: List[str] = field(default_factory=list)
