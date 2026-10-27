"""Reproducible hit@3 benchmark for the Wiki hybrid search ranking."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wiki_tools import wiki_search as search  # noqa: E402


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if len(cases) != 16:
        raise SystemExit(f"benchmark cases must contain 16 cases, got {len(cases)}")
    for case in cases:
        if not all(case.get(key) for key in ("id", "query", "expected")):
            raise SystemExit(f"invalid benchmark case: {case!r}")
        case["expected"] = case["expected"].removesuffix(".md")
        if not (search.ROOT / f"{case['expected']}.md").is_file():
            raise SystemExit(f"expected document not found in wiki-repo: {case['expected']}")
    return cases


def run(cases: list[dict], weight: float) -> dict:
    search.EMBED_WEIGHT = weight
    rows = []
    for case in cases:
        concepts, terms = search.analyze(case["query"])
        concepts, terms, fixes = search.fix_typos(concepts, terms)
        groups = search.build_groups(concepts, terms)
        query = search.corrected(case["query"], fixes)
        ranked = search.INDEX.search(groups, query, limit=20)
        paths = [doc.path for doc, _ in ranked]
        rank = paths.index(case["expected"]) + 1 if case["expected"] in paths else None
        rows.append({"id": case["id"], "query": case["query"], "expected": case["expected"],
                     "rank": rank, "hit_at_3": rank is not None and rank <= 3,
                     "top3": paths[:3]})
    hits = sum(row["hit_at_3"] for row in rows)
    return {"embed_weight": weight, "cases": len(rows), "hits_at_3": hits,
            "hit_at_3_percent": round(100 * hits / len(rows), 1), "results": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Wiki search hit@3 for embedding weights.")
    parser.add_argument("--cases", type=Path, default=ROOT / "test" / "wiki_search_benchmark_cases.json")
    parser.add_argument("--weights", type=float, nargs="+", default=[0.8, 8.0])
    parser.add_argument("--output", type=Path, help="optional JSON result path")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    search.INDEX = search.Index(search.ROOT)
    if not search.INDEX.embeddings.vectors:
        raise SystemExit("embeddings are unavailable; set Azure/OpenAI credentials and rerun")

    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "wiki_root": str(search.ROOT), "embedding_model": search.INDEX.embeddings.model,
              "document_count": len(search.INDEX.docs),
              "cases_file": str(args.cases), "runs": [run(cases, weight) for weight in args.weights]}
    for result in report["runs"]:
        print(f"weight={result['embed_weight']:g}: {result['hits_at_3']}/{result['cases']} "
              f"({result['hit_at_3_percent']:.1f}%)")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"result={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
