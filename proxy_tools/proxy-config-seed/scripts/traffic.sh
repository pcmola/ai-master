#!/usr/bin/env bash
# Send test requests through the proxy so access.log has fresh entries.
#
#   scripts/traffic.sh <vm-ip> [rounds]     (default 5 rounds)
#   PROXY_PORT=<port> scripts/traffic.sh <vm-ip>   (default 8080, the Squid listening port)
#
# 허용 도메인 4곳에 rounds 번씩 HTTPS 요청을 보낸다. 정상이면 모두 200/30x,
# 차단되면 CONNECT 403, 업스트림·DNS 장애면 503/504 가 찍힌다.
set -uo pipefail

host="${1:?usage: $0 <vm-ip> [rounds]}"
rounds="${2:-5}"
port="${PROXY_PORT:-8080}"
urls=(https://www.data.go.kr https://api.data.go.kr https://www.law.go.kr https://open.law.go.kr)

ok=0 fail=0
for ((i = 1; i <= rounds; i++)); do
  for url in "${urls[@]}"; do
    # http_connect: 프록시의 CONNECT 응답 코드, http_code: 목적지 응답 코드
    out="$(curl -s -o /dev/null -x "http://$host:$port" --max-time 20 \
      -w '%{http_connect} %{http_code}' "$url" 2>/dev/null)" || true
    connect="${out%% *}" code="${out##* }"
    if [ "$connect" = 200 ] && [ "$code" != 000 ]; then ok=$((ok + 1)); else fail=$((fail + 1)); fi
    printf '%s round %d  connect=%s http=%s  %s\n' "$(date +%T)" "$i" "${connect:-000}" "${code:-000}" "$url"
  done
done
echo "ok=$ok fail=$fail"
[ "$fail" = 0 ]
