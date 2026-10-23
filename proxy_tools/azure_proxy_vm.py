r"""Create low-cost Linux proxy VMs (Squid or HAProxy) on Azure with the Azure CLI.

- 이름은 wjy-proxy01, wjy-proxy02 ... 순으로 구독 안의 기존 VM 을 보고 다음 번호를 쓴다.
- 기본값: 구독 wonjy, Korea Central, Availability zone 1, Trusted launch, Ubuntu Server 24.04 LTS x64 Gen2.
- 비용 최소화: 가장 싼 B 시리즈 크기부터 해당 지역·영역에서 쓸 수 있는 것을 고르고,
  OS 디스크는 Standard HDD(Standard_LRS) 30GB, 공용 IP 는 Standard 1개(--no-public-ip 로 생략 가능).
- 프록시는 cloud-init 으로 설치한다. Squid: 포워드 프록시(3128), HAProxy: TCP 전달(기본 80 -> --backend).
  설치 방식은 podman 이미지(Quadlet 으로 systemd 서비스화, 권장), docker 이미지, apt 패키지 중 고른다.
  이미지 기본값은 ubuntu/squid:latest, haproxy:lts 이고 --docker-image 로 바꾼다(podman 은 docker.io/ 를 붙인다).
- NSG 는 SSH·프록시 포트를 이 PC 의 공인 IP(또는 --allow)에서만 연다.
- 매일 01:00(한국 표준시)에 자동 종료(deallocate)한다. 포털 Auto-shutdown 에도 Korea Standard Time 으로 보인다.
  --auto-shutdown HH:MM 으로 바꾸거나 off 로 끈다.

사용 예:
    az login
    python proxy_tools/azure_proxy_vm.py                         # 프록시 종류를 물어본다
    python proxy_tools/azure_proxy_vm.py --proxy squid --install podman --count 2
    python proxy_tools/azure_proxy_vm.py --proxy squid --install podman --docker-image myacr.azurecr.io/infra/squid:6.10 \
        --registry myacr.azurecr.io --registry-user <token-name> --registry-password <token>
    python proxy_tools/azure_proxy_vm.py --proxy haproxy --backend 10.0.0.4:8080
    python proxy_tools/azure_proxy_vm.py --proxy squid --dry-run  # 실행할 az 명령만 출력
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
PROXY_PORT = {"squid": 3128, "haproxy": 80, "nginx": 80}
# --install podman|docker 일 때 기본 이미지. 사내 ACR 등 다른 이미지는 --docker-image 와 --registry 옵션으로 지정한다.
DOCKER_IMAGE = {"squid": "ubuntu/squid:latest", "haproxy": "haproxy:lts", "nginx": "nginx:stable"}

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


WHITELIST = "/etc/squid/lists/whitelist.txt"  # squid 가 읽는 경로 (docker 는 VM 의 /etc/proxy/lists 를 마운트)
# cloud-init 파일은 az CLI 가 시스템 인코딩(cp949)으로 읽으므로 VM 에 들어가는 내용은 ASCII 로만 쓴다.
NO_WHITELIST = "# No destination restriction. Enable the whitelist acl in proxy.conf and list domains here.\n"


def squid_conf(allow: str, port: int, whitelist: bool) -> str:
    # whitelist 가 있으면 목록의 도메인으로만 나갈 수 있다. 목록은 한 줄에 하나, '.도메인' 은 하위 도메인 포함.
    dest = (f'acl whitelist dstdomain "{WHITELIST}"\n', "allowed_clients whitelist") if whitelist else ("", "allowed_clients")
    return f"""http_port {port}
