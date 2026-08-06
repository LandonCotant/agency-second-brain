// Wikilink ([[X]]) parsing + edge materialization — port of
// common/wikilink_parser.py (ADR 0053). Best-effort: per-edge failures
// log and continue so a parent notes write is never unwound.

import { queryRows } from "../gcp/bq";
import type { Env } from "../types";

// Open `[[`, capture target up to `|` or `]`, optional `|alias`, close `]]`.
const WIKILINK_RE = /\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]/g;

export function extractWikilinks(markdown: string): string[] {
  if (!markdown) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const m of markdown.matchAll(WIKILINK_RE)) {
    const target = m[1].trim();
    if (!target || seen.has(target)) continue;
    seen.add(target);
    out.push(target);
  }
  return out;
}

export interface WikilinkResult {
  matched: number;
  resolved: number;
  inserted: number;
  skipped_duplicate: number;
  skipped_unresolved: number;
}

async function resolveTarget(
  env: Env,
  title: string,
  projectId: string,
  datasetId: string,
  notesTable: string,
): Promise<string | null> {
  const rows = await queryRows(
    env,
    `SELECT note_id FROM \`${projectId}.${datasetId}.${notesTable}\` ` +
      `WHERE LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) = LOWER(@title) ` +
      `AND COALESCE(hipaa_isolated, FALSE) = FALSE ` +
      `ORDER BY ingested_at DESC LIMIT 1`,
    [{ name: "title", type: "STRING", value: title }],
  );
  return rows.length ? (rows[0].note_id as string) : null;
}

export async function writeWikilinkEdges(
  env: Env,
  sourceNoteId: string,
  markdownContent: string,
  projectId: string,
  datasetId: string,
  notesTable: string,
  linksTable: string,
): Promise<WikilinkResult> {
  const targets = extractWikilinks(markdownContent);
  const result: WikilinkResult = {
    matched: targets.length,
    resolved: 0,
    inserted: 0,
    skipped_duplicate: 0,
    skipped_unresolved: 0,
  };
  if (!targets.length) return result;

  for (const targetTitle of targets) {
    let targetNoteId: string | null;
    try {
      targetNoteId = await resolveTarget(env, targetTitle, projectId, datasetId, notesTable);
    } catch (e) {
      console.error("wikilink resolve failed", sourceNoteId, targetTitle, e);
      continue;
    }
    if (!targetNoteId) {
      result.skipped_unresolved++;
      continue;
    }
    if (targetNoteId === sourceNoteId) {
      result.skipped_duplicate++;
      continue;
    }
    result.resolved++;

    try {
      const existing = await queryRows(
        env,
        `SELECT source_note_id FROM \`${projectId}.${datasetId}.${linksTable}\` ` +
          `WHERE source_note_id = @src AND target_note_id = @tgt AND link_type = 'wikilink' LIMIT 1`,
        [
          { name: "src", type: "STRING", value: sourceNoteId },
          { name: "tgt", type: "STRING", value: targetNoteId },
        ],
      );
      if (existing.length) {
        result.skipped_duplicate++;
        continue;
      }
      await queryRows(
        env,
        `INSERT INTO \`${projectId}.${datasetId}.${linksTable}\` ` +
          `(source_note_id, target_note_id, similarity, computed_at, link_type) ` +
          `VALUES (@src, @tgt, 1.0, @now, 'wikilink')`,
        [
          { name: "src", type: "STRING", value: sourceNoteId },
          { name: "tgt", type: "STRING", value: targetNoteId },
          { name: "now", type: "TIMESTAMP", value: new Date().toISOString() },
        ],
      );
      result.inserted++;
    } catch (e) {
      console.error("wikilink insert failed", sourceNoteId, targetNoteId, e);
    }
  }
  return result;
}
