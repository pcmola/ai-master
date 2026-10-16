"""Simulate Squid error logs and detect anomalies in proxy access logs.

simulate: 정상 트래픽에 장애·이상 시나리오를 섞은 가상의 access.log 를 만든다(실제 로그는 건드리지 않는다).
detect  : 로컬 파일 또는 프록시 VM(SSH)의 로그를 규칙으로 분석해 이상 징후를 보고한다.

사용 예:
    python proxy_log_detect.py simulate                         # sim_access.log 생성 + 심은 시나리오 출력
    python proxy_log_detect.py simulate --upload 72.155.73.113  # VM 의 ~/sim_access.log 로도 올린다
    python proxy_log_detect.py detect sim_access.log            # 로컬 파일 분석
    python proxy_log_detect.py detect --host 72.155.73.113 --sim  # VM 의 가상 로그 분석
    python proxy_log_detect.py detect --host 72.155.73.113        # VM 의 실제 squid 로그 분석
    python proxy_log_detect.py detect sim_access.log --json       # 결과를 JSON 으로

detect 는 high 등급이 하나라도 있으면 종료 코드 2, medium 만 있으면 1, 없으면 0 을 돌려준다(스케줄 실행용).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from proxy_log_viewer import LOG_PATH, SSH_USER, parse_line

BASE = Path(__file__).resolve().parent
SIM_FILE = BASE / "sim_access.log"
REMOTE_SIM = "sim_access.log"  # VM 의 /home/azureuser 아래
ALLOWED_CLIENTS = ["14.39.36.171/32"]
WHITELIST = ["data.go.kr", "law.go.kr"]

# 탐지 기준. 값은 소규모 프록시(1~2명 사용) 기준이며 운영하면서 조정한다.
RULES = {
    "denied_burst": {"count": 10, "window": 60},          # 한 클라이언트가 60초에 10회 이상 차단
    "domain_scan": {"count": 8, "window": 300},           # 한 클라이언트가 5분에 서로 다른 차단 도메인 8개 이상
    "upstream_error": {"count": 3, "window": 300},        # 한 도메인에서 5분에 5xx/000 오류 3회 이상
    "slow": {"ms": 30000},                                # 요청 하나가 30초 이상
    "large_transfer": {"bytes": 50 * 1024 * 1024},        # 요청 하나가 50MB 이상
    "client_volume": {"bytes": 200 * 1024 * 1024, "window": 600},  # 한 클라이언트가 10분에 200MB 이상
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------- simulate

def line(ts: float, ms: int, client: str, code: str, status: int, size: int, method: str, url: str,
         hier: str = "HIER_DIRECT/27.101.236.55", ctype: str = "-") -> str:
    return f"{ts:.3f} {ms:6d} {client} {code}/{status:03d} {size} {method} {url} - {hier} {ctype}"


def simulate(seed: int, minutes: int) -> tuple[list[str], list[str]]:
    rnd = random.Random(seed)
    end = time.time()
    start = end - minutes * 60
    me = ALLOWED_CLIENTS[0].split("/")[0]
    hosts = ["www.data.go.kr", "api.data.go.kr", "www.law.go.kr", "open.law.go.kr"]
    rows: list[tuple[float, str]] = []
    truth: list[str] = []

    # 정상 트래픽: 허용 도메인으로의 HTTPS 터널
    for _ in range(minutes * 4):
        ts = rnd.uniform(start, end)
        rows.append((ts, line(ts, rnd.randint(40, 900), me, "TCP_TUNNEL", 200, rnd.randint(2_000, 400_000),
                              "CONNECT", f"{rnd.choice(hosts)}:443")))

    def at(frac: float) -> float:
        return start + (end - start) * frac

    # 1) 화이트리스트 밖 사이트를 짧은 시간에 반복 접근 → denied_burst
    t0 = at(0.15)
    for i in range(15):
        ts = t0 + i * 2.5
        rows.append((ts, line(ts, 0, me, "TCP_DENIED", 403, 3394, "CONNECT", "www.google.com:443", "HIER_NONE/-", "text/html")))
    truth.append("차단 반복: 14.39.36.171 → www.google.com 15회/40초")

    # 2) 여러 외부 도메인을 훑는 접근 → domain_scan
    t0 = at(0.35)
    scan_ip = "10.10.0.50"
    for i, d in enumerate(["github.com", "pypi.org", "naver.com", "daum.net", "youtube.com", "facebook.com",
                           "dropbox.com", "pastebin.com", "telegram.org", "ngrok.io"]):
        ts = t0 + i * 12
        rows.append((ts, line(ts, 0, scan_ip, "TCP_DENIED", 403, 3394, "CONNECT", f"{d}:443", "HIER_NONE/-", "text/html")))
    truth.append("도메인 스캔: 10.10.0.50 → 차단 도메인 10개/2분 (허용 목록에 없는 클라이언트)")

    # 3) 허용 도메인의 업스트림 장애(접속 불가·게이트웨이 오류)
    t0 = at(0.55)
    for i, (code, status) in enumerate([("TCP_MISS", 503), ("TCP_TUNNEL", 503), ("NONE_NONE", 0),
                                        ("TCP_MISS", 502), ("TCP_MISS", 504)]):
        ts = t0 + i * 20
        rows.append((ts, line(ts, rnd.randint(5_000, 15_000), me, code, status, 0, "CONNECT",
                              "api.data.go.kr:443", "HIER_NONE/-")))
    truth.append("업스트림 오류: api.data.go.kr 5xx/000 5회/100초")

    # 4) 응답 지연
    ts = at(0.65)
    rows.append((ts, line(ts, 62_000, me, "TCP_TUNNEL", 200, 12_000, "CONNECT", "open.law.go.kr:443")))
    truth.append("응답 지연: open.law.go.kr 62초")

    # 5) 대용량 전송(대량 다운로드)
    t0 = at(0.75)
    for i in range(4):
        ts = t0 + i * 60
        rows.append((ts, line(ts, rnd.randint(20_000, 40_000), me, "TCP_TUNNEL", 200, 80 * 1024 * 1024,
                              "CONNECT", "api.data.go.kr:443")))
    truth.append("대용량 전송: 14.39.36.171 80MB x 4회/3분 (단건 + 누적)")

    # 6) 비표준 포트 터널 시도(SSH 등) → Safe_ports 에서 차단
    t0 = at(0.85)
    for i, target in enumerate(["www.data.go.kr:22", "www.data.go.kr:8443", "45.33.32.156:3389"]):
        ts = t0 + i * 5
        rows.append((ts, line(ts, 0, me, "TCP_DENIED", 403, 3394, "CONNECT", target, "HIER_NONE/-", "text/html")))
    truth.append("비표준 포트: :22, :8443, :3389 CONNECT 시도 3회")

    # 7) 허용되지 않은 클라이언트 IP(NSG 가 열려 있으면 생길 수 있음)
    ts = at(0.9)
    rows.append((ts, line(ts, 0, "203.0.113.77", "TCP_DENIED", 403, 3394, "CONNECT", "www.data.go.kr:443",
                          "HIER_NONE/-", "text/html")))
    truth.append("미허용 클라이언트: 203.0.113.77")

    rows.sort()
    return [r for _, r in rows], truth


def upload(ip: str, path: Path):
    cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", str(path), f"{SSH_USER}@{ip}:{REMOTE_SIM}"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise SystemExit(f"업로드 실패: {res.stderr.decode('utf-8', errors='replace').strip()}")


# ---------------------------------------------------------------- detect

@dataclass
class Finding:
    severity: str
    rule: str
    title: str
    detail: str
    first: str
    last: str
    count: int
    samples: list[str] = field(default_factory=list)


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def window_hits(rows: list[dict], window: float, threshold: int) -> list[list[dict]]:
    """시간순 rows 에서 window 초 안에 threshold 건 이상 몰린 구간들(겹치면 하나로 합침)."""
    bursts, q = [], deque()
    current: list[dict] | None = None
    for r in rows:
        q.append(r)
        while r["ts"] - q[0]["ts"] > window:
            q.popleft()
        if len(q) >= threshold:
            if current is not None and q[0]["ts"] <= current[-1]["ts"]:
                current.extend(x for x in q if x["ts"] > current[-1]["ts"])
            else:
                current = list(q)
                bursts.append(current)
    return bursts


def finding(severity, rule, title, detail, rows) -> Finding:
    return Finding(severity, rule, title, detail, fmt(rows[0]["ts"]), fmt(rows[-1]["ts"]), len(rows),
                   [f"{fmt(r['ts'])} {r['client']} {r['code']}/{r['status']} {r['method']} {r['url']}" for r in rows[:3]])


def is_error(r: dict) -> bool:
    return r["status"].startswith("5") or (r["status"] == "000" and not r["denied"])


def detect(rows: list[dict], allowed: list[str], whitelist: list[str]) -> list[Finding]:
    rows = sorted(rows, key=lambda r: r["ts"])
    nets = [ipaddress.ip_network(c) for c in allowed]
    by_client, denied_by_client, err_by_host = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_client[r["client"]].append(r)
        if r["denied"]:
            denied_by_client[r["client"]].append(r)
        if is_error(r):
            err_by_host[r["host"]].append(r)
    out: list[Finding] = []

    rule = RULES["denied_burst"]
    for client, rs in denied_by_client.items():
        for b in window_hits(rs, rule["window"], rule["count"]):
            hosts = sorted({x["host"] for x in b})
            out.append(finding("medium", "denied_burst", "차단 반복",
                               f"{client} 가 {len(b)}회 차단됨 (대상: {', '.join(hosts[:5])})", b))

    rule = RULES["domain_scan"]
    for client, rs in denied_by_client.items():
        q: deque = deque()
        flagged_until = 0.0
        for r in rs:
            q.append(r)
            while r["ts"] - q[0]["ts"] > rule["window"]:
                q.popleft()
            domains = {x["host"] for x in q}
            if len(domains) >= rule["count"] and r["ts"] > flagged_until:
                b = list(q)
                out.append(finding("high", "domain_scan", "도메인 스캔 의심",
                                   f"{client} 가 {len(domains)}개 도메인에 접근 시도 후 차단됨", b))
                flagged_until = r["ts"] + rule["window"]

    rule = RULES["upstream_error"]
    for host, rs in err_by_host.items():
        for b in window_hits(rs, rule["window"], rule["count"]):
            codes = sorted({f"{x['code']}/{x['status']}" for x in b})
            out.append(finding("high", "upstream_error", "업스트림 장애",
                               f"{host} 오류 {len(b)}회 ({', '.join(codes)})", b))

    def allowed_client(ip: str) -> bool:
        try:
            return any(ipaddress.ip_address(ip) in n for n in nets)
        except ValueError:
            return False

    # 건별 규칙은 (규칙, 클라이언트, 대상) 단위로 묶어 한 건으로 보고한다.
    single: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["ms"] >= RULES["slow"]["ms"]:
            single[("slow", r["client"], r["host"])].append(r)
        if r["bytes"] >= RULES["large_transfer"]["bytes"]:
            single[("large_transfer", r["client"], r["host"])].append(r)
        port = r["url"].rsplit(":", 1)[-1] if r["method"] == "CONNECT" else ""
        if port and port.isdigit() and port != "443":
            single[("odd_port", r["client"], "")].append(r)
        elif (r["denied"] and allowed_client(r["client"]) and whitelist
              and any(r["host"] == w or r["host"].endswith("." + w) for w in whitelist)):
            # 허용 클라이언트가 허용 도메인으로 갔는데 막힘 → 화이트리스트·설정 문제
            single[("whitelisted_denied", r["client"], r["host"])].append(r)
    for (kind, client, host), rs in single.items():
        if kind == "slow":
            worst = max(x["ms"] for x in rs) / 1000
            out.append(finding("low", kind, "응답 지연", f"{host} {len(rs)}건, 최대 {worst:.1f}초", rs))
        elif kind == "large_transfer":
            total = sum(x["bytes"] for x in rs) / 1048576
            out.append(finding("low", kind, "대용량 전송", f"{client} → {host} {len(rs)}건, 합계 {total:.0f}MB", rs))
        elif kind == "odd_port":
            targets = ", ".join(sorted({x["url"] for x in rs}))
            out.append(finding("medium", kind, "비표준 포트 터널 시도", f"{client} → {targets}", rs))
        else:
            out.append(finding("medium", kind, "허용 도메인이 차단됨",
                               f"{host} 는 화이트리스트에 있는데 {client} 요청이 {len(rs)}회 차단됨", rs))

    rule = RULES["client_volume"]
    for client, rs in by_client.items():
        q, total, flagged_until = deque(), 0, 0.0
        for r in rs:
            q.append(r)
            total += r["bytes"]
            while r["ts"] - q[0]["ts"] > rule["window"]:
                total -= q.popleft()["bytes"]
            if total >= rule["bytes"] and r["ts"] > flagged_until:
                out.append(finding("medium", "client_volume", "누적 전송량 과다",
                                   f"{client} 가 {rule['window'] // 60}분간 {total / 1048576:.0f}MB 전송", list(q)))
                flagged_until = r["ts"] + rule["window"]

    for client, rs in by_client.items():
        if not allowed_client(client):
            out.append(finding("high", "unknown_client", "미허용 클라이언트 접근",
                               f"{client} 는 허용 목록({', '.join(allowed)})에 없음 — NSG 설정 확인", rs))

    out.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.first))
    return out


def load_rows(args) -> tuple[list[dict], str]:
    if args.host:
        path = REMOTE_SIM if args.sim else LOG_PATH
        remote = f"cat {REMOTE_SIM}" if args.sim else f"sudo docker exec squid cat {LOG_PATH}"
        res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{SSH_USER}@{args.host}", remote],
                             capture_output=True, timeout=60)
        if res.returncode != 0:
            raise SystemExit(f"로그 읽기 실패: {res.stderr.decode('utf-8', errors='replace').strip()}")
        text, source = res.stdout.decode("utf-8", errors="replace"), f"{args.host}:{path}"
    else:
        file = Path(args.file or SIM_FILE)
        text, source = file.read_text(encoding="utf-8", errors="replace"), str(file)
    return [r for r in map(parse_line, text.splitlines()) if r], source


def report(findings: list[Finding], rows: list[dict], source: str):
    print(f"분석 대상: {source} ({len(rows)}건"
          + (f", {fmt(rows[0]['ts'])} ~ {fmt(rows[-1]['ts'])})" if rows else ")"))
    if not findings:
        print("탐지된 이상 징후가 없습니다.")
        return
    counts = {s: sum(f.severity == s for f in findings) for s in SEVERITY_ORDER}
    print(f"탐지 {len(findings)}건: high {counts['high']} · medium {counts['medium']} · low {counts['low']}\n")
    for f in findings:
        span = f.first if f.first == f.last else f"{f.first} ~ {f.last}"
        print(f"[{f.severity.upper():6}] {f.title} — {f.detail}")
        print(f"         {span}, 관련 로그 {f.count}건")
        for s in f.samples:
            print(f"           {s}")


def main():
    p = argparse.ArgumentParser(description="Squid 프록시 가상 에러 로그 생성 및 이상 탐지")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate", help="가상 로그 생성")
    s.add_argument("--out", default=str(SIM_FILE))
    s.add_argument("--minutes", type=int, default=60, help="로그가 덮는 기간(분)")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--upload", metavar="IP", help="VM 의 ~/sim_access.log 로 업로드")
    d = sub.add_parser("detect", help="이상 탐지")
    d.add_argument("file", nargs="?", help=f"로컬 로그 파일 (기본 {SIM_FILE.name})")
    d.add_argument("--host", help="프록시 VM IP. 지정하면 SSH 로 읽는다")
    d.add_argument("--sim", action="store_true", help="--host 와 함께: 실제 로그 대신 VM 의 가상 로그를 읽는다")
    d.add_argument("--allow", action="append", help=f"허용 클라이언트 CIDR (기본 {ALLOWED_CLIENTS})")
    d.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.cmd == "simulate":
        lines, truth = simulate(args.seed, args.minutes)
        out = Path(args.out)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{out} 에 {len(lines)}줄 생성. 심어 둔 시나리오:")
        for t in truth:
            print(f"  - {t}")
        if args.upload:
            upload(args.upload, out)
            print(f"VM {args.upload} 의 ~/{REMOTE_SIM} 로 업로드했습니다.")
        return

    rows, source = load_rows(args)
    findings = detect(rows, args.allow or ALLOWED_CLIENTS, WHITELIST)
    if args.json:
        print(json.dumps({"source": source, "rows": len(rows), "findings": [asdict(f) for f in findings]},
                         ensure_ascii=False, indent=2))
    else:
        report(findings, sorted(rows, key=lambda r: r["ts"]), source)
    sys.exit(2 if any(f.severity == "high" for f in findings) else 1 if findings else 0)


if __name__ == "__main__":
    main()
