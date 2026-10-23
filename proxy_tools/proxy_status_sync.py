"""시나리오 2: Proxy 현황 Wiki 현행화. Wiki 의 Proxy VM 표와 Azure 의 실제 VM 을 비교해, 차이가 있으면
Teams 로 승인을 받아 Azure DevOps Task 를 만들고 Wiki 표를 고친 뒤 결과를 Teams 로 알린다.

단계(노드별 트레이스는 data/traces/scenario2-YYYYMMDD.jsonl 과 콘솔에 남는다)
1. trigger      직접 실행 또는 스케줄러(--yes 로 승인 생략 가능)
2. wiki_read    Wiki 문서의 'VM 목록' 표를 읽는다
3. azure_read   리소스 그룹에서 이름이 proxy·prxy·pxy(+숫자)로 끝나는 VM 의 IP·상태·SIZE·리전을 읽는다
4. compare      신규·삭제·변경(사설/공인 IP, 상태, SIZE, 리전). 차이가 없으면 여기서 끝난다
5. approval     Teams 채널에 요약 카드(Yes/No 버튼)를 보내고 이 PC 의 승인 서버에서 답을 기다린다.
                TEAMS_WEBHOOK_URL 이 없으면 승인 화면을 브라우저로 연다. No·시간 초과면 기록하고 끝낸다
6. task_create  Azure DevOps Task(변경 내역, 담당자)
7. wiki_update  표를 Azure 값으로 고치고 변경 이력 행을 더해 wiki-repo 에 커밋한다(--push 일 때만 push)
8. report       Task 를 완료 상태로 바꾸고 Teams 에 결과를 알린다

설정(.env): AZURE_DEVOPS_ORG/PROJECT/PAT, TEAMS_WEBHOOK_URL,
            SCENARIO2_RESOURCE_GROUP(기본 WJY-PROXY-RG), SCENARIO2_ASSIGNEE(비우면 PAT 소유자)

사용 예:
    python proxy_tools/proxy_status_sync.py --dry-run     # 1~4단계만 하고 할 일을 출력
    python proxy_tools/proxy_status_sync.py               # Teams 승인 후 Task·Wiki 반영
    python proxy_tools/proxy_status_sync.py --verify      # 4단계 결과를 테스트 정답(data/scenario2)과 비교
    python proxy_tools/proxy_status_sync.py --yes --push  # 승인 없이 진행하고 Azure DevOps Wiki 로 push
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import DATA_DIR, ENV_FILE, WIKI_REPO  # noqa: E402
from common.trace import span, trace  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

KST = timezone(timedelta(hours=9))
RESOURCE_GROUP = os.environ.get("SCENARIO2_RESOURCE_GROUP", "WJY-PROXY-RG")
NAME_PATTERN = r"(?i)(proxy|prxy|pxy)\d*$"
WIKI_PAGE = (WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "01.-공통" / "06-네트워크-및-Proxy"
             / "01-네트워크-및-Proxy-현황-파악" / "Proxy-VM-현황-(테스트-구독).md")
COLUMNS = ["NAME", "RESOURCE GROUP", "LOCATION", "사설 IP", "공인 IP", "SIZE", "Proxy", "상태"]
FIELDS = ["name", "resource_group", "location", "private_ip", "public_ip", "size", "proxy", "state"]
COMPARE = {"private_ip": "사설 IP", "public_ip": "공인 IP", "state": "상태", "size": "SIZE",
           "location": "LOCATION", "resource_group": "RESOURCE GROUP"}  # Proxy(용도) 칸은 사람이 관리한다
LOCATIONS = {"koreacentral": "Korea Central", "eastus": "East US"}
EXPECTED = DATA_DIR / "scenario2" / "expected_diff.json"


# ---------------------------------------------------------------- 2·3 읽기

def az(*args: str):
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        raise RuntimeError("az CLI 를 찾지 못했습니다.")
    out = subprocess.run([exe, *args, "-o", "json"], capture_output=True, text=True, encoding="utf-8", timeout=180)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"az {' '.join(args)} 실패 (az login 필요할 수 있음)")
    return json.loads(out.stdout)


def proxy_vms(resource_group: str = RESOURCE_GROUP) -> list[dict]:
    """Wiki 표와 같은 모양으로 정리한 Proxy VM 목록(이름순)."""
    raw = az("vm", "list", "-d", "-g", resource_group, "--query",
             "[].{name:name,rg:resourceGroup,loc:location,priv:privateIps,pub:publicIps,"
             "size:hardwareProfile.vmSize,power:powerState,tags:tags}")
    vms = []
    for v in raw:
        if not re.search(NAME_PATTERN, v["name"]):
            continue
        tags = v.get("tags") or {}
        vms.append({"name": v["name"], "resource_group": v["rg"], "location": LOCATIONS.get(v["loc"], v["loc"]),
                    "private_ip": v.get("priv") or "-", "public_ip": v.get("pub") or "-", "size": v["size"],
                    "proxy": f"{tags.get('proxy', '-')} ({tags.get('install', '-')})",
                    "state": "운영" if v.get("power") == "VM running" else "중지"})
    return sorted(vms, key=lambda v: v["name"])


def table_rows(rows: list[dict]) -> str:
    head = f"| {' | '.join(COLUMNS)} |\n|{'---|' * len(COLUMNS)}\n"
    return head + "\n".join("| " + " | ".join(r[f] for f in FIELDS) + " |" for r in rows)


def read_wiki(text: str) -> list[dict]:
    """'## VM 목록' 아래 첫 표를 읽는다. 머리글 이름으로 칸을 찾으므로 칸 순서가 바뀌어도 된다."""
    section = text.split("## VM 목록", 1)
    if len(section) < 2:
        raise RuntimeError("Wiki 문서에 '## VM 목록' 절이 없습니다.")
    lines = [l.strip() for l in section[1].splitlines()]
    start = next((i for i, l in enumerate(lines) if l.startswith("|")), None)
    if start is None:
        raise RuntimeError("'## VM 목록' 아래에 표가 없습니다.")
    header = [c.strip() for c in lines[start].strip("|").split("|")]
    index = {f: header.index(c) for c, f in zip(COLUMNS, FIELDS) if c in header}
    if "name" not in index:
        raise RuntimeError(f"표에 NAME 칸이 없습니다: {header}")
    rows = []
    for l in lines[start + 2:]:
        if not l.startswith("|"):
            break
        cells = [c.strip() for c in l.strip("|").split("|")]
        rows.append({f: cells[i] if i < len(cells) else "" for f, i in index.items()})
    return rows


# ---------------------------------------------------------------- 4 비교

def compare(wiki: list[dict], azure: list[dict]) -> list[dict]:
    w, a = {r["name"]: r for r in wiki}, {r["name"]: r for r in azure}
    diffs = [{"type": "added", "name": n} for n in sorted(set(a) - set(w))]
    diffs += [{"type": "removed", "name": n} for n in sorted(set(w) - set(a))]
    for n in sorted(set(w) & set(a)):
        for f in COMPARE:
            if f in w[n] and w[n][f] != a[n][f]:
                diffs.append({"type": "changed", "name": n, "field": f, "wiki": w[n][f], "azure": a[n][f]})
    return diffs


def describe(d: dict) -> str:
    if d["type"] == "added":
        return f"신규(Azure 에만 있음): {d['name']}"
    if d["type"] == "removed":
        return f"삭제(Wiki 에만 있음): {d['name']}"
    return f"{COMPARE[d['field']]} 변경: {d['name']} ({d['wiki']} → {d['azure']})"


def verify(diffs: list[dict]) -> bool:
    if not EXPECTED.is_file():
        print(f"  [verify] 정답 파일이 없습니다: {EXPECTED} (test/scenario2/setup_env.py 를 먼저 실행)")
        return False
    want = json.loads(EXPECTED.read_text(encoding="utf-8"))["differences"]
    key = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)
    got_k, want_k = {key(d) for d in diffs}, {key(d) for d in want}
    ok = got_k == want_k
    print(f"  [verify] 정답 {len(want_k)}건 · 찾은 차이 {len(got_k)}건 → {'일치' if ok else '불일치'}")
    for k in sorted(want_k - got_k):
        print(f"    못 찾음: {k}")
    for k in sorted(got_k - want_k):
        print(f"    더 찾음: {k}")
    return ok


# ---------------------------------------------------------------- 5 승인

def wiki_links(dv) -> list[tuple[str, str]]:
    """확인용 Wiki 링크. 로컬 뷰어(이 PC 의 wiki-repo)와, 문서가 원격에 push 되어 있을 때만 Azure DevOps Wiki."""
    from urllib.parse import quote
    from common.server import site_url
    rel = WIKI_PAGE.relative_to(WIKI_REPO).with_suffix("").as_posix()
    viewer = os.environ.get("WIKI_VIEWER_URL") or site_url("wiki.local", 8765)[2]
    links = [("Wiki 문서 보기 (로컬 뷰어)", f"{viewer}/?path={quote(rel)}")]
    try:
        pushed = bool(git("ls-tree", "-r", "--name-only", "@{u}", "--", WIKI_PAGE.relative_to(WIKI_REPO).as_posix()))
    except RuntimeError:
        pushed = False
    links.append(("Azure DevOps Wiki", dv.wiki_url(WIKI_PAGE)) if pushed
                 else ("Azure DevOps Wiki 에는 없음 (로컬 전용 문서, --push 로 올림)", ""))
    return links


def ask_approval(diffs: list[dict], when: str, timeout: float, links: list[tuple[str, str]]) -> bool | None:
    from teams_tools import teams_notify
    from teams_tools.approval_local import request_approval

    items = [describe(d) for d in diffs]
    webhook = bool(os.environ.get("TEAMS_WEBHOOK_URL"))

    def notify(page_url: str, yes_url: str, no_url: str):
        if not webhook:
            return
        with span("teams_notify", kind="approval_request", buttons=3 + sum(1 for _, u in links if u)) as s:
            status = teams_notify.send(
                "Wiki 의 Proxy 현황과 Azure 의 실제 VM 이 다릅니다. 현행화할까요?\n\n" + "\n".join(f"- {i}" for i in items)
                + f"\n\n버튼은 이 알림을 보낸 PC({os.environ.get('COMPUTERNAME', '-')})에서 눌러야 합니다. "
                  f"{int(timeout // 60)}분 안에 답이 없으면 취소됩니다.",
                f"[Proxy 현황] 변경 {len(diffs)}건 · 승인 요청",
                facts=[("리소스 그룹", RESOURCE_GROUP), ("감지 시각", when), ("Wiki 문서", WIKI_PAGE.stem)]
                      + [("", label) for label, url in links if not url],
                buttons=[("Yes · 현행화", yes_url), ("No · 기록만", no_url), ("자세히 보기", page_url)]
                        + [(label.split(" (")[0], url) for label, url in links if url])
            s.set(http=status)

    with span("approval", channel="teams" if webhook else "local", timeout_s=timeout) as s:
        result = request_approval(f"Proxy 현황 변경 {len(diffs)}건", items, "Wiki 현행화와 Task 등록을 진행할까요?",
                                  timeout=timeout, open_browser=not webhook, on_ready=notify, links=links)
        s.set(decision={True: "yes", False: "no", None: "timeout"}[result])
    return result


# ---------------------------------------------------------------- 6·8 Azure DevOps

def devops():
    from squid_policy_publish import DevOps
    return DevOps()


def assignee(dv) -> str | None:
    """SCENARIO2_ASSIGNEE, 없으면 PAT 소유자의 계정(메일)."""
    if os.environ.get("SCENARIO2_ASSIGNEE"):
        return os.environ["SCENARIO2_ASSIGNEE"]
    from urllib.parse import quote
    from urllib.request import Request, urlopen
    try:  # connectionData 는 프로젝트가 아니라 조직 주소에서만 동작한다
        req = Request(f"https://dev.azure.com/{quote(dv.org)}/_apis/connectionData", headers={"Authorization": dv.auth})
        with urlopen(req, timeout=20) as res:
            me = json.load(res)["authenticatedUser"]
        return me.get("properties", {}).get("Account", {}).get("$value") or me.get("providerDisplayName")
    except (OSError, ValueError, KeyError):
        return None


def create_task(dv, diffs: list[dict], when: str, links: list[tuple[str, str]], owner: str | None) -> int:
    rows = "".join(f"<li>{html.escape(describe(d))}</li>" for d in diffs)
    title = f"[Proxy 현황] {RESOURCE_GROUP} 변경 {len(diffs)}건 현행화 ({when})"
    body = (f"<p>Wiki Proxy 현황과 Azure 실제 VM 비교 결과 차이 {len(diffs)}건이 {html.escape(when)} 에 감지되어 "
            f"Teams 에서 승인되었습니다.</p><ul>{rows}</ul>"
            + "<p>현황 문서 " + html.escape(WIKI_PAGE.stem) + ": "
            + " · ".join(f'<a href="{html.escape(u)}">{html.escape(l)}</a>' if u else html.escape(l) for l, u in links)
            + "</p>"
            "<p>등록: proxy_status_sync.py (시나리오 2)</p>")
    ops = [{"op": "add", "path": "/fields/System.Title", "value": title[:250]},
           {"op": "add", "path": "/fields/System.Description", "value": body},
           {"op": "add", "path": "/fields/System.Tags", "value": "proxy-status; scenario2"}]
    if owner:
        ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": owner})
    return dv.request("POST", "/_apis/wit/workitems/$Task?api-version=7.1", ops, "application/json-patch+json")["id"]


def complete_task(dv, task_id: int, note: str) -> str:
    """프로세스(Basic·Agile·Scrum)마다 완료 상태 이름이 달라 'Completed' 범주의 상태를 찾아 쓴다."""
    states = dv.request("GET", "/_apis/wit/workitemtypes/Task/states?api-version=7.1", None).get("value", [])
    done = next((s["name"] for s in states if s.get("category") == "Completed"), "Done")
    dv.request("PATCH", f"/_apis/wit/workitems/{task_id}?api-version=7.1",
               [{"op": "add", "path": "/fields/System.State", "value": done}], "application/json-patch+json")
    dv.add_comment(task_id, note)
    return done


# ---------------------------------------------------------------- 7 Wiki

def git(*args: str) -> str:
    res = subprocess.run(["git", "-C", str(WIKI_REPO), *args], capture_output=True)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


def updated_page(text: str, wiki: list[dict], azure: list[dict], diffs: list[dict], when: str, task: str) -> str:
    keep = {r["name"]: r.get("proxy") for r in wiki}  # Proxy(용도) 칸은 Wiki 값을 살린다
    rows = [{**a, "proxy": keep.get(a["name"]) or a["proxy"]} for a in azure]
    head, rest = text.split("## VM 목록", 1)
    lines = rest.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("|"))
    end = next((i for i in range(start, len(lines)) if not lines[i].strip().startswith("|")), len(lines))
    rest = "\n".join(lines[:start] + table_rows(rows).splitlines() + lines[end:]) + ("\n" if text.endswith("\n") else "")
    page = head + "## VM 목록" + rest
    page = re.sub(r"(> 최종 수정: )[^·\n]+", rf"\g<1>{when[:10]} ", page, count=1)
    # '## 변경 이력' 표의 마지막 행 뒤에 한 줄을 더한다(표가 없으면 절과 표를 만든다).
    row = f"| {when} | {', '.join(describe(d) for d in diffs)} | {task} |"
    lines = page.rstrip("\n").splitlines()
    sec = next((i for i, l in enumerate(lines) if l.strip() == "## 변경 이력"), None)
    if sec is None:
        lines += ["", "## 변경 이력", "", "| 일시 | 변경 내용 | Task |", "|---|---|---|", row]
    else:
        first = next((i for i in range(sec + 1, len(lines)) if lines[i].strip().startswith("|")), None)
        if first is None:
            lines[sec + 1:sec + 1] = ["", "| 일시 | 변경 내용 | Task |", "|---|---|---|", row]
        else:
            last = first
            while last + 1 < len(lines) and lines[last + 1].strip().startswith("|"):
                last += 1
            lines.insert(last + 1, row)
    return "\n".join(lines) + "\n"


def write_wiki(text: str, message: str, push: bool) -> str:
    WIKI_PAGE.write_text(text, encoding="utf-8", newline="\n")
    back = WIKI_PAGE.read_text(encoding="utf-8")  # WIKI_DOCUMENT_RULES: 다시 읽어 깨짐 검사
    if back != text or "�" in back or "???" in back:
        raise RuntimeError("Wiki 문서 인코딩 검사 실패")
    rel = WIKI_PAGE.relative_to(WIKI_REPO).as_posix()
    git("add", "--", rel)
    git("commit", "-q", "-m", message, "--", rel)
    commit = git("rev-parse", "--short", "HEAD")
    if push:
        git("push")
    return commit


# ---------------------------------------------------------------- main

def run(args) -> int:
    when = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    with span("trigger", mode="dry-run" if args.dry_run else "auto" if args.yes else "teams", push=args.push):
        pass
    with span("wiki_read", page=WIKI_PAGE.name) as s:
        text = WIKI_PAGE.read_text(encoding="utf-8-sig")
        wiki = read_wiki(text)
        s.set(rows=len(wiki), names=[r["name"] for r in wiki])
    with span("azure_read", resource_group=RESOURCE_GROUP, pattern=NAME_PATTERN) as s:
        azure = proxy_vms()
        s.set(vms=len(azure), names=[v["name"] for v in azure])
    with span("compare") as s:
        diffs = compare(wiki, azure)
        s.set(diffs=len(diffs), summary=[describe(d) for d in diffs][:6])
    print(f"\n차이 {len(diffs)}건" + "".join(f"\n  - {describe(d)}" for d in diffs))
    if args.verify:
        verify(diffs)
    if not diffs:
        print("Wiki 와 Azure 가 같습니다. 종료합니다.")
        return 0
    if args.dry_run:
        print(f"[dry-run] Teams 승인 요청 → Task 생성 → Wiki 표 {len(azure)}행으로 갱신 → 완료 보고 (아무것도 바꾸지 않음)")
        return 0

    dv = devops()
    links = wiki_links(dv)
    decision = True if args.yes else ask_approval(diffs, when, args.timeout, links)
    if not decision:
        reason = "거절(No)" if decision is False else "시간 초과"
        print(f"승인되지 않아 종료합니다: {reason}. 기록은 data/traces/scenario2-*.jsonl 에 남았습니다.")
        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            with span("teams_notify", kind="declined"):
                teams_notify.send(f"Proxy 현황 변경 {len(diffs)}건은 {reason}로 반영하지 않았습니다.",
                                  "[Proxy 현황] 반영 안 함", facts=[("감지 시각", when)])
        return 1

    with span("task_create") as s:
        owner = assignee(dv)
        task_id = create_task(dv, diffs, when, links, owner)
        s.set(task_id=task_id, assigned_to=owner)
    print(f"Task #{task_id} 생성 (담당 {owner or '-'}): {dv.task_url(task_id)}")

    with span("wiki_update", push=args.push) as s:
        page = updated_page(text, wiki, azure, diffs, when, f"[#{task_id}]({dv.task_url(task_id)})")
        commit = write_wiki(page, f"Proxy 현황 현행화: 변경 {len(diffs)}건 (Task #{task_id})", args.push)
        s.set(commit=commit, rows=len(azure))
    print(f"Wiki 현행화 커밋 {commit}" + (" · push 완료" if args.push else " (로컬 커밋, --push 로 Azure DevOps Wiki 에 반영)"))

    with span("report") as s:
        state = complete_task(dv, task_id, f"Wiki 현행화 완료: wiki-repo 커밋 {commit}"
                                           + (" (Azure DevOps Wiki push 완료)" if args.push else " (로컬 커밋)"))
        s.set(task_state=state)
        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            teams_notify.send(f"Proxy 현황 변경 {len(diffs)}건을 반영했습니다.\n\n" + "\n".join(f"- {describe(d)}" for d in diffs),
                              "[Proxy 현황] 현행화 완료",
                              facts=[("Task", f"#{task_id} ({state})"), ("Wiki 커밋", commit + (" · push" if args.push else " · 로컬")),
                                     ("담당", owner or "-")],
                              buttons=[("Task 열기", dv.task_url(task_id))])
            s.set(teams="sent")
    print(f"완료 보고: Task #{task_id} → {state}" + (", Teams 알림 보냄" if os.environ.get("TEAMS_WEBHOOK_URL") else ""))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="시나리오 2: Proxy 현황 Wiki 현행화")
    p.add_argument("--dry-run", action="store_true", help="1~4단계만 하고 할 일을 출력")
    p.add_argument("--verify", action="store_true", help="비교 결과를 테스트 정답(data/scenario2/expected_diff.json)과 대조")
    p.add_argument("--yes", action="store_true", help="승인 없이 진행(스케줄러용)")
    p.add_argument("--push", action="store_true", help="Wiki 커밋을 Azure DevOps Wiki 로 push")
    p.add_argument("--timeout", type=float, default=900, help="승인 대기 시간(초, 기본 900)")
    args = p.parse_args()
    try:
        with trace("scenario2", "proxy_status_sync", resource_group=RESOURCE_GROUP, dry_run=args.dry_run) as t:
            code = run(args)
            t.set(exit=code)
            return code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
