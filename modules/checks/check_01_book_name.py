"""
Check 1: Book name — language-aware validation.

Rules (revised):
- Books can be in ANY language. English is NOT required.
- The content-title language must match the declared XML `language` field.
- Character set is validated against the declared language's expected script.
- If language detection is unavailable, check is Unverified (not Fail).
- AI language verification used when source content is available.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import config_loader as cfg
from ..models import CheckFinding, CheckResult, ExecutionStatus, ParsedXML, QAStatus, Route, SourceDocument

try:
    from langdetect import detect, detect_langs, LangDetectException
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False

# Unicode-block language detection (no external library needed)
from ..auto_extract import detect_language_from_text as _detect_unicode, language_matches_declared

# Language codes that use non-Latin scripts — these require Unicode chars in title
_NON_LATIN_SCRIPTS = {
    "ar", "fa", "ur", "he", "yi",        # Arabic / Hebrew scripts
    "zh", "zh-cn", "zh-tw", "ja", "ko",  # CJK
    "ru", "uk", "bg", "sr", "mk",        # Cyrillic
    "el",                                  # Greek
    "th", "lo",                            # Southeast Asian
    "am", "ti",                            # Ethiopic
    "ka",                                  # Georgian
    "hy",                                  # Armenian
    "hi", "bn", "pa", "gu", "mr",         # Indic scripts
    "ne", "si",
}

# Map declared language code → approximate expected script families
_LATIN_FAMILY = {
    "en", "pt", "es", "fr", "de", "it", "nl", "pl", "cs", "sk",
    "ro", "sv", "da", "no", "fi", "hu", "tr", "id", "ms", "vi",
    "hr", "sl", "lt", "lv", "et", "sq", "af", "eu", "ca", "gl",
}


def _detect_language(text: str) -> Optional[str]:
    if not LANGDETECT_AVAILABLE or not text.strip():
        return None
    try:
        return detect(text)
    except Exception:
        return None


def _detect_language_probs(text: str) -> list:
    """Return list of (lang, prob) sorted by probability descending."""
    if not LANGDETECT_AVAILABLE or not text.strip():
        return []
    try:
        langs = detect_langs(text)
        return [(str(l).split(":")[0], float(str(l).split(":")[1])) for l in langs]
    except Exception:
        return []


def _normalise_lang_code(code: str) -> str:
    """Normalise language codes for comparison (zh-cn → zh, en-US → en)."""
    if not code:
        return ""
    return code.lower().split("-")[0].split("_")[0]


def _title_is_blank_or_placeholder(title: str) -> bool:
    placeholders = {"", "n/a", "na", "none", "null", "undefined", "title", "untitled"}
    return title.strip().lower() in placeholders


def _validate_characters_for_language(title: str, lang_code: str) -> list[str]:
    """
    Return list of disallowed character issues, accounting for declared language.
    Latin-script languages: standard Latin + digits + punctuation.
    Non-Latin languages: allow full Unicode for the appropriate script range.
    """
    norm = _normalise_lang_code(lang_code)

    if norm in _NON_LATIN_SCRIPTS:
        # For non-Latin scripts, only ban truly problematic characters:
        # control characters, null bytes, and HTML tags
        issues = []
        if re.search(r'[\x00-\x08\x0b\x0e-\x1f\x7f]', title):
            issues.append("Control characters found in title")
        if re.search(r'<[a-zA-Z/]', title):
            issues.append("HTML/XML markup found in title")
        return issues

    # For Latin-script languages: standard allowed set
    allowed_chars = cfg.get(
        "checks.book_name.allowed_chars",
        r"A-Za-z0-9 ,.:;'\"/()\\[\]\-\–\—&@#%+=\u00C0-\u024F"  # include Latin extended
    )
    allowed_pattern = re.compile(f"^[{allowed_chars}]*$")
    if not allowed_pattern.match(title):
        illegal = sorted(set(c for c in title if not re.match(f"[{allowed_chars}]", c)))
        if illegal:
            return [f"Disallowed characters for {lang_code or 'unknown'} language: {illegal[:20]}"]
    return []


def run(
    parsed_xml: ParsedXML,
    source_doc: Optional[SourceDocument] = None,
    ai_client=None,
    **_kwargs,
) -> CheckResult:
    title = (parsed_xml.content_title or "").strip()
    declared_lang = _normalise_lang_code(parsed_xml.language or "")

    # ── Blank title ──────────────────────────────────────────────────────────
    if _title_is_blank_or_placeholder(title):
        return CheckResult(
            check_number=1,
            check_name="Book Name",
            qa_status=QAStatus.FAIL,
            execution_status=ExecutionStatus.COMPLETED,
            reason="content-title is blank or a placeholder value.",
            findings=[CheckFinding(
                xml_id=None, parent_section_id=None,
                xpath="//metadata/content-title",
                source_location=None,
                observed=f"(blank)" if not title else repr(title),
                expected="A non-blank title matching the document's declared language",
                severity="error",
            )],
            method="metadata-inspection",
            coverage="content-title field only",
            recommended_correction="Supply the book title in its original language.",
            routing=Route.DATA_MANAGEMENT,
        )

    findings: list[CheckFinding] = []
    informational: list[str] = []
    limitations: list[str] = []
    status = QAStatus.PASS
    reason_parts = []

    # ── Character validation (language-aware) ────────────────────────────────
    char_issues = _validate_characters_for_language(title, declared_lang)
    for issue in char_issues:
        findings.append(CheckFinding(
            xml_id=None, parent_section_id=None,
            xpath="//metadata/content-title",
            source_location=None,
            observed=f"Title: {title!r} — {issue}",
            expected=f"Title characters appropriate for declared language '{declared_lang or 'unknown'}'",
            severity="error",
        ))
        status = QAStatus.FAIL
        reason_parts.append(issue)

    # ── Language detection (Unicode block + langdetect fallback) ────────────
    if not declared_lang:
        limitations.append("XML language field is blank — cannot verify title language.")
        informational.append("Set the `language` metadata field so the title language can be validated.")
    elif len(title) >= 3:
        # 1. Unicode block detection (no external library)
        uni_detected, uni_conf = _detect_unicode(title)
        uni_norm = _normalise_lang_code(uni_detected or "")
        declared_norm_check = _normalise_lang_code(declared_lang)

        # 2. langdetect fallback for Latin scripts
        ld_detected = None
        if LANGDETECT_AVAILABLE and len(title) >= 8:
            ld_detected = _detect_language(title)

        detected = uni_detected if uni_conf >= 0.4 else (ld_detected or uni_detected)
        detected_norm = _normalise_lang_code(detected or "")

        if detected is None:
            limitations.append("Language detection inconclusive for this title.")
        elif detected_norm == declared_norm_check or _lang_codes_compatible(detected_norm, declared_norm_check):
            informational.append(
                f"✅ Language verified: title detected as '{detected}' matches declared '{declared_lang}' "
                f"(confidence {uni_conf:.0%} via Unicode analysis)."
            )
            # Keep PASS status
        else:
            if len(title.split()) <= 2:
                limitations.append(
                    f"Title too short for reliable detection ({len(title.split())} words). "
                    f"Detected '{detected}', declared '{declared_lang}'."
                )
            else:
                findings.append(CheckFinding(
                    xml_id=None, parent_section_id=None,
                    xpath="//metadata/content-title",
                    source_location=None,
                    observed=f"Title: {title!r} (detected: '{detected}', declared: '{declared_lang}')",
                    expected=f"Title in declared language '{declared_lang}'",
                    severity="warning",
                ))
                informational.append(
                    f"Detected language '{detected}' does not match declared '{declared_lang}'. "
                    "Legal titles often contain proper nouns — verify the Language field."
                )
                if status == QAStatus.PASS:
                    status = QAStatus.UNVERIFIED
                reason_parts.append(
                    f"Language mismatch: detected '{detected}' vs declared '{declared_lang}'."
                )

    # ── AI language verification from source ─────────────────────────────────
    if source_doc and source_doc.text and ai_client and len(source_doc.text) > 100:
        ai_result = _ai_verify_language(
            source_snippet=source_doc.text[:1500],
            declared_lang=declared_lang,
            xml_title=title,
            ai_client=ai_client,
        )
        if ai_result:
            ai_detected, ai_notes = ai_result
            ai_norm = _normalise_lang_code(ai_detected)
            if ai_norm and not _lang_codes_compatible(ai_norm, declared_lang):
                findings.append(CheckFinding(
                    xml_id=None, parent_section_id=None,
                    xpath="//metadata/language",
                    source_location=source_doc.origin,
                    observed=f"XML language='{declared_lang}' but source content detected as '{ai_detected}'",
                    expected=f"XML language code should match source document language",
                    severity="error",
                ))
                status = QAStatus.FAIL
                reason_parts.append(
                    f"AI verified source language as '{ai_detected}' but XML declares '{declared_lang}'."
                )
                if ai_notes:
                    informational.append(f"AI language analysis: {ai_notes}")
            else:
                informational.append(
                    f"AI verified: source language '{ai_detected}' matches XML declared language '{declared_lang}'. {ai_notes or ''}"
                )

    # ── Body language note ───────────────────────────────────────────────────
    body_title = (parsed_xml.body_title or "").strip()
    if body_title and declared_lang:
        body_non_latin = sum(1 for c in body_title if ord(c) > 127) / max(len(body_title), 1)
        if body_non_latin > 0.3 and declared_lang in _LATIN_FAMILY:
            informational.append(
                f"Body title appears to contain non-Latin characters despite language='{declared_lang}'. "
                "This may indicate a translation mismatch — Check 8 will investigate further."
            )

    # ── Final result ─────────────────────────────────────────────────────────
    if status == QAStatus.PASS:
        reason = (
            f"Title language and characters are consistent with declared language '{declared_lang}'. "
            + " ".join(informational[:1])
        ).strip()
    elif status == QAStatus.UNVERIFIED:
        reason = "; ".join(reason_parts) if reason_parts else "Title could not be fully verified."
    else:
        reason = "; ".join(reason_parts)

    return CheckResult(
        check_number=1,
        check_name="Book Name",
        qa_status=status,
        execution_status=ExecutionStatus.COMPLETED,
        reason=reason,
        findings=findings,
        informational_notes=informational,
        method="language-detection + character-set validation (language-aware)",
        coverage="metadata/content-title + metadata/language",
        recommended_correction=(
            f"Ensure content-title is in the declared language ('{declared_lang}') "
            "and uses only characters appropriate for that language."
        ) if status == QAStatus.FAIL else None,
        routing=Route.DATA_MANAGEMENT if status == QAStatus.FAIL else None,
        verification_limitations=limitations,
    )


def _lang_codes_compatible(detected: str, declared: str) -> bool:
    """Return True if detected and declared language codes are compatible."""
    if detected == declared:
        return True
    # Some common compatible pairs
    compat = {
        ("zh", "zh-cn"), ("zh", "zh-tw"), ("zh-cn", "zh"), ("zh-tw", "zh"),
        ("nb", "no"), ("nn", "no"), ("no", "nb"), ("no", "nn"),
        ("sr", "hr"), ("bs", "hr"),
        ("pt", "pt-br"), ("pt-br", "pt"),
        ("es", "es-419"),
    }
    return (detected, declared) in compat or (declared, detected) in compat


def _ai_verify_language(
    source_snippet: str,
    declared_lang: str,
    xml_title: str,
    ai_client,
) -> Optional[tuple[str, str]]:
    """
    Use AI to detect language of source content.
    Returns (detected_lang_code, explanation) or None on failure.
    """
    try:
        prompt = (
            f"Analyse this text excerpt from a legal/regulatory document and identify its language.\n\n"
            f"TEXT EXCERPT:\n{source_snippet[:1000]}\n\n"
            f"XML TITLE: {xml_title}\n"
            f"DECLARED LANGUAGE CODE: {declared_lang}\n\n"
            "Respond ONLY with a JSON object (no markdown, no preamble):\n"
            '{"detected_language_code": "<ISO 639-1 code e.g. en, ar, pt, fr>", '
            '"confidence": "<high|medium|low>", '
            '"matches_declared": <true|false>, '
            '"notes": "<one sentence explanation>"}'
        )
        import json, anthropic
        msg = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        data = json.loads(raw)
        return data.get("detected_language_code", ""), data.get("notes", "")
    except Exception:
        return None
