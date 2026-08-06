# ADR 0049 — Gmail-into-corpus (CRM Auto-updater side effect)

**Status:** Accepted — 2026-05-11 (Phase H, WS-G corpus expansion).
Extends ADR 0047 (CRM Auto-updater). Closes one of the v2 questions
raised on the WS-G7 Knowledge Surfacer roadmap.

## Context

The Knowledge Surfacer `/ask` retrieves from `agent_outputs.notes` but
the corpus has never included email bodies. The 2026-05-11 plan
identified "not enough stuff ingested" as the headline gap. Gmail
content rarely produces an actionable Task but routinely holds the
context `/ask` needs to answer ("what did Acme say about timing last
week?", "did anyone reply to that proposal?").

Two architectural paths were considered (from the ROADMAP v2 questions):

- **(a) Pre-index labeled emails into `agent_outputs.notes`** —
  scope-bounded by the existing `secondbrain` label, retrieval works
  with `VECTOR_SEARCH`, requires only embedding cost (no extra DWD).
- **(b) Live-fetch via `gmail.readonly`** — no storage, 2-5s per query,
  keyword search only, harder to compose with structured retrieval.

(a) is chosen. The CRM Auto-updater (ADR 0047) already reads
`secondbrain`-labeled messages once per day and runs Gemini structured
extraction on them. The cheapest path is to add a side-effect notes
write inside the existing agent — no new Cloud Run Job, no new
Dockerfile, no new IAM, no new scheduler.

## Decision

### §1 — Side-effect write inside `CrmUpdaterAgent`

After the existing extraction step (`Extractor.extract`) succeeds and
the Airtable draft writer runs, the agent invokes a new
`CrmNotesWriter.write(message)` that:

1. Pre-INSERT SELECT on
   `WHERE external_id = @msg_id AND note_kind = 'email'` — dedup-skip
   on hit (a re-processed thread, before the `secondbrain-processed`
   label gets applied, must not double-insert).
2. Builds the headered Markdown payload (see §3).
3. Embeds via `text-embedding-005` (ADR 0038 §1) using the same
   `VertexEmbedder` already wired by `notes_ingestor` / `librarian` /
   `captures_materializer` / `evening_reflection` / `knowledge_surfacer`.
4. INSERTs a row into `agent_outputs.notes` with
   `note_kind='email'`, `scope='agency'`, `hipaa_isolated=False`,
   `external_id=message_id`.

The notes write is **isolated from the draft-write path**: a failed
embed or BQ insert logs and surfaces as `NotesWriteOutcome(inserted=False,
skip_reason=...)`, but does NOT raise into the agent. Airtable drafts
are the primary product of the Job; corpus enrichment is a side
benefit that must not block them.

### §2 — Dedup + row keys

| Column                 | Value                                              |
|------------------------|----------------------------------------------------|
| `note_id`              | `email:<message_id>` (prefixed for human-readable provenance) |
| `revision_id`          | `message_id` (emails have no revision concept)     |
| `external_id`          | `message_id`                                       |
| `source_drive_file_id` | `""`                                               |
| `note_kind`            | `'email'` (new string value; column is STRING not enum) |
| `scope`                | `'agency'`                                         |
| `hipaa_isolated`       | `False` (HIPAA pre-flight blocks before this point) |
| `extraction_method`    | `'email-passthrough'`                              |
| `extraction_confidence`| `1.0`                                              |
| `created_at`           | `message.received_at` (partition column)           |

Dedup key is `(external_id, note_kind='email')`. The Calendar ingester
uses the same `(external_id, note_kind='calendar_event')` shape per
ADR 0046; this consistency lets future cross-source-type queries use a
single pattern.

### §3 — Markdown shape

```
From: alice@example.com
Subject: Project kickoff next week
Date: 2026-05-11T14:32:00+00:00
To: owner@example.com

<body_text>
```

Header lines precede the body so the embedding (and downstream
Gemini synthesis in `/ask`) sees the sender + subject + date alongside
the content. Same pattern as ADR 0048 §5 for Solutions client folders.
No schema change — header is just text inside `markdown_content`.

### §4 — HIPAA stance

The CRM Auto-updater already does a HIPAA pre-flight on participant
domains (ADR 0047 §4 + PR B1 wire-up). HIPAA-blocked emails skip BOTH
the extractor AND the notes write — by design, so HIPAA content never
reaches `/ask`. Acceptance test
`test_notes_writer_skipped_on_hipaa_block` pins this.

### §5 — No new IAM / DWD / Dockerfile

- `asb-crm-updater-sa` already has `aiplatform.endpoints.predict`
  (used today by the extractor) — same permission covers
  `text-embedding-005`.
- `asb-crm-updater-sa` already has `bigquery.dataEditor` on
  `agent_outputs` (for the `crm_updater_runs` checkpoint) — same
  permission writes to `agent_outputs.notes`.
- `Dockerfile.crm-updater` already pins `google-cloud-aiplatform>=1.46`
  which includes `vertexai.language_models.TextEmbeddingModel`.
- `gmail.readonly` DWD scope is already on `asb-agent-triage-sa` per
  ADR 0047 §3 — no scope expansion.

The only new TF surface is two env vars on the existing Cloud Run Job
(`VERTEX_LOCATION`, `EMBEDDING_MODEL`) and a kill-switch
(`CRM_UPDATER_DISABLE_NOTES_WRITE`, default empty = ON).

### §6 — Cost

Per-email cost adds one `text-embedding-005` call (~$0.0001/email at
768-dim). At ~10 emails/day (ADR 0047 §6 baseline), the side-effect
adds ~$0.001/day ≈ $0.03/month. Well within the $50/mo budget
(ADR 0024).

## Consequences

- `/ask` retrieves email content from the next CRM Auto-updater tick
  forward. Re-running on already-processed emails (label cleared) is
  idempotent via the dedup pre-check.
- Backfill of historical `secondbrain`-labeled emails is NOT automatic.
  If the user wants the full history in the corpus, they remove the
  `secondbrain-processed` label from old threads; the next tick picks
  them up and they dedup-skip on any that are still in the corpus.
- A failed embed lands the row without an embedding — same backfill
  path the `notes_ingestor` `embeddings_only` mode (ADR 0039 §4) uses
  for pre-ADR-0038 rows. The row's `extraction_method='email-passthrough'`
  + `embedding IS NULL` lets the backfiller find it.
- Kill switch: setting `crm_updater_disable_notes_write = "1"` in tfvars
  + targeted apply disables the side effect without rebuilding the
  image. Useful if Vertex embeddings cost spikes or if the dedup
  pre-check ever produces false positives.

## Out of scope

- **Full-mailbox Gmail ingestion.** v1 stays on `secondbrain`-labeled
  threads (high signal, low volume). Re-evaluate after a month of
  usage data.
- **Live-fetch `/ask` path.** Option (b) above. Not pursued in v1
  because the pre-index path has materially better retrieval quality
  for the same operator-effort.
- **Thread reconstruction.** Each email is its own row. If the user
  wants thread-level summaries in `/ask`, that's a future thread
  aggregation step on top of the per-message rows.

## Supersedence

None. Extends ADR 0047. Closes the "Gmail-into-corpus" v2 question on
the ROADMAP.
