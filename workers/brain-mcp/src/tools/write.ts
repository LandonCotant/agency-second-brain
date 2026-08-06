// Write tools — port of mcp_server/tools/write.py. Each carries the same
// safety wrapper (forced scope/status, hash dedup, drafted-only guard).

import { getConfig } from "../config";
import { queryRows, type QueryParam } from "../gcp/bq";
import { embed } from "../gcp/vertex";
import { sha256hex } from "../lib/hash";
import { writeWikilinkEdges } from "../lib/wikilink";
import type { Env } from "../types";

const FEEDBACK_SCOPES = ["risk", "triage", "draft"];
const FEEDBACK_VERDICTS = ["noise", "valid", "wrong_tone", "wrong_target"];

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}
function addDays(d: Date, n: number): Date {
  return new Date(d.getTime() + n * 86_400_000);
}
function mondayOf(d: Date): Date {
  const dow = (d.getUTCDay() + 6) % 7; // 0 = Monday
  return addDays(d, -dow);
}
function uuid12(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
}

// ADR 0052 — companion notes row so brain_ask retrieves decisions/wins.
async function insertSyntheticNote(
  env: Env,
  opts: {
    noteId: string;
    noteKind: string;
    sourceRecordId: string;
    title: string;
    body: string;
    revisionId: string;
    extractionMethod: string;
  },
): Promise<{ inserted: boolean; error: string | null }> {
  const cfg = getConfig(env);
  const existing = await queryRows(
    env,
    `SELECT note_id FROM \`${cfg.projectId}.${cfg.outputsDataset}.notes\` WHERE note_id = @nid LIMIT 1`,
    [{ name: "nid", type: "STRING", value: opts.noteId }],
  );
  if (existing.length) return { inserted: false, error: null };

  const markdown = `# ${opts.title}\n\n${opts.body}`.trim();
  const contentHash = await sha256hex(markdown);
  let embedding: number[];
  try {
    embedding = await embed(env, markdown);
  } catch (e) {
    return { inserted: false, error: `embed_failed: ${e}` };
  }
  const now = new Date().toISOString();
  await queryRows(
    env,
    `INSERT INTO \`${cfg.projectId}.${cfg.outputsDataset}.notes\` ` +
      `(note_id, revision_id, ingested_at, created_at, source_drive_file_id, source_drive_url, ` +
      `filename, extraction_method, extraction_confidence, page_count, hipaa_isolated, note_kind, ` +
      `scope, markdown_content, external_id, embedding, embedding_model, embedding_content_hash) ` +
      `VALUES (@note_id, @revision_id, @now, @now, @source_drive_file_id, '', @filename, ` +
      `@extraction_method, 1.0, 1, FALSE, @note_kind, 'personal', @text, @external_id, ` +
      `@embedding, 'text-embedding-005', @content_hash)`,
    [
      { name: "note_id", type: "STRING", value: opts.noteId },
      { name: "revision_id", type: "STRING", value: opts.revisionId },
      { name: "now", type: "TIMESTAMP", value: now },
      { name: "source_drive_file_id", type: "STRING", value: `${opts.noteKind}:${opts.sourceRecordId}` },
      { name: "filename", type: "STRING", value: opts.title.slice(0, 120) },
      { name: "extraction_method", type: "STRING", value: opts.extractionMethod },
      { name: "note_kind", type: "STRING", value: opts.noteKind },
      { name: "text", type: "STRING", value: markdown },
      { name: "external_id", type: "STRING", value: opts.sourceRecordId },
      { name: "embedding", type: "ARRAY_FLOAT64", value: embedding },
      { name: "content_hash", type: "STRING", value: contentHash },
    ],
  );
  try {
    await writeWikilinkEdges(
      env,
      opts.noteId,
      markdown,
      cfg.projectId,
      cfg.outputsDataset,
      cfg.notesTable,
      cfg.linksTable,
    );
  } catch (e) {
    console.error("synthetic note wikilink write failed", opts.noteId, e);
  }
  return { inserted: true, error: null };
}

