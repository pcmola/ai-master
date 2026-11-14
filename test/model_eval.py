"""LLM 모델 비교 평가: 시나리오 1·2·3 이 실제로 부르는 LLM 함수를 모델만 바꿔 같은 입력으로 돌리고 규칙으로 채점한다.

프롬프트·후처리·검증은 운영 코드 함수를 그대로 쓴다(복사하지 않음). 바꾸는 것은 OPENAI_MODEL 하나다.

    python test/model_eval.py                         # 기본 모델 전부, 모델마다 별도 프로세스로 동시에
    python test/model_eval.py --models gpt-6.1-sol gpt-5 --repeat 1
    python test/model_eval.py --tasks s2_parse s3_fix  # 일부 과제만

과제와 채점(정답은 사람이 정한 규칙, LLM 채점자 없음):
  s1_guide    시나리오 1 가이드 생성(guide_events, '프록시 설정 가이드 알려줘' · 포워드). 캐시는 끄고 매번 새로 생성.
              완료 여부, LLM 시간, 토큰, 프롬프트가 요구한 구성(개요·단계별 가이드·주의사항·참고 문서) 4절,
              단계마다 근거 문서를 달았는지, 코드 블록, Wiki 의 Squid 포트 3128, wiki-repo 에 없는 문서 인용(경로를 지어냄),
              사실 대조(인라인 코드·코드 줄의 명령·경로·값이 근거 문서 원문에 글자 그대로 있는지, fact_check).
  s2_parse    시나리오 2 형식이 다른 현황 문서 파싱(llm_parse, Proxy VM 정리 ('26.09.17 기준) 11행).
              VM 11개 × (LOCATION·사설 IP·상태) 33칸 정답률, 빠진 VM(이름을 못 뽑음)·없는 VM(지어낸 이름).
  s2_summary  시나리오 2 AI 요약(ai_summary, 2026-11-11 실제 차이 4건). 120자·2문장 이내, 존댓말,
              바뀐 VM 3대 언급 수, 데이터에 없는 VM 이름.
  s3_fix      시나리오 3 원인·수정안(triage → propose_fix, 장애 3종: 허용 도메인 오타·허용 클라이언트 IP 오타·잘못된 DNS).
              로그는 탐지기(proxy_log_detect.detect)에 Health Check 와 같은 시험 접속 로그를 넣어 만들고, 설정은
              proxy-config origin/main 에 장애 커밋 하나를 더한 상태로 만든다(저장소는 바꾸지 않음).
              수정안 규칙 검증 통과, 수정안을 적용하면 장애가 사라지는지, 원인 커밋 적중, 재시도 수, 한국어.

결과: data/model_eval/<시각>/ 에 모델별 JSON·가이드 원문, summary.md(표). 입력에 실제 허용 클라이언트 IP 가 들어가므로
data/(Git 제외)에만 둔다. 시간은 모델을 동시에 돌린 값이라 게이트웨이 부하에 따라 달라질 수 있다.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "proxy_tools", ROOT / "wiki_tools"):
    sys.path.insert(0, str(p))

from common.paths import DATA_DIR, WIKI_REPO  # noqa: E402

MODELS = ["gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-luna", "gpt-5.4", "gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"]
TASKS = ["s1_guide", "s2_parse", "s2_summary", "s3_fix"]
OUT_ROOT = DATA_DIR / "model_eval"
LLM_NODES = {"llm_generate", "wiki_parse_llm", "llm_summary", "llm_triage", "llm_fix"}
FAULT_HASH = "e5a91c3"  # 평가용 장애 커밋의 짧은 해시(실제 저장소에는 없음)

PARSE_DOC = (WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "01.-공통" / "06.-네트워크-및-Proxy"
             / "01.-네트워크-및-Proxy-현황-파악" / "Proxy-VM-정리-('26.09.17-기준).md")
# 사람이 원문을 읽고 정한 정답(이름 → LOCATION, 상태). 사설 IP 는 원문 표의 IP 칸에서 숫자만 뽑아 정답으로 쓴다.
PARSE_TRUTH = {
    "dmo-dmz-proxy01": ("Korea Central", "운영"), "dmo-app-proxy01": ("Korea Central", "운영"),
    "dmo-ml-proxy01": ("Korea Central", "폐기 예정"), "aks-dmo-prod-01": ("Korea Central", "운영"),
    "dmo-koce-inpxy01": ("Korea Central", "운영"), "dmo-koce-expxy01": ("Korea Central", "운영"),
    "dmo-eus-inpxy01": ("East US", "운영"), "dmo-eus-expxy01": ("East US", "운영"),
    "dmo-aips-pxy01": ("East US", "확인 필요"), "dmo-ana-proxy01": ("Korea Central", "운영"),
    "dmo-collab-pxy01": ("Korea Central", "운영"),
}


# ---------------------------------------------------------------- 공통

def llm_usage(t) -> dict:
    """트레이스 안 LLM 노드들의 토큰·호출 수."""
    spans = [s for s in t.spans if s.name in LLM_NODES]
    return {"calls": len(spans),
            "prompt_tokens": sum(s.attrs.get("prompt_tokens") or 0 for s in spans),
            "completion_tokens": sum(s.attrs.get("completion_tokens") or 0 for s in spans),
            "reasoning_tokens": sum(s.attrs.get("reasoning_tokens") or 0 for s in spans),
            "llm_s": round(sum(s.ms or 0 for s in spans) / 1000, 1),  # LLM 응답을 기다린 시간(검색·색인 제외)
            "errors": [s.error for s in spans if s.error]}


def hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("가" <= c <= "힣" for c in letters) / len(letters) if letters else 0.0


def run_case(task: str, fn, **attrs) -> dict:
    """한 번 실행: 트레이스로 감싸 토큰을 모으고, 예외도 결과로 남긴다."""
    from common.trace import trace
    started = time.perf_counter()
    with trace("model-eval", task, **attrs) as t:
        try:
            out = fn()
            out["error"] = None
        except Exception as exc:  # 한 모델의 실패가 평가 전체를 멈추지 않게
            out = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]}
    out["seconds"] = round(time.perf_counter() - started, 1)
    out["usage"] = llm_usage(t)
    return out


# ---------------------------------------------------------------- s1 가이드

def s1_guide(out_dir: Path, model: str, i: int) -> dict:
    import wiki_search as ws
    ws.guide_cache_get = lambda *a, **k: None  # 캐시를 보지도 쓰지도 않는다(운영 캐시 파일을 건드리지 않음)
    ws.guide_cache_put = lambda *a, **k: None
    ws.refresh_index()
    meta, done = {}, {}
    for ev in ws.guide_events("프록시 설정 가이드 알려줘", "proxy", "forward", fresh=True):
        if ev["type"] == "meta":
            meta = ev
        elif ev["type"] == "done":
            done = ev
    md = done.get("markdown") or ""
    (out_dir / f"s1_guide_{i}.md").write_text(md, encoding="utf-8")
    sources = sorted(r["path"] for r in meta.get("results", []))
    return {"mode": done.get("mode"), "sources": sources, **score_guide(md), **fact_check(md, sources)}


def guide_sources() -> list[str]:
    """가이드 근거 문서 15개의 경로. 검색·선별은 모델과 상관없으므로 LLM 을 부르기 전(meta 이벤트)에 멈춰 얻는다."""
    import wiki_search as ws
    ws.guide_cache_get = lambda *a, **k: None
    ws.refresh_index()
    gen = ws.guide_events("프록시 설정 가이드 알려줘", "proxy", "forward", fresh=True)
    for ev in gen:
        if ev["type"] == "meta":
            gen.close()
            return sorted(r["path"] for r in ev["results"])
    return []


def squash(text: str) -> str:
    return " ".join(text.split())


def fact_check(md: str, sources: list[str]) -> dict:
    """가이드의 구체 값(인라인 코드·코드 블록 줄: 명령·경로·IP·포트·설정값)이 근거 문서 원문에 글자 그대로 있는지 본다.
    프롬프트가 '위키에 없는 내용은 지어내지 마라', '명령어·값은 그대로 옮겨라'라고 지시하므로, 원문에 없는 값은 모델이
    바꾸거나 지어낸 것이다. 근거 문서 15개 전체와 대조한다(LLM 이 받은 조각보다 넓어 너그러운 기준)."""
    body = md.split("\n## 원문 링크", 1)[0]
    body = re.sub(r"::: mermaid.*?:::", "", body, flags=re.S)  # 다이어그램은 코드가 원문을 그대로 붙인다
    items = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", body, re.S):
        for ln in block.splitlines():
            ln = re.sub(r"\s{2,}#.*$", "", ln).strip()  # 줄 끝 설명 주석은 뺀다
            if len(ln) >= 4 and not ln.startswith("#"):
                items.append(ln)
    for span_ in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", re.sub(r"```.*?```", "", body, flags=re.S)):
        if len(span_.strip()) >= 4:
            items.append(span_.strip())
    corpus = squash("\n".join((WIKI_REPO / f"{p}.md").read_text(encoding="utf-8-sig", errors="replace")
                              for p in sources if (WIKI_REPO / f"{p}.md").is_file()))
    unique = list(dict.fromkeys(squash(x) for x in items))
    absent = [x for x in unique if x not in corpus]
    return {"facts": len(unique), "facts_absent": absent}


REQUIRED = ("개요", "단계별 가이드", "주의사항", "참고 문서")  # llm_guide 프롬프트가 요구하는 구성


def score_guide(md: str) -> dict:
    """가이드 채점. 저장한 원문만으로 다시 계산할 수 있게 따로 둔다(--summarize 가 다시 부름)."""
    from urllib.parse import unquote
    body = md.split("\n## 원문 링크", 1)[0]
    cited = set()
    for m in re.finditer(r"\?path=([^)\s]+)\)", body):
        cited.add(unquote(m.group(1)).removesuffix(".md"))
    for m in re.finditer(r"AI-Master-프로젝트/[^\s)`\]]+", body):  # 링크로 바뀌지 않고 글자로 남은 경로
        cited.add(unquote(m.group(0)).rstrip(".,").removesuffix(".md"))

    def exists(path: str) -> bool:
        return (WIKI_REPO / f"{path}.md").is_file() or (WIKI_REPO / path).is_dir()
    missing = sorted(c for c in cited if not exists(c))  # wiki-repo 에 없는 문서(경로를 지어냄)
    h2 = [h.strip() for h in re.findall(r"^## (.+)$", body, re.M)]
    part = body.split("\n## 단계별 가이드", 1)[1].split("\n## ", 1)[0] if "\n## 단계별 가이드" in body else ""
    steps = re.split(r"^### ", part, flags=re.M)[1:]
    return {"chars": len(body), "structure": sum(any(h.startswith(r) for h in h2) for r in REQUIRED),
            "steps": len(steps), "steps_cited": sum(bool(re.search(r"\?path=|AI-Master-프로젝트/", x)) for x in steps),
            "code_blocks": body.count("```") // 2, "port_3128": "3128" in body,
            "cited_docs": len(cited), "missing_docs": missing, "korean": round(hangul_ratio(body), 2)}


# ---------------------------------------------------------------- s2 파싱·요약

def parse_truth() -> dict:
    text = PARSE_DOC.read_text(encoding="utf-8-sig")
    truth = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        name = re.sub(r"\s*\(.*\)", "", cells[3]).strip()
        if name in PARSE_TRUTH:
            ip = re.search(r"\d+\.\d+\.\d+\.\d+", cells[4])
            truth[name] = {"location": PARSE_TRUTH[name][0], "private_ip": ip.group(0) if ip else "",
                           "state": PARSE_TRUTH[name][1]}
    assert len(truth) == len(PARSE_TRUTH), f"정답 문서 형식이 바뀌었습니다: {sorted(set(PARSE_TRUTH) - set(truth))}"
    return truth


def s2_parse(truth: dict) -> dict:
    import proxy_status_sync as ps
    rows = ps.llm_parse(PARSE_DOC.read_text(encoding="utf-8-sig"))
    got = {r["name"]: r for r in rows}
    right, wrong = 0, []
    for name, want in truth.items():
        for f, v in want.items():
            if got.get(name, {}).get(f, "") == v:
                right += 1
            else:
                wrong.append(f"{name}.{f}: {got.get(name, {}).get(f, '(없음)')!r} ≠ {v!r}")
    return {"cells": right, "cells_total": len(truth) * 3 + 0, "missing": sorted(set(truth) - set(got)),
            "extra": sorted(set(got) - set(truth)), "wrong": wrong}


SUMMARY_WIKI = [
    {"name": "wjy-proxy01", "state": "중지", "size": "Standard_B1s", "location": "koreacentral", "proxy": "Squid", "private_ip": ""},
    {"name": "wjy-proxy02", "state": "운영", "size": "Standard_B2ats_v2", "location": "koreacentral", "proxy": "Squid", "private_ip": ""},
    {"name": "wjy-prxy04", "state": "운영", "size": "Standard_B1s", "location": "koreacentral", "proxy": "Squid", "private_ip": ""},
]
SUMMARY_AZURE = [
    {"name": "wjy-proxy01", "state": "운영", "size": "Standard_B2ats_v2", "location": "koreacentral", "proxy": "", "private_ip": ""},
    {"name": "wjy-proxy02", "state": "운영", "size": "Standard_B2ats_v2", "location": "koreacentral", "proxy": "", "private_ip": ""},
    {"name": "wjy-proxy03", "state": "운영", "size": "Standard_B2ats_v2", "location": "koreacentral", "proxy": "", "private_ip": ""},
]
SUMMARY_DIFFS = [
    {"type": "added", "name": "wjy-proxy03"}, {"type": "removed", "name": "wjy-prxy04"},
    {"type": "changed", "name": "wjy-proxy01", "field": "state", "wiki": "중지", "azure": "운영"},
    {"type": "changed", "name": "wjy-proxy01", "field": "size", "wiki": "Standard_B1s", "azure": "Standard_B2ats_v2"},
]


def s2_summary() -> dict:
    import proxy_status_sync as ps
    ai = ps.ai_summary(SUMMARY_DIFFS, SUMMARY_WIKI, SUMMARY_AZURE)
    if not ai:
        raise RuntimeError("요약 없음")
    return score_summary(ai["summary"])


def score_summary(text: str) -> dict:
    """요약 채점(--summarize 가 저장한 요약문으로 다시 부름). 문장마다 존댓말(~니다·~세요)로 끝나야 한다."""
    sentences = [x.strip() for x in re.split(r"(?<=[.!?;])\s+", text.strip()) if x.strip()]
    names = set(re.findall(r"wjy-[a-z]+\d+", text))
    known = {r["name"] for r in SUMMARY_WIKI + SUMMARY_AZURE}
    return {"summary": text, "chars": len(text), "sentences": len(sentences),
            "within_limit": len(text) <= 120 and len(sentences) <= 2,
            "polite": all(re.search(r"(니다|세요|십시오)[.!]?$", x) for x in sentences),
            "mentioned": len(names & {"wjy-proxy01", "wjy-proxy03", "wjy-prxy04"}),
            "invented_names": sorted(names - known)}


# ---------------------------------------------------------------- s3 원인·수정안

def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if res.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.decode('utf-8', 'replace')}")
    return res.stdout.decode("utf-8", "replace").strip()


def s3_fixture(fault: str) -> dict:
    """proxy-config origin/main(정상)에 장애 커밋 하나를 더한 저장소 모습과, 그 설정에서 Health Check 접속이 남길 로그."""
    import incident_response as ir
    import proxy_fault_inject as fi
    from dataclasses import asdict
    from proxy_log_detect import ALLOWED_CLIENTS, WHITELIST, detect, line
    from proxy_log_viewer import parse_line
    repo, ref = ir.CONFIG_REPO, "origin/main"
    if fi.active(ref):
        raise RuntimeError(f"origin/main 에 장애가 심어져 있어 정상 기준을 만들 수 없습니다: {fi.active(ref)}")
    paths = git(repo, "ls-tree", "-r", "--name-only", ref, "squid").splitlines()
    clean = {p: git(repo, "show", f"{ref}:{p}") + "\n" for p in paths}
    sc = fi.SCENARIOS[fault]
    files = dict(clean)
    assert files[sc["file"]].count(sc["before"]) == 1
    files[sc["file"]] = files[sc["file"]].replace(sc["before"], sc["after"])
    now = datetime.now().astimezone()
    when = (now - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S %z")
    author = git(repo, "log", "-1", "--format=%an", ref)
    commits = [f"{FAULT_HASH}|{when}|{author}|{sc['message']}"] + git(
        repo, "log", "-n", "7", "--date=iso", "--format=%h|%ad|%an|%s", ref).splitlines()
    patch = "".join(difflib.unified_diff(clean[sc["file"]].splitlines(True), files[sc["file"]].splitlines(True),
                                         f"a/{sc['file']}", f"b/{sc['file']}"))
    diffs = (f"--- commit {FAULT_HASH} {when} {sc['message']}\n\ndiff --git a/{sc['file']} b/{sc['file']}\n{patch}\n"
             + git(repo, "log", "-n", "4", "-p", "--format=--- commit %h %ad %s", "--date=iso", ref, "--", "squid"))[:6000]
    # Health Check 와 같은 시험 접속: 허용 목적지 4곳 × 5회, 차단 목적지 2곳 × 1회
    client = fi.CLIENT
    allowed = ["www.data.go.kr", "api.data.go.kr", "www.law.go.kr", "open.law.go.kr"]
    t0, lines = time.time() - 600, []
    for k in range(5):
        for j, host in enumerate(allowed):
            ts = t0 + k * 40 + j * 3
            if fault == "client-acl-typo" or (fault == "whitelist-typo" and "data.go.kr" in host):
                lines.append(line(ts, 0, client, "TCP_DENIED", 403, 3900, "CONNECT", f"{host}:443", "HIER_NONE/-"))
            elif fault == "bad-dns":
                lines.append(line(ts, 5012, client, "TCP_TUNNEL", 503, 0, "CONNECT", f"{host}:443", "HIER_NONE/-"))
            else:
                lines.append(line(ts, 120, client, "TCP_TUNNEL", 200, 5400, "CONNECT", f"{host}:443"))
    for j, host in enumerate(["www.naver.com", "www.google.com"]):
        lines.append(line(t0 + 210 + j, 0, client, "TCP_DENIED", 403, 3900, "CONNECT", f"{host}:443", "HIER_NONE/-"))
    rows = [parse_line(x) for x in lines]
    found = [asdict(f) for f in detect(rows, ALLOWED_CLIENTS, WHITELIST)]
    if not any(f["severity"] in ir.SEVERE for f in found):
        raise RuntimeError(f"{fault}: 탐지기가 high·medium 을 내지 않았습니다")
    incidents = [{"vm": {"name": "wjy-proxy01", "kind": "squid"}, "rows": rows, "findings": found}]
    return {"src": {"commits": commits, "diffs": diffs, "files": files}, "clean": clean, "incidents": incidents,
            "file": sc["file"], "client": client}


def fault_gone(fault: str, text: str, clean: str, client: str) -> bool:
    """수정안을 적용한 파일에서 장애가 사라졌는가(정상 설정과 글자까지 같지 않아도, 장애를 만든 설정이 없어지면 통과)."""
    if fault == "whitelist-typo":
        return ".date.go.kr" not in text and re.search(r"^\.data\.go\.kr$", text, re.M) is not None
    if fault == "client-acl-typo":
        return f" {client}/32" in text and f" {client[:-1]}/32" not in re.sub(rf" {re.escape(client)}/32", "", text)
    if fault == "bad-dns":
        return not re.search(r"^\s*dns_nameservers\s+192\.0\.2\.53", text, re.M)
    return text == clean


def s3_fix(fault: str, fx: dict, cases: list[dict]) -> dict:
    import incident_response as ir
    tri = ir.triage(fx["incidents"])
    fix = ir.propose_fix(fx["incidents"], tri, cases, fx["src"]) or {}
    applied, gone, exact = None, False, False
    if fix.get("applicable"):
        applied = fx["src"]["files"][fix["file"]].replace(fix["before"], fix["after"], 1)
        clean = fx["clean"][fix["file"]]
        gone = fault_gone(fault, applied, clean, fx["client"]) if fix["file"] == fx["file"] else False
        exact = applied == clean
    cause = (fix.get("cause") or "") + (fix.get("fix_summary") or "")
    return {"triage_ok": bool(tri and tri.get("summary") and tri.get("hypotheses")),
            "applicable": bool(fix.get("applicable")), "fixed": gone, "exact": exact,
            "commit_hit": (fix.get("suspect_commit") or "")[:7] == FAULT_HASH,
            "confidence": fix.get("confidence"), "korean": round(hangul_ratio(cause), 2) if cause else 0.0,
            "file": fix.get("file"), "cause": (fix.get("cause") or "")[:300]}


# ---------------------------------------------------------------- 실행

def worker(model: str, tasks: list[str], repeat: int, out: Path) -> None:
    os.environ["OPENAI_MODEL"] = model
    out_dir = out / model
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"model": model, "tasks": {}}
    log = (out_dir / "run.log").open("w", encoding="utf-8")
    with contextlib.redirect_stdout(log):
        if "s1_guide" in tasks:
            result["tasks"]["s1_guide"] = [run_case("s1_guide", lambda i=i: s1_guide(out_dir, model, i), model=model)
                                           for i in range(1, repeat + 1)]
        if "s2_parse" in tasks:
            truth = parse_truth()
            result["tasks"]["s2_parse"] = [run_case("s2_parse", lambda: s2_parse(truth), model=model) for _ in range(repeat)]
        if "s2_summary" in tasks:
            result["tasks"]["s2_summary"] = [run_case("s2_summary", s2_summary, model=model) for _ in range(repeat)]
        if "s3_fix" in tasks:
            import incident_response as ir
            cases = json.loads((out / "s3_cases.json").read_text(encoding="utf-8"))
            runs = []
            for fault in ("whitelist-typo", "client-acl-typo", "bad-dns"):
                fx = s3_fixture(fault)
                for _ in range(repeat):
                    r = run_case("s3_fix", lambda: s3_fix(fault, fx, cases), model=model, fault=fault)
                    r["fault"] = fault
                    runs.append(r)
            result["tasks"]["s3_fix"] = runs
    log.close()
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def prepare(out: Path, tasks: list[str]) -> None:
    """모델과 무관한 입력을 한 번만 만든다: 시나리오 3 의 Wiki 유사 사례(검색 결과가 모델마다 달라지지 않게)."""
    if "s3_fix" not in tasks:
        return
    import incident_response as ir
    with contextlib.redirect_stdout(open(out / "prepare.log", "w", encoding="utf-8")):
        git(ir.CONFIG_REPO, "fetch", "-q", "origin")
        cases = ir.similar("프록시 허용 도메인 차단 403 장애 원인 조치")
    (out / "s3_cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def pct(n, d) -> str:
    return f"{n}/{d}" if d else "-"


def summarize(out: Path, models: list[str]) -> str:
    res = {m: json.loads((out / m / "result.json").read_text(encoding="utf-8")) for m in models
           if (out / m / "result.json").is_file()}
    lines = [f"# LLM 모델 비교 ({out.name})", ""]

    def ok(runs):
        return [r for r in runs if not r.get("error")]

    def tok(runs):
        return mean([r["usage"]["completion_tokens"] for r in ok(runs)])

    if any("s1_guide" in r["tasks"] for r in res.values()):
        lines += ["## s1_guide 시나리오 1 가이드 생성", "",
                  "| 모델 | 완료 | LLM 시간(초) | 출력 토큰 | 글자 | 필수 구성(4) | 단계 | 근거 단 단계 | 코드 블록 | 3128 | 인용 문서 | 없는 문서 인용 | 구체 값 | 원문에 없는 값 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        src_file = out / "s1_sources.json"  # 근거 문서 목록을 남기지 않던 때의 결과용(검색은 모델과 무관해 한 번만 구함)
        for m, r in res.items():
            runs = r["tasks"].get("s1_guide", [])
            for i, x in enumerate(runs, 1):  # 채점 기준을 고쳐도 다시 돌리지 않게 저장한 원문으로 다시 매긴다
                f = out / m / f"s1_guide_{i}.md"
                if not x.get("error") and f.is_file():
                    if not isinstance(x.get("sources"), list):
                        if not src_file.is_file():
                            with contextlib.redirect_stdout(open(out / "sources.log", "w", encoding="utf-8")):
                                src_file.write_text(json.dumps(guide_sources(), ensure_ascii=False), encoding="utf-8")
                        x["sources"] = json.loads(src_file.read_text(encoding="utf-8"))
                    md = f.read_text(encoding="utf-8")
                    x.update(score_guide(md))
                    x.update(fact_check(md, x["sources"]))
            g = ok(runs)
            if not runs:
                continue
            steps = sum(x["steps"] for x in g)
            lines.append(f"| {m} | {pct(sum(x['mode'] == 'llm' for x in g), len(runs))} | {mean([x['usage']['llm_s'] for x in g]) or 0:.0f} "
                         f"| {tok(runs) or 0:,.0f} | {mean([x['chars'] for x in g]) or 0:,.0f} | {mean([x['structure'] for x in g]) or 0:.1f} "
                         f"| {mean([x['steps'] for x in g]) or 0:.1f} | {pct(sum(x['steps_cited'] for x in g), steps)} "
                         f"| {mean([x['code_blocks'] for x in g]) or 0:.1f} | {pct(sum(x['port_3128'] for x in g), len(runs))} "
                         f"| {mean([x['cited_docs'] for x in g]) or 0:.1f} | {sum(len(x['missing_docs']) for x in g)} "
                         f"| {sum(x['facts'] for x in g)} | {sum(len(x['facts_absent']) for x in g)} |")
        lines.append("")
        lines += ["원문에 없는 값(근거 문서 15개 원문에 글자 그대로 없는 인라인 코드·코드 줄):", ""]
        for m, r in res.items():
            absent = [v for x in ok(r["tasks"].get("s1_guide", [])) for v in x.get("facts_absent", [])]
            if absent:
                ip = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")  # summary.md 는 docs 로 옮기므로 IP 는 가린다
                lines.append(f"- {m}: " + " · ".join(f"`{ip.sub('<IP>', v)[:70]}`" for v in dict.fromkeys(absent)))
        lines.append("")
    if any("s2_parse" in r["tasks"] for r in res.values()):
        lines += ["## s2_parse 시나리오 2 비표준 문서 파싱 (VM 11개 × 3칸)", "",
                  "| 모델 | 성공 | 칸 정답률 | 빠진 VM | 없는 VM | LLM 시간(초) | 출력 토큰 |", "|---|---|---|---|---|---|---|"]
        for m, r in res.items():
            runs = r["tasks"].get("s2_parse", [])
            g = ok(runs)
            if not runs:
                continue
            cells = sum(x["cells"] for x in g)
            total = sum(x["cells_total"] for x in g)
            lines.append(f"| {m} | {pct(len(g), len(runs))} | {100 * cells / total if total else 0:.1f}% "
                         f"| {sum(len(x['missing']) for x in g)} | {sum(len(x['extra']) for x in g)} "
                         f"| {mean([x['usage']['llm_s'] for x in g]) or 0:.1f} | {tok(runs) or 0:,.0f} |")
        lines.append("")
    if any("s2_summary" in r["tasks"] for r in res.values()):
        lines += ["## s2_summary 시나리오 2 AI 요약", "",
                  "| 모델 | 성공 | 120자·2문장 | 존댓말 | 바뀐 VM 3대를 다 적음 | 없는 VM 이름 | 평균 글자 | LLM 시간(초) | 예시 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for m, r in res.items():
            runs = r["tasks"].get("s2_summary", [])
            for x in runs:
                if not x.get("error"):
                    x.update(score_summary(x["summary"]))
            g = ok(runs)
            if not runs:
                continue
            lines.append(f"| {m} | {pct(len(g), len(runs))} | {pct(sum(x['within_limit'] for x in g), len(runs))} "
                         f"| {pct(sum(x['polite'] for x in g), len(runs))} | {pct(sum(x['mentioned'] == 3 for x in g), len(runs))} "
                         f"| {sum(len(x['invented_names']) for x in g)} | {mean([x['chars'] for x in g]) or 0:.0f} "
                         f"| {mean([x['usage']['llm_s'] for x in g]) or 0:.1f} | {(g[0]['summary'] if g else '').replace('|', '/')} |")
        lines.append("")
    if any("s3_fix" in r["tasks"] for r in res.values()):
        lines += ["## s3_fix 시나리오 3 원인·수정안 (장애 3종)", "",
                  "| 모델 | 규칙 검증 통과 | 장애 해소 | 원래 설정과 같음 | 원인 커밋 적중 | LLM 호출(평균) | LLM 시간(초) | 출력 토큰 | 장애별 해소 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for m, r in res.items():
            runs = r["tasks"].get("s3_fix", [])
            g = ok(runs)
            if not runs:
                continue
            per = " · ".join(f"{f.split('-')[0]} {sum(x['fixed'] for x in g if x['fault'] == f)}/"
                             f"{sum(1 for x in runs if x['fault'] == f)}" for f in ("whitelist-typo", "client-acl-typo", "bad-dns"))
            lines.append(f"| {m} | {pct(sum(x['applicable'] for x in g), len(runs))} | {pct(sum(x['fixed'] for x in g), len(runs))} "
                         f"| {pct(sum(x['exact'] for x in g), len(runs))} | {pct(sum(x['commit_hit'] for x in g), len(runs))} "
                         f"| {mean([x['usage']['calls'] for x in g]) or 0:.1f} | {mean([x['usage']['llm_s'] for x in g]) or 0:.0f} "
                         f"| {tok(runs) or 0:,.0f} | {per} |")
        lines.append("")
    errors = [f"- {m} {t}: {x['error']}" for m, r in res.items() for t, runs in r["tasks"].items() for x in runs if x.get("error")]
    if errors:
        lines += ["## 실패", ""] + errors + [""]
    text = "\n".join(lines)
    (out / "summary.md").write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 모델 비교 평가")
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--tasks", nargs="+", default=TASKS, choices=TASKS)
    ap.add_argument("--repeat", type=int, default=2, help="과제마다 반복 횟수(s3 는 장애 3종 × 이 값)")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--summarize", type=Path, help="이미 돌린 결과 폴더의 표만 다시 만든다")
    args = ap.parse_args()
    if args.summarize:
        have = [p.name for p in args.summarize.iterdir() if (p / "result.json").is_file()]
        print(summarize(args.summarize, [m for m in MODELS if m in have] + sorted(set(have) - set(MODELS))))
        return 0
    if args.worker:
        worker(args.worker, args.tasks, args.repeat, args.out)
        return 0
    out = OUT_ROOT / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(vars(args), default=str, ensure_ascii=False), encoding="utf-8")
    print(f"결과 폴더: {out}")
    prepare(out, args.tasks)
    procs = {m: subprocess.Popen([sys.executable, __file__, "--worker", m, "--out", str(out), "--repeat", str(args.repeat),
                                  "--tasks", *args.tasks], stdout=subprocess.DEVNULL, stderr=(out / f"{m}.err").open("w"))
             for m in args.models}
    started = time.time()
    for m, p in procs.items():
        p.wait()
        print(f"  {m}: 종료 코드 {p.returncode} ({time.time() - started:.0f}초)", flush=True)
    print(summarize(out, args.models))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
