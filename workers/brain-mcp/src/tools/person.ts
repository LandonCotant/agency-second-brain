// Person tools — port of mcp_server/tools/person.py (ADR 0057 §6).
// sync_people fires asb-people-sync via the Cloud Run Jobs :run REST API
// (the Worker can't shell to gcloud); asb-mcp-sa holds run.invoker on it.

import { getConfig } from "../config";
import { getAccessToken } from "../gcp/auth";
import { queryRows } from "../gcp/bq";
import { excludeHipaa, filterRecency } from "../lib/sql";
import type { Env } from "../types";

const ALLOWED_REGIONS = new Set(["us-central1", "us-east1", "us-east4", "us-west1"]);

function strOrNull(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s || null;
}
function dateStrOrNull(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s ? s.slice(0, 10) : null;
}

export async function personSummary(env: Env, nameOrEmail: string) {
  const cfg = getConfig(env);
  const q = (nameOrEmail || "").trim();
  if (!q) return { found: false };

  const contacts = await queryRows(
    env,
    `SELECT c._airtable_record_id AS airtable_id, c.name, c.email, c.role, ` +
      `c.relationship_type, c.warmth, c.last_contact, c.next_followup, ` +
      `c.linkedin_url AS linkedin, c.phone, c.notes, ` +
      `c.account[SAFE_OFFSET(0)] AS primary_account_id ` +
      `FROM \`${cfg.projectId}.airtable_replica.contacts\` c ` +
      `WHERE ${excludeHipaa("c")} ` +
      `AND (LOWER(c.name) = LOWER(@query_str) OR LOWER(c.email) = LOWER(@query_str)) ` +
      `ORDER BY c._airtable_last_modified DESC NULLS LAST LIMIT 5`,
    [{ name: "query_str", type: "STRING", value: q }],
  );
  if (!contacts.length) return { found: false };

  const c = contacts[0];
  let organization: string | null = null;
  if (c.primary_account_id) {
    const rows = await queryRows(
      env,
      `SELECT a.company_name FROM \`${cfg.projectId}.airtable_replica.accounts\` a ` +
        `WHERE a._airtable_record_id = @account_id AND ${excludeHipaa("a")} LIMIT 1`,
      [{ name: "account_id", type: "STRING", value: String(c.primary_account_id) }],
    );
    if (rows.length) organization = strOrNull(rows[0].company_name);
  }

  const recentActivity: { date: string; line: string }[] = [];
  const email = c.email;
  if (email) {
    const rows = await queryRows(
      env,
      `WITH calendar_hits AS ( ` +
        `SELECT DATE(SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', n.event_metadata.start)) AS activity_date, ` +
        `CONCAT('meeting: ', SUBSTR(n.filename, 0, 80)) AS line ` +
        `FROM \`${cfg.projectId}.agent_outputs.notes\` n ` +
        `WHERE n.note_kind = 'calendar_event' AND ${filterRecency("n.created_at", 90)} ` +
        `AND @email != '' ` +
        `AND EXISTS (SELECT 1 FROM UNNEST(n.event_metadata.attendees) AS att WHERE LOWER(att) = LOWER(@email)) ` +
        `) SELECT * FROM calendar_hits ORDER BY activity_date DESC LIMIT 5`,
      [{ name: "email", type: "STRING", value: String(email) }],
    );
    for (const r of rows) {
      recentActivity.push({ date: String(r.activity_date ?? ""), line: String(r.line ?? "") });
    }
  }

  let openFollowupDue = false;
  const nextFollowup = dateStrOrNull(c.next_followup);
  if (nextFollowup) openFollowupDue = nextFollowup <= isoToday();

  return {
    found: true,
    name: strOrNull(c.name),
    email: strOrNull(c.email),
    role: strOrNull(c.role),
    organization,
    relationship_type: strOrNull(c.relationship_type),
    warmth: strOrNull(c.warmth),
    last_contact: dateStrOrNull(c.last_contact),
    next_followup: nextFollowup,
    linkedin: strOrNull(c.linkedin),
    phone: strOrNull(c.phone),
    notes: strOrNull(c.notes),
    open_followup_due: openFollowupDue,
    recent_activity: recentActivity,
  };
}

export async function pendingFollowups(env: Env, windowDays = 0, limit = 20) {
  const cfg = getConfig(env);
  const safeWindow = Math.max(0, Math.min(Math.trunc(windowDays), 60));
  const safeLimit = Math.max(1, Math.min(Math.trunc(limit), 100));
  const rows = await queryRows(
    env,
    `SELECT c._airtable_record_id AS contact_id, c.name, c.email, c.warmth, ` +
      `c.relationship_type, c.last_contact, c.next_followup, ` +
      `c.account[SAFE_OFFSET(0)] AS primary_account_id ` +
      `FROM \`${cfg.projectId}.airtable_replica.contacts\` c ` +
      `WHERE c.next_followup IS NOT NULL ` +
      `AND c.next_followup <= DATE_ADD(CURRENT_DATE(), INTERVAL @window_days DAY) ` +
      `AND ${excludeHipaa("c")} ORDER BY c.next_followup ASC LIMIT @limit`,
    [
      { name: "window_days", type: "INT64", value: safeWindow },
      { name: "limit", type: "INT64", value: safeLimit },
    ],
  );
  return {
    contacts: rows.map((r) => ({
      contact_id: r.contact_id ?? null,
      name: r.name ?? null,
      email: r.email ?? null,
      warmth: r.warmth ?? null,
      relationship_type: r.relationship_type ?? null,
      last_contact: dateStrOrNull(r.last_contact),
      next_followup: dateStrOrNull(r.next_followup),
      primary_account_id: r.primary_account_id ?? null,
    })),
    total: rows.length,
    window_days: safeWindow,
  };
}

export async function syncPeople(env: Env, region = "us-central1") {
  if (!ALLOWED_REGIONS.has(region)) {
    return { succeeded: false, execution_name: null, error: `invalid region ${JSON.stringify(region)}` };
  }
  const cfg = getConfig(env);
  const token = await getAccessToken(env);
  const url =
    `https://run.googleapis.com/v2/projects/${cfg.projectId}/locations/${region}/jobs/asb-people-sync:run`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: "{}",
  });
  if (!resp.ok) {
    return { succeeded: false, execution_name: null, error: `run failed: ${resp.status} ${await resp.text()}` };
  }
  const data = (await resp.json()) as { name?: string; metadata?: { name?: string } };
  // The :run API is async — it returns a long-running operation, not a
  // finished execution. We report the operation name; the job runs in the
  // background (otherwise weekly Sunday tick).
  return { succeeded: true, execution_name: data.name ?? data.metadata?.name ?? null, async: true };
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10);
}
