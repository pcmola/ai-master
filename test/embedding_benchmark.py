"""임베딩 모델 비교: 회사 허용 임베딩 모델마다 Wiki 문서 전체를 임베딩해 같은 16개 질의로 검색 정확도를 잰다.

    python test/embedding_benchmark.py                                   # 허용 모델 3개
    python test/embedding_benchmark.py --models text-embedding-3-small

- 질의와 정답 문서는 test/wiki_search_benchmark_cases.json(16개, wiki_search_benchmark.py 와 같음)을 쓴다.
- 운영 캐시(data/wiki_search_cache.json)는 모델 하나만 담으므로 건드리지 않는다. 모델마다 data/embed_bench/<모델>.json 을
  따로 쓴다(운영 모델과 같은 캐시가 있으면 복사해 다시 계산하지 않음). 운영 중인 Wiki 검색 서버에 영향이 없다.
- 재는 것
  - 혼합 검색(운영 방식: 키워드 + 임베딩 × EMBED_WEIGHT)의 hit@3·MRR. 임베딩 가중치 0 은 키워드만(임베딩 없이)의 기준선.
  - 임베딩만으로 순위를 매겼을 때 hit@1·hit@3·MRR. 모델 자체의 의미 검색 실력.
  - 웹 검색 판단 기준(WEB_SEARCH_MIN_SIM=0.5)에 쓰는 '위키 최고 유사도'가 위키에 있는 질의와 없는 질의에서 얼마나 갈리는지.
    기준값은 text-embedding-3-large 로 정했으므로, 모델을 바꾸면 이 값도 다시 정해야 하는지 본다.
- 결과: data/embed_bench/result-<시각>.json 과 화면 표.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

from wiki_tools import wiki_search as search  # noqa: E402
from wiki_search_benchmark import load_cases  # noqa: E402

MIN_SIM = float(__import__("os").environ.get("WEB_SEARCH_MIN_SIM", "0.5"))  # wiki_search 와 같은 기준
MODELS = ["text-embedding-3-large", "text-embedding-3-small", "text-embedding-ada-002"]
OUT = search.DATA_DIR / "embed_bench"
WEIGHTS = [0.0, 0.8, 2.0, 4.0, 8.0, 16.0]  # 0: 키워드만, 8: 운영 기본값(EMBED_WEIGHT). 모델마다 유사도 크기가 달라 맞는 가중치도 다르다
# 위키에 답이 없는 질의(웹 검색으로 가야 하는 것). 위키 최고 유사도가 이보다 위키 질의에서 충분히 높아야 기준값이 갈린다.
OFF_TOPIC = ["오늘 서울 날씨 알려줘", "삼성전자 주가 전망", "파이썬 리스트 정렬하는 방법", "SK이노베이션 본사 주소",
             "김치찌개 맛있게 끓이는 법", "아이폰 배터리 교체 비용"]


def ranks(cases, weight: float) -> list[int | None]:
    search.EMBED_WEIGHT = weight
    out = []
    for case in cases:
        concepts, terms = search.analyze(case["query"])
        concepts, terms, fixes = search.fix_typos(concepts, terms)
        groups = search.build_groups(concepts, terms)
        query = search.corrected(case["query"], fixes)
        paths = [d.path for d, _ in search.INDEX.search(groups, query, limit=20)]
        out.append(paths.index(case["expected"]) + 1 if case["expected"] in paths else None)
    return out


def embed_only_ranks(cases) -> list[int]:
    docs = search.INDEX.docs
    out = []
    for case in cases:
        sims = search.INDEX.embeddings.similarity(case["query"])
        order = sorted(range(len(docs)), key=lambda i: -sims[i])
        out.append(next(r for r, i in enumerate(order, 1) if docs[i].path == case["expected"]))
    return out


def best_sim(query: str) -> float:
    return max(search.INDEX.embeddings.similarity(query))


def stats(rs: list[int | None]) -> dict:
    n = len(rs)
    return {"hit1": sum(r == 1 for r in rs), "hit3": sum(r is not None and r <= 3 for r in rs), "n": n,
            "mrr": round(sum(1 / r for r in rs if r) / n, 3), "ranks": rs}


def run_model(model: str, cases) -> dict:
    import os
    os.environ["OPENAI_EMBED_MODEL"] = model
    OUT.mkdir(parents=True, exist_ok=True)
    cache = OUT / f"{model}.json"
    live = search.CACHE
    if not cache.is_file() and live.is_file():
        try:
            if json.loads(live.read_text(encoding="utf-8")).get("model") == model:
                shutil.copyfile(live, cache)  # 운영 캐시가 같은 모델이면 복사해서 다시 계산하지 않는다
        except ValueError:
            pass
    search.CACHE = cache  # 운영 캐시 파일 대신 모델별 파일을 쓰게 한다
    started = time.perf_counter()
    search.INDEX = search.Index(search.ROOT)
    build_s = time.perf_counter() - started
    emb = search.INDEX.embeddings
    if not emb.vectors:
        raise SystemExit(f"{model}: 임베딩을 만들지 못했습니다({emb.cache_status})")
    t0 = time.perf_counter()
    hybrid = {w: stats(ranks(cases, w)) for w in WEIGHTS}
    only = stats(embed_only_ranks(cases))
    q_ms = (time.perf_counter() - t0) * 1000 / (len(cases) * (len(WEIGHTS) + 1))
    on = [best_sim(c["query"]) for c in cases]
    off = [best_sim(q) for q in OFF_TOPIC]
    return {"model": model, "dims": len(emb.vectors[0]), "docs": len(search.INDEX.docs),
            "embedded_new": emb.cache_new, "index_build_s": round(build_s, 1), "query_ms": round(q_ms),
            "hybrid": {str(w): v for w, v in hybrid.items()}, "embed_only": only,
            "sim_wiki": {"min": round(min(on), 3), "mean": round(statistics.mean(on), 3)},
            "sim_off": {"max": round(max(off), 3), "mean": round(statistics.mean(off), 3)},
            "sim_gap": round(min(on) - max(off), 3),
            "web_threshold_ok": sum(s >= MIN_SIM for s in on) == len(on)
                                and all(s < MIN_SIM for s in off)}


def table(results: list[dict]) -> str:
    w = "8.0"
    rows = ["| 모델 | 차원 | 혼합 검색 hit@3 (가중치 8, 운영) | 혼합 MRR | 가중치를 맞춘 최고 hit@3 | 임베딩만 hit@1 | 임베딩만 hit@3 | 임베딩만 MRR "
            "| 위키 질의 최저 유사도 | 위키 밖 질의 최고 유사도 | 기준 0.5로 갈림 |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        h, e = r["hybrid"][w], r["embed_only"]
        best = max((v["hit3"], v["mrr"], k) for k, v in r["hybrid"].items() if k != "0.0")
        rows.append(f"| {r['model']} | {r['dims']} | {h['hit3']}/{h['n']} | {h['mrr']:.3f} "
                    f"| {best[0]}/{h['n']} (가중치 {float(best[2]):g}, MRR {best[1]:.3f}) | {e['hit1']}/{e['n']} "
                    f"| {e['hit3']}/{e['n']} | {e['mrr']:.3f} | {r['sim_wiki']['min']:.3f} | {r['sim_off']['max']:.3f} "
                    f"| {'예' if r['web_threshold_ok'] else '아니오'} |")
    base = results[0]["hybrid"]["0.0"]
    rows.append(f"\n키워드만(임베딩 없이): hit@3 {base['hit3']}/{base['n']}, MRR {base['mrr']:.3f}")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="임베딩 모델 비교")
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--cases", type=Path, default=ROOT / "test" / "wiki_search_benchmark_cases.json")
    args = ap.parse_args()
    cases = load_cases(args.cases)
    results = []
    for m in args.models:
        print(f"== {m}", flush=True)
        results.append(run_model(m, cases))
    out = OUT / f"result-{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps({"generated_at": datetime.now().isoformat(), "cases": len(cases),
                               "off_topic": OFF_TOPIC, "results": results}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(table(results))
    print(f"result={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
