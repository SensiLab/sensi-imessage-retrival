#!/usr/bin/env python3
"""
ingest_imessages.py — Ingest today's iMessage image attachments into the
Sensi Memory vector database (http://localhost:8000).

Run: python ingest_imessages.py
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess

try:
    import requests
except ImportError:
    raise SystemExit("ERROR: 'requests' is required. Run: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    raise SystemExit("ERROR: 'python-dotenv' is required. Run: pip install python-dotenv")

try:
    import Contacts
except ImportError:
    Contacts = None

from utils import (
    setup_logging,
    generate_tags_and_description,
    NATIVE_MIMES,
    CONVERTIBLE_MIMES,
)

from pipeline import (
    classify_image,
    screenshot_pipeline,
    presentation_pipeline,
    poster_pipeline,
    book_pipeline,
    other_pipeline,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL      = "http://localhost:8000"
INGEST_ENDPOINT   = f"{API_BASE_URL}/ingest/image"
TEXT_INGEST_ENDPOINT = f"{API_BASE_URL}/ingest/text"
CHAT_DB_PATH      = os.path.expanduser("~/Library/Messages/chat.db")
STATE_FILE        = os.path.join(os.path.dirname(__file__), ".ingested_ids.json")
_env_data_dir     = os.environ.get("IMAGE_SAVE_DIR")
if _env_data_dir:
    if not os.path.exists(_env_data_dir):
        _parent = os.path.dirname(_env_data_dir)
        if not os.path.isdir(_parent):
            raise SystemExit(
                f"ERROR: IMAGE_SAVE_DIR '{_env_data_dir}' and its parent '{_parent}' do not exist."
            )
        os.makedirs(_env_data_dir)
    DATA_DIR = _env_data_dir
else:
    DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MAC_EPOCH_OFFSET  = 978307200  # seconds between Unix epoch (1970-01-01) and Mac epoch (2001-01-01)

# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------

def get_mac_time_range(days_back: int = 1) -> tuple[float, float]:
    """Return (start_mac_sec, end_mac_sec) spanning the last `days_back` days."""
    today = datetime.date.today()
    local_tz = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo
    end_dt   = datetime.datetime(today.year, today.month, today.day, tzinfo=local_tz) + datetime.timedelta(days=1)
    start_dt = end_dt - datetime.timedelta(days=days_back)
    start_mac = start_dt.timestamp() - MAC_EPOCH_OFFSET
    end_mac   = end_dt.timestamp()   - MAC_EPOCH_OFFSET
    return start_mac, end_mac

# ---------------------------------------------------------------------------
# Deduplication state
# ---------------------------------------------------------------------------

def load_state() -> set[int]:
    """Load today's already-ingested message IDs. Resets automatically on a new day."""
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()
    if data.get("last_run_date") != str(datetime.date.today()):
        return set()
    return set(data.get("ingested_message_ids", []))


