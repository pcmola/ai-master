"""Local semantic search and guide generator for wiki-repo (read-only).

- 개념 사전(동의어/한영 표기) + 문자 bigram TF-IDF 로 의미 기반 검색을 한다.
- 모호한 개념(예: 프록시)은 포워드/리버스 중 무엇인지 되묻는다.
- 선택 결과로 재검색한 문서를 조합해 Markdown 가이드를 만든다.
- 가이드 작성 LLM: ANTHROPIC_API_KEY 가 있으면 Claude, 없으면 OpenAI 호환 클라이언트를 쓴다.
- OpenAI 호환 클라이언트: AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY 면 사내 Azure OpenAI 게이트웨이,
  OPENAI_API_KEY 면 OpenAI. 이 클라이언트가 있으면 임베딩 재순위화도 사용한다.
- 웹 검색: TAVILY_API_KEY 가 있고 위키 근거가 약하면(최고 임베딩 유사도 < WEB_SEARCH_MIN_SIM, 또는 근거 문서 0건)
  Tavily 검색 결과를 가이드 근거에 보탠다. 사내 게이트웨이는 내장 web_search 도구(Responses API)를 지원하지 않는다.
- 트레이싱: 요청마다 노드(refresh_index → analyze → hybrid_search(embed_query) → select_sources → web_decision →
  web_search → llm_generate | template)별 소요 시간과 결과를 콘솔(허브 로그)과 data/traces/wiki-search-YYYYMMDD.jsonl 에 남긴다.
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
import urllib.request
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE, WIKI_REPO  # noqa: E402
from common.server import favicon, local_server, open_chrome, site_url  # noqa: E402,F401  (다른 도구가 여기서 가져다 쓰던 이름 유지)
from common.trace import heartbeat, plan, replan, simulate, span, step, think, trace  # noqa: E402

ROOT = WIKI_REPO.resolve()
CACHE = DATA_DIR / "wiki_search_cache.json"
CHUNK_CACHE = DATA_DIR / "wiki_search_chunks.json"  # 가이드 재료 조각(청크) 임베딩 캐시
AI_DOC_PREFIX = "AI-Master-프로젝트/02.-운영/00.-AI-문서-생성"  # 시나리오 1 이 등록한 문서(가이드 재료에서 제외)
GUIDE_CHUNKS = int(os.environ.get("GUIDE_CHUNKS", "24"))           # LLM 에 넘길 조각 수 상한
GUIDE_CHUNK_CHARS = int(os.environ.get("GUIDE_CHUNK_CHARS", "1500"))  # 조각 하나의 대략적인 최대 길이
GUIDE_CONTEXT_CHARS = int(os.environ.get("GUIDE_CONTEXT_CHARS", "16000"))  # 넘길 조각 전체 글자 수 상한
EMBED_WEIGHT = float(os.environ.get("EMBED_WEIGHT", "8"))  # 혼합 검색에서 임베딩 유사도 가중치
OPENAI_TIMEOUT = 120  # 초. 게이트웨이가 응답하지 않으면 오래 매달리지 않고 오프라인 검색으로 넘어간다.
# 검색 결과의 문서 링크. 비우면 wiki_viewer.py 가 뜨는 주소(hosts 의 wiki.local 기준)를 쓴다.
VIEWER = os.environ.get("WIKI_VIEWER_URL") or site_url("wiki.local", 8765)[2]

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path: Path):
        """Load the project's simple KEY=VALUE .env file without an optional dependency."""
        if not path.is_file():
            return False
        loaded = False
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name or name in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[name] = value
            loaded = True
        return loaded

load_dotenv(ENV_FILE)


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


WEB_GENERIC = {"guide"}  # 웹 검색어에 넣으면 잡음만 늘리는 일반 개념


