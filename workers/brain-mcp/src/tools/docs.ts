// update_weekly_doc — port of mcp_server/tools/docs.py.
//
// Prepends today's section to a consolidated weekly/quarterly Google Doc.
// Auth: Drive-scoped token via asb-mcp-sa -> asb-agent-triage-sa
// impersonation (ADR 0044); rollup folders must be shared with the triage
// SA as Editor. Idempotent on (kind, date) via the first-H1 header check.

import { getConfig } from "../config";
import { getDriveToken } from "../gcp/auth";
import { queryRows } from "../gcp/bq";
import { extractWikilinks } from "../lib/wikilink";
import type { Env } from "../types";

const GDOC_MIME = "application/vnd.google-apps.document";
const WIKILINK_RE = /\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]/g;

// ---- date helpers ----
function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}
function mondayOf(d: Date): Date {
  const dow = (d.getUTCDay() + 6) % 7;
  return new Date(d.getTime() - dow * 86_400_000);
}
function quarterOf(d: Date): number {
  return Math.floor(d.getUTCMonth() / 3) + 1;
}
function parseIsoDate(value: string): [Date | null, string | null] {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return [null, `date must be ISO YYYY-MM-DD, got ${JSON.stringify(value)}`];
  }
  const d = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) {
    return [null, `date must be ISO YYYY-MM-DD, got ${JSON.stringify(value)}`];
  }
  return [d, null];
}

function resolveFolderId(env: Env, kind: string): string | null {
  if (kind === "brief") return env.BRAIN_BRIEFS_FOLDER_ID || null;
  if (kind === "reflection") return env.BRAIN_REFLECTIONS_FOLDER_ID || null;
  if (kind === "review") return env.BRAIN_REVIEWS_FOLDER_ID || null;
  return null;
}

function docTitleFor(kind: string, anchor: Date): string {
  if (kind === "review") return `Weekly Reviews — Q${quarterOf(anchor)} ${anchor.getUTCFullYear()}`;
  const weekMonday = isoDate(mondayOf(anchor));
  if (kind === "brief") return `Morning Briefs — Week of ${weekMonday}`;
  return `Evening Reflections — Week of ${weekMonday}`;
}

function sectionHeader(dateIso: string, label?: string): string {
  return label ? `${dateIso} — ${label}` : dateIso;
}

function sectionAlreadyInserted(existingPlain: string, dateIso: string, label?: string): boolean {
  if (!existingPlain) return false;
  const header = sectionHeader(dateIso, label);
  for (const line of existingPlain.split("\n")) {
    const stripped = line.trim();
    if (!stripped) continue;
    return stripped === header;
  }
  return false;
}

// ---- wikilink → Drive-URL linkification ----
async function lookupGalaxyUrls(env: Env, targets: string[]): Promise<Map<string, string>> {
  if (!targets.length) return new Map();
  const cfg = getConfig(env);
  const rows = await queryRows(
    env,
    `WITH ranked AS ( SELECT ` +
      `LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) AS key, ` +
      `source_drive_url AS url, ` +
      `ROW_NUMBER() OVER ( PARTITION BY LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) ` +
      `ORDER BY ingested_at DESC ) AS rn ` +
      `FROM \`${cfg.projectId}.${cfg.notesDataset}.${cfg.notesTable}\` ` +
      `WHERE LOWER(REGEXP_REPLACE(filename, r'\\.[a-zA-Z0-9]+$', '')) IN UNNEST(@targets) ` +
      `AND COALESCE(hipaa_isolated, FALSE) = FALSE AND source_drive_url IS NOT NULL ) ` +
      `SELECT key, url FROM ranked WHERE rn = 1`,
    [{ name: "targets", type: "ARRAY_STRING", value: targets.map((t) => t.toLowerCase()) }],
  );
  const m = new Map<string, string>();
  for (const r of rows) m.set(String(r.key), String(r.url));
  return m;
}

async function linkifyWikilinks(
  env: Env,
  sectionMd: string,
): Promise<{ text: string; links: [number, number, string][] }> {
  const targets = extractWikilinks(sectionMd);
  const urls = targets.length ? await lookupGalaxyUrls(env, targets) : new Map<string, string>();
  if (!urls.size) return { text: sectionMd, links: [] };

  const outParts: string[] = [];
  const links: [number, number, string][] = [];
  let cursor = 0;
  let outLen = 0;
  for (const m of sectionMd.matchAll(WIKILINK_RE)) {
    const start = m.index ?? 0;
    const end = start + m[0].length;
    const target = m[1].trim();
    const url = urls.get(target.toLowerCase());
    if (url === undefined) {
      const chunk = sectionMd.slice(cursor, end);
      outParts.push(chunk);
      outLen += chunk.length;
    } else {
      const prefix = sectionMd.slice(cursor, start);
      outParts.push(prefix);
      outLen += prefix.length;
      const inner = m[0].slice(2, -2);
      const display = (inner.includes("|") ? inner.split("|")[1] : inner).trim();
      const linkStart = outLen;
      outParts.push(display);
      outLen += display.length;
      links.push([linkStart, outLen, url]);
    }
    cursor = end;
  }
  outParts.push(sectionMd.slice(cursor));
  return { text: outParts.join(""), links };
}

