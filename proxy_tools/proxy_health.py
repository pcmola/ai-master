"""프록시 Health Check: Wiki 기준 문서의 목적지로 실제 접속해 프록시 VM 이 정상인지 본다(시나리오 3 의 첫 단계).

기대값은 Wiki '02. 운영 > 01. 공통 > 06. 네트워크 및 Proxy > 02. 기준 정리 > Proxy Health Check 기준' 에서 읽는다.
proxy-config 의 허용 목록에서 만들지 않는 이유: 설정에 오타가 나면 기대값도 같이 틀려 오타를 못 찾는다.

VM 마다 점검하는 항목
- vm       Azure 전원 상태(운영 = Running)
- service  SSH 로 podman 컨테이너(squid|nginx)가 Up 인지
- allow    이 PC 에서 VM 을 거쳐 '허용 목적지'에 접속 → squid: CONNECT 200, nginx: HTTP 2xx·3xx
- deny     '차단 목적지'에 접속 → CONNECT 403 (squid 만)
설정 일치(배포된 설정 = proxy-config main)는 incident_response.config_drift 가 이어서 본다.

결과는 data/health/latest.json 에 남기고(허브 화면이 읽음) 콘솔에 표로 찍는다.
점검 접속은 VM 의 접속 로그에도 남으므로, 이어지는 로그 탐지(collect)가 같은 증상을 근거로 원인을 찾는다.

    python proxy_tools/proxy_health.py      # 점검만(장애 대응 없이)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import DATA_DIR, WIKI_REPO  # noqa: E402
from common.trace import span, think  # noqa: E402

BASELINE_PAGE = ("AI-Master-프로젝트/02.-운영/01.-공통/06.-네트워크-및-Proxy/02.-네트워크-및-Proxy-기준-정리/"
                 "Proxy-Health-Check-기준.md")
RESULT_FILE = DATA_DIR / "health" / "latest.json"
SQUID_PORT = int(os.environ.get("PROXY_PORT") or 8080)
TRIGGER_LABEL = {"manual": "수동 점검", "auto": "자동 점검", "deploy": "배포 뒤 확인 점검"}
ITEMS = [("vm", "VM 상태"), ("service", "서비스"), ("allow", "허용 목적지 접속"), ("deny", "차단 목적지 접속")]


def load_baseline() -> dict:
    """{'allow': [(kind, url)], 'deny': [(kind, url)]} — 3장 '허용 목적지', 4장 '차단 목적지' 표의 '점검 주소' 열."""
    path = WIKI_REPO / BASELINE_PAGE
    text = path.read_text(encoding="utf-8-sig")
    out: dict[str, list[tuple[str, str]]] = {"allow": [], "deny": []}
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = "allow" if "허용 목적지" in line else "deny" if "차단 목적지" in line else None
            continue
        m = re.match(r"\|\s*\d+\s*\|\s*(squid|nginx)\s*\|\s*(https?://\S+)\s*\|", line)
        if section and m:
            out[section].append((m.group(1), m.group(2)))
    if not out["allow"]:
        raise RuntimeError(f"기준 문서에서 허용 목적지를 찾지 못했습니다: {path}")
    return out


def curl(args: list[str]) -> str:
    """curl.exe(Windows 기본 포함) 출력. 실패해도 -w 출력은 남으므로 그대로 돌려준다."""
    try:
        res = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", "20", *args],
                             capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        return res.stdout.decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def probe_squid(ip: str, url: str) -> tuple[str, str]:
    """(CONNECT 응답 코드, 목적지 응답 코드). 접속 자체가 안 되면 000."""
    out = curl(["-x", f"http://{ip}:{SQUID_PORT}", "-w", "%{http_connect} %{http_code}", url]).split()
    return (out[0] if out else "000"), (out[1] if len(out) > 1 else "000")


def probe_nginx(ip: str, url: str) -> str:
    """Host 머리글로 이름별 전달을 시험한다. HTTP 응답 코드."""
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return curl(["-H", f"Host: {host}", "-w", "%{http_code}", f"http://{ip}/"]) or "000"


def why_blocked(code: str) -> str:
    return {"403": "프록시가 차단(허용 목록·허용 클라이언트 확인)", "503": "업스트림·DNS 실패", "504": "업스트림 시간 초과",
            "000": "프록시 접속 안 됨(포트·NSG 확인)"}.get(code, f"응답 {code}")


def check_vm(vm: dict, base: dict) -> list[dict]:
    from proxy_log_viewer import ssh
    res = []
    add = lambda item, status, detail: res.append({"item": item, "status": status, "detail": detail})
    if vm["state"] != "운영":
        add("vm", "fail", f"VM 이 {vm['state']} 상태 (프록시 VM 은 항상 켜져 있어야 함)")
        for item, _ in ITEMS[1:]:
            add(item, "skip", "VM 이 꺼져 있어 점검 못 함")
        return res
    add("vm", "ok", "Running")

    out, err = ssh(vm["ip"], "sudo podman ps --format '{{.Names}} {{.Status}}'", "container status")
    if err:
        add("service", "skip", f"SSH 실패: {err[:120]}")
    else:
        line = next((x for x in out.splitlines() if x.startswith(vm["kind"])), "")
        add("service", "ok" if " Up " in f" {line} " else "fail", line or f"{vm['kind']} 컨테이너 없음")

    allow = [u for k, u in base["allow"] if k == vm["kind"]]
    deny = [u for k, u in base["deny"] if k == vm["kind"]]
    bad, okn = [], 0
    for url in allow:
        if vm["kind"] == "squid":
            connect, code = probe_squid(vm["ip"], url)
            good = connect == "200" and code != "000"
            if not good:
                bad.append(f"{url} → {why_blocked(connect if connect != '200' else code)}")
        else:
            code = probe_nginx(vm["ip"], url)
            good = code[:1] in ("2", "3")
            if not good:
                bad.append(f"{url} → {why_blocked(code)}")
        okn += good
    if allow:
        add("allow", "fail" if bad else "ok", f"{okn}/{len(allow)} 통과" + (": " + "; ".join(bad) if bad else ""))
    else:
        add("allow", "skip", "기준 없음")

    if deny:
        leaked = [url for url in deny if probe_squid(vm["ip"], url)[0] != "403"]
        add("deny", "fail" if leaked else "ok",
            f"{len(deny) - len(leaked)}/{len(deny)} 차단" + (": 막혀야 하는데 통과 " + ", ".join(leaked) if leaked else ""))
    else:
        add("deny", "skip", "기준 없음" if vm["kind"] == "squid" else "리버스 프록시는 해당 없음")
    return res


def run_checks(trigger: str = "manual") -> dict:
    """모든 프록시 VM 을 점검하고 결과를 저장한다. 결과의 failed 는 이상 항목 [{vm, item, detail}]."""
    from proxy_policy_monitor import proxy_targets
    started = datetime.now().isoformat(timespec="seconds")
    with span("health_check", trigger=trigger) as s:
        base = load_baseline()
        think("Tool Call", f"Wiki 기준 문서 읽기 → 허용 목적지 {len(base['allow'])}곳, 차단 목적지 {len(base['deny'])}곳")
        think("Tool Call", "az vm list → 프록시 VM 판별 (종류 태그 squid|nginx)")
        vms = proxy_targets()
        result = {"at": started, "trigger": trigger, "baseline": BASELINE_PAGE, "vms": [], "failed": []}
        for vm in vms:
            checks = check_vm(vm, base)
            result["vms"].append({"name": vm["name"], "kind": vm["kind"], "ip": vm["ip"], "checks": checks})
            for c in checks:
                if c["status"] == "fail":
                    result["failed"].append({"vm": vm["name"], "item": dict(ITEMS)[c["item"]], "detail": c["detail"]})
            mark = {"ok": "✓", "fail": "✗", "skip": "–"}
            think("Observation", f"{vm['name']} ({vm['kind']}): "
                  + " · ".join(f"{mark[c['status']]} {dict(ITEMS)[c['item']]}" for c in checks))
        result["finished"] = datetime.now().isoformat(timespec="seconds")
        s.set(vms=len(vms), failed=len(result["failed"]))
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print_table(result)
    return result


def print_table(result: dict) -> None:
    mark = {"ok": "정상", "fail": "이상", "skip": "-"}
    print(f"\nHealth Check ({TRIGGER_LABEL.get(result['trigger'], result['trigger'])}, {result['at'].replace('T', ' ')})")
    print(f"  {'VM':14}" + "".join(f"{name:12}" for _, name in ITEMS))
    for v in result["vms"]:
        st = {c["item"]: mark[c["status"]] for c in v["checks"]}
        print(f"  {v['name']:14}" + "".join(f"{st.get(k, '-'):14}" for k, _ in ITEMS))
    for f in result["failed"]:
        print(f"  ✗ {f['vm']} {f['item']}: {f['detail']}")
    print("  결과: " + (f"이상 {len(result['failed'])}건" if result["failed"] else "모두 정상") + "\n")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        from common.paths import ENV_FILE
        load_dotenv(ENV_FILE)
    except ImportError:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="프록시 Health Check (점검만, 장애 대응 없이)")
    ap.add_argument("--trigger", choices=list(TRIGGER_LABEL), default="manual",
                    help="deploy: VM 배포 파이프라인이 끝난 뒤 허브가 자동으로 돌리는 확인 점검")
    args = ap.parse_args()
    if args.trigger == "deploy":
        print("VM 배포 파이프라인이 성공해, 배포된 설정이 기준대로 동작하는지 Health Check 로 확인합니다.")
    t0 = time.monotonic()
    r = run_checks(args.trigger)
    print(f"({time.monotonic() - t0:.1f}초)")
    if args.trigger == "deploy":
        print("배포 뒤 확인: " + ("모두 정상 ✓ 장애 대응을 마칩니다." if not r["failed"]
                               else f"이상 {len(r['failed'])}건이 남았습니다 ✗ 허브에서 'Health Check 실행'으로 다시 대응하세요."))
    raise SystemExit(1 if r["failed"] else 0)
