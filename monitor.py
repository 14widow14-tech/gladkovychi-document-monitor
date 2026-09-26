#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state.json"
CHANGES_FILE = ROOT / "changes.json"

DOC_ID_RE = re.compile(r"/docs/(\d+)/?$")
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
NUMBER_RE = re.compile(r"№\s*([^\s<]+)")
FILES_COUNT_RE = re.compile(r"(\d+)\s+файл", re.I)
EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".odt",
    ".rtf",
    ".zip",
    ".rar",
    ".jpg",
    ".jpeg",
    ".png",
    ".ppt",
    ".pptx",
)
BRIEF_FIELDS = ("url", "type", "title", "date", "number", "listed_files")

SOURCE_PARTS = urlparse(CONFIG["documents_url"])
SOURCE_HOST = (SOURCE_PARTS.hostname or "").lower()

session = requests.Session()
session.headers.update(
    {
        "User-Agent": CONFIG["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk,en;q=0.8",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def norm(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_http_url(
    value: str, base: str, allowed_host: Optional[str] = None
) -> Optional[str]:
    url = urljoin(base, value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if allowed_host and parsed.hostname.lower() != allowed_host.lower():
        return None
    return url


def fetch(url: str) -> requests.Response:
    last_error: Optional[Exception] = None
    attempts = int(CONFIG.get("request_attempts", 3))
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=float(CONFIG["request_timeout"]))
            response.raise_for_status()
            return response
        except Exception as exc:  # requests exposes several transport exceptions
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(float(CONFIG.get("retry_backoff_seconds", 2)) * (2**attempt))
    raise RuntimeError(f"Не вдалося завантажити {url}: {last_error}")


def detected_last_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    for anchor in soup.select(".pagination a[href]"):
        try:
            query = parse_qs(urlparse(anchor["href"]).query)
            if CONFIG["page_param"] in query:
                pages.append(int(query[CONFIG["page_param"]][0]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(pages)


def parse_listing(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    documents: dict[str, dict] = {}
    for card in soup.select(".one_doc"):
        anchor = card.select_one(".title a[href*='/docs/']")
        if not anchor:
            continue
        url = safe_http_url(anchor.get("href", ""), CONFIG["documents_url"], SOURCE_HOST)
        if not url:
            continue
        match = DOC_ID_RE.search(urlparse(url).path)
        if not match:
            continue

        doc_id = match.group(1)
        type_element = card.select_one(".type")
        bottom_element = card.select_one(".bottom")
        bottom = norm(bottom_element.get_text(" ", strip=True) if bottom_element else "")
        date_match = DATE_RE.search(bottom)
        number_match = NUMBER_RE.search(bottom)
        files_match = FILES_COUNT_RE.search(bottom)
        documents[doc_id] = {
            "id": doc_id,
            "url": url,
            "type": norm(type_element.get_text(" ", strip=True) if type_element else ""),
            "title": norm(anchor.get_text(" ", strip=True)),
            "date": date_match.group(1) if date_match else "",
            "number": number_match.group(1) if number_match else "",
            "listed_files": int(files_match.group(1)) if files_match else None,
        }
    return documents


def parse_detail(brief: dict) -> dict:
    response = fetch(brief["url"])
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.select_one("#main_content") or soup

    metadata: dict[str, str] = {}
    for row in main.select("table tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        key = norm(cells[0].get_text(" ", strip=True)).rstrip(":")
        value = norm(cells[1].get_text(" ", strip=True))
        if key:
            metadata[key] = value

    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in main.select("a[href]"):
        url = safe_http_url(anchor.get("href", ""), brief["url"])
        if not url:
            continue
        clean_path = urlparse(url).path.lower()
        if clean_path.endswith(EXTENSIONS) and url not in seen:
            seen.add(url)
            attachments.append(
                {"url": url, "label": norm(anchor.get_text(" ", strip=True))}
            )

    heading = main.find("h1")
    document = {
        "id": brief["id"],
        "url": brief["url"],
        "type": metadata.get("Тип документу", brief.get("type", "")),
        "date": metadata.get("Дата", brief.get("date", "")),
        "number": metadata.get("Номер документу", brief.get("number", "")),
        "title": metadata.get(
            "Назва документу",
            norm(heading.get_text(" ", strip=True) if heading else brief.get("title", "")),
        ),
        "listed_files": brief.get("listed_files"),
        "attachments": sorted(attachments, key=lambda item: item["url"]),
        "detail_ok": True,
    }
    document["content_hash"] = digest(document)
    return document


def fallback_document(brief: dict) -> dict:
    document = dict(brief)
    document["attachments"] = []
    document["detail_ok"] = False
    document["content_hash"] = digest(document)
    return document


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Пошкоджений JSON {path.name}: {exc}") from exc


def save_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def set_action_output(name: str, value: object) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={str(value).lower()}\n")


def brief_changed(old: dict, brief: dict) -> bool:
    return any(old.get(field) != brief.get(field) for field in BRIEF_FIELDS)


def is_recent(date_value: str, checked_at: datetime) -> bool:
    date = parse_date(date_value)
    if not date:
        return False
    age_days = (checked_at - date).days
    return age_days <= int(CONFIG["recent_document_days"])


def should_run_full_scan(old_state: dict, checked_at: datetime) -> bool:
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    if not old_state.get("documents"):
        return True
    previous = parse_iso(old_state.get("last_full_scan"))
    if not previous:
        return True
    elapsed_hours = (checked_at - previous).total_seconds() / 3600
    return elapsed_hours >= float(CONFIG["full_scan_interval_hours"])


def attachment_urls(documents: dict[str, dict]) -> set[str]:
    return {
        attachment["url"]
        for document in documents.values()
        for attachment in document.get("attachments", [])
        if attachment.get("url")
    }


def main() -> None:
    checked_dt = utc_now()
    checked = iso(checked_dt)
    old_state = load_json(STATE_FILE, {})
    if not isinstance(old_state, dict):
        raise RuntimeError("state.json повинен містити JSON-об'єкт")
    old_documents = old_state.get("documents", {})
    if not isinstance(old_documents, dict):
        raise RuntimeError("state.json: documents повинен бути JSON-об'єктом")

    first_response = fetch(CONFIG["documents_url"])
    last_page = detected_last_page(first_response.text)
    max_pages = int(CONFIG["max_pages"])
    if last_page > max_pages:
        raise RuntimeError(
            f"Каталог містить щонайменше {last_page} сторінок, "
            f"що перевищує захисний ліміт {max_pages}."
        )

    listing = parse_listing(first_response.text)
    page_counts = [len(listing)]
    if not listing:
        raise RuntimeError("Перша сторінка каталогу не містить жодної розпізнаної картки")

    for page in range(2, last_page + 1):
        url = f"{CONFIG['documents_url']}?{CONFIG['page_param']}={page}"
        page_documents = parse_listing(fetch(url).text)
        if not page_documents:
            raise RuntimeError(f"Сторінка каталогу {page} не містить розпізнаних карток")
        page_counts.append(len(page_documents))
        listing.update(page_documents)

    if old_documents:
        ratio = len(listing) / max(len(old_documents), 1)
        minimum_ratio = float(CONFIG["minimum_listing_ratio"])
        if ratio < minimum_ratio:
            raise RuntimeError(
                f"Захист від масового хибного видалення: отримано {len(listing)} "
                f"із попередніх {len(old_documents)} документів ({ratio:.1%}); "
                f"мінімум {minimum_ratio:.0%}. Стан не змінено."
            )

    full_scan = should_run_full_scan(old_state, checked_dt)
    current: dict[str, dict] = {}
    errors: list[dict[str, str]] = []
    detail_requests = 0
    delay = float(CONFIG["request_delay_seconds"])

    for doc_id, brief in sorted(listing.items(), key=lambda item: int(item[0]), reverse=True):
        old = old_documents.get(doc_id)
        refresh = (
            full_scan
            or old is None
            or old.get("detail_ok") is False
            or brief_changed(old, brief)
            or is_recent(brief.get("date", ""), checked_dt)
        )
        if not refresh:
            current[doc_id] = old
            continue

        try:
            current[doc_id] = parse_detail(brief)
        except Exception as exc:
            errors.append({"id": doc_id, "url": brief["url"], "error": str(exc)})
            current[doc_id] = old if old is not None else fallback_document(brief)
        finally:
            detail_requests += 1
            if delay > 0:
                time.sleep(delay)

    previous_missing = old_state.get("missing_counts", {})
    if not isinstance(previous_missing, dict):
        previous_missing = {}
    missing_counts: dict[str, int] = {}
    confirmed_removed: list[str] = []
    confirmations = int(CONFIG["removal_confirmations"])
    for doc_id in set(old_documents) - set(listing):
        misses = int(previous_missing.get(doc_id, 0)) + 1
        if misses >= confirmations:
            confirmed_removed.append(doc_id)
        else:
            missing_counts[doc_id] = misses
            current[doc_id] = old_documents[doc_id]

    old_ids = set(old_documents)
    new_ids = set(current)
    added_ids = sorted(new_ids - old_ids, key=int, reverse=True)
    removed_ids = sorted(confirmed_removed, key=int, reverse=True)
    changed_ids = sorted(
        [
            doc_id
            for doc_id in old_ids & new_ids
            if old_documents[doc_id].get("content_hash")
            != current[doc_id].get("content_hash")
        ],
        key=int,
        reverse=True,
    )

    old_attachments = attachment_urls(old_documents)
    new_attachments = attachment_urls(current)
    added_attachments = sorted(new_attachments - old_attachments)
    removed_attachments = sorted(old_attachments - new_attachments)
    baseline = not bool(old_documents)

    last_change = {
        "added_documents": 0 if baseline else len(added_ids),
        "changed_documents": 0 if baseline else len(changed_ids),
        "removed_documents": 0 if baseline else len(removed_ids),
        "added_attachments": 0 if baseline else len(added_attachments),
        "removed_attachments": 0 if baseline else len(removed_attachments),
    }
    material_change = any(last_change.values())
    missing_changed = missing_counts != previous_missing
    should_commit = baseline or full_scan or material_change or bool(errors) or missing_changed
    should_record_event = should_commit

    last_full_scan = checked if full_scan else old_state.get("last_full_scan")
    event = {
        "checked_at": checked,
        "first_run": baseline,
        "full_scan": full_scan,
        "source_ok": not errors,
        "source_url": CONFIG["documents_url"],
        "pages_scanned": last_page,
        "listing_documents": len(listing),
        "documents_current": len(current),
        "attachments_current": len(new_attachments),
        "detail_requests": detail_requests,
        "added_documents": [] if baseline else [current[item] for item in added_ids],
        "changed_documents": []
        if baseline
        else [
            {"before": old_documents[item], "after": current[item]}
            for item in changed_ids
        ],
        "removed_documents": []
        if baseline
        else [old_documents[item] for item in removed_ids],
        "added_attachments": [] if baseline else added_attachments,
        "removed_attachments": [] if baseline else removed_attachments,
        "pending_missing": [
            {"id": item, "misses": count}
            for item, count in sorted(missing_counts.items(), key=lambda pair: int(pair[0]))
        ],
        "errors": errors,
    }

    history = load_json(CHANGES_FILE, {"events": []})
    if not isinstance(history, dict) or not isinstance(history.get("events", []), list):
        raise RuntimeError("changes.json повинен містити об'єкт із масивом events")
    events = history.get("events", [])
    if should_record_event:
        events.insert(0, event)
        events = events[: int(CONFIG["keep_change_events"])]
        save_json(CHANGES_FILE, {"events": events})

    state = {
        "schema_version": 2,
        "site_name": CONFIG["site_name"],
        "source_url": CONFIG["documents_url"],
        "checked_at": checked,
        "last_full_scan": last_full_scan,
        "source_ok": not errors,
        "pages_scanned": last_page,
        "page_counts": page_counts,
        "listing_documents_count": len(listing),
        "documents_count": len(current),
        "attachments_count": len(new_attachments),
        "detail_requests": detail_requests,
        "last_change": last_change,
        "pending_missing": len(missing_counts),
        "missing_counts": missing_counts,
        "errors": errors,
        "documents": current,
    }
    save_json(STATE_FILE, state)

    set_action_output("should_commit", should_commit)
    set_action_output("full_scan", full_scan)
    set_action_output("source_ok", not errors)
    print(
        f"OK: listing={len(listing)}, current={len(current)}, "
        f"attachments={len(new_attachments)}, details={detail_requests}, "
        f"full_scan={full_scan}, +{last_change['added_documents']} "
        f"~{last_change['changed_documents']} -{last_change['removed_documents']}, "
        f"pending_missing={len(missing_counts)}, errors={len(errors)}, "
        f"commit={should_commit}"
    )


if __name__ == "__main__":
    main()