acl allowed_clients src {allow}
{dest[0]}acl SSL_ports port 443
acl Safe_ports port 80 443
acl CONNECT method CONNECT
http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow {dest[1]}
http_access deny all
coredump_dir /var/spool/squid
"""


def whitelist_lines(domains: list[str]) -> str:
    return "".join(f"{d if d.startswith('.') else '.' + d}\n" for d in domains)


# proxy_log_viewer.py 가 읽는 nginx 접속 로그 형식(| 구분 10개 필드). 바꾸면 뷰어의 parse_nginx 도 같이 바꾼다.
NGINX_LOG_FORMAT = ("$time_iso8601|$remote_addr|$host|$request_method|$request_uri|$status|$body_bytes_sent|"
                    "$request_time|$upstream_addr|$upstream_status")


def nginx_conf(port: int, backend: str, allow: str, vhosts: list[tuple[str, str]] = ()) -> str:
    """리버스 프록시. IP·모르는 도메인으로 오면 backend 로, vhosts 의 (도메인, URL) 은 Host 이름으로 나눠 보낸다.
    allow 밖의 IP 는 403. 접속 로그는 NGINX_LOG_FORMAT 으로 표준 출력에 쓴다(podman logs 로 읽음)."""

    def server(name: str, url: str, default: bool = False) -> str:
        host = re.sub(r"^https?://", "", url).split("/", 1)[0].split(":")[0]
        return f"""server {{
    listen {port}{' default_server' if default else ''};
    server_name {name};
    access_log /dev/stdout proxy;

    location / {{
        set $upstream {url.rstrip('/')};
        proxy_pass $upstream;
        proxy_set_header Host {host};
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_ssl_server_name on;
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
    }}
}}
"""
    blocks = [server("_", backend, default=True)] + [server(n, u) for n, u in vhosts]
    # proxy_pass 에 변수를 쓰면 시작 시점이 아니라 요청 시점에 DNS 를 조회한다. 부팅 직후 DNS 가 잠깐 실패해도
    # nginx 가 'host not found in upstream' 으로 못 뜨는 일이 없다. 168.63.129.16 은 Azure VM 기본 DNS.
    return (f"log_format proxy '{NGINX_LOG_FORMAT}';\n\n"
            "resolver 168.63.129.16 valid=300s ipv6=off;\n\n"
            f"# allowed clients (applies to every server below)\nallow {allow};\ndeny all;\n\n" + "\n".join(blocks))


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
    wl = whitelist_lines(args.whitelist) if args.proxy == "squid" and args.whitelist else ""
    if args.install == "apt":
        if args.proxy == "squid":
            path, conf = "/etc/squid/squid.conf", squid_conf(allow, port, bool(wl))
            check = "squid -k parse"
        elif args.proxy == "nginx":
            path, conf = "/etc/nginx/sites-enabled/default", nginx_conf(port, args.backend, allow, args.vhost)
            check = "nginx -t"
        else:
            path, conf = "/etc/haproxy/haproxy.cfg", haproxy_cfg(port, args.backend, "/dev/log local0")
            check = "haproxy -c -f /etc/haproxy/haproxy.cfg"
        wl_file = f"  - path: {WHITELIST}\n    content: |\n{indent(wl)}\n" if wl else ""
        # 패키지 설치가 설정 파일을 덮어쓰지 않도록 따로 써 두고 설치 후에 복사한다.
        return f"""#cloud-config
package_update: true
packages: [{args.proxy}]
write_files:
  - path: /root/proxy.conf
    content: |
{indent(conf)}
{wl_file}runcmd:
  - cp /root/proxy.conf {path}
  - {check}
  - systemctl restart {args.proxy}
"""

    # 컨테이너(docker/podman): 설정 파일을 /etc/proxy 에 두고 컨테이너에 읽기 전용으로 마운트한다.
    if args.proxy == "squid":
        conf, inner = squid_conf(allow, 3128, bool(wl)), 3128
        # 목록 파일은 디렉터리째 마운트해야 VM 에서 고친 내용이 'squid -k reconfigure' 로 바로 반영된다.
        volumes = [("/etc/proxy/proxy.conf", "/etc/squid/squid.conf"), ("/etc/proxy/lists", "/etc/squid/lists")]
        # 로그는 VM 디스크에 둔다. 컨테이너는 --rm 이라 재시작·VM 중지 때 컨테이너 안 로그가 함께 지워진다.
        # 이미지의 squid 는 uid 13(proxy) 으로 쓰므로 디렉터리 소유자를 맞춘다.
        logs = ("/var/log/proxy", "/var/log/squid", "13:13")
        check_cmd = "squid -k parse"
    elif args.proxy == "nginx":
        # 공식 nginx 이미지는 master 가 root 라 80 을 그대로 쓸 수 있다. 기본 사이트 설정을 대체한다.
        conf, inner = nginx_conf(80, args.backend, allow, args.vhost), 80
        volumes = [("/etc/proxy/proxy.conf", "/etc/nginx/conf.d/default.conf")]
        logs = None  # 공식 이미지는 로그를 stdout 으로 보낸다(journald)
        check_cmd = "nginx -t"
    else:
        # 공식 haproxy 이미지는 root 가 아닌 haproxy 사용자로 돌아 1024 미만 포트를 못 열므로 8080 에서 받는다.
        conf, inner = haproxy_cfg(8080, args.backend, "stdout format raw local0"), 8080
        volumes = [("/etc/proxy/proxy.conf", "/usr/local/etc/haproxy/haproxy.cfg")]
        logs = None  # stdout 으로 보낸다(journald)
        check_cmd = "haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg"
    cli = args.install  # docker | podman
    image = qualify_image(args.docker_image) if cli == "podman" else args.docker_image
    mounts = " ".join(f"-v {src}:{dst}:ro" for src, dst in volumes)
    run_mounts = mounts + (f" -v {logs[0]}:{logs[1]}" if logs else "")
    log_dir = f"  - mkdir -p {logs[0]} && chown {logs[2]} {logs[0]} && chmod 750 {logs[0]}\n" if logs else ""
    login = (f"  - echo '{args.registry_password}' | {cli} login {args.registry} "
             f"-u '{args.registry_user}' --password-stdin\n") if args.registry else ""
    files = f"""write_files:
  - path: /etc/proxy/proxy.conf
    permissions: '0644'
    content: |
{indent(conf)}
  - path: /etc/proxy/lists/whitelist.txt
    permissions: '0644'
    content: |
{indent(wl or NO_WHITELIST)}
"""
    if cli == "docker":
        return f"""#cloud-config
