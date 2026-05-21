# sensi-imessage-retrival

A macOS script that pulls today's iMessage image attachments, generates natural-language descriptions and tags using GPT-4o vision, and ingests them into the [Sensi Memory](https://github.com/anthropics) vector database so they become searchable by semantic meaning.

## How it works

1. Opens `~/Library/Messages/chat.db` in read-only mode and queries all image attachments sent or received in the configured look-back window (default: today only).
2. Converts every image to JPEG using macOS `sips` and saves it permanently in `data/<date>/`. JPEG sources are copied directly; all other formats (HEIC, GIF, WebP, TIFF, etc.) are converted in-place.
3. Calls the OpenAI GPT-4o vision API to generate a natural-language description and comma-separated tags for each image.
4. POSTs each JPEG to the Sensi Memory `/ingest/image` endpoint, attaching the description, tags, sender name, timestamp, and the local JPEG path as metadata.
5. Tracks ingested message IDs in `.ingested_ids.json` to skip duplicates on re-runs. The state file resets automatically each day.

Sender phone numbers and emails are resolved to display names via macOS Contacts (AppleScript). macOS will prompt for Contacts permission on the first run.

## Requirements

- macOS (uses `sips` for image conversion and `osascript` for Contacts lookup)
- Python 3.11+
- Sensi Memory server running on `http://localhost:8000` (see [API.md](API.md))
- An OpenAI API key with access to `gpt-4o`
- Full Disk Access for the Python runtime that will execute the script (interactive terminal runs and `launchd` background runs are treated differently by macOS)

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd sensi-imessage-retrival
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install requests python-dotenv openai
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your OpenAI API key:

```
OPENAI_API_KEY=sk-...
```

If you want to disable OpenAI vision analysis, remove the `OPENAI_API_KEY` line from `.env` or leave it blank:

```
OPENAI_API_KEY=
```

In that mode the script still ingests images, but it skips the vision model call and falls back to basic tags with no generated description.

### 4. Grant Full Disk Access

`chat.db` is protected by macOS. For manual runs, grant **Full Disk Access** to the app you launch Python from, such as Terminal, iTerm2, or VS Code, in:

> System Settings → Privacy & Security → Full Disk Access

For scheduled `launchd` runs, you may also need to grant Full Disk Access to the actual Python runtime used by the job. In this workspace the LaunchAgent uses `.venv/bin/python`, which resolves to a Homebrew Python framework install. If the scheduled job logs `Cannot open chat.db: unable to open database file` while manual runs still work, add the corresponding `Python.app` from the Homebrew framework to Full Disk Access as well.

### 5. Start the Sensi Memory server

The Sensi Memory server must be running on `http://localhost:8000` before executing the script. See [API.md](API.md) for setup instructions.

## Usage

```bash
python ingest_imessages.py
```

Pass `--days N` to look back further than today:

```bash
# Backfill the last 7 days
python ingest_imessages.py --days 7
```

The default is `--days 1` (today only). The look-back window always ends at the end of the current day.

The script logs progress to stdout and to a dated file in `logs/`. A typical run looks like:

```
2026-05-11 09:00:01 INFO [ingest_imessages] Looking back 1 day(s) ending 2026-05-11
2026-05-11 09:00:01 INFO Already ingested today: 0 message(s)
2026-05-11 09:00:02 INFO Loaded 312 contact entries from macOS Contacts
2026-05-11 09:00:02 INFO Found 4 image attachment(s) in the last 1 day(s)
2026-05-11 09:00:05 INFO [OK] message_id=12345 sender=Alice
2026-05-11 09:00:07 INFO [OK] message_id=12346 sender=me
2026-05-11 09:00:07 INFO Summary — Ingested: 2 | Skipped (dup): 0 | Missing: 1 | Bad mime: 0 | Errors: 0
```

### Automating with launchd

A ready-to-use daily launch agent is included at `launchd/com.sensi.imessage-ingest.plist`. It is currently configured for this workspace to run every day at 15:15 using `.venv/bin/python`, with the repo root as the working directory so `.env` loads correctly.

Install it with:

