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
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
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
        print(f"{datetime.now():%H:%M:%S} [-] {error}", flush=True)
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

    def set(self, **attrs) -> Trace:
        self.attrs.update(attrs)
        return self

    def log(self, text: str):
        print(f"{datetime.now():%H:%M:%S} [{self.id}] {text}", flush=True)


def heartbeat(s, detail=None, every: float = 5.0):
    """오래 걸리는 노드가 도는 동안 every 초마다 '진행 중' 줄을 남긴다(로그가 멈춘 것처럼 보이지 않게).
    돌려준 함수를 부르면 멈춘다. 트레이스 밖(_NullSpan)이면 아무것도 하지 않는다."""
    if not isinstance(s, Span):
        return lambda: None
    stop = threading.Event()

    def beat():
        while not stop.wait(every):
            extra = detail() if detail else ""
            s.trace.log(f"↻ {'  ' * s.depth}{s.no:02d} {s.name:<16}{time.perf_counter() - s.start:>7.0f}s  진행 중"
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
        t.log(f"{mark} {'  ' * s.depth}{s.no:02d} {s.name:<16}{s.ms:>8,.0f}ms  {tail}")


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
        total = (time.perf_counter() - t.t0) * 1000
        if t.status == "ok" and any(s.status == "cancelled" for s in t.spans):
            t.status = "cancelled"
        top = sorted((s for s in t.spans if s.depth == 0), key=lambda s: -(s.ms or 0))[:4]
        nodes = " · ".join(f"{s.name} {(s.ms or 0) / 1000:.1f}s" for s in top)
        errors = [f"{s.name}: {s.error}" for s in t.spans if s.error]
        t.log(f"■ {name} {t.status} {total / 1000:.1f}s | {nodes}"
              + (f" | {_attrs(t.attrs)}" if t.attrs else "") + (f" | 오류 {'; '.join(errors)}" if errors else ""))
        _write(t, total)


def _write(t: Trace, total: float):
    record = {"trace_id": t.id, "service": t.service, "name": t.name, "start": t.started.isoformat(timespec="milliseconds"),
              "ms": round(total, 1), "status": t.status, "error": t.error, "attrs": t.attrs,
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