export async function captureNote(env: Env, text: string, sourceHint?: string) {
  text = (text || "").trim();
  if (!text) return { note_id: null, inserted: false, duplicate: false, error: "empty text" };

  const cfg = getConfig(env);
  const contentHash = await sha256hex(text);
  const noteId = `cap-${contentHash.slice(0, 12)}`;
  const filename = sourceHint || `capture-${isoDate(new Date())}`;
  const now = new Date().toISOString();

  const existing = await queryRows(
    env,
    `SELECT note_id FROM \`${cfg.projectId}.${cfg.outputsDataset}.notes\` ` +
      `WHERE note_id = @nid AND note_kind = 'capture' LIMIT 1`,
    [{ name: "nid", type: "STRING", value: noteId }],
  );
  if (existing.length) return { note_id: noteId, inserted: false, duplicate: true };

  let embedding: number[];
  try {
    embedding = await embed(env, text);
  } catch (e) {
    return { note_id: noteId, inserted: false, duplicate: false, error: String(e) };
  }
  await queryRows(
    env,
    `INSERT INTO \`${cfg.projectId}.${cfg.outputsDataset}.notes\` ` +
      `(note_id, revision_id, ingested_at, created_at, source_drive_file_id, source_drive_url, ` +
      `filename, extraction_method, extraction_confidence, page_count, hipaa_isolated, note_kind, ` +
      `scope, markdown_content, external_id, embedding, embedding_model, embedding_content_hash) ` +
      `VALUES (@note_id, @revision_id, @now, @now, @source_drive_file_id, '', @filename, ` +
      `'mcp-capture-v1', 1.0, 1, FALSE, 'capture', 'personal', @text, @note_id, @embedding, ` +
      `'text-embedding-005', @content_hash)`,
    [
      { name: "note_id", type: "STRING", value: noteId },
      { name: "revision_id", type: "STRING", value: contentHash },
      { name: "now", type: "TIMESTAMP", value: now },
      { name: "source_drive_file_id", type: "STRING", value: `capture:${noteId}` },
      { name: "filename", type: "STRING", value: filename },
      { name: "text", type: "STRING", value: text },
      { name: "embedding", type: "ARRAY_FLOAT64", value: embedding },
      { name: "content_hash", type: "STRING", value: contentHash },
    ],
  );
  const wikilinks = await writeWikilinkEdges(
    env,
    noteId,
    text,
    cfg.projectId,
    cfg.outputsDataset,
    cfg.notesTable,
    cfg.linksTable,
  );
  return { note_id: noteId, inserted: true, duplicate: false, wikilinks };
}

export async function markDecisionStatus(env: Env, decisionId: string, status: string) {
  if (status !== "confirmed" && status !== "dismissed") {
    return { updated: false, error: `status must be 'confirmed' or 'dismissed', got ${JSON.stringify(status)}` };
  }
  const cfg = getConfig(env);
  const prior = await queryRows(
    env,
    `SELECT status FROM \`${cfg.projectId}.${cfg.outputsDataset}.decisions\` WHERE decision_id = @did LIMIT 1`,
    [{ name: "did", type: "STRING", value: decisionId }],
  );
  const priorStatus = prior.length ? (prior[0].status as string) : null;
  if (priorStatus === null) return { updated: false, prior_status: null, error: "decision not found" };
  if (priorStatus !== "drafted") return { updated: false, prior_status: priorStatus, new_status: priorStatus };

  await queryRows(
    env,
    `UPDATE \`${cfg.projectId}.${cfg.outputsDataset}.decisions\` ` +
      `SET status = @status, refined_at = CURRENT_TIMESTAMP() ` +
      `WHERE decision_id = @did AND status = 'drafted'`,
    [
      { name: "did", type: "STRING", value: decisionId },
      { name: "status", type: "STRING", value: status },
    ],
  );
  return { updated: true, prior_status: "drafted", new_status: status };
}

