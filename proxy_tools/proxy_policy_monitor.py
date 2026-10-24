"""Read the policy of every proxy VM (Squid and nginx) over SSH and show what changed since the baseline.

읽기 전용이다. 프록시 설정·Azure DevOps·Wiki·기준 스냅샷을 바꾸지 않는다. 승인 후 반영은 proxy_policy_publish.py 가 한다.

- 대상: 시나리오 2 와 같은 기준으로 찾은 프록시 VM(SCENARIO2_TAG 태그 또는 이름 규칙)의 Azure 태그 proxy=squid|nginx.
  중지된 VM 은 읽지 못하므로 건너뛴다.
- 읽는 파일: VM 의 /etc/proxy/proxy.conf 와 /etc/proxy/lists/* (컨테이너에 연결된 설정 원본)
- 정책 항목
    squid: 허용 도메인(lists/whitelist.txt), 허용 클라이언트(acl … src), http_access·http_port 규칙
    nginx: 가상 도메인 라우팅(server_name → set $upstream / proxy_pass), 허용 클라이언트(allow), deny·listen 규칙
- 기준: data/proxy_policy_state/<VM 이름>.json (proxy_policy_publish.py 가 승인 후 저장)

    python proxy_tools/proxy_policy_monitor.py   # VM 별로 기준 대비 바뀐 정책을 출력만

설정(.env): PROXY_SSH_USER(기본 azureuser), PROXY_SSH_KEY(비우면 이 PC 의 기본 키)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import DATA_DIR, ENV_FILE  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

STATE_DIR = DATA_DIR / "proxy_policy_state"
FILE_MARKER = re.compile(r"^---FILE:(.+)---$")
# 설정 원본 전체를 파일 표시와 함께 한 번에 읽는다(/etc/proxy 는 일반 사용자도 읽을 수 있다)
READ_COMMAND = ("for f in /etc/proxy/proxy.conf /etc/proxy/lists/*; do [ -f \"$f\" ] || continue; "
                "printf '\\n---FILE:%s---\\n' \"$f\"; cat \"$f\"; done")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def ssh_user() -> str:
    return env("PROXY_SSH_USER") or env("SQUID_SSH_USER") or "azureuser"  # SQUID_* 는 예전 이름


def ssh_key() -> str:
    return env("PROXY_SSH_KEY") or env("SQUID_SSH_KEY")


# ---------------------------------------------------------------- 대상 VM

def proxy_targets() -> list[dict]:
    """[{name, ip, kind, state}] — 시나리오 2 와 같은 기준으로 찾은 프록시 VM 중 종류 태그(proxy=squid|nginx)가 있는 것."""
    from proxy_status_sync import proxy_vms  # az 조회와 프록시 판별 기준을 시나리오 2 와 같게 쓴다
    out = []
    for v in proxy_vms():
        kind = v["proxy"].split()[0]  # 'squid (podman)' → squid
        if kind in ("squid", "nginx"):
            out.append({"name": v["name"], "ip": v["public_ip"], "kind": kind, "state": v["state"]})
    return out


# ---------------------------------------------------------------- 읽기·정리

def read_files(ip: str) -> dict[str, str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if ssh_key():
        command += ["-i", ssh_key()]
    command += [f"{ssh_user()}@{ip}", READ_COMMAND]
    try:
        res = subprocess.run(command, capture_output=True, timeout=45)
    except FileNotFoundError as exc:
        raise RuntimeError("ssh 명령을 찾지 못했습니다") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("SSH 응답 없음(45초). VM 이 켜져 있는지, 이 PC IP 가 NSG 에 허용됐는지 확인하세요") from exc
    if res.returncode:
        raise RuntimeError(res.stderr.decode("utf-8", errors="replace").strip() or f"ssh exit {res.returncode}")
    files: dict[str, list[str]] = {}
    current = None
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        m = FILE_MARKER.match(line)
        if m:
            current = m.group(1)
            files.setdefault(current, [])
        elif current is not None:
            files[current].append(line)
    if "/etc/proxy/proxy.conf" not in files:
        raise RuntimeError("/etc/proxy/proxy.conf 가 없습니다")
    return {name: "\n".join(lines) for name, lines in files.items()}


def clean(text: str) -> list[str]:
    """주석·빈 줄을 빼고 공백을 하나로."""
    return [re.sub(r"\s+", " ", ln.split("#", 1)[0]).strip() for ln in text.splitlines()
            if ln.split("#", 1)[0].strip()]


def policy_items(kind: str, files: dict[str, str]) -> list[str]:
    """비교 단위인 정책 항목 목록. 'domain …', 'client …', 'route …', 'rule …' 로 시작한다."""
    items: list[str] = []
    conf = clean(files.get("/etc/proxy/proxy.conf", ""))
    if kind == "squid":
        for ln in conf:
            parts = ln.split(" ")
            if parts[0] == "acl" and len(parts) > 3 and parts[2] == "src":
                items += [f"client {c}" for c in parts[3:]]
            elif parts[0] in ("acl", "http_access", "http_port"):
                items.append(f"rule {ln}")
        for name, text in files.items():
            if "/lists/" in name:
                items += [f"domain {d}" for d in clean(text)]
    else:  # nginx
        server = None
        for ln in conf:
            ln = ln.rstrip(";").strip()
            if ln.startswith("server_name "):
                server = ln.split(" ", 1)[1]
            elif ln.startswith("allow "):
                items.append(f"client {ln.split(' ', 1)[1]}")
            elif ln.startswith(("deny ", "listen ")):
                items.append(f"rule {ln}")
            elif ln.startswith("set $upstream ") or (ln.startswith("proxy_pass ") and "$" not in ln):
                items.append(f"route {'(기본: IP·기타 도메인)' if server in (None, '_') else server} → {ln.split()[-1]}")
    return sorted(set(items))


def make_policy(kind: str, files: dict[str, str]) -> dict:
    items = policy_items(kind, files)
    digest = hashlib.sha256(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"kind": kind, "items": items, "sha256": digest}


def describe(item: str) -> str:
    """정책 항목을 사람이 읽는 문장으로."""
    head, _, rest = item.partition(" ")
    if head == "domain":
        return f"허용 도메인 {rest.lstrip('.')}" + (" (하위 도메인 포함)" if rest.startswith(".") else "")
    if head == "client":
        return f"허용 클라이언트 {rest}"
    if head == "route":
        return f"가상 도메인 {rest}"
    return f"규칙 `{rest}`"


# ---------------------------------------------------------------- 기준·비교

def snapshot_path(vm: str) -> Path:
    return STATE_DIR / f"{vm}.json"


def load_snapshot(vm: str) -> dict | None:
    path = snapshot_path(vm)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"기준 스냅샷 JSON 이 깨졌습니다: {path}") from exc


def diff(previous: dict | None, current: dict) -> dict:
    old, new = set((previous or {}).get("items", [])), set(current["items"])
    return {"baseline": previous is None, "changed": previous is not None and old != new,
            "added": sorted(new - old) if previous else [], "removed": sorted(old - new)}


def check_all() -> list[dict]:
    """VM 마다 {vm, policy, change} 또는 {vm, skipped} 를 돌려준다."""
    from common.trace import span
    out = []
    for vm in proxy_targets():
        if vm["state"] != "운영":
            out.append({"vm": vm, "skipped": "중지되어 읽지 못함(기준은 그대로)"})
            continue
        with span("read_policy", vm=vm["name"], kind=vm["kind"]) as s:
            try:
                policy = make_policy(vm["kind"], read_files(vm["ip"]))
            except RuntimeError as exc:
                s.fail(str(exc)[:160])
                out.append({"vm": vm, "skipped": f"읽기 실패: {exc}"})
                continue
            change = diff(load_snapshot(vm["name"]), policy)
            s.set(items=len(policy["items"]), baseline=change["baseline"], added=len(change["added"]),
                  removed=len(change["removed"]))
        out.append({"vm": vm, "policy": policy, "change": change})
    return out


def print_result(results: list[dict]):
    for r in results:
        vm = r["vm"]
        head = f"{vm['name']} ({vm['kind']})"
        if "skipped" in r:
            print(f"{head}: 건너뜀 — {r['skipped']}")
        elif r["change"]["baseline"]:
            print(f"{head}: 기준 없음 — 지금 정책 {len(r['policy']['items'])}개 항목을 기준으로 저장할 대상")
        elif not r["change"]["changed"]:
            print(f"{head}: 변경 없음 ({len(r['policy']['items'])}개 항목)")
        else:
            print(f"{head}: 변경 추가 {len(r['change']['added'])} / 삭제 {len(r['change']['removed'])}")
            for a in r["change"]["added"]:
                print(f"  + {describe(a)}")
            for d in r["change"]["removed"]:
                print(f"  - {describe(d)}")


def main() -> int:
    argparse.ArgumentParser(description="프록시 VM(Squid·nginx) 정책 변경 확인(읽기 전용)").parse_args()
    try:
        results = check_all()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_result(results)
    if any(r.get("change", {}).get("changed") or r.get("change", {}).get("baseline") for r in results):
        print("반영하려면(승인 후 Task·Wiki): python proxy_tools/proxy_policy_publish.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
