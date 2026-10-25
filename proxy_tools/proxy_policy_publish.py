"""Approval step for proxy_policy_monitor: when a proxy policy (Squid or nginx) changes, register a Task and update the Wiki.

흐름
1. proxy_policy_monitor 로 프록시 VM 전체(Squid·nginx)의 정책을 SSH 로 읽어 VM 별 기준과 비교한다. 중지된 VM 은 건너뛴다.
2. 바뀐 내용(VM 별 추가/삭제)을 보여 주고 콘솔에서 승인을 받는다.
3. 기준이 있던 VM 에 변경이 있으면 Azure DevOps Task 를 하나 만든다(바뀐 VM 을 모두 적는다).
4. wiki-repo 의 '프록시 정책 현황' 문서를 VM 별 절로 다시 쓰고 commit/push 한다.
5. 모두 성공한 뒤에만 VM 별 기준 스냅샷과 변경 이력을 갱신한다. 중간에 실패하면 다음 실행 때 같은 변경을 다시 처리한다.

기준 스냅샷이 없는 VM(처음 보는 VM)은 Task 없이 지금 정책을 기준으로 저장하고 문서에 올린다.

사용 예:
    python proxy_tools/proxy_policy_publish.py            # 변경이 있으면 승인 후 Task 등록 + Wiki 갱신
    python proxy_tools/proxy_policy_publish.py --dry-run  # 무엇을 할지 출력만 (Task·Wiki·스냅샷 변경 없음)

필요한 설정(.env): AZURE_DEVOPS_ORG / PROJECT / PAT (Work Items·Code Read & write), PROXY_SSH_USER, PROXY_SSH_KEY
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import ENV_FILE, WIKI_REPO  # noqa: E402
from common.trace import span, trace  # noqa: E402
from proxy_policy_monitor import STATE_DIR, check_all, describe, print_result, snapshot_path  # noqa: E402

WIKI_PAGE = (WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "01.-공통" / "06.-네트워크-및-Proxy"
             / "01.-네트워크-및-Proxy-현황-파악" / "프록시-정책-현황.md")
HISTORY = STATE_DIR / "publish_history.json"
KST = timezone(timedelta(hours=9))
TAGS = "프록시; 정책; 자동등록"


def load_env():
    """.env 를 읽는다(기존 환경 변수 우선). wiki_documents 는 import 시 종료 통계를 출력하므로 쓰지 않는다."""
    path = ENV_FILE
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


load_env()


# ---------------------------------------------------------------- Azure DevOps

class DevOps:
    def __init__(self):
        load_env()
        self.org = os.environ.get("AZURE_DEVOPS_ORG", "").strip()
        self.project = os.environ.get("AZURE_DEVOPS_PROJECT", "").strip()
        pat = os.environ.get("AZURE_DEVOPS_PAT", "").strip()
        if not (self.org and self.project and pat):
            raise RuntimeError("AZURE_DEVOPS_ORG / AZURE_DEVOPS_PROJECT / AZURE_DEVOPS_PAT 를 .env 에 설정하세요.")
        self.auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
        self.base = f"https://dev.azure.com/{quote(self.org)}/{quote(self.project)}"

    def request(self, method: str, path: str, body, content_type: str = "application/json") -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None  # GET 은 본문 없이
        req = Request(self.base + path, method=method, data=data,
                      headers={"Authorization": self.auth, "Content-Type": content_type})
        try:
            with urlopen(req, timeout=30) as res:
                return json.loads(res.read() or b"{}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            hint = " (PAT 에 Work Items Read & write 권한이 있는지 확인하세요)" if exc.code in (401, 403) else ""
            raise RuntimeError(f"Azure DevOps {method} {path.split('?')[0]} 실패: HTTP {exc.code}{hint} {detail}")
        except URLError as exc:
            raise RuntimeError(f"Azure DevOps 연결 실패: {exc.reason}")

    def create_task(self, title: str, description: str) -> int:
        ops = [{"op": "add", "path": "/fields/System.Title", "value": title},
               {"op": "add", "path": "/fields/System.Description", "value": description},
               {"op": "add", "path": "/fields/System.Tags", "value": TAGS}]
        return self.request("POST", "/_apis/wit/workitems/$Task?api-version=7.1", ops,
                            "application/json-patch+json")["id"]

    def add_comment(self, task_id: int, text: str):
        self.request("POST", f"/_apis/wit/workItems/{task_id}/comments?api-version=7.1-preview.4", {"text": text})

    def task_url(self, task_id: int) -> str:
        return f"{self.base}/_workitems/edit/{task_id}"

    def wiki_url(self, page: Path) -> str:
        # 파일 경로의 '-' 는 위키 제목의 공백, '%2D' 는 실제 하이픈이다.
        rel = page.relative_to(WIKI_REPO).with_suffix("").as_posix()
        title_path = "/" + "/".join(unquote(p.replace("-", " ")) for p in rel.split("/"))
        wiki = os.environ.get("AZURE_DEVOPS_WIKI", "").strip()
        return f"{self.base}/_wiki/wikis/{quote(wiki)}?pagePath={quote(title_path)}"


def short(item: str) -> str:
    """Task 제목용 짧은 표기."""
    head, _, rest = item.partition(" ")
    return rest.lstrip(".") if head in ("domain", "client") else rest


def task_body(changed: list[dict], when: str, wiki_url: str | None) -> tuple[str, str]:
    parts = []
    for r in changed:
        c = r["change"]
        bits = [f"+{short(a)}" for a in c["added"][:2]] + [f"-{short(d)}" for d in c["removed"][:2]]
        parts.append(f"{r['vm']['name']} {', '.join(bits)}")
    title = f"[프록시] 정책 변경: {' / '.join(parts)}"[:250]
    li = lambda items: "".join(f"<li>{html.escape(describe(i))}</li>" for i in items) or "<li>없음</li>"
    body = f"<p>프록시 정책 변경이 {html.escape(when)} 에 감지되었습니다.</p>"
    for r in changed:
        vm, c = r["vm"], r["change"]
        body += (f"<h3>{html.escape(vm['name'])} ({html.escape(vm['kind'])}, {html.escape(vm['ip'])})</h3>"
                 f"<p><b>추가</b></p><ul>{li(c['added'])}</ul><p><b>삭제</b></p><ul>{li(c['removed'])}</ul>")
    body += ((f'<p>현황 문서: <a href="{html.escape(wiki_url)}">프록시 정책 현황</a></p>' if wiki_url else "")
             + "<p>등록: proxy_policy_publish.py (자동)</p>")
    return title, body


# ---------------------------------------------------------------- Wiki

def vm_section(n: int, vm: dict, items: list[str], note: str = "") -> list[str]:
    pick = lambda head: [i.split(" ", 1)[1] for i in items if i.startswith(head + " ")]
    lines = [f"## {n}. {vm['name']} ({'Squid 포워드 프록시' if vm['kind'] == 'squid' else 'NGINX 리버스 프록시'})", ""]
    if note:
        lines += [f"> {note}", ""]
    lines += ["| 항목 | 값 |", "|---|---|", f"| 공용 IP | {vm['ip']} |",
              f"| 포트 | {'8080 (Squid, podman 컨테이너)' if vm['kind'] == 'squid' else '80 (NGINX, podman 컨테이너)'} |",
              f"| 정책 항목 | {len(items)}개 |", ""]
    if vm["kind"] == "squid":
        domains = pick("domain")
        lines += ["### 접속 허용 도메인", ""]
        lines += (["| # | 도메인 | 범위 |", "|---|---|---|"]
                  + [f"| {i} | {d.lstrip('.')} | {'하위 도메인 포함' if d.startswith('.') else '해당 도메인만'} |"
                     for i, d in enumerate(domains, 1)]) if domains else ["목적지 제한이 없습니다(허용 클라이언트는 모든 사이트에 접속 가능)."]
    else:
        routes = pick("route")
        lines += ["### 가상 도메인 라우팅", ""]
        lines += (["| 가상 도메인 | 전달 대상 |", "|---|---|"]
                  + [f"| {r.split(' → ')[0]} | {r.split(' → ')[-1]} |" for r in routes]) if routes else ["라우팅 설정을 찾지 못했습니다."]
    lines += ["", "### 접속 허용 클라이언트", ""] + ([f"- `{c}`" for c in pick("client")] or ["- 설정에서 찾지 못했습니다."])
    rules = pick("rule")
    if rules:
        lines += ["", "### 기타 규칙", ""] + [f"- `{r}`" for r in rules]
    return lines + [""]


def render_page(results: list[dict], history: list[dict], when: str, devops: DevOps | None) -> str:
    lines = ["# 프록시 정책 현황", "",
             f"> 최종 갱신: {when} (KST) · 자동 생성 문서입니다. `proxy_policy_publish.py` 가 정책 변경을 감지할 때 다시 작성하므로 "
             "직접 수정한 내용은 다음 갱신 때 사라집니다.", ""]
    n = 0
    for r in results:
        if "policy" in r:
            n += 1
            lines += vm_section(n, r["vm"], r["policy"]["items"])
        else:  # 중지·읽기 실패: 마지막 기준이 있으면 그것으로 보여 준다
            path = snapshot_path(r["vm"]["name"])
            if path.is_file():
                n += 1
                saved = json.loads(path.read_text(encoding="utf-8"))
                lines += vm_section(n, r["vm"], saved["items"], f"이번 갱신 때 읽지 못했습니다({r['skipped']}). 마지막으로 확인한 정책입니다.")
    lines += [f"## {n + 1}. 변경 이력", "", "| 일시 | VM | 추가 | 삭제 | Task |", "|---|---|---|---|---|"]
    for h in reversed(history[-50:]):
        task = f"[#{h['task_id']}]({devops.task_url(h['task_id'])})" if h.get("task_id") and devops else "-"
        fmt = lambda items: "<br>".join(describe(i) for i in items) or "-"
        lines.append(f"| {h['when']} | {h['host']} | {fmt(h['added'])} | {fmt(h['removed'])} | {task} |")
    if not history:
        lines.append("| - | - | - | - | - |")
    lines += ["", f"## {n + 2}. 관리 방법", "",
              "- Squid: 허용 도메인은 VM 의 `/etc/proxy/lists/whitelist.txt` 에 한 줄씩 적고 "
              "`sudo podman exec squid squid -k reconfigure` 로 반영한다.",
              "- NGINX: 가상 도메인과 허용 클라이언트는 VM 의 `/etc/proxy/proxy.conf` 에서 고치고 "
              "`sudo podman exec nginx nginx -s reload` 로 반영한다.",
              "- 반영 후 `python proxy_tools/proxy_policy_publish.py` 를 실행하면 Task 등록과 이 문서 갱신이 함께 진행된다.", ""]
    return "\n".join(lines)


def git(*args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(WIKI_REPO), *args], capture_output=True)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


def publish_files(files: dict[Path, str], message: str) -> str | None:
    """files(wiki-repo 안 경로 → 내용)만 바꾼 커밋을 원격 최신 위에 만들어 push 한다. wiki-repo 에 아직 올리지 않은
    다른 로컬 커밋(예: 시나리오 2 테스트 문서)은 함께 올라가지 않고, push 뒤 로컬 브랜치를 원격 위로 다시 얹어(rebase)
    그대로 둔다. 바뀐 것이 없으면 None, 있으면 커밋 해시."""
    if not (WIKI_REPO / ".git").exists():
        raise RuntimeError(f"wiki-repo 가 git 저장소가 아닙니다: {WIKI_REPO}")
    for text in files.values():  # WIKI_DOCUMENT_RULES: UTF-8, 깨짐 문자열·대체문자 검사
        if "�" in text or "???" in text:
            raise RuntimeError("Wiki 문서 인코딩 검사 실패(대체문자 또는 ??? 포함)")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git("fetch", "-q", "origin", branch)
    base = git("rev-parse", f"origin/{branch}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    index = STATE_DIR / "wiki_index.tmp"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    run_git = lambda *a: subprocess.run(["git", "-C", str(WIKI_REPO), *a], capture_output=True, env=env, check=True
                                        ).stdout.decode("utf-8").strip()
    try:
        run_git("read-tree", base)
        for path, text in files.items():
            blob_file = STATE_DIR / "wiki_blob.tmp"
            blob_file.write_bytes(text.encode("utf-8"))
            blob = git("hash-object", "-w", str(blob_file))
            blob_file.unlink(missing_ok=True)
            run_git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path.relative_to(WIKI_REPO).as_posix()}")
        tree = run_git("write-tree")
    finally:
        index.unlink(missing_ok=True)
    if tree == git("rev-parse", f"{base}^{{tree}}"):
        print("  Wiki 문서 내용이 원격과 같아 커밋하지 않습니다.")
        return None
    commit = git("commit-tree", tree, "-p", base, "-m", message)
    git("push", "origin", f"{commit}:refs/heads/{branch}")
    git("fetch", "-q", "origin", branch)
    git("rebase", "--autostash", f"origin/{branch}")  # 로컬 전용 커밋은 방금 올린 커밋 위로 옮겨 그대로 둔다
    for path, text in files.items():
        if path.read_bytes().decode("utf-8") != text:
            raise RuntimeError(f"push 뒤 로컬 문서가 올린 내용과 다릅니다: {path}")
    return commit[:7]


def publish_wiki(text: str, message: str):
    publish_files({WIKI_PAGE: text}, message)


# ---------------------------------------------------------------- main

def confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run(args) -> int:
    results = check_all()
    print_result(results)
    changed = [r for r in results if r.get("change", {}).get("changed")]
    baseline = [r for r in results if r.get("change", {}).get("baseline")]
    if not changed and not baseline:
        print("반영할 것이 없습니다.")
        return 0
    when = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    devops = DevOps()
    wiki_url = devops.wiki_url(WIKI_PAGE)
    title = task_body(changed, when, wiki_url)[0] if changed else None
    if args.dry_run:
        print(f"[dry-run] Task 생성: {title}" if title else "[dry-run] Task 없음(기준만 새로 저장)")
        print(f"[dry-run] Wiki 갱신: {WIKI_PAGE.relative_to(WIKI_REPO).as_posix()}"
              + (f" · 기준 새로 저장: {', '.join(r['vm']['name'] for r in baseline)}" if baseline else ""))
        return 0
    if not confirm("Task 등록과 Wiki 갱신을 진행할까요?" if changed else "기준을 저장하고 Wiki 현황 문서를 만들까요?"):
        print("취소했습니다. 기준 스냅샷은 그대로 두었으므로 다음 실행 때 다시 묻습니다.")
        return 1

    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.is_file() else []
    task_id = None
    if changed:
        with span("task_create", vms=[r["vm"]["name"] for r in changed]) as s:
            task_id = devops.create_task(*task_body(changed, when, wiki_url))
            s.set(task_id=task_id)
        print(f"  Task #{task_id} 생성: {devops.task_url(task_id)}")
        history += [{"when": when, "host": r["vm"]["name"], "added": r["change"]["added"],
                     "removed": r["change"]["removed"], "task_id": task_id} for r in changed]
    names = ", ".join(r["vm"]["name"] for r in changed + baseline)
    with span("wiki_update", page=WIKI_PAGE.name):
        publish_wiki(render_page(results, history, when, devops),
                     f"프록시 정책 현황 갱신 ({names}, Task #{task_id})" if task_id else f"프록시 정책 현황 기준 저장 ({names})")
    print(f"  Wiki 갱신: {wiki_url}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in changed + baseline:
        snapshot_path(r["vm"]["name"]).write_text(json.dumps(r["policy"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"완료. 기준 스냅샷을 갱신했습니다: {names}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="프록시(Squid·NGINX) 정책 변경을 Azure DevOps Task 와 Wiki 현황에 반영한다.")
    p.add_argument("--dry-run", action="store_true", help="출력만 하고 Task·Wiki·스냅샷은 바꾸지 않음")
    args = p.parse_args()
    try:
        with trace("proxy-policy", "publish", dry_run=args.dry_run) as t:
            code = run(args)
            t.set(exit=code)
            return code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
