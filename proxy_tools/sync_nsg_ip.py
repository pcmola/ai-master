"""Proxy VM NSG 의 허용 IP 를 '고정 허용 IP + 이 PC 의 현재 공인 IP' 로 맞춘다.

집 회선·테더링처럼 공인 IP 가 바뀌면 NSG 가 SSH(22)·프록시 포트를 막아, 시나리오 3·장애 재현·로그 뷰어의
SSH 접속이 'Connection timed out' 으로 실패한다. 이 스크립트는 리소스 그룹의 NSG 에서
'출발지가 단일 IP(/32)인 인바운드 Allow 규칙'(Allow-SSH, Allow-squid-8080, Allow-nginx 등)만 골라
출발지를 '.env 의 PROXY_ALLOWED_CLIENTS 에 적힌 단일 IP(집 회선 등, 항상 유지) + 현재 공인 IP/32' 로 맞춘다.
그 밖의 예전 IP(지난 테더링 IP 등)는 지운다. '*'·서비스 태그·대역(/24 등) 규칙은 건드리지 않는다.

Squid 자체의 허용 클라이언트(proxy-config 의 acl allowed_clients)는 바꾸지 않는다.
SSH 로 로그를 읽는 데는 NSG 만 맞으면 된다.

설정(.env): PROXY_ALLOWED_CLIENTS(쉼표 구분, 유지할 IP), SCENARIO2_RESOURCE_GROUP(기본 WJY-PROXY-RG)

사용 예:
    python proxy_tools/sync_nsg_ip.py --check   # 규칙만 비교(아무것도 바꾸지 않음)
    python proxy_tools/sync_nsg_ip.py           # 다른 규칙의 출발지를 '고정 허용 IP + 현재 IP' 로 맞춤
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


def keep_ips() -> list[str]:
    """PROXY_ALLOWED_CLIENTS 의 단일 IPv4(/32) 값. 대역은 NSG 단일 IP 규칙 기준에 맞지 않아 뺀다."""
    out = []
    for c in os.environ.get("PROXY_ALLOWED_CLIENTS", "").split(","):
        try:
            net = ipaddress.ip_network(c.strip(), strict=False)
        except ValueError:
            continue
        if net.version == 4 and net.num_addresses == 1:
            out.append(f"{net.network_address}/32")
    return out


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


def host_rules(resource_group: str) -> list[dict]:
    """리소스 그룹 NSG 의 단일 IP 허용 규칙: {nsg, name, port, src(/32 로 맞춘 목록)}."""
    out = []
    for nsg in sorted(az("network", "nsg", "list", "-g", resource_group, "--query", "[].name") or []):
        for rule in sorted(az("network", "nsg", "rule", "list", "-g", resource_group, "--nsg-name", nsg),
                           key=lambda r: r["priority"]):
            if host_rule(rule):
                out.append({"nsg": nsg, "name": rule["name"],
                            "port": rule.get("destinationPortRange") or ",".join(rule.get("destinationPortRanges") or []),
                            "src": [s if "/" in s else f"{s}/32" for s in sources(rule)]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="비교만 하고 바꾸지 않음")
    ap.add_argument("--resource-group", default=RESOURCE_GROUP)
    args = ap.parse_args()

    current = f"{my_public_ip()}/32"
    want = sorted(set(keep_ips()) | {current})
    print(f"현재 공인 IP: {current}  유지할 IP(PROXY_ALLOWED_CLIENTS): {', '.join(keep_ips()) or '없음'}"
          f"  (리소스 그룹 {args.resource_group})")
    rules = host_rules(args.resource_group)
    if not rules:
        print("단일 IP 허용 규칙이 있는 NSG 가 없습니다.")
        return 1
    stale = []
    for r in rules:
        ok = sorted(r["src"]) == want
        print(f"  {'=' if ok else '≠'} {r['nsg']} {r['name']} (포트 {r['port']})  출발지 {', '.join(r['src'])}")
        if not ok:
            stale.append((r["nsg"], r["name"]))
    if not stale:
        print(f"모든 규칙이 {', '.join(want)} 를 허용합니다. 바꿀 것이 없습니다.")
        return 0
    if args.check:
        print(f"{', '.join(want)} 와 다른 규칙 {len(stale)}개. 맞추려면 --check 없이 실행하세요.")
        return 0
    for nsg, name in stale:
        r = az("network", "nsg", "rule", "update", "-g", args.resource_group, "--nsg-name", nsg, "-n", name,
               "--source-address-prefixes", *want)
        src = r.get("sourceAddressPrefixes") or [r.get("sourceAddressPrefix")]
        print(f"  → {nsg} {name}: 출발지 {', '.join(map(str, src))} ({r.get('provisioningState')})")
    print(f"규칙 {len(stale)}개의 출발지를 {', '.join(want)} 로 맞췄습니다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # az 미로그인·네트워크 오류 등은 한 줄로
        print(f"실패: {type(exc).__name__}: {exc}")
        sys.exit(1)
