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
from common.trace import heartbeat, plan, replan, simulate, span, think, trace  # noqa: E402
from common.trace import step as plan_step  # noqa: E402  (run() 의 step 은 승인 화면 진행 표시)

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
FIX_ATTEMPTS = 2  # 수정안이 규칙 검증에 실패하면 이유를 돌려주고 한 번 더 만든다(Self-Correction)


# ---------------------------------------------------------------- 공통

def now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def llm(system: str, prompt: str, node: str, **attrs) -> dict | None:
    """JSON 으로 답하는 LLM 호출. 실패하면 노드에 이유를 남기고 None."""
    from proxy_status_sync import llm_client
    model = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")
    think("Tool Call", f"llm.chat(model={model}, node={node}, format=json, prompt={len(prompt):,}자)")
    with span(node, model=model, prompt_chars=len(prompt), **attrs) as s:
        stop = heartbeat(s, lambda: "LLM 응답 기다리는 중")
        try:
            res = llm_client().chat.completions.create(
                model=model, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            s.set(prompt_tokens=res.usage.prompt_tokens, completion_tokens=res.usage.completion_tokens)
            data = json.loads(res.choices[0].message.content or "{}")
            think("Observation", f"{node} 응답 항목: {', '.join(data) or '없음'} (출력 토큰 {res.usage.completion_tokens})")
            return data
        except Exception as exc:  # 분석이 실패해도 규칙 탐지 결과로는 진행할 수 있게
            s.fail(f"{type(exc).__name__}: {str(exc)[:160]}")
            think("Observation", f"{node} 실패: {type(exc).__name__}")
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
        think("Tool Call", f"ssh.read_logs(vm={vm['name']}, kind={vm['kind']}, lines=2000) → detect(window={WINDOW_MIN}m)")
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
        think("Observation", f"{vm['name']}: 최근 {WINDOW_MIN}분 {len(rows)}건, 규칙 탐지 {len(found)}건 "
                             f"(high·medium {sum(1 for f in found if f['severity'] in SEVERE)}건)")
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
    think("Tool Call", f"wiki_search.hybrid_search(query=\"{query[:60]}\", top={n})")
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
    think("Observation", f"Wiki 근거 {len(out)}건" + (f": {', '.join(o['title'] for o in out[:3])}" if out else ""))
    return out


# ---------------------------------------------------------------- 4. 설정 저장소 확인·수정안

def read_source() -> dict:
    think("Tool Call", f"git fetch·log -p ({CONFIG_REPO_NAME}: 최근 커밋 8개, squid diff 5개)")
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
    think("Observation", f"최근 커밋 {len(commits)}개 · 설정 파일 {len(files)}개"
                         + (f" · 최신 {commits[0].split('|')[0]} {commits[0].split('|')[-1][:40]}" if commits else ""))
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
    feedback = ""
    for attempt in range(1, FIX_ATTEMPTS + 1):
        fix = llm("너는 Squid 프록시 설정 변경을 검토하는 SRE 다. 최소한의 수정만 제안한다.", prompt + feedback, "llm_fix",
                  commits=len(src["commits"]), cases=len(cases), attempt=attempt)
        if not fix:
            return None
        with span("fix_check", attempt=attempt) as s:  # 규칙 검증: LLM 이 고른 줄이 실제로 그 파일에 정확히 한 번 있는가
            file, before, after = fix.get("file") or "", fix.get("before") or "", fix.get("after") or ""
            text = src["files"].get(file)
            found = text.count(before) if text is not None and before else 0
            ok = bool(file and text is not None and before and before != after and found == 1)
            reason = (None if ok else "수정 대상 없음" if not file else f"'{file}' 은 저장소에 없는 파일" if text is None
                      else "before 와 after 가 같음" if before == after else f"바꿀 줄이 파일에 {found}번 있음(정확히 1번이어야 함)")
            fix["applicable"] = ok
            s.set(file=file or "-", applicable=ok, confidence=fix.get("confidence"), reason=reason)
        if ok:
            think("Observation", f"수정안 검증 통과: {file} 의 한 줄을 바꿈 (시도 {attempt}/{FIX_ATTEMPTS})")
            break
        if not file:  # LLM 이 스스로 '고칠 설정 없음'이라고 판단한 경우는 다시 묻지 않는다
            think("Decision", "LLM 이 저장소에서 고칠 설정을 특정하지 않음 → 수동 조치로 판단")
            break
        if attempt == FIX_ATTEMPTS:
            think("Self-Correction", f"재시도 후에도 검증 실패({reason}) → 자동 수정을 포기하고 수동 조치로 전환")
            break
        think("Self-Correction", f"수정안 검증 실패({reason}) → 실패 이유를 LLM 에 돌려주고 다시 생성 "
                                 f"(재시도 {attempt}/{FIX_ATTEMPTS - 1})")
        feedback = (f"\n\n[이전 답 검증 실패] {reason}. 이전 답: file={file!r}, before={before!r}. "
                    "before 는 current_files 의 해당 파일에서 한 줄을 글자 그대로(공백 포함) 복사해야 한다. "
                    "고칠 줄을 확신할 수 없으면 file 을 \"\" 로 둔다.")
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

PLAN = [("collect", f"프록시 VM 로그 수집·규칙 탐지 (최근 {WINDOW_MIN}분)"),
        ("llm_triage", "(AI) 장애 요약·원인 후보·Wiki 검색어 도출"),
        ("similar", "Wiki 유사 장애 사례·가이드 검색 (RAG)"),
        ("source", "proxy-config 최근 변경 이력·현재 설정 확인"),
        ("llm_fix", "(AI) 원인 커밋 특정·수정안 생성 → 규칙 검증 (실패 시 자기 수정)"),
        ("approval", "Teams 승인 요청 (Yes/No)"),
        ("task_create", "Azure DevOps Task 생성"),
        ("pull_request", "수정 브랜치·PR 생성 (tests/validate.py 통과 필수)"),
        ("wiki_history", "장애 대응 이력 Wiki 갱신·결과 알림")]


def preflight(args) -> None:
    """계획 수립과 시뮬레이션: 실행 조건(dry-run)으로 단계를 정하고, 각 단계의 전제를 실행 전에 확인한다."""
    plan("프록시 장애 감지 → 원인 분석 → 수정안 → 승인 후 조치" + (" (dry-run: 수정안까지)" if args.dry_run else ""),
         PLAN[:5] if args.dry_run else PLAN)
    llm_ok = bool(os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY")
                  or os.environ.get("OPENAI_API_KEY"))
    repo_ok = (CONFIG_REPO / ".git").exists()
    checks = [("LLM", llm_ok, os.environ.get("OPENAI_MODEL", "gpt-6.1-sol") if llm_ok
               else "설정 없음 → triage·fix 는 규칙 탐지 결과만으로 진행"),
              ("proxy-config", repo_ok, str(CONFIG_REPO) if repo_ok else f"{CONFIG_REPO} 없음 → source 단계에서 중단 예상")]
    if not args.dry_run:
        webhook = bool(os.environ.get("TEAMS_WEBHOOK_URL"))
        checks += [("Teams", webhook, f"승인 카드, 대기 최대 {int(APPROVAL_TIMEOUT // 60)}분" if webhook
                    else "웹훅 없음 → 로컬 승인 화면으로 대체"),
                   ("Azure DevOps", bool(os.environ.get("AZURE_DEVOPS_PAT")), "Task·PR 생성, Wiki push")]
    simulate(checks)
    think("Simulation", "예상 분기: 장애 없음 → 1단계에서 종료 · 수정안 검증 실패 → 1회 자기 수정 후 수동 조치(PR 제외)"
                        + ("" if args.dry_run else " · 승인 No/시간 초과 → 기록만 하고 종료"))


def run(args) -> int:
    when = now()
    with span("trigger", mode="dry-run" if args.dry_run else "teams", window_min=WINDOW_MIN):
        pass
    preflight(args)
    plan_step("collect")
    results = collect()
    incidents = [r for r in results if any(f["severity"] in SEVERE for f in r["findings"])]
    for r in results:
        sev = [f for f in r["findings"] if f["severity"] in SEVERE]
        print(f"{r['vm']['name']} ({r['vm']['kind']}): 최근 {WINDOW_MIN}분 {len(r['rows'])}건, "
              + (f"장애 의심 {len(sev)}건: " + ", ".join(f"[{f['severity']}] {f['title']}" for f in sev) if sev else "이상 없음"))
    if not incidents:
        replan("high·medium 탐지 없음 → 분석·조치 단계 생략", drop=[n for n, _ in PLAN])
        print("감지된 장애가 없습니다. 종료합니다.")
        return 0
    think("Decision", f"장애 의심 VM {len(incidents)}대 ({', '.join(i['vm']['name'] for i in incidents)}) → 원인 분석 진행")

    plan_step("llm_triage")
    tri = triage(incidents)
    if tri:
        print(f"\nAI 1차 분석 (참고용): {tri.get('summary')}")
        for h in tri.get("hypotheses", [])[:3]:
            print(f"  - 원인 후보: {h.get('cause')} (근거: {h.get('evidence')})")
    rule_query = "프록시 장애 " + " ".join(
        f["title"] for i in incidents for f in i["findings"] if f["severity"] in SEVERE)[:80]
    query = (tri or {}).get("search_query") or rule_query
    if not tri:
        think("Self-Correction", "AI 1차 분석 실패 → 규칙 탐지 제목으로 검색어를 만들어 계속")
    plan_step("similar")
    cases = similar(query)
    if not cases and query != rule_query:
        think("Self-Correction", "AI 검색어로 Wiki 근거 0건 → 규칙 탐지 제목으로 검색어를 바꿔 다시 검색")
        query, cases = rule_query, similar(rule_query)
    print(f"\n유사 사례·가이드 (검색어: {query}):" + "".join(f"\n  - {c['title']}" for c in cases))

    plan_step("source")
    src = read_source()
    plan_step("llm_fix")
    fix = propose_fix(incidents, tri, cases, src)
    if fix:
        print(f"\n원인: {fix.get('cause')}  (원인 커밋 {fix.get('suspect_commit') or '-'}, 신뢰도 {fix.get('confidence')})")
        if fix["applicable"]:
            print(f"수정안: {fix['file']}\n  - {fix['before'].strip()}\n  + {fix['after'].strip()}\n  {fix.get('fix_summary')}")
        else:
            print("자동 수정안 없음 → Task 에 수동 조치로 남깁니다.")
    applicable = bool(fix and fix["applicable"])
    think("Reflection", (f"원인 커밋 {fix.get('suspect_commit') or '-'} · 신뢰도 {fix.get('confidence')} · " if fix else "원인 분석 실패 · ")
          + ("규칙 검증 통과 → 자동 수정 PR 경로" if applicable else "자동 수정 불가 → Task 수동 조치 경로")
          + f" · 근거 Wiki {len(cases)}건")
    if not applicable and not args.dry_run:
        replan("자동 수정안 없음 → PR 단계 제외", drop=["pull_request"])
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
    plan_step("approval")
    decision, ap = ask_approval(headline, items, links, when)
    step = ap.step
    try:
        if not decision:
            reason = "거절(No)" if decision is False else "시간 초과"
            replan(f"승인 {reason} → 조치 단계 생략, 기록만 남김", drop=["task_create", "pull_request", "wiki_history"])
            if os.environ.get("TEAMS_WEBHOOK_URL"):
                from teams_tools import teams_notify
                teams_notify.send(f"{headline} 장애는 {reason}로 조치하지 않았습니다.", "[프록시 장애] 조치 안 함",
                                  facts=[("감지 시각", when)])
            ap.finish(f"{reason}: 아무것도 바꾸지 않았습니다.")
            print(f"승인되지 않아 종료합니다: {reason}")
            return 1

        think("Decision", "승인(Yes) → 조치 단계 실행")
        plan_step("task_create")
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
            plan_step("pull_request")
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

        plan_step("wiki_history")
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
