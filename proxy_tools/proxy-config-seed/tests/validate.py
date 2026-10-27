"""Static checks for squid/ before deploy or merge.

    python tests/validate.py

형식만 검사한다(squid 문법 검사는 deploy.sh 가 VM 에서 'squid -k parse' 로 한다).
도메인 철자나 DNS 서버가 실제로 맞는지는 알 수 없으므로 배포 후 scripts/traffic.sh 로 확인한다.
문제가 있으면 목록을 출력하고 종료 코드 1 을 돌려준다.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "squid" / "proxy.conf"
CONTAINER_LISTS = "/etc/squid/lists/"  # 컨테이너 안 경로 → 저장소의 squid/lists/
DOMAIN = re.compile(r"^\.?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def directives(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def check_list(path: Path, errors: list[str]):
    seen: dict[str, int] = {}
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        d = raw.strip().lower()
        if not d or d.startswith("#"):
            continue
        where = f"{path.relative_to(ROOT).as_posix()}:{no}"
        if not DOMAIN.match(d):
            errors.append(f"{where}: 도메인 형식이 아님: {raw.strip()!r}")
            continue
        base = d.lstrip(".")
        if base in seen:
            errors.append(f"{where}: 중복 도메인 {base} (줄 {seen[base]})")
        seen[base] = no
    # squid 는 '.a.kr' 와 'b.a.kr' 이 함께 있으면 겹친다고 경고하고 하나를 버린다.
    for d in seen:
        for other in seen:
            if d != other and other.endswith("." + d):
                errors.append(f"{path.name}: {other} 는 .{d} 에 이미 포함됨")


def main() -> int:
    errors: list[str] = []
    text = CONF.read_text(encoding="utf-8")
    if not text.isascii():
        errors.append("proxy.conf 에 ASCII 가 아닌 문자가 있음 (VM cloud-init·squid 는 ASCII 기준)")
    rows = directives(text)
    names = [r[0] for r in rows]

    if "http_port" not in names:
        errors.append("http_port 가 없음")
    access = [r for r in rows if r[0] == "http_access"]
    if not access or access[-1][1:] != ["deny", "all"]:
        errors.append("마지막 http_access 는 'http_access deny all' 이어야 함")

    acls = {}
    for r in rows:
        if r[0] == "acl" and len(r) >= 4:
            acls.setdefault(r[1], []).append(r[2:])
    for typ, *values in acls.get("allowed_clients", []):
        for v in values:
            try:
                ipaddress.ip_network(v)
            except ValueError:
                errors.append(f"allowed_clients 의 주소 형식이 틀림: {v}")
    if "allowed_clients" not in acls:
        errors.append("acl allowed_clients 가 없음")
    for name in ("SSL_ports", "Safe_ports"):
        ports = [v for _, *vs in acls.get(name, []) for v in vs]
        if "443" not in ports:
            errors.append(f"{name} 에 443 이 없음")

    for typ, *values in acls.get("whitelist", []):
        for v in values:
            v = v.strip('"')
            if not v.startswith(CONTAINER_LISTS):
                errors.append(f"whitelist 파일은 {CONTAINER_LISTS} 아래여야 함: {v}")
                continue
            local = ROOT / "squid" / "lists" / v[len(CONTAINER_LISTS):]
            if local.is_file():
                check_list(local, errors)
            else:
                errors.append(f"whitelist 파일이 저장소에 없음: {local.relative_to(ROOT).as_posix()}")

    for r in rows:
        if r[0] == "dns_nameservers":
            for v in r[1:]:
                try:
                    ipaddress.ip_address(v)
                except ValueError:
                    errors.append(f"dns_nameservers 주소 형식이 틀림: {v}")

    if errors:
        print("validate: FAIL")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("validate: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
