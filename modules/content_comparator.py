"""
Content comparison utilities for CUBE QA.

Key principles:
- Keep raw and normalised text separate
- Normalise harmless whitespace/presentation differences
- Preserve meaningful differences (numbers, dates, missing text)
- Do not use similarity score alone as Pass criterion
- Styling differences (bold/italic/caps) must not fail content fidelity
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher, unified_diff
from typing import Dict, List, Optional, Tuple


# ─── Text normalisation ───────────────────────────────────────────────────────

def normalise_whitespace(text: str) -> str:
    """Collapse whitespace but preserve paragraph breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of spaces/tabs on one line
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse runs of newlines to at most two
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalise_for_comparison(text: str, preserve_case: bool = False) -> str:
    """
    Normalise text for comparison purposes.
    Does NOT remove: numbers, dates, special characters.
    Does NOT change: content meaning.
    DOES normalise: unicode NFC, whitespace, optional casing.

    Styling differences (bold, italic, capitalization-only) must not become failures.
    The checklist says capitalization-only changes are excluded.
    """
    if not text:
        return ""
    # Unicode NFC normalisation
    text = unicodedata.normalize("NFC", text)
    # Normalise whitespace
    text = normalise_whitespace(text)
    # Decode XML entities that may have been left as literals
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # Normalise non-breaking spaces
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    if not preserve_case:
        text = text.lower()
    return text.strip()


def extract_numbers(text: str) -> List[str]:
    """Extract all numeric values from text for targeted comparison."""
    return re.findall(r"\b\d[\d.,]*\b", text)


def extract_dates(text: str) -> List[str]:
    """Extract date-like patterns for comparison."""
    patterns = [
        r"\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b",
        r"\b\d{4}[/\-\.]\d{2}[/\-\.]\d{2}\b",
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2,4}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
        r"\b\d{1,2}\s+de\s+\w+\s+de\s+\d{4}\b",  # Portuguese date format
    ]
    results = []
    for pat in patterns:
        results.extend(re.findall(pat, text, re.IGNORECASE))
    return results


# ─── Diff utilities ───────────────────────────────────────────────────────────

def similarity_ratio(text_a: str, text_b: str) -> float:
    """Return SequenceMatcher ratio between two normalised strings (0–1)."""
    a = normalise_for_comparison(text_a)
    b = normalise_for_comparison(text_b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def find_differences(
    source_text: str,
    xml_text: str,
    max_excerpt_chars: int = 300,
) -> List[Dict]:
    """
    Find meaningful differences between source and XML text.
    Returns a list of diffs with context.
    Does NOT flag styling-only differences as errors.
    """
    diffs = []
    source_norm = normalise_for_comparison(source_text)
    xml_norm = normalise_for_comparison(xml_text)

    # Split into sentences/fragments for granular comparison
    source_frags = _split_fragments(source_norm)
    xml_frags = _split_fragments(xml_norm)

    matcher = SequenceMatcher(None, source_frags, xml_frags)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        source_chunk = " ".join(source_frags[i1:i2])
        xml_chunk = " ".join(xml_frags[j1:j2])

        if tag == "replace":
            # Check if this is styling-only (capitalization difference)
            if source_chunk.lower() == xml_chunk.lower():
                continue  # Capitalization-only: not a failure per checklist
            diff_type = "replaced"
        elif tag == "delete":
            diff_type = "missing_from_xml"
        elif tag == "insert":
            diff_type = "extra_in_xml"
        else:
            continue

        diffs.append({
            "type": diff_type,
            "source_excerpt": source_chunk[:max_excerpt_chars],
            "xml_excerpt": xml_chunk[:max_excerpt_chars],
            "severity": "warning" if len(source_chunk.split()) <= 3 else "error",
        })

    return diffs


def _split_fragments(text: str) -> List[str]:
    """Split text into comparable fragments."""
    frags = re.split(r"(?<=[.!?;])\s+", text)
    result = []
    for frag in frags:
        frag = frag.strip()
        if frag:
            result.append(frag)
    return result


def check_number_fidelity(source_text: str, xml_text: str) -> List[Dict]:
    """
    Specifically check that numbers in the source appear in the XML.
    A missing or changed number/date is always an error regardless of overall similarity.
    """
    issues = []
    source_nums = set(extract_numbers(source_text))
    xml_nums = set(extract_numbers(xml_text))

    missing = source_nums - xml_nums
    extra = xml_nums - source_nums

    for n in sorted(missing):
        issues.append({"type": "number_missing", "value": n, "severity": "error"})
    for n in sorted(extra):
        issues.append({"type": "number_extra", "value": n, "severity": "warning"})

    # Date comparison
    source_dates = set(d.lower() for d in extract_dates(source_text))
    xml_dates = set(d.lower() for d in extract_dates(xml_text))
    for d in sorted(source_dates - xml_dates):
        issues.append({"type": "date_missing", "value": d, "severity": "error"})

    return issues


def align_sections(
    source_headings: List[str],
    xml_titles: List[str],
) -> List[Tuple[Optional[int], Optional[int], float]]:
    """
    Align source headings to XML section titles.
    Returns list of (source_idx, xml_idx, similarity).
    """
    alignments = []
    used_xml = set()

    for si, sh in enumerate(source_headings):
        sh_norm = normalise_for_comparison(sh)
        best_sim = 0.0
        best_xi = None

        for xi, xt in enumerate(xml_titles):
            if xi in used_xml:
                continue
            xt_norm = normalise_for_comparison(xt)
            sim = SequenceMatcher(None, sh_norm, xt_norm).ratio()
            if sim > best_sim:
                best_sim = sim
                best_xi = xi

        if best_sim >= 0.5:
            alignments.append((si, best_xi, best_sim))
            if best_xi is not None:
                used_xml.add(best_xi)
        else:
            alignments.append((si, None, best_sim))

    # Mark unmatched XML sections
    for xi in range(len(xml_titles)):
        if xi not in used_xml:
            alignments.append((None, xi, 0.0))

    return alignments
