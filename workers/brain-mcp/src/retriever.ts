// brain_ask retrieval — faithful port of
// agents/knowledge_surfacer/retriever.py (ADR 0046 + ADR 0068 hybrid).
//
// Hybrid (vector + keyword, RRF-fused) with recency as a third RRF
// channel. Degrades to keyword-only on embedding failure. The four
// load-bearing invariants — ARRAY_LENGTH(embedding)=768, hipaa_isolated,
// included note_kinds, COSINE — are carried by every arm (ADR 0045 §9).

import { getConfig, type Config } from "./config";
import { embed } from "./gcp/vertex";
import { queryRows, type QueryParam } from "./gcp/bq";
import type { Env } from "./types";

const DEFAULT_DIMS = 768;
const OVERSAMPLE_FACTOR = 3;
const EXCERPT_CHARS = 1500;

const INCLUDED_NOTE_KINDS = [
  "inbox",
  "area",
  "galaxy",
  "calendar_event",
  "capture",
  "decision",
  "win",
  "resource",
];

const MIN_TOKEN_CHARS = 2;
const STOPWORDS = new Set<string>([
  "a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "at", "by",
  "from", "as", "is", "are", "was", "were", "be", "been", "being", "do",
  "does", "did", "have", "has", "had", "will", "would", "can", "could",
  "should", "about", "what", "whats", "who", "whom", "whose", "when", "where",
  "why", "how", "which", "that", "this", "these", "those", "it", "its", "i",
  "me", "my", "we", "our", "you", "your", "he", "she", "they", "them", "their",
  "re", "vs", "tell", "find", "show", "say", "said", "know", "anything",
  "something",
]);
const TOKEN_RE = /[a-z0-9]+/g;

export interface Chunk {
  note_id: string;
  filename: string;
  source_url: string | null;
  scope: string;
  note_kind: string;
  excerpt: string;
  similarity: number;
  match_type: string;
  rrf_score: number | null;
}

export function tokenizeQuery(query: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  const matches = query.toLowerCase().match(TOKEN_RE) ?? [];
  for (const tok of matches) {
    if (tok.length < MIN_TOKEN_CHARS || STOPWORDS.has(tok) || seen.has(tok)) continue;
    seen.add(tok);
    out.push(tok);
  }
  return out;
}

// SEARCH()'s second arg must be a constant, so each term is its own bound
// scalar param @kw0, @kw1, … and the score counts matching terms.
function keywordScoreExpr(terms: string[]): { expr: string; params: QueryParam[] } {
  const parts: string[] = [];
  const params: QueryParam[] = [];
  terms.forEach((term, i) => {
    const name = `kw${i}`;
    parts.push(`CAST(SEARCH(markdown_content, @${name}) OR SEARCH(filename, @${name}) AS INT64)`);
    params.push({ name, type: "STRING", value: term });
  });
  return { expr: parts.length ? parts.join(" + ") : "0", params };
}

export async function retrieve(env: Env, query: string, maxResults: number): Promise<Chunk[]> {
  if (!query || !query.trim()) return [];
  const cfg = getConfig(env);
  const topK = Math.max(1, Math.min(50, maxResults));
  const notesRef = `${cfg.projectId}.${cfg.notesDataset}.${cfg.notesTable}`;

  let embedding: number[] = [];
  try {
    embedding = await embed(env, query);
  } catch {
    embedding = [];
  }
  const hasEmbedding = embedding.length === DEFAULT_DIMS;
  const queryTerms = cfg.hybridEnabled ? tokenizeQuery(query) : [];

  let rows: Record<string, unknown>[];
  if (cfg.hybridEnabled && queryTerms.length) {
    rows = hasEmbedding
      ? await hybridSearch(env, notesRef, topK, cfg, embedding, queryTerms)
      : await keywordOnlySearch(env, notesRef, topK, cfg, queryTerms);
  } else if (hasEmbedding) {
    rows = await vectorSearch(env, notesRef, topK, cfg, embedding);
  } else {
    return [];
  }
  return rows.map(rowToChunk);
}

