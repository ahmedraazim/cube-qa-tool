"""
Check 4: Source URL — presence, validity, and content match.
Check 5: Source URL — genuinely broken vs. blocked.

Check 4 now auto-passes when source content matches XML identifiers:
- Title words, document number, issuing body, date all searched in source text.
- 3+ matches → PASS (document identity confirmed).
- 1-2 matches → UNVERIFIED (partial evidence).
- 0 matches but HTTP 200 → FAIL (wrong document).
- Homepage / search page → FAIL.
- Retrieval failed → UNVERIFIED (see check 5).
"""
from __future__ import annotations

import re
from typing import Optional, List, Tuple
from urllib.parse import unquote, urlparse

from ..models import (
    CheckFinding, CheckResult, ExecutionStatus, ParsedXML,
    QAStatus, Route, SourceDocument,
)


# ─── URL pattern helpers ──────────────────────────────────────────────────────

_HOMEPAGE_PATTERNS = [
    r"^https?://[^/]+/?$",
    r"[?&]q=", r"[?&]search=", r"/search[/?]",
    r"/home/?$", r"#$",
]
_PLACEHOLDER_PATTERNS = [
    r"example\.com", r"placeholder", r"your[-_]url",
    r"insert[-_]url", r"\bTBD\b", r"\bN/A\b",
]
_DOCUMENT_INDICATORS = [
    r"\.(pdf|docx?|html?|htm)(\?|#|$)",
    r"normativo", r"document", r"regulation", r"legislation",
    r"decreto", r"resolu", r"circular", r"numero=", r"tipo=",
    r"legisl", r"law", r"act\b", r"statute",
]


def _looks_like_url(s: str) -> bool:
    return bool(s and s.strip().lower().startswith(("http://", "https://")))


def _looks_like_homepage(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in _HOMEPAGE_PATTERNS)


def _looks_like_placeholder(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in _PLACEHOLDER_PATTERNS)


def _has_document_indicator(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in _DOCUMENT_INDICATORS)


# ─── Content identity matching ────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for loose matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s\d]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_key_terms(parsed_xml: ParsedXML) -> List[Tuple[str, str, int]]:
    """
    Extract identifiable terms from XML metadata.
    Returns list of (term, field_name, weight).
    Higher weight = more unique / more diagnostic.
    """
    terms = []

    title = (parsed_xml.content_title or "").strip()
    number = (parsed_xml.content_number or "").strip()
    issuing_body = (parsed_xml.issuing_body or "").strip()
    body_title = (parsed_xml.body_title or "").strip()
    issue_date = (parsed_xml.issue_date or "").strip()
    language = (parsed_xml.language or "").strip().lower()

    # Document number — very diagnostic
    if number:
        terms.append((number, "content-number", 3))
        # Also try just the numeric part
        nums = re.findall(r'\d+', number)
        for n in nums:
            if len(n) >= 2:
                terms.append((n, "content-number (digits)", 2))

    # Body title (original language) — high weight
    if body_title and len(body_title) > 8:
        # Use first 6 meaningful words
        words = [w for w in body_title.split() if len(w) > 3][:6]
        if words:
            terms.append((" ".join(words[:4]), "body-title", 3))

    # Content title (English) — medium weight
    if title and len(title) > 5:
        words = [w for w in title.split() if len(w) > 3][:5]
        if words:
            terms.append((" ".join(words[:3]), "content-title", 2))

    # Issuing body — medium weight (skip DEFAULT)
    if issuing_body and issuing_body.upper() != "DEFAULT" and len(issuing_body) > 4:
        words = [w for w in issuing_body.split() if len(w) > 3][:3]
        if words:
            terms.append((" ".join(words), "issuing-body", 2))

    # Issue date — extract year and specific numbers
    if issue_date:
        year_match = re.search(r'(20\d\d|19\d\d)', issue_date)
        if year_match:
            terms.append((year_match.group(1), "issue-date (year)", 1))

    return terms


def _match_content(
    source_text: str,
    parsed_xml: ParsedXML,
) -> Tuple[int, int, List[str], List[str]]:
    """
    Match XML key terms against source text.
    Returns (matched_weight, total_weight, matched_list, missed_list).
    """
    if not source_text:
        return 0, 0, [], []

    src_norm = _normalise(source_text[:8000])
    terms = _extract_key_terms(parsed_xml)

    if not terms:
        return 0, 0, [], []

    matched = []
    missed = []
    matched_weight = 0
    total_weight = sum(w for _, _, w in terms)

    for term, field, weight in terms:
        term_norm = _normalise(term)
        if not term_norm or len(term_norm) < 2:
            continue
        if term_norm in src_norm:
            matched.append(f"'{term}' ({field})")
            matched_weight += weight
        else:
            missed.append(f"'{term}' ({field})")

    return matched_weight, total_weight, matched, missed


