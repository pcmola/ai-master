"""시나리오 3: 프록시 장애 감지 및 자동 조치. 로그에서 장애를 감지하면 Wiki 사례와 설정 저장소(proxy-config)
변경 이력으로 원인을 찾고, 수정안을 Teams 로 승인받아 Task·PR·장애 대응 이력 Wiki 까지 만든다.

단계(노드별 트레이스는 data/traces/scenario3-YYYYMMDD.jsonl 과 콘솔·data/logs/scenario3.log 에 남는다)
1. trigger        직접 실행(허브 '시나리오 실행' 탭 또는 명령줄)
2. collect        프록시 VM 마다 최근 접속 로그를 SSH 로 읽고(proxy_log_viewer), 최근 SCENARIO3_WINDOW_MIN 분 안의
                  기록만 규칙으로 탐지한다(proxy_log_detect). high·medium 이 없으면 '이상 없음'으로 끝낸다
   llm_triage     (AI) 탐지 결과와 로그 표본으로 장애 요약·원인 후보·Wiki 검색어를 만든다
3. similar        Wiki 에서 비슷한 장애 사례·관련 가이드를 찾는다(시나리오 1 의 검색 엔진, RAG)
4. source         proxy-config 저장소를 최신으로 받아 최근 커밋 diff 와 현재 설정을 읽고,
   llm_fix        (AI) 원인 커밋·설정 줄을 특정하고 수정안(파일, 바꿀 줄 → 새 줄)을 만든다.
                  수정안은 '바꿀 줄이 파일에 정확히 한 번 있는지' 규칙으로 검증하고, 아니면 수동 조치로 돌린다
5. approval       Teams 카드(로컬 승인 화면)로 "조치할까요?" Yes/No. No·시간 초과는 기록하고 끝낸다
6. task_create    Epic → Feature '[공통] 프록시 장애 대응' → 그 달 PBI → 'NN. [프록시 장애] …' Task
7. pull_request   proxy-config 에 수정 브랜치를 만들어 수정안을 커밋(tests/validate.py 통과 필수)하고 push,
                  main 으로 가는 PR 을 만들어 Task 와 연결한다. 병합·배포는 사람이 PR 검토 후 한다
8. wiki_history   Wiki '02. 운영 > 01. 공통 > 04. 장애 대응 > 01. 프록시 장애 대응 이력' 맨 위에 이번 건을 더하고
                  Teams 로 결과(Task·PR·Wiki 링크)를 알린다

사용 예:
    python proxy_tools/incident_response.py            # 감지되면 승인 후 Task·PR·이력 Wiki
    python proxy_tools/incident_response.py --dry-run  # 1~4단계만(분석·수정안까지 출력, 아무것도 바꾸지 않음)

설정(.env): AZURE_DEVOPS_*, AZURE_OPENAI_*·OPENAI_MODEL, TEAMS_WEBHOOK_URL, PROXY_CONFIG_DIR(기본 ../proxy-config),
            SCENARIO3_WINDOW_MIN(기본 60), SCENARIO3_TIMEOUT(승인 대기 초, 기본 900), SCENARIO3_EPIC_ID(기본 1),
            SCENARIO3_FEATURE(기본 '[공통] 프록시 장애 대응'), SCENARIO3_PBI(기본 '프록시 장애 대응')
장애 재현: proxy_tools/proxy_fault_inject.py apply <시나리오> --push --deploy <VM_IP>
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wiki_tools"))
from common.paths import DATA_DIR, ENV_FILE, PROJECT_ROOT, WIKI_REPO  # noqa: E402
from common.trace import heartbeat, span, trace  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

KST = timezone(timedelta(hours=9))
WINDOW_MIN = int(os.environ.get("SCENARIO3_WINDOW_MIN") or 60)
APPROVAL_TIMEOUT = float(os.environ.get("SCENARIO3_TIMEOUT") or 900)
CONFIG_REPO = Path(os.environ.get("PROXY_CONFIG_DIR", PROJECT_ROOT.parent / "proxy-config"))
CONFIG_REPO_NAME = "proxy-config"
HISTORY_DIR = WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "01.-공통" / "04.-장애-대응"
HISTORY_PAGE = HISTORY_DIR / "01.-프록시-장애-대응-이력.md"
LOG_FILE = DATA_DIR / "logs" / "scenario3.log"
SEVERE = {"high", "medium"}


# ---------------------------------------------------------------- 공통

def now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def llm(system: str, prompt: str, node: str, **attrs) -> dict | None:
    """JSON 으로 답하는 LLM 호출. 실패하면 노드에 이유를 남기고 None."""
    from proxy_status_sync import llm_client
    model = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")
    with span(node, model=model, prompt_chars=len(prompt), **attrs) as s:
        stop = heartbeat(s, lambda: "LLM 응답 기다리는 중")
        try:
            res = llm_client().chat.completions.create(
                model=model, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            s.set(prompt_tokens=res.usage.prompt_tokens, completion_tokens=res.usage.completion_tokens)
            return json.loads(res.choices[0].message.content or "{}")
        except Exception as exc:  # 분석이 실패해도 규칙 탐지 결과로는 진행할 수 있게
            s.fail(f"{type(exc).__name__}: {str(exc)[:160]}")
            return None
        finally:
            stop()


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, stdin=subprocess.DEVNULL)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


# ---------------------------------------------------------------- 2. 로그 수집·감지

def collect() -> list[dict]:
    """VM 마다 {vm, rows, findings}. 최근 WINDOW_MIN 분 안의 기록만 탐지한다(오래된 장애로 매번 울리지 않게)."""
    from dataclasses import asdict
    from proxy_log_detect import ALLOWED_CLIENTS, WHITELIST, detect
    from proxy_log_viewer import read_logs
    from proxy_policy_monitor import proxy_targets
    since = time.time() - WINDOW_MIN * 60
    out = []
    for vm in proxy_targets():
        if vm["state"] != "운영":
            print(f"{vm['name']}: 중지되어 건너뜀 (프록시 VM 은 항상 켜져 있어야 합니다 → 시나리오 2 이상 징후)")
            continue
        with span("collect", vm=vm["name"], kind=vm["kind"], window_min=WINDOW_MIN) as s:
            d = read_logs(vm["ip"], 2000, "real", vm["kind"])
            if "error" in d:
                s.fail(d["error"][:160])
                print(f"{vm['name']}: 로그 읽기 실패 — {d['error']}")
                continue
            rows = [r for r in d["rows"] if r["ts"] >= since]
            found = [asdict(f) for f in detect(rows, ALLOWED_CLIENTS, WHITELIST)]
            s.set(rows=len(rows), denied=sum(1 for r in rows if r.get("denied")), findings=len(found),
                  severe=sum(1 for f in found if f["severity"] in SEVERE))
        out.append({"vm": vm, "rows": rows, "findings": found})
    return out


def sample_lines(rows: list[dict], n: int = 25) -> list[str]:
    """LLM 에 보낼 로그 표본: 차단·오류 위주로 최근 n 줄."""
    bad = [r for r in rows if r.get("denied") or str(r.get("status", "")).startswith(("5", "0"))]
    pick = sorted(bad or rows, key=lambda r: r["ts"])[-n:]
    return [f"{r['time'][:19]} {r['client']} {r['code']}/{r['status']} {r['method']} {r['host']} {r['ms']}ms" for r in pick]


def triage(incidents: list[dict]) -> dict | None:
    payload = [{"vm": i["vm"]["name"], "kind": i["vm"]["kind"],
                "findings": [{k: f[k] for k in ("severity", "rule", "title", "detail", "count", "first", "last")}
                             for f in i["findings"] if f["severity"] in SEVERE],
                "log_sample": sample_lines(i["rows"])} for i in incidents]
    prompt = ("아래는 프록시 VM 접속 로그를 규칙으로 탐지한 결과와 로그 표본이다. 운영자가 장애를 이해하도록 정리하라.\n"
              "- summary: 무슨 일이 일어났는지 2문장 이내 존댓말(~습니다).\n"
              "- hypotheses: 원인 후보 1~3개. 각 {cause, evidence}. 로그에 근거가 있는 것만, 근거를 evidence 에 쓴다.\n"
              "- search_query: 사내 Wiki 에서 비슷한 장애 사례·조치 가이드를 찾을 한국어 검색어 한 줄(프록시 용어 포함).\n"
              "- 주어진 데이터에 없는 사실은 지어내지 마라.\n"
              'JSON 으로만 답하라: {"summary": str, "hypotheses": [{"cause": str, "evidence": str}], "search_query": str}\n\n'
              + json.dumps(payload, ensure_ascii=False))
    return llm("너는 프록시 서버 장애를 1차 분석하는 운영 엔지니어다. 근거 없는 추측을 하지 않는다.", prompt, "llm_triage",
               incidents=len(incidents))


# ---------------------------------------------------------------- 3. 유사 장애 검색

def similar(query: str, n: int = 4) -> list[dict]:
    """시나리오 1 검색 엔진으로 Wiki 문서를 찾는다. 각 {title, path, link, excerpt}."""
    import wiki_search
    with span("similar", query=query) as s:
        wiki_search.refresh_index()
        d = wiki_search.do_search(query)
        out = []
        for r in d["results"]:
            hist = HISTORY_PAGE.relative_to(WIKI_REPO).as_posix()
            if r["path"].startswith(wiki_search.AI_DOC_PREFIX) or r["path"] == hist:  # AI 가 만든 문서·이 이력 문서는 근거에서 뺀다
                continue
            text = (WIKI_REPO / r["path"]).read_text(encoding="utf-8-sig", errors="replace") \
                if (WIKI_REPO / r["path"]).is_file() else ""
            out.append({"title": r["title"], "path": r["path"], "link": r["link"], "excerpt": text[:1500]})
            if len(out) >= n:
                break
        s.set(results=len(d["results"]), used=len(out), top=[o["title"] for o in out])
    return out


# ---------------------------------------------------------------- 4. 설정 저장소 확인·수정안

def read_source() -> dict:
    with span("source", repo=CONFIG_REPO_NAME) as s:
        if not (CONFIG_REPO / ".git").exists():
            raise RuntimeError(f"proxy-config 저장소가 없습니다: {CONFIG_REPO} (PROXY_CONFIG_DIR 로 지정)")
        if git(CONFIG_REPO, "status", "--porcelain"):
            raise RuntimeError(f"{CONFIG_REPO} 에 커밋하지 않은 변경이 있습니다. 정리한 뒤 다시 실행하세요.")
        git(CONFIG_REPO, "fetch", "-q", "origin")
        branch = git(CONFIG_REPO, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            git(CONFIG_REPO, "switch", "-q", "main")
        git(CONFIG_REPO, "merge", "-q", "--ff-only", "origin/main")
        commits = git(CONFIG_REPO, "log", "-n", "8", "--date=iso", "--format=%h|%ad|%an|%s").splitlines()
        diffs = git(CONFIG_REPO, "log", "-n", "5", "-p", "--format=--- commit %h %ad %s", "--date=iso", "--", "squid")[:6000]
        files = {p.relative_to(CONFIG_REPO).as_posix(): p.read_text(encoding="utf-8")
                 for p in sorted((CONFIG_REPO / "squid").rglob("*")) if p.is_file()}
        s.set(head=commits[0].split("|")[0] if commits else None, commits=len(commits), files=list(files))
    return {"commits": commits, "diffs": diffs, "files": files}


def propose_fix(incidents: list[dict], tri: dict | None, cases: list[dict], src: dict) -> dict | None:
    payload = {"incidents": [{"vm": i["vm"]["name"], "kind": i["vm"]["kind"],
                              "findings": [f"{f['severity']}:{f['rule']} {f['title']} — {f['detail']}"
                                           for f in i["findings"] if f["severity"] in SEVERE],
                              "log_sample": sample_lines(i["rows"], 15)} for i in incidents],
               "triage": tri, "wiki_cases": [{"title": c["title"], "excerpt": c["excerpt"][:800]} for c in cases],
               "recent_commits": src["commits"], "recent_diffs": src["diffs"], "current_files": src["files"]}
    prompt = ("프록시 장애의 원인을 설정 저장소(proxy-config, Squid 설정)의 최근 변경에서 찾아 수정안을 만들어라.\n"
              "- cause: 원인 한두 문장(존댓말). suspect_commit: 원인으로 보이는 커밋 해시(없으면 \"\").\n"
              "- 수정이 필요하면 file(current_files 의 키), before(그 파일에 있는 줄을 그대로, 줄 단위), after(고친 줄)를 쓴다. "
              "before 는 파일에 정확히 한 번 나와야 한다. 고칠 설정이 저장소에 없거나 확신이 없으면 file 을 \"\" 로 둔다.\n"
              "- fix_summary: 무엇을 어떻게 고치는지 한 문장. impact: 고쳤을 때 영향과 배포 후 확인할 것 한 문장.\n"
              "- confidence: high|medium|low.\n- 데이터에 없는 사실은 지어내지 마라.\n"
              'JSON 으로만 답하라: {"cause": str, "suspect_commit": str, "file": str, "before": str, "after": str, '
              '"fix_summary": str, "impact": str, "confidence": str}\n\n' + json.dumps(payload, ensure_ascii=False))
    fix = llm("너는 Squid 프록시 설정 변경을 검토하는 SRE 다. 최소한의 수정만 제안한다.", prompt, "llm_fix",
              commits=len(src["commits"]), cases=len(cases))
    if not fix:
        return None
    with span("fix_check") as s:  # 규칙 검증: LLM 이 고른 줄이 실제로 그 파일에 정확히 한 번 있는가
        file, before, after = fix.get("file") or "", fix.get("before") or "", fix.get("after") or ""
        text = src["files"].get(file)
        ok = bool(file and text is not None and before and before != after and text.count(before) == 1)
        fix["applicable"] = ok
        s.set(file=file or "-", applicable=ok, confidence=fix.get("confidence"),
              reason=None if ok else ("수정 대상 없음" if not file else "바꿀 줄이 파일에 정확히 한 번 있지 않음"))
    return fix


# ---------------------------------------------------------------- 5. 승인

def ask_approval(title: str, items: list[str], links: list[tuple[str, str]], when: str):
    from teams_tools import teams_notify
    from teams_tools.approval_local import Approval
    webhook = bool(os.environ.get("TEAMS_WEBHOOK_URL"))
    ap = Approval(title, items, "장애 조치(Task·수정 PR·이력 Wiki)를 진행할까요?", links)
    with span("approval", channel="teams" if webhook else "local", timeout_s=APPROVAL_TIMEOUT) as s:
        page_url, yes_url, no_url = ap.start(open_browser=not webhook)
        if webhook:
            with span("teams_notify", kind="approval_request"):
                teams_notify.send("프록시 장애가 감지되었습니다. 조치할까요?\n\n" + "\n".join(f"- {i}" for i in items)
                                  + f"\n\n버튼은 이 알림을 보낸 PC({os.environ.get('COMPUTERNAME', '-')})에서 눌러야 합니다. "
                                    f"{int(APPROVAL_TIMEOUT // 60)}분 안에 답이 없으면 취소됩니다.",
                                  f"[프록시 장애] {title} · 조치 승인 요청", facts=[("감지 시각", when)],
                                  buttons=[("Yes · 조치", yes_url), ("No · 기록만", no_url), ("자세히 보기", page_url)]
                                          + [(l, u) for l, u in links if u][:2])
        result = ap.wait(APPROVAL_TIMEOUT)
        s.set(decision={True: "yes", False: "no", None: "timeout"}[result])
    return result, ap


# ---------------------------------------------------------------- 6. Task

def create_task(dv, when: str, headline: str, body: str) -> tuple[int, dict]:
    from proxy_status_sync import assignee, children, ensure_child, parent_link, work_item
    tags = "proxy-incident; scenario3"
    epic = int(os.environ.get("SCENARIO3_EPIC_ID", "1"))
    feature, _ = ensure_child(dv, epic, "Feature", os.environ.get("SCENARIO3_FEATURE", "[공통] 프록시 장애 대응"),
                              "<p>프록시 로그에서 감지한 장애를 분석·조치하는 운영 업무(시나리오 3).</p>", tags)
    pbi_title = f"{when[2:4]}{when[5:7]}) " + os.environ.get("SCENARIO3_PBI", "프록시 장애 대응")
    pbi, _ = ensure_child(dv, feature, "Product Backlog Item", pbi_title,
                          "<p>이번 달 프록시 장애 대응 건. 장애마다 아래에 Task 가 하나씩 붙는다 "
                          "(proxy_tools/incident_response.py).</p>", tags)
    numbers = [int(c["title"][:2]) for c in children(dv, pbi) if c["type"] == "Task" and c["title"][:2].isdigit()]
    title = f"{max(numbers, default=0) + 1:02d}. [프록시 장애] {headline} ({when})"[:250]
    pf = work_item(dv, pbi)["fields"]
    ops = [{"op": "add", "path": "/fields/System.Title", "value": title},
           {"op": "add", "path": "/fields/System.Description", "value": body},
           {"op": "add", "path": "/fields/System.Tags", "value": tags},
           {"op": "add", "path": "/fields/System.AreaPath", "value": pf["System.AreaPath"]},
           {"op": "add", "path": "/fields/System.IterationPath", "value": pf["System.IterationPath"]},
           parent_link(dv, pbi)]
    owner = assignee(dv)
    if owner:
        ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": owner})
    task = dv.request("POST", "/_apis/wit/workitems/$Task?api-version=7.1", ops, "application/json-patch+json")["id"]
    return task, {"feature": feature, "pbi": pbi, "pbi_title": pbi_title, "title": title}


# ---------------------------------------------------------------- 7. 수정 PR

def create_pr(dv, fix: dict, task_id: int, when: str) -> dict:
    """proxy-config 에 수정 브랜치·커밋을 만들고 main 으로 가는 PR 을 만든다(Task 연결). 로컬은 main 으로 돌려 둔다."""
    branch = f"fix/incident-{when[:10].replace('-', '')}-{when[11:].replace(':', '')}"
    path = CONFIG_REPO / fix["file"]
    git(CONFIG_REPO, "switch", "-q", "-c", branch, "origin/main")
    try:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(fix["before"], fix["after"], 1), encoding="utf-8", newline="\n")
        check = subprocess.run([sys.executable, str(CONFIG_REPO / "tests" / "validate.py")], capture_output=True,
                               stdin=subprocess.DEVNULL, cwd=CONFIG_REPO)
        if check.returncode != 0:
            raise RuntimeError("수정안이 tests/validate.py 를 통과하지 못했습니다: "
                               + check.stdout.decode("utf-8", "replace").strip()[:300])
        git(CONFIG_REPO, "add", "--", fix["file"])
        git(CONFIG_REPO, "commit", "-q", "-m", f"장애 조치: {fix['fix_summary']} (Task #{task_id})")
        commit = git(CONFIG_REPO, "rev-parse", "--short", "HEAD")
        git(CONFIG_REPO, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    finally:
        git(CONFIG_REPO, "checkout", "-q", "--", ".")
        git(CONFIG_REPO, "switch", "-q", "main")
    body = (f"## 장애 조치\n\n{fix['cause']}\n\n- 수정: {fix['fix_summary']}\n- 영향·확인: {fix['impact']}\n"
            f"- 원인 커밋: {fix.get('suspect_commit') or '-'}\n- AI 신뢰도: {fix.get('confidence')}\n\n"
            "병합 후 `scripts/deploy.sh <VM_IP>` 로 배포하고 `scripts/traffic.sh <VM_IP>` 로 확인하세요.\n\n"
            "등록: incident_response.py (시나리오 3)")
    pr = dv.request("POST", f"/_apis/git/repositories/{CONFIG_REPO_NAME}/pullrequests?api-version=7.1",
                    {"sourceRefName": f"refs/heads/{branch}", "targetRefName": "refs/heads/main",
                     "title": f"[장애 조치] {fix['fix_summary']}"[:200], "description": body[:3900],
                     "workItemRefs": [{"id": str(task_id)}]})
    return {"id": pr["pullRequestId"], "branch": branch, "commit": commit,
            "url": f"{dv.base}/_git/{CONFIG_REPO_NAME}/pullrequest/{pr['pullRequestId']}"}


# ---------------------------------------------------------------- 8. 장애 대응 이력 Wiki

def sentences(text: str) -> list[str]:
    """문단을 문장 단위로 나눈다('~다.', '~요.' 뒤에서). 커밋 해시는 코드로 표시."""
    text = re.sub(r"(?<![`\w])([0-9a-f]{7})(?![`\w])", r"`\1`", (text or "").replace("~", "\u223c"))
    parts = [x.strip() for x in re.split(r"(?<=[다요음])\.\s+", text) if x.strip()]
    return [x if x.endswith((".", "?", "!")) else x + "." for x in parts] or ["-"]


def entry_md(head: str, refs: str, summary: str, cause: str, fix_lines: list[str],
             finding_lines: list[str], case_lines: list[str]) -> str:
    """이력 한 건. 긴 문단을 표 칸에 넣지 않고 소제목과 한 줄짜리 항목으로 나눈다."""
    lines = [f"## {head}", "", refs, "",
             "### 요약", ""] + [f"- {x}" for x in sentences(summary)] + ["",
             "### 원인", ""] + [f"- {x}" for x in sentences(cause)] + ["",
             "### 조치", ""] + fix_lines + ["",
             "### 감지 내용", ""] + finding_lines
    if case_lines:
        lines += ["", "### 참고한 Wiki 문서", ""] + case_lines
    return "\n".join(lines) + "\n"


def history_entry(when: str, incidents, tri, fix, cases, task, pr, task_url) -> str:
    finding_lines = [f"- {i['vm']['name']}: [{f['severity']}] {f['title']} — {f['detail']} ({f['first']} ∼ {f['last']}, {f['count']}건)"
                     for i in incidents for f in i["findings"] if f["severity"] in SEVERE]
    refs = (f"**Task** [#{task}]({task_url})" if task else "**Task** -") + " · " + (
        f"**PR** [!{pr['id']}]({pr['url']}) (브랜치 `{pr['branch']}`, 병합·배포 전)" if pr else "**PR** 없음(자동 수정안 없음)")
    if fix and fix.get("applicable"):
        fix_lines = [f"- `{fix['file']}`: `{fix['before'].strip()}` → `{fix['after'].strip()}`" if fix["after"].strip()
                     else f"- `{fix['file']}`: `{fix['before'].strip()}` 삭제"] + [f"- {x}" for x in sentences(fix.get("fix_summary", ""))]
    else:
        fix_lines = ["- 자동 수정안 없음: Task 에서 수동 조치"]
    return entry_md(f"{when} · {', '.join(i['vm']['name'] for i in incidents)}", refs,
                    (tri or {}).get("summary", "-"), (fix or {}).get("cause", "-"), fix_lines, finding_lines,
                    [f"- [{c['title']}](/{c['path'].removesuffix('.md')})" for c in cases])


def write_history(entry: str, when: str, task: int | None):
    from proxy_policy_publish import publish_files
    head = ("# 01. 프록시 장애 대응 이력\n\n"
            "> 자동 생성 문서입니다. `proxy_tools/incident_response.py`(시나리오 3)가 장애를 조치할 때마다 맨 위에 한 건씩 더합니다.\n\n")
    old = HISTORY_PAGE.read_text(encoding="utf-8") if HISTORY_PAGE.is_file() else head
    body = old[len(head):] if old.startswith(head) else old
    files = {HISTORY_PAGE: head + entry + ("\n" + body if body.strip() else "")}
    order = HISTORY_DIR / ".order"
    if not order.is_file():  # 하위 문서가 처음 생기는 폴더: .order 를 새로 만든다(WIKI_DOCUMENT_RULES)
        files[order] = HISTORY_PAGE.stem
    elif HISTORY_PAGE.stem not in order.read_text(encoding="utf-8-sig").split():
        files[order] = order.read_text(encoding="utf-8-sig").rstrip("\n") + "\n" + HISTORY_PAGE.stem
    with span("wiki_history", page=HISTORY_PAGE.name, new_order=order in files) as s:
        s.set(commit=publish_files(files, f"프록시 장애 대응 이력 추가 ({when}" + (f", Task #{task})" if task else ")")))


# ---------------------------------------------------------------- main

def run(args) -> int:
    when = now()
    with span("trigger", mode="dry-run" if args.dry_run else "teams", window_min=WINDOW_MIN):
        pass
    results = collect()
    incidents = [r for r in results if any(f["severity"] in SEVERE for f in r["findings"])]
    for r in results:
        sev = [f for f in r["findings"] if f["severity"] in SEVERE]
        print(f"{r['vm']['name']} ({r['vm']['kind']}): 최근 {WINDOW_MIN}분 {len(r['rows'])}건, "
              + (f"장애 의심 {len(sev)}건: " + ", ".join(f"[{f['severity']}] {f['title']}" for f in sev) if sev else "이상 없음"))
    if not incidents:
        print("감지된 장애가 없습니다. 종료합니다.")
        return 0

    tri = triage(incidents)
    if tri:
        print(f"\nAI 1차 분석 (참고용): {tri.get('summary')}")
        for h in tri.get("hypotheses", [])[:3]:
            print(f"  - 원인 후보: {h.get('cause')} (근거: {h.get('evidence')})")
    query = (tri or {}).get("search_query") or "프록시 장애 " + " ".join(
        f["title"] for i in incidents for f in i["findings"] if f["severity"] in SEVERE)[:80]
    cases = similar(query)
    print(f"\n유사 사례·가이드 (검색어: {query}):" + "".join(f"\n  - {c['title']}" for c in cases))

    src = read_source()
    fix = propose_fix(incidents, tri, cases, src)
    if fix:
        print(f"\n원인: {fix.get('cause')}  (원인 커밋 {fix.get('suspect_commit') or '-'}, 신뢰도 {fix.get('confidence')})")
        if fix["applicable"]:
            print(f"수정안: {fix['file']}\n  - {fix['before'].strip()}\n  + {fix['after'].strip()}\n  {fix.get('fix_summary')}")
        else:
            print("자동 수정안 없음 → Task 에 수동 조치로 남깁니다.")
    if args.dry_run:
        print("[dry-run] Teams 승인 → Task 생성 → " + ("수정 PR 생성 → " if fix and fix["applicable"] else "")
              + "장애 대응 이력 Wiki → 결과 알림 (아무것도 바꾸지 않음)")
        return 0

    import wiki_search
    from proxy_status_sync import devops
    dv = devops()
    vms = ", ".join(i["vm"]["name"] for i in incidents)
    headline = f"{vms} " + ", ".join(sorted({f["title"] for i in incidents for f in i["findings"] if f["severity"] in SEVERE}))[:120]
    items = [f"AI 1차 분석 (참고용): {(tri or {}).get('summary', '분석 실패')}"]
    items += [f"[{f['severity']}] {i['vm']['name']}: {f['title']} — {f['detail']}"
              for i in incidents for f in i["findings"] if f["severity"] in SEVERE]
    if fix:
        items.append(f"원인: {fix.get('cause')} (원인 커밋 {fix.get('suspect_commit') or '-'})")
        items.append(f"수정안: {fix['file']} `{fix['before'].strip()}` → `{fix['after'].strip()}` ({fix.get('fix_summary')})"
                     if fix["applicable"] else "수정안: 자동 수정안 없음(수동 조치)")
    links = [(f"사례: {c['title']}", c["link"]) for c in cases[:2]]
    decision, ap = ask_approval(headline, items, links, when)
    step = ap.step
    try:
        if not decision:
            reason = "거절(No)" if decision is False else "시간 초과"
            if os.environ.get("TEAMS_WEBHOOK_URL"):
                from teams_tools import teams_notify
                teams_notify.send(f"{headline} 장애는 {reason}로 조치하지 않았습니다.", "[프록시 장애] 조치 안 함",
                                  facts=[("감지 시각", when)])
            ap.finish(f"{reason}: 아무것도 바꾸지 않았습니다.")
            print(f"승인되지 않아 종료합니다: {reason}")
            return 1

        step("Task 생성 중…")
        cases_html = "".join(f"<li>{html.escape(c['title'])} — {html.escape(c['path'])}</li>" for c in cases)
        body = ("<p>" + html.escape((tri or {}).get("summary", "")) + "</p><ul>"
                + "".join(f"<li>{html.escape(x)}</li>" for x in items[1:]) + "</ul>"
                + (f"<p><b>영향·확인</b>: {html.escape(fix.get('impact', ''))}</p>" if fix else "")
                + (f"<p><b>참고 Wiki</b></p><ul>{cases_html}</ul>" if cases else "")
                + "<p>등록: incident_response.py (시나리오 3)</p>")
        with span("task_create") as s:
            task, parent = create_task(dv, when, headline, body)
            s.set(task=task, pbi=parent["pbi"])
        print(f"Task #{task}: {dv.task_url(task)}")
        step(f"Task #{task} {parent['title']}", "ok", ("Task 열기", dv.task_url(task)))

        pr = None
        if fix and fix["applicable"]:
            step("수정 브랜치·PR 생성 중…")
            with span("pull_request", file=fix["file"]) as s:
                pr = create_pr(dv, fix, task, when)
                s.set(pr=pr["id"], branch=pr["branch"], commit=pr["commit"])
            print(f"PR !{pr['id']}: {pr['url']}")
            step(f"수정 PR !{pr['id']} (브랜치 {pr['branch']}, 병합·배포는 검토 후)", "ok", ("PR 열기", pr["url"]))
            dv.add_comment(task, f"<p>수정 PR: <a href=\"{html.escape(pr['url'])}\">!{pr['id']}</a> "
                                 f"(브랜치 {html.escape(pr['branch'])}). 병합 후 deploy.sh 로 배포하고 traffic.sh 로 확인하세요.</p>")
        else:
            step("자동 수정안이 없어 PR 은 만들지 않았습니다(Task 에서 수동 조치)", "info")

        step("장애 대응 이력 Wiki 갱신 중…")
        write_history(history_entry(when, incidents, tri, fix, cases, task, pr, dv.task_url(task)), when, task)
        wiki_url = dv.wiki_url(HISTORY_PAGE)
        step("장애 대응 이력 Wiki 에 이번 건을 더했습니다", "ok", ("이력 Wiki", wiki_url))
        print(f"장애 대응 이력: {wiki_url}")

        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            with span("teams_notify", kind="result"):
                teams_notify.send(f"{headline} 장애 조치를 시작했습니다. "
                                  + ("수정 PR 을 검토·병합한 뒤 배포해 주세요." if pr else "자동 수정안이 없어 Task 에서 수동 조치가 필요합니다.")
                                  + f"\n\n**처리 단계**\n{ap.steps_text()}",
                                  "[프록시 장애] 조치 진행",
                                  facts=[("Task", f"[#{task}]({dv.task_url(task)})")]
                                        + ([("PR", f"[!{pr['id']}]({pr['url']})")] if pr else []) + [("감지 시각", when)],
                                  buttons=[("Task 열기", dv.task_url(task))] + ([("PR 열기", pr["url"])] if pr else [])
                                          + [("이력 Wiki", wiki_url)])
        ap.finish("조치를 시작했습니다. " + ("수정 PR 을 검토·병합한 뒤 배포하세요." if pr else "Task 에서 수동 조치하세요."),
                  [("Task 열기", dv.task_url(task))] + ([("PR 열기", pr["url"])] if pr else []) + [("이력 Wiki", wiki_url)])
        return 0
    except Exception as exc:
        ap.step(f"실패: {exc}", "fail")
        ap.finish(f"처리 중 오류가 났습니다: {exc}", ok=False)
        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            teams_notify.send(f"{headline} 장애 조치 중 오류가 나서 멈췄습니다: {exc}\n\n**처리 단계**\n{ap.steps_text()}",
                              "[프록시 장애] 처리 실패", facts=[("감지 시각", when)])
        raise
    finally:
        ap.close()


def main() -> int:
    from proxy_status_sync import Tee
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.is_file() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
        LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    log = LOG_FILE.open("a", encoding="utf-8")
    log.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: incident_response.py {' '.join(sys.argv[1:])} =====\n")
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(out, log), Tee(err, log)
    try:
        p = argparse.ArgumentParser(description="시나리오 3: 프록시 장애 감지 및 자동 조치")
        p.add_argument("--dry-run", action="store_true", help="1~4단계(감지·분석·수정안)만 하고 아무것도 바꾸지 않음")
        args = p.parse_args()
        try:
            with trace("scenario3", "incident_response", dry_run=args.dry_run, window_min=WINDOW_MIN) as t:
                code = run(args)
                t.set(exit=code)
                return code
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    finally:
        sys.stdout, sys.stderr = out, err
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
