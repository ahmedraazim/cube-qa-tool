"""
Read-only RM integration adapter.

IMPORTANT: No RM API documentation was available at build time.
The live integration adapter is UNCONFIGURED and will clearly say so.

This module provides:
1. RMConnectionPanel — status/metadata display
2. RMAdapter — interface definition + export-based fallback
3. No write operations exist anywhere in this module.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import MetadataProvenance, RMMetadata

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False


# ─── RM URL parsing ───────────────────────────────────────────────────────────

_BOOK_ID_PATTERN = re.compile(
    r"/book/review/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def extract_book_id_from_url(url: str) -> Optional[str]:
    """Extract book UUID from an RM review URL. Does NOT validate live access."""
    m = _BOOK_ID_PATTERN.search(url)
    return m.group(1).lower() if m else None


# ─── Connection state ─────────────────────────────────────────────────────────

@dataclass
class RMConnectionState:
    review_url: str = ""
    book_id_from_url: Optional[str] = None
    connection_attempted: bool = False
    connection_successful: bool = False
    authentication_status: str = "Not attempted"
    error: Optional[str] = None
    metadata: Optional[RMMetadata] = None
    retrieval_time: Optional[datetime] = None
    provenance: MetadataProvenance = MetadataProvenance.XML_ONLY

    @property
    def is_connected(self) -> bool:
        return self.connection_successful and self.metadata is not None

    @property
    def display_status(self) -> str:
        if not self.review_url:
            return "Not connected"
        if not self.connection_attempted:
            return "URL entered — not connected"
        if self.is_connected:
            return f"Connected ({self.provenance.value})"
        return f"Connection failed: {self.error or 'unknown error'}"


# ─── Adapter interface ────────────────────────────────────────────────────────

# ─── Selenium RDM page scraper ───────────────────────────────────────────────

# Field label mappings — covers English and common CUBE UI labels
_FIELD_MAP = {
    # Book / document identity
    "book name":        "book_name",
    "content title":    "book_name",
    "title":            "book_name",
    "عنوان":            "book_name",
    "citation":         "citation",
    "اقتباس":           "citation",

    # Type
    "issuance type":    "issuance_type",
    "document type":    "issuance_type",
    "type":             "issuance_type",
    "نوع الإصدار":      "issuance_type",

    # Dates
    "issuance date":    "issuance_date",
    "issue date":       "issuance_date",
    "date of issue":    "issuance_date",
    "effective date":   "effective_date",
    "compliance date":  "compliance_date",

    # Jurisdiction / issuing body
    "jurisdiction":     "jurisdiction",
    "country":          "jurisdiction",
    "country of issue": "jurisdiction",
    "issuing body":     "issuing_body",
    "issuing authority":"issuing_body",
    "publisher":        "issuing_body",
    "الجهة المصدرة":    "issuing_body",

    # Language / translation
    "language":         "language",
    "translation":      "has_translation_tab",

    # URL
    "source url":       "source_url",
    "url":              "source_url",
    "link":             "source_url",
}


def _selenium_options():
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--ignore-ssl-errors")
    opts.add_argument("--allow-running-insecure-content")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    return opts


def _make_driver():
    """Create a Selenium driver, trying Chrome then Edge."""
    opts = _selenium_options()
    try:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            svc = ChromeService(ChromeDriverManager().install())
        except Exception:
            svc = ChromeService()
        return webdriver.Chrome(service=svc, options=opts), None
    except Exception as e1:
        # Try Edge
        try:
            from selenium.webdriver.edge.options import Options as EdgeOptions
            from selenium.webdriver.edge.service import Service as EdgeService
            eopts = EdgeOptions()
            for arg in ["--headless=new","--no-sandbox","--disable-dev-shm-usage",
                        "--disable-gpu","--ignore-certificate-errors","--ignore-ssl-errors"]:
                eopts.add_argument(arg)
            try:
                from webdriver_manager.microsoft import EdgeChromiumDriverManager
                esvc = EdgeService(EdgeChromiumDriverManager().install())
            except Exception:
                esvc = EdgeService()
            return webdriver.Edge(service=esvc, options=eopts), None
        except Exception as e2:
            return None, f"Chrome failed: {e1}; Edge failed: {e2}"


def _extract_page_fields(driver) -> Dict[str, str]:
    """
    Extract label-value pairs from a rendered page.
    Tries multiple strategies to cover different UI frameworks.
    """
    fields: Dict[str, str] = {}

    # Strategy 1: explicit <label> + following input/span
    try:
        labels = driver.find_elements(By.TAG_NAME, "label")
        for lbl in labels:
            text = lbl.text.strip()
            if not text:
                continue
            # Try to get the associated value element
            for_id = lbl.get_attribute("for")
            value = ""
            if for_id:
                try:
                    el = driver.find_element(By.ID, for_id)
                    value = el.get_attribute("value") or el.text or ""
                except Exception:
                    pass
            if not value:
                # Try sibling / parent content
                try:
                    parent = lbl.find_element(By.XPATH, "..")
                    spans = parent.find_elements(By.TAG_NAME, "span")
                    inputs = parent.find_elements(By.TAG_NAME, "input")
                    if spans:
                        value = " ".join(s.text for s in spans if s.text.strip())
                    elif inputs:
                        value = inputs[0].get_attribute("value") or ""
                except Exception:
                    pass
            if text and value:
                fields[text] = value.strip()
    except Exception:
        pass

    # Strategy 2: definition lists <dt>/<dd>
    try:
        dts = driver.find_elements(By.TAG_NAME, "dt")
        dds = driver.find_elements(By.TAG_NAME, "dd")
        for dt, dd in zip(dts, dds):
            if dt.text.strip():
                fields[dt.text.strip()] = dd.text.strip()
    except Exception:
        pass

    # Strategy 3: table rows with th/td or two td
    try:
        rows = driver.find_elements(By.TAG_NAME, "tr")
        for row in rows:
            cells = row.find_elements(By.CSS_SELECTOR, "th, td")
            if len(cells) == 2:
                k, v = cells[0].text.strip(), cells[1].text.strip()
                if k and v and len(k) < 60:
                    fields[k] = v
    except Exception:
        pass

    # Strategy 4: elements with data-label or aria-label attributes
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "[data-label], [data-field]"):
            label = el.get_attribute("data-label") or el.get_attribute("data-field")
            if label and el.text.strip():
                fields[label] = el.text.strip()
    except Exception:
        pass

    # Strategy 5: common field wrapper patterns (Bootstrap, Material, Angular)
    field_selectors = [
        ".field-wrapper", ".form-group", ".metadata-field",
        ".detail-row", ".info-row", ".property-row",
        "[class*='field-']", "[class*='metadata-']",
    ]
    for sel in field_selectors:
        try:
            for container in driver.find_elements(By.CSS_SELECTOR, sel):
                texts = [el.text.strip() for el in
                         container.find_elements(By.CSS_SELECTOR, "label, .label, .key")]
                values = [el.text.strip() for el in
                          container.find_elements(By.CSS_SELECTOR, "span, .value, input, p")]
                if texts and values:
                    fields[texts[0]] = values[0]
        except Exception:
            continue

    return fields


def _map_fields(raw_fields: Dict[str, str]) -> Dict[str, str]:
    """Map scraped labels to known field names using _FIELD_MAP."""
    result: Dict[str, str] = {}
    for label, value in raw_fields.items():
        label_norm = label.lower().strip().rstrip(":")
        for pattern, field_name in _FIELD_MAP.items():
            if pattern in label_norm or label_norm in pattern:
                if field_name not in result:
                    result[field_name] = value
                break
    return result


def scrape_rdm_page(review_url: str) -> Tuple[Optional[RMMetadata], str, Dict[str, str]]:
    """
    Use Selenium to open the RDM review page and extract all visible metadata.
    Returns (RMMetadata, status_message, raw_fields_dict).
    Never writes to RM — read-only scrape only.
    """
    if not SELENIUM_AVAILABLE:
        return None, "Selenium not installed. Run: pip install selenium webdriver-manager", {}

    driver, err = _make_driver()
    if driver is None:
        return None, f"Browser not available: {err}", {}

    raw_fields: Dict[str, str] = {}
    try:
        driver.set_page_load_timeout(35)
        driver.get(review_url)

        # Wait for the page to render — try multiple selectors
        render_selectors = [
            "[class*='metadata']", "[class*='field']",
            "label", "dt", "table", "form",
            "[class*='book']", "[class*='review']",
            "main", "article", ".content",
        ]
        for sel in render_selectors:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                break
            except TimeoutException:
                continue

        time.sleep(3)  # let remaining JS settle

        # Scroll to load lazy content
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")

        raw_fields = _extract_page_fields(driver)

        if not raw_fields:
            # Fallback: get all visible text and try to parse
            page_text = driver.find_element(By.TAG_NAME, "body").text
            status = (
                f"Page rendered but no structured fields detected. "
                f"Page text length: {len(page_text)} chars. "
                "The RDM page may use a UI pattern not yet covered."
            )
            return None, status, {"_page_text": page_text[:3000]}

    except WebDriverException as exc:
        return None, f"Browser error opening RDM: {str(exc)[:200]}", {}
    except Exception as exc:
        return None, f"Scraping error: {str(exc)[:200]}", {}
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    mapped = _map_fields(raw_fields)

    if not mapped:
        return (
            None,
            f"Page opened but no metadata fields were recognised. "
            f"Found {len(raw_fields)} raw fields: {list(raw_fields.keys())[:10]}",
            raw_fields,
        )

    meta = RMMetadata(
        book_name=mapped.get("book_name"),
        citation=mapped.get("citation"),
        issuance_type=mapped.get("issuance_type"),
        issuance_date=mapped.get("issuance_date"),
        effective_date=mapped.get("effective_date"),
        compliance_date=mapped.get("compliance_date"),
        jurisdiction=mapped.get("jurisdiction"),
        issuing_body=mapped.get("issuing_body"),
        language=mapped.get("language"),
        source_url=mapped.get("source_url"),
        retrieval_time=datetime.utcnow(),
        provenance=MetadataProvenance.LIVE_RM,
        raw_data=raw_fields,
    )

    status = (
        f"RDM page scraped successfully. "
        f"{len(mapped)} metadata field(s) extracted from {len(raw_fields)} visible fields."
    )
    return meta, status, raw_fields



class RMAdapter:
    """
    Read-only RM metadata adapter.

    Live API integration: UNCONFIGURED (no documented API available).
    Export-based fallback: supported via load_export().

    No write operations, approval/rejection controls, publishing,
    routing, or comment submission exist in this class.
    """

    LIVE_INTEGRATION_STATUS = (
        "UNCONFIGURED — RM API documentation and credentials were not available "
        "at build time. The live adapter interface is defined but not connected. "
        "Provide RM_BASE_URL and RM_API_TOKEN environment variables and implement "
        "the _fetch_live() method once API documentation is available."
    )

    def __init__(self) -> None:
        self._base_url = os.environ.get("RM_BASE_URL", "").rstrip("/")
        self._token = os.environ.get("RM_API_TOKEN", "")
        self._timeout = int(os.environ.get("RM_TIMEOUT", "15"))

    @property
    def live_configured(self) -> bool:
        return bool(self._base_url and self._token)

    def connect(self, review_url: str) -> RMConnectionState:
        """
        Open the RDM review URL with Selenium and scrape all visible metadata.
        Fully read-only — no write operations.
        """
        state = RMConnectionState(review_url=review_url)
        state.book_id_from_url = extract_book_id_from_url(review_url)
        state.connection_attempted = True

        if not review_url or not review_url.strip().startswith("http"):
            state.error = "No valid URL provided."
            state.authentication_status = "Not attempted"
            return state

        if not SELENIUM_AVAILABLE:
            state.error = "Selenium not installed. Run: pip install selenium webdriver-manager"
            state.authentication_status = "Selenium unavailable"
            return state

        meta, status_msg, raw_fields = scrape_rdm_page(review_url)

        state.connection_attempted = True
        if meta is not None:
            state.metadata = meta
            state.connection_successful = True
            state.authentication_status = "Page scraped successfully"
            state.provenance = MetadataProvenance.LIVE_RM
            state.retrieval_time = datetime.utcnow()
        else:
            state.error = status_msg
            state.authentication_status = "Scraping failed"
            # Store raw fields for debugging
            if raw_fields:
                state.metadata = RMMetadata(raw_data=raw_fields)

        return state

    def load_export(self, export_path: Path) -> RMConnectionState:
        """
        Load RM metadata from an exported JSON file (fallback when live access is unavailable).
        """
        state = RMConnectionState()
        state.connection_attempted = True

        try:
            raw = json.loads(export_path.read_text(encoding="utf-8"))
        except Exception as exc:
            state.error = f"Could not read RM export: {exc}"
            return state

        try:
            meta = RMMetadata(
                book_name=raw.get("bookName") or raw.get("book_name") or raw.get("name"),
                citation=raw.get("citation"),
                issuance_type=raw.get("issuanceType") or raw.get("issuance_type"),
                issuance_date=raw.get("issuanceDate") or raw.get("issuance_date"),
                effective_date=raw.get("effectiveDate") or raw.get("effective_date"),
                compliance_date=raw.get("complianceDate") or raw.get("compliance_date"),
                jurisdiction=raw.get("jurisdiction"),
                issuing_body=raw.get("issuingBody") or raw.get("issuing_body"),
                language=raw.get("language"),
                source_url=raw.get("sourceUrl") or raw.get("source_url"),
                has_translation_tab=raw.get("hasTranslationTab"),
                has_native_tab=raw.get("hasNativeTab"),
                toc_sections=raw.get("tocSections") or raw.get("toc_sections") or [],
                retrieval_time=datetime.utcnow(),
                provenance=MetadataProvenance.EXPORTED_RM,
                raw_data=raw,
            )

            state.metadata = meta
            state.connection_successful = True
            state.review_url = raw.get("reviewUrl") or raw.get("review_url") or ""
            state.book_id_from_url = raw.get("bookId") or raw.get("book_id")
            state.authentication_status = "Loaded from export"
            state.provenance = MetadataProvenance.EXPORTED_RM
            state.retrieval_time = datetime.utcnow()

        except Exception as exc:
            state.error = f"RM export parse error: {exc}"

        return state


def compare_xml_rm(
    parsed_xml,
    rm_meta: RMMetadata,
) -> list[dict]:
    """
    Compare XML metadata with RM metadata.
    Returns a list of comparison rows: {field, xml_value, rm_value, finding}.
    Neither source overwrites the other; conflicts remain visible.
    """
    rows = []

    def _row(field: str, xml_val: str, rm_val: str) -> dict:
        xml_n = (xml_val or "").strip()
        rm_n = (rm_val or "").strip()
        if not xml_n and not rm_n:
            finding = "Both blank"
        elif not xml_n:
            finding = "Missing in XML"
        elif not rm_n:
            finding = "Not available from RM"
        elif xml_n.lower() == rm_n.lower():
            finding = "Match"
        else:
            finding = "CONFLICT"
        return {"field": field, "xml_value": xml_n or "(blank)", "rm_value": rm_n or "(blank)", "finding": finding}

    rows.append(_row("Book Name / Content Title", parsed_xml.content_title, rm_meta.book_name))
    rows.append(_row("Citation", parsed_xml.citation, rm_meta.citation))
    rows.append(_row("Issuance Type", parsed_xml.issuance_type, rm_meta.issuance_type))
    rows.append(_row("Issuance Date", parsed_xml.issue_date, rm_meta.issuance_date))
    rows.append(_row("Jurisdiction / Country", parsed_xml.country_of_issue, rm_meta.jurisdiction))
    rows.append(_row("Issuing Body", parsed_xml.issuing_body, rm_meta.issuing_body))
    rows.append(_row("Language", parsed_xml.language, rm_meta.language))
    rows.append(_row("Source URL", parsed_xml.url_link, rm_meta.source_url))

    return rows
