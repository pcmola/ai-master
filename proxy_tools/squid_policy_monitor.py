"""Read Squid policy files over SSH and produce an approval-ready change draft.

The monitor is intentionally read-only. It never edits Squid, creates an Azure
DevOps item, updates Wiki, or moves the baseline snapshot; squid_policy_publish.py
does that after approval. Settings come from .env (SQUID_HOST, SQUID_SSH_USER,
SQUID_SSH_KEY, SQUID_POLICY_ROOT, SQUID_CONTAINER, SQUID_POLICY_WRAPPER, SQUID_POLICY_SNAPSHOT).

    python proxy_tools/squid_policy_monitor.py   # 기준 대비 정책 변경을 출력만
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import shlex
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_HOST = "wjy-proxy01"
DEFAULT_USER = "azureuser"
DEFAULT_ROOT = "/etc/squid"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE  # noqa: E402

DEFAULT_SNAPSHOT = DATA_DIR / "proxy_policy_state" / "wjy-proxy01.json"
FILE_MARKER = re.compile(r"^---FILE:(.+)---$")

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def ssh_policy_command(root: str, container: str = "", wrapper: str = "") -> str:
    if wrapper:
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", wrapper):
            raise ValueError("SQUID_POLICY_WRAPPER must be a simple absolute path")
        return f"sudo -n {wrapper}"
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", root):
        raise ValueError("SQUID_POLICY_ROOT must be a simple absolute path")
    quoted = root.replace("'", "'\\''")
    # 컨테이너 안에는 sudo 가 없으므로(exec 가 이미 root) 호스트에서 읽을 때만 sudo 를 붙인다.
    sudo = "" if container else "sudo -n "
    script = (
        sudo + "find '" + quoted + "' -maxdepth 3 -type f "
        "\\( -name 'squid.conf' -o -name '*.conf' -o -name '*.acl' -o -path '*/lists/*' \\) -print "
        "| sort | while IFS= read -r f; do "
        "printf '\\n---FILE:%s---\\n' \"$f\"; " + sudo + "cat \"$f\"; "
        "done"
    )
    if container:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ValueError("SQUID_CONTAINER contains invalid characters")
        runtime = env("SQUID_RUNTIME", "podman")
        if runtime not in ("podman", "docker"):
            raise ValueError("SQUID_RUNTIME must be podman or docker")
        return f"sudo -n {runtime} exec {container} sh -c {shlex.quote(script)}"
    return script


def read_remote_policy(host: str, user: str, root: str, key: str = "", container: str = "", wrapper: str = "") -> dict[str, str]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        command.extend(["-i", key])
    command.extend([f"{user}@{host}", ssh_policy_command(root, container, wrapper)])
    try:
        result = subprocess.run(command, capture_output=True, timeout=45)
    except FileNotFoundError as exc:
        raise RuntimeError("ssh command was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("SSH policy read timed out after 45 seconds") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"ssh exited with code {result.returncode}")
    return parse_bundle(result.stdout.decode("utf-8", errors="replace"))


def parse_bundle(text: str) -> dict[str, str]:
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = FILE_MARKER.match(line)
        if match:
            current = match.group(1)
            files.setdefault(current, [])
        elif current is not None:
            files[current].append(line)
    return {name: "\n".join(lines).rstrip() + "\n" for name, lines in files.items()}


def normalize_policy(files: dict[str, str]) -> dict:
    rules = []
    for path, text in sorted(files.items()):
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"\s+", " ", line)
            if "/lists/" in path:
                # acl ... "/etc/squid/lists/whitelist.txt" 가 읽는 목록 파일: 한 줄이 하나의 항목이다.
                rules.append({"file": path, "line": number, "kind": "list", "text": f"{Path(path).name} {line}"})
                continue
            parts = line.split(" ")
            kind = parts[0].lower()
            if kind not in {"acl", "http_access", "url_regex", "urlpath_regex", "include"}:
                continue
            rules.append({"file": path, "line": number, "kind": kind, "text": line})
    digest = hashlib.sha256(
        json.dumps(rules, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"sha256": digest, "files": files, "rules": rules}


def diff_policy(previous: dict | None, current: dict) -> dict:
    old_rules = {item["text"] for item in (previous or {}).get("rules", [])}
    new_rules = {item["text"] for item in current["rules"]}
    added = sorted(new_rules - old_rules)
    removed = sorted(old_rules - new_rules)
    return {
        "changed": (previous or {}).get("sha256") != current["sha256"],
        "added": added,
        "removed": removed,
        "added_count": len(added),
        "removed_count": len(removed),
    }


def load_snapshot(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid snapshot JSON: {path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Squid policy change monitor")
    parser.parse_args()  # 옵션 없음(-h 만). 설정은 .env
    host = env("SQUID_HOST", DEFAULT_HOST)
    snapshot = Path(env("SQUID_POLICY_SNAPSHOT", str(DEFAULT_SNAPSHOT)))

    try:
        previous = load_snapshot(snapshot)
        files = read_remote_policy(host, env("SQUID_SSH_USER", DEFAULT_USER), env("SQUID_POLICY_ROOT", DEFAULT_ROOT),
                                   env("SQUID_SSH_KEY"), env("SQUID_CONTAINER"), env("SQUID_POLICY_WRAPPER"))
        change = diff_policy(previous, normalize_policy(files))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    args = argparse.Namespace(host=host)
    if change["changed"]:
        print(f"Policy changed on {args.host}: +{change['added_count']} / -{change['removed_count']}")
        for item in change["added"]:
            print(f"  ADDED   {item}")
        for item in change["removed"]:
            print(f"  REMOVED {item}")
        print("Approval required before Task/Wiki registration: python proxy_tools/squid_policy_publish.py")
    else:
        print(f"No policy change on {args.host}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
