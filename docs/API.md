# Sensi Memory HTTP API

Base URL: `http://localhost:8000`

An interactive version of these docs (Swagger UI) is available at `/docs` when the server is running.

---

## Endpoints

### `GET /health`

Liveness check. Returns immediately without touching the database or Gemini API.

**Response `200 OK`**
```json
{ "status": "ok" }
```

---

### `POST /ingest/text`

Embed and store a text string. Large inputs are split into paragraph-aligned chunks (≤ 2000 characters each by default); each chunk becomes its own record but all share the same `document_id`.

**Content-Type:** `application/json`

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes | The text to embed and store |
| `tags` | string[] | no | Labels attached to every chunk |
| `metadata` | object | no | Arbitrary key/value pairs stored alongside the record |
| `document_id` | string | no | Stable hex ID linking all chunks; auto-generated if omitted |
| `chunk` | boolean | no | Set to `false` to skip chunking (default: `true`) |

**Example request**
```bash
curl -X POST http://localhost:8000/ingest/text \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Paris is the capital of France.",
    "tags": ["geography", "europe"],
    "metadata": {"source": "wiki"}
  }'
```

**Response `200 OK`** — array of stored records, one per chunk

```json
[
  {
    "id": "abc123:chunk:0",
    "document_id": "abc123",
    "modality": "text",
    "document": "Paris is the capital of France.",
    "metadata": {
      "document_id": "abc123",
      "modality": "text",
      "created_at": "2026-05-05T12:00:00+00:00",
      "tags": "geography,europe",
      "mime_type": "text/plain",
      "chunk_index": 0,
      "chunk_count": 1,
      "source": "wiki"
    }
  }
]
```

**Error responses**

| Status | When |
|---|---|
| `422` | Text is empty or validation fails |
| `502` | Gemini API embedding call failed |

---

### `POST /ingest/image`

Embed and store an image file. Accepts a multipart form upload. Only PNG and JPEG are supported. The image is embedded using Gemini's multimodal model, optionally combined with a text caption.

**Content-Type:** `multipart/form-data`

**Form fields**

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | yes | PNG or JPEG image file |
| `sender` | string | yes | Who or what is ingesting this record |
| `source_path` | string | **yes** | Original path or URI of the source screenshot/image as known to the sender |
| `tags` | string | no | Comma-separated tags (e.g. `"photo,nature"`) |
| `text` | string | no | Caption or description used alongside the image embedding |
| `object_path` | string | no | Path to an artifact extracted from the source image, if stored alongside it |
| `metadata` | string | no | JSON object string (e.g. `'{"source":"camera"}'`) |
| `document_id` | string | no | Stable hex ID for the record; auto-generated if omitted |

**Example request**
```bash
curl -X POST http://localhost:8000/ingest/image \
  -F "file=@/path/to/photo.jpg" \
  -F "sender=ingest-bot" \
  -F "source_path=/data/images/photo.jpg" \
  -F "text=Sunset over the mountains" \
  -F "tags=photo,landscape" \
  -F "object_path=/data/objects/photo_crop.jpg" \
  -F 'metadata={"camera":"iPhone"}'
```

**Response `200 OK`** — single stored record

```json
{
  "id": "def456",
  "document_id": "def456",
  "modality": "image",
  "source_path": "/data/images/photo.jpg",
  "object_path": "/data/objects/photo_crop.jpg",
  "document": "Sunset over the mountains",
  "metadata": {
    "mime_type": "image/jpeg",
    "camera": "iPhone"
  }
}
```

**Error responses**

| Status | When |
|---|---|
| `422` | Unsupported image format, malformed `metadata` JSON, or file not found |
| `502` | Gemini API embedding call failed |

---

### `POST /search`

Embed a query string and return the most semantically similar records from the database. Results are ranked by cosine distance (lower = more similar).

**Content-Type:** `application/json`

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes | The search query |
| `top_k` | integer | no | Number of results to return (default: 5, minimum: 1) |
| `metadata_filter` | object | no | ChromaDB `where` filter to restrict results by metadata fields |

**Example request**
```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "text": "European capital cities",
    "top_k": 3
  }'
```

**Example with metadata filter**
```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "text": "mountain landscape",
    "top_k": 5,
    "metadata_filter": {"modality": {"$eq": "image"}}
  }'
```

**Response `200 OK`**

```json
{
  "hits": [
    {
      "id": "abc123:chunk:0",
      "document": "Paris is the capital of France.",
      "metadata": {
        "document_id": "abc123",
        "modality": "text",
        "created_at": "2026-05-05T12:00:00+00:00",
        "tags": "geography,europe",
        "mime_type": "text/plain",
        "chunk_index": 0,
        "chunk_count": 1
      },
      "distance": 0.142
    }
  ]
}
```

**Error responses**

| Status | When |
|---|---|
| `422` | Query text is empty |
| `502` | Gemini API embedding call failed |

---

### `POST /search/image`

Embed an uploaded image and return the most semantically similar records from the database. The image is **not stored** — it is embedded transiently and discarded after the query.

**Content-Type:** `multipart/form-data`

**Form fields**

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | yes | PNG or JPEG image file |
| `top_k` | integer | no | Number of results to return (default: 5, minimum: 1) |

**Example request**
```bash
curl -X POST http://localhost:8000/search/image \
  -F "file=@/path/to/photo.jpg" \
  -F "top_k=5"
```

**Response `200 OK`** — same structure as `POST /search`

```json
{
  "hits": [
    {
      "id": "def456",
      "document": "Sunset over the mountains",
      "metadata": {
        "modality": "image",
        "filename": "photo.jpg",
        "filepath": "/path/to/photo.jpg"
      },
      "distance": 0.087
    }
  ]
}
```

