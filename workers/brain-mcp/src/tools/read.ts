// Read tools — port of mcp_server/tools/read.py. Same SQL, same shapes.
// BQ TIMESTAMP/DATE cells are already ISO strings out of queryRows(), so
// no per-field date formatting is needed here.

import { getConfig } from "../config";
import { queryRows, type QueryParam } from "../gcp/bq";
import { excludeHipaa, filterRecency } from "../lib/sql";
import { retrieve } from "../retriever";
import type { Env } from "../types";

export async function brainAsk(env: Env, query: string, maxResults = 8) {
  const chunks = await retrieve(env, query, maxResults);
  return { chunks, embedded: true };
}

export async function openRiskFlags(env: Env, segment?: string, minSeverity?: string) {
  const cfg = getConfig(env);
  const where = ["rf.resolved_at IS NULL", excludeHipaa("a")];
  const params: QueryParam[] = [];
  if (segment) {
    where.push("rf.segment = @segment");
    params.push({ name: "segment", type: "STRING", value: segment });
  }
  if (minSeverity) {
    const order: Record<string, number> = { low: 1, medium: 2, high: 3, critical: 4 };
    const floor = order[minSeverity.toLowerCase()];
    if (floor !== undefined) {
      const allowed = Object.keys(order).filter((k) => order[k] >= floor);
      where.push("LOWER(rf.severity) IN UNNEST(@severities)");
      params.push({ name: "severities", type: "ARRAY_STRING", value: allowed });
    }
  }
  const sql =
    `SELECT rf.flag_id, a.company_name AS account_name, rf.segment, rf.severity, ` +
    `rf.pattern_name, rf.flagged_at, rf.reasoning ` +
    `FROM \`${cfg.projectId}.${cfg.outputsDataset}.risk_flags\` AS rf ` +
    `JOIN \`${cfg.projectId}.airtable_replica.accounts\` AS a ` +
    `ON rf.account_id = a._airtable_record_id ` +
    `WHERE ${where.join(" AND ")} ORDER BY rf.flagged_at DESC LIMIT 50`;
  const rows = await queryRows(env, sql, params);
  return {
    flags: rows.map((r) => ({
      flag_id: r.flag_id ?? null,
      account_name: r.account_name ?? null,
      segment: r.segment ?? null,
      severity: r.severity ?? null,
      pattern_name: r.pattern_name ?? null,
      flagged_at: r.flagged_at ?? null,
      reason: r.reasoning ?? null,
    })),
  };
}

export async function clientSummary(env: Env, accountName: string) {
  const cfg = getConfig(env);
  const accounts = await queryRows(
    env,
    `SELECT _airtable_record_id AS airtable_id, company_name, segment, ` +
      `COALESCE(hipaa, FALSE) AS hipaa ` +
      `FROM \`${cfg.projectId}.airtable_replica.accounts\` ` +
      `WHERE LOWER(company_name) LIKE @pattern AND ${excludeHipaa()} LIMIT 5`,
    [{ name: "pattern", type: "STRING", value: `%${accountName.toLowerCase()}%` }],
  );
  if (!accounts.length) return { accounts: [] };

  const results = [];
  for (const acct of accounts) {
    const aid = acct.airtable_id as string;
    const company = String(acct.company_name ?? "");
    const pattern = `%${company.toLowerCase()}%`;
    const flags = await queryRows(
      env,
      `SELECT flag_id, severity, pattern_name, flagged_at, reasoning ` +
        `FROM \`${cfg.projectId}.${cfg.outputsDataset}.risk_flags\` ` +
        `WHERE account_id = @aid AND resolved_at IS NULL ORDER BY flagged_at DESC LIMIT 10`,
      [{ name: "aid", type: "STRING", value: aid }],
    );
    const triaged = await queryRows(
      env,
      `SELECT item_id, severity, category, triaged_at, reasoning ` +
        `FROM \`${cfg.projectId}.${cfg.outputsDataset}.triaged_items\` ` +
        `WHERE account_id = @aid AND ${filterRecency("triaged_at", 30)} ` +
        `ORDER BY triaged_at DESC LIMIT 10`,
      [{ name: "aid", type: "STRING", value: aid }],
    );
    const meetings = await queryRows(
      env,
      `SELECT note_id, filename, source_drive_url, created_at ` +
        `FROM \`${cfg.projectId}.${cfg.outputsDataset}.notes\` ` +
        `WHERE note_kind = 'calendar_event' AND LOWER(markdown_content) LIKE @pattern ` +
        `AND ${filterRecency("created_at", 30)} AND ${excludeHipaa("", "isolated")} ` +
        `ORDER BY created_at DESC LIMIT 10`,
      [{ name: "pattern", type: "STRING", value: pattern }],
    );
    const emails = await queryRows(
      env,
      `SELECT note_id, filename, source_drive_url, created_at ` +
        `FROM \`${cfg.projectId}.${cfg.outputsDataset}.notes\` ` +
        `WHERE note_kind = 'email' AND LOWER(markdown_content) LIKE @pattern ` +
        `AND ${filterRecency("created_at", 30)} AND ${excludeHipaa("", "isolated")} ` +
        `ORDER BY created_at DESC LIMIT 10`,
      [{ name: "pattern", type: "STRING", value: pattern }],
    );
    results.push({
      name: company,
      segment: acct.segment ?? null,
      hipaa: Boolean(acct.hipaa),
      open_risk_flags: flags.map((f) => ({
        flag_id: f.flag_id ?? null,
        severity: f.severity ?? null,
        pattern_name: f.pattern_name ?? null,
        flagged_at: f.flagged_at ?? null,
        reason: f.reasoning ?? null,
      })),
      recent_triaged_items: triaged.map((t) => ({
        item_id: t.item_id ?? null,
        severity: t.severity ?? null,
        category: t.category ?? null,
        triaged_at: t.triaged_at ?? null,
        reasoning: t.reasoning ?? null,
      })),
      recent_meetings: meetings.map((m) => ({
        note_id: m.note_id ?? null,
        filename: m.filename ?? null,
        source_url: m.source_drive_url ?? null,
        when: m.created_at ?? null,
      })),
      recent_emails: emails.map((e) => ({
        note_id: e.note_id ?? null,
        filename: e.filename ?? null,
        source_url: e.source_drive_url ?? null,
        when: e.created_at ?? null,
      })),
    });
  }
  return { accounts: results };
}