def jamo(word: str) -> str:
    """한글 음절을 초성·중성·종성으로 풀어 쓴다(프록실 → ㅍㅡㄹㅗㄱㅅㅣㄹ). 받침 하나 차이도 편집 거리 1이 된다."""
    out = []
    for ch in word:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out += [chr(0x1100 + code // 588), chr(0x1161 + code % 588 // 28)]
            if code % 28:
                out.append(chr(0x11A7 + code % 28))
        else:
            out.append(ch)
    return "".join(out)


def edit_distance(a: str, b: str, limit: int) -> int:
    """글자 넣기·빼기·바꾸기·이웃 두 글자 뒤바꿈(porxy→proxy)을 1로 센다. limit 를 넘으면 limit+1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if prev2 and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def fix_typos(concepts: list[str], terms: list[str]) -> tuple[list[str], list[str], list[dict]]:
    """사전에 없는 단어가 개념 사전의 단어와 거의 같으면(오타) 그 개념으로 고친다.
    허용 거리: 한글은 3음절 이상(풀어 쓴 길이 7 이상)만 1, 영문은 5글자 이상만 1, 풀어 쓴 길이 10 이상은 2.
    두 음절 말(설정·결정, 로그·로고)은 서로 다른 실제 단어와 거리 1 이라 잘못 고치므로 고치지 않는다."""
    fixes, rest = [], []
    for term in terms:
        tj, best = jamo(term), None
        for concept, syns in CONCEPTS.items():
            for syn in syns:
                sj = jamo(syn)
                short = len(sj) < (7 if sj != syn else 5)  # sj != syn 이면 한글이 들어 있다
                limit = 0 if short else 1 if len(sj) < 10 else 2
                d = edit_distance(tj, sj, limit) if limit else limit + 1
                if d <= limit and (best is None or d < best[0]):
                    best = (d, concept, syn)
        if best:
            fixes.append({"from": term, "to": best[2], "concept": best[1]})
            if best[1] not in concepts:
                concepts = concepts + [best[1]]
        else:
            rest.append(term)
    return concepts, rest, fixes


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
            # 검색 랭킹 비교는 test/wiki_search_benchmark.py와 고정된 16개 케이스로 재현한다.
            # 키워드 점수가 의미 점수를 덮지 않도록 임베딩 가중치를 크게 둔다.
            scores = [s + EMBED_WEIGHT * max(sim, 0.0) for s, sim in zip(scores, sims)]
        ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)
        return [(self.docs[i], scores[i]) for i in ranked[:limit] if scores[i] > 0.05]


class Embeddings:
    """OPENAI_API_KEY 가 있을 때만 동작하는 선택적 임베딩 재순위화."""

    def __init__(self, docs: list[Doc]):
        self.client = openai_client()
        self.model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        self.vectors: list[list[float]] = []
        self.docs = docs
        self._last: tuple[str, list[float]] | None = None
        self.cache_model = None
        self.cache_reused = 0
        self.cache_new = 0
        self.cache_status = "client unavailable"
        if not self.client:
            print(f"Embeddings: off model={self.model} reason=client unavailable", flush=True)
            return
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else {}
            self.cache_model = cache.get("model")
            if cache.get("model") != self.model:
                cache = {"model": self.model, "vectors": {}}
                self.cache_status = f"model changed ({self.cache_model or 'none'} -> {self.model})"
            else:
                self.cache_status = "same model"
            keys = [hashlib.sha1((d.title + d.text).encode("utf-8")).hexdigest() for d in docs]
            missing = [(k, d) for k, d in zip(keys, docs) if k not in cache["vectors"]]
            self.cache_reused = len(docs) - len(missing)
            self.cache_new = len(missing)
            print(f"Embeddings: model={self.model} cache_model={self.cache_model or '-'} "
                  f"재사용 {self.cache_reused}건, 새로 계산 {self.cache_new}건, "
                  f"전체 {len(docs)}건 ({self.cache_status})", flush=True)
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
            dimensions = len(self.vectors[0]) if self.vectors else 0
            print(f"Embeddings ready: model={self.model} dims={dimensions} "
                  f"cache_reused={self.cache_reused} cache_new={self.cache_new}", flush=True)
        except Exception as exc:  # 네트워크·키 오류 시 오프라인 검색만 사용
            print(f"[embeddings disabled] {exc}")
            self.vectors = []

    def similarity(self, query: str) -> list[float]:
        if not self.vectors:
            return []
        if self._last and self._last[0] == query:
            return self._last[1]
        q = self.vector(query)
        if q is None:
            return []
        with span("doc_similarity", docs=len(self.vectors)) as s:
            qn = math.sqrt(sum(x * x for x in q)) or 1.0
            sims = [sum(a * b for a, b in zip(q, v)) / (qn * (math.sqrt(sum(x * x for x in v)) or 1.0))
                    for v in self.vectors]
            best = max(range(len(sims)), key=sims.__getitem__)
            s.set(dims=len(q), best_sim=round(sims[best], 3), best_doc=self.docs[best].title)
        self._last = (query, sims)
        return sims

    def vector(self, query: str) -> list[float] | None:
        """질의 임베딩(같은 질의는 한 번만 계산)."""
        if not self.client:
            return None
        cached = getattr(self, "_qvec", None)
        if cached and cached[0] == query:
            return cached[1]
        with span("embed_query", model=self.model) as s:
            try:
                q = self.client.embeddings.create(model=self.model, input=[query]).data[0].embedding
            except Exception as exc:
                s.fail(f"임베딩 질의 실패 → 키워드 검색만 사용: {exc}")
                return None
            s.set(dims=len(q))
        self._qvec = (query, q)
        return q

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """조각 임베딩. CHUNK_CACHE 에 모델별로 저장해 같은 조각은 다시 계산하지 않는다."""
        if not self.client or not texts:
            return None
        try:
            cache = json.loads(CHUNK_CACHE.read_text(encoding="utf-8")) if CHUNK_CACHE.is_file() else {}
        except ValueError:
            cache = {}
        if cache.get("model") != self.model:
            cache = {"model": self.model, "vectors": {}}
        keys = [hashlib.sha1(x.encode("utf-8")).hexdigest() for x in texts]
        missing = [(k, x) for k, x in zip(keys, texts) if k not in cache["vectors"]]
        for start in range(0, len(missing), 100):
            batch = missing[start:start + 100]
            res = self.client.embeddings.create(model=self.model, input=[x[:6000] for _, x in batch])
            for (k, _), item in zip(batch, res.data):
                cache["vectors"][k] = item.embedding
        if missing:
            CHUNK_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        self.chunk_new = len(missing)
        return [cache["vectors"][k] for k in keys]


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


def traced_analyze(query: str) -> tuple[list[str], list[str], list[list[str]], list[dict]]:
    with span("analyze") as s:
        concepts, terms = analyze(query)
        concepts, terms, fixes = fix_typos(concepts, terms)
        groups = build_groups(concepts, terms)
        s.set(concepts=concepts, terms=terms, groups=len(groups),
              typo_fixes=[f"{f['from']}→{f['to']}" for f in fixes])
    if fixes:
        think("Self-Correction", "질의 오타 교정: " + ", ".join(f"{f['from']}→{f['to']}" for f in fixes)
                                 + " → 고친 질의로 검색")
    return concepts, terms, groups, fixes


def corrected(query: str, fixes: list[dict]) -> str:
    """오타를 고친 질의. 키워드(bigram)·임베딩 검색에 원문 대신 쓴다."""
    for f in fixes:
        query = re.sub(re.escape(f["from"]), f["to"], query, flags=re.I)
    return query


def traced_search(groups, query: str, boost=()) -> list[tuple[Doc, float]]:
    with span("hybrid_search", docs=len(INDEX.docs), embeddings="on" if INDEX.embeddings.vectors else "off") as s:
        ranked = INDEX.search(groups, query, boost)
        s.set(results=len(ranked), top=round(ranked[0][1], 2) if ranked else None,
              top_doc=ranked[0][0].title if ranked else None)
    return ranked


def do_search(query: str) -> dict:
    concepts, terms, groups, fixes = traced_analyze(query)
    highlights = sorted({w for g in groups for w in g}, key=len, reverse=True)
    key, variant = detect_variant(query, concepts)
    results = traced_search(groups, corrected(query, fixes))
    clarify = None
    if key and not variant:
        spec = CLARIFY[key]
        clarify = {"key": key, "question": spec["question"],
                   "options": [{"id": i, "label": o["label"], "desc": o["desc"]} for i, o in spec["options"].items()]}
    return {
        "analysis": {"concepts": [{"id": c, "words": CONCEPTS[c]} for c in concepts], "terms": terms, "fixes": fixes},
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


def do_guide(query: str, key: str | None, variant: str | None, revise: dict | None = None) -> dict:
    """guide_events 를 끝까지 받아 한 번에 돌려준다(스트리밍이 필요 없는 호출용)."""
    result: dict = {}
    for event in guide_events(query, key, variant, revise):
        if event["type"] in ("meta", "done"):
            result.update({k: v for k, v in event.items() if k != "type"})
    return result


def guide_events(query: str, key: str | None, variant: str | None, revise: dict | None = None):
    """가이드 생성 과정을 이벤트로 낸다: meta(검색 결과) → delta(LLM 이 쓴 완성된 줄, 여러 번) → done(최종 Markdown).
    revise={"markdown": 지금 가이드, "request": 수정 요청} 이면 근거 조각을 요청까지 반영해 다시 고르고 가이드를 고쳐 쓴다."""
    option = CLARIFY[key]["options"][variant] if key in CLARIFY and variant in CLARIFY[key]["options"] else None
    plan(f"'{query[:40]}' 통합 가이드 작성" + (f" (유형: {option['label']})" if option else "")
         + (" · 수정 요청 반영" if revise else ""),
         [("analyze", "질의 해석 (개념·오타 교정)"),
          ("hybrid_search", "Wiki 하이브리드 검색 (키워드 + 임베딩)"),
          ("select_sources", "핵심 개념이 있는 문서만 근거로 선별"),
          ("chunk_select", "근거 문서를 조각으로 나눠 관련 조각만 선택 (RAG)"),
          ("web_decision", "근거 충분성 판단 → 부족하면 웹 검색 추가"),
          ("llm_generate", "(AI) 근거·출처를 단 가이드 생성" + (" — 기존 가이드 + 수정 요청" if revise else ""))])
    llm_ready = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")
                     or os.environ.get("OPENAI_API_KEY"))
    simulate([("Wiki 색인", bool(INDEX.docs), f"문서 {len(INDEX.docs):,}건"),
              ("임베딩", bool(INDEX.embeddings.vectors), "의미 검색 사용" if INDEX.embeddings.vectors
               else "없음 → 키워드 검색만"),
              ("LLM", llm_ready, "가이드 생성" if llm_ready else "설정 없음 → 템플릿 가이드로 대체"),
              ("웹 검색", bool(os.environ.get("TAVILY_API_KEY")), "Tavily (근거 약할 때만)"
               if os.environ.get("TAVILY_API_KEY") else "TAVILY_API_KEY 없음 → Wiki 만 사용")])
    step("analyze")
    concepts, terms, groups, fixes = traced_analyze(query)
    think("Observation", f"개념 {', '.join(concepts) or '없음'} · 고유어 {', '.join(terms[:5]) or '없음'}")
    boost = option["terms"] if option else []
    fixed = corrected(query, fixes)
    search_query = f"{fixed} {option['label']} {' '.join(boost)}" if option else fixed
    step("hybrid_search")
    think("Tool Call", f"index.hybrid_search(query=\"{search_query[:60]}\", boost={len(boost)})")
    ranked = traced_search(groups, search_query, boost)

    step("select_sources")
    with span("select_sources", candidates=len(ranked)) as sp:
        # 핵심 개념(가장 드문 동의어 묶음)이 실제로 나오는 문서만 가이드 재료로 쓴다.
        core = min(groups, key=lambda g: sum(1 for d in INDEX.docs if any(s in d.low for s in g))) if groups else []
        # AI 가 만들어 등록한 문서(00. AI 문서 생성)는 재료에서 뺀다. 자기가 쓴 글을 근거로 다시 쓰면 틀린 내용이 이어진다.
        sources = [(d, s) for d, s in ranked if (not core or any(w in d.low for w in core))
                   and not d.path.startswith(AI_DOC_PREFIX)][:15]

        statements: dict[str, list[Doc]] = {}
        for d, _ in sources:
            for line in matching_lines(d, [(core, 1.0)] if core else group_weights(groups), limit=5):
                statements.setdefault(line, []).append(d)
        sp.set(core=core[:3], sources=len(sources), statements=len(statements),
               top3=[f"{d.title}({s:.2f})" for d, s in sources[:3]])
    think("Observation", f"후보 {len(ranked)}건 중 근거 문서 {len(sources)}건, 근거 문장 {len(statements)}개")
    request = (revise or {}).get("request", "").strip()
    step("chunk_select")
    chunks = select_chunks(sources, f"{search_query} {request}".strip(), [w for g in groups for w in g] + boost)

    title = f"{option['label'] if option else query.strip()} 가이드"
    step("web_decision")
    web_reason = web_search_reason(sources, search_query, option)
    # 유형을 골랐으면 그 유형이 속한 개념(예: proxy)을 기준으로 검색어를 만들고 결과를 거른다.
    base = key if option and key in CONCEPTS else None
    web, wq, must = [], None, []
    if not web_reason and request:
        # 원 질의는 위키에 있어도 수정 요청이 위키 밖 사실(예: 출근지 주소)을 물을 수 있다 → 요청 기준으로 한 번 더 본다.
        web_reason, wq, must = request_web_reason(request, title)
    if web_reason:
        replan(f"Wiki 근거 부족({web_reason})", add=[("web_search", "웹 검색으로 근거 보완 (Tavily)")], before="llm_generate")
        step("web_search")
        if not wq:
            wq, must = web_query(concepts, terms, option, base), web_must(option, base, core)
        think("Tool Call", f"tavily.search(query=\"{wq[:60]}\")")
        web = web_search(wq, must)
        think("Observation", f"웹 근거 {len(web)}건")
    else:
        think("Decision", "Wiki 근거로 충분 → 웹 검색 생략")
    client = (claude_client() or openai_client()) if statements or web else None
    model = llm_model(client) if client else None
    highlights = sorted({w for g in groups for w in g} | set(boost), key=len, reverse=True)
    weighted = group_weights(groups) + [([b], 0.3) for b in boost]
    yield {"type": "meta", "title": title, "model": model,
           "web": [{"title": w["title"], "url": w["url"]} for w in web], "web_reason": web_reason,
           "analysis": {"concepts": [{"id": c, "words": CONCEPTS[c]} for c in concepts], "terms": terms, "fixes": fixes},
           "highlights": highlights,
           "results": [{"path": d.path, "title": d.title, "link": view_link(d.path), "score": round(s, 3),
                        "snippets": matching_lines(d, weighted)} for d, s in sources]}

    markdown = None
    if client:
        step("llm_generate")
        think("Tool Call", f"llm.generate(model={model}, 근거 조각 {len(chunks)}개, 웹 {len(web)}건)")
        gen = llm_guide(client, query, title, option, statements, sources, web, chunks, revise)
        try:
            while True:
                yield {"type": "delta", "text": next(gen)}
        except StopIteration as stop:
            markdown = stop.value
    if markdown:
        think("Reflection", f"가이드 {len(markdown):,}자 · 근거 문서 {len({c['doc'].path for c in chunks}) or len(sources)}건"
                            + (f" + 웹 {len(web)}건" if web else "") + " 출처 표기 · 근거 없는 내용은 '근거 없음'으로 표시하도록 지시")
        yield {"type": "done", "mode": "llm", "model": model, "markdown": markdown}
        return
    if revise:  # 수정 요청이 실패하면 지금 가이드를 그대로 돌려준다(템플릿으로 덮지 않는다)
        think("Self-Correction", "수정 생성 실패 → 기존 가이드를 그대로 유지(템플릿으로 덮지 않음)")
        yield {"type": "done", "mode": "llm", "model": model, "markdown": revise.get("markdown", ""), "revise_failed": True}
        return
    # LLM 이 없거나 도중에 실패하면 템플릿으로 바꿔 낸다(화면은 받은 글을 이 내용으로 갈아 끼운다)
    replan("LLM 생성 불가 → 근거 문장 템플릿으로 대체", drop=["llm_generate"], add=[("template", "근거 문장을 분류해 템플릿 가이드 작성")])
    step("template")
    with span("template", reason="LLM 실패" if client else ("근거 없음" if not (statements or web) else "LLM 설정 없음")) as s:
        markdown = template_guide(query, title, option, statements, sources)
        if web:
            markdown += "\n\n## 웹 참고 링크\n\n" + "\n".join(f"- [{w['title']}]({w['url']})" for w in web)
        s.set(chars=len(markdown))
    yield {"type": "done", "mode": "template", "model": None, "markdown": markdown}


def split_chunks(doc: Doc) -> list[dict]:
    """문서를 제목(#~####) 단위 조각으로 나눈다. 긴 절은 문단(빈 줄) 단위로 GUIDE_CHUNK_CHARS 안팎으로 더 나누되,
    코드 블록(```)과 다이어그램(::: mermaid)은 중간에서 자르지 않는다. 각 조각에 상위 제목 경로를 붙인다."""
    sections, heads, buf, fence = [], [], [], None

    def close():
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join(h for _, h in heads), body))
        buf.clear()

    for line in doc.text.split("\n"):
        s = line.strip()
        if fence:
            buf.append(line)
            if (fence == "```" and s.startswith("```")) or (fence == ":::" and s == ":::"):
                fence = None
            continue
        if s.startswith("```"):
            fence = "```"
        elif re.match(r"^:::\s*mermaid", s, re.I):
            fence = ":::"
        m = re.match(r"^(#{1,4})\s+(.+)$", line) if not fence else None
        if m:
            close()
            level = len(m.group(1))
            heads[:] = [h for h in heads if h[0] < level] + [(level, m.group(2).strip())]
            buf.append(line)
            continue
        buf.append(line)
    close()

    chunks = []
    for heading, body in sections:
        if len(body) <= GUIDE_CHUNK_CHARS:
            chunks.append({"heading": heading, "text": body})
            continue
        part, size, fence = [], 0, None  # 문단 단위로 나눈다(코드 블록 안의 빈 줄에서는 자르지 않음)
        for line in body.split("\n"):
            s = line.strip()
            if fence and ((fence == "```" and s.startswith("```")) or (fence == ":::" and s == ":::")):
                fence = None
            elif not fence and s.startswith("```"):
                fence = "```"
            elif not fence and re.match(r"^:::\s*mermaid", s, re.I):
                fence = ":::"
            if not s and not fence and size >= GUIDE_CHUNK_CHARS:
                chunks.append({"heading": heading, "text": "\n".join(part).strip()})
                part, size = [], 0
                continue
            part.append(line)
            size += len(line) + 1
        if "\n".join(part).strip():
            chunks.append({"heading": heading, "text": "\n".join(part).strip()})
    for i, c in enumerate(chunks):
        c.update(doc=doc, order=i)
    return chunks


def select_chunks(sources, query: str, words: list[str]) -> list[dict]:
    """후보 문서를 조각으로 나눠 질의와 가까운 조각만 고른다(RAG 의 Retrieval).
    점수 = 질의 임베딩과의 코사인 + 0.08 × 문서 순위 점수(1위=1) + 0.05 × 핵심어 포함. 임베딩이 없으면 키워드 점수.
    같은 글(공백 무시)이거나 거의 같은 조각(코사인 0.97 이상)은 하나만 남기고, 조각 수·글자 수 상한 안에서 고른다."""
    with span("chunk_select", docs=len(sources)) as s:
        chunks = [c for d, _ in sources for c in split_chunks(d)]
        if not chunks:
            s.set(chunks=0)
            return []
        top = sources[0][1] or 1.0
        prior = {d.path: sc / top for d, sc in sources}
        low_words = [w.lower() for w in words if w]
        qv = INDEX.embeddings.vector(query)
        vecs = INDEX.embeddings.embed([f"{c['doc'].title} > {c['heading']}\n{c['text']}" for c in chunks]) if qv else None
        norm = lambda v: math.sqrt(sum(x * x for x in v)) or 1.0
        cos = lambda a, b: sum(x * y for x, y in zip(a, b)) / (norm(a) * norm(b))
        for i, c in enumerate(chunks):
            hit = any(w in c["text"].lower() for w in low_words)
            if vecs:
                c["vec"] = vecs[i]
                c["score"] = cos(qv, vecs[i]) + 0.08 * prior[c["doc"].path] + 0.05 * hit
            else:  # 임베딩이 없으면 핵심어 출현 수로 고른다
                c["score"] = sum(c["text"].lower().count(w) for w in low_words) / 10 + 0.08 * prior[c["doc"].path]
        picked, seen, dup, used = [], set(), 0, 0
        for c in sorted(chunks, key=lambda c: -c["score"]):
            key = re.sub(r"\s+", "", c["text"])
            if key in seen or (vecs and any(cos(c["vec"], p["vec"]) >= 0.97 for p in picked)):
                dup += 1
                continue
            if len(picked) >= GUIDE_CHUNKS or used + len(c["text"]) > GUIDE_CONTEXT_CHARS:
                continue
            seen.add(key)
            picked.append(c)
            used += len(c["text"])
        full = sum(len(d.text) for d, _ in sources)
        s.set(chunks=len(chunks), selected=len(picked), duplicates=dup, embeddings="on" if vecs else "off",
              new_chunk_vectors=getattr(INDEX.embeddings, "chunk_new", 0),
              chars_full=full, chars_selected=used, reduction=f"{100 - round(100 * used / full) if full else 0}%",
              docs_used=len({c["doc"].path for c in picked}))
        return picked


def web_search_reason(sources, query: str, option: dict | None = None) -> str | None:
    """위키 근거가 약해 웹 검색이 필요하면 그 이유를, 아니면 None 을 돌려준다.
    2026-10-20 측정: 위키에 있는 질의 5개는 최고 유사도 0.587~0.648, 없는 질의 5개는 0.248~0.463 이라 0.5 를 기준으로 둔다.
    유형(포워드/리버스 등)을 골랐으면 유형 이름만으로도 비교한다. 주제(프록시)는 위키에 많아도 그 유형은 없을 수 있다.
    2026-10-23 측정: '포워드 프록시' 0.499, '리버스 프록시' 0.412(위키 1,026건 중 리버스 언급 2건) 라 0.45 를 기준으로 둔다."""
    limit = float(os.environ.get("WEB_SEARCH_MIN_SIM", "0.5"))
    with span("web_decision", enabled=bool(os.environ.get("TAVILY_API_KEY")), threshold=limit) as s:
        if not os.environ.get("TAVILY_API_KEY"):
            s.set(result="꺼짐 (TAVILY_API_KEY 없음)")
            return None
        if not sources:
            s.set(result="웹 검색", reason="관련 위키 문서 없음")
            return "관련 위키 문서 없음"
        sims = INDEX.embeddings.similarity(query)
        best = max(sims) if sims else None
        s.set(best_sim=round(best, 3) if best is not None else None)
        if best is not None and best < limit:
            s.set(result="웹 검색")
            return f"위키 최고 유사도 {best:.2f} < {limit}"
        if option:
            v_limit = float(os.environ.get("WEB_SEARCH_MIN_VARIANT_SIM", "0.45"))
            v_sims = INDEX.embeddings.similarity(option["label"])
            v_best = max(v_sims) if v_sims else None
            s.set(variant=option["label"], variant_sim=round(v_best, 3) if v_best is not None else None,
                  variant_threshold=v_limit)
            if v_best is not None and v_best < v_limit:
                s.set(result="웹 검색")
                return f"위키에 '{option['label']}' 내용 부족 (유형 유사도 {v_best:.2f} < {v_limit})"
        s.set(result="위키만 사용")
        return None


def request_web_reason(request: str, title: str) -> tuple[str | None, str | None, list[str]]:
    """수정 요청이 위키 밖 사실을 물으면 (이유, 웹 검색어, 필수어)를, 아니면 (None, None, []) 를 돌려준다.
    2026-10-26 측정: 요청의 위키 최고 유사도가 'sk 이노베이션에 출근하려면' 0.39, '3단계를 더 자세히' 0.40,
    'nginx 예시 추가' 0.36 이고 요청 단어의 위키 문서 수도 '출근'·'자세히' 모두 0건이라, 유사도·단어로는 서식 요청과
    가를 수 없다. 그래서 유사도가 기준 미만일 때만 LLM 에 웹에서 찾을 사실이 있는지 묻고 검색어를 받는다."""
    limit = float(os.environ.get("WEB_SEARCH_MIN_SIM", "0.5"))
    with span("web_decision_request", request=request[:80], threshold=limit) as s:
        if not os.environ.get("TAVILY_API_KEY"):
            s.set(result="꺼짐 (TAVILY_API_KEY 없음)")
            return None, None, []
        sims = INDEX.embeddings.similarity(request)
        best = max(sims) if sims else None
        s.set(best_sim=round(best, 3) if best is not None else None)
        if best is not None and best >= limit:
            s.set(result="위키만 사용")
            return None, None, []
        client = claude_client() or openai_client()
        if not client:
            s.set(result="위키만 사용 (LLM 설정 없음)")
            return None, None, []
        prompt = (f"가이드 제목: {title}\n수정 요청: {request}\n\n"
                  "이 요청을 반영하려면 사내 위키에 없을 외부의 사실 정보(회사·기관의 주소나 위치, 공개된 신청 절차, "
                  "제품 정보 등)를 웹에서 찾아야 하는가?\n"
                  "- 필요하면 '검색어 | 필수어' 한 줄만 출력하라. 검색어는 웹에 공개돼 있을 사실 하나만 찾는 짧은 말"
                  "(예: 'SK이노베이션 본사 주소')로, 출입 절차·신규 입사자 안내처럼 회사 내부에만 있을 내용은 넣지 마라. "
                  "필수어는 결과에 꼭 있어야 할 고유명사(예: 'SK이노베이션')다.\n"
                  "- 요청 대부분이 사내 절차여도 일부에 웹에서 확인할 공개 사실(예: 회사 위치)이 있으면 검색어를 출력하라.\n"
                  "- 문장 다듬기·서식·상세화·순서 변경처럼 지금 근거만으로 고칠 수 있으면 NONE 만 출력하라.")
        try:
            if hasattr(client, "chat"):
                res = client.chat.completions.create(model=llm_model(client),
                                                     messages=[{"role": "user", "content": prompt}])
                text = res.choices[0].message.content or ""
            else:
                res = client.messages.create(model=llm_model(client), max_tokens=200,
                                             messages=[{"role": "user", "content": prompt}])
                text = "".join(b.text for b in res.content if b.type == "text")
        except Exception as exc:
            s.fail(f"LLM 판단 실패 → 위키만 사용: {exc}")
            return None, None, []
        line = (text.strip().splitlines() or [""])[0].strip().strip("\"'`")
        query, _, must = (p.strip().strip("\"'`") for p in line.partition("|"))
        if not query or query.upper() == "NONE":
            s.set(result="위키만 사용 (LLM: 웹 불필요)")
            return None, None, []
        s.set(result="웹 검색", query=query[:100], must=must)
        return f"수정 요청이 위키 밖 내용 (유사도 {best:.2f} < {limit})", query[:100], [must] if must else []


def web_query(concepts: list[str], terms: list[str], option: dict | None, base: str | None = None) -> str:
    """웹 검색어: 유형 이름 + 질의의 고유어 + 개념의 한글 대표어(예: 리버스 프록시 장애). '알려줘' 같은 말투와
    '가이드' 같은 일반어, 영문 동의어는 넣지 않는다. 2026-10-23 Tavily 비교: 섞으면 게임·주식·코인 기사가 걸렸다."""
    words: list[str] = [option["label"]] if option else []
    words += terms
    words += [CONCEPTS[c][0] for c in concepts if c not in WEB_GENERIC and not (option and c == base)]
    seen: list[str] = []
    for w in " ".join(words).split():
        if w.lower() not in (s.lower() for s in seen):
            seen.append(w)
    return " ".join(seen)


def web_must(option: dict | None, base: str | None, core: list[str]) -> list[str]:
    """웹 결과에 하나라도 있어야 남기는 문구. 유형이면 붙은 문구 그대로(리버스 프록시, reverse proxy)."""
    if option and base:
        eng = next((s for s in option["detect"] if s.isascii()), "")
        return [option["label"], f"{eng} {next((s for s in CONCEPTS[base] if s.isascii()), '')}".strip()]
    return core


def web_search(query: str, must: list[str] = ()) -> list[dict]:
    """Tavily 검색 결과(제목·URL·본문 요약)를 돌려준다. must(핵심 개념 동의어)가 하나도 없는 결과는 버린다.
    실패하면 빈 목록(가이드는 위키만으로 만든다)."""
    body = {"query": query, "search_depth": "basic", "max_results": int(os.environ.get("WEB_SEARCH_MAX", "5"))}
    if not os.environ.get("TAVILY_API_KEY"):
        return []
    req = urllib.request.Request("https://api.tavily.com/search", json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json",
                                  "Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"})
    with span("web_search", provider="tavily", query=query, max_results=body["max_results"]) as s:
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                results = json.load(res).get("results", [])
        except Exception as exc:
            s.fail(f"웹 검색 실패 → 위키만 사용: {exc}")
            return []
        found = [{"title": r.get("title") or r["url"], "url": r["url"], "content": (r.get("content") or "")[:2000]}
                 for r in results if r.get("url")]
        # 공백은 빼고 비교한다('SK 이노베이션' = 'SK이노베이션').
        flat = lambda t: re.sub(r"\s+", "", t.lower())
        kept = [w for w in found if not must or any(flat(m) in flat(w["title"] + " " + w["content"]) for m in must)]
        s.set(results=len(kept), dropped=len(found) - len(kept), domains=sorted({urlparse(w["url"]).netloc for w in kept})[:5])
        return kept


def llm_model(client) -> str:
    return (os.environ.get("OPENAI_MODEL", "gpt-6.1-sol") if hasattr(client, "chat")
            else os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"))


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


def llm_guide(client, query, title, option, statements, sources, web=(), chunks=None, revise=None):
    """LLM 이 쓰는 가이드를 완성된 줄 단위로 낸다(다이어그램 자리표시자는 원문으로 되돌린 뒤). 최종 Markdown 은 return 값, 실패하면 None."""
    # mermaid 다이어그램은 LLM 이 글로 풀어 쓰거나 고치지 않도록 자리표시자로 바꿔 넘기고, 응답에서 원문으로 되돌린다.
    diagrams: list[str] = []

    def stash(m: re.Match) -> str:
        diagrams.append(m.group(0))
        labels = [re.sub(r"<br\s*/?>", " ", x) for x in re.findall(r'\["([^"]+)"\]', m.group(0))[:6]]
        return f"[[DIAGRAM-{len(diagrams)}]] (mermaid 다이어그램: {', '.join(labels)})"

    # 질의와 가까운 조각만 넘긴다(select_chunks). 조각은 문서별로 묶고 문서 안 원래 순서대로 둔다.
    rank = {d.path: i for i, (d, _) in enumerate(sources, 1)}
    by_doc: dict[str, list[dict]] = {}
    for c in chunks or []:
        by_doc.setdefault(c["doc"].path, []).append(c)
    docs = "\n\n".join(
        f'<document rank="{rank[path]}" path="{path}" title="{cs[0]["doc"].title}">\n'
        + "\n\n".join(f'<section heading="{c["heading"]}">\n{MERMAID.sub(stash, c["text"])}\n</section>'
                       for c in sorted(cs, key=lambda c: c["order"]))
        + "\n</document>"
        for path, cs in sorted(by_doc.items(), key=lambda kv: rank[kv[0]]))
    webs = "\n\n".join(f'<web rank="{i}" url="{w["url"]}" title="{w["title"]}">\n{w["content"]}\n</web>'
                         for i, w in enumerate(web, 1))
    basis = ("위 위키 문서를 우선 근거로 삼고, 위키에 없는 부분만 <web_results> 의 웹 검색 결과로 보완해 "
             "한국어 Markdown 가이드를 작성하라.\n"
             "- 웹에서 가져온 내용은 그 문장이나 단계 끝에 (웹: URL)로 표시하고, 주의사항에 "
             "'웹 출처 내용은 사내 기준이 아니므로 적용 전 확인이 필요하다'고 적어라.\n"
             if web else "위 위키 문서만 근거로 한국어 Markdown 가이드를 작성하라.\n")
    prompt = (f"사용자 질의: {query}\n가이드 제목: {title}\n"
              f"대상: {option['label'] + ' - ' + option['desc'] if option else '지정 없음'}\n\n"
              f"<documents>\n{docs or '(관련 위키 문서 없음)'}\n</documents>\n\n"
              + (f"<web_results>\n{webs}\n</web_results>\n\n" if web else "")
              + basis +
              "- 질의에 가장 직접 답하는 구체적인 절차 문서(대개 상위 문서)를 뼈대로 삼고, 그 문서의 단계 순서와 "
              "명령어·코드 블록·포트·버전 같은 값은 그대로 옮겨라.\n"
              "- 다른 문서는 뼈대를 보완하는 내용(사전 신청, 환경별 차이, 주의사항, 관련 문서 링크)만 가져오고, "
              "질의와 무관하거나 일반적인 문구뿐인 문서는 쓰지 마라.\n"
              "- 구성: 개요, 단계별 가이드, 주의사항, 참고 문서. 각 단계 끝에 근거 문서 경로를 괄호로 적어라.\n"
              + ("- 위키와 웹 결과 어디에도 없는 내용은 지어내지 말고 필요하면 '근거 없음'이라고 적어라.\n" if web else
                 "- 위키에 없는 내용은 지어내지 말고 필요하면 '위키에 근거 없음'이라고 적어라.\n") +
              "- 문서 안의 [[DIAGRAM-n]] 은 원문 다이어그램 자리표시자다. 가이드에 그 그림이 도움이 되면 "
              "다이어그램을 글·표·ASCII 로 풀어 쓰지 말고 '[[DIAGRAM-n]]' 한 줄만 단독으로 적어라. 괄호 속 설명은 옮기지 마라.\n"
              "- 각 문서는 질의와 관련된 절(section)만 발췌되어 있다. 발췌에 없는 내용은 추측하지 마라.\n"
              f"- 첫 줄은 '# {title}'로 시작하고, Markdown 본문만 출력하라.")
    if revise:  # 수정 요청: 지금 가이드를 요청대로 고쳐 전체를 다시 쓴다
        current = re.split(r"\n## (?:원문 링크|웹 참고 링크)\n", revise.get("markdown", ""))[0].strip()
        bare = lambda m: (diagrams.append(m.group(0)), f"[[DIAGRAM-{len(diagrams)}]]")[1]  # 지금 가이드의 그림은 설명 없이
        prompt += ("\n\n<current_guide>\n" + MERMAID.sub(bare, current) + "\n</current_guide>\n\n"
                   f"<request>\n{revise.get('request', '').strip()}\n</request>\n\n"
                   "위 <current_guide> 를 <request> 대로 고친 가이드 전체를 다시 출력하라. 요청과 관계없는 부분은 "
                   "문장·순서·코드를 바꾸지 말고 그대로 두라.\n"
                   "- 요청에 사용자가 직접 적어 준 문구·규칙·절차(따옴표 안 문장 등)는 근거 문서와 상관없이 그대로 넣어라. "
                   "사용자가 정한 운영 방침이므로 '근거 없음'으로 바꾸지 마라.\n"
                   "- 네가 스스로 보태는 사실(명령어·설정값·경로·절차)은 위 문서" + ("·웹 결과" if web else "")
                   + " 근거 범위 안에서만 쓰고, "
                   "근거가 없으면 그 자리에 '근거 없음'이라고 적어라.\n"
                   "- '원문 링크'·'웹 참고 링크' 절은 쓰지 마라(자동으로 붙는다).")
    system = "너는 사내 위키 문서를 근거로 운영 가이드를 작성하는 기술 문서 작성자다."
    with span("llm_generate", model=llm_model(client), docs=len(by_doc), context_chunks=len(chunks or []), web=len(web),
              diagrams=len(diagrams), revise=bool(revise), prompt_chars=len(system) + len(prompt)) as s:
        result = yield from _llm_run(client, system, prompt, diagrams, sources, web, s, chunks)  # chunks → context
    return result


def _llm_run(client, system, prompt, diagrams, sources, web, s, context=None):
    """llm_guide 의 호출부. s(llm_generate 노드)에 첫 응답 시각, 줄 수, 토큰 사용량, 실패 이유를 남긴다."""
    text, pending = "", ""
    started, first, n_lines = time.perf_counter(), None, 0

    def lines(chunk: str):
        # 자리표시자가 한 줄에 단독으로 오므로, 줄이 끝난 부분만 복원해 내보낸다.
        nonlocal pending, first, n_lines
        pending += chunk
        cut = pending.rfind("\n") + 1
        if cut:
            done, pending = pending[:cut], pending[cut:]
            if first is None:
                first = (time.perf_counter() - started) * 1000
            n_lines += done.count("\n")
            yield unbox(DIAGRAM_TOKEN.sub(lambda m: restore(diagrams, m), done))

    # 게이트웨이가 응답을 모아 한 번에 보내므로 그동안 로그가 멈춰 보인다 → 5초마다 진행 줄을 남긴다
    stop_beat = heartbeat(s, lambda: f"받은 글자 {len(text):,}" if text else "LLM 응답 기다리는 중")
    try:
        if hasattr(client, "chat"):  # OpenAI / Azure OpenAI 게이트웨이
            stream = client.chat.completions.create(
                model=llm_model(client), stream=True, stream_options={"include_usage": True},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            chunks, finish = 0, None
            for chunk in stream:
                if getattr(chunk, "usage", None):  # include_usage: 마지막 조각에 토큰 사용량이 온다
                    u = chunk.usage
                    s.set(prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens,
                          reasoning_tokens=getattr(u.completion_tokens_details, "reasoning_tokens", None))
                if chunk.choices:
                    finish = chunk.choices[0].finish_reason or finish
                piece = chunk.choices[0].delta.content if chunk.choices else None
                if piece:
                    chunks += 1
                    text += piece
                    yield from lines(piece)
            s.set(chunks=chunks, finish=finish)
            if pending:
                yield from lines("\n")
            text = text.strip()
        else:  # Anthropic
            model = llm_model(client)
            # 안전 분류기가 거절하면 서버가 다른 모델로 이어서 답하게 한다(지원 모델만).
            extra = ({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
                     if model in {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"} else {"betas": []})
            res = client.beta.messages.create(
                model=model, max_tokens=16000, output_config={"effort": "medium"},
                system=system, messages=[{"role": "user", "content": prompt}], **extra)
            s.set(prompt_tokens=res.usage.input_tokens, completion_tokens=res.usage.output_tokens, finish=res.stop_reason)
            if res.stop_reason == "refusal":
                s.fail(f"LLM 거절 → 템플릿 사용: {res.stop_details}")
                return None
            text = "".join(b.text for b in res.content if b.type == "text").strip()
            yield from lines(text + "\n")
        text = unbox(DIAGRAM_TOKEN.sub(lambda m: restore(diagrams, m), text))
        s.set(first_line_ms=round(first) if first is not None else None, lines=n_lines, out_chars=len(text))
        if not text:
            s.fail("빈 응답 → 템플릿 사용")
            return None
        used = {c["doc"].path for c in context or []}
        cited = [(d, sc) for d, sc in sources if d.path in used] or sources
        if cited:
            text += "\n\n## 원문 링크\n\n" + "\n".join(f"- [{d.title}]({view_link(d.path)}) — {d.path}" for d, _ in cited)
        # 웹 결과는 본문에 (웹: URL)로 인용한 것만 붙인다. 쓰지 않은 결과(관련 없는 뉴스 등)까지 출처처럼 보이지 않게.
        used_web = [w for w in web if w["url"] in text]
        if used_web:
            text += "\n\n## 웹 참고 링크\n\n" + "\n".join(f"- [{w['title']}]({w['url']})" for w in used_web)
        s.set(web_cited=len(used_web))
        return text
    except Exception as exc:
        s.fail(f"LLM 호출 실패 → 템플릿 사용: {type(exc).__name__}: {exc}")
        return None
    finally:
        stop_beat()


BOX_NOTE = re.compile(r"[ 	]*\(mermaid 다이어그램:.*$", re.M)


def unbox(text: str) -> str:
    """LLM 이 자리표시자 옆의 '(mermaid 다이어그램: …)' 설명까지 옮겨 쓴 경우 지운다(그 줄의 나머지)."""
    return BOX_NOTE.sub("", text)


def restore(diagrams: list[str], m: re.Match) -> str:
    n = int(m.group(1) or m.group(2))
    return f"\n{diagrams[n - 1]}\n" if 0 < n <= len(diagrams) else ""


HTML_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
__FAVICON__<title>Wiki Guide Search</title><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css"><script defer src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script><style>
:root{--ink:#172033;--muted:#718096;--line:#e5eaf0;--blue:#2563eb;--soft:#f5f8fc;--panel:#fff;--hl:#fff3b0}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px/1.65 "Segoe UI","Malgun Gothic",sans-serif;background:#eef2f7}
.top{height:58px;background:#13213a;color:#fff;display:flex;align-items:center;padding:0 24px;gap:14px}.mark{width:28px;height:28px;border-radius:8px;background:#16a34a;display:grid;place-items:center;font-weight:800}.brand{font-size:16px;font-weight:700}.crumb{color:#aebbd0;font-size:12px}
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
.guide pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow-x:auto;font:12.5px/1.55 Consolas,"Cascadia Mono","D2Coding",monospace}.guide pre code{font:inherit}.guide pre code.hljs{background:none;padding:0}.guide .mermaid{margin:14px 0;text-align:center;overflow-x:auto}
.tools{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-bottom:8px;flex-wrap:wrap}.revise{margin-top:18px;border-top:1px solid var(--line);padding-top:12px}.revise textarea{width:100%;margin:6px 0;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;resize:vertical}.revbar{display:flex;gap:10px;align-items:center;justify-content:space-between;font-size:12px}.regok{background:#e3f5ec;color:#1e7a4f;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:13px}.regerr{background:#fdecec;color:#9b2c2c;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:13px}.gen{margin-right:auto;font-size:12px;font-weight:600;border-radius:999px;padding:4px 12px;background:#f1f5f9;color:#475569}.gen.llm{background:#dcfce7;color:#166534}.gen.live{background:#dbeafe;color:#1e40af}.tools[hidden]{display:none}.muted{color:var(--muted)}.empty{color:var(--muted);text-align:center;padding:40px 0}
@media(max-width:820px){.grid{grid-template-columns:1fr}.opts{grid-template-columns:1fr}}
</style></head><body>
<header class="top"><div class="mark">G</div><div class="brand">Wiki Guide Search</div><div class="crumb">wiki-repo 의미 검색 · 가이드 생성</div></header>
<div class="wrap">
<form class="search" id="form"><input id="q" placeholder="예: 프록시 설정 가이드 알려줘" autocomplete="off" autofocus><button id="go">검색</button></form>
<div class="steps"><span class="step" id="s1">1. 문서 검색</span><span class="step" id="s2">2. 유형 확인</span><span class="step" id="s3">3. 관련 문서 재검색</span><span class="step" id="s4">4. 가이드 생성</span></div>
<div class="grid"><section class="card"><h2>검색 결과</h2><div id="analysis"></div><div id="results"><div class="empty">질문을 입력하세요.</div></div></section>
<section><div id="ask"></div><div class="card" id="guideCard"><div class="tools" id="tools" hidden><span class="gen" id="gen"></span><button class="ghost" id="regen" title="같은 질의·유형으로 가이드를 다시 만든다">다시 생성</button><button class="ghost" id="copy">Markdown 복사</button><button class="ghost" id="dl">.md 다운로드</button><button id="reg" title="02. 운영 > 00. AI 문서 생성 아래에 등록">Wiki에 등록</button></div><div id="regResult"></div><div class="guide" id="guide"><div class="empty">검색 후 가이드가 여기에 생성됩니다.</div></div><div class="revise" id="revise" hidden><b>수정 요청</b> <span class="muted" id="revCount"></span><textarea id="revText" rows="2" placeholder="예: 3단계를 더 자세히 써 주세요 / nginx 예시를 추가해 주세요 / 주의사항을 표로 정리해 주세요"></textarea><div class="revbar"><span class="muted">지금 가이드와 요청을 함께 보내 고친 판을 만듭니다. 요청과 관계없는 부분은 그대로 둡니다.</span><button id="revBtn">수정 반영</button></div></div></div></section></div>
</div>
<script>
const $=s=>document.querySelector(s);let state={q:'',md:'',title:'guide'};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function hl(s,words){let x=esc(s);if(!words.length)return x;const re=new RegExp('('+words.map(w=>esc(w).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')','gi');return x.replace(re,'<mark>$1</mark>')}
function steps(n,done){for(let i=1;i<=4;i++){const e=$('#s'+i);e.className='step'+(i<n||done&&i===n?' done':i===n?' on':'')}}
function inline(s){let x=esc(s);x=x.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');x=x.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(m,t,u)=>`<a href="${u}" target="_blank" rel="noopener">${t}</a>`);return x.replace(/^\[ \] /,'☐ ')}
function md(src){let out='',list=false,table=[],code=null;const fl=()=>{if(list){out+='</ul>';list=false}if(table.length){const rows=table.filter(r=>!/^\|[\s|:-]+\|$/.test(r)).map(r=>r.replace(/^\||\|$/g,'').split('|').map(c=>c.trim()));out+='<table>'+rows.map((r,i)=>'<tr>'+r.map(c=>i?`<td>${inline(c)}</td>`:`<th>${inline(c)}</th>`).join('')+'</tr>').join('')+'</table>';table=[]}};
for(const raw of src.replace(/\r/g,'').split('\n')){const line=raw.trimEnd();let m;if(code){if(code.end.test(line)){const body=esc(code.lines.join('\n'));out+=code.mermaid?`<div class="mermaid">${body}</div>`:`<pre><code${code.lang?` class="language-${esc(code.lang)}"`:''}>${body}</code></pre>`;code=null}else code.lines.push(raw);continue}if(/^\s*:::\s*mermaid\s*$/.test(line)){fl();code={mermaid:true,end:/^\s*:::\s*$/,lines:[]};continue}if(m=line.match(/^\s*```\s*([\w-]*)/)){fl();code={mermaid:m[1]==='mermaid',lang:m[1],end:/^\s*```\s*$/,lines:[]};continue}if(line.startsWith('|')){if(list){out+='</ul>';list=false}table.push(line);continue}if(table.length)fl();
if(m=line.match(/^(#{1,4})\s+(.*)$/)){fl();out+=`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`}else if(m=line.match(/^\s{2,}(\S.*)$/)){out=out.replace(/<\/li>$/,'<br><span class="muted">'+inline(m[1])+'</span></li>')}else if(m=line.match(/^[-*]\s+(.*)$/)){if(!list){out+='<ul>';list=true}out+='<li>'+inline(m[1])+'</li>'}else if(m=line.match(/^>\s?(.*)$/)){fl();out+='<blockquote>'+inline(m[1])+'</blockquote>'}else if(line.trim()){fl();out+='<p>'+inline(line)+'</p>'}else if(!list)fl()}if(code)out+=`<pre><code>${esc(code.lines.join('\n'))}</code></pre>`;fl();return out}
// 완성된 가이드의 코드 블록 구문 강조(highlight.js, cdnjs). 언어가 없으면 자동 판별, 못 불러오면 그대로 둔다.
function highlightCode(){if(!window.hljs){setTimeout(()=>window.hljs&&highlightCode(),500);return}document.querySelectorAll('#guide pre code:not(.hljs)').forEach(el=>{try{hljs.highlightElement(el)}catch(e){}})}
let mermaidLib;async function diagrams(){const els=[...document.querySelectorAll('#guide .mermaid')];if(!els.length)return;try{mermaidLib=mermaidLib||(await import('https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs')).default;mermaidLib.initialize({startOnLoad:false,securityLevel:'strict'});await mermaidLib.run({nodes:els,suppressErrors:true})}catch(e){console.warn('mermaid',e)}els.filter(el=>!el.querySelector('svg')).forEach(el=>{el.outerHTML='<pre><code>'+esc(el.textContent)+'</code></pre><p class="muted" style="font-size:11px">다이어그램을 그리지 못해 원문을 표시합니다.</p>'})}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error(await r.text());return r.json()}
function showResults(d,label){const a=d.analysis;$('#analysis').innerHTML='<div class="chips">해석: '+a.concepts.map(c=>`<span class="chip" title="${esc(c.words.join(', '))}">${esc(c.words[0])} ≈ ${esc(c.words.slice(1).join(' · '))}</span>`).join('')+a.terms.map(t=>`<span class="chip">${esc(t)}</span>`).join('')+(a.fixes||[]).map(f=>`<span class="chip" style="background:#fef3c7;color:#92400e" title="오타로 보고 고쳐서 검색">오타 교정: ${esc(f.from)} → ${esc(f.to)}</span>`).join('')+(label?`<span class="chip" style="background:#d9f5e6;color:#11643c">${esc(label)}</span>`:'')+((d.web||[]).length?`<span class="chip" style="background:#fef3c7;color:#92400e" title="${esc(d.web_reason||'')}">웹 검색 ${d.web.length}건</span>`:'')+` <span>· ${d.results.length}건</span></div>`;
const web=(d.web||[]).length?`<h2 style="margin-top:18px">웹 검색 결과 <span class="muted" style="font-size:12px;font-weight:400">${esc(d.web_reason||'')}</span></h2>`+d.web.map(w=>`<div class="res"><a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.title)}</a><div class="p">${esc(w.url)}</div></div>`).join(''):'';
if(!d.results.length){$('#results').innerHTML='<div class="empty">관련 위키 문서가 없습니다.</div>'+web;return}const max=d.results[0].score||1;
$('#results').innerHTML=d.results.map(r=>`<div class="res"><a href="${esc(r.link)}" target="_blank" rel="noopener">${hl(r.title,d.highlights)}</a><div class="p">${esc(r.path)}</div>${r.snippets.map(s=>`<div class="s">${hl(s,d.highlights)}</div>`).join('')}<div class="bar"><i style="width:${Math.round(r.score/max*100)}%"></i></div></div>`).join('')+web}
async function search(q){state.q=q;steps(1);$('#ask').innerHTML='';$('#tools').hidden=true;$('#guide').innerHTML='<div class="empty">검색 중…</div>';
const d=await post('/api/search',{q});showResults(d);
if(d.clarify){steps(2);$('#guide').innerHTML='<div class="empty">위에서 유형을 선택하면 가이드를 생성합니다.</div>';
$('#ask').innerHTML=`<div class="card ask" style="margin-bottom:16px"><h2>${esc(d.clarify.question)}</h2><div class="opts">${d.clarify.options.map(o=>`<button class="opt" data-k="${esc(d.clarify.key)}" data-v="${esc(o.id)}"><b>${esc(o.label)}</b><span>${esc(o.desc)}</span></button>`).join('')}</div></div>`;
document.querySelectorAll('.opt').forEach(b=>b.onclick=()=>guide(b.dataset.k,b.dataset.v,b.querySelector('b').textContent))}
else guide(d.variant&&d.variant.key,d.variant&&d.variant.id,d.variant&&d.variant.label)}
async function guide(key,variant,label,revise){steps(3);Object.assign(state,{key,variant,label,done:null});state.rev=revise?(state.rev||0)+1:0;$('#regResult').innerHTML='';$('#revise').hidden=true;const prev=state.md;if(label)$('#ask').innerHTML=`<div class="card" style="margin-bottom:16px">선택한 유형: <b>${esc(label)}</b> <button class="ghost" style="float:right;padding:3px 10px" id="re">다시 선택</button></div>`;const re=$('#re');if(re)re.onclick=()=>search(state.q);
const t0=Date.now(),sec=()=>Math.floor((Date.now()-t0)/1000);
$('#guide').innerHTML='<div class="wait">'+(revise?'수정 요청을 반영하는 중… ('+esc(revise.request)+')':'관련 문서를 다시 검색해 가이드를 만드는 중…')+' <b id="elapsed">0초</b><div class="prog"><i></i></div></div>';
const tick=setInterval(()=>{const e=$('#elapsed');if(e)e.textContent=sec()+'초';const g=$('#gen');if(g.classList.contains('live'))g.textContent=g.textContent.replace(/\d+초$/,sec()+'초')},1000);
if(state.ctl)state.ctl.abort();const ctl=state.ctl=new AbortController();const g=$('#gen');let model=null,text='',drawn=0,timer=null;
const draw=()=>{timer=null;if(drawn!==text.length){drawn=text.length;$('#guide').innerHTML=md(text)}};
const on=e=>{if(e.type==='meta'){model=e.model;state.title=e.title;state.meta={sources:e.results.length,web:(e.web||[]).length};showResults(e,label?label+' 기준 재검색':'')}
else if(e.type==='delta'){if(!text){steps(4);g.className='gen live';g.textContent=`작성 중 · ${model||'LLM'} · ${sec()}초`;$('#tools').hidden=false}text+=e.text;if(!timer)timer=setTimeout(draw,120)}
else if(e.type==='done'){clearTimeout(timer);steps(4,true);if(e.revise_failed){state.rev--;$('#regResult').innerHTML='<div class="regerr">수정 요청을 반영하지 못해 이전 가이드를 그대로 보여 줍니다.</div>'}state.md=e.markdown;state.done=e;$('#revise').hidden=false;$('#revCount').textContent=state.rev?`(수정 ${state.rev}회 반영됨)`:'';$('#guide').innerHTML=md(e.markdown);g.className='gen'+(e.mode==='llm'?' llm':'');g.textContent=`생성 방식: ${e.mode==='llm'?'LLM 작성'+(e.model?' · '+e.model:''):'문서 조합 템플릿'} · 소요 ${sec()}초`;$('#tools').hidden=false;diagrams();highlightCode()}};
try{const r=await fetch('/api/guide/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:state.q,key,variant,revise}),signal:ctl.signal});if(!r.ok)throw new Error(await r.text());
const rd=r.body.getReader(),dec=new TextDecoder();let buf='';for(;;){const {value,done}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+1);if(line.trim())on(JSON.parse(line))}}}
catch(err){if(err.name==='AbortError')return;$('#guide').innerHTML='<div class="empty">오류: '+esc(err.message)+' ('+sec()+'초)</div>'}finally{clearInterval(tick)}}
$('#form').onsubmit=e=>{e.preventDefault();const q=$('#q').value.trim();if(q)search(q).catch(err=>{$('#guide').innerHTML='<div class="empty">오류: '+esc(err.message)+'</div>'})};
// 허브의 '시나리오 실행' 탭에서 ?q=질의 로 열면 바로 검색한다.
{const q0=new URLSearchParams(location.search).get('q');if(q0){$('#q').value=q0;search(q0).catch(err=>{$('#guide').innerHTML='<div class="empty">오류: '+esc(err.message)+'</div>'})}}
$('#copy').onclick=()=>navigator.clipboard.writeText(state.md).then(()=>{$('#copy').textContent='복사됨';setTimeout(()=>$('#copy').textContent='Markdown 복사',1200)});
$('#dl').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([state.md],{type:'text/markdown'}));a.download=state.title.replace(/\s+/g,'-')+'.md';a.click()};
$('#regen').onclick=()=>guide(state.key,state.variant,state.label);
// 7단계 수정 요청: 지금 가이드와 요청을 함께 보내 고친 판을 받는다. 결과는 그대로 'Wiki에 등록'할 수 있다.
$('#revBtn').onclick=()=>{const req=$('#revText').value.trim();if(!req||!state.done)return;$('#revText').value='';guide(state.key,state.variant,state.label,{markdown:state.md,request:req})};$('#revText').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))$('#revBtn').click()});
// 시나리오 1의 가벼운 승인: 미리보기를 확인한 사람이 눌러야 등록한다(같은 제목이면 갱신).
$('#reg').onclick=async()=>{if(!state.done)return;if(!confirm(`'${state.title}'을(를) Azure DevOps Wiki의 '02. 운영 > 00. AI 문서 생성' 아래에 등록할까요?\n팀원에게 공개되며, 같은 제목이 있으면 갱신됩니다.`))return;
const b=$('#reg'),r=$('#regResult');b.disabled=true;b.textContent='등록 중…';r.innerHTML='';
try{const d=await post('/api/register',{q:state.q,title:state.title,markdown:state.md,model:state.done.model,mode:state.done.mode,sources:(state.meta||{}).sources||0,web:(state.meta||{}).web||0});
if(d.error)throw new Error(d.error);
r.innerHTML=`<div class="regok">${d.updated?'갱신':'등록'}했습니다: <b>${esc(d.title)}</b> (커밋 ${esc(d.commit)}${d.pushed?', Azure DevOps Wiki 반영':''}) · <a href="${esc(d.url)}" target="_blank" rel="noopener">Wiki에서 열기</a> · <a href="${esc(d.local)}" target="_blank" rel="noopener">로컬 뷰어</a>${d.warnings.length?'<br>확인: '+esc(d.warnings.join(', ')):''}</div>`;b.textContent='등록됨'}
catch(err){r.innerHTML='<div class="regerr">등록 실패: '+esc(err.message)+'</div>';b.disabled=false;b.textContent='Wiki에 등록'}};
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_stream(self, events, t=None):
        """이벤트를 한 줄에 JSON 하나(NDJSON)로 바로바로 보낸다. 길이를 모르므로 연결을 닫아 끝을 알린다."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in events:
                if t and event["type"] == "done":
                    t.set(mode=event["mode"], model=event["model"], chars=len(event["markdown"]))
                self.wfile.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if t:
                t.set(disconnected=True)  # 새 검색을 해서 브라우저가 이전 요청을 끊은 경우
        finally:
            events.close()  # 끊긴 경우에도 열린 노드를 지금 닫아 cancelled 로 남긴다
        self.close_connection = True

    def do_GET(self):
        if urlparse(self.path).path != "/":
            self.send_error(404); return
        data = HTML_PAGE.replace("__FAVICON__", favicon("G", "#16a34a")).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            q = str(body.get("q", "")).strip()[:500]
            if not q:
                self.send_json({"error": "empty query"}, 400); return
            path = urlparse(self.path).path
            if path == "/api/register":
                self.send_json(do_register(q, body)); return
            if path not in ("/api/search", "/api/guide", "/api/guide/stream"):
                self.send_error(404); return
            name = path.removeprefix("/api/")
            with trace("wiki-search", name, q=q, key=body.get("key"), variant=body.get("variant")) as t:
                refresh_index()
                if path == "/api/search":
                    d = do_search(q)
                    t.set(results=len(d["results"]), clarify=bool(d["clarify"]))
                    self.send_json(d); return
                if path == "/api/guide":
                    d = do_guide(q, body.get("key"), body.get("variant"))
                    t.set(mode=d.get("mode"), model=d.get("model"), chars=len(d.get("markdown") or ""))
                    self.send_json(d); return
                revise = body.get("revise") if isinstance(body.get("revise"), dict) else None
                if revise:
                    t.set(revise=str(revise.get("request", ""))[:80])
                self.send_stream(guide_events(q, body.get("key"), body.get("variant"), revise), t)
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "bad request"}, 400)

    def log_message(self, *_):
        pass


INDEX: Index
_index_sig: str | None = None
_index_lock = threading.Lock()


def do_register(q: str, body: dict) -> dict:
    """시나리오 1 5·6단계: 사용자가 미리보기를 보고 'Wiki에 등록'을 누르면 02. 운영 > 00. AI 문서 생성 아래에 등록한다."""
    from wiki_register import register
    title, markdown = str(body.get("title", "")).strip(), str(body.get("markdown", ""))
    if not title or not markdown.strip():
        return {"error": "등록할 가이드가 없습니다. 먼저 가이드를 생성하세요."}
    with trace("wiki-search", "register", q=q, title=title) as t, span("wiki_register", chars=len(markdown)) as s:
        try:
            r = register(title, markdown, q, body.get("model") or ("템플릿" if body.get("mode") == "template" else None),
                         int(body.get("sources") or 0), int(body.get("web") or 0),
                         push=os.environ.get("WIKI_REGISTER_PUSH", "1") != "0")
        except Exception as exc:
            s.fail(f"{type(exc).__name__}: {exc}")
            return {"error": str(exc)}
        r["local"] = view_link(r["path"].removesuffix(".md"))
        s.set(number=r["number"], updated=r["updated"], commit=r["commit"], pushed=r["pushed"])
        t.set(path=r["path"])
    return r


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
    with _index_lock, span("refresh_index") as s:
        sig = wiki_signature(ROOT)
        if sig == _index_sig:
            s.set(changed=False, docs=len(INDEX.docs))
            return
        INDEX = Index(ROOT)
        _index_sig = sig
        s.set(changed=True, docs=len(INDEX.docs))


def main():
    global INDEX, _index_sig
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8766
    if not ROOT.is_dir(): raise SystemExit(f"Wiki root not found: {ROOT}")
    print(f"Indexing {ROOT} ...", flush=True)
    INDEX = Index(ROOT)
    _index_sig = wiki_signature(ROOT)
    print(f"Indexed {len(INDEX.docs)} documents from {ROOT}")
    if INDEX.embeddings.vectors:
        dims = len(INDEX.embeddings.vectors[0])
        print(f"Embeddings: on model={INDEX.embeddings.model} dims={dims} "
              f"cache_reused={INDEX.embeddings.cache_reused} cache_new={INDEX.embeddings.cache_new}")
    else:
        print(f"Embeddings: off model={INDEX.embeddings.model} (offline semantic search)")
    server = local_server(port, Handler, "wiki-search.local")
    print(f"Wiki guide search: {server.url}")
    if "--no-browser" not in sys.argv:
        open_chrome(server.url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