**Error responses**

| Status | When |
|---|---|
| `415` | File is not JPEG or PNG |
| `502` | Gemini API embedding call failed |

---

### `GET /export/csv`

Export every record in the database as a CSV file download. Embeddings are excluded. All metadata fields appear as columns; fields not present on a given record are left blank.

**Response `200 OK`** — `text/csv` file attachment (`export.csv`)

The CSV contains a header row followed by one row per stored record. The fixed columns come first, followed by all metadata keys found across the dataset (order reflects insertion order):

| Column | Description |
|---|---|
| `id` | Unique record ID |
| `document_id` | Parent document ID |
| `modality` | `"text"` or `"image"` |
| `document` | Stored text or image caption |
| `document_id` *(metadata)* | Repeated in metadata columns |
| `modality` *(metadata)* | Repeated in metadata columns |
| `created_at` | ISO 8601 UTC timestamp |
| `mime_type` | e.g. `"text/plain"`, `"image/jpeg"` |
| `tags` | Comma-separated tags (if set) |
| `chunk_index` | Text records only |
| `chunk_count` | Text records only |
| `source_path` | Image records only |
| `object_path` | Image records only |
| `filename` | Image records only |
| *(custom fields)* | Any user-supplied metadata attributes |

**Example request**
```bash
curl -o export.csv http://localhost:8000/export/csv
```

**Example output (truncated)**
```
id,document_id,modality,document,created_at,mime_type,tags,chunk_index,chunk_count,...
abc123:chunk:0,abc123,text,Paris is the capital of France.,2026-05-05T12:00:00+00:00,text/plain,geography,0,1,...
def456,def456,image,Sunset over the mountains,2026-05-05T12:01:00+00:00,image/jpeg,photo,,,,...
```

**Error responses**

| Status | When |
|---|---|
| `500` | Unexpected server error reading from ChromaDB |

---

## Data models

### `StoredRecord`

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique record ID (format: `<document_id>:chunk:<index>` for text, `<document_id>` for images) |
| `document_id` | string | Groups all chunks belonging to the same original document |
| `modality` | `"text"` \| `"image"` | Content type |
| `source_path` | `string \| null` | Original path or URI of the source screenshot/image. Set for image records; `null` for text. |
| `object_path` | `string \| null` | Path to an artifact extracted from the source image. `null` when not supplied. |
| `document` | string | The stored text (or image caption/filename for images) |
| `metadata` | object | All stored metadata fields (see below) |

### `SearchHit`

All fields from `StoredRecord` plus:

| Field | Type | Description |
|---|---|---|
| `distance` | float | Cosine distance from the query embedding (0 = identical, 2 = opposite) |

### Common metadata fields

| Field | Always present | Description |
|---|---|---|
| `document_id` | yes | Links chunks to their parent document |
| `modality` | yes | `"text"` or `"image"` |
| `created_at` | yes | ISO 8601 UTC timestamp |
| `mime_type` | yes | e.g. `"text/plain"`, `"image/jpeg"` |
| `tags` | if provided | Comma-separated tag string |
| `chunk_index` | text only | Zero-based index of this chunk |
| `chunk_count` | text only | Total number of chunks for the document |
| `filename` | image only | Original uploaded filename |

---

## Metadata filtering

The `metadata_filter` field in `/search` accepts ChromaDB's `where` clause syntax.

**Filter by modality**
```json
{ "modality": { "$eq": "image" } }
```

**Filter by tag (exact match on comma-joined string)**
```json
{ "tags": { "$contains": "geography" } }
```

**Combine conditions**
```json
{
  "$and": [
    { "modality": { "$eq": "text" } },
    { "chunk_index": { "$eq": 0 } }
  ]
}
```

See the [ChromaDB query docs](https://docs.trychroma.com/guides#filtering-by-metadata) for the full operator reference.

---

## MCP server (SSE)

The MCP server exposes the same ingest and search operations as MCP tools over HTTP+SSE, allowing LLM clients (Claude Desktop, Cursor, VS Code) to call them directly.

**Base URL:** `http://localhost:8001`
**SSE endpoint:** `http://localhost:8001/sse`

### Tools

| Tool | Arguments | Returns |
|---|---|---|
| `ingest_text` | `text: str`, `tags: str` (comma-separated, optional) | Confirmation string with stored record ID(s) |
| `ingest_image` | `image_path: str`, `description: str` (optional) | Confirmation string with record ID and source path |
| `search_memory` | `query: str`, `top_k: int` (default 5) | Formatted list of matching records with distance scores |

> **Note:** `ingest_image` takes a filesystem path, so the image must be accessible from inside the container. For network-based image ingestion, use the HTTP API's `POST /ingest/image` upload endpoint instead.

### Client configuration

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "sensi-memory": {
      "url": "http://localhost:8001/sse"
    }
  }
}
```

**Cursor / VS Code** — same URL, configured in each client's MCP settings.

---

## Running the servers

**With Docker — starts both HTTP and MCP (recommended)**
```bash
docker compose up --build
```

This starts three containers sharing the same ChromaDB volume:
- `sensi-http` on port 8000 — REST API for scripts and non-LLM clients
- `sensi-mcp` on port 8001 — MCP SSE server for LLM clients
- `sensi-visualisation` on port 3000 — browser UI for search and image upload

**Locally (after `pip install -e .`)**
```bash
# HTTP server
sensi-memory-http

# MCP server (separate terminal)
sensi-memory-mcp
```

Both require `GEMINI_API_KEY` to be set in the environment or in a `.env` file at the project root.
