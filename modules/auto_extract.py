"""
Auto-extraction utilities — pull real values from source content
to suggest or confirm XML metadata fields.

All functions return None if extraction is uncertain.
Never fabricate values.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional, List, Tuple


# ─── Language detection without langdetect ────────────────────────────────────
# Uses Unicode block analysis — works with zero dependencies.

_SCRIPT_RANGES = {
    "ar": [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "he": [(0x0590, 0x05FF), (0xFB1D, 0xFB4F)],
    "zh": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF)],
    "ja": [(0x3040, 0x309F), (0x30A0, 0x30FF), (0xFF65, 0xFF9F)],
    "ko": [(0xAC00, 0xD7AF), (0x1100, 0x11FF)],
    "ru": [(0x0400, 0x04FF)],
    "el": [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    "th": [(0x0E00, 0x0E7F)],
    "hi": [(0x0900, 0x097F)],
}

_LATIN_LANGS = {"en", "pt", "es", "fr", "de", "it", "nl", "pl", "tr", "id", "ms", "ro", "sv", "da", "no"}


def detect_language_from_text(text: str) -> Tuple[Optional[str], float]:
    """
    Detect language using Unicode block analysis.
    Returns (lang_code, confidence) — no external libraries needed.
    Confidence 0.0–1.0.
    """
    if not text or len(text) < 5:
        return None, 0.0

    sample = text[:500]
    total_chars = len([c for c in sample if not c.isspace()])
    if total_chars == 0:
        return None, 0.0

    script_counts = {lang: 0 for lang in _SCRIPT_RANGES}

    for ch in sample:
        cp = ord(ch)
        for lang, ranges in _SCRIPT_RANGES.items():
            for lo, hi in ranges:
                if lo <= cp <= hi:
                    script_counts[lang] += 1
                    break

    # Find dominant non-Latin script
    dominant_lang = max(script_counts, key=script_counts.get)
    dominant_count = script_counts[dominant_lang]

    if dominant_count / total_chars >= 0.25:
        conf = min(dominant_count / total_chars, 1.0)
        return dominant_lang, round(conf, 2)

    # Mostly Latin — detect sub-language from common words
    text_lower = sample.lower()
    # Portuguese markers
    pt_markers = ["de ", "da ", "do ", " no ", " na ", " que ", "ção", "ões", "não", "são", "também", "resolução"]
    # Spanish markers
    es_markers = ["de ", "del ", "las ", "los ", "que ", "ción", "ones", "también", "resolución"]
    # French markers
    fr_markers = ["de ", "du ", "les ", "des ", " en ", "tion", "ment", "résolution"]
    # Arabic in Latin context
    ar_latin = ["al-", "ibn", "bint"]

    pt_score = sum(1 for m in pt_markers if m in text_lower)
    es_score = sum(1 for m in es_markers if m in text_lower)
    fr_score = sum(1 for m in fr_markers if m in text_lower)

    if pt_score >= 3:
        return "pt", min(pt_score / 10, 0.9)
    if es_score >= 3:
        return "es", min(es_score / 10, 0.9)
    if fr_score >= 3:
        return "fr", min(fr_score / 10, 0.9)

    # Default to English if mostly Latin
    latin_count = sum(1 for c in sample if unicodedata.category(c).startswith("L") and ord(c) < 0x0250)
    if latin_count / max(total_chars, 1) >= 0.5:
        return "en", 0.6

    return None, 0.0


def language_matches_declared(text: str, declared_lang: str) -> Tuple[bool, str]:
    """
    Check if the text language matches the declared XML language code.
    Returns (matches, explanation).
    """
    detected, conf = detect_language_from_text(text)
    if detected is None:
        return True, "Could not detect language from text."

    declared_norm = declared_lang.lower().split("-")[0]
    detected_norm = detected.lower().split("-")[0]

    if detected_norm == declared_norm:
        return True, f"Detected language '{detected}' matches declared '{declared_lang}' (confidence {conf:.0%})."
    # Compatible pairs
    compat = {("nb","no"),("nn","no"),("pt","pt-br"),("zh","zh-cn"),("zh","zh-tw")}
    if (detected_norm, declared_norm) in compat or (declared_norm, detected_norm) in compat:
        return True, f"Detected '{detected}' is compatible with declared '{declared_lang}'."

    return False, (
        f"Detected language '{detected}' (confidence {conf:.0%}) does not match "
        f"declared '{declared_lang}'. Verify the Language metadata field."
    )


# ─── Date extraction from source text ────────────────────────────────────────

_DATE_PATTERNS = [
    # "26 de fevereiro de 2026" / "26 de febrero de 2026"
    (r'\b(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})\b', "dmy_pt"),
    # "February 26, 2026" / "26 February 2026"
    (r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b', "dmy_en"),
    (r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b', "mdy_en"),
    # DD/MM/YYYY or DD-MM-YYYY
    (r'\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b', "dmy_num"),
    # YYYY-MM-DD (ISO)
    (r'\b(20\d{2}|19\d{2})[/\-\.](\d{2})[/\-\.](\d{2})\b', "iso"),
]

_PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
_ES_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_month(name: str) -> Optional[int]:
    n = name.lower()
    return _PT_MONTHS.get(n) or _ES_MONTHS.get(n) or _EN_MONTHS.get(n)


def extract_dates_from_text(text: str) -> List[Tuple[str, str]]:
    """
    Extract all dates from text. Returns list of (iso_date, context_excerpt).
    """
    results = []
    text_clean = text[:5000] if len(text) > 5000 else text

    for pattern, fmt in _DATE_PATTERNS:
        for m in re.finditer(pattern, text_clean, re.IGNORECASE):
            try:
                if fmt == "dmy_pt":
                    day, month_name, year = m.group(1), m.group(2), m.group(3)
                    mo = _parse_month(month_name)
                    if mo:
                        iso = f"{year}-{mo:02d}-{int(day):02d}"
                        ctx = text_clean[max(0, m.start()-30):m.end()+30].strip()
                        results.append((iso, ctx))
                elif fmt == "dmy_en":
                    day, month_name, year = m.group(1), m.group(2), m.group(3)
                    mo = _parse_month(month_name)
                    if mo:
                        iso = f"{year}-{mo:02d}-{int(day):02d}"
                        results.append((iso, m.group(0)))
                elif fmt == "mdy_en":
                    month_name, day, year = m.group(1), m.group(2), m.group(3)
                    mo = _parse_month(month_name)
                    if mo:
                        iso = f"{year}-{mo:02d}-{int(day):02d}"
                        results.append((iso, m.group(0)))
                elif fmt == "dmy_num":
                    d, mo_n, yr = m.group(1), m.group(2), m.group(3)
                    if 1 <= int(mo_n) <= 12 and int(yr) >= 1900:
                        iso = f"{yr}-{int(mo_n):02d}-{int(d):02d}"
                        results.append((iso, m.group(0)))
                elif fmt == "iso":
                    yr, mo_n, d = m.group(1), m.group(2), m.group(3)
                    iso = f"{yr}-{mo_n}-{d}"
                    results.append((iso, m.group(0)))
            except Exception:
                continue

    # Deduplicate
    seen = set()
    unique = []
    for iso, ctx in results:
        if iso not in seen:
            seen.add(iso)
            unique.append((iso, ctx))
    return unique


def extract_issuance_date(source_text: str, doc_number: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """
    Extract the most likely issuance date from source text.
    Prefers dates near the document number/title.
    Returns (iso_date, evidence) or None.
    """
    if not source_text:
        return None

    dates = extract_dates_from_text(source_text)
    if not dates:
        return None

    # If doc number is known, prefer dates near the doc number in text
    if doc_number and doc_number.strip():
        num = re.escape(doc_number.strip().split(".")[0])  # "5280" from "5.280"
        for iso, ctx in dates:
            if re.search(num, ctx, re.IGNORECASE):
                return iso, ctx

    # Prefer dates in the first 500 chars (usually near the title/header)
    header_text = source_text[:500]
    header_dates = extract_dates_from_text(header_text)
    if header_dates:
        return header_dates[0]

    return dates[0]


# ─── Issuance type extraction ─────────────────────────────────────────────────

_TYPE_KEYWORDS = {
    "Resolution":   ["resolução", "resolution", "resolución", "résolution", "قرار"],
    "Circular":     ["circular"],
    "Notice":       ["notice", "aviso", "avis"],
    "Regulation":   ["regulation", "regulamento", "regulación", "règlement", "لائحة"],
    "Directive":    ["directive", "diretiva", "directiva"],
    "Decision":     ["decision", "decisão", "decisión", "décision", "قرار إداري"],
    "Order":        ["order", "ordem", "orden", "ordonnance", "أمر"],
    "Law":          ["law", "lei", "ley", "loi", "قانون", "federal law"],
    "Decree":       ["decree", "decreto", "décret", "مرسوم"],
    "Guideline":    ["guideline", "orientação", "orientación", "ligne directrice"],
    "Standard":     ["standard", "norma", "norme"],
    "Instruction":  ["instruction", "instrução", "instrucción", "تعليمات"],
    "Policy":       ["policy", "política", "politique", "سياسة"],
    "Strategy":     ["strategy", "estratégia", "estrategia", "stratégie", "استراتيجية"],
}


def extract_issuance_type(
    content_title: str,
    body_title: str,
    source_text: str,
    url: str = "",
) -> Optional[Tuple[str, str]]:
    """
    Extract the most likely issuance type from all available text.
    Returns (type_value, evidence) or None.
    """
    combined = f"{content_title} {body_title} {source_text[:300]} {url}".lower()

    scores: dict = {}
    for type_name, keywords in _TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in combined:
                scores[type_name] = scores.get(type_name, 0) + (2 if kw in f"{content_title} {body_title}".lower() else 1)

    if not scores:
        return None

    best = max(scores, key=scores.get)
    evidence = f"Keyword match in title/source (score: {scores[best]})"
    return best, evidence


# ─── Issuing body extraction ──────────────────────────────────────────────────

def extract_issuing_body(source_text: str, url: str = "") -> Optional[Tuple[str, str]]:
    """
    Try to extract the issuing authority from source text.
    Returns (issuing_body, evidence) or None.
    """
    if not source_text:
        return None

    # Common patterns: "Published by X", "Issued by X", "O [Authority]"
    patterns = [
        r'(?:published|issued|promulgated)\s+by\s+([A-ZÀ-Ú][^.;\n]{3,60})',
        r'(?:O|A|The)\s+([A-ZÀ-Ú][^,\n]{5,60}(?:Central|Bank|Ministry|Council|Committee|Commission|Authority|Board|Agency|Federal|National|Bank))',
        r'^([A-ZÀ-Ú][^,\n]{5,60}(?:Central|Bank|Ministry|Council|Committee|Commission|Authority|Board|Agency|Federal|National))',
        r'(?:Signed by|By authority of)\s+([A-ZÀ-Ú][^,\n]{5,60})',
    ]

    for pat in patterns:
        m = re.search(pat, source_text[:2000], re.IGNORECASE | re.MULTILINE)
        if m:
            body = m.group(1).strip().rstrip(",.")
            if 5 < len(body) < 80:
                return body, f"Extracted from source: '{body}'"

    # Try URL domain as hint
    if url:
        domain_hints = {
            "bcb.gov.br": "Banco Central do Brasil",
            "bcb.gov": "Banco Central do Brasil",
            "mof.gov": "Ministry of Finance",
            "sec.gov": "Securities and Exchange Commission",
            "eba.europa.eu": "European Banking Authority",
            "bis.org": "Bank for International Settlements",
            "legislation.gov.uk": "UK Government",
            "federalregister.gov": "US Federal Register",
            "au.int": "African Union",
        }
        for domain, name in domain_hints.items():
            if domain in url.lower():
                return name, f"Inferred from URL domain: {domain}"

    return None


# ─── Corrections builder ──────────────────────────────────────────────────────

def build_auto_corrections(
    parsed_xml,
    source_doc=None,
) -> List[dict]:
    """
    For each known FAIL condition, extract a suggested correction value
    from source content wherever possible.
    Returns list of {field, current_value, suggested_value, evidence, confidence}.
    """
    corrections = []
    src_text = (source_doc.text if source_doc else None) or ""
    src_url  = (source_doc.origin if source_doc else None) or ""

    # IssuanceType
    if not (parsed_xml.issuance_type or "").strip():
        result = extract_issuance_type(
            parsed_xml.content_title or "",
            parsed_xml.body_title or "",
            src_text,
            src_url,
        )
        if result:
            corrections.append({
                "field": "IssuanceType",
                "xpath": "//metadata/IssuanceType",
                "current": "(blank)",
                "suggested": result[0],
                "evidence": result[1],
                "confidence": "Medium — verify against approved vocabulary",
            })

    # Issuance date
    if (parsed_xml.issue_date_status or "").strip() in (
        "Date Replaced By Capture Date", "Capture Date Used", "No Date Found", ""
    ):
        if src_text:
            doc_num = (parsed_xml.content_number or "").strip()
            result = extract_issuance_date(src_text, doc_num)
            if result:
                corrections.append({
                    "field": "issue-date",
                    "xpath": "//metadata/issue-date",
                    "current": (parsed_xml.issue_date or "(capture date substitute)"),
                    "suggested": result[0],
                    "evidence": f"Found in source: '{result[1][:80]}'",
                    "confidence": "High — extracted from source document",
                })

    # Issuing body DEFAULT
    if (parsed_xml.issuing_body or "").strip().upper() == "DEFAULT":
        result = extract_issuing_body(src_text, src_url)
        if result:
            corrections.append({
                "field": "issuing-body",
                "xpath": "//metadata/issuing-body",
                "current": "DEFAULT",
                "suggested": result[0],
                "evidence": result[1],
                "confidence": "Medium — verify in RM taxonomy",
            })

    return corrections
