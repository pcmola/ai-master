"""Inject a realistic misconfiguration into the proxy-config repo to test incident detection and repair.

장애 대응 흐름(탐지 → 원인 분석 → Task·PR → 위키 이력)을 시험하려고, Azure Repos 의 proxy-config 저장소에
운영자가 실수로 넣은 것처럼 보이는 커밋을 만든다. 커밋 메시지에는 장애라는 말을 쓰지 않는다.
원인은 로그와 저장소 변경 이력으로 찾아야 하므로 시나리오 정의는 proxy-config 가 아니라 여기에 둔다.

사용 예:
    python proxy_tools/proxy_fault_inject.py list
    python proxy_tools/proxy_fault_inject.py apply whitelist-typo                  # 로컬 커밋만
    python proxy_tools/proxy_fault_inject.py apply bad-dns --push --deploy <VM_IP>
    python proxy_tools/proxy_fault_inject.py status                                 # 지금 심어진 장애가 있는지

--deploy 는 proxy-config 의 scripts/deploy.sh 를 --restart 로 실행한 뒤 scripts/traffic.sh 로 로그를 만든다.
Windows 에서는 Git Bash(bash.exe)가 필요하다.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import ENV_FILE, PROJECT_ROOT  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

# proxy-config 의 allowed_clients 에 적힌 실제 IP. 저장소가 공개라 .env 의 PROXY_ALLOWED_CLIENTS 첫 값을 쓴다.
CLIENT = os.environ.get("PROXY_ALLOWED_CLIENTS", "203.0.113.10/32").split(",")[0].strip().split("/")[0]

REPO = Path(os.environ.get("PROXY_CONFIG_DIR", PROJECT_ROOT.parent / "proxy-config"))
GIT_BASH = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"

# file 안에서 before 가 정확히 한 번 나와야 한다. symptom 은 시험하는 사람만 보는 기대 증상이다.
SCENARIOS = {
    "whitelist-typo": {
        "file": "squid/lists/whitelist.txt",
        "before": ".data.go.kr\n",
        "after": ".date.go.kr\n",
        "message": "Normalize whitelist entries",
        "symptom": "허용 클라이언트의 *.data.go.kr CONNECT 가 TCP_DENIED/403 → whitelisted_denied, denied_burst",
    },
    "client-acl-typo": {
        "file": "squid/proxy.conf",
        "before": f" {CLIENT}/32",  # allowed_clients 줄의 이 IP 만 바꾼다(같은 줄에 클라이언트가 여럿이어도 됨)
        "after": f" {CLIENT[:-1]}/32",  # 마지막 숫자 하나를 빠뜨린 오타
        "message": "Update office egress IP for allowed_clients",
        "symptom": f"{CLIENT} 의 모든 요청이 TCP_DENIED/403 → whitelisted_denied, denied_burst",
    },
    "bad-dns": {
        "file": "squid/proxy.conf",
        "before": "coredump_dir /var/spool/squid\n",
        "after": ("# Use the internal resolver instead of the Azure default\n"
                  "dns_nameservers 192.0.2.53\ndns_timeout 5 seconds\n\ncoredump_dir /var/spool/squid\n"),
        "message": "Use internal DNS resolver",
        "symptom": "DNS 조회 실패로 CONNECT 가 503 (약 5초 지연) → upstream_error(high), cache.log 에 DNS timeout",
    },
}


def git(*args: str) -> str:
    res = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


def bash(script: str, *args: str):
    # PATH 의 bash 가 WSL(System32\bash.exe)이면 Windows 경로를 못 읽으므로 Git Bash 를 먼저 쓴다.
    exe = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")
    if not exe:
        raise RuntimeError("bash 를 찾지 못했습니다. Git for Windows 를 설치하세요.")
    res = subprocess.run([exe, (REPO / script).as_posix(), *args])
    if res.returncode != 0:
        raise RuntimeError(f"{script} 종료 코드 {res.returncode}")


def active() -> list[str]:
    found = []
    for name, s in SCENARIOS.items():
        path = REPO / s["file"]
        if path.is_file() and s["after"] in path.read_text(encoding="utf-8"):
            found.append(name)
    return found


def apply(name: str, push: bool, deploy: str | None):
    s = SCENARIOS[name]
    if git("status", "--porcelain"):
        raise RuntimeError(f"{REPO} 에 커밋하지 않은 변경이 있습니다. 정리한 뒤 다시 실행하세요.")
    path = REPO / s["file"]
    text = path.read_text(encoding="utf-8")
    if text.count(s["before"]) != 1:
        raise RuntimeError(f"{s['file']} 가 예상한 정상 상태가 아닙니다(이미 장애가 심어졌거나 내용이 바뀜).")
    path.write_text(text.replace(s["before"], s["after"]), encoding="utf-8", newline="\n")
    git("add", "--", s["file"])
    git("commit", "-q", "-m", s["message"])
    print(f"커밋: {git('log', '-1', '--format=%h %s')}")
    print(f"기대 증상: {s['symptom']}")
    if push:
        git("push", "-q", "origin", "HEAD")
        print("push 완료")
    if deploy:
        bash("scripts/deploy.sh", deploy, "--restart")
        try:
            bash("scripts/traffic.sh", deploy)
        except RuntimeError:
            print("테스트 트래픽에서 실패가 나왔습니다(예상한 결과). 이제 탐지를 실행하세요:")
            print(f"  python proxy_tools/proxy_log_detect.py detect --host {deploy}")


def main() -> int:
    p = argparse.ArgumentParser(description="proxy-config 저장소에 장애 시나리오 커밋을 넣는다(장애 대응 테스트용).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="시나리오 목록")
    sub.add_parser("status", help="저장소에 심어진 시나리오 확인")
    a = sub.add_parser("apply", help="시나리오 커밋 생성")
    a.add_argument("scenario", choices=sorted(SCENARIOS))
    a.add_argument("--push", action="store_true", help="origin 으로 push")
    a.add_argument("--deploy", metavar="IP", help="VM 에 배포하고 테스트 트래픽을 보낸다")
    args = p.parse_args()

    if not (REPO / ".git").exists():
        print(f"ERROR: proxy-config 저장소가 없습니다: {REPO} (PROXY_CONFIG_DIR 로 지정 가능)", file=sys.stderr)
        return 1
    try:
        if args.cmd == "list":
            for name, s in SCENARIOS.items():
                print(f"{name:16} {s['file']:28} {s['symptom']}")
        elif args.cmd == "status":
            found = active()
            print(f"심어진 시나리오: {', '.join(found)}" if found else "심어진 시나리오 없음 (정상 상태)")
        else:
            apply(args.scenario, args.push, args.deploy)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