```bash
mkdir -p ~/Library/LaunchAgents
cp /Users/rtdbot/sensi-imessage-retrival/launchd/com.sensi.imessage-ingest.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist
launchctl list | grep com.sensi.imessage-ingest
```

If you want a different run time, edit the `Hour` and `Minute` values in the plist before loading it.

If you later update `launchd/com.sensi.imessage-ingest.plist`, those changes do not take effect automatically. `launchd` is using the copy in `~/Library/LaunchAgents`, so after any update you should:

```bash
cp /Users/rtdbot/sensi-imessage-retrival/launchd/com.sensi.imessage-ingest.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist
launchctl unload ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist
launchctl print gui/$(id -u)/com.sensi.imessage-ingest
```

If you edited the file directly in `~/Library/LaunchAgents`, skip the `cp` step and just run the `plutil`, `unload`, `load`, and `print` commands.

`launchctl print` is the reliable check for what `launchd` is actually using. If the file on disk says one schedule but `launchctl print` shows another, the old job definition is still loaded.

To turn off automatic daily runs entirely, unload the LaunchAgent:

```bash
launchctl unload ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist
```

If you want to remove it completely instead of just disabling it for now, delete the installed copy after unloading:

```bash
rm ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist
```

### Logs

The script writes its own dated application logs to `logs/`, for example `logs/imessage_ingest_2026-05-11.log`.

When launched by `launchd`, stdout and stderr are also captured in:

- `logs/launchd.out.log`
- `logs/launchd.err.log`

These paths come from the `StandardOutPath` and `StandardErrorPath` entries in the LaunchAgent plist.

## Image format support

All supported formats are stored and uploaded as JPEG. The local copy in `data/` always has a `.jpg` extension.

| Format | Handling |
|---|---|
| JPEG | Copied directly to `data/<date>/` as `.jpg` |
| PNG, HEIC/HEIF, GIF, WebP, TIFF, BMP, AVIF | Converted to JPEG via `sips` and saved as `.jpg` |
| All other `image/*` types | Skipped with a warning |

## Configuration

Top-level constants in [ingest_imessages.py](ingest_imessages.py) can be adjusted without touching the logic:

| Constant | Default | Description |
|---|---|---|
| `API_BASE_URL` | `http://localhost:8000` | Base URL of the Sensi Memory server |
| `CHAT_DB_PATH` | `~/Library/Messages/chat.db` | Path to the iMessage SQLite database |
| `STATE_FILE` | `.ingested_ids.json` | Deduplication state (resets daily) |
| `LOG_DIR` | `logs/` | Directory for dated log files |

## Project structure

```
sensi-imessage-retrival/
├── ingest_imessages.py   # Main script
├── utils.py              # Shared helpers (logging, vision, MIME constants)
├── .env.example          # Environment variable template
├── requirements.txt      # Minimal pip dependencies
├── API.md                # Sensi Memory HTTP & MCP API reference
├── data/                 # Local JPEG store, organised by date (git-ignored)
└── logs/                 # Runtime logs (git-ignored)
```

## Troubleshooting

**`OperationalError: unable to open database file`**
If this happens during a manual run, the app you launched Python from probably lacks Full Disk Access. If it happens only during a scheduled `launchd` run, the background Python runtime itself likely lacks Full Disk Access even if Terminal or VS Code already has it. Grant access in System Settings → Privacy & Security → Full Disk Access, then reload the LaunchAgent.

After updating the plist or changing macOS privacy permissions, reload and verify the job with:

```bash
launchctl unload ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.sensi.imessage-ingest.plist
launchctl print gui/$(id -u)/com.sensi.imessage-ingest
```

**`OPENAI_API_KEY not set — skipping vision analysis`**
The `.env` file is missing or the key is blank. This is also the supported way to disable OpenAI vision analysis. Images will still be ingested but without generated descriptions or meaningful tags.

**`sips conversion failed`**
The source image file is corrupted or in an unsupported variant. The attachment is skipped; check the log for details.

**Contacts always returns raw phone numbers**
The terminal app was denied Contacts permission. Grant access in System Settings → Privacy & Security → Contacts.
