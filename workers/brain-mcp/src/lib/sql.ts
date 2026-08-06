// Shared SQL fragments — port of common/bq_helpers.py. HIPAA exclusion
// MUST stay identical across every callsite (a missed filter is a
// compliance breach), hence the centralization.

const HIPAA_COLUMNS = { excluded: "hipaa_excluded", isolated: "hipaa_isolated" } as const;

export function excludeHipaa(alias = "", kind: keyof typeof HIPAA_COLUMNS = "excluded"): string {
  const col = HIPAA_COLUMNS[kind];
  const prefix = alias ? `${alias}.` : "";
  return `COALESCE(${prefix}${col}, FALSE) = FALSE`;
}

export function filterRecency(column: string, days: number): string {
  return `${column} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${days} DAY)`;
}
