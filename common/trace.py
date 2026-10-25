"""요청 하나를 노드(단계)별로 기록하는 가벼운 트레이서. 외부 패키지 없이 콘솔과 JSONL 파일에 남긴다.

    with trace("wiki-search", "guide", q="프록시 설정") as t:     # 요청 하나
        with span("hybrid_search", docs=1274) as s:              # 노드 하나
            ...
            s.set(results=20, top=8.63)                           # 결과 값
        t.set(mode="llm")                                         # 요청 전체 결과

- 노드가 끝날 때마다 콘솔에 한 줄: `01:23:46 [a1b2c3d4]   03 hybrid_search   412ms  results=20 top=8.63`
  (중첩 노드는 들여쓰기. 끝날 때 찍으므로 자식이 부모보다 먼저 나온다. 번호는 시작 순서.)
- 요청이 끝나면 노드별 소요 시간 요약 한 줄과, data/traces/<service>-YYYYMMDD.jsonl 에 상세 기록 한 줄을 남긴다.
- 예외는 status=error 로 기록하고 다시 던진다. 잡아서 처리한 실패는 s.fail("이유") 로 남긴다.
  연결이 끊겨 생성기가 닫히면(GeneratorExit) status=cancelled.
- 현재 트레이스는 contextvars 로 찾으므로 인자로 넘기지 않는다. 트레이스 밖의 span() 은 기록하지 않고,
  fail() 메시지만 콘솔에 찍는다(서버 시작 중 오류 등).
- 오래된 JSONL 은 TRACE_KEEP_DAYS(기본 14)일이 지나면 지운다. 질의 원문이 들어가므로 data/(Git 제외)에만 둔다.

에이전트 추론 로그(노드 줄과 따로, ◆ 로 시작). 노드 줄이 '무엇을 얼마나 걸려 했나'라면 이 줄은 '왜 그렇게 했나'다.

    plan("장애 원인 분석·조치", [("collect", "로그 수집·규칙 탐지"), ("llm_fix", "수정안 생성")])  # Planning: 2-step plan generated
    simulate([("LLM", True, "gpt-6.1-sol"), ("Teams", False, "로컬 승인 화면으로 대체")])   # Simulating plan… 사전 점검
    step("collect")                                     # Step 1/2 collect — 로그 수집·규칙 탐지
    think("Tool Call", "ssh.read_logs(vm=wjy-proxy01)")  # Tool Call / Observation / Self-Correction / Decision / Reflection
    replan("자동 수정안 없음 → PR 단계 제외", drop=["pull_request"])   # Replan: 계획 수정 후 남은 단계

  JSONL 에는 plan(단계 목록·실행 여부)과 reasoning(시각·종류·내용)으로 남는다.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from common.paths import DATA_DIR

TRACE_DIR = DATA_DIR / "traces"
_current: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("trace", default=None)
_write_lock = threading.Lock()
_cleaned: set[str] = set()  # 오늘 이미 오래된 파일을 정리한 서비스


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") if abs(value) < 100 else f"{value:,.0f}"
    if isinstance(value, (list, tuple)):
        value = ",".join(map(str, value))
    text = str(value).replace("\n", " ")
    return text if len(text) <= 60 else text[:57] + "…"


def _pad(text: str, width: int) -> str:
    """화면 폭 기준으로 오른쪽을 채운다(한글 등 전각 문자는 2칸). ljust 는 글자 수로 세어 한글 줄이 밀린다."""
    used = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(width - used, 0)


def _attrs(attrs: dict) -> str:
    return " ".join(f"{k}={_fmt(v)}" for k, v in attrs.items() if v not in (None, "", [], ()))


class Span:
    def __init__(self, trace: Trace, name: str, attrs: dict):
        self.trace, self.name, self.attrs = trace, name, dict(attrs)
        self.no = len(trace.spans) + 1
        self.depth = len(trace.stack)
        self.start = time.perf_counter()
        self.ms: float | None = None
        self.status, self.error = "ok", None
        self.opened = False  # 시작 줄을 찍었는지(하위 노드가 있을 때만 찍는다)

    def set(self, **attrs) -> Span:
        self.attrs.update(attrs)
        return self

    def fail(self, error: str) -> Span:
        self.status, self.error = "error", error
        return self

    def record(self) -> dict:
        return {"no": self.no, "name": self.name, "depth": self.depth,
                "offset_ms": round((self.start - self.trace.t0) * 1000, 1), "ms": round(self.ms or 0, 1),
                "status": self.status, "error": self.error, "attrs": self.attrs}


class _NullSpan:
    """트레이스 밖에서 쓰는 빈 노드. 실패 메시지만 콘솔에 남긴다."""

    def set(self, **_):
        return self

    def fail(self, error: str):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [-] {error}", flush=True)
        return self


class Trace:
    def __init__(self, service: str, name: str, attrs: dict):
        self.service, self.name, self.attrs = service, name, dict(attrs)
        self.id = uuid.uuid4().hex[:8]
        self.t0 = time.perf_counter()
        self.started = datetime.now()
        self.spans: list[Span] = []
        self.stack: list[Span] = []
        self.status, self.error = "ok", None
        self.plan: list[dict] = []        # [{name, desc, status: planned|running|done|skipped}]
        self.reasoning: list[dict] = []   # 추론 로그(◆ 줄)

    def set(self, **attrs) -> Trace:
        self.attrs.update(attrs)
        return self

    def log(self, text: str):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{self.id}] {text}", flush=True)  # 서버가 며칠씩 돌아도 날짜를 알 수 있게


def _row(mark: str, s: Span, value: str) -> str:
    """노드 한 줄의 앞부분. 들여쓰기(깊이)까지 포함해 이름 칸을 고정 폭으로 맞춰 시간 칸이 세로로 정렬되게 한다."""
    return f"{mark} " + f"{'  ' * s.depth}{s.no:02d} {s.name}".ljust(26) + f"{value:>10}"


def heartbeat(s, detail=None, every: float = 5.0):
    """오래 걸리는 노드가 도는 동안 every 초마다 '진행 중' 줄을 남긴다(로그가 멈춘 것처럼 보이지 않게).
    돌려준 함수를 부르면 멈춘다. 트레이스 밖(_NullSpan)이면 아무것도 하지 않는다."""
    if not isinstance(s, Span):
        return lambda: None
    stop = threading.Event()

    def beat():
        while not stop.wait(every):
            extra = detail() if detail else ""
            s.trace.log(_row("↻", s, f"{time.perf_counter() - s.start:.0f}s") + "  진행 중"
                        + (f" · {extra}" if extra else ""))

    threading.Thread(target=beat, daemon=True, name=f"heartbeat-{s.name}").start()
    return stop.set


@contextmanager
def span(name: str, **attrs):
    t = _current.get()
    if t is None:
        yield _NullSpan()
        return
    s = Span(t, name, attrs)
    parent = t.stack[-1] if t.stack else None
    if parent is not None and not parent.opened:
        # 하위 노드가 생기면 상위의 시작 줄을 먼저 찍는다. 끝 줄만 있으면 하위가 상위보다 위에 찍혀 앞 노드의 하위처럼 보인다.
        parent.opened = True
        t.log(_row("▸", parent, "") + "  시작 (하위 단계는 아래)")
    t.spans.append(s)
    t.stack.append(s)
    try:
        yield s
    except GeneratorExit:
        s.status = "cancelled"
        raise
    except BaseException as exc:
        s.fail(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        t.stack.pop()
        s.ms = (time.perf_counter() - s.start) * 1000
        mark = {"ok": " ", "error": "✗", "cancelled": "…"}[s.status]
        tail = _attrs(s.attrs) + (f"  error={_fmt(s.error)}" if s.error else "")
        t.log(_row(mark, s, f"{s.ms:,.0f}ms") + f"  {tail}")


@contextmanager
def trace(service: str, name: str, **attrs):
    t = Trace(service, name, attrs)
    token = _current.set(t)
    t.log(f"▶ {name}  {_attrs(attrs)}")
    try:
        yield t
    except BaseException as exc:
        t.status, t.error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _current.reset(token)
        for p in t.plan:
            if p["status"] == "running":
                p["status"] = "done" if t.status == "ok" else "error"
        total = (time.perf_counter() - t.t0) * 1000
        if t.status == "ok" and any(s.status == "cancelled" for s in t.spans):
            t.status = "cancelled"
        top = sorted((s for s in t.spans if s.depth == 0), key=lambda s: -(s.ms or 0))[:4]
        nodes = " · ".join(f"{s.name} {(s.ms or 0) / 1000:.1f}s" for s in top)
        errors = [f"{s.name}: {s.error}" for s in t.spans if s.error]
        t.log(f"■ {name} {t.status} {total / 1000:.1f}s | {nodes}"
              + (f" | {_attrs(t.attrs)}" if t.attrs else "") + (f" | 오류 {'; '.join(errors)}" if errors else ""))
        _write(t, total)


def think(kind: str, text: str, **attrs):
    """추론 로그 한 줄: `◆ Self-Correction   수정안 검증 실패 → 이유를 돌려주고 다시 생성`. 트레이스 밖이면 그냥 찍는다."""
    t = _current.get()
    line = f"◆ {kind:<16} {text}" + (f"  {_attrs(attrs)}" if _attrs(attrs) else "")
    if t is None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [-] {line}", flush=True)
        return
    t.reasoning.append({"offset_ms": round((time.perf_counter() - t.t0) * 1000, 1), "kind": kind, "text": text,
                        "attrs": attrs or None})
    t.log(line)


def plan(goal: str, steps: list[tuple[str, str]]):
    """실행 계획을 세워 남긴다. steps=[(노드 이름, 설명)]. 계획은 실행 조건(dry-run, 설정)에 따라 호출하는 쪽이 만든다."""
    t = _current.get()
    if t is not None:
        t.plan = [{"name": n, "desc": d, "status": "planned"} for n, d in steps]
    think("Planning", f"{len(steps)}-step plan generated · 목표: {goal}")
    for i, (n, d) in enumerate(steps, 1):
        think("Planning", f"  {i}. {_pad(n, 14)} {d}")


def simulate(checks: list[tuple[str, bool, str]]) -> list[str]:
    """계획을 실행하기 전에 각 단계의 전제(설정·저장소·채널)를 점검한다. checks=[(항목, 통과, 설명)].
    통과하지 못한 항목 이름을 돌려준다(대체 경로로 가거나 멈추는 판단은 호출하는 쪽이 한다)."""
    t = _current.get()
    n = len(t.plan) if t else 0
    think("Simulation", f"Simulating plan… {n}단계 실행 전 사전 점검 {len(checks)}항목")
    for name, ok, note in checks:
        think("Simulation", f"  {'✓' if ok else '△'} {_pad(name, 14)} {note}")
    failed = [name for name, ok, _ in checks if not ok]
    think("Simulation", "시뮬레이션 결과: " + ("모든 단계 실행 가능" if not failed
                                                else f"{len(failed)}항목은 대체 경로로 진행 ({', '.join(failed)})"))
    return failed


def step(name: str, note: str = ""):
    """계획의 단계를 시작한다: `◆ Step 3/5        similar — Wiki 유사 사례 검색`. 앞 단계는 끝난 것으로 본다."""
    t = _current.get()
    if t is None or not t.plan:
        think("Step", name + (f" — {note}" if note else ""))
        return
    active = [p for p in t.plan if p["status"] != "skipped"]
    for p in active:
        if p["status"] == "running":
            p["status"] = "done"
    i = next((k for k, p in enumerate(active, 1) if p["name"] == name), None)
    if i is None:  # 계획에 없던 단계(재계획으로 끼어든 단계 등)
        t.plan.append({"name": name, "desc": note, "status": "running"})
        think("Step", f"+ {name} — {note}")
        return
    active[i - 1]["status"] = "running"
    think(f"Step {i}/{len(active)}", f"{name} — {note or active[i - 1]['desc']}")


def replan(reason: str, drop: list[str] = (), add: list[tuple[str, str]] = (), before: str | None = None):
    """관찰 결과에 따라 계획을 고친다. drop 은 건너뛸 단계, add 는 before 단계 앞(없으면 끝)에 넣을 단계."""
    t = _current.get()
    if t is not None:
        for p in t.plan:
            if p["name"] in drop and p["status"] == "planned":
                p["status"] = "skipped"
        for n, d in add:
            at = next((k for k, p in enumerate(t.plan) if p["name"] == before), len(t.plan))
            t.plan.insert(at, {"name": n, "desc": d, "status": "planned"})
    rest = [p["name"] for p in (t.plan if t else []) if p["status"] == "planned"]
    think("Replan", f"{reason} → 남은 단계 {len(rest)}개" + (f": {' → '.join(rest)}" if rest else " (종료)"))


def _write(t: Trace, total: float):
    record = {"trace_id": t.id, "service": t.service, "name": t.name, "start": t.started.isoformat(timespec="milliseconds"),
              "ms": round(total, 1), "status": t.status, "error": t.error, "attrs": t.attrs,
              "plan": t.plan or None, "reasoning": t.reasoning or None,
              "spans": [s.record() for s in t.spans]}
    path = TRACE_DIR / f"{t.service}-{t.started:%Y%m%d}.jsonl"
    try:
        with _write_lock:
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            day = f"{t.service}-{t.started:%Y%m%d}"
            if day not in _cleaned:
                _cleaned.add(day)
                keep = timedelta(days=int(os.environ.get("TRACE_KEEP_DAYS", "14")))
                for old in TRACE_DIR.glob(f"{t.service}-*.jsonl"):
                    if datetime.fromtimestamp(old.stat().st_mtime) < t.started - keep:
                        old.unlink(missing_ok=True)
    except OSError as exc:
        t.log(f"[trace] 기록 실패: {exc}")