// ---- Drive / Docs REST ----
async function driveFindDoc(env: Env, folderId: string, title: string): Promise<string | null> {
  const token = await getDriveToken(env);
  const safeTitle = title.replace(/'/g, "\\'");
  const q =
    `name = '${safeTitle}' and '${folderId}' in parents and ` +
    `mimeType = '${GDOC_MIME}' and trashed = false`;
  const url =
    `https://www.googleapis.com/drive/v3/files?` +
    `q=${encodeURIComponent(q)}&fields=${encodeURIComponent("files(id,name)")}` +
    `&pageSize=1&supportsAllDrives=true&includeItemsFromAllDrives=true`;
  const resp = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) throw new Error(`drive files.list failed: ${resp.status} ${await resp.text()}`);
  const data = (await resp.json()) as { files?: { id: string }[] };
  return data.files && data.files.length ? data.files[0].id : null;
}

async function driveCreateEmptyDoc(env: Env, folderId: string, title: string): Promise<string> {
  const token = await getDriveToken(env);
  const resp = await fetch("https://www.googleapis.com/drive/v3/files?fields=id&supportsAllDrives=true", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ name: title, mimeType: GDOC_MIME, parents: [folderId] }),
  });
  if (!resp.ok) throw new Error(`drive files.create failed: ${resp.status} ${await resp.text()}`);
  const data = (await resp.json()) as { id: string };
  return data.id;
}

async function driveReadPlainText(env: Env, fileId: string): Promise<string> {
  const token = await getDriveToken(env);
  const resp = await fetch(
    `https://www.googleapis.com/drive/v3/files/${fileId}/export?mimeType=text/plain`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!resp.ok) throw new Error(`drive files.export failed: ${resp.status} ${await resp.text()}`);
  return resp.text();
}

async function docsBatchUpdate(env: Env, fileId: string, requests: unknown[]): Promise<void> {
  const token = await getDriveToken(env);
  const resp = await fetch(`https://docs.googleapis.com/v1/documents/${fileId}:batchUpdate`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ requests }),
  });
  if (!resp.ok) throw new Error(`docs batchUpdate failed: ${resp.status} ${await resp.text()}`);
}

export async function updateWeeklyDoc(
  env: Env,
  kind: string,
  date: string,
  sectionMarkdown: string,
  sectionLabel?: string,
) {
  if (kind !== "brief" && kind !== "reflection" && kind !== "review") {
    return { file_id: null, updated: false, error: `kind must be 'brief', 'reflection', or 'review', got ${JSON.stringify(kind)}` };
  }
  sectionMarkdown = (sectionMarkdown || "").trim();
  if (!sectionMarkdown) return { file_id: null, updated: false, error: "empty section_markdown" };
  const [dateObj, dateErr] = parseIsoDate(date);
  if (dateErr || !dateObj) return { file_id: null, updated: false, error: dateErr };

  const folderId = resolveFolderId(env, kind);
  if (!folderId) {
    const envName = { brief: "BRAIN_BRIEFS_FOLDER_ID", reflection: "BRAIN_REFLECTIONS_FOLDER_ID", review: "BRAIN_REVIEWS_FOLDER_ID" }[kind];
    return { file_id: null, updated: false, error: `${envName} not set` };
  }

  const title = docTitleFor(kind, dateObj);
  const anchor =
    kind === "review"
      ? new Date(Date.UTC(dateObj.getUTCFullYear(), 3 * (quarterOf(dateObj) - 1), 1))
      : mondayOf(dateObj);

  let fileId = await driveFindDoc(env, folderId, title);
  let created = false;
  if (!fileId) {
    fileId = await driveCreateEmptyDoc(env, folderId, title);
    created = true;
  }

  if (!created) {
    const existing = await driveReadPlainText(env, fileId);
    if (sectionAlreadyInserted(existing, isoDate(dateObj), sectionLabel)) {
      return {
        file_id: fileId,
        doc_url: `https://docs.google.com/document/d/${fileId}/edit`,
        updated: false,
        created: false,
        duplicate: true,
        anchor_date: isoDate(anchor),
      };
    }
  }

  const headerLine = sectionHeader(isoDate(dateObj), sectionLabel);
  const { text: bodyMd, links: linkRanges } = await linkifyWikilinks(env, sectionMarkdown);
  const prependText = `${headerLine}\n${bodyMd}\n\n---\n\n`;
  const dateLineLen = headerLine.length;
  const bodyDocOffset = 1 + dateLineLen + 1;

  const requests: unknown[] = [
    { insertText: { location: { index: 1 }, text: prependText } },
    {
      updateParagraphStyle: {
        range: { startIndex: 1, endIndex: 1 + dateLineLen + 1 },
        paragraphStyle: { namedStyleType: "HEADING_1" },
        fields: "namedStyleType",
      },
    },
  ];
  for (const [linkStart, linkEnd, url] of linkRanges) {
    requests.push({
      updateTextStyle: {
        range: { startIndex: bodyDocOffset + linkStart, endIndex: bodyDocOffset + linkEnd },
        textStyle: { link: { url } },
        fields: "link",
      },
    });
  }
  await docsBatchUpdate(env, fileId, requests);

  return {
    file_id: fileId,
    doc_url: `https://docs.google.com/document/d/${fileId}/edit`,
    updated: true,
    created,
    duplicate: false,
    anchor_date: isoDate(anchor),
  };
}
