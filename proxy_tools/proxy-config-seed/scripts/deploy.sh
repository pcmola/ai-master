#!/usr/bin/env bash
# Deploy squid/ to the proxy VM.
#
#   scripts/deploy.sh <vm-ip> [--user azureuser] [--restart] [--skip-validate]
#
# 1. tests/validate.py 로 형식을 검사한다.
# 2. VM 의 임시 폴더로 올린 뒤 같은 이미지로 'squid -k parse' 를 돌려 문법을 확인한다.
# 3. 지금 설정(/etc/proxy/proxy.conf, lists/)을 /etc/proxy/backup/<시각>/ 에 백업하고(최근 10개만 남김),
#    /etc/proxy 에 설치한 뒤 reconfigure(기본) 또는 restart(--restart) 한다.
#    되돌리기: VM 에서 sudo cp -a /etc/proxy/backup/<시각>/* /etc/proxy/ 후 sudo podman exec squid squid -k reconfigure
# 문법 검사를 통과해도 동작이 틀릴 수 있다(오타 난 도메인, 잘못된 DNS 등). 배포 후 scripts/traffic.sh 로 확인한다.
set -euo pipefail

host="" user="azureuser" restart=0 validate=1
while [ $# -gt 0 ]; do
  case "$1" in
    --user) user="$2"; shift 2 ;;
    --restart) restart=1; shift ;;
    --skip-validate) validate=0; shift ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) host="$1"; shift ;;
  esac
done
[ -n "$host" ] || { echo "usage: $0 <vm-ip> [--user U] [--restart] [--skip-validate]" >&2; exit 2; }

root="$(cd "$(dirname "$0")/.." && pwd)"
image="$(sed -n 's/^Image=//p' "$root/systemd/squid.container")"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10)
target="$user@$host"
stage="/tmp/proxy-config-$$"

if [ "$validate" = 1 ]; then
  "${PYTHON:-python}" "$root/tests/validate.py"
fi

echo "upload -> $target:$stage"
ssh "${ssh_opts[@]}" "$target" "mkdir -p $stage/lists"
scp -q "${ssh_opts[@]}" "$root/squid/proxy.conf" "$target:$stage/proxy.conf"
scp -q "${ssh_opts[@]}" "$root/squid/lists/"* "$target:$stage/lists/"

action="sudo podman exec squid squid -k reconfigure"
[ "$restart" = 1 ] && action="sudo systemctl restart squid"

ssh "${ssh_opts[@]}" "$target" "set -e
trap 'rm -rf $stage' EXIT
sudo podman run --rm -v $stage/proxy.conf:/etc/squid/squid.conf:ro -v $stage/lists:/etc/squid/lists:ro $image squid -k parse >/dev/null
backup=/etc/proxy/backup/\$(date +%Y%m%d-%H%M%S)
sudo mkdir -p \$backup
sudo cp -a /etc/proxy/proxy.conf /etc/proxy/lists \$backup/ 2>/dev/null || true
ls -1dt /etc/proxy/backup/*/ 2>/dev/null | tail -n +11 | xargs -r sudo rm -rf
echo backup: \$backup
sudo install -m 0644 $stage/proxy.conf /etc/proxy/proxy.conf
sudo install -m 0644 $stage/lists/* /etc/proxy/lists/
$action
echo deployed: \$(sha256sum /etc/proxy/proxy.conf | cut -c1-12)"