export async function getCalendarEvents(
  env: Env,
  startDate: string,
  endDate: string,
  scope?: string,
  includeAllDay = true,
) {
  const cfg = getConfig(env);
  const scopeNormalized = scope === "work" ? "agency" : (scope ?? null);
  const sql =
    `SELECT note_id, filename, source_drive_url, scope, ` +
    `event_metadata.start AS start_iso, event_metadata.end AS end_iso, ` +
    `event_metadata.location AS location, event_metadata.status AS status ` +
    `FROM \`${cfg.projectId}.${cfg.outputsDataset}.notes\` ` +
    `WHERE note_kind = 'calendar_event' AND ${excludeHipaa("", "isolated")} ` +
    `AND event_metadata.start IS NOT NULL ` +
    `AND event_metadata.start >= @start_date AND event_metadata.start < @end_date ` +
    `AND COALESCE(event_metadata.status, 'confirmed') != 'cancelled' ` +
    `AND (@scope IS NULL OR scope = @scope) ` +
    `AND (@include_all_day OR STRPOS(event_metadata.start, 'T') > 0) ` +
    `ORDER BY event_metadata.start ASC LIMIT 200`;
  const rows = await queryRows(env, sql, [
    { name: "start_date", type: "STRING", value: startDate },
    { name: "end_date", type: "STRING", value: endDate },
    { name: "scope", type: "STRING", value: scopeNormalized },
    { name: "include_all_day", type: "BOOL", value: Boolean(includeAllDay) },
  ]);
  const events = rows.map((r) => {
    const startIso = String(r.start_iso ?? "");
    return {
      note_id: r.note_id ?? null,
      title: r.filename ?? null,
      start: startIso,
      end: r.end_iso ?? null,
      location: r.location ?? null,
      all_day: !startIso.includes("T"),
      scope: r.scope ?? null,
      source_url: r.source_drive_url ?? null,
    };
  });
  return { events, total: events.length, date_range: { start: startDate, end: endDate } };
}

export async function relatedNotes(env: Env, noteId: string, linkTypes?: string[], limit = 20) {
  const cfg = getConfig(env);
  const safeLimit = Math.max(1, Math.min(100, Math.trunc(limit)));
  const where = ["nl.source_note_id = @src"];
  const params: QueryParam[] = [{ name: "src", type: "STRING", value: noteId }];
  if (linkTypes && linkTypes.length) {
    const normalized = linkTypes.map(String);
    if (normalized.includes("semantic") && !normalized.includes("NULL")) {
      where.push("(nl.link_type IN UNNEST(@link_types) OR nl.link_type IS NULL)");
    } else {
      where.push("nl.link_type IN UNNEST(@link_types)");
    }
    params.push({ name: "link_types", type: "ARRAY_STRING", value: normalized });
  }
  const sql =
    `SELECT nl.target_note_id, n.filename AS target_filename, ` +
    `n.source_drive_url AS target_url, nl.similarity, ` +
    `COALESCE(nl.link_type, 'semantic') AS link_type ` +
    `FROM \`${cfg.projectId}.${cfg.outputsDataset}.${cfg.linksTable}\` AS nl ` +
    `LEFT JOIN \`${cfg.projectId}.${cfg.outputsDataset}.${cfg.notesTable}\` AS n ` +
    `ON nl.target_note_id = n.note_id AND ${excludeHipaa("n", "isolated")} ` +
    `WHERE ${where.join(" AND ")} ORDER BY nl.similarity DESC, link_type LIMIT @limit`;
  params.push({ name: "limit", type: "INT64", value: safeLimit });
  const rows = await queryRows(env, sql, params);
  return {
    source_note_id: noteId,
    links: rows.map((r) => ({
      target_note_id: r.target_note_id ?? null,
      target_filename: r.target_filename ?? null,
      target_url: r.target_url ?? null,
      similarity: Number(r.similarity ?? 0),
      link_type: r.link_type ?? null,
    })),
    total: rows.length,
  };
}

