r"""Create low-cost Linux proxy VMs (Squid or HAProxy) on Azure with the Azure CLI.

- 이름은 wjy-proxy01, wjy-proxy02 ... 순으로 구독 안의 기존 VM 을 보고 다음 번호를 쓴다.
- 기본값: 구독 wonjy, Korea Central, Availability zone 1, Trusted launch, Ubuntu Server 24.04 LTS x64 Gen2.
- 비용 최소화: 가장 싼 B 시리즈 크기부터 해당 지역·영역에서 쓸 수 있는 것을 고르고,
  OS 디스크는 Standard HDD(Standard_LRS) 30GB, 공용 IP 는 Standard 1개(--no-public-ip 로 생략 가능).
- 프록시는 cloud-init 으로 설치한다. Squid: 포워드 프록시(3128), HAProxy: TCP 전달(기본 80 -> --backend).
  설치 방식은 docker 이미지(기본 ubuntu/squid, haproxy:lts 또는 --docker-image) 또는 apt 패키지 중 고른다.
- NSG 는 SSH·프록시 포트를 이 PC 의 공인 IP(또는 --allow)에서만 연다.

사용 예:
    az login
    python azure_proxy_vm.py                         # 프록시 종류를 물어본다
    python azure_proxy_vm.py --proxy squid --install docker --count 2
    python azure_proxy_vm.py --proxy squid --install docker --docker-image myacr.azurecr.io/infra/squid:6.10 \
        --registry myacr.azurecr.io --registry-user <token-name> --registry-password <token>
    python azure_proxy_vm.py --proxy haproxy --backend 10.0.0.4:8080
    python azure_proxy_vm.py --proxy squid --dry-run  # 실행할 az 명령만 출력
"""

from __future__ import annotations

import argparse
import json
import locale
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

PREFIX = "wjy-proxy"
DEFAULTS = {
    "subscription": "wonjy",
    "resource_group": "wjy-proxy-rg",
    "location": "koreacentral",
    "zone": "1",
    "image": "Canonical:ubuntu-24_04-lts:server:latest",  # Ubuntu Server 24.04 LTS - x64 Gen2
    "vnet": "wjy-proxy-vnet",
    "admin": "azureuser",
}
# 월 요금이 싼 순서. 앞에서부터 이 지역·영역에서 제한 없이 쓸 수 있는 첫 크기를 고른다.
SIZE_CANDIDATES = ["Standard_B1ls", "Standard_B2ats_v2", "Standard_B2ts_v2", "Standard_B1s"]
PROXY_PORT = {"squid": 3128, "haproxy": 80}
# --install docker 일 때 기본 이미지. 사내 ACR 등 다른 이미지는 --docker-image 와 --registry 옵션으로 지정한다.
DOCKER_IMAGE = {"squid": "ubuntu/squid:latest", "haproxy": "haproxy:lts"}

AZ = shutil.which("az") or "az"
DRY_RUN = False


def az(*args: str, parse: bool = True, mutate: bool = False, label: str | None = None):
    """az 명령 실행. mutate=True 인 명령은 --dry-run 이면 출력만 한다.
    label 을 주면 실행하는 동안 '  label ... 12s' 처럼 경과 시간을 갱신해 보여준다."""
    cmd = [AZ, *args, "-o", "json" if parse else "none"]
    if mutate and DRY_RUN:
        print("  [dry-run] az " + " ".join(a if " " not in a else f'"{a}"' for a in args))
        return None
    label = label or "az " + " ".join(args[:2])
    # 한국어 Windows 에서 az 의 오류 메시지는 cp949 로 나오므로 바이트로 받아 decode() 로 푼다.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result: dict = {}
    reader = threading.Thread(target=lambda: result.update(zip(("out", "err"), proc.communicate())), daemon=True)
    started = time.monotonic()
    reader.start()
    while reader.is_alive():
        print(f"\r  {label} ... {int(time.monotonic() - started)}s", end="", flush=True)
        reader.join(1)
    print(f"\r  {label} ... {int(time.monotonic() - started)}s {'완료' if proc.returncode == 0 else '실패'}", flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"az {' '.join(args[:3])} 실패:\n{decode(result.get('err')).strip()}")
    out = decode(result.get("out"))
    return json.loads(out) if parse and out.strip() else None


def decode(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def my_public_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=10) as res:
        return res.read().decode().strip()