# ─── Check 4 ─────────────────────────────────────────────────────────────────

def run_check_04(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    **_kwargs,
) -> CheckResult:
    """
    Check 4: Source URL presence and content-based identity verification.
    Passes when source content confirms the document matches the XML.
    """

    # ── Collect URL fields ───────────────────────────────────────────────────
    urlname_raw = (parsed_xml.requested_urlname or "").strip()
    urlname_is_doc_title = bool(urlname_raw and not _looks_like_url(urlname_raw))

    url_fields = {
        "url-link": parsed_xml.url_link,
        "requested-url": parsed_xml.requested_url,
    }
    if _looks_like_url(urlname_raw):
        url_fields["requested-urlname"] = urlname_raw

    populated = {k: v.strip() for k, v in url_fields.items() if v and v.strip()}
    blank = [k for k, v in url_fields.items() if not (v and v.strip())]

    findings: list[CheckFinding] = []
    informational: list[str] = []

    # ── No URL at all ────────────────────────────────────────────────────────
    if not populated:
        return CheckResult(
            check_number=4,
            check_name="Source URL — Presence and Validity",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="All source URL fields are blank. A source URL is required.",
            findings=[CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//metadata/url-link", source_location=None,
                observed="(blank)",
                expected="A URL pointing to the specific source document",
                severity="error",
            )],
            method="metadata-inspection",
            coverage="url-link, requested-url, requested-urlname",
            recommended_correction="Supply the direct URL to the original source document.",
            routing=Route.RESEARCH,
        )

    # ── URL field notes ──────────────────────────────────────────────────────
    def _norm(u: str) -> str:
        return unquote(u.strip()).lower()

    unique_normalised = list(dict.fromkeys(_norm(v) for v in populated.values()))
    if len(unique_normalised) > 1:
        informational.append(
            f"Multiple different URLs found across fields: {list(populated.values())}. "
            "Verify which is the authoritative source URL."
        )

    if urlname_is_doc_title:
        informational.append(
            f"requested-urlname is a document display name (not a URL): {urlname_raw!r}. "
            "This is normal."
        )

    if blank:
        real_blank = [k for k in blank if k != "requested-urlname" or not urlname_is_doc_title]
        if real_blank:
            informational.append(
                f"Blank URL fields (not failures if url-link is populated): {real_blank}"
            )

    primary_url = populated.get("url-link") or list(populated.values())[0]

    # ── Structural quality ───────────────────────────────────────────────────
    if _looks_like_placeholder(primary_url):
        return CheckResult(
            check_number=4,
            check_name="Source URL — Presence and Validity",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason=f"URL matches a placeholder pattern: {primary_url!r}",
            findings=[CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//metadata/url-link", source_location=primary_url,
                observed=primary_url, expected="A real document URL (not a placeholder)",
                severity="error",
            )],
            method="url-structural-analysis",
            coverage="url-link",
            recommended_correction="Replace placeholder with the actual source URL.",
            routing=Route.RESEARCH,
        )

    if _looks_like_homepage(primary_url):
        return CheckResult(
            check_number=4,
            check_name="Source URL — Presence and Validity",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason=f"URL appears to be a homepage or search page, not a specific document: {primary_url!r}",
            findings=[CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//metadata/url-link", source_location=primary_url,
                observed=primary_url,
                expected="A URL pointing directly to the specific regulation document",
                severity="error",
            )],
            method="url-structural-analysis",
            coverage="url-link",
            recommended_correction="Replace with the direct URL to the specific document page or PDF.",
            routing=Route.RESEARCH,
        )

    # ── No source doc retrieved ──────────────────────────────────────────────
    if source_doc is None:
        status = QAStatus.UNVERIFIED
        reason = (
            f"URL {primary_url!r} is structurally plausible but was not retrieved. "
            "Cannot verify document identity without fetching content."
        )
        if _has_document_indicator(primary_url):
            informational.append("URL path suggests a specific document (PDF/normativo endpoint).")
        return CheckResult(
            check_number=4,
            check_name="Source URL — Presence and Validity",
            qa_status=status,
            execution_status=ExecutionStatus.COMPLETED,
            reason=reason,
            informational_notes=informational,
            method="url-structural-analysis",
            coverage="url-link",
            verification_limitations=[
                "Source was not retrieved — upload the document or ensure network access."
            ],
        )

    # ── Retrieval failed ─────────────────────────────────────────────────────
    if source_doc.retrieval_error and not (source_doc.text or "").strip():
        return CheckResult(
            check_number=4,
            check_name="Source URL — Presence and Validity",
            qa_status=QAStatus.UNVERIFIED,
            execution_status=ExecutionStatus.COMPLETED,
            reason=f"URL present but retrieval failed: {source_doc.retrieval_error}. See check 5.",
            informational_notes=informational,
            method="url-structural-analysis + http-retrieval",
            coverage="url-link, requested-url, requested-urlname",
            verification_limitations=["Retrieval failed — see check 5 for broken vs. blocked analysis."],
        )

    # ── Content identity matching ─────────────────────────────────────────────
    src_text = (source_doc.text or "").strip()
    http_status = source_doc.retrieval_status_code

    matched_weight, total_weight, matched_terms, missed_terms = _match_content(
        src_text, parsed_xml
    )

    match_ratio = matched_weight / total_weight if total_weight > 0 else 0.0

    informational.append(
        f"Content identity check: {len(matched_terms)} term(s) matched, "
        f"{len(missed_terms)} missed. "
        f"Match score: {match_ratio:.0%}."
    )
    if matched_terms:
        informational.append(f"Matched: {'; '.join(matched_terms[:6])}")
    if missed_terms:
        informational.append(f"Not found in source: {'; '.join(missed_terms[:4])}")

    # ── Decision based on match score ────────────────────────────────────────
    if match_ratio >= 0.60 and matched_terms:
        # Strong match — PASS
        status = QAStatus.PASS
        reason = (
            f"Document identity confirmed. "
            f"URL retrieved (HTTP {http_status}) and {len(matched_terms)} key identifier(s) "
            f"from XML found in source content: {'; '.join(matched_terms[:4])}."
        )

    elif match_ratio >= 0.25 and matched_terms:
        # Partial match — UNVERIFIED but with positive evidence
        status = QAStatus.UNVERIFIED
        reason = (
            f"URL retrieved (HTTP {http_status}). Partial content match: "
            f"{len(matched_terms)} identifier(s) found, {len(missed_terms)} not found. "
            f"Human review recommended to confirm this is the correct document."
        )
        findings.append(CheckFinding(
            xml_id=None, parent_section_id=None,
            xpath="//metadata/url-link", source_location=primary_url,
            observed=f"Partial match — missed: {'; '.join(missed_terms[:3])}",
            expected="All key XML identifiers present in source content",
            severity="warning",
        ))

    elif src_text and http_status == 200 and total_weight > 0:
        # Retrieved something but nothing matched — likely wrong document
        status = QAStatus.FAIL
        reason = (
            f"URL retrieved (HTTP {http_status}) but source content does not match XML identifiers. "
            f"0 of {len(missed_terms)} key term(s) found — this may be the wrong document."
        )
        findings.append(CheckFinding(
            xml_id=None, parent_section_id=None,
            xpath="//metadata/url-link", source_location=primary_url,
            observed=f"Source content does not contain: {'; '.join(missed_terms[:4])}",
            expected=f"Source should contain XML identifiers: {'; '.join(missed_terms[:4])}",
            severity="error",
        ))

    elif src_text and http_status == 200 and total_weight == 0:
        # No identifiers to compare — can only say HTTP 200
        status = QAStatus.UNVERIFIED
        reason = (
            f"URL retrieved (HTTP {http_status}). "
            "No unique XML identifiers available to confirm document identity — "
            "human review required."
        )
        informational.append(
            "No matchable identifiers found in XML metadata "
            "(title, number, issuing body all blank or DEFAULT). "
            "Fix metadata to enable automated identity confirmation."
        )

    else:
        status = QAStatus.UNVERIFIED
        reason = (
            f"URL retrieved (HTTP {http_status}) but content could not be extracted. "
            "Document identity unconfirmed."
        )

    return CheckResult(
        check_number=4,
        check_name="Source URL — Presence and Validity",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method="url-structural-analysis + http-retrieval + content-identity-matching",
        coverage="url-link, requested-url, requested-urlname",
        recommended_correction=(
            "Verify the URL loads the correct document and that XML metadata identifiers match."
        ) if status == QAStatus.FAIL else None,
        routing=Route.RESEARCH if status == QAStatus.FAIL else None,
        verification_limitations=(
            [] if status == QAStatus.PASS else
            ["Content identity is based on key term matching — a human should confirm the full document."]
        ),
    )


