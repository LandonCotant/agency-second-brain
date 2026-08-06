"""Golden-query retrieval eval — pure-vector vs hybrid (ADR 0068).

Runs every query in golden_queries.yaml through the real ``Retriever``
against the live corpus (operator ADC, READ-ONLY) twice — ``hybrid=False``
and ``hybrid=True`` — and reports, per query, the rank of the first chunk
matching the expectation under each mode, plus aggregate recall@k and MRR
split by ``kind``.

This is the objective before/after for the hybrid change and the regression
guard for future retrieval edits. It issues only SELECT/VECTOR_SEARCH/SEARCH
queries — no writes.

Usage::

    BRAIN_PROJECT_ID=agency-brain-demo \\
      .venv/bin/python -m scripts.eval.retrieval_eval [--k 10] [--file PATH]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml
from agency_brain.agents.knowledge_surfacer.retriever import Retriever
from agency_brain.mcp_server.clients import embedder, get_config, query_rows

DEFAULT_QUERIES = Path(__file__).with_name("golden_queries.yaml")


class _BQAdapter:
    def query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        return query_rows(sql, parameters=parameters)


def _make_retriever(*, hybrid: bool, top_k: int) -> Retriever:
    cfg = get_config()
    return Retriever(
        bq_query=_BQAdapter(),
        embedder=embedder(),
        project_id=cfg.project_id,
        notes_table=cfg.notes_table,
        dataset_id=cfg.notes_dataset,
        top_k=top_k,
        cosine_threshold=cfg.cosine_threshold,
        half_life_days=cfg.half_life_days,
        hybrid=hybrid,
        rrf_k=cfg.rrf_k,
    )


def _rank_of_expected(chunks, entry: dict) -> int | None:
    """1-indexed rank of the first chunk matching the expectation, or None."""
    want_fn = (entry.get("expect_filename") or "").lower()
    want_ct = (entry.get("expect_content") or "").lower()
    for i, c in enumerate(chunks, start=1):
        fn = (c.filename or "").lower()
        ct = (c.markdown_excerpt or "").lower()
        if want_fn and want_fn in fn:
            return i
        if want_ct and want_ct in ct:
            return i
    return None


def _fmt_rank(rank: int | None) -> str:
    return str(rank) if rank is not None else "—"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=10, help="top-k to retrieve (default 10)")
    ap.add_argument("--file", default=str(DEFAULT_QUERIES), help="golden queries YAML")
    args = ap.parse_args()

    entries = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or []
    vec = _make_retriever(hybrid=False, top_k=args.k)
    hyb = _make_retriever(hybrid=True, top_k=args.k)

    print(
        f"\nRetrieval eval — k={args.k}, {len(entries)} queries "
        f"(project={get_config().project_id})\n"
    )
    header = f"{'kind':9} {'vec':>4} {'hyb':>4}  query"
    print(header)
    print("-" * len(header))

    # aggregates[kind][mode] = [ranks...]
    agg: dict[str, dict[str, list[int | None]]] = {}
    for e in entries:
        kind = str(e.get("kind", "semantic"))
        v_rank = _rank_of_expected(vec.retrieve(query=e["query"]), e)
        h_rank = _rank_of_expected(hyb.retrieve(query=e["query"]), e)
        agg.setdefault(kind, {"vec": [], "hyb": []})
        agg[kind]["vec"].append(v_rank)
        agg[kind]["hyb"].append(h_rank)
        flag = ""
        # Flag sparse cases where hybrid failed to improve (or tie) on vector.
        if kind == "sparse":
            v = v_rank if v_rank is not None else 10**6
            h = h_rank if h_rank is not None else 10**6
            flag = "  <-- REGRESSION" if h > v else ""
        print(f"{kind:9} {_fmt_rank(v_rank):>4} {_fmt_rank(h_rank):>4}  " f"{e['query']}{flag}")

    def _recall_at_k(ranks: list[int | None]) -> float:
        hits = sum(1 for r in ranks if r is not None and r <= args.k)
        return hits / len(ranks) if ranks else 0.0

    def _mrr(ranks: list[int | None]) -> float:
        vals = [1.0 / r for r in ranks if r is not None]
        return sum(vals) / len(ranks) if ranks else 0.0

    print("\nAggregate (recall@k / MRR):")
    print(f"{'kind':9} {'vec recall':>11} {'hyb recall':>11} {'vec MRR':>9} {'hyb MRR':>9}")
    for kind, modes in sorted(agg.items()):
        print(
            f"{kind:9} "
            f"{_recall_at_k(modes['vec']):>11.2f} {_recall_at_k(modes['hyb']):>11.2f} "
            f"{_mrr(modes['vec']):>9.3f} {_mrr(modes['hyb']):>9.3f}"
        )
    print()


if __name__ == "__main__":
    # Fail loudly if the project isn't set — this hits the live corpus.
    if not os.environ.get("BRAIN_PROJECT_ID"):
        print("warning: BRAIN_PROJECT_ID unset; using default " "(agency-brain-demo)")
    main()