def next_names(count: int) -> list[str]:
    try:
        names = az("vm", "list", "--query", "[].name", label="기존 VM 이름 조회(번호 채번)") or []
    except SystemExit:
        if not DRY_RUN:
            raise
        names = []  # 로그인 전 dry-run 은 기존 VM 이 없다고 본다
    used = [int(m.group(1)) for n in names if (m := re.fullmatch(rf"{PREFIX}(\d+)", n))]
    start = max(used, default=0) + 1
    return [f"{PREFIX}{n:02d}" for n in range(start, start + count)]


def pick_size(location: str, zone: str) -> str:
    # list-skus 는 --size 를 줘도 지역 전체 목록을 내려받아 걸러내므로 오래 걸린다. 한 번만 받아 후보를 모두 확인한다.
    query = ("[?contains('" + ",".join(SIZE_CANDIDATES) + "', name)].{name:name, family:family, info:locationInfo, "
             "r:restrictions, vcpus:capabilities[?name=='vCPUs'].value | [0]}")
    try:
        skus = az("vm", "list-skus", "-l", location, "--resource-type", "virtualMachines", "--zone",
                  "--query", query, label=f"{location} 에서 쓸 수 있는 VM 크기 조회(30초~2분)")
    except SystemExit:
        if DRY_RUN:
            return SIZE_CANDIDATES[0]
        raise
    # 구독의 VM 계열별 vCPU 할당량. 새 구독은 일부 계열(예: Basv2)이 0 이라 생성 단계에서 QuotaExceeded 로 실패한다.
    usage = az("vm", "list-usage", "-l", location, "--query", "[].{n:name.value, used:currentValue, limit:limit}",
               label="VM 계열별 vCPU 할당량 조회")
    free = {u["n"].lower(): int(u["limit"]) - int(u["used"]) for u in usage or []}
    found = {sku["name"]: sku for sku in skus or []}
    for size in SIZE_CANDIDATES:
        sku = found.get(size)
        zones = sum((i.get("zones") or [] for i in (sku or {}).get("info") or []), [])
        if not sku or sku.get("r") or zone not in zones:
            print(f"  {size}: {location} zone {zone} 에서 사용 불가")
            continue
        need, left = int(sku.get("vcpus") or 1), free.get((sku.get("family") or "").lower(), 0)
        if left < need:
            print(f"  {size}: {sku.get('family')} vCPU 할당량 부족 (남은 {left}, 필요 {need})")
            continue
        return size
    raise SystemExit(f"사용 가능한 저가 크기가 없습니다: {', '.join(SIZE_CANDIDATES)}\n"
                     "Portal 의 Subscriptions > Usage + quotas 에서 할당량을 늘리거나 --size 로 지정하세요.")


def indent(text: str, n: int = 6) -> str:
    return "\n".join(" " * n + line if line else "" for line in text.splitlines())


def squid_conf(allow: str, port: int) -> str:
    return f"""http_port {port}
acl allowed_clients src {allow}
http_access allow localhost
http_access allow allowed_clients
http_access deny all
coredump_dir /var/spool/squid
"""


def haproxy_cfg(port: int, backend: str, log: str) -> str:
    return f"""global
    log {log}
defaults
    mode tcp
    log global
    option tcplog
    timeout connect 5s
    timeout client 1m
    timeout server 1m
frontend proxy_in
    bind *:{port}
    default_backend proxy_out
backend proxy_out
    server upstream1 {backend} check
"""