async function vectorSearch(
  env: Env,
  notesRef: string,
  topK: number,
  cfg: Config,
  embedding: number[],
): Promise<Record<string, unknown>[]> {
  const maxDistance = Math.max(0, Math.min(2, 1 - cfg.cosineThreshold));
  const useRecency = cfg.halfLifeDays > 0;
  const oversampleTopK = useRecency ? topK * OVERSAMPLE_FACTOR : topK;
  const sql = useRecency
    ? `
WITH search AS (
  SELECT base.note_id, base.filename, base.source_drive_url, base.markdown_content,
         base.scope, base.note_kind, base.hipaa_isolated,
         TO_JSON_STRING(base.event_metadata) AS event_metadata_json,
         base.created_at, base.ingested_at, distance
  FROM VECTOR_SEARCH(
    (SELECT * FROM \`${notesRef}\`
     WHERE ARRAY_LENGTH(embedding) = 768 AND hipaa_isolated = FALSE
       AND note_kind IN UNNEST(@included_kinds)),
    'embedding', (SELECT @query_embedding AS embedding),
    top_k => @oversample_top_k, distance_type => 'COSINE')
),
scored AS (
  SELECT *,
    (1.0 - distance) * EXP(- TIMESTAMP_DIFF(CURRENT_TIMESTAMP(),
       COALESCE(created_at, ingested_at), DAY) / @half_life_days) AS recency_score
  FROM search WHERE distance <= @max_distance
)
SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
       hipaa_isolated, event_metadata_json, distance
FROM scored
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY recency_score DESC) = 1
ORDER BY recency_score DESC LIMIT @top_k`
    : `
WITH search AS (
  SELECT base.note_id, base.filename, base.source_drive_url, base.markdown_content,
         base.scope, base.note_kind, base.hipaa_isolated,
         TO_JSON_STRING(base.event_metadata) AS event_metadata_json, distance
  FROM VECTOR_SEARCH(
    (SELECT * FROM \`${notesRef}\`
     WHERE ARRAY_LENGTH(embedding) = 768 AND hipaa_isolated = FALSE
       AND note_kind IN UNNEST(@included_kinds)),
    'embedding', (SELECT @query_embedding AS embedding),
    top_k => @top_k, distance_type => 'COSINE')
)
SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
       hipaa_isolated, event_metadata_json, distance
FROM search WHERE distance <= @max_distance
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY distance ASC) = 1
ORDER BY distance ASC LIMIT @top_k`;

  const params: QueryParam[] = [
    { name: "query_embedding", type: "ARRAY_FLOAT64", value: embedding },
    { name: "top_k", type: "INT64", value: topK },
    { name: "max_distance", type: "FLOAT64", value: maxDistance },
    { name: "included_kinds", type: "ARRAY_STRING", value: INCLUDED_NOTE_KINDS },
  ];
  if (useRecency) {
    params.push({ name: "oversample_top_k", type: "INT64", value: oversampleTopK });
    params.push({ name: "half_life_days", type: "FLOAT64", value: cfg.halfLifeDays });
  }
  return safeQuery(env, sql, params);
}

async function hybridSearch(
  env: Env,
  notesRef: string,
  topK: number,
  cfg: Config,
  embedding: number[],
  queryTerms: string[],
): Promise<Record<string, unknown>[]> {
  const maxDistance = Math.max(0, Math.min(2, 1 - cfg.cosineThreshold));
  const useRecency = cfg.halfLifeDays > 0;
  const candK = topK * OVERSAMPLE_FACTOR;
  const finalScore = useRecency ? "base_rrf + 1.0 / (@rrf_k + rec_rank)" : "base_rrf";
  const { expr: kwExpr, params: termParams } = keywordScoreExpr(queryTerms);
  const sql = `
WITH vec AS (
  SELECT base.note_id, base.filename, base.source_drive_url, base.markdown_content,
         base.scope, base.note_kind, base.hipaa_isolated,
         TO_JSON_STRING(base.event_metadata) AS event_metadata_json,
         base.created_at, base.ingested_at, distance,
         ROW_NUMBER() OVER (ORDER BY distance ASC) AS vec_rank
  FROM VECTOR_SEARCH(
    (SELECT * FROM \`${notesRef}\`
     WHERE ARRAY_LENGTH(embedding) = 768 AND hipaa_isolated = FALSE
       AND note_kind IN UNNEST(@included_kinds)),
    'embedding', (SELECT @query_embedding AS embedding),
    top_k => @cand_k, distance_type => 'COSINE')
  WHERE distance <= @max_distance
),
kw AS (
  SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
         hipaa_isolated, event_metadata_json, created_at, ingested_at, kw_score,
         ROW_NUMBER() OVER (ORDER BY kw_score DESC, created_at DESC) AS kw_rank
  FROM (
    SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
           hipaa_isolated, TO_JSON_STRING(event_metadata) AS event_metadata_json,
           created_at, ingested_at, (${kwExpr}) AS kw_score
    FROM \`${notesRef}\`
    WHERE ARRAY_LENGTH(embedding) = 768 AND hipaa_isolated = FALSE
      AND note_kind IN UNNEST(@included_kinds)
  )
  WHERE kw_score > 0 ORDER BY kw_score DESC, created_at DESC LIMIT @cand_k
),
fused AS (
  SELECT
    COALESCE(vec.note_id, kw.note_id) AS note_id,
    COALESCE(vec.filename, kw.filename) AS filename,
    COALESCE(vec.source_drive_url, kw.source_drive_url) AS source_drive_url,
    COALESCE(vec.markdown_content, kw.markdown_content) AS markdown_content,
    COALESCE(vec.scope, kw.scope) AS scope,
    COALESCE(vec.note_kind, kw.note_kind) AS note_kind,
    COALESCE(vec.hipaa_isolated, kw.hipaa_isolated) AS hipaa_isolated,
    COALESCE(vec.event_metadata_json, kw.event_metadata_json) AS event_metadata_json,
    COALESCE(vec.created_at, kw.created_at) AS created_at,
    COALESCE(vec.ingested_at, kw.ingested_at) AS ingested_at,
    vec.distance AS distance,
    CASE
      WHEN vec.note_id IS NOT NULL AND kw.note_id IS NOT NULL THEN 'both'
      WHEN vec.note_id IS NOT NULL THEN 'semantic'
      ELSE 'keyword'
    END AS match_type,
    (COALESCE(1.0 / (@rrf_k + vec.vec_rank), 0.0)
     + COALESCE(1.0 / (@rrf_k + kw.kw_rank), 0.0)) AS base_rrf
  FROM vec FULL OUTER JOIN kw ON vec.note_id = kw.note_id
),
ranked AS (
  SELECT *, ROW_NUMBER() OVER (ORDER BY COALESCE(created_at, ingested_at) DESC) AS rec_rank
  FROM fused
)
SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
       hipaa_isolated, event_metadata_json, distance, match_type,
       (${finalScore}) AS rrf_score
FROM ranked
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY rrf_score DESC) = 1
ORDER BY rrf_score DESC LIMIT @top_k`;

  const params: QueryParam[] = [
    { name: "query_embedding", type: "ARRAY_FLOAT64", value: embedding },
    { name: "top_k", type: "INT64", value: topK },
    { name: "cand_k", type: "INT64", value: candK },
    { name: "max_distance", type: "FLOAT64", value: maxDistance },
    { name: "rrf_k", type: "INT64", value: cfg.rrfK },
    { name: "included_kinds", type: "ARRAY_STRING", value: INCLUDED_NOTE_KINDS },
    ...termParams,
  ];
  if (useRecency) {
    params.push({ name: "half_life_days", type: "FLOAT64", value: cfg.halfLifeDays });
  }
  return safeQuery(env, sql, params);
}

