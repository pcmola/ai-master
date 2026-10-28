"""Proxy VM NSG 의 허용 IP 를 이 PC 의 현재 공인 IP 로 맞춘다.

집 회선·테더링처럼 공인 IP 가 바뀌면 NSG 가 SSH(22)·프록시 포트를 막아, 시나리오 3·장애 재현·로그 뷰어의
SSH 접속이 'Connection timed out' 으로 실패한다. 이 스크립트는 리소스 그룹의 NSG 에서
'출발지가 단일 IP(/32)인 인바운드 Allow 규칙'(Allow-SSH, Allow-squid-8080, Allow-nginx 등)만 골라
출발지를 현재 공인 IP/32 로 바꾼다(추가가 아니라 교체). '*'·서비스 태그·대역(/24 등) 규칙은 건드리지 않는다.

Squid 자체의 허용 클라이언트(proxy-config 의 acl allowed_clients, .env 의 PROXY_ALLOWED_CLIENTS)는 바꾸지 않는다.
SSH 로 로그를 읽는 데는 NSG 만 맞으면 된다.

설정(.env): SCENARIO2_RESOURCE_GROUP(기본 WJY-PROXY-RG)

사용 예:
    python proxy_tools/sync_nsg_ip.py --check   # 현재 IP 와 규칙만 비교(아무것도 바꾸지 않음)
    python proxy_tools/sync_nsg_ip.py           # 다른 규칙의 출발지를 현재 IP 로 교체
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import ENV_FILE  # noqa: E402
from proxy_status_sync import az  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

RESOURCE_GROUP = os.environ.get("SCENARIO2_RESOURCE_GROUP", "WJY-PROXY-RG")


def my_public_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=10) as res:
        ip = res.read().decode().strip()
    ipaddress.IPv4Address(ip)  # 이상한 응답이면 여기서 멈춘다
    return ip


def sources(rule: dict) -> list[str]:
    return rule.get("sourceAddressPrefixes") or ([rule["sourceAddressPrefix"]] if rule.get("sourceAddressPrefix") else [])


def host_rule(rule: dict) -> bool:
    """출발지가 전부 단일 IPv4(/32 또는 접두사 없음)인 인바운드 Allow 규칙인가."""
    if rule.get("direction") != "Inbound" or rule.get("access") != "Allow":
        return False
    src = sources(rule)
    if not src:
        return False
    for s in src:
        try:
            if ipaddress.ip_network(s, strict=False).num_addresses != 1 or ":" in s:
                return False
        except ValueError:  # '*', VirtualNetwork 같은 서비스 태그
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="비교만 하고 바꾸지 않음")
    ap.add_argument("--resource-group", default=RESOURCE_GROUP)
    args = ap.parse_args()

    want = f"{my_public_ip()}/32"
    print(f"현재 공인 IP: {want}  (리소스 그룹 {args.resource_group})")
    nsgs = az("network", "nsg", "list", "-g", args.resource_group, "--query", "[].name")
    if not nsgs:
        print("NSG 가 없습니다.")
        return 1
    stale = []
    for nsg in sorted(nsgs):
        for rule in sorted(az("network", "nsg", "rule", "list", "-g", args.resource_group, "--nsg-name", nsg),
                           key=lambda r: r["priority"]):
            if not host_rule(rule):
                continue
            src = [s if "/" in s else f"{s}/32" for s in sources(rule)]
            ok = src == [want]
            print(f"  {'=' if ok else '≠'} {nsg} {rule['name']} (포트 {rule.get('destinationPortRange') or ','.join(rule.get('destinationPortRanges') or [])})"
                  f"  출발지 {', '.join(src)}")
            if not ok:
                stale.append((nsg, rule["name"]))
    if not stale:
        print("모든 규칙이 현재 IP 를 허용합니다. 바꿀 것이 없습니다.")
        return 0
    if args.check:
        print(f"현재 IP 와 다른 규칙 {len(stale)}개. 맞추려면 --check 없이 실행하세요.")
        return 0
    for nsg, name in stale:
        r = az("network", "nsg", "rule", "update", "-g", args.resource_group, "--nsg-name", nsg, "-n", name,
               "--source-address-prefixes", want)
        print(f"  → {nsg} {name}: 출발지 {r.get('sourceAddressPrefix')} ({r.get('provisioningState')})")
    print(f"규칙 {len(stale)}개의 출발지를 {want} 로 바꿨습니다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # az 미로그인·네트워크 오류 등은 한 줄로
        print(f"실패: {type(exc).__name__}: {exc}")
        sys.exit(1)
