#!/usr/bin/env python3
"""
ingest_imessages.py — Ingest today's iMessage image attachments into the
Sensi Memory vector database (http://localhost:8000).

Run: python ingest_imessages.py
"""

import datetime
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import tempfile

try:
    import requests
except ImportError:
    raise SystemExit("ERROR: 'requests' is required. Run: pip install requests")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL      = "http://localhost:8000"
INGEST_ENDPOINT   = f"{API_BASE_URL}/ingest/image"
CHAT_DB_PATH      = os.path.expanduser("~/Library/Messages/chat.db")
STATE_FILE        = os.path.join(os.path.dirname(__file__), ".ingested_ids.json")
LOG_DIR           = os.path.join(os.path.dirname(__file__), "logs")
MAC_EPOCH_OFFSET  = 978307200  # seconds between Unix epoch (1970-01-01) and Mac epoch (2001-01-01)

# Formats the API accepts natively (no conversion needed)
NATIVE_MIMES = {"image/jpeg", "image/png"}

# Formats that can be converted to JPEG via macOS sips before upload
CONVERTIBLE_MIMES = {
    "image/heic", "image/heif",
    "image/gif", "image/webp",
    "image/tiff", "image/bmp", "image/x-bmp",
    "image/avif",
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"imessage_ingest_{datetime.date.today()}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------

def get_today_mac_time_range() -> tuple[float, float]:
    """Return (start_mac_sec, end_mac_sec) spanning today in the local timezone."""
    today = datetime.date.today()
    local_tz = datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo
    start_dt = datetime.datetime(today.year, today.month, today.day, tzinfo=local_tz)
    end_dt   = start_dt + datetime.timedelta(days=1)
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


def query_today_images() -> list[dict]:
    start_mac, end_mac = get_today_mac_time_range()
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


def build_contact_cache() -> dict[str, str]:
    """
    Return a mapping of normalised phone digits / lowercase email → display name
    by querying macOS Contacts via AppleScript. Returns an empty dict if access
    is denied or Contacts is unavailable (sender will fall back to raw handle).
    macOS may prompt for Contacts permission on the first run.
    """
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
        name   = name.strip()
        handle = handle.strip()
        if not name or not handle:
            continue
        if "@" in handle:
            cache[handle.lower()] = name
        else:
            digits = "".join(c for c in handle if c.isdigit())
            if digits:
                cache[digits] = name
                if len(digits) > 10:
                    cache[digits[-10:]] = name  # last-10 fallback for E.164 numbers

    logging.info("Loaded %d contact entries from macOS Contacts", len(cache))
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
# Image format conversion (macOS sips)
# ---------------------------------------------------------------------------

def convert_to_jpeg_if_needed(src_path: str, mime_type: str) -> tuple[str, bool]:
    """
    Return (path_to_upload, is_temp_file).
    If is_temp_file is True the caller must delete the file after use.
    Uses macOS built-in `sips` to convert non-JPEG/PNG formats to JPEG.
    """
    if mime_type in NATIVE_MIMES:
        return src_path, False

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    result = subprocess.run(
        ["sips", "-s", "format", "jpeg", src_path, "--out", tmp.name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        os.unlink(tmp.name)
        raise RuntimeError(f"sips conversion failed: {result.stderr.strip()}")
    return tmp.name, True

# ---------------------------------------------------------------------------
# LLM hook point
# ---------------------------------------------------------------------------

def generate_tags_and_description(
    image_path: str,
    sender: str,
    timestamp: str,
) -> tuple[str, str]:
    """
    ########################################################################
    # LLM HOOK POINT
    #
    # Replace this function body with a vision model call to generate real
    # tags and a natural-language description for the image before ingestion.
    #
    # Suggested integrations:
    #   Anthropic Claude:  anthropic.Anthropic().messages.create(
    #                          model="claude-opus-4-7", ...)
    #   Google Gemini:     google.generativeai.GenerativeModel(...).generate_content(...)
    #   OpenAI GPT-4o:     openai.OpenAI().chat.completions.create(
    #                          model="gpt-4o", ...)
    #
    # Parameters available to your LLM call:
    #   image_path  — absolute path to the (possibly converted) image file
    #   sender      — "me" or the sender's phone/email string
    #   timestamp   — ISO 8601 UTC string of when the message was sent
    #
    # Return: (tags_csv_string, description_string)
    #   tags        — comma-separated labels, e.g. "food,restaurant,lunch"
    #   description — free-text caption passed to /ingest/image as `text`,
    #                 influencing the multimodal embedding
    ########################################################################
    """
    # PLACEHOLDER — replace with real LLM call above
    tags = "imessage,untagged"
    description = ""
    return tags, description

# ---------------------------------------------------------------------------
# Ingest a single row
# ---------------------------------------------------------------------------

def mac_raw_to_iso(raw_date: int) -> str:
    mac_secs = raw_date / 1e9 if raw_date > 1e12 else float(raw_date)
    unix_ts  = mac_secs + MAC_EPOCH_OFFSET
    return datetime.datetime.utcfromtimestamp(unix_ts).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # Convert to JPEG if needed
    upload_path, is_temp = None, False
    try:
        try:
            upload_path, is_temp = convert_to_jpeg_if_needed(image_path, mime_type)
        except RuntimeError as exc:
            logging.error("Conversion failed for message_id=%s: %s", message_id, exc)
            return "error"

        sender    = "me" if is_from_me == 1 else resolve_sender(sender_handle, contact_cache)
        timestamp = mac_raw_to_iso(raw_date)
        tags, description = generate_tags_and_description(upload_path, sender, timestamp)

        # Stable document_id derived from the message rowid — prevents duplicate
        # records if the script is restarted and the state file is lost
        document_id = hashlib.sha256(f"imessage:{message_id}".encode()).hexdigest()

        metadata = json.dumps({
            "source":            "imessage",
            "sender":            sender,
            "message_id":        message_id,
            "timestamp":         timestamp,
            "filepath":          image_path,   # original path, before any conversion
            "original_mime_type": mime_type,
        })

        upload_mime = "image/jpeg" if is_temp else mime_type
        upload_name = os.path.basename(upload_path)

        form_data: dict = {
            "tags":        tags,
            "metadata":    metadata,
            "document_id": document_id,
        }
        if description:
            form_data["text"] = description

        with open(upload_path, "rb") as img_file:
            resp = requests.post(
                INGEST_ENDPOINT,
                files={"file": (upload_name, img_file, upload_mime)},
                data=form_data,
                timeout=30,
            )
        resp.raise_for_status()

    except requests.RequestException as exc:
        logging.error("HTTP error for message_id=%s: %s", message_id, exc)
        return "error"
    finally:
        if is_temp and upload_path and os.path.exists(upload_path):
            os.unlink(upload_path)

    ingested_ids.add(message_id)
    save_state(ingested_ids)
    return "ingested"

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    logging.info("[ingest_imessages] Running for date: %s", datetime.date.today())

    ingested_ids  = load_state()
    logging.info("Already ingested today: %d message(s)", len(ingested_ids))

    contact_cache = build_contact_cache()

    rows = query_today_images()
    logging.info("Found %d image attachment(s) in today's messages", len(rows))

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
