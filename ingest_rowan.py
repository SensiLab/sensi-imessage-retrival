#!/usr/bin/env python3
"""
ingest_rowan.py — Ingest all images from the Rowan/ test folder into the
test Sensi Memory database (http://localhost:8002).

Run: python ingest_rowan.py
"""

import datetime
import hashlib
import json
import logging
import os

try:
    import requests
except ImportError:
    raise SystemExit("ERROR: 'requests' is required. Run: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    raise SystemExit("ERROR: 'python-dotenv' is required. Run: pip install python-dotenv")

from utils import setup_logging, convert_to_jpeg_if_needed, generate_tags_and_description, NATIVE_MIMES

ROWAN_DIR       = os.path.join(os.path.dirname(__file__), "Rowan")
API_BASE_URL    = "http://localhost:8002"
INGEST_ENDPOINT = f"{API_BASE_URL}/ingest/image"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".gif", ".webp", ".tiff", ".bmp", ".avif"}

MIME_BY_EXT = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".bmp":  "image/bmp",
    ".avif": "image/avif",
}


def ingest_image(filepath: str) -> str:
    filename  = os.path.basename(filepath)
    ext       = os.path.splitext(filename)[1].lower()
    mime_type = MIME_BY_EXT.get(ext, "image/jpeg")

    upload_path, is_temp = None, False
    try:
        try:
            upload_path, is_temp = convert_to_jpeg_if_needed(filepath, mime_type)
        except RuntimeError as exc:
            logging.error("Conversion failed for %s: %s", filename, exc)
            return "error"

        mtime     = os.path.getmtime(filepath)
        timestamp = datetime.datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
        tags, description = generate_tags_and_description(upload_path, sender="rowan", timestamp=timestamp)

        document_id = hashlib.sha256(f"rowan:{filename}".encode()).hexdigest()

        metadata = json.dumps({
            "source":            "rowan",
            "sender":            "rowan",
            "filename":          filename,
            "timestamp":         timestamp,
            "filepath":          filepath,
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
        logging.error("HTTP error for %s: %s", filename, exc)
        return "error"
    finally:
        if is_temp and upload_path and os.path.exists(upload_path):
            os.unlink(upload_path)

    logging.info("[OK] %s → tags=%s", filename, tags)
    return "ingested"


def main() -> None:
    setup_logging(log_prefix="rowan_ingest")
    logging.info("[ingest_rowan] Scanning: %s", ROWAN_DIR)

    image_files = sorted(
        os.path.join(ROWAN_DIR, f)
        for f in os.listdir(ROWAN_DIR)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
    )
    logging.info("Found %d image(s) to ingest", len(image_files))

    counts: dict[str, int] = {"ingested": 0, "error": 0}
    for filepath in image_files:
        result = ingest_image(filepath)
        counts[result] = counts.get(result, 0) + 1

    logging.info(
        "Summary — Ingested: %d | Errors: %d",
        counts["ingested"],
        counts["error"],
    )


if __name__ == "__main__":
    main()