def cloud_init(args, allow: str, port: int) -> str:
    if args.install == "apt":
        if args.proxy == "squid":
            path, conf = "/etc/squid/squid.conf", squid_conf(allow, port)
            check = "squid -k parse"
        else:
            path, conf = "/etc/haproxy/haproxy.cfg", haproxy_cfg(port, args.backend, "/dev/log local0")
            check = "haproxy -c -f /etc/haproxy/haproxy.cfg"
        # 패키지 설치가 설정 파일을 덮어쓰지 않도록 따로 써 두고 설치 후에 복사한다.
        return f"""#cloud-config
package_update: true
packages: [{args.proxy}]
write_files:
  - path: /root/proxy.conf
    content: |
{indent(conf)}
runcmd:
  - cp /root/proxy.conf {path}
  - {check}
  - systemctl restart {args.proxy}
"""

    # docker: 설정 파일을 /etc/proxy 에 두고 컨테이너에 읽기 전용으로 마운트한다.
    if args.proxy == "squid":
        conf, mount = squid_conf(allow, 3128), "/etc/squid/squid.conf"
        inner = 3128
        check = f"docker run --rm -v /etc/proxy/proxy.conf:{mount}:ro {args.docker_image} squid -k parse"
    else:
        # 공식 haproxy 이미지는 root 가 아닌 haproxy 사용자로 돌아 1024 미만 포트를 못 열므로 8080 에서 받는다.
        conf, mount = haproxy_cfg(8080, args.backend, "stdout format raw local0"), "/usr/local/etc/haproxy/haproxy.cfg"
        inner = 8080
        check = f"docker run --rm -v /etc/proxy/proxy.conf:{mount}:ro {args.docker_image} haproxy -c -f {mount}"
    login = (f"  - echo '{args.registry_password}' | docker login {args.registry} "
             f"-u '{args.registry_user}' --password-stdin\n") if args.registry else ""
    return f"""#cloud-config
package_update: true
packages: [docker.io]
write_files:
  - path: /etc/proxy/proxy.conf
    permissions: '0644'
    content: |
{indent(conf)}
runcmd:
  - systemctl enable --now docker
{login}  - docker pull {args.docker_image}
  - {check}
  - docker run -d --name {args.proxy} --restart=always -p {port}:{inner} -v /etc/proxy/proxy.conf:{mount}:ro {args.docker_image}
"""


def create_vm(name: str, args, size: str, allow: str, port: int):
    print(f"\n== {name} ({size}, {args.proxy} via {args.install}, port {port})")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(cloud_init(args, allow, port))
        custom_data = f.name
    try:
        create = ["vm", "create", "-g", args.resource_group, "-n", name, "-l", args.location,
                  "--zone", args.zone, "--image", args.image, "--size", size,
                  "--security-type", "TrustedLaunch", "--enable-secure-boot", "true", "--enable-vtpm", "true",
                  "--storage-sku", "Standard_LRS", "--os-disk-size-gb", "30", "--os-disk-delete-option", "Delete",
                  "--vnet-name", args.vnet, "--subnet", "default",
                  "--vnet-address-prefix", "10.10.0.0/16", "--subnet-address-prefix", "10.10.0.0/24",
                  "--nsg", f"{name}-nsg", "--nsg-rule", "NONE",
                  "--admin-username", DEFAULTS["admin"], "--generate-ssh-keys",
                  "--custom-data", custom_data, "--tags", f"proxy={args.proxy}", f"install={args.install}", "owner=wjy"]
        if args.no_public_ip:
            create += ["--public-ip-address", ""]
        else:
            create += ["--public-ip-sku", "Standard", "--public-ip-address", f"{name}-ip",
                       "--public-ip-address-allocation", "static"]
        vm = az(*create, mutate=True, label=f"{name} 생성(보통 1~3분)")
    finally:
        Path(custom_data).unlink(missing_ok=True)

    for prio, (rule, rport) in enumerate([("Allow-SSH", 22), (f"Allow-{args.proxy}", port)], start=100):
        az("network", "nsg", "rule", "create", "-g", args.resource_group, "--nsg-name", f"{name}-nsg",
           "-n", rule, "--priority", str(prio), "--direction", "Inbound", "--access", "Allow",
           "--protocol", "Tcp", "--source-address-prefixes", allow, "--destination-port-ranges", str(rport),
           parse=False, mutate=True, label=f"NSG 규칙 {rule}({rport}) 추가")

    if vm:
        ip = vm.get("publicIpAddress") or vm.get("privateIpAddress")
        print(f"  생성 완료: {ip}  (ssh {DEFAULTS['admin']}@{ip})")
        if args.proxy == "squid":
            print(f"  확인: curl -x http://{ip}:{port} https://ifconfig.me   (설치에 2~3분 걸림)")
        else:
            print(f"  확인: {ip}:{port} -> {args.backend}   (설치에 2~3분 걸림)")


