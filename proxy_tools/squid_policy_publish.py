"""Approval step for squid_policy_monitor: when the Squid policy changes, register a Task and update the Wiki.

흐름
1. squid_policy_monitor 의 함수로 프록시 VM 의 Squid 정책(설정 + lists/whitelist.txt)을 SSH 로 읽어 기준과 비교한다.
2. 바뀐 내용(추가/삭제)을 보여 주고 승인을 받는다.
3. Azure DevOps 에 Task 를 만든다.
4. wiki-repo 의 '프록시 화이트리스트 현황' 문서를 다시 쓰고 commit/push 한다.
5. 모두 성공한 뒤에만 기준 스냅샷을 갱신한다. 중간에 실패하면 다음 실행 때 같은 변경을 다시 처리한다.

처음 실행(기준 스냅샷 없음)은 Task 없이 기준만 저장하고 위키 현황 문서를 만든다.

사용 예:
    python proxy_tools/squid_policy_publish.py            # 변경이 있으면 승인 후 Task 등록 + 위키 갱신
    python proxy_tools/squid_policy_publish.py --dry-run  # 무엇을 할지 출력만 (Task·위키·스냅샷 변경 없음)

필요한 설정(.env): AZURE_DEVOPS_ORG / PROJECT / PAT (PAT 에 Work Items Read & write 권한 필요),
                   SQUID_HOST(프록시 IP), SQUID_NAME(문서·Task 에 표시할 VM 이름, 기본 wjy-proxy01),
                   SQUID_SSH_USER, SQUID_SSH_KEY, SQUID_CONTAINER=squid, SQUID_POLICY_ROOT=/etc/squid, SQUID_POLICY_SNAPSHOT
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE, WIKI_REPO  # noqa: E402
from squid_policy_monitor import (  # noqa: E402
    DEFAULT_SNAPSHOT, diff_policy, env, load_snapshot, normalize_policy, read_remote_policy)

WIKI_PAGE = (WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "01.-공통" / "06-네트워크-및-Proxy"
             / "01-네트워크-및-Proxy-현황-파악" / "프록시-화이트리스트-현황.md")
HISTORY = DATA_DIR / "proxy_policy_state" / "publish_history.json"
KST = timezone(timedelta(hours=9))
TAGS = "프록시; 화이트리스트; 자동등록"


def load_env():
    """스크립트 옆 .env 를 읽는다(기존 환경 변수 우선). wiki_documents 는 import 시 종료 통계를 출력하므로 쓰지 않는다."""
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


load_env()  # squid_policy_monitor 의 env() 기본값보다 먼저 .env 를 반영한다


# ---------------------------------------------------------------- policy helpers

def whitelist_of(rules: list[dict]) -> list[str]:
    return [r["text"].split(" ", 1)[1] for r in rules if r["kind"] == "list" and r["file"].endswith("whitelist.txt")]


def clients_of(rules: list[dict]) -> list[str]:
    out = []
    for r in rules:
        parts = r["text"].split()
        if r["kind"] == "acl" and len(parts) > 3 and parts[1] == "allowed_clients" and parts[2] == "src":
            out += parts[3:]
    return out


def describe(item: str) -> str:
    """diff 항목을 사람이 읽기 쉬운 문장으로 바꾼다."""
    if item.startswith("whitelist.txt "):
        domain = item.split(" ", 1)[1]
        return f"허용 도메인 {domain.lstrip('.')}" + (" (하위 도메인 포함)" if domain.startswith(".") else "")
    return f"정책 규칙 `{item}`"


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


def task_body(host: str, ip: str, change: dict, whitelist: list[str], when: str, wiki_url: str | None) -> tuple[str, str]:
    added, removed = change["added"], change["removed"]
    summary = ", ".join([f"+{describe(a).replace('허용 도메인 ', '')}" for a in added[:3]]
                        + [f"-{describe(r).replace('허용 도메인 ', '')}" for r in removed[:3]])
    title = f"[프록시] {host} 정책 변경: {summary}"[:250]
    li = lambda items: "".join(f"<li>{html.escape(describe(i))}</li>" for i in items) or "<li>없음</li>"
    body = (f"<p><b>{html.escape(host)}</b> ({html.escape(ip)}) 의 Squid 정책 변경이 {html.escape(when)} 에 감지되었습니다.</p>"
            f"<p><b>추가</b></p><ul>{li(added)}</ul><p><b>삭제</b></p><ul>{li(removed)}</ul>"
            f"<p><b>변경 후 허용 도메인</b>: {html.escape(', '.join(d.lstrip('.') for d in whitelist) or '제한 없음')}</p>"
            + (f'<p>현황 문서: <a href="{html.escape(wiki_url)}">프록시 화이트리스트 현황</a></p>' if wiki_url else "")
            + "<p>등록: squid_policy_publish.py (자동)</p>")
    return title, body


# ---------------------------------------------------------------- Wiki

def render_page(host: str, ip: str, policy: dict, history: list[dict], when: str, devops: DevOps | None) -> str:
    whitelist, clients = whitelist_of(policy["rules"]), clients_of(policy["rules"])
    lines = [
        "# 프록시 화이트리스트 현황", "",
        f"> 최종 갱신: {when} (KST) · 자동 생성 문서입니다. `squid_policy_publish.py` 가 정책 변경을 감지할 때 다시 작성하므로 "
        "직접 수정한 내용은 다음 갱신 때 사라집니다.", "",
        "## 1. 대상 프록시", "",
        "| 항목 | 값 |", "|---|---|",
        f"| 호스트 | {host} |", f"| 공용 IP | {ip} |", "| 프록시 포트 | 3128 (Squid, podman 컨테이너, systemd 서비스 squid) |",
        f"| 정책 해시 | `{policy['sha256'][:12]}` |", "",
        "## 2. 접속 허용 도메인", "",
    ]
    if whitelist:
        lines += ["| # | 도메인 | 범위 |", "|---|---|---|"]
        lines += [f"| {i} | {d.lstrip('.')} | {'하위 도메인 포함' if d.startswith('.') else '해당 도메인만'} |"
                  for i, d in enumerate(whitelist, 1)]
    else:
        lines.append("목적지 제한이 없습니다(허용 클라이언트는 모든 사이트에 접속 가능).")
    lines += ["", "## 3. 접속 허용 클라이언트", ""]
    lines += [f"- `{c}`" for c in clients] or ["- 정책에서 찾지 못했습니다."]
    lines += ["", "## 4. 변경 이력", "", "| 일시 | 추가 | 삭제 | Task |", "|---|---|---|---|"]
    for h in reversed(history[-30:]):
        task = f"[#{h['task_id']}]({devops.task_url(h['task_id'])})" if h.get("task_id") and devops else "-"
        fmt = lambda items: "<br>".join(describe(i) for i in items) or "-"
        lines.append(f"| {h['when']} | {fmt(h['added'])} | {fmt(h['removed'])} | {task} |")
    if not history:
        lines.append("| - | - | - | - |")
    lines += ["", "## 5. 관리 방법", "",
              "- 허용 도메인은 VM 의 `/etc/proxy/lists/whitelist.txt` 에 한 줄씩 적고 "
              "`sudo podman exec squid squid -k reconfigure` 로 반영한다.",
              "- 반영 후 `python proxy_tools/squid_policy_publish.py` 를 실행하면 Task 등록과 이 문서 갱신이 함께 진행된다.", ""]
    return "\n".join(lines)


def git(*args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(WIKI_REPO), *args], capture_output=True)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


def publish_wiki(text: str, message: str):
    if not (WIKI_REPO / ".git").exists():
        raise RuntimeError(f"wiki-repo 가 git 저장소가 아닙니다: {WIKI_REPO}")
    git("pull", "--ff-only")
    WIKI_PAGE.parent.mkdir(parents=True, exist_ok=True)
    WIKI_PAGE.write_text(text, encoding="utf-8")
    # WIKI_DOCUMENT_RULES: 저장 후 UTF-8 로 다시 읽어 깨짐 문자열·대체문자를 검사한다.
    check = WIKI_PAGE.read_text(encoding="utf-8")
    if "�" in check or "???" in check:
        raise RuntimeError(f"위키 문서 인코딩 검사 실패: {WIKI_PAGE}")
    rel = WIKI_PAGE.relative_to(WIKI_REPO).as_posix()
    git("add", "--", rel)
    if not git("diff", "--cached", "--name-only", "--", rel):
        print("  위키 문서 내용이 같아 커밋하지 않습니다.")
        return
    git("commit", "-m", message, "--", rel)
    git("push")


# ---------------------------------------------------------------- main

def confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="Squid 정책 변경을 Azure DevOps Task 와 위키 현황에 반영한다.")
    p.add_argument("--dry-run", action="store_true", help="출력만 하고 Task·위키·스냅샷은 바꾸지 않음")
    args = p.parse_args()
    # 접속 정보는 옵션 대신 .env 에서만 읽는다
    args.host, args.name = env("SQUID_HOST"), env("SQUID_NAME", "wjy-proxy01")
    args.user, args.key = env("SQUID_SSH_USER", "azureuser"), env("SQUID_SSH_KEY")
    args.policy_root, args.container = env("SQUID_POLICY_ROOT", "/etc/squid"), env("SQUID_CONTAINER", "squid")
    args.snapshot = Path(env("SQUID_POLICY_SNAPSHOT", str(DEFAULT_SNAPSHOT)))
    if not args.host:
        p.error(".env 의 SQUID_HOST 에 프록시 IP 를 지정하세요.")

    try:
        print(f"{args.name} ({args.host}) 정책 읽는 중 ...", flush=True)
        previous = load_snapshot(args.snapshot)
        current = normalize_policy(read_remote_policy(args.host, args.user, args.policy_root, args.key,
                                                      args.container))
        change = diff_policy(previous, current)
        when = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.is_file() else []
        whitelist = whitelist_of(current["rules"])
        print(f"  허용 도메인: {', '.join(d.lstrip('.') for d in whitelist) or '제한 없음'}")

        baseline = previous is None
        if not baseline and not change["changed"]:
            print("변경 없음.")
            return 0
        if baseline:
            print("기준 스냅샷이 없어 현재 정책을 기준으로 저장하고 위키 현황 문서를 만듭니다(Task 등록 없음).")
        else:
            print(f"정책 변경 감지: 추가 {change['added_count']} / 삭제 {change['removed_count']}")
            for a in change["added"]:
                print(f"  + {describe(a)}")
            for r in change["removed"]:
                print(f"  - {describe(r)}")

        devops = DevOps()
        wiki_url = devops.wiki_url(WIKI_PAGE)
        if args.dry_run:
            if not baseline:
                title, _ = task_body(args.name, args.host, change, whitelist, when, wiki_url)
                print(f"[dry-run] Task 생성: {title}")
            print(f"[dry-run] 위키 갱신: {WIKI_PAGE.relative_to(WIKI_REPO).as_posix()}")
            return 0
        if not confirm("Task 등록과 위키 갱신을 진행할까요?" if not baseline else "위키 현황 문서를 만들까요?"):
            print("취소했습니다. 기준 스냅샷은 그대로 두었으므로 다음 실행 때 다시 묻습니다.")
            return 1

        task_id = None
        if not baseline:
            title, body = task_body(args.name, args.host, change, whitelist, when, wiki_url)
            task_id = devops.create_task(title, body)
            print(f"  Task #{task_id} 생성: {devops.task_url(task_id)}")
            history.append({"when": when, "host": args.name, "added": change["added"], "removed": change["removed"],
                            "task_id": task_id})

        page = render_page(args.name, args.host, current, history, when, devops)
        message = (f"프록시 화이트리스트 현황 갱신 ({args.name}, Task #{task_id})" if task_id
                   else f"프록시 화이트리스트 현황 문서 생성 ({args.name})")
        publish_wiki(page, message)
        print(f"  위키 갱신: {wiki_url}")

        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        print("완료. 기준 스냅샷을 갱신했습니다.")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