def save_state(ids: set[int]) -> None:
    data = {
        "ingested_message_ids": sorted(ids),
        "last_run_date": str(datetime.date.today()),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ---------------------------------------------------------------------------
# SQLite query
# ---------------------------------------------------------------------------

SQL_QUERY = """
SELECT
    m.rowid            AS message_id,
    m.date             AS raw_date,
    m.is_from_me,
    h.id               AS sender_handle,
    a.rowid            AS attachment_id,
    a.filename         AS attachment_filename,
    a.mime_type
FROM message AS m
LEFT JOIN handle AS h ON m.handle_id = h.rowid
JOIN message_attachment_join AS maj ON maj.message_id = m.rowid
JOIN attachment AS a ON a.rowid = maj.attachment_id
WHERE a.mime_type LIKE 'image/%'
  AND (CASE WHEN m.date > 1000000000000
            THEN m.date / 1000000000.0
            ELSE CAST(m.date AS REAL) END) >= :start_mac
  AND (CASE WHEN m.date > 1000000000000
            THEN m.date / 1000000000.0
            ELSE CAST(m.date AS REAL) END) < :end_mac
ORDER BY m.date ASC
"""


def query_images(days_back: int = 1) -> list[dict]:
    start_mac, end_mac = get_mac_time_range(days_back)
    uri = f"file:{CHAT_DB_PATH}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        logging.error("Cannot open chat.db: %s", exc)
        raise SystemExit(1)

    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(SQL_QUERY, {"start_mac": start_mac, "end_mac": end_mac})
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# macOS Contacts lookup
# ---------------------------------------------------------------------------

_CONTACTS_APPLESCRIPT = """\
tell application "Contacts"
    launch
    activate
    set output to ""
    repeat with p in every person
        set pname to name of p
        try
            repeat with ph in phones of p
                set output to output & pname & "|" & (value of ph) & linefeed
            end repeat
        end try
        try
            repeat with em in emails of p
                set output to output & pname & "|" & (value of em) & linefeed
            end repeat
        end try
    end repeat
    return output
end tell
"""


def _cache_contact_handle(cache: dict[str, str], name: str, handle: str) -> None:
    name = name.strip()
    handle = handle.strip()
    if not name or not handle:
        return

    if "@" in handle:
        cache[handle.lower()] = name
        return

    digits = "".join(c for c in handle if c.isdigit())
    if not digits:
        return

    cache[digits] = name
    if len(digits) > 10:
        cache[digits[-10:]] = name


def build_contact_cache_native() -> dict[str, str] | None:
    if Contacts is None:
        return None

    status = Contacts.CNContactStore.authorizationStatusForEntityType_(
        Contacts.CNEntityTypeContacts
    )
    if status == Contacts.CNAuthorizationStatusDenied:
        logging.warning("Contacts.framework access is denied for this Python environment")
        return {}
    if status == Contacts.CNAuthorizationStatusRestricted:
        logging.warning("Contacts.framework access is restricted for this Python environment")
        return {}

    cache: dict[str, str] = {}
    keys = [
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactOrganizationNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
    ]
    fetch_request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    store = Contacts.CNContactStore.alloc().init()

    def collect_contact(contact, _stop) -> None:
        name_parts = [str(contact.givenName() or "").strip(), str(contact.familyName() or "").strip()]
        display_name = " ".join(part for part in name_parts if part)
        if not display_name:
            display_name = str(contact.organizationName() or "").strip()
        if not display_name:
            return

        for labeled_phone in contact.phoneNumbers() or []:
            phone_value = labeled_phone.value()
            phone_number = str(phone_value.stringValue() if phone_value else "")
            _cache_contact_handle(cache, display_name, phone_number)

        for labeled_email in contact.emailAddresses() or []:
            email_value = str(labeled_email.value() or "")
            _cache_contact_handle(cache, display_name, email_value)

    try:
        ok, error = store.enumerateContactsWithFetchRequest_error_usingBlock_(
            fetch_request, None, collect_contact
        )
    except Exception as exc:
        logging.warning("Contacts.framework lookup failed: %s", exc)
        return {}

    if not ok:
        logging.warning("Contacts.framework fetch failed: %s", error)
        return {}

    logging.info("Loaded %d contact entries from Contacts.framework", len(cache))
    return cache


def build_contact_cache() -> dict[str, str]:
    """
    Return a mapping of normalised phone digits / lowercase email → display name
    by querying macOS Contacts via AppleScript. Returns an empty dict if access
    is denied or Contacts is unavailable (sender will fall back to raw handle).
    macOS may prompt for Contacts permission on the first run.
    """
    logging.info("Loading sender display names from macOS Contacts")

    native_cache = build_contact_cache_native()
    if native_cache is not None:
        if not native_cache:
            logging.warning("Native Contacts lookup returned no entries; falling back to AppleScript")
        else:
            return native_cache

    try:
        result = subprocess.run(
            ["osascript", "-e", _CONTACTS_APPLESCRIPT],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        logging.warning("osascript failed: %s", exc)
        return {}

    if result.returncode != 0:
        logging.warning(
            "Could not load Contacts (check Contacts permission in System Settings): %s",
            result.stderr.strip(),
        )
        return {}

    cache: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        name, handle = line.split("|", 1)
        _cache_contact_handle(cache, name, handle)

    logging.info("Loaded %d contact entries from macOS Contacts", len(cache))
    if not cache:
        logging.warning("Contacts lookup returned no entries; sender names will fall back to raw handles")
    return cache


def resolve_sender(handle: str, contact_cache: dict[str, str]) -> str:
    """Resolve a raw iMessage handle (phone number or email) to a contact name."""
    if not handle:
        return "unknown"
    if "@" in handle:
        return contact_cache.get(handle.lower(), handle)
    digits = "".join(c for c in handle if c.isdigit())
    return (
        contact_cache.get(digits)
        or contact_cache.get(digits[-10:] if len(digits) > 10 else digits)
        or handle
    )


# ---------------------------------------------------------------------------
# Ingest a single row
# ---------------------------------------------------------------------------

def mac_raw_to_iso(raw_date: int) -> str:
    mac_secs = raw_date / 1e9 if raw_date > 1e12 else float(raw_date)
    unix_ts  = mac_secs + MAC_EPOCH_OFFSET
    return datetime.datetime.utcfromtimestamp(unix_ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def copy_image_to_local_store(src_path: str, attachment_id: int, date_str: str, mime_type: str) -> str:
    """Store src_path as JPEG in data/<date_str>/ and return the destination path."""
    dest_dir = os.path.join(DATA_DIR, date_str)
    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    dest_path = os.path.join(dest_dir, f"{attachment_id}_{stem}.jpg")
    if not os.path.exists(dest_path):
        if mime_type == "image/jpeg":
            shutil.copy2(src_path, dest_path)
        else:
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", src_path, "--out", dest_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"sips conversion failed: {result.stderr.strip()}")
    return dest_path


def save_figure_to_disk(fig_bytes: bytes, attachment_id: int, date_str: str, index: int = 0) -> str:
    """Save extracted figure bytes to DATA_DIR/<date_str>/<attachment_id>_fig_<index>.jpg."""
    dest_dir = os.path.join(DATA_DIR, date_str)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{attachment_id}_fig_{index}.jpg")
    with open(dest_path, "wb") as f:
        f.write(fig_bytes)
    return dest_path


def ingest_ocr_text(
    text: str,
    sender: str,
    tags: str,
    document_id_seed: str,
    source_path: str,
    timestamp: str,
) -> None:
    """POST OCR-extracted text to the /ingest/text endpoint."""
    document_id = hashlib.sha256(f"{document_id_seed}:ocr".encode()).hexdigest()
    payload = {
        "text": text,
        "sender": sender,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "document_id": document_id,
        "metadata": {
            "source_path": source_path,
            "created_at": timestamp,
        },
    }
    resp = requests.post(TEXT_INGEST_ENDPOINT, json=payload, timeout=30)
    resp.raise_for_status()


def _post_image(
    file_path: str,
    source_path: str,
    sender: str,
    tags: str,
    description: str,
    document_id: str,
    metadata: dict,
    object_path: str | None = None,
) -> None:
    """POST a JPEG image to the /ingest/image endpoint."""
    form_data: dict = {
        "sender":      sender,
        "source_path": source_path,
        "tags":        tags,
        "metadata":    json.dumps(metadata),
        "document_id": document_id,
    }
    if description:
        form_data["text"] = description
    if object_path:
        form_data["object_path"] = object_path
    with open(file_path, "rb") as img_file:
        resp = requests.post(
            INGEST_ENDPOINT,
            files={"file": (os.path.basename(file_path), img_file, "image/jpeg")},
            data=form_data,
            timeout=30,
        )
    resp.raise_for_status()


def ingest_row(row: dict, ingested_ids: set[int], contact_cache: dict[str, str]) -> str:
    """
    Process one database row.
    Returns one of: ingested | skipped_duplicate | skipped_missing |
                    skipped_mime | error
    """
    message_id    = row["message_id"]
    mime_type     = row["mime_type"] or ""
    raw_path      = row["attachment_filename"]
    is_from_me    = row["is_from_me"]
    sender_handle = row["sender_handle"]
    raw_date      = row["raw_date"]

    if message_id in ingested_ids:
        return "skipped_duplicate"

    # Resolve and verify the attachment path
    image_path = os.path.expanduser(raw_path) if raw_path else None
    if not image_path or not os.path.isfile(image_path):
        logging.warning("Attachment not found on disk (message_id=%s): %s", message_id, raw_path)
        return "skipped_missing"

    # MIME guard — skip entirely unknown image types
    if mime_type not in NATIVE_MIMES and mime_type not in CONVERTIBLE_MIMES:
        logging.warning("Unsupported mime type '%s' for message_id=%s — skipping", mime_type, message_id)
        return "skipped_mime"

    timestamp = mac_raw_to_iso(raw_date)
    date_str  = timestamp[:10]

    try:
        local_path = copy_image_to_local_store(image_path, row["attachment_id"], date_str, mime_type)
    except (OSError, RuntimeError) as exc:
        logging.error("Failed to store attachment for message_id=%s: %s", message_id, exc)
        return "error"

    sender = "me" if is_from_me == 1 else resolve_sender(sender_handle, contact_cache)
    id_seed = f"imessage:{message_id}"
    base_metadata = {"filename": os.path.basename(local_path)}

    # Classify the image and run the appropriate pipeline
    pipeline_ok = False
    try:
        logging.info("Classifying message_id=%s", message_id)
        category = classify_image(local_path)
        logging.info("[pipeline] message_id=%s category=%s", message_id, category)

        if category in ("presentation", "poster", "other"):
            if category == "presentation":
                _img_bytes, ocr_text = presentation_pipeline(local_path)
            elif category == "poster":
                _img_bytes, ocr_text = poster_pipeline(local_path)
            else:
                _img_bytes, ocr_text = other_pipeline(local_path)
            tags, description = generate_tags_and_description(local_path, sender, timestamp)
            document_id = hashlib.sha256(id_seed.encode()).hexdigest()
            _post_image(local_path, local_path, sender, tags, description, document_id, base_metadata)
            if ocr_text:
                ingest_ocr_text(ocr_text, sender, tags, id_seed, local_path, timestamp)
                logging.info("[pipeline] message_id=%s ingested OCR text (%d chars)", message_id, len(ocr_text))

        elif category == "screenshot":
            img_bytes, ocr_text, is_original = screenshot_pipeline(local_path)
            if is_original:
                tags, description = generate_tags_and_description(local_path, sender, timestamp)
                document_id = hashlib.sha256(id_seed.encode()).hexdigest()
                _post_image(local_path, local_path, sender, tags, description, document_id, base_metadata)
            else:
                fig_path = save_figure_to_disk(img_bytes, row["attachment_id"], date_str, index=0)
                logging.info("[pipeline] message_id=%s saved figure 0 → %s", message_id, fig_path)
                tags, description = generate_tags_and_description(fig_path, sender, timestamp)
                document_id = hashlib.sha256(f"{id_seed}:fig:0".encode()).hexdigest()
                _post_image(fig_path, local_path, sender, tags, description, document_id,
                            {"filename": os.path.basename(fig_path)}, object_path=fig_path)
            if ocr_text:
                ingest_ocr_text(ocr_text, sender, tags, id_seed, local_path, timestamp)
                logging.info("[pipeline] message_id=%s ingested OCR text (%d chars)", message_id, len(ocr_text))

        elif category == "book":
            figures_list, ocr_text, is_original = book_pipeline(local_path)
            if is_original:
                tags, description = generate_tags_and_description(local_path, sender, timestamp)
                document_id = hashlib.sha256(id_seed.encode()).hexdigest()
                _post_image(local_path, local_path, sender, tags, description, document_id, base_metadata)
            else:
                tags, description = "", ""
                for i, fig_bytes in enumerate(figures_list):
                    fig_path = save_figure_to_disk(fig_bytes, row["attachment_id"], date_str, index=i)
                    logging.info("[pipeline] message_id=%s saved figure %d → %s", message_id, i, fig_path)
                    tags, description = generate_tags_and_description(fig_path, sender, timestamp)
                    document_id = hashlib.sha256(f"{id_seed}:fig:{i}".encode()).hexdigest()
                    _post_image(fig_path, local_path, sender, tags, description, document_id,
                                {"filename": os.path.basename(fig_path)}, object_path=fig_path)
            if ocr_text:
                ingest_ocr_text(ocr_text, sender, tags, id_seed, local_path, timestamp)
                logging.info("[pipeline] message_id=%s ingested OCR text (%d chars)", message_id, len(ocr_text))

        pipeline_ok = True

    except requests.RequestException as exc:
        logging.error("HTTP error for message_id=%s: %s", message_id, exc)
        return "error"
    except Exception as exc:
        logging.warning("[pipeline] message_id=%s pipeline failed, falling back to default: %s", message_id, exc)

    # Fallback: ingest original image without pipeline processing
    if not pipeline_ok:
        try:
            tags, description = generate_tags_and_description(local_path, sender, timestamp)
            document_id = hashlib.sha256(id_seed.encode()).hexdigest()
            _post_image(local_path, local_path, sender, tags, description, document_id, base_metadata)
        except requests.RequestException as exc:
            logging.error("HTTP error for message_id=%s: %s", message_id, exc)
            return "error"

    ingested_ids.add(message_id)
    save_state(ingested_ids)
    return "ingested"

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest iMessage image attachments into Sensi Memory.")
    parser.add_argument("--days", type=int, default=1, metavar="N",
                        help="How many days to look back (default: 1 = today only)")
    args = parser.parse_args()
    days_back = max(1, args.days)

    setup_logging(log_prefix="imessage_ingest")
    logging.info("[ingest_imessages] Looking back %d day(s) ending %s", days_back, datetime.date.today())

    ingested_ids  = load_state()
    logging.info("Already ingested today: %d message(s)", len(ingested_ids))

    contact_cache = build_contact_cache()

    rows = query_images(days_back)
    logging.info("Found %d image attachment(s) in the last %d day(s)", len(rows), days_back)

    counts: dict[str, int] = {
        "ingested": 0,
        "skipped_duplicate": 0,
        "skipped_missing": 0,
        "skipped_mime": 0,
        "error": 0,
    }

    for row in rows:
        result = ingest_row(row, ingested_ids, contact_cache)
        counts[result] += 1
        if result == "ingested":
            if row["is_from_me"]:
                display = "me"
            else:
                display = resolve_sender(row.get("sender_handle") or "", contact_cache)
            logging.info("[OK] message_id=%s sender=%s", row["message_id"], display)

    logging.info(
        "Summary — Ingested: %d | Skipped (dup): %d | Missing: %d | Bad mime: %d | Errors: %d",
        counts["ingested"],
        counts["skipped_duplicate"],
        counts["skipped_missing"],
        counts["skipped_mime"],
        counts["error"],
    )


if __name__ == "__main__":
    main()