export async function insertDecision(env: Env, text: string, source?: string) {
  text = (text || "").trim();
  if (!text) return { decision_id: null, inserted: false, error: "empty text" };

  const cfg = getConfig(env);
  const decisionId = `dec-${uuid12()}`;
  const now = new Date();
  const nowIso = now.toISOString();
  const firstLine = (text.split("\n")[0] || "").trim();
  const title = (firstLine || text).slice(0, 80);
  await queryRows(
    env,
    `INSERT INTO \`${cfg.projectId}.${cfg.outputsDataset}.decisions\` ` +
      `(decision_id, decided_at, title, context, choice, status, ` +
      `review_30_at, review_90_at, review_365_at, source_reflection_id) ` +
      `VALUES (@did, @now, @title, @context, @choice, 'drafted', @r30, @r90, @r365, @source)`,
    [
      { name: "did", type: "STRING", value: decisionId },
      { name: "now", type: "TIMESTAMP", value: nowIso },
      { name: "title", type: "STRING", value: title },
      { name: "context", type: "STRING", value: "" },
      { name: "choice", type: "STRING", value: text },
      { name: "r30", type: "DATE", value: isoDate(addDays(now, 30)) },
      { name: "r90", type: "DATE", value: isoDate(addDays(now, 90)) },
      { name: "r365", type: "DATE", value: isoDate(addDays(now, 365)) },
      { name: "source", type: "STRING", value: source || "mcp-server" },
    ],
  );
  const syntheticNoteId = `syn-dec-${decisionId}`;
  const { inserted, error } = await insertSyntheticNote(env, {
    noteId: syntheticNoteId,
    noteKind: "decision",
    sourceRecordId: decisionId,
    title,
    body: text,
    revisionId: nowIso,
    extractionMethod: "synthetic-decision-v1",
  });
  return {
    decision_id: decisionId,
    inserted: true,
    synthetic_note_id: inserted ? syntheticNoteId : null,
    synthetic_note_error: error,
  };
}

export async function insertWin(env: Env, title: string, context?: string, sourceId?: string) {
  title = (title || "").trim();
  if (!title) return { win_id: null, inserted: false, duplicate: false, error: "empty title" };

  const cfg = getConfig(env);
  const titleHash12 = (await sha256hex(title.toLowerCase())).slice(0, 12);
  const now = new Date();
  const weekOf = mondayOf(now);
  const winId = `mcp-${isoDate(weekOf)}-${titleHash12}`;

  const existing = await queryRows(
    env,
    `SELECT win_id FROM \`${cfg.projectId}.${cfg.outputsDataset}.wins\` WHERE win_id = @wid LIMIT 1`,
    [{ name: "wid", type: "STRING", value: winId }],
  );
  if (existing.length) return { win_id: existing[0].win_id, inserted: false, duplicate: true };

  await queryRows(
    env,
    `INSERT INTO \`${cfg.projectId}.${cfg.outputsDataset}.wins\` ` +
      `(win_id, captured_at, week_of, source_kind, source_id, title, summary) ` +
      `VALUES (@wid, @now, @week_of, 'mcp', @source, @title, @summary)`,
    [
      { name: "wid", type: "STRING", value: winId },
      { name: "now", type: "TIMESTAMP", value: now.toISOString() },
      { name: "week_of", type: "DATE", value: isoDate(weekOf) },
      { name: "source", type: "STRING", value: sourceId || "mcp-server" },
      { name: "title", type: "STRING", value: title },
      { name: "summary", type: "STRING", value: context || "" },
    ],
  );
  const syntheticNoteId = `syn-win-${winId}`;
  const { inserted, error } = await insertSyntheticNote(env, {
    noteId: syntheticNoteId,
    noteKind: "win",
    sourceRecordId: winId,
    title,
    body: context || "",
    revisionId: now.toISOString(),
    extractionMethod: "synthetic-win-v1",
  });
  return {
    win_id: winId,
    inserted: true,
    duplicate: false,
    synthetic_note_id: inserted ? syntheticNoteId : null,
    synthetic_note_error: error,
  };
}