async function keywordOnlySearch(
  env: Env,
  notesRef: string,
  topK: number,
  cfg: Config,
  queryTerms: string[],
): Promise<Record<string, unknown>[]> {
  const useRecency = cfg.halfLifeDays > 0;
  const finalScore = useRecency
    ? "1.0 / (@rrf_k + kw_rank) + 1.0 / (@rrf_k + rec_rank)"
    : "1.0 / (@rrf_k + kw_rank)";
  const { expr: kwExpr, params: termParams } = keywordScoreExpr(queryTerms);
  const sql = `
WITH kw AS (
  SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
         hipaa_isolated, TO_JSON_STRING(event_metadata) AS event_metadata_json,
         created_at, ingested_at, (${kwExpr}) AS kw_score
  FROM \`${notesRef}\`
  WHERE ARRAY_LENGTH(embedding) = 768 AND hipaa_isolated = FALSE
    AND note_kind IN UNNEST(@included_kinds)
),
ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (ORDER BY kw_score DESC, created_at DESC) AS kw_rank,
    ROW_NUMBER() OVER (ORDER BY COALESCE(created_at, ingested_at) DESC) AS rec_rank
  FROM kw WHERE kw_score > 0
)
SELECT note_id, filename, source_drive_url, markdown_content, scope, note_kind,
       hipaa_isolated, event_metadata_json, CAST(NULL AS FLOAT64) AS distance,
       'keyword' AS match_type, (${finalScore}) AS rrf_score
FROM ranked
QUALIFY ROW_NUMBER() OVER (PARTITION BY note_id ORDER BY rrf_score DESC) = 1
ORDER BY rrf_score DESC LIMIT @top_k`;

  const params: QueryParam[] = [
    { name: "top_k", type: "INT64", value: topK },
    { name: "rrf_k", type: "INT64", value: cfg.rrfK },
    { name: "included_kinds", type: "ARRAY_STRING", value: INCLUDED_NOTE_KINDS },
    ...termParams,
  ];
  return safeQuery(env, sql, params);
}

async function safeQuery(
  env: Env,
  sql: string,
  params: QueryParam[],
): Promise<Record<string, unknown>[]> {
  try {
    return await queryRows(env, sql, params);
  } catch (e) {
    console.error("retriever query failed", e);
    return [];
  }
}

function rowToChunk(row: Record<string, unknown>): Chunk {
  let markdown = String(row.markdown_content ?? "");
  if (markdown.length > EXCERPT_CHARS) markdown = markdown.slice(0, EXCERPT_CHARS) + "…";
  const distance = row.distance;
  const rrf = row.rrf_score;
  const similarity = distance !== null && distance !== undefined ? 1 - Number(distance) : 0;
  return {
    note_id: String(row.note_id ?? ""),
    filename: String(row.filename ?? ""),
    source_url: (row.source_drive_url as string) || null,
    scope: String(row.scope ?? ""),
    note_kind: String(row.note_kind ?? ""),
    excerpt: markdown,
    similarity: round(similarity, 4),
    match_type: String(row.match_type ?? "semantic"),
    rrf_score: rrf !== null && rrf !== undefined ? round(Number(rrf), 6) : null,
  };
}

function round(n: number, places: number): number {
  const f = 10 ** places;
  return Math.round(n * f) / f;
}
