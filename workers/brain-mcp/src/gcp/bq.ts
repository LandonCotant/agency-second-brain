// BigQuery REST query layer (ADR 0067).
//
// queryRows() is the TS equivalent of mcp_server/clients.py:query_rows —
// same {name, type, value} parameter shape so the ported tool SQL stays
// byte-for-byte close to the Python. Runs jobs.query (synchronous) and
// coerces typed cells to JS-native values (TIMESTAMP/DATE already ISO).

import { getConfig } from "../config";
import type { Env } from "../types";
import { getAccessToken } from "./auth";

export interface QueryParam {
  name: string;
  type: string; // STRING | INT64 | FLOAT64 | BOOL | ARRAY_STRING | ARRAY_FLOAT64 | ...
  value: unknown;
}

interface BqField {
  name: string;
  type: string;
}
interface BqResponse {
  jobComplete: boolean;
  jobReference?: { projectId: string; jobId: string; location?: string };
  schema?: { fields: BqField[] };
  rows?: Array<{ f: Array<{ v: unknown }> }>;
  errors?: unknown;
}

export async function queryRows(
  env: Env,
  sql: string,
  parameters: QueryParam[] = [],
): Promise<Record<string, unknown>[]> {
  const cfg = getConfig(env);
  const token = await getAccessToken(env);
  const body = {
    query: sql,
    useLegacySql: false,
    parameterMode: "NAMED",
    queryParameters: parameters.map(toBqParam),
    timeoutMs: 30000,
    maxResults: 1000,
  };
  const resp = await fetch(
    `https://bigquery.googleapis.com/bigquery/v2/projects/${cfg.projectId}/queries`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (!resp.ok) {
    throw new Error(`bigquery query failed: ${resp.status} ${await resp.text()}`);
  }
  let data = (await resp.json()) as BqResponse;

  // Small corpus → queries complete within timeoutMs. If not, poll once.
  if (!data.jobComplete && data.jobReference) {
    data = await pollResults(env, token, cfg.projectId, data.jobReference);
  }
  return mapRows(data);
}

async function pollResults(
  env: Env,
  token: string,
  projectId: string,
  ref: { jobId: string; location?: string },
): Promise<BqResponse> {
  for (let attempt = 0; attempt < 10; attempt++) {
    const params = new URLSearchParams({ timeoutMs: "30000", maxResults: "1000" });
    if (ref.location) params.set("location", ref.location);
    const resp = await fetch(
      `https://bigquery.googleapis.com/bigquery/v2/projects/${projectId}/queries/${ref.jobId}?${params}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (!resp.ok) throw new Error(`bigquery getQueryResults failed: ${resp.status}`);
    const data = (await resp.json()) as BqResponse;
    if (data.jobComplete) return data;
  }
  throw new Error("bigquery query did not complete in time");
}

function toBqParam(p: QueryParam) {
  if (p.type.startsWith("ARRAY_")) {
    const inner = p.type.slice("ARRAY_".length);
    const arr = (p.value as unknown[]) ?? [];
    return {
      name: p.name,
      parameterType: { type: "ARRAY", arrayType: { type: inner } },
      parameterValue: { arrayValues: arr.map((v) => ({ value: scalar(v) })) },
    };
  }
  return {
    name: p.name,
    parameterType: { type: p.type },
    parameterValue: { value: scalar(p.value) },
  };
}

function scalar(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  return String(v);
}

function mapRows(data: BqResponse): Record<string, unknown>[] {
  if (!data.rows || !data.schema) return [];
  const fields = data.schema.fields;
  return data.rows.map((row) => {
    const obj: Record<string, unknown> = {};
    row.f.forEach((cell, i) => {
      obj[fields[i].name] = coerce(cell.v, fields[i].type);
    });
    return obj;
  });
}

function coerce(v: unknown, type: string): unknown {
  if (v === null || v === undefined) return null;
  switch (type) {
    case "INTEGER":
    case "INT64":
      return Number(v);
    case "FLOAT":
    case "FLOAT64":
    case "NUMERIC":
    case "BIGNUMERIC":
      return Number(v);
    case "BOOLEAN":
    case "BOOL":
      return v === "true" || v === true;
    case "TIMESTAMP":
      // BQ REST returns epoch seconds as a string (e.g. "1.715e9").
      return new Date(parseFloat(v as string) * 1000).toISOString();
    default:
      // STRING, DATE (already "YYYY-MM-DD"), DATETIME, etc.
      return v;
  }
}
