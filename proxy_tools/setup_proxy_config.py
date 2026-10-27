"""Create the proxy-config repo (Squid settings for scenario 3) in your Azure DevOps project from the seed in this folder.

proxy-config 는 ai-master 와 별개인 Azure Repos 저장소다(시나리오 3 이 원인 커밋을 찾고 수정 PR 을 만드는 곳).
ai-master 만 받은 사람도 시나리오 3 을 쓸 수 있도록, proxy_tools/proxy-config-seed/ 로 저장소를 만들고 로컬에 받는다.

1. PROXY_CONFIG_DIR(기본 ../proxy-config)에 이미 Git 저장소가 있으면 아무것도 하지 않는다.
2. .env 의 Azure DevOps 프로젝트에 proxy-config 저장소가 있고 내용이 있으면 받기만 한다.
3. 없으면 저장소를 만들고(비어 있으면 그대로 쓰고), 시드의 allowed_clients 를 PROXY_ALLOWED_CLIENTS 로 채워
   첫 커밋을 push 한다.

사용 예:
    python proxy_tools/setup_proxy_config.py --check   # 지금 상태만 출력 (아무것도 만들지 않음)
    python proxy_tools/setup_proxy_config.py           # 저장소 만들기·받기

필요한 설정(.env): AZURE_DEVOPS_ORG / PROJECT / PAT (Code Read & write), PROXY_ALLOWED_CLIENTS, PROXY_CONFIG_DIR(선택)
시드에는 실제 IP 를 두지 않는다(ai-master 는 공개 저장소). 커밋 이력도 없으므로, 원인 분석에 쓸 변경 이력은
proxy_fault_inject.py 로 장애를 재현하면서 쌓인다.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import PROJECT_ROOT  # noqa: E402
from proxy_policy_publish import DevOps  # noqa: E402  (.env 도 여기서 읽는다)

REPO = Path(os.environ.get("PROXY_CONFIG_DIR") or PROJECT_ROOT.parent / "proxy-config")
SEED = Path(__file__).resolve().parent / "proxy-config-seed"
NAME = "proxy-config"
EXAMPLE_CLIENT = "203.0.113.10/32"  # .env.sample·시드의 예시 값(문서용 대역)


def git(cwd: Path, *args: str, auth: str | None = None) -> str:
    # PAT 는 이번 명령에만 헤더로 넘긴다(.git/config·원격 주소에 남기지 않는다).
    pre = ["-c", f"http.extraHeader=Authorization: {auth}"] if auth else []
    res = subprocess.run(["git", *pre, *args], cwd=cwd, capture_output=True, stdin=subprocess.DEVNULL)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]} 실패: {out}")
    return out


def clients() -> list[str]:
    """PROXY_ALLOWED_CLIENTS(쉼표 구분)를 CIDR 목록으로. IP 하나는 /32 로 적는다(proxy_fault_inject 가 'IP/32' 를 찾는다)."""
    out = []
    for c in os.environ.get("PROXY_ALLOWED_CLIENTS", "").split(","):
        c = c.strip()
        if not c:
            continue
        c = c if "/" in c else f"{c}/32"
        try:
            ipaddress.ip_network(c)
        except ValueError:
            raise RuntimeError(f"PROXY_ALLOWED_CLIENTS 의 주소 형식이 틀렸습니다: {c}")
        out.append(c)
    if not out or out == [EXAMPLE_CLIENT]:
        raise RuntimeError("PROXY_ALLOWED_CLIENTS 에 프록시를 쓸 PC 의 공인 IP 를 넣으세요(예시 값 그대로면 접속이 막힙니다).")
    return out


def identity() -> list[str]:
    """첫 커밋의 작성자. 전역 설정이 없어도 ai-master 에 저장소별로 정한 user.name/email 을 쓴다.
    없으면 원격 저장소를 만들기 전에 멈춘다(만든 뒤에 실패하면 빈 저장소만 남는다)."""
    def get(key: str) -> str:
        res = subprocess.run(["git", "config", key], cwd=PROJECT_ROOT, capture_output=True, stdin=subprocess.DEVNULL)
        return res.stdout.decode("utf-8", errors="replace").strip()
    name, email = get("user.name"), get("user.email")
    if not (name and email):
        raise RuntimeError("Git 작성자 정보가 없습니다. git config --global user.name / user.email 을 먼저 설정하세요.")
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def find_remote(dv: DevOps) -> tuple[dict | None, str]:
    """프로젝트의 proxy-config 저장소(없으면 None)와 프로젝트 id."""
    repos = dv.request("GET", "/_apis/git/repositories?api-version=7.1", None)["value"]
    if not repos:
        raise RuntimeError(f"프로젝트 {dv.project} 의 저장소 목록이 비어 있습니다.")
    remote = next((r for r in repos if r["name"].lower() == NAME), None)
    return remote, repos[0]["project"]["id"]


def seed(dv: DevOps, remote_url: str, cidrs: list[str], author: list[str]):
    """시드를 복사해 allowed_clients 를 채우고 첫 커밋을 push 한다."""
    REPO.mkdir(parents=True, exist_ok=True)
    for src in SEED.rglob("*"):
        if src.is_file():
            dst = REPO / src.relative_to(SEED)
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            # VM 으로 올리는 파일·셸 스크립트는 LF 여야 한다(ai-master 를 CRLF 로 받았어도)
            dst.write_bytes(data if src.suffix == ".png" else data.replace(b"\r\n", b"\n"))
    conf = REPO / "squid" / "proxy.conf"
    text, n = re.subn(r"^acl allowed_clients src .*$", "acl allowed_clients src " + " ".join(cidrs),
                      conf.read_text(encoding="utf-8"), count=1, flags=re.M)
    if n != 1:
        raise RuntimeError("시드 proxy.conf 에 'acl allowed_clients src' 줄이 없습니다.")
    conf.write_text(text, encoding="utf-8", newline="\n")
    git(REPO, "init", "-q", "-b", "main")
    git(REPO, "add", "-A")
    git(REPO, *author, "commit", "-q", "-m", "초기 설정 (ai-master proxy-config-seed)")
    git(REPO, "remote", "add", "origin", remote_url)
    git(REPO, "push", "-q", "-u", "origin", "main", auth=dv.auth)


def main() -> int:
    p = argparse.ArgumentParser(description="시나리오 3 용 proxy-config 저장소를 Azure DevOps 에 만들고 로컬에 받는다.")
    p.add_argument("--check", action="store_true", help="지금 상태만 출력하고 아무것도 만들지 않음")
    args = p.parse_args()
    try:
        if (REPO / ".git").exists():
            origin = git(REPO, "remote", "get-url", "origin") if git(REPO, "remote") else "(origin 없음)"
            print(f"이미 있음: {REPO}  origin={origin}")
            return 0
        if REPO.exists() and any(REPO.iterdir()):
            raise RuntimeError(f"{REPO} 가 Git 저장소가 아닌데 비어 있지도 않습니다. 비우거나 PROXY_CONFIG_DIR 를 바꾸세요.")
        dv = DevOps()
        remote, project_id = find_remote(dv)
        has_content = bool(remote and remote.get("defaultBranch"))
        print(f"로컬: {REPO} (없음)")
        print(f"Azure Repos {dv.project}/{NAME}: "
              + ("있음 → 받기만 함" if has_content else "비어 있음 → 시드로 첫 커밋" if remote else "없음 → 만들고 시드로 첫 커밋"))
        if args.check:
            return 0
        if has_content:
            git(REPO.parent, "clone", "-q", remote["remoteUrl"], str(REPO), auth=dv.auth)
        else:
            cidrs, author = clients(), identity()
            if not remote:
                remote = dv.request("POST", "/_apis/git/repositories?api-version=7.1",
                                    {"name": NAME, "project": {"id": project_id}})
                print(f"저장소 만듦: {remote['webUrl']}")
            try:
                seed(dv, remote["remoteUrl"], cidrs, author)
            except Exception:
                # 반쯤 만든 로컬 폴더를 남기면 다시 실행할 때 '비어 있지 않음'으로 막힌다. 원격은 빈 채로 두면 다음 실행이 이어 쓴다.
                shutil.rmtree(REPO, ignore_errors=True)
                raise
            print(f"시드 push 완료 (allowed_clients: {' '.join(cidrs)})")
        print(f"준비 완료: {REPO}")
        print("이후 시나리오 3·장애 재현의 fetch/push 는 Git 자격 증명(Git Credential Manager)으로 인증합니다.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
