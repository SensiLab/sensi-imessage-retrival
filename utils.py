import base64
import datetime
import logging
import os
import subprocess
import tempfile

try:
    import openai
except ImportError:
    raise SystemExit("ERROR: 'openai' is required. Run: pip install openai")

NATIVE_MIMES = {"image/jpeg", "image/png"}

CONVERTIBLE_MIMES = {
    "image/heic", "image/heif",
    "image/gif", "image/webp",
    "image/tiff", "image/bmp", "image/x-bmp",
    "image/avif",
}

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def setup_logging(log_prefix: str = "ingest") -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    log_file = os.path.join(_LOG_DIR, f"{log_prefix}_{datetime.date.today()}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def convert_to_jpeg_if_needed(src_path: str, mime_type: str) -> tuple[str, bool]:
    """Return (path_to_upload, is_temp_file). Caller must delete temp file if is_temp_file is True."""
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


def generate_tags_and_description(
    image_path: str,
    sender: str,
    timestamp: str,
) -> tuple[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logging.warning("OPENAI_API_KEY not set — skipping vision analysis")
        return "untagged", ""

    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

    prompt = (
        "Analyze this image and document its contents. "
        "1) Write a concise natural-language description (1–3 sentences). "
        "2) List 2-4 short comma-separated tags that categorize the content. "
        "Respond in exactly this format:\n"
        "DESCRIPTION: <description>\n"
        "TAGS: <tag1>,<tag2>,..."
    )

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}", "detail": "auto"},
                    },
                ],
            }
        ],
        max_tokens=300,
    )

    text = response.choices[0].message.content or ""
    description, tags = "", "untagged"
    for line in text.splitlines():
        if line.startswith("DESCRIPTION:"):
            description = line.split(":", 1)[1].strip()
        elif line.startswith("TAGS:"):
            tags = line.split(":", 1)[1].strip()

    return tags, description