package_update: true
packages: [docker.io]
{files}runcmd:
  - systemctl enable --now docker
{login}  - docker pull {image}
  - docker run --rm {mounts} {image} {check_cmd}
{log_dir}  - docker run -d --name {args.proxy} --restart=always -p {port}:{inner} {run_mounts} {image}
"""
    # podman: 데몬이 없으므로 Quadlet(systemd 서비스 <proxy>.service)으로 부팅 시 자동 시작·재시작을 맡긴다.
    unit = "\n".join([
        "[Unit]", f"Description={args.proxy} proxy (podman)", "Wants=network-online.target",
        "After=network-online.target", "", "[Container]", f"ContainerName={args.proxy}", f"Image={image}",
        f"PublishPort={port}:{inner}", *[f"Volume={src}:{dst}:ro" for src, dst in volumes],
        *([f"Volume={logs[0]}:{logs[1]}"] if logs else []), "",
        "[Service]", "Restart=always", "", "[Install]", "WantedBy=multi-user.target", ""])
    return f"""#cloud-config
package_update: true
packages: [podman]
{files}  - path: /etc/containers/systemd/{args.proxy}.container
    permissions: '0644'
    content: |
{indent(unit)}
runcmd:
{login}  - podman pull {image}
  - podman run --rm {mounts} {image} {check_cmd}
{log_dir}  - systemctl daemon-reload
  - systemctl start {args.proxy}
