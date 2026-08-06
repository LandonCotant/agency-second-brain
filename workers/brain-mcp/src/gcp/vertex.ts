// Vertex AI text embeddings (ADR 0067).
//
// REST equivalent of notes_ingestor/embedder.py:VertexEmbedder — the
// text-embedding-005 :predict endpoint, 768-dim output. Used by brain_ask.

import { getConfig } from "../config";
import type { Env } from "../types";
import { getAccessToken } from "./auth";

const MODEL = "text-embedding-005";

export async function embed(env: Env, text: string): Promise<number[]> {
  const cfg = getConfig(env);
  const token = await getAccessToken(env);
  const url =
    `https://${cfg.location}-aiplatform.googleapis.com/v1/projects/` +
    `${cfg.projectId}/locations/${cfg.location}/publishers/google/models/${MODEL}:predict`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ instances: [{ content: text }] }),
  });
  if (!resp.ok) {
    throw new Error(`vertex embed failed: ${resp.status} ${await resp.text()}`);
  }
  const data = (await resp.json()) as {
    predictions?: Array<{ embeddings?: { values?: number[] } }>;
  };
  return data.predictions?.[0]?.embeddings?.values ?? [];
}