export async function openDrafts(env: Env, limit = 20) {
  const cfg = getConfig(env);
  const safeLimit = Math.max(1, Math.min(100, Math.trunc(limit)));
  const sql =
    `SELECT _airtable_record_id AS task_id, task_name, category, action_type, task_type, ` +
    `owner, source, source_reference, due_date, _airtable_last_modified AS created ` +
    `FROM \`${cfg.projectId}.airtable_replica.tasks\` ` +
    `WHERE approval_status = 'Drafted by Agent' AND ${excludeHipaa()} ` +
    `ORDER BY _airtable_last_modified DESC LIMIT @limit`;
  const rows = await queryRows(env, sql, [{ name: "limit", type: "INT64", value: safeLimit }]);
  return {
    drafts: rows.map((r) => ({
      task_id: r.task_id ?? null,
      task_name: r.task_name ?? null,
      category: r.category ?? null,
      action_type: r.action_type ?? null,
      task_type: r.task_type ?? null,
      owner: r.owner ?? null,
      source: r.source ?? null,
      source_reference: r.source_reference ?? null,
      due_date: r.due_date ?? null,
      created: r.created ?? null,
    })),
    total: rows.length,
  };
}

export async function openCommitments(
  env: Env,
  direction?: string,
  accountName?: string,
  daysOverdue = 0,
) {
  const cfg = getConfig(env);
  const effDue = `COALESCE(c.due_date, DATE(c.extracted_at) + ${cfg.commitmentStaleDays})`;
  const where = [
    "c.status = 'open'",
    excludeHipaa("n", "isolated"),
    excludeHipaa("a"),
    `${effDue} <= DATE_SUB(CURRENT_DATE(), INTERVAL @days_overdue DAY)`,
  ];
  const params: QueryParam[] = [
    { name: "days_overdue", type: "INT64", value: Math.max(0, Math.trunc(daysOverdue)) },
  ];
  if (direction === "mine" || direction === "theirs") {
    where.push("c.direction = @direction");
    params.push({ name: "direction", type: "STRING", value: direction });
  }
  if (accountName) {
    where.push("LOWER(a.company_name) LIKE @acct");
    params.push({ name: "acct", type: "STRING", value: `%${accountName.toLowerCase()}%` });
  }
  const sql =
    `SELECT c.commitment_id, c.direction, ` +
    `COALESCE(c.counterparty_name, c.counterparty_email) AS counterparty, ` +
    `a.company_name AS account_name, c.commitment_text, c.due_date, ` +
    `${effDue} AS effective_due, DATE_DIFF(CURRENT_DATE(), ${effDue}, DAY) AS days_overdue, ` +
    `n.source_drive_url AS source_url, c.confidence ` +
    `FROM \`${cfg.projectId}.${cfg.outputsDataset}.commitments\` AS c ` +
    `JOIN \`${cfg.projectId}.${cfg.outputsDataset}.notes\` AS n ON c.source_note_id = n.note_id ` +
    `LEFT JOIN \`${cfg.projectId}.airtable_replica.accounts\` AS a ` +
    `ON c.account_id = a._airtable_record_id ` +
    `WHERE ${where.join(" AND ")} ORDER BY effective_due ASC LIMIT 50`;
  const rows = await queryRows(env, sql, params);
  return {
    commitments: rows.map((r) => ({
      commitment_id: r.commitment_id ?? null,
      direction: r.direction ?? null,
      counterparty: r.counterparty ?? null,
      account: r.account_name ?? null,
      what: r.commitment_text ?? null,
      due_date: r.due_date ?? null,
      effective_due: r.effective_due ?? null,
      days_overdue: r.days_overdue ?? null,
      source_url: r.source_url ?? null,
      confidence: r.confidence !== null && r.confidence !== undefined ? Number(r.confidence) : null,
    })),
  };
}
