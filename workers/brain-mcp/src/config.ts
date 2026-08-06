// Resolved configuration — mirrors mcp_server/clients.py:get_config().
// Defaults match the Python server so behavior is identical.

import type { Env } from "./types";

export interface Config {
  projectId: string;
  location: string;
  notesDataset: string;
  notesTable: string;
  outputsDataset: string;
  cosineThreshold: number;
  topKDefault: number;
  halfLifeDays: number;
  hybridEnabled: boolean;
  rrfK: number;
  commitmentStaleDays: number;
  linksTable: string;
}

function num(v: string | undefined, fallback: number): number {
  const n = v === undefined ? NaN : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function getConfig(env: Env): Config {
  return {
    projectId: env.BRAIN_PROJECT_ID || "agency-brain-demo",
    location: env.BRAIN_VERTEX_LOCATION || "us-central1",
    notesDataset: "agent_outputs",
    notesTable: "notes",
    outputsDataset: env.BRAIN_OUTPUTS_DATASET || "agent_outputs",
    // Lower than the Surfacer's 0.50 module-default — the MCP path returns
    // chunks to Claude, which filters further during synthesis.
    cosineThreshold: num(undefined, 0.5),
    topKDefault: 8,
    // brain_ask reranks with a 30-day recency half-life (clients.py default).
    halfLifeDays: 30,
    // ADR 0068 — hybrid retrieval on by default.
    hybridEnabled: true,
    rrfK: 60,
    // ADR 0069 — must match the extractor's COMMITMENT_STALE_DAYS.
    commitmentStaleDays: 7,
    linksTable: "notes_links",
  };
}
