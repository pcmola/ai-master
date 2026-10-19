"""Local semantic search and guide generator for wiki-repo (read-only).

- 개념 사전(동의어/한영 표기) + 문자 bigram TF-IDF 로 의미 기반 검색을 한다.
- 모호한 개념(예: 프록시)은 포워드/리버스 중 무엇인지 되묻는다.
- 선택 결과로 재검색한 문서를 조합해 Markdown 가이드를 만든다.
- 가이드 작성 LLM: ANTHROPIC_API_KEY 가 있으면 Claude, 없으면 OpenAI 호환 클라이언트를 쓴다.
- OpenAI 호환 클라이언트: AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY 면 사내 Azure OpenAI 게이트웨이,
  OPENAI_API_KEY 면 OpenAI. 이 클라이언트가 있으면 임베딩 재순위화도 사용한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE, WIKI_REPO  # noqa: E402
from common.server import local_server, open_chrome  # noqa: E402,F401  (다른 도구가 여기서 가져다 쓰던 이름 유지)

ROOT = WIKI_REPO.resolve()
CACHE = DATA_DIR / "wiki_search_cache.json"
EMBED_WEIGHT = float(os.environ.get("EMBED_WEIGHT", "8"))  # 혼합 검색에서 임베딩 유사도 가중치
OPENAI_TIMEOUT = 120  # 초. 게이트웨이가 응답하지 않으면 오래 매달리지 않고 오프라인 검색으로 넘어간다.
VIEWER = os.environ.get("WIKI_VIEWER_URL", "http://127.0.0.1:8765")

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass


# 개념 → 동의어(소문자). 질의와 문서 모두 같은 개념으로 묶어 표기 차이를 흡수한다.
CONCEPTS = {
    "proxy": ["프록시", "proxy", "프락시"],
    "guide": ["가이드", "guide", "안내", "매뉴얼", "manual"],
    "setting": ["설정", "구성", "config", "세팅"],
    "install": ["설치", "install", "도입"],
    "request": ["신청", "request"],
    "permission": ["권한", "승인", "permission"],
    "check": ["점검", "검토", "check"],
    "incident": ["장애", "오류", "에러", "incident", "error"],
    "security": ["보안", "security", "정책"],
    "log": ["로그", "log"],
    "deploy": ["배포", "deploy", "릴리스", "release"],
    "test": ["테스트", "test"],
    "meeting": ["회의록", "회의", "meeting"],
}

# 모호한 개념별 되묻기 정의. detect: 질의에 있으면 묻지 않음, terms: 재검색 가중 키워드.
CLARIFY = {
    "proxy": {
        "question": "어떤 프록시에 대한 가이드가 필요하신가요?",
        "options": {
            "forward": {
                "label": "포워드 프록시",
                "desc": "내부 사용자·서버가 외부로 나갈 때 경유하는 프록시 (아웃바운드)",
                "detect": ["포워드", "forward", "아웃바운드", "outbound"],
                "terms": ["접근 권한", "신청", "설치", "예외 승인", "보안 정책", "사용자", "외부",
                          "허용", "차단", "로그", "미상 요청", "사전 점검"],
                "intro": "포워드 프록시는 내부 사용자나 서버가 외부 자원에 접근할 때 경유하는 중계 서버입니다. "
                         "접근 권한 신청, 설치 조건, 보안 정책과 예외 승인, 접근 로그 점검이 핵심입니다.",
                "order": ["신청·권한", "설치·도입", "보안 정책", "설정·운영 기준", "점검·모니터링", "장애 대응"],
            },
            "reverse": {
                "label": "리버스 프록시",
                "desc": "외부 요청을 받아 내부 서비스로 전달하는 프록시 (인바운드)",
                "detect": ["리버스", "reverse", "인바운드", "inbound"],
                "terms": ["장애 대응", "운영 가이드", "설정 기준", "배포", "서비스", "라우팅", "부하",
                          "인증서", "ssl", "tls", "가용성", "트래픽", "후속 조치"],
                "intro": "리버스 프록시는 외부 요청을 받아 내부 서비스로 전달하는 진입점입니다. "
                         "설정·운영 기준, 배포 시 반영, 장애 대응 절차와 운영 점검이 핵심입니다.",
                "order": ["설정·운영 기준", "장애 대응", "점검·모니터링", "보안 정책", "설치·도입", "신청·권한"],
            },
        },
    },
}

# 가이드 본문에서 문장을 분류할 범주. 앞에서부터 처음 맞는 범주를 쓴다.
CATEGORIES = [
    ("장애 대응", ["장애", "후속 조치", "복구"]),
    ("신청·권한", ["신청", "권한", "미상 요청"]),
    ("설치·도입", ["설치", "도입"]),
    ("보안 정책", ["보안", "예외 승인", "정책"]),
    ("설정·운영 기준", ["설정", "운영", "가이드", "기준"]),
    ("점검·모니터링", ["점검", "로그", "확인", "모니터링"]),
]

STOPWORDS = {"알려줘", "알려주세요", "알려", "해줘", "주세요", "대해", "대해서", "관련", "관련된", "방법",
             "뭐야", "어떻게", "문서", "찾아줘", "찾아", "보여줘", "필요해", "싶어"}
# Azure DevOps 위키의 mermaid 블록(::: mermaid ... :::). 가이드에 원문 그대로 옮기기 위해 쓴다.
MERMAID = re.compile(r"^:::[ \t]*mermaid[ \t]*\n.*?^:::[ \t]*$", re.M | re.S)
# LLM 응답의 자리표시자. 목록·백틱으로 감싸 한 줄에 적었으면 줄 전체를, 문장 중간이면 토큰만 바꾼다.
DIAGRAM_TOKEN = re.compile(r"^[ \t]*(?:[-*][ \t]+)?`?\[\[DIAGRAM-(\d+)\]\]`?[ \t]*$|\[\[DIAGRAM-(\d+)\]\]", re.M)
JOSA = re.compile(r"(으로|에서|에게|까지|부터|이랑|하고|을|를|이|가|은|는|의|에|와|과|도|로|만)$")


@dataclass
class Doc:
    path: str          # wiki-repo 기준 상대경로, .md 제외 (wiki_viewer 의 path 와 동일)
    title: str
    text: str
    low: str
    length: int
    grams: dict
    norm: float = 1.0


def bigrams(text: str) -> Counter:
    grams = Counter()
    for word in re.findall(r"[0-9a-z가-힣]+", text.lower()):
        if len(word) == 1:
            grams[word] += 1
        grams.update(word[i:i + 2] for i in range(len(word) - 1))
    return grams


def analyze(query: str) -> tuple[list[str], list[str]]:
    """질의를 개념 목록과 사전에 없는 나머지 용어로 나눈다."""
    low = query.lower()
    concepts = [c for c, syns in CONCEPTS.items() if any(s in low for s in syns)]
    rest = low
    for c in concepts:
        for s in sorted(CONCEPTS[c], key=len, reverse=True):
            rest = rest.replace(s, " ")
    for spec in CLARIFY.values():
        for opt in spec["options"].values():
            for s in opt["detect"]:
                rest = rest.replace(s, " ")
    terms = []
    for word in re.findall(r"[0-9a-z가-힣]+", rest):
        if len(word) > 2:
            word = JOSA.sub("", word)
        if len(word) >= 2 and word not in STOPWORDS and word not in terms:
            terms.append(word)
    return concepts, terms


def detect_variant(query: str, concepts: list[str]) -> tuple[str | None, str | None]:
    low = query.lower()
    for key in concepts:
        if key in CLARIFY:
            for opt_id, opt in CLARIFY[key]["options"].items():
                if any(s in low for s in opt["detect"]):
                    return key, opt_id
            return key, None
    return None, None


class Index:
    def __init__(self, root: Path):
        self.docs: list[Doc] = []
        for file in sorted(root.rglob("*.md")):
            rel = file.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            text = file.read_text(encoding="utf-8-sig", errors="replace")
            m = re.match(r"\s*#\s+(.+)", text)
            title = m.group(1).strip() if m else file.stem.replace("-", " ")
            path = rel.as_posix()[:-3]
            low = (path.replace("-", " ") + "\n" + text).lower()
            self.docs.append(Doc(path, title, text, low, max(len(text), 1), bigrams(title + "\n" + text)))
        n = len(self.docs)
        df = Counter()
        for d in self.docs:
            df.update(d.grams.keys())
        self.idf = {g: math.log((n + 1) / (c + 1)) + 1 for g, c in df.items()}
        for d in self.docs:
            d.grams = {g: tf * self.idf[g] for g, tf in d.grams.items()}
            d.norm = math.sqrt(sum(w * w for w in d.grams.values())) or 1.0
        self.avgdl = sum(d.length for d in self.docs) / max(n, 1)
        self.embeddings = Embeddings(self.docs)

    def search(self, groups: list[list[str]], query: str, boost: list[str] = (), limit: int = 20):
        n = len(self.docs)
        scores = [0.0] * n
        # BM25: 동의어 묶음 하나를 한 용어로 취급한다.
        for group in groups:
            counts = [sum(d.low.count(s) for s in group) for d in self.docs]
            df = sum(1 for c in counts if c)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(counts):
                if tf:
                    d = self.docs[i]
                    title_hit = 1.0 if any(s in d.title.lower() for s in group) else 0.0
                    scores[i] += idf * (tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * d.length / self.avgdl)) + title_hit)
        # 문자 bigram 코사인: 사전에 없는 표현의 부분 일치를 보완한다.
        q = bigrams(query)
        qv = {g: tf * self.idf.get(g, 0.0) for g, tf in q.items()}
        qn = math.sqrt(sum(w * w for w in qv.values())) or 1.0
        top = max(scores) or 1.0
        for i, d in enumerate(self.docs):
            cos = sum(w * d.grams.get(g, 0.0) for g, w in qv.items()) / (qn * d.norm)
            scores[i] = scores[i] / top + 0.5 * cos
            if boost:
                scores[i] += 0.15 * sum(math.log1p(d.low.count(b)) for b in boost)
        sims = self.embeddings.similarity(query)
        if sims:
            # 2026-10-19 정답 16문항 비교: text-embedding-3-large + 가중치 8 이 hit@3 87.5%, 최저 7위로 가장 좋았다
            # (기존 0.8 은 68.8%, 최저 23위). 키워드 점수가 의미 점수를 덮지 않도록 크게 둔다.
            scores = [s + EMBED_WEIGHT * max(sim, 0.0) for s, sim in zip(scores, sims)]
        ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)
        return [(self.docs[i], scores[i]) for i in ranked[:limit] if scores[i] > 0.05]


class Embeddings:
    """OPENAI_API_KEY 가 있을 때만 동작하는 선택적 임베딩 재순위화."""

    def __init__(self, docs: list[Doc]):
        self.client = openai_client()
        self.model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.vectors: list[list[float]] = []
        if not self.client:
            return
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else {}
            if cache.get("model") != self.model:
                cache = {"model": self.model, "vectors": {}}
            keys = [hashlib.sha1((d.title + d.text).encode("utf-8")).hexdigest() for d in docs]
            missing = [(k, d) for k, d in zip(keys, docs) if k not in cache["vectors"]]
            print(f"Embeddings: 캐시 {len(docs) - len(missing)}건, 새로 계산 {len(missing)}건", flush=True)
            for start in range(0, len(missing), 100):
                batch = missing[start:start + 100]
                res = self.client.embeddings.create(model=self.model,
                                                    input=[(d.title + "\n" + d.text)[:4000] for _, d in batch])
                for (k, _), item in zip(batch, res.data):
                    cache["vectors"][k] = item.embedding
                # 중간에 실패해도 받은 만큼은 다음 실행에 재사용하도록 배치마다 저장한다.
                CACHE.write_text(json.dumps(cache), encoding="utf-8")
                print(f"  embedding {start + len(batch)}/{len(missing)}", flush=True)
            self.vectors = [cache["vectors"][k] for k in keys]
        except Exception as exc:  # 네트워크·키 오류 시 오프라인 검색만 사용
            print(f"[embeddings disabled] {exc}")
            self.vectors = []

    def similarity(self, query: str) -> list[float]:
        if not self.vectors:
            return []
        try:
            q = self.client.embeddings.create(model=self.model, input=[query]).data[0].embedding
        except Exception as exc:
            print(f"[embeddings query failed] {exc}")
            return []
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        return [sum(a * b for a, b in zip(q, v)) / (qn * (math.sqrt(sum(x * x for x in v)) or 1.0))
                for v in self.vectors]


def openai_client():
    """AZURE_OPENAI_ENDPOINT/AZURE_OPENAI_API_KEY 가 있으면 사내 Azure OpenAI 게이트웨이, 아니면 OpenAI 를 쓴다."""
    try:
        if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
            from openai import AzureOpenAI
            return AzureOpenAI(api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"),
                               timeout=OPENAI_TIMEOUT, max_retries=1)
        if os.environ.get("OPENAI_API_KEY"):
            from openai import OpenAI
            return OpenAI(timeout=OPENAI_TIMEOUT, max_retries=1)
    except Exception as exc:
        print(f"[openai disabled] {exc}")
    return None


def claude_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception as exc:
        print(f"[claude disabled] {exc}")
        return None


def view_link(path: str) -> str:
    return f"{VIEWER}/?path={quote(path)}"


def group_weights(groups: list[list[str]]) -> list[tuple[list[str], float]]:
    """동의어 묶음별 IDF. 드문 개념(예: 프록시)이 들어간 문장을 먼저 고르기 위해 쓴다."""
    n = len(INDEX.docs)
    weights = []
    for g in groups:
        df = sum(1 for d in INDEX.docs if any(s in d.low for s in g))
        weights.append((g, math.log(1 + (n - df + 0.5) / (df + 0.5)) if df else 0.0))
    return weights


def matching_lines(doc: Doc, weighted: list[tuple[list[str], float]], limit: int = 3) -> list[str]:
    scored = []
    for line in doc.text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        low = s.lower()
        score = sum(w for g, w in weighted if any(x in low for x in g))
        if score > 0:
            s = re.sub(r"^[-*]\s+", "", s)
            if all(s != t for _, t in scored):
                scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


def build_groups(concepts: list[str], terms: list[str]) -> list[list[str]]:
    return [CONCEPTS[c] for c in concepts] + [[t] for t in terms]


def do_search(query: str) -> dict:
    concepts, terms = analyze(query)
    groups = build_groups(concepts, terms)
    highlights = sorted({w for g in groups for w in g}, key=len, reverse=True)
    key, variant = detect_variant(query, concepts)
    results = INDEX.search(groups, query)
    clarify = None
    if key and not variant:
        spec = CLARIFY[key]
        clarify = {"key": key, "question": spec["question"],
                   "options": [{"id": i, "label": o["label"], "desc": o["desc"]} for i, o in spec["options"].items()]}
    return {
        "analysis": {"concepts": [{"id": c, "words": CONCEPTS[c]} for c in concepts], "terms": terms},
        "highlights": highlights,
        "clarify": clarify,
        "variant": {"key": key, "id": variant, "label": CLARIFY[key]["options"][variant]["label"]} if variant else None,
        "results": [{"path": d.path, "title": d.title, "score": round(s, 3), "link": view_link(d.path),
                     "snippets": matching_lines(d, group_weights(groups))} for d, s in results],
    }


def categorize(sentence: str) -> str:
    low = sentence.lower()
    for name, words in CATEGORIES:
        if any(w in low for w in words):
            return name
    return "기타"


def do_guide(query: str, key: str | None, variant: str | None) -> dict:
    concepts, terms = analyze(query)
    groups = build_groups(concepts, terms)
    option = CLARIFY[key]["options"][variant] if key in CLARIFY and variant in CLARIFY[key]["options"] else None
    boost = option["terms"] if option else []
    search_query = f"{query} {option['label']} {' '.join(boost)}" if option else query
    ranked = INDEX.search(groups, search_query, boost)

    # 핵심 개념(가장 드문 동의어 묶음)이 실제로 나오는 문서만 가이드 재료로 쓴다.
    core = min(groups, key=lambda g: sum(1 for d in INDEX.docs if any(s in d.low for s in g))) if groups else []
    sources = [(d, s) for d, s in ranked if not core or any(w in d.low for w in core)][:15]

    statements: dict[str, list[Doc]] = {}
    for d, _ in sources:
        for line in matching_lines(d, [(core, 1.0)] if core else group_weights(groups), limit=5):
            statements.setdefault(line, []).append(d)

    title = f"{option['label'] if option else query.strip()} 가이드"
    mode = "template"
    markdown = None
    client = claude_client() or openai_client()
    if client and statements:
        markdown = llm_guide(client, query, title, option, statements, sources)
        mode = "llm" if markdown else "template"
    if not markdown:
        markdown = template_guide(query, title, option, statements, sources)
    highlights = sorted({w for g in groups for w in g} | set(boost), key=len, reverse=True)
    weighted = group_weights(groups) + [([b], 0.3) for b in boost]
    return {"title": title, "mode": mode, "markdown": markdown,
            "analysis": {"concepts": [{"id": c, "words": CONCEPTS[c]} for c in concepts], "terms": terms},
            "highlights": highlights,
            "results": [{"path": d.path, "title": d.title, "link": view_link(d.path), "score": round(s, 3),
                         "snippets": matching_lines(d, weighted)} for d, s in sources]}


def cite(docs: list[Doc]) -> str:
    links = ", ".join(f"[{d.title}]({view_link(d.path)})" for d in docs[:2])
    return links + (f" 외 {len(docs) - 2}건" if len(docs) > 2 else "")


def template_guide(query, title, option, statements, sources) -> str:
    out = [f"# {title}", "",
           f"> wiki-repo 문서 {len(sources)}건을 조합해 자동 생성했습니다. 질의: \"{query.strip()}\"", ""]
    if not statements:
        out += ["관련 문서를 찾지 못했습니다. 다른 표현으로 다시 검색해 보세요."]
        return "\n".join(out)

    out += ["## 1. 개요", ""]
    if option:
        out.append(option["intro"])
        if not any(any(s in d.low for s in option["detect"]) for d, _ in sources):
            out += ["", f"※ 위키 문서에 '{option['label']}' 구분 표기가 없어, 관련 키워드 기준으로 문서 우선순위를 정했습니다."]
    else:
        out.append(f"\"{query.strip()}\"와 관련된 위키 문서의 업무 문구를 단계별로 정리했습니다.")
    out.append("")

    by_cat: dict[str, list[tuple[str, list[Doc]]]] = {}
    for sentence, docs in statements.items():
        by_cat.setdefault(categorize(sentence), []).append((sentence, docs))
    order = (option["order"] if option else [c for c, _ in CATEGORIES]) + ["기타"]
    cats = [c for c in order if c in by_cat]

    out += ["## 2. 단계별 가이드", ""]
    for n, cat in enumerate(cats, 1):
        out += [f"### 2.{n} {cat}", ""]
        for sentence, docs in sorted(by_cat[cat], key=lambda x: -len(x[1])):
            out.append(f"- {sentence}  ")
            out.append(f"  출처: {cite(docs)}")
        out.append("")

    out += ["## 3. 체크리스트", ""]
    for cat in cats:
        sentence = max(by_cat[cat], key=lambda x: len(x[1]))[0]
        out.append(f"- [ ] **{cat}**: {sentence}")
    out.append("")

    out += ["## 4. 참고 문서", "", "| # | 문서 | 경로 | 관련도 |", "|---|---|---|---|"]
    for i, (d, s) in enumerate(sources, 1):
        out.append(f"| {i} | [{d.title}]({view_link(d.path)}) | {d.path} | {s:.2f} |")
    return "\n".join(out)


def llm_guide(client, query, title, option, statements, sources) -> str | None:
    # mermaid 다이어그램은 LLM 이 글로 풀어 쓰거나 고치지 않도록 자리표시자로 바꿔 넘기고, 응답에서 원문으로 되돌린다.
    diagrams: list[str] = []

    def stash(m: re.Match) -> str:
        diagrams.append(m.group(0))
        labels = [re.sub(r"<br\s*/?>", " ", x) for x in re.findall(r'\["([^"]+)"\]', m.group(0))[:6]]
        return f"[[DIAGRAM-{len(diagrams)}]] (mermaid 다이어그램: {', '.join(labels)})"

    # 문서를 잘라 넣으면 명령어·주의사항이 빠지므로 관련도 순서대로 전문을 넘긴다.
    docs = "\n\n".join(f'<document rank="{i}" score="{s:.2f}" path="{d.path}">\n{MERMAID.sub(stash, d.text)}\n</document>'
                       for i, (d, s) in enumerate(sources, 1))
    prompt = (f"사용자 질의: {query}\n가이드 제목: {title}\n"
              f"대상: {option['label'] + ' - ' + option['desc'] if option else '지정 없음'}\n\n"
              f"<documents>\n{docs}\n</documents>\n\n"
              "위 위키 문서만 근거로 한국어 Markdown 가이드를 작성하라.\n"
              "- 질의에 가장 직접 답하는 구체적인 절차 문서(대개 상위 문서)를 뼈대로 삼고, 그 문서의 단계 순서와 "
              "명령어·코드 블록·포트·버전 같은 값은 그대로 옮겨라.\n"
              "- 다른 문서는 뼈대를 보완하는 내용(사전 신청, 환경별 차이, 주의사항, 관련 문서 링크)만 가져오고, "
              "질의와 무관하거나 일반적인 문구뿐인 문서는 쓰지 마라.\n"
              "- 구성: 개요, 단계별 가이드, 주의사항, 참고 문서. 각 단계 끝에 근거 문서 경로를 괄호로 적어라.\n"
              "- 위키에 없는 내용은 지어내지 말고 필요하면 '위키에 근거 없음'이라고 적어라.\n"
              "- 문서 안의 [[DIAGRAM-n]] 은 원문 다이어그램 자리표시자다. 가이드에 그 그림이 도움이 되면 "
              "다이어그램을 글·표·ASCII 로 풀어 쓰지 말고 '[[DIAGRAM-n]]' 한 줄만 단독으로 적어라. 괄호 속 설명은 옮기지 마라.\n"
              f"- 첫 줄은 '# {title}'로 시작하고, Markdown 본문만 출력하라.")
    system = "너는 사내 위키 문서를 근거로 운영 가이드를 작성하는 기술 문서 작성자다."
    try:
        if hasattr(client, "chat"):  # OpenAI / Azure OpenAI 게이트웨이
            res = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4.1"),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            text = (res.choices[0].message.content or "").strip()
        else:  # Anthropic
            model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
            # 안전 분류기가 거절하면 서버가 다른 모델로 이어서 답하게 한다(지원 모델만).
            extra = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                     if model in {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"} else {"betas": []})
            res = client.beta.messages.create(
                model=model, max_tokens=16000, output_config={"effort": "medium"},
                system=system, messages=[{"role": "user", "content": prompt}], **extra)
            if res.stop_reason == "refusal":
                print(f"[llm guide refused] {res.stop_details}")
                return None
            text = "".join(b.text for b in res.content if b.type == "text").strip()
        text = DIAGRAM_TOKEN.sub(lambda m: restore(diagrams, m), text)
        refs = "\n".join(f"- [{d.title}]({view_link(d.path)}) — {d.path}" for d, _ in sources)
        return f"{text}\n\n## 원문 링크\n\n{refs}" if text else None
    except Exception as exc:
        print(f"[llm guide failed] {exc}")
        return None


def restore(diagrams: list[str], m: re.Match) -> str:
    n = int(m.group(1) or m.group(2))
    return f"\n{diagrams[n - 1]}\n" if 0 < n <= len(diagrams) else ""


HTML_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wiki Guide Search</title><style>
:root{--ink:#172033;--muted:#718096;--line:#e5eaf0;--blue:#2563eb;--soft:#f5f8fc;--panel:#fff;--hl:#fff3b0}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px/1.65 "Segoe UI","Malgun Gothic",sans-serif;background:#eef2f7}
.top{height:58px;background:#13213a;color:#fff;display:flex;align-items:center;padding:0 24px;gap:14px}.mark{width:28px;height:28px;border-radius:8px;background:#42b883;display:grid;place-items:center;font-weight:800}.brand{font-size:16px;font-weight:700}.crumb{color:#aebbd0;font-size:12px}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 80px}
.search{display:flex;gap:8px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:8px}.search input{flex:1;border:0;outline:0;font:inherit;font-size:16px;padding:8px 10px}
button{font:inherit;border:0;border-radius:8px;padding:9px 16px;cursor:pointer;background:var(--blue);color:#fff;font-weight:600}button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:default}
.steps{display:flex;gap:8px;margin:16px 0;flex-wrap:wrap}.step{padding:4px 12px;border-radius:999px;background:#dfe6ef;color:var(--muted);font-size:12px;font-weight:600}.step.on{background:#13213a;color:#fff}.step.done{background:#42b883;color:#fff}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 0;font-size:12px;color:var(--muted);align-items:center}.chip{background:#e8effa;color:#174ea6;border-radius:6px;padding:2px 8px}
.grid{display:grid;grid-template-columns:minmax(0,380px) minmax(0,1fr);gap:16px;align-items:start}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px}.card h2{font-size:15px;margin:0 0 10px}
.res{border-top:1px solid var(--line);padding:10px 0}.res:first-of-type{border-top:0}.res a{color:var(--blue);font-weight:600;text-decoration:none}.res .p{color:var(--muted);font-size:11px;word-break:break-all}.res .s{font-size:12px;margin-top:4px}.bar{height:4px;background:#e5eaf0;border-radius:2px;margin-top:4px}.bar i{display:block;height:100%;background:#42b883;border-radius:2px}
mark{background:var(--hl);padding:0 1px;border-radius:2px}
.ask{border:2px solid var(--blue)}.opts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}.opt{text-align:left;background:#f5f8fc;color:var(--ink);border:1px solid var(--line);padding:12px}.opt:hover{border-color:var(--blue);background:#edf4ff}.opt b{display:block;font-size:15px}.opt span{font-size:12px;color:var(--muted);font-weight:400}
.guide h1{font-size:26px;margin:0 0 8px}.guide h2{font-size:19px;border-bottom:1px solid var(--line);padding-bottom:6px;margin-top:28px}.guide h3{font-size:16px;margin-top:20px}.guide blockquote{margin:0;padding:8px 14px;background:var(--soft);border-left:4px solid var(--blue);color:#4a5568}.guide a{color:var(--blue);text-decoration:none}.guide ul{padding-left:22px}.guide table{border-collapse:collapse;width:100%;font-size:12px;display:block;overflow-x:auto}.guide td,.guide th{border:1px solid var(--line);padding:5px 8px;text-align:left}.guide th{background:var(--soft)}
.wait{text-align:center;padding:36px 0;color:var(--muted)}.wait b{color:var(--ink);font-variant-numeric:tabular-nums}.prog{height:6px;max-width:320px;margin:14px auto 0;background:#e5eaf0;border-radius:3px;overflow:hidden}.prog i{display:block;width:35%;height:100%;background:var(--blue);border-radius:3px;animation:slide 1.4s ease-in-out infinite}@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(290%)}}
.guide pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow-x:auto;font:12.5px/1.55 Consolas,monospace}.guide .mermaid{margin:14px 0;text-align:center;overflow-x:auto}
.tools{display:flex;gap:8px;justify-content:flex-end;margin-bottom:8px}.tools[hidden]{display:none}.muted{color:var(--muted)}.empty{color:var(--muted);text-align:center;padding:40px 0}
@media(max-width:820px){.grid{grid-template-columns:1fr}.opts{grid-template-columns:1fr}}
</style></head><body>
<header class="top"><div class="mark">G</div><div class="brand">Wiki Guide Search</div><div class="crumb">wiki-repo 의미 검색 · 가이드 생성</div></header>
<div class="wrap">
<form class="search" id="form"><input id="q" placeholder="예: 프록시 설정 가이드 알려줘" autocomplete="off" autofocus><button id="go">검색</button></form>
<div class="steps"><span class="step" id="s1">1. 문서 검색</span><span class="step" id="s2">2. 유형 확인</span><span class="step" id="s3">3. 관련 문서 재검색</span><span class="step" id="s4">4. 가이드 생성</span></div>
<div class="grid"><section class="card"><h2>검색 결과</h2><div id="analysis"></div><div id="results"><div class="empty">질문을 입력하세요.</div></div></section>
<section><div id="ask"></div><div class="card" id="guideCard"><div class="tools" id="tools" hidden><button class="ghost" id="copy">Markdown 복사</button><button class="ghost" id="dl">.md 다운로드</button></div><div class="guide" id="guide"><div class="empty">검색 후 가이드가 여기에 생성됩니다.</div></div></div></section></div>
</div>
<script>
const $=s=>document.querySelector(s);let state={q:'',md:'',title:'guide'};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function hl(s,words){let x=esc(s);if(!words.length)return x;const re=new RegExp('('+words.map(w=>esc(w).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')','gi');return x.replace(re,'<mark>$1</mark>')}
function steps(n,done){for(let i=1;i<=4;i++){const e=$('#s'+i);e.className='step'+(i<n||done&&i===n?' done':i===n?' on':'')}}
function inline(s){let x=esc(s);x=x.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');x=x.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(m,t,u)=>`<a href="${u}" target="_blank" rel="noopener">${t}</a>`);return x.replace(/^\[ \] /,'☐ ')}
function md(src){let out='',list=false,table=[],code=null;const fl=()=>{if(list){out+='</ul>';list=false}if(table.length){const rows=table.filter(r=>!/^\|[\s|:-]+\|$/.test(r)).map(r=>r.replace(/^\||\|$/g,'').split('|').map(c=>c.trim()));out+='<table>'+rows.map((r,i)=>'<tr>'+r.map(c=>i?`<td>${inline(c)}</td>`:`<th>${inline(c)}</th>`).join('')+'</tr>').join('')+'</table>';table=[]}};
for(const raw of src.replace(/\r/g,'').split('\n')){const line=raw.trimEnd();let m;if(code){if(code.end.test(line)){const body=esc(code.lines.join('\n'));out+=code.mermaid?`<div class="mermaid">${body}</div>`:`<pre><code>${body}</code></pre>`;code=null}else code.lines.push(raw);continue}if(/^\s*:::\s*mermaid\s*$/.test(line)){fl();code={mermaid:true,end:/^\s*:::\s*$/,lines:[]};continue}if(m=line.match(/^\s*```\s*([\w-]*)/)){fl();code={mermaid:m[1]==='mermaid',end:/^\s*```\s*$/,lines:[]};continue}if(line.startsWith('|')){if(list){out+='</ul>';list=false}table.push(line);continue}if(table.length)fl();
if(m=line.match(/^(#{1,4})\s+(.*)$/)){fl();out+=`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`}else if(m=line.match(/^\s{2,}(\S.*)$/)){out=out.replace(/<\/li>$/,'<br><span class="muted">'+inline(m[1])+'</span></li>')}else if(m=line.match(/^[-*]\s+(.*)$/)){if(!list){out+='<ul>';list=true}out+='<li>'+inline(m[1])+'</li>'}else if(m=line.match(/^>\s?(.*)$/)){fl();out+='<blockquote>'+inline(m[1])+'</blockquote>'}else if(line.trim()){fl();out+='<p>'+inline(line)+'</p>'}else if(!list)fl()}if(code)out+=`<pre><code>${esc(code.lines.join('\n'))}</code></pre>`;fl();return out}
let mermaidLib;async function diagrams(){const els=[...document.querySelectorAll('#guide .mermaid')];if(!els.length)return;try{mermaidLib=mermaidLib||(await import('https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs')).default;mermaidLib.initialize({startOnLoad:false,securityLevel:'strict'});await mermaidLib.run({nodes:els,suppressErrors:true})}catch(e){console.warn('mermaid',e)}els.filter(el=>!el.querySelector('svg')).forEach(el=>{el.outerHTML='<pre><code>'+esc(el.textContent)+'</code></pre><p class="muted" style="font-size:11px">다이어그램을 그리지 못해 원문을 표시합니다.</p>'})}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error(await r.text());return r.json()}
function showResults(d,label){const a=d.analysis;$('#analysis').innerHTML='<div class="chips">해석: '+a.concepts.map(c=>`<span class="chip" title="${esc(c.words.join(', '))}">${esc(c.words[0])} ≈ ${esc(c.words.slice(1).join(' · '))}</span>`).join('')+a.terms.map(t=>`<span class="chip">${esc(t)}</span>`).join('')+(label?`<span class="chip" style="background:#d9f5e6;color:#11643c">${esc(label)}</span>`:'')+` <span>· ${d.results.length}건</span></div>`;
if(!d.results.length){$('#results').innerHTML='<div class="empty">관련 문서가 없습니다.</div>';return}const max=d.results[0].score||1;
$('#results').innerHTML=d.results.map(r=>`<div class="res"><a href="${esc(r.link)}" target="_blank" rel="noopener">${hl(r.title,d.highlights)}</a><div class="p">${esc(r.path)}</div>${r.snippets.map(s=>`<div class="s">${hl(s,d.highlights)}</div>`).join('')}<div class="bar"><i style="width:${Math.round(r.score/max*100)}%"></i></div></div>`).join('')}
async function search(q){state.q=q;steps(1);$('#ask').innerHTML='';$('#tools').hidden=true;$('#guide').innerHTML='<div class="empty">검색 중…</div>';
const d=await post('/api/search',{q});showResults(d);
if(d.clarify){steps(2);$('#guide').innerHTML='<div class="empty">위에서 유형을 선택하면 가이드를 생성합니다.</div>';
$('#ask').innerHTML=`<div class="card ask" style="margin-bottom:16px"><h2>${esc(d.clarify.question)}</h2><div class="opts">${d.clarify.options.map(o=>`<button class="opt" data-k="${esc(d.clarify.key)}" data-v="${esc(o.id)}"><b>${esc(o.label)}</b><span>${esc(o.desc)}</span></button>`).join('')}</div></div>`;
document.querySelectorAll('.opt').forEach(b=>b.onclick=()=>guide(b.dataset.k,b.dataset.v,b.querySelector('b').textContent))}
else guide(d.variant&&d.variant.key,d.variant&&d.variant.id,d.variant&&d.variant.label)}
async function guide(key,variant,label){steps(3);if(label)$('#ask').innerHTML=`<div class="card" style="margin-bottom:16px">선택한 유형: <b>${esc(label)}</b> <button class="ghost" style="float:right;padding:3px 10px" id="re">다시 선택</button></div>`;const re=$('#re');if(re)re.onclick=()=>search(state.q);
const t0=Date.now(),sec=()=>Math.floor((Date.now()-t0)/1000);
$('#guide').innerHTML='<div class="wait">관련 문서를 다시 검색해 가이드를 만드는 중… <b id="elapsed">0초</b><div class="prog"><i></i></div></div>';
const tick=setInterval(()=>{const e=$('#elapsed');if(e)e.textContent=sec()+'초'},1000);
let d;try{d=await post('/api/guide',{q:state.q,key,variant})}catch(err){$('#guide').innerHTML='<div class="empty">오류: '+esc(err.message)+' ('+sec()+'초)</div>';return}finally{clearInterval(tick)}steps(4,true);state.md=d.markdown;state.title=d.title;
showResults(d,label?label+' 기준 재검색':'');
$('#guide').innerHTML=md(d.markdown)+`<p class="muted" style="font-size:11px;margin-top:24px">생성 방식: ${d.mode==='llm'?'LLM 작성':'문서 조합 템플릿'} · 소요 ${sec()}초</p>`;$('#tools').hidden=false;diagrams()}
$('#form').onsubmit=e=>{e.preventDefault();const q=$('#q').value.trim();if(q)search(q).catch(err=>{$('#guide').innerHTML='<div class="empty">오류: '+esc(err.message)+'</div>'})};
$('#copy').onclick=()=>navigator.clipboard.writeText(state.md).then(()=>{$('#copy').textContent='복사됨';setTimeout(()=>$('#copy').textContent='Markdown 복사',1200)});
$('#dl').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([state.md],{type:'text/markdown'}));a.download=state.title.replace(/\s+/g,'-')+'.md';a.click()};
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path != "/":
            self.send_error(404); return
        data = HTML_PAGE.encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            q = str(body.get("q", "")).strip()[:500]
            if not q:
                self.send_json({"error": "empty query"}, 400); return
            path = urlparse(self.path).path
            refresh_index()
            if path == "/api/search":
                self.send_json(do_search(q)); return
            if path == "/api/guide":
                self.send_json(do_guide(q, body.get("key"), body.get("variant"))); return
            self.send_error(404)
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "bad request"}, 400)

    def log_message(self, *_):
        pass


INDEX: Index
_index_sig: str | None = None
_index_lock = threading.Lock()


def wiki_signature(root: Path) -> str:
    """wiki-repo 의 .md 경로·수정 시각 목록의 해시. 문서 추가·삭제·이름 변경·수정이 있으면 값이 바뀐다."""
    items = []
    for p in root.rglob("*.md"):
        rel = p.relative_to(root)
        if not any(part.startswith(".") for part in rel.parts):
            items.append(f"{rel.as_posix()}|{p.stat().st_mtime_ns}")
    return hashlib.sha1("\n".join(sorted(items)).encode("utf-8")).hexdigest()


def refresh_index():
    """검색 요청마다 wiki-repo 변경 여부를 보고, 바뀌었으면 인덱스를 다시 만든다(임베딩은 바뀐 문서만 새로 계산)."""
    global INDEX, _index_sig
    with _index_lock:
        sig = wiki_signature(ROOT)
        if sig == _index_sig:
            return
        started = time.monotonic()
        INDEX = Index(ROOT)
        _index_sig = sig
        print(f"Re-indexed {len(INDEX.docs)} documents ({time.monotonic() - started:.1f}s)", flush=True)


def main():
    global INDEX, _index_sig
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8766
    if not ROOT.is_dir(): raise SystemExit(f"Wiki root not found: {ROOT}")
    print(f"Indexing {ROOT} ...", flush=True)
    INDEX = Index(ROOT)
    _index_sig = wiki_signature(ROOT)
    print(f"Indexed {len(INDEX.docs)} documents from {ROOT}")
    print(f"Embeddings: {'on' if INDEX.embeddings.vectors else 'off (offline semantic search)'}")
    server = local_server(port, Handler)
    print(f"Wiki guide search: http://127.0.0.1:{port}")
    if "--no-browser" not in sys.argv:
        open_chrome(f"http://127.0.0.1:{port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
