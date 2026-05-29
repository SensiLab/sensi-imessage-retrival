# Database Schema

## Storage engine

[ChromaDB](https://www.trychroma.com/) persistent collection, stored on disk at the path configured by `SENSI_CHROMA_PATH` (default: `./local_storage`).

| Setting | Default | Env var |
|---|---|---|
| Collection name | `sensi_memories` | `SENSI_CHROMA_COLLECTION` |
| Distance metric | cosine | — |
| Embedding model | `gemini-embedding-2` | `SENSI_EMBEDDING_MODEL` |
| Embedding dimensions | 3072 | `SENSI_EMBEDDING_DIMENSIONS` |

---

## Record structure

Each record in the collection has four physical columns (ChromaDB internals):

| Column | Description |
|---|---|
| `id` | Unique string identifier for the record |
| `document` | The raw text content embedded into the vector |
| `embedding` | Float vector (3072 dimensions by default) |
| `metadata` | Flat key-value store for all other fields |

At the **Python model layer**, records are surfaced as structured objects with first-class typed fields separate from the freeform custom metadata dict.

---

## First-class fields

These fields are present on every record and exposed as typed attributes on `StoredRecord` and `SearchHit`. They are **not** included in the `metadata` dict.

| Field | Type | Who sets it | Description |
|---|---|---|---|
| `id` | `str` | code | Unique record identifier (see format below) |
| `document_id` | `str` | user (optional) | Groups all chunks that belong to the same source document. Auto-generated if not provided. |
| `sender` | `str` | **user (required)** | Identifies who or what ingested this record (e.g. a user ID, service name, or agent name) |
| `modality` | `"text"` \| `"image"` | code | Determined by ingest path; never user-supplied |
| `tags` | `list[str]` | **user (required)** | Arbitrary labels for the record. Pass `[]` for no tags. |
| `date` | `str` (ISO 8601 UTC) | code | Timestamp when the record was created |
| `source_path` | `str \| null` | **user (required for image)** | Original path or URI of the source screenshot/image provided by the sender. Required for image ingest; `null` for text records. |
| `object_path` | `str \| null` | user (optional) | Path to an artifact extracted from the original source image (e.g. a crop or derived file) and stored alongside it. Optional for image records; `null` when not supplied. |
| `document` | `str` | code | The text content (chunk text, image caption, or image filename fallback) |

---

## Metadata dict

The `metadata` field on `StoredRecord` / `SearchHit` contains only fields that are **not** promoted to first-class status. These come from two sources:

### Technical fields (code-generated)

| Field | Present for | Description |
|---|---|---|
| `mime_type` | all records | MIME type of the content (`text/plain`, `image/png`, `image/jpeg`) |
| `chunk_index` | text records | Zero-based index of this chunk within its document |
| `chunk_count` | text records | Total number of chunks for this document |


### Custom user attributes

Any extra key-value pairs passed as `metadata` on the ingest request are stored here verbatim. Values must be scalar types (`str`, `int`, `float`, `bool`) due to ChromaDB constraints.

---

## Record ID format

| Modality | Format | Example |
|---|---|---|
| Text chunk | `{document_id}:chunk:{index}` | `abc123:chunk:0` |
| Image | `{document_id}` | `def456` |

A multi-chunk text document with `document_id = "abc123"` and 3 chunks produces records `abc123:chunk:0`, `abc123:chunk:1`, `abc123:chunk:2`, all sharing the same `document_id`.

---

## Example records

### Text chunk

```json
{
  "id": "abc123:chunk:0",
  "document_id": "abc123",
  "sender": "user-42",
  "modality": "text",
  "tags": ["meeting", "notes"],
  "date": "2025-05-25T10:30:00+00:00",
  "document": "Discussed Q3 roadmap and resource allocation...",
  "metadata": {
    "mime_type": "text/plain",
    "chunk_index": 0,
    "chunk_count": 3,
    "project": "roadmap-2025"
  }
}
```

### Image

```json
{
  "id": "def456",
  "document_id": "def456",
  "sender": "ingest-bot",
  "modality": "image",
  "tags": ["diagram", "architecture"],
  "date": "2025-05-25T11:00:00+00:00",
  "source_path": "/data/images/arch.png",
  "object_path": "/data/objects/arch_crop.png",
  "document": "system architecture diagram",
  "metadata": {
    "mime_type": "image/png"
  }
}
```

---

## Filtering

Records can be filtered at query time using ChromaDB WHERE clauses. Because `sender`, `modality`, `tags`, and `date` are stored in the underlying Chroma metadata dict, they are all valid filter targets.

**Note:** `tags` is stored as a comma-separated string internally (e.g. `"meeting,notes"`). Use `$contains` for partial matching:

```json
{ "tags": { "$contains": "meeting" } }
```

Filter by sender:

```json
{ "sender": { "$eq": "user-42" } }
```

Filter by modality:

```json
{ "modality": { "$eq": "image" } }
```