"""


def qualify_image(image: str) -> str:
    """podman 은 짧은 이미지 이름을 레지스트리 없이 풀지 못하므로 docker.io 를 붙인다(공식 이미지는 library/)."""
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}" if "/" in image else f"docker.io/library/{image}"


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

    if args.auto_shutdown != "off":
        # az vm auto-shutdown 은 시간대를 지정할 수 없어(UTC 고정) 만든 뒤 일정 리소스의 시간대를 한국 표준시로 바꾼다.
        # 포털 Auto-shutdown 화면에도 'Korea Standard Time, 1:00 AM' 처럼 보인다.
        hh, mm = map(int, args.auto_shutdown.split(":"))
        az("vm", "auto-shutdown", "-g", args.resource_group, "-n", name, "--time", f"{hh:02d}{mm:02d}",
           parse=False, mutate=True, label=f"매일 {args.auto_shutdown} 자동 종료 설정")
        az("resource", "update", "-g", args.resource_group, "--resource-type", "Microsoft.DevTestLab/schedules",
           "-n", f"shutdown-computevm-{name}", "--set", "properties.timeZoneId=Korea Standard Time",
           parse=False, mutate=True, label="자동 종료 시간대를 한국 표준시로 변경")

    if vm:
        ip = vm.get("publicIpAddress") or vm.get("privateIpAddress")
        print(f"  생성 완료: {ip}  (ssh {DEFAULTS['admin']}@{ip})")
        if args.proxy == "squid":
            print(f"  확인: curl -x http://{ip}:{port} https://ifconfig.me   (설치에 2~3분 걸림)")
        elif args.proxy == "nginx":
            print(f"  확인: curl -I http://{ip}:{port}/  -> {args.backend}   (설치에 2~3분 걸림)")
        else:
            print(f"  확인: {ip}:{port} -> {args.backend}   (설치에 2~3분 걸림)")


def main():
    global DRY_RUN
    p = argparse.ArgumentParser(description="Azure 에 저가 Linux 프록시 VM(Squid/HAProxy/nginx)을 만든다.")
    p.add_argument("--proxy", choices=["squid", "haproxy", "nginx"], help="설치할 프록시. 생략하면 물어본다.")
    p.add_argument("--count", type=int, default=1, help="만들 VM 수 (기본 1)")
    p.add_argument("--backend", help="전달 대상. haproxy: host:port, nginx: http(s)://host[:port]")
    p.add_argument("--port", type=int, help="프록시 수신 포트 (기본 squid 3128, haproxy·nginx 80)")
    p.add_argument("--vhost", action="append", default=[], metavar="DOMAIN=URL",
                   type=lambda s: tuple(x.strip() for x in s.split("=", 1)),
                   help="nginx 가상 도메인 (여러 번 지정 가능). 예: --vhost data.wjy.test=https://www.data.go.kr")
    p.add_argument("--install", choices=["podman", "docker", "apt"],
                   help="설치 방식. podman(Quadlet systemd 서비스, 권장) / docker / apt 패키지. 생략하면 물어본다.")
    p.add_argument("--whitelist", type=lambda s: [d.strip() for d in s.split(",") if d.strip()],
                   default=["data.go.kr", "law.go.kr"],
                   help="Squid 가 접속을 허용할 목적지 도메인(쉼표 구분, 하위 도메인 포함). "
                        "기본 data.go.kr,law.go.kr. 빈 문자열이면 제한 없음")
    p.add_argument("--docker-image", help=f"사용할 이미지 (기본 {DOCKER_IMAGE})")
    p.add_argument("--registry", help="비공개 레지스트리 주소 (예: myacr.azurecr.io). 지정하면 docker login 한다.")
    p.add_argument("--registry-user", help="레지스트리 사용자 (ACR 은 토큰 이름 또는 서비스 주체 ID)")
    p.add_argument("--registry-password", help="레지스트리 비밀번호/토큰 (cloud-init 에 담겨 VM 에 남는다)")
    p.add_argument("--allow", help="SSH·프록시 접속을 허용할 CIDR (기본: 이 PC 공인 IP/32)")
    p.add_argument("--size", help=f"VM 크기 직접 지정 (기본: {SIZE_CANDIDATES} 중 사용 가능한 가장 싼 것)")
    p.add_argument("--no-public-ip", action="store_true", help="공용 IP 없이 생성 (비용 절감, 외부 접속 불가)")
    p.add_argument("--auto-shutdown", default="01:00", metavar="HH:MM",
                   help="매일 자동 종료(deallocate) 시각, 한국 시간 (기본 01:00). 'off' 면 설정 안 함")
    p.add_argument("--dry-run", action="store_true", help="az 생성 명령을 실행하지 않고 출력만 한다")
    for key in ("subscription", "resource_group", "location", "zone", "image", "vnet"):
        p.add_argument("--" + key.replace("_", "-"), default=DEFAULTS[key])
    args = p.parse_args()
    DRY_RUN = args.dry_run

    if not args.proxy:
        choice = input("프록시 종류를 고르세요 [1] squid (포워드 프록시)  [2] haproxy (TCP 전달)  "
                       "[3] nginx (리버스 프록시): ").strip().lower()
        args.proxy = {"1": "squid", "2": "haproxy", "3": "nginx"}.get(choice, choice)
        if args.proxy not in PROXY_PORT:
            p.error("squid, haproxy, nginx 중에서 골라야 합니다.")
    if args.proxy == "haproxy" and not args.backend:
        args.backend = input("HAProxy 가 전달할 대상 host:port: ").strip()
    if args.proxy == "haproxy" and not re.fullmatch(r"[\w.-]+:\d+", args.backend or ""):
        p.error("--backend 는 host:port 형식이어야 합니다.")
    if args.proxy == "nginx" and not args.backend:
        args.backend = input("nginx 가 전달할 대상 URL (예: https://www.data.go.kr): ").strip()
    if args.proxy == "nginx" and not re.fullmatch(r"https?://[\w.-]+(:\d+)?/?", args.backend or ""):
        p.error("nginx 의 --backend 는 http(s)://host[:port] 형식이어야 합니다.")
    port = args.port or PROXY_PORT[args.proxy]
    if args.auto_shutdown != "off" and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", args.auto_shutdown):
        p.error("--auto-shutdown 은 HH:MM(한국 시간) 또는 off 여야 합니다.")
    if not args.install:
        choice = input("설치 방식을 고르세요 [1] podman 이미지(권장)  [2] docker 이미지  [3] apt 패키지: ").strip().lower()
        args.install = {"1": "podman", "2": "docker", "3": "apt", "": "podman"}.get(choice, choice)
        if args.install not in ("podman", "docker", "apt"):
            p.error("podman, docker, apt 중에서 골라야 합니다.")
    if args.install in ("podman", "docker"):
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