export async function recordFeedback(
  env: Env,
  args: {
    scope: string;
    verdict: string;
    account_name?: string;
    pattern_name?: string;
    note?: string;
    mute_days?: number;
    source_flag_id?: string;
  },
) {
  const { scope, verdict, account_name, pattern_name, note, mute_days, source_flag_id } = args;
  if (!FEEDBACK_SCOPES.includes(scope)) {
    return { inserted: false, error: `scope must be one of ${FEEDBACK_SCOPES}, got ${JSON.stringify(scope)}` };
  }
  if (!FEEDBACK_VERDICTS.includes(verdict)) {
    return { inserted: false, error: `verdict must be one of ${FEEDBACK_VERDICTS}, got ${JSON.stringify(verdict)}` };
  }
  if (mute_days !== undefined && (!Number.isInteger(mute_days) || mute_days <= 0)) {
    return { inserted: false, error: `mute_days must be a positive int, got ${JSON.stringify(mute_days)}` };
  }
  const cfg = getConfig(env);

  let accountId: string | null = null;
  if (account_name) {
    const rows = await queryRows(
      env,
      `SELECT DISTINCT _airtable_record_id FROM \`${cfg.projectId}.airtable_replica.accounts\` ` +
        `WHERE LOWER(company_name) = LOWER(@name) LIMIT 2`,
      [{ name: "name", type: "STRING", value: account_name.trim() }],
    );
    if (!rows.length) return { inserted: false, error: `no account matched company_name ${JSON.stringify(account_name)}` };
    if (rows.length > 1) {
      return { inserted: false, error: `account_name ${JSON.stringify(account_name)} is ambiguous (matched >1 record); be more specific` };
    }
    accountId = rows[0]._airtable_record_id as string;
  }

  const now = new Date();
  const nowIso = now.toISOString();
  let muteUntilIso: string | null = null;
  if (verdict === "noise" && mute_days !== undefined) {
    muteUntilIso = addDays(now, mute_days).toISOString();
  }

  const existing = await queryRows(
    env,
    `SELECT feedback_id FROM \`${cfg.projectId}.${cfg.outputsDataset}.signal_feedback\` ` +
      `WHERE scope = @scope AND verdict = @verdict ` +
      `AND account_id IS NOT DISTINCT FROM @account_id ` +
      `AND pattern_name IS NOT DISTINCT FROM @pattern_name ` +
      `AND created_at >= TIMESTAMP_SUB(@now, INTERVAL 60 SECOND) ` +
      `ORDER BY created_at DESC LIMIT 1`,
    [
      { name: "scope", type: "STRING", value: scope },
      { name: "verdict", type: "STRING", value: verdict },
      { name: "account_id", type: "STRING", value: accountId },
      { name: "pattern_name", type: "STRING", value: pattern_name ?? null },
      { name: "now", type: "TIMESTAMP", value: nowIso },
    ],
  );
  if (existing.length) {
    return {
      feedback_id: existing[0].feedback_id,
      inserted: false,
      duplicate: true,
      account_id: accountId,
      mute_until: muteUntilIso,
    };
  }

  const feedbackId = `fb-${uuid12()}`;
  await queryRows(
    env,
    `INSERT INTO \`${cfg.projectId}.${cfg.outputsDataset}.signal_feedback\` ` +
      `(feedback_id, created_at, scope, account_id, pattern_name, verdict, note, mute_until, ` +
      `source_flag_id, created_by) ` +
      `VALUES (@feedback_id, @now, @scope, @account_id, @pattern_name, @verdict, @note, ` +
      `@mute_until, @source_flag_id, 'operator')`,
    [
      { name: "feedback_id", type: "STRING", value: feedbackId },
      { name: "now", type: "TIMESTAMP", value: nowIso },
      { name: "scope", type: "STRING", value: scope },
      { name: "account_id", type: "STRING", value: accountId },
      { name: "pattern_name", type: "STRING", value: pattern_name ?? null },
      { name: "verdict", type: "STRING", value: verdict },
      { name: "note", type: "STRING", value: note ?? null },
      { name: "mute_until", type: "TIMESTAMP", value: muteUntilIso },
      { name: "source_flag_id", type: "STRING", value: source_flag_id ?? null },
    ],
  );
  return { feedback_id: feedbackId, inserted: true, duplicate: false, account_id: accountId, mute_until: muteUntilIso };
}