# ─── Check 5 ─────────────────────────────────────────────────────────────────

def run_check_05(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    **_kwargs,
) -> CheckResult:
    """Check 5: Is the URL genuinely broken, or just blocked/geo-restricted?"""

    primary_url = (
        parsed_xml.url_link or parsed_xml.requested_url or
        (parsed_xml.requested_urlname if _looks_like_url(parsed_xml.requested_urlname or "") else None)
        or ""
    )

    if not primary_url:
        return CheckResult(
            check_number=5, check_name="Source URL — Broken vs. Blocked",
            qa_status=QAStatus.NA, execution_status=ExecutionStatus.COMPLETED,
            reason="No URL to test (handled in check 4).",
            method="n/a", coverage="n/a",
        )

    if source_doc is None:
        return CheckResult(
            check_number=5, check_name="Source URL — Broken vs. Blocked",
            qa_status=QAStatus.UNVERIFIED, execution_status=ExecutionStatus.COMPLETED,
            reason="Source retrieval was not attempted. Cannot determine broken vs. blocked.",
            method="not-attempted", coverage="n/a",
            verification_limitations=["No retrieval attempt was made."],
        )

    status_code = source_doc.retrieval_status_code
    error = source_doc.retrieval_error

    # Retrieved successfully — PASS
    if status_code and 200 <= status_code < 300 and not error:
        return CheckResult(
            check_number=5, check_name="Source URL — Broken vs. Blocked",
            qa_status=QAStatus.PASS, execution_status=ExecutionStatus.COMPLETED,
            reason=f"URL loaded successfully (HTTP {status_code}). Link is live and reachable — not broken or blocked.",
            method="http-retrieval", coverage="single retrieval attempt",
        )

    findings: list[CheckFinding] = []
    limitations = [
        "Check 5 requires a second access method (VPN, different network) to "
        "definitively distinguish broken from blocked. This tool makes a single attempt."
    ]

    if error:
        error_lower = error.lower()
        if "ssl" in error_lower or "certificate" in error_lower:
            note = (
                f"SSL/certificate error ({error[:120]}). "
                "This is consistent with a corporate proxy intercepting HTTPS traffic. "
                "The URL may be reachable — the certificate chain is the issue, not the link."
            )
            status = QAStatus.UNVERIFIED
        elif "timed out" in error_lower or "timeout" in error_lower:
            note = (
                f"Connection timed out. "
                "May be geographic blocking, a slow server, or a broken link. "
                "Cannot distinguish without an alternative network path."
            )
            status = QAStatus.UNVERIFIED
        elif "connection error" in error_lower or "name or service" in error_lower:
            note = (
                f"DNS/connection failure ({error[:120]}). "
                "May be broken or network-filtered."
            )
            status = QAStatus.UNVERIFIED
        elif status_code in (403, 401):
            note = (
                f"HTTP {status_code} — authentication or geo-block. "
                "Cannot confirm broken vs. blocked without alternative access."
            )
            status = QAStatus.UNVERIFIED
        elif status_code == 404:
            note = (
                f"HTTP 404 Not Found. Server reports the resource does not exist. "
                "Consistent with a broken/dead link (document may have moved)."
            )
            status = QAStatus.FAIL
        elif status_code and status_code >= 500:
            note = f"HTTP {status_code} server error. Server-side problem — link status unclear."
            status = QAStatus.UNVERIFIED
        else:
            note = (
                f"Retrieval failed: {error[:120]} (HTTP {status_code or 'no status'}). "
                "Cause is unresolved."
            )
            status = QAStatus.UNVERIFIED

        findings.append(CheckFinding(
            xml_id=None, parent_section_id=None,
            xpath="//metadata/url-link", source_location=primary_url,
            observed=f"HTTP {status_code or 'no response'}: {error[:150]}",
            expected="Successful retrieval of source document",
            severity="error",
        ))
    else:
        note = f"Unexpected retrieval state (HTTP {status_code}, no error). Marked Unverified."
        status = QAStatus.UNVERIFIED

    return CheckResult(
        check_number=5, check_name="Source URL — Broken vs. Blocked",
        qa_status=status, execution_status=ExecutionStatus.COMPLETED,
        reason=note, findings=findings,
        method="http-retrieval", coverage="single retrieval attempt from current network",
        recommended_correction="Verify the URL from a different network or obtain an alternative source.",
        routing=Route.RESEARCH,
        verification_limitations=limitations,
    )