def main():
    global DRY_RUN
    p = argparse.ArgumentParser(description="Azure 에 저가 Linux 프록시 VM(Squid/HAProxy)을 만든다.")
    p.add_argument("--proxy", choices=["squid", "haproxy"], help="설치할 프록시. 생략하면 물어본다.")
    p.add_argument("--count", type=int, default=1, help="만들 VM 수 (기본 1)")
    p.add_argument("--backend", help="HAProxy 가 전달할 대상 host:port")
    p.add_argument("--port", type=int, help="프록시 수신 포트 (기본 squid 3128, haproxy 80)")
    p.add_argument("--install", choices=["docker", "apt"], help="설치 방식. 생략하면 물어본다.")
    p.add_argument("--docker-image", help=f"사용할 이미지 (기본 {DOCKER_IMAGE})")
    p.add_argument("--registry", help="비공개 레지스트리 주소 (예: myacr.azurecr.io). 지정하면 docker login 한다.")
    p.add_argument("--registry-user", help="레지스트리 사용자 (ACR 은 토큰 이름 또는 서비스 주체 ID)")
    p.add_argument("--registry-password", help="레지스트리 비밀번호/토큰 (cloud-init 에 담겨 VM 에 남는다)")
    p.add_argument("--allow", help="SSH·프록시 접속을 허용할 CIDR (기본: 이 PC 공인 IP/32)")
    p.add_argument("--size", help=f"VM 크기 직접 지정 (기본: {SIZE_CANDIDATES} 중 사용 가능한 가장 싼 것)")
    p.add_argument("--no-public-ip", action="store_true", help="공용 IP 없이 생성 (비용 절감, 외부 접속 불가)")
    p.add_argument("--dry-run", action="store_true", help="az 생성 명령을 실행하지 않고 출력만 한다")
    for key in ("subscription", "resource_group", "location", "zone", "image", "vnet"):
        p.add_argument("--" + key.replace("_", "-"), default=DEFAULTS[key])
    args = p.parse_args()
    DRY_RUN = args.dry_run

    if not args.proxy:
        choice = input("프록시 종류를 고르세요 [1] squid (포워드 프록시)  [2] haproxy (TCP 전달): ").strip().lower()
        args.proxy = {"1": "squid", "2": "haproxy"}.get(choice, choice)
        if args.proxy not in PROXY_PORT:
            p.error("squid 또는 haproxy 중에서 골라야 합니다.")
    if args.proxy == "haproxy" and not args.backend:
        args.backend = input("HAProxy 가 전달할 대상 host:port: ").strip()
    if args.proxy == "haproxy" and not re.fullmatch(r"[\w.-]+:\d+", args.backend or ""):
        p.error("--backend 는 host:port 형식이어야 합니다.")
    port = args.port or PROXY_PORT[args.proxy]
    if not args.install:
        choice = input("설치 방식을 고르세요 [1] docker 이미지  [2] apt 패키지: ").strip().lower()
        args.install = {"1": "docker", "2": "apt"}.get(choice, choice)
        if args.install not in ("docker", "apt"):
            p.error("docker 또는 apt 중에서 골라야 합니다.")
    if args.install == "docker":
        args.docker_image = args.docker_image or DOCKER_IMAGE[args.proxy]
        if args.registry and not (args.registry_user and args.registry_password):
            p.error("--registry 를 쓰면 --registry-user 와 --registry-password 도 필요합니다.")

    if not DRY_RUN:
        subs = az("account", "list", "--query", "[].{name:name, id:id, tenant:tenantId}", label="구독 확인") or []
        if not any(args.subscription in (s["name"], s["id"]) for s in subs):
            listed = "\n".join(f"  {s['name']}  ({s['id']}, tenant {s['tenant']})" for s in subs) or "  (없음)"
            raise SystemExit(f"구독 '{args.subscription}' 이(가) 현재 az 로그인 계정에 없습니다. 보이는 구독:\n{listed}\n"
                             "다른 계정/테넌트라면 az login --tenant <테넌트ID> 로 다시 로그인하거나 --subscription 으로 지정하세요.")
        az("account", "set", "--subscription", args.subscription, parse=False, label=f"구독 {args.subscription} 선택")
    if args.allow:
        allow = args.allow
    else:
        print("  이 PC 공인 IP 확인 ...", end="", flush=True)
        allow = f"{my_public_ip()}/32"
        print(f" {allow}")
    size = args.size or pick_size(args.location, args.zone)
    names = next_names(args.count)
    print(f"구독 {args.subscription} / {args.resource_group} / {args.location} zone {args.zone}")
    print(f"크기 {size}, 허용 IP {allow}, 생성할 VM: {', '.join(names)}")

    az("group", "create", "-n", args.resource_group, "-l", args.location, parse=False, mutate=True,
       label=f"리소스 그룹 {args.resource_group} 준비")
    for name in names:
        create_vm(name, args, size, allow, port)


if __name__ == "__main__":
    main()
