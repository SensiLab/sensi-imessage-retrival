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

from pipeline import (
    classify_image,
    screenshot_pipeline,
    presentation_pipeline,
    poster_pipeline,
    book_pipeline,
    other_pipeline,
)

ROWAN_DIR            = os.path.join(os.path.dirname(__file__), "Rowan")
API_BASE_URL         = "http://localhost:8002"
INGEST_ENDPOINT      = f"{API_BASE_URL}/ingest/image"
TEXT_INGEST_ENDPOINT = f"{API_BASE_URL}/ingest/text"

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


def _save_figure(fig_bytes: bytes, source_filepath: str, index: int) -> str:
    """Save extracted figure bytes alongside the source file."""
    base = os.path.splitext(source_filepath)[0]
    fig_path = f"{base}_fig_{index}.jpg"
    with open(fig_path, "wb") as f:
        f.write(fig_bytes)
    return fig_path


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


def _ingest_ocr_text(
    text: str,
    sender: str,
    tags: str,
    document_id_seed: str,
    source_path: str,
    timestamp: str,
) -> None:
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
        id_seed   = f"rowan:{filename}"
        base_meta = {"filename": filename}

        # Classify and dispatch through the appropriate pipeline
        pipeline_ok = False
        try:
            logging.info("Classifying %s", filename)
            category = classify_image(upload_path)
            logging.info("[pipeline] %s category=%s", filename, category)

            if category in ("presentation", "poster", "other"):
                if category == "presentation":
                    _img_bytes, ocr_text = presentation_pipeline(upload_path)
                elif category == "poster":
                    _img_bytes, ocr_text = poster_pipeline(upload_path)
                else:
                    _img_bytes, ocr_text = other_pipeline(upload_path)
                tags, description = generate_tags_and_description(upload_path, sender="rowan", timestamp=timestamp)
                document_id = hashlib.sha256(id_seed.encode()).hexdigest()
                _post_image(upload_path, filepath, "rowan", tags, description, document_id, base_meta)
                if ocr_text:
                    _ingest_ocr_text(ocr_text, "rowan", tags, id_seed, filepath, timestamp)
                    logging.info("[pipeline] %s ingested OCR text (%d chars)", filename, len(ocr_text))

            elif category == "screenshot":
                img_bytes, ocr_text, is_original = screenshot_pipeline(upload_path)
                if is_original:
                    tags, description = generate_tags_and_description(upload_path, sender="rowan", timestamp=timestamp)
                    document_id = hashlib.sha256(id_seed.encode()).hexdigest()
                    _post_image(upload_path, filepath, "rowan", tags, description, document_id, base_meta)
                else:
                    fig_path = _save_figure(img_bytes, filepath, index=0)
                    logging.info("[pipeline] %s saved figure 0 → %s", filename, fig_path)
                    tags, description = generate_tags_and_description(fig_path, sender="rowan", timestamp=timestamp)
                    document_id = hashlib.sha256(f"{id_seed}:fig:0".encode()).hexdigest()
                    _post_image(fig_path, filepath, "rowan", tags, description, document_id,
                                {"filename": os.path.basename(fig_path)}, object_path=fig_path)
                if ocr_text:
                    _ingest_ocr_text(ocr_text, "rowan", tags, id_seed, filepath, timestamp)
                    logging.info("[pipeline] %s ingested OCR text (%d chars)", filename, len(ocr_text))

            elif category == "book":
                figures_list, ocr_text, is_original = book_pipeline(upload_path)
                if is_original:
                    tags, description = generate_tags_and_description(upload_path, sender="rowan", timestamp=timestamp)
                    document_id = hashlib.sha256(id_seed.encode()).hexdigest()
                    _post_image(upload_path, filepath, "rowan", tags, description, document_id, base_meta)
                else:
                    tags, description = "", ""
                    for i, fig_bytes in enumerate(figures_list):
                        fig_path = _save_figure(fig_bytes, filepath, index=i)
                        logging.info("[pipeline] %s saved figure %d → %s", filename, i, fig_path)
                        tags, description = generate_tags_and_description(fig_path, sender="rowan", timestamp=timestamp)
                        document_id = hashlib.sha256(f"{id_seed}:fig:{i}".encode()).hexdigest()
                        _post_image(fig_path, filepath, "rowan", tags, description, document_id,
                                    {"filename": os.path.basename(fig_path)}, object_path=fig_path)
                if ocr_text:
                    _ingest_ocr_text(ocr_text, "rowan", tags, id_seed, filepath, timestamp)
                    logging.info("[pipeline] %s ingested OCR text (%d chars)", filename, len(ocr_text))

            pipeline_ok = True

        except requests.RequestException as exc:
            logging.error("HTTP error for %s: %s", filename, exc)
            return "error"
        except Exception as exc:
            logging.warning("[pipeline] %s pipeline failed, falling back to default: %s", filename, exc)

        # Fallback: ingest original image without pipeline processing
        if not pipeline_ok:
            try:
                tags, description = generate_tags_and_description(upload_path, sender="rowan", timestamp=timestamp)
                document_id = hashlib.sha256(id_seed.encode()).hexdigest()
                _post_image(upload_path, filepath, "rowan", tags, description, document_id, base_meta)
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
