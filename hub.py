"""Local hub: one page to start, stop and watch the ai-master local web servers.

    python hub.py                # http://ai-master.local (hosts 등록 시), 없으면 http://127.0.0.1:8764
                                 # 이미 떠 있는 허브가 있으면 그 허브만 끄고 새로 띄운다.
                                 # 떠 있는 서버는 그대로 두고, 꺼져 있는 서버만 시작한다
    python hub.py --restart-all  # 서버를 모두 다시 시작(서버 코드를 고친 뒤 반영할 때)
    python hub.py --keep         # 이미 떠 있는 허브가 있으면 끄지 않고 종료
    python hub.py --no-restart   # 서버를 전혀 건드리지 않음(꺼진 서버도 시작하지 않음)
    python hub.py --no-browser

- 서버는 창 없이 띄우고 출력은 data/logs/<key>.log 에 남긴다. 허브를 꺼도 서버는 계속 돈다.
- 실행 여부는 프로세스 명령줄의 스크립트 이름으로 찾는다. 허브 밖에서 직접 띄운 서버도 보이고 끌 수 있다.
- 콘솔에는 시각과 함께 시작 설정, 요청, 생성·종료한 PID, 상태 변화(허브 밖에서 끄거나 죽은 경우 포함), 오류를 남긴다.
- 서버 주소 규칙(hosts 의 IP 에 따라 포트 없는 주소)은 common/server.py 설명 참고.
"""

from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.paths import DATA_DIR, PROJECT_ROOT  # noqa: E402
from common.server import favicon, local_server, open_chrome, site_url  # noqa: E402

LOG_DIR = DATA_DIR / "logs"
HUB_PORT, HUB_SITE, HUB_TITLE = 8764, "ai-master.local", "Azure DevOps 기반 AI 운영"
LOG_ROTATE_BYTES = 2 * 1024 * 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACH = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

SERVERS = [s for s in [
    {"key": "wiki-viewer", "title": "Wiki 뷰어", "dir": PROJECT_ROOT, "script": "wiki_tools/wiki_viewer.py",
     "port": 8765, "site": "wiki.local"},
    {"key": "wiki-search", "title": "Wiki 검색", "dir": PROJECT_ROOT, "script": "wiki_tools/wiki_search.py",
     "port": 8766, "site": "wiki-search.local"},
    {"key": "proxy-log", "title": "프록시 로그 뷰어", "dir": PROJECT_ROOT, "script": "proxy_tools/proxy_log_viewer.py",
     "port": 8768, "site": "proxy-log.local"},
] if (s["dir"] / s["script"]).is_file()]
BY_KEY = {s["key"]: s for s in SERVERS}
# 허브를 켤 때의 로그 파일 크기. 화면은 이 위치를 '지우기'한 것으로 보고 그 뒤 로그만 보여 준다(전체 보기로 되돌림).
LOG_START: dict = {"at": "", "sizes": {}}

# 서버를 띄운 셸(powershell -Command "python x.py")까지 한 묶음으로 보고 끄기 위해 셸 프로세스도 함께 읽는다.
PS_LIST = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process -Filter \"Name like 'python%' or Name='py.exe' or Name='powershell.exe' "
    "or Name='pwsh.exe' or Name='cmd.exe'\" | Select-Object ProcessId,ParentProcessId,Name,CommandLine,"
    "@{n='Started';e={$_.CreationDate.ToString('s')}} | ConvertTo-Json -Compress"
)


# ---------------------------------------------------------------- process state

def processes() -> list[dict]:
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_LIST],
                         capture_output=True, timeout=30, creationflags=NO_WINDOW)
    text = res.stdout.decode("utf-8", errors="replace").strip()
    rows = json.loads(text) if text else []
    rows = rows if isinstance(rows, list) else [rows]
    me = os.getpid()
    return [{"pid": r["ProcessId"], "ppid": r["ParentProcessId"], "name": r["Name"],
             "cmd": r.get("CommandLine") or "", "started": r.get("Started") or ""}
            for r in rows if r["ProcessId"] != me]


def listeners() -> dict[int, list[str]]:
    """PID → 수신 중인 TCP 주소 목록."""
    res = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
    out: dict[int, list[str]] = {}
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "TCP" and parts[3] == "LISTENING" and parts[4].isdigit():
            out.setdefault(int(parts[4]), []).append(parts[1])
    return out


_cache: dict = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()
_busy: dict[str, str] = {}  # key → 진행 중인 작업 이름
_print_lock = threading.Lock()


def log(msg: str):
    """허브 콘솔 로그. 여러 스레드가 함께 쓰므로 줄이 섞이지 않게 잠근다."""
    with _print_lock:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def describe(s: dict) -> str:
    """상태 한 줄: 상태, PID(수신 주소), 브라우저 주소, 경고."""
    pids = ", ".join(f"{i['pid']}({'/'.join(i['listen']) or '수신 없음'})" for i in s["instances"]) or "-"
    notes = [f"중복 {len(s['instances'])}개" if len(s["instances"]) > 1 else "",
             "예전 주소" if s["stale"] else "",
             f"주소 충돌: PID {s['conflict']['pid']} {s['conflict']['name']}" if s["conflict"] else ""]
    notes = [n for n in notes if n]
    return f"{s['state']:8} PID {pids}  {s['url']}" + (f"  [{', '.join(notes)}]" if notes else "")


def server_status(force: bool = False) -> list[dict]:
    with _cache_lock:
        if not force and _cache["data"] is not None and time.monotonic() - _cache["at"] < 1.5:
            return _cache["data"]
        procs, listen = processes(), listeners()
        by_pid = {p["pid"]: p for p in procs}
        owner = {addr: pid for pid, addrs in listen.items() for addr in addrs}
        result = []
        for s in SERVERS:
            script = Path(s["script"]).name.lower()
            matched = {p["pid"]: p for p in procs if script in p["cmd"].lower()}
            children: dict[int, list[int]] = {}
            for p in matched.values():
                children.setdefault(p["ppid"], []).append(p["pid"])
            instances = []
            for root in (p for p in matched.values() if p["ppid"] not in matched):
                tree, stack = [], [root["pid"]]
                while stack:
                    pid = stack.pop()
                    tree.append(pid)
                    stack += children.get(pid, [])
                addrs = sorted({a for pid in tree for a in listen.get(pid, [])})
                instances.append({"pid": root["pid"], "pids": sorted(tree), "started": root["started"], "listen": addrs})
            ip, port, url = site_url(s["site"], s["port"])
            expected = f"{ip}:{port}"
            conflict = None
            if expected in owner and owner[expected] not in {pid for i in instances for pid in i["pids"]}:
                other = by_pid.get(owner[expected], {})
                conflict = {"pid": owner[expected], "name": other.get("name", "?")}
            listening = any(expected in i["listen"] for i in instances)
            other = next((a for i in instances for a in i["listen"]), None)
            # hosts 를 바꾸기 전에 뜬 서버는 예전 주소에 떠 있다. 동작은 하므로 그 주소로 연결하고 재시작을 권한다.
            stale = not listening and other is not None
            if stale:
                url = f"http://{other}"
            state = ("running" if listening or stale else "starting") if instances else "stopped"
            result.append({"key": s["key"], "title": s["title"], "script": s["script"], "url": url, "address": expected,
                           "state": state, "stale": stale, "instances": instances, "conflict": conflict,
                           "busy": _busy.get(s["key"])})
        _cache.update(at=time.monotonic(), data=result)
        return result


# ---------------------------------------------------------------- actions

def start(s: dict):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_file = LOG_DIR / f"{s['key']}.log"
    if out_file.is_file() and out_file.stat().st_size > LOG_ROTATE_BYTES:
        try:
            out_file.replace(out_file.with_suffix(".old.log"))
        except OSError:
            pass  # 다른 프로세스가 쓰고 있으면 그대로 이어 쓴다
    cmd = [sys.executable, s["script"], "--no-browser"]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    with open(out_file, "ab") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {' '.join(cmd)} =====\n".encode("utf-8"))
        f.flush()
        proc = subprocess.Popen(cmd, cwd=s["dir"], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, creationflags=DETACH, close_fds=True)
    log(f"[{s['key']}] 프로세스 생성 PID {proc.pid}: {' '.join(cmd)}")
    log(f"[{s['key']}]   작업 폴더 {s['dir']}, 출력 {out_file}")


def stop(key: str):
    current = next(x for x in server_status(force=True) if x["key"] == key)
    if not current["instances"]:
        log(f"[{key}] 실행 중인 프로세스가 없어 끌 것이 없습니다")
        return
    for inst in current["instances"]:
        res = subprocess.run(["taskkill", "/PID", str(inst["pid"]), "/T", "/F"], capture_output=True,
                             creationflags=NO_WINDOW)
        out = " / ".join(line.strip() for line in res.stdout.decode("cp949", errors="replace").splitlines() if line.strip())
        log(f"[{key}] taskkill PID {inst['pid']} (트리 {inst['pids']}) → 종료 코드 {res.returncode} {out}")
    began, deadline = time.monotonic(), time.monotonic() + 10
    while time.monotonic() < deadline:
        now = next(x for x in server_status(force=True) if x["key"] == key)
        if not now["instances"]:
            log(f"[{key}] 종료 확인 ({time.monotonic() - began:.1f}초)")
            return
        time.sleep(0.5)
    log(f"[{key}] 10초가 지나도 프로세스가 남아 있습니다: {describe(now)}")


def wait_running(key: str, timeout: float = 60) -> dict:
    """시작한 서버가 주소에서 응답할 때까지 기다린다. 그 전에 죽으면 서버 출력의 마지막 줄을 남긴다."""
    deadline = time.monotonic() + timeout
    while True:
        now = next(x for x in server_status(force=True) if x["key"] == key)
        if now["state"] == "running" or time.monotonic() > deadline:
            return now
        if not now["instances"]:
            log(f"[{key}] 시작 직후 프로세스가 끝났습니다. 서버 출력 마지막 줄:")
            for line in log_tail(key, 8)[0].splitlines():
                log(f"[{key}]   | {line}")
            return now
        time.sleep(1)


def run_action(key: str, action: str):
    s = BY_KEY[key]
    _busy[key] = {"start": "시작 중", "stop": "중지 중", "restart": "재시작 중"}[action]
    began = time.monotonic()
    log(f"[{key}] {action} 요청 처리 시작")
    try:
        if action in ("stop", "restart"):
            stop(key)
        if action == "start" and next(x for x in server_status(force=True) if x["key"] == key)["instances"]:
            log(f"[{key}] 이미 실행 중이라 새로 띄우지 않습니다")  # 하나 더 띄우면 중복 실행이 된다
            return
        if action in ("start", "restart"):
            start(s)
            log(f"[{key}] 결과: {describe(wait_running(key))}")
    except Exception as exc:  # 스레드 안의 오류가 콘솔에 남지 않고 사라지지 않도록
        log(f"[{key}] {action} 실패: {type(exc).__name__}: {exc}")
    finally:
        _busy.pop(key, None)
        _cache["at"] = 0.0
        log(f"[{key}] {action} 끝 ({time.monotonic() - began:.1f}초)")


def dispatch(key: str, action: str) -> list[str]:
    keys = list(BY_KEY) if key == "all" else [key]
    busy = [k for k in keys if k in _busy]
    if busy:
        log(f"다른 작업이 진행 중이라 건너뜀: {', '.join(f'{k}({_busy[k]})' for k in busy)}")
    keys = [k for k in keys if k in BY_KEY and k not in _busy]
    for k in keys:
        threading.Thread(target=run_action, args=(k, action), daemon=True).start()
    return keys


def watch(interval: float = 5):
    """상태가 바뀌면 남긴다. 허브 밖에서 끄거나 서버가 죽은 경우도 여기서 보인다."""
    last: dict[str, str] = {}
    while True:
        try:
            for s in server_status():
                if s["busy"]:
                    last.pop(s["key"], None)  # 작업 중 변화와 결과는 run_action 이 남기므로 끝난 뒤 상태를 새 기준으로
                    continue
                line = describe(s)
                if s["key"] in last and last[s["key"]] != line:
                    log(f"[{s['key']}] 상태 변경: {line}")
                last[s["key"]] = line
        except Exception as exc:
            log(f"상태 확인 실패: {type(exc).__name__}: {exc}")
        time.sleep(interval)


# ---------------------------------------------------------------- VM 배포 파이프라인(Azure DevOps) 지켜보기

DEPLOY_PIPELINE = os.environ.get("SCENARIO3_DEPLOY_PIPELINE", "proxy-config VM 배포")
PIPELINE_POLL_SEC = 10
PIPELINE_STATE: dict = {}  # 화면용 최근 실행 {name, state, result, started, finished, url}
LOG_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+Z ?")


def pipeline_watch():
    """VM 배포 파이프라인은 Azure DevOps 에서 사람이 Run 한다. 허브에서도 진행을 보도록 10초마다 최근 실행을 읽어
    배포 단계 로그를 data/logs/scenario3.log 에 이어 붙인다(시나리오 3 로그 창에 보임). 허브를 켜기 전 끝난 실행은 건너뛴다."""
    sys.path.insert(0, str(PROJECT_ROOT / "proxy_tools"))
    try:
        from proxy_policy_publish import DevOps
        dv = DevOps()
        found = [x for x in dv.request("GET", "/_apis/pipelines?api-version=7.1", None).get("value", [])
                 if x.get("name") == DEPLOY_PIPELINE]
    except Exception as exc:
        log(f"VM 배포 파이프라인 지켜보기 끔: {exc}")
        return
    if not found:
        log(f"VM 배포 파이프라인 '{DEPLOY_PIPELINE}' 이 없어 지켜보지 않습니다")
        return
    pid, seen, sent = found[0]["id"], set(), {}  # seen: 끝까지 남긴 실행, sent: 실행별 이미 남긴 로그 줄 수
    log(f"VM 배포 파이프라인 '{DEPLOY_PIPELINE}' (id {pid}) 실행을 {PIPELINE_POLL_SEC}초마다 확인해 scenario3.log 에 남깁니다")
    first = True
    while True:
        try:
            runs = dv.request("GET", f"/_apis/pipelines/{pid}/runs?api-version=7.1", None).get("value", [])[:3]
            for run in reversed(runs):
                rid = run["id"]
                if rid in seen:
                    continue
                if first and run.get("state") == "completed":
                    seen.add(rid)  # 허브를 켜기 전에 끝난 실행
                    continue
                PIPELINE_STATE.update(name=run.get("name"), state=run.get("state"), result=run.get("result"),
                                      started=run.get("createdDate"), finished=run.get("finishedDate"),
                                      url=run.get("_links", {}).get("web", {}).get("href"))
                path = LOG_DIR / "scenario3.log"
                with open(path, "a", encoding="utf-8") as f:
                    if rid not in sent:
                        sent[rid] = 0
                        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 파이프라인 '{DEPLOY_PIPELINE}' #{run.get('name')} "
                                f"실행 확인 (Azure DevOps, 10초마다 읽음) =====\n")
                    lines, step = pipeline_log_lines(dv, rid)
                    for line in lines[sent[rid]:]:
                        f.write(line + "\n")
                    if len(lines) == sent[rid] and run.get("state") != "completed":
                        # 실행 중인 단계의 로그는 그 단계가 끝나야 받을 수 있어, 그동안 지금 단계를 남긴다
                        if step and "배포" in step:
                            step += f" ({pipeline_target(run)})"
                        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} … 파이프라인 진행 중: {step or '대기'}\n")
                    sent[rid] = len(lines)
                    if run.get("state") == "completed":
                        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ===== 파이프라인 #{run.get('name')} 종료: "
                                f"{run.get('result')} =====\n")
                        seen.add(rid)
            first = False
        except Exception as exc:
            log(f"VM 배포 파이프라인 확인 실패: {type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(PIPELINE_POLL_SEC)


def pipeline_target(run: dict) -> str:
    """실행할 때 고른 배포 대상 VM(targetVMs, 비우면 실행 중인 squid VM 전부)과 문법 확인만 여부."""
    params = run.get("templateParameters") or {}
    vms = ", ".join(v.strip() for v in (params.get("targetVMs") or "").split(",") if v.strip())
    label = vms or "실행 중인 squid VM 전부"
    if str(params.get("checkOnly")).lower() == "true":
        label += ", 문법 확인만"
    return label


def pipeline_log_lines(dv, run_id: int) -> tuple[list[str], str | None]:
    """(배포 단계 로그 줄, 지금 실행 중인 단계 이름). 로그는 배포 스크립트가 시작된 뒤 줄만, 한국 시각을 붙인다.
    그 앞의 az 로그인 출력(구독 JSON)은 뺀다."""
    from datetime import timezone
    tl = dv.request("GET", f"/_apis/build/builds/{run_id}/timeline?api-version=7.1", None)
    records = tl.get("records") or []
    step = next((r.get("name") for r in records if r.get("type") == "Task" and r.get("state") == "inProgress"), None)
    rec = next((r for r in records if r.get("type") == "Task" and "배포" in (r.get("name") or "")
                and r.get("log")), None)
    if not rec:
        return [], step
    from urllib.request import Request, urlopen
    with urlopen(Request(rec["log"]["url"], headers={"Authorization": dv.auth}), timeout=30) as res:
        raw = res.read().decode("utf-8", errors="replace").splitlines()
    out, started = [], False
    for line in raw:
        m = LOG_STAMP.match(line)
        text = line[m.end():] if m else line
        if "pipeline_deploy.sh" in text:
            started = True
            continue
        if not started or not text.strip() or text.startswith("##[") or "az account clear" in text:
            continue
        when = (datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
                if m else datetime.now())
        out.append(f"{when:%Y-%m-%d %H:%M:%S} [pipeline] {text}")
    return out, step


# 서버가 아니라 실행하고 끝나는 작업(시나리오 실행 탭). log 는 data/logs/<log>.log.
# self_log=True 인 작업은 스스로 그 파일에 출력을 남기므로 허브는 출력을 버린다(중복 방지).
JOBS = {
    "scenario2": {"title": "시나리오 2 · Wiki 현행화", "script": "proxy_tools/proxy_status_sync.py", "log": "scenario2",
                  "self_log": True,
                  "options": {}},
    "scenario3": {"title": "시나리오 3 · Health Check", "script": "proxy_tools/incident_response.py", "log": "scenario3",
                  "self_log": True, "options": {"dry_run": "--dry-run"}},
    # 시험용: proxy-config 에 잘못된 설정을 넣어 wjy-proxy01 에 배포(장애 모의 훈련), 그리고 되돌리기
    "scenario3-fault": {"title": "시나리오 3 · 장애 모의 훈련(시험용)", "script": "proxy_tools/proxy_fault_inject.py", "log": "scenario3",
                        "self_log": False, "options": {}, "base": ["apply"],
                        "choice": ["whitelist-typo", "client-acl-typo", "bad-dns"],
                        "tail": ["--push", "--deploy", "wjy-proxy01"]},
    "scenario3-restore": {"title": "시나리오 3 · 장애 원상 복구", "script": "proxy_tools/proxy_fault_inject.py", "log": "scenario3",
                          "self_log": False, "options": {}, "base": ["restore", "--push", "--deploy", "wjy-proxy01"]},
    # 공인 IP 가 바뀌어 SSH 가 타임아웃 나면: Proxy VM NSG 의 단일 IP 허용 규칙을 이 PC 의 현재 IP 로 교체
    "nsg-ip": {"title": "내 IP 허용 (NSG)", "script": "proxy_tools/sync_nsg_ip.py", "log": "scenario3",
               "self_log": False, "options": {}},
    "scenario2-servers": {"title": "시나리오 2 · 서버 환경 점검", "script": "test/scenario2/setup_env.py", "log": "scenario2",
                          "self_log": False, "options": {}, "base": ["--servers"]},  # 꺼진 VM 만 켜고 사전 조건 점검
    "scenario2-setup": {"title": "시나리오 2 · 불일치 모의 훈련", "script": "test/scenario2/setup_env.py", "log": "scenario2",
                        "self_log": False, "options": {}, "base": ["--seed"]},  # 표·정답을 다시 만들고 문서를 '현행화 전'으로
    "scenario2-restore": {"title": "시나리오 2 · 원상 복구", "script": "test/scenario2/setup_env.py", "log": "scenario2",
                          "self_log": False, "options": {}, "base": ["--restore"]},  # 문서만 실제 VM 과 같은 정상 표로
}
JOB_LOGS = {j["log"] for j in JOBS.values()}
RUNNING: dict[str, dict] = {}  # 작업 → {proc, pid, started, args, finished, code}
_job_lock = threading.Lock()


# 작업 출력에 섞여 오는 다른 시각 표기: Squid(VM 의 UTC, '2026/10/31 02:36:35| '), traffic.sh('11:36:47 ')
FOREIGN_STAMP = re.compile(r"^(?:\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\| ?|\d{2}:\d{2}:\d{2} +)")
JOB_QUIET_SEC = 5  # 이만큼 출력이 없으면 '진행 중' 줄을 남긴다


def pump_job_log(proc: subprocess.Popen, path: Path):
    """자체 로그가 없는 작업의 출력을 줄마다 'YYYY-MM-DD HH:MM:SS ' 를 붙여 로그 파일에 쓴다.
    출력이 JOB_QUIET_SEC 넘게 없으면 진행 줄을 남겨 멈춘 것처럼 보이지 않게 하고, 끝나면 종료 코드·소요 시간을 남긴다."""
    lock, started = threading.Lock(), time.time()
    last = [started]

    def write(text: str):
        with lock, open(path, "ab") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n".encode("utf-8"))
        last[0] = time.time()

    def beat():
        while proc.poll() is None:
            time.sleep(1)
            quiet = time.time() - last[0]
            if quiet >= JOB_QUIET_SEC and proc.poll() is None:
                write(f"… 진행 중 (마지막 출력 후 {quiet:.0f}초, 시작 후 {time.time() - started:.0f}초)")

    threading.Thread(target=beat, daemon=True).start()
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.strip():
            write(FOREIGN_STAMP.sub("", line))
    code = proc.wait()
    write(f"===== 종료: 코드 {code}, 소요 {time.time() - started:.0f}초 =====")


def stop_job(job: str, reason: str) -> None:
    """실행 중인 작업을 자식 프로세스(ssh·승인 서버 등)까지 끈다. 로그에 중지 줄을 남긴다."""
    r = RUNNING[job]
    subprocess.run(["taskkill", "/PID", str(r["pid"]), "/T", "/F"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
    try:
        r["proc"].wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    with open(LOG_DIR / f"{JOBS[job]['log']}.log", "ab") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 중지: {JOBS[job]['title']} ({reason}) =====\n".encode("utf-8"))
    log(f"[{job}] 작업 중지 PID {r['pid']}: {reason}")


def run_job(job: str, options: dict) -> dict:
    """작업을 창 없이 실행한다. 같은 로그를 쓰는 작업(예: 시나리오 2 실행과 테스트 환경 준비)은 동시에 돌리지 않는다.
    원상 복구(-restore)는 force 로 부르면 실행 중인 작업을 멈추고 실행한다(화면에서 한 번 확인받은 뒤)."""
    spec = JOBS[job]
    with _job_lock:
        busy = [k for k, r in RUNNING.items() if JOBS[k]["log"] == spec["log"] and r["proc"].poll() is None]
        if busy and options.get("force") and job.endswith("-restore"):
            for k in busy:
                stop_job(k, "원상 복구를 위해 허브에서 멈춤")
            busy = []
        if busy:
            return {"error": f"이미 실행 중입니다: {JOBS[busy[0]]['title']}"}
        args = [flag for opt, flag in spec["options"].items() if options.get(opt)]
        if "choice" in spec:  # 고정 목록에서만 고른다(화면에서 온 값을 그대로 명령에 넣지 않는다)
            if options.get("choice") not in spec["choice"]:
                return {"error": f"고를 수 있는 값: {', '.join(spec['choice'])}"}
            args = [options["choice"], *args]
        args = [*spec.get("base", []), *args, *spec.get("tail", [])]
        cmd = [sys.executable, spec["script"], *args]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if spec["self_log"]:
            proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
        else:
            path = LOG_DIR / f"{spec['log']}.log"
            with open(path, "ab") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {spec['script']} {' '.join(args)} =====\n".encode("utf-8"))
            proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
            threading.Thread(target=pump_job_log, args=(proc, path), daemon=True).start()
        RUNNING[job] = {"proc": proc, "pid": proc.pid, "started": datetime.now().isoformat(timespec="seconds"),
                        "args": args, "finished": None, "code": None}
    log(f"[{job}] 작업 실행 PID {proc.pid}: {' '.join(cmd)}")
    return {"pid": proc.pid, "args": args}


_fault_fetch = {"at": 0.0, "busy": False}
FAULT_FETCH_SEC = 30


def _fetch_proxy_config(fi):
    try:
        fi.git("fetch", "-q", "origin")
    except Exception:
        pass
    finally:
        _fault_fetch.update(at=time.monotonic(), busy=False)


def fault_state() -> dict:
    """시나리오 3 시험용 장애가 proxy-config 원격 main(origin/main)에 심어져 있는지.
    로컬 사본은 장애 모의 훈련·원상 복구 때만 최신화되므로, Azure DevOps 에서 수정 PR 을 병합해도 로컬은 장애 그대로였다
    (2026-10-31). 그래서 30초마다 뒤에서 git fetch 하고 origin/main 내용을 본다(화면 갱신은 2초라 기다리지 않음)."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "proxy_tools"))
        import proxy_fault_inject as fi
        if not _fault_fetch["busy"] and time.monotonic() - _fault_fetch["at"] > FAULT_FETCH_SEC:
            _fault_fetch["busy"] = True
            threading.Thread(target=_fetch_proxy_config, args=(fi,), daemon=True).start()
        names = {"whitelist-typo": "허용 도메인 오타", "client-acl-typo": "허용 클라이언트 IP 오타", "bad-dns": "잘못된 DNS 서버"}
        return {"active": [names.get(n, n) for n in fi.active("origin/main")]}
    except Exception as exc:  # 저장소가 없거나 읽기 실패
        return {"error": str(exc)[:120]}


_s2_cache: dict = {}
_s2_lock = threading.Lock()


def s2_links(refresh: bool = False) -> dict:
    """시나리오 2 카드의 링크: Proxy VM(Azure Portal)과 비교 대상 현황 문서(Wiki 는 이 문서 하나).
    az·Azure DevOps 호출이 몇 초 걸려 5분 동안 캐시한다(refresh 면 다시 읽음)."""
    with _s2_lock:
        if not refresh and _s2_cache.get("at", 0) > time.time() - 300:
            return _s2_cache["data"]
        sys.path.insert(0, str(PROJECT_ROOT / "proxy_tools"))
        from urllib.parse import quote
        import proxy_status_sync as ps
        viewer = os.environ.get("WIKI_VIEWER_URL") or site_url("wiki.local", 8765)[2]
        try:
            from proxy_policy_publish import DevOps
            dv = DevOps()
        except Exception:  # PAT 없음 등: 로컬 뷰어 링크만
            dv = None

        def doc(page: Path) -> dict:
            rel = page.relative_to(ps.WIKI_REPO).with_suffix("").as_posix()
            devops = ""
            if dv:
                try:  # 원격에 올라간 문서일 때만 Azure DevOps Wiki 링크
                    if ps.git("ls-tree", "-r", "--name-only", "@{u}", "--", page.relative_to(ps.WIKI_REPO).as_posix()):
                        devops = dv.wiki_url(page)
                except Exception:
                    pass
            return {"title": page.stem.replace("-", " "), "path": rel, "local": f"{viewer}/?path={quote(rel)}", "devops": devops}

        # Azure 링크는 Portal 의 VM 목록 화면(Compute infrastructure > Virtual machines). 옆에 Proxy VM 상태 요약만 붙인다.
        data: dict = {"status_page": doc(ps.WIKI_PAGE), "vms": [], "error": None,
                      "portal_vms": "https://portal.azure.com/#view/Microsoft_Azure_ComputeHub/ComputeHubMenuBlade/~/virtualMachinesBrowse"}
        started = time.perf_counter()
        try:
            data["vms"] = [{"name": v["name"], "state": v["state"]} for v in ps.proxy_vms()]
        except Exception as exc:
            data["error"] = f"Azure VM 조회 실패: {exc}"[:200]
        # 작업(job)이 아니라 허브 안에서 바로 읽으므로, '새로 읽기'를 눌렀을 때 무엇을 읽었는지 시나리오 2 로그에 남긴다
        # (탭을 열 때마다 남기면 실행 로그 끝이 이 줄들로 가려진다)
        sp = data["status_page"]
        lines = [f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} Azure VM·Wiki 링크 " + ("다시 읽기 (허브 '새로 읽기')" if refresh else "읽기") + " =====",
                 f"az vm list -g {ps.RESOURCE_GROUP}: " + (data["error"] or f"Proxy VM {len(data['vms'])}대 ("
                 + ", ".join(f"{v['name']} {v['state']}" for v in data["vms"]) + ")") + f" · {time.perf_counter() - started:.1f}초",
                 f"Wiki 현황 문서: {sp['path']} (Azure DevOps Wiki: {'있음' if sp['devops'] else '없음, 로컬 전용'})"]
        try:
            if not refresh:
                raise OSError("탭을 열 때 읽은 것은 남기지 않음")
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / "scenario2.log", "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass
        _s2_cache.update(at=time.time(), data=data)
        return data


APPROVAL_URL = site_url("approval.local", 8771)[2]  # teams_tools/approval_local.py 의 승인 서버


GUIDE_CACHE = DATA_DIR / "guide_cache.json"  # wiki_search.py 의 가이드 캐시(요청마다 파일을 읽으므로 지우면 바로 반영)


def clear_guide_cache() -> dict:
    """시나리오 1 가이드 캐시를 비운다. 실수에 대비해 직전 내용은 guide_cache.json.bak 으로 남긴다."""
    if not GUIDE_CACHE.is_file():
        return {"removed": 0}
    try:
        count = len(json.loads(GUIDE_CACHE.read_text(encoding="utf-8")))
    except ValueError:
        count = 0
    try:
        GUIDE_CACHE.replace(GUIDE_CACHE.with_name(GUIDE_CACHE.name + ".bak"))
    except OSError as exc:
        return {"error": str(exc)}
    return {"removed": count}


def approval_waiting(jobs: dict) -> str | None:
    """시나리오 2·3 중 지금 승인을 기다리는 것(승인 서버가 떠 있고 아직 결정 전). 없으면 None."""
    running = [k for k in ("scenario2", "scenario3") if jobs.get(k, {}).get("state") == "running"]
    if not running or not approval_up():
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(APPROVAL_URL + "/status", timeout=2) as res:
            state = json.loads(res.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return running[0] if state.get("decision") is None else None


def approval_decide(answer: str) -> dict:
    """허브 화면의 승인 버튼: 승인 서버에 결정을 넘긴다. Teams 버튼과 같은 곳이라 먼저 누른 쪽만 반영된다."""
    import urllib.error
    import urllib.request
    if answer not in ("yes", "no"):
        return {"error": "answer 는 yes|no"}
    req = urllib.request.Request(APPROVAL_URL + "/decide", data=json.dumps({"answer": answer, "exclude": []}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return {"ok": True}
    except urllib.error.HTTPError as exc:
        return {"error": "이미 처리된 승인 요청입니다(Teams 에서 먼저 눌렀거나 시간 초과)." if exc.code == 409 else f"승인 서버 오류 {exc.code}"}
    except OSError as exc:
        return {"error": f"승인 서버에 연결하지 못했습니다: {exc}"}


def approval_up() -> bool:
    import socket
    ip = APPROVAL_URL.split("//", 1)[1].split(":")[0]
    port = int(APPROVAL_URL.rsplit(":", 1)[1]) if APPROVAL_URL.count(":") == 2 else 80
    try:
        with socket.create_connection((hosts_ip(ip), port), timeout=1):
            return True
    except OSError:
        return False


def hosts_ip(name: str) -> str:
    from common.server import hosts_entries
    return hosts_entries().get(name.lower(), name)


def approval_record_page(token: str) -> bytes:
    """승인 서버가 닫힌 뒤 카드 버튼을 누르면 보여 줄 안내. data/approvals/ 의 기록으로 결정·처리 결과를 보여 준다."""
    import html as h
    rec = None
    for f in sorted((DATA_DIR / "approvals").glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if token and d.get("token") == token:
            rec = d
            break
    if rec:
        s = rec.get("state") or {}
        said = {"yes": "Yes(승인)", "no": "No(반영 안 함)", "timeout": "시간 초과(반영 안 함)"}.get(s.get("decision"), "결정 없음")
        icon = {"ok": "✓", "fail": "✗", "run": "↻", "info": "·"}
        steps = "".join(f"<li>{icon.get(x.get('state'), '·')} {h.escape(x.get('text', ''))}</li>" for x in s.get("steps") or [])
        links = " · ".join(f'<a href="{h.escape(u)}" target="_blank" rel="noopener">{h.escape(l)}</a>'
                           for l, u in s.get("links") or [] if u)
        body = (f"<h1>{h.escape(rec.get('title', ''))}</h1><p class='msg'>이미 끝난 승인 요청입니다. 결정: <b>{said}</b>"
                f" ({h.escape((rec.get('closed_at') or '').replace('T', ' '))})</p>"
                + (f"<p>{h.escape(s.get('message', ''))}</p>" if s.get("message") else "")
                + (f"<ul>{steps}</ul>" if steps else "") + (f"<p>{links}</p>" if links else "")
                + "<p class='muted'>다시 처리하지 않습니다. 결과는 Teams 결과 카드에도 있습니다.</p>")
    else:
        body = ("<h1>승인 요청</h1><p class='msg'>이미 끝났거나 만료된 승인 요청입니다(승인 서버가 닫혀 있음).</p>"
                "<p class='muted'>처리 결과는 Teams 결과 카드를 확인하세요. 새로 승인하려면 허브에서 시나리오를 다시 실행합니다.</p>")
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>승인 요청</title>"
            f"<style>body{{font:14px/1.6 'Segoe UI','Malgun Gothic',sans-serif;background:#f5f5f5;margin:0}}"
            f".c{{max-width:560px;margin:40px auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px #0002;padding:18px 22px}}"
            f"h1{{font-size:17px}}.msg{{background:#eef2ff;color:#3730a3;padding:10px 12px;border-radius:6px}}"
            f".muted{{color:#666;font-size:13px}}ul{{padding-left:4px;list-style:none}}</style></head>"
            f"<body><div class='c'>{body}</div></body></html>").encode("utf-8")


def job_status() -> dict:
    out = {}
    for job, spec in JOBS.items():
        r = RUNNING.get(job)
        if not r:
            out[job] = {"title": spec["title"], "state": "idle"}
            continue
        code = r["proc"].poll()
        if code is not None and r["finished"] is None:
            r["finished"], r["code"] = datetime.now().isoformat(timespec="seconds"), code
            log(f"[{job}] 작업 종료 PID {r['pid']} → 종료 코드 {code}")
        out[job] = {"title": spec["title"], "state": "running" if code is None else "finished", "pid": r["pid"],
                    "started": r["started"], "finished": r["finished"], "code": r["code"], "args": r["args"]}
    return out


_JOB_START = re.compile(r"===== \S+ \S+ 시작: (.*) =====")


def restore_needed(key: str) -> dict:
    """로그를 처음부터 따라가며 지금 원상 복구가 필요한지 판단한다(허브 '원상 복구' 버튼을 켜고 끄는 데 씀).
    시나리오 2: 테스트 환경 준비 성공 → 필요. 원상 복구 성공, 실행으로 Wiki 반영(wiki_update), 'Wiki 와 Azure 가 같음' → 불필요.
    시나리오 3: 장애 모의 훈련 성공 → 필요. 원상 복구 성공, VM 배포 파이프라인 성공(수정 PR 병합 후 배포) → 불필요.
    판단할 기록이 없으면 불필요로 본다."""
    log = LOG_DIR / f"{key}.log"
    if not log.is_file():
        return {"need": False, "why": "기록 없음"}
    with open(log, "rb") as f:
        f.seek(max(0, log.stat().st_size - 1024 * 1024))
        rows = f.read().decode("utf-8", errors="replace").splitlines()
    need, why, pending, at = False, "기록 없음", None, None
    stamp = lambda line: (m.group(0).replace(" ", "T") if (m := re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line)) else None)
    for r in rows:
        m = _JOB_START.match(r)
        if m:
            cmd = m.group(1)
            if key == "scenario2" and "setup_env.py" in cmd:  # --servers(서버 환경 점검)는 문서를 바꾸지 않는다
                pending = "restore" if "--restore" in cmd else None if "--servers" in cmd else "setup"
            elif key == "scenario3" and "proxy_fault_inject.py" in cmd:
                pending = "restore" if " restore" in cmd else "setup" if " apply" in cmd else None
            else:
                pending = None
        elif "===== 종료: 코드 0," in r and pending:
            need = pending == "setup"
            at = stamp(r)
            why = {"scenario2": {"setup": "불일치 모의 훈련 후 아직 반영 전", "restore": "원상 복구 완료"},
                   "scenario3": {"setup": "장애 모의 훈련 후 아직 복구 전", "restore": "원상 복구 완료"}}[key][pending]
            pending = None
        elif key == "scenario2" and ((" wiki_update " in r and "commit=" in r) or "Wiki 와 Azure 가 같음" in r):
            need, why = False, "실행으로 Wiki 가 실제 VM 과 같아짐"
        elif key == "scenario3" and "===== 파이프라인 #" in r and "종료: succeeded" in r:
            need, why, at = False, "VM 배포 파이프라인 성공(수정 반영)", stamp(r)
    return {"need": need, "why": why, "at": at}  # at: 마지막으로 상태가 바뀐(복구·배포·모의 훈련) 시각


_TRACE_LINE = re.compile(r"(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2}:\d{2}) \[([0-9a-f]{6,})\] (.*)")
_THINK = re.compile(r"◆ (\S+(?: \S+)?)\s{2,}(.*)")


def run_plan(key: str, start: int = 0) -> dict | None:
    """로그에서 가장 최근 실행의 계획·사전 점검·단계 진행을 읽어 돌려준다(허브 '실행 계획' 패널).
    common/trace.py 의 plan()·simulate()·step()·replan() 이 남기는 ◆ 줄과 끝 줄(■)을 그대로 해석한다.
    로그 화면은 마지막 300줄만 받으므로, 계획 줄이 밀려나도 보이게 여기서 따로 읽는다."""
    log = LOG_DIR / f"{key}.log"
    if not log.is_file():
        return None
    size = log.stat().st_size
    start = start if 0 < start <= size else 0
    with open(log, "rb") as f:
        f.seek(max(start, size - 512 * 1024))
        rows = f.read().decode("utf-8", errors="replace").splitlines()
    head = next((i for i in range(len(rows) - 1, -1, -1)
                 if "◆ Planning" in rows[i] and "-step plan generated" in rows[i]), None)
    if head is None:
        return None
    # 계획이 있는 실행 뒤에 다른 작업(테스트 환경 준비·원상 복구 등)을 돌렸으면 그 계획은 지금 화면과 맞지 않는다.
    if any(r.startswith("===== ") and " 시작: " in r for r in rows[head:]):
        return None
    parsed = [m.groups() for m in map(_TRACE_LINE.match, rows) if m]
    tid = _TRACE_LINE.match(rows[head]).group(3)
    lines = [(d, t, text) for d, t, i, text in parsed if i == tid]
    plan = {"id": tid, "goal": "", "started": "", "steps": [], "checks": [], "sim": "", "status": "running",
            "ended": "", "summary": ""}
    steps = plan["steps"]
    cur = None

    def at(d, t):
        return f"{d} {t}" if d else t

    for d, t, text in lines:
        if text.startswith("▶ "):
            plan["started"] = at(d, t)
            continue
        if text.startswith("■ "):
            parts = text[2:].split()
            plan["status"] = parts[1] if len(parts) > 1 else "ok"
            plan["ended"] = at(d, t)
            plan["summary"] = text.split(" | ")[0][2:]
            for s in steps:
                if s["status"] == "running":
                    s["status"] = "done" if plan["status"] == "ok" else "error"
                    s["ended"] = at(d, t)
            continue
        if text.startswith("✗ ") and cur is not None:
            m = re.match(r"✗\s+\d+\s+(\S+)", text)
            if m and m.group(1) == cur["name"]:
                cur["status"], cur["ended"] = "error", at(d, t)
            continue
        m = _THINK.match(text)
        if not m:
            continue
        kind, body = m.group(1), m.group(2)
        if kind == "Planning":
            g = re.match(r"\d+-step plan generated · 목표: (.*)", body)
            if g:
                plan["goal"] = g.group(1)
            else:
                s = re.match(r"\s*(\d+)\.\s+(\S+)\s+(.*)", body)
                if s:
                    steps.append({"name": s.group(2), "desc": s.group(3).strip(), "status": "planned",
                                  "started": "", "ended": "", "note": ""})
        elif kind == "Simulation":
            c = re.match(r"\s*([✓△])\s+(.+?)\s{2,}(.*)", body)
            if c:
                plan["checks"].append({"ok": c.group(1) == "✓", "name": c.group(2).strip(), "note": c.group(3).strip()})
            elif body.startswith("시뮬레이션 결과:"):
                plan["sim"] = body.split(":", 1)[1].strip()
        elif kind.startswith("Step"):
            s = re.match(r"\+?\s*(\S+) — (.*)", body)
            if not s:
                continue
            name = s.group(1)
            for p in steps:
                if p["status"] == "running":
                    p["status"], p["ended"] = "done", at(d, t)
            cur = next((p for p in steps if p["name"] == name), None)
            if cur is None:  # 재계획으로 끼어든 단계
                cur = {"name": name, "desc": s.group(2), "status": "planned", "started": "", "ended": "", "note": ""}
                steps.append(cur)
            cur["desc"] = s.group(2).strip() or cur["desc"]  # 실행할 때 설명이 바뀌면(예: 저장된 가이드 사용) 그 설명으로
            cur["status"], cur["started"] = "running", at(d, t)
        elif kind == "Replan":
            rest = body.split("남은 단계", 1)[-1]
            names = rest.split(":", 1)[1].split(" → ") if ":" in rest else []
            names = [n.strip() for n in names]
            for p in steps:
                if p["status"] == "planned" and p["name"] not in names:
                    p["status"] = "skipped"
            for n in names:
                if not any(p["name"] == n for p in steps):
                    steps.append({"name": n, "desc": "", "status": "planned", "started": "", "ended": "", "note": ""})
        elif kind in ("Observation", "Decision") and cur is not None:
            cur["note"] = body
    return plan


def log_tail(key: str, lines: int = 300, start: int = 0) -> tuple[str, int]:
    """(마지막 lines 줄, 파일 크기). start 를 주면 그 바이트 위치 뒤만 본다(화면의 '지우기' 이후).
    파일이 2MB 를 넘겨 새로 시작했으면(크기 < start) 처음부터 본다."""
    log = LOG_DIR / f"{key}.log"
    if not log.is_file():
        return ("(로그 없음: 작업을 실행하면 남습니다)" if key in JOB_LOGS
                else "(로그 없음: 허브에서 시작한 서버만 로그가 남습니다)"), 0
    size = log.stat().st_size
    start = start if 0 < start <= size else 0
    with open(log, "rb") as f:
        f.seek(max(start, size - 128 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    if start and not text.strip():
        return "(지운 뒤 새 로그가 없습니다. '전체 보기'로 이전 로그를 볼 수 있습니다)", size
    return "\n".join(text.splitlines()[-lines:]), size


# ---------------------------------------------------------------- web

class Handler(BaseHTTPRequestHandler):
    def send_body(self, body: bytes, ctype: str, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status: int = 200):
        self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def trusted(self) -> bool:
        # 다른 사이트가 브라우저를 통해 서버를 끄지 못하게: 우리 호스트 이름 + 단순 요청으로는 못 붙이는 헤더를 요구한다.
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in {HUB_SITE, "127.0.0.1", "localhost"} and self.headers.get("X-Hub") == "1"

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_body(PAGE.replace("__TITLE__", HUB_TITLE).replace("__FAVICON__", favicon("A", "#2563eb")).encode("utf-8"),
                           "text/html; charset=utf-8")
        elif url.path == "/api/status":
            try:
                self.send_json({"servers": server_status(), "time": datetime.now().isoformat(timespec="seconds"),
                                "log_start": LOG_START})
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                log(f"상태 조회 실패: {type(exc).__name__}: {exc}")
                self.send_json({"error": str(exc)}, 500)
        elif url.path == "/api/log":
            qs = parse_qs(url.query)
            key = qs.get("key", [""])[0]
            if key not in BY_KEY and key not in JOB_LOGS:
                self.send_error(404)
                return
            start = qs.get("from", ["0"])[0]
            text, size = log_tail(key, start=int(start) if start.isdigit() else 0)
            self.send_body(text.encode("utf-8"), "text/plain; charset=utf-8", headers={"X-Log-Size": str(size)})
        elif url.path == "/api/plan":
            qs = parse_qs(url.query)
            key = qs.get("key", [""])[0]
            if key not in JOB_LOGS and key != "wiki-search":  # 시나리오 1 은 서버 로그에서 가장 최근 질의 1건
                self.send_error(404)
                return
            start = qs.get("from", ["0"])[0]
            body = json.dumps(run_plan(key, start=int(start) if start.isdigit() else 0), ensure_ascii=False)
            self.send_body(body.encode("utf-8"), "application/json; charset=utf-8")
        elif url.path in ("/approval/decide", "/approval/open"):
            # Teams 카드 버튼. 승인 서버가 떠 있으면 그리로 넘기고, 닫혔으면(이미 끝난 요청) 기록으로 안내한다.
            qs = parse_qs(url.query)
            token = qs.get("token", [""])[0]
            if approval_up():
                target = (f"{APPROVAL_URL}/decide?answer={quote(qs.get('answer', [''])[0])}&token={quote(token)}"
                          if url.path == "/approval/decide" else APPROVAL_URL + "/")
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
            else:
                self.send_body(approval_record_page(token), "text/html; charset=utf-8")
        elif url.path == "/api/s2result":  # 시나리오 2 마지막 비교 결과(proxy_status_sync.py 가 남김)
            path = PROJECT_ROOT / "data" / "scenario2" / "latest.json"
            try:
                self.send_json(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})
            except (OSError, ValueError) as exc:
                self.send_json({"error": str(exc)})
        elif url.path == "/api/health":  # 시나리오 3 Health Check 마지막 결과(proxy_health.py 가 남김)
            path = PROJECT_ROOT / "data" / "health" / "latest.json"
            try:
                self.send_json(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})
            except (OSError, ValueError) as exc:
                self.send_json({"error": str(exc)})
        elif url.path == "/api/s2links":
            try:
                self.send_json(s2_links(parse_qs(url.query).get("refresh") == ["1"]))
            except Exception as exc:
                log(f"시나리오 2 링크 조회 실패: {type(exc).__name__}: {exc}")
                self.send_json({"error": str(exc)}, 500)
        elif url.path == "/api/jobs":
            jobs = job_status()
            # 시나리오 3 이 승인을 기다리는 동안 승인 서버 주소를 알려 화면이 승인 창을 띄우게 한다
            waiting = approval_waiting(jobs)
            self.send_json({**jobs, "_fault": fault_state(), "_approval": APPROVAL_URL + "/" if waiting else None,
                            "_approval_wait": waiting,
                            "_pipeline": PIPELINE_STATE or None,
                            "_restore": {k: restore_needed(k) for k in ("scenario2", "scenario3")}})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/action", "/api/job", "/api/guide-cache/clear", "/api/approval"):
            self.send_error(404)
            return
        client = self.client_address[0]
        if not self.trusted():
            log(f"요청 거부(403) {client}: Host={self.headers.get('Host')} Origin={self.headers.get('Origin')}")
            self.send_json({"error": "forbidden"}, 403)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            log(f"요청 거부(400) {client}: JSON 형식 오류")
            self.send_json({"error": "bad json"}, 400)
            return
        if path == "/api/job":
            job = body.get("job")
            if job not in JOBS:
                self.send_json({"error": "bad job"}, 400)
                return
            result = run_job(job, body.get("options") or {})
            log(f"요청 {client}: 작업 {job} {body.get('options') or {}} → {result}")
            self.send_json(result, 409 if "error" in result else 200)
            return
        if path == "/api/approval":
            result = approval_decide(str(body.get("answer")))
            log(f"요청 {client}: 허브 화면 승인 {body.get('answer')} → {result}")
            self.send_json(result, 409 if "error" in result else 200)
            return
        if path == "/api/guide-cache/clear":
            result = clear_guide_cache()
            log(f"요청 {client}: 가이드 캐시 초기화 → {result}")
            self.send_json(result, 500 if "error" in result else 200)
            return
        key, action = body.get("key"), body.get("action")
        if action not in ("start", "stop", "restart") or (key != "all" and key not in BY_KEY):
            log(f"요청 거부(400) {client}: key={key} action={action}")
            self.send_json({"error": "bad request"}, 400)
            return
        accepted = dispatch(key, action)
        log(f"요청 {client}: {action} {key} → 처리 {', '.join(accepted) or '없음'}")
        self.send_json({"accepted": accepted})

    def log_message(self, *_):
        pass


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
__FAVICON__<title>__TITLE__</title>
<style>
:root{--bg:#eef2f7;--panel:#fff;--ink:#1d2939;--muted:#667085;--line:#d9e0ea;--top:#13213a;--blue:#2563eb;
--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--off:#98a2b3;--soft:#f5f7fb;--code:#101827;--codeink:#e6edf6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1520;--panel:#171f2c;--ink:#e4e8ef;--muted:#98a2b3;--line:#2a3445;
--top:#0b111b;--blue:#6ea0ff;--soft:#1d2636;--code:#0b111b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI","Malgun Gothic",sans-serif}
header{background:var(--top);color:#fff;display:flex;align-items:center;gap:12px;padding:12px 24px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:700}header .time{color:#aebbd0;font-size:12px;margin-right:auto}header .time.stale{color:#fca5a5;font-weight:700}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--blue)}button:disabled{opacity:.45;cursor:default}
header button{background:#22324f;border-color:#34476b;color:#fff}
main{padding:20px 24px;display:grid;gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse}th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--soft)}tr:last-child td{border-bottom:0}
tr.sel td{background:var(--soft)}
.state{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}.dot{width:9px;height:9px;border-radius:50%;background:var(--off)}
.running .dot{background:var(--ok)}.starting .dot{background:var(--warn)}.stopped .dot{background:var(--off)}
.title{font-weight:600}.script{color:var(--muted);font-size:12px}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.warn{color:var(--bad);font-size:12px}.muted{color:var(--muted);font-size:12px}.acts{display:flex;gap:6px;flex-wrap:wrap}
.tabs{display:flex;gap:4px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--soft)}
.tabs button.on{background:var(--blue);border-color:var(--blue);color:#fff}
button.logon{background:color-mix(in srgb,var(--blue) 14%,var(--panel));border-color:var(--blue);color:var(--blue);font-weight:700;box-shadow:inset 0 0 0 1px var(--blue)}.tabs .name{margin-left:8px;font-weight:600}
pre{margin:0;background:var(--code);color:var(--codeink);padding:12px 14px;height:420px;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap}
.err{color:var(--bad);padding:12px}
[hidden]{display:none!important}
.scwrap{display:grid;grid-template-columns:minmax(220px,260px) minmax(0,1fr);gap:16px;align-items:start}.scgrid{display:grid;gap:16px;align-content:start}.scright{position:sticky;top:20px;display:flex;flex-direction:column;gap:16px;height:calc(100vh - 40px);min-height:520px}.brand{background:var(--top);color:#fff;border-radius:10px;padding:16px 18px;display:grid;gap:2px}.brand h1{font-size:17px;margin:0;font-weight:700;line-height:1.35}.brand .time:empty{display:none}.scview{display:flex;flex-direction:column;flex:0 1 auto;min-height:120px}#s1view{flex:44 1 0;min-height:90px}#s1view iframe{flex:1;border:0;width:100%;background:#fff}.sc button.s1btn{font-size:15px;font-weight:700;padding:10px 14px;width:100%}#s1state:empty{display:none}.apbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border:2px solid #ea580c;background:#fff7ed;border-radius:8px;padding:8px 12px;font-size:13px}.apbar b{color:#c2410c}.apbar .muted{margin-right:auto}.drilltag{display:inline-block;background:#f59e0b;color:#fff;font-weight:700;font-size:12px;border-radius:6px;padding:0 7px;margin-right:4px}.s1desc{margin:0;padding:10px 18px;color:var(--muted);font-size:13px;flex:none;border-bottom:1px solid var(--line)}.drill{display:inline-flex;align-items:stretch;border:1px solid #f59e0b;border-radius:8px;overflow:hidden;background:#fffbeb;margin-right:4px}.drill button{border:0;border-radius:0;background:#f59e0b;color:#fff;font-weight:700;padding:5px 10px}.drill button:hover:not(:disabled),button.drillbtn:hover:not(:disabled){background:#d97706}button.drillbtn{background:#f59e0b;border-color:#f59e0b;color:#fff;font-weight:700}.drill select{font:inherit;font-size:13px;border:0;border-left:1px solid #f59e0b;background:transparent;color:var(--ink);padding:4px 6px;max-width:220px;cursor:pointer}#s2result table,#s3health table{border-collapse:collapse;font-size:13px;width:100%;max-width:820px}#s2result th,#s2result td,#s3health th,#s3health td{padding:5px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}#s2result th,#s3health th{color:var(--muted);font-weight:600;font-size:12px}#s2result .hok,#s3health .hok{color:var(--ok);font-weight:700}#s2result .hfail,#s3health .hfail{color:var(--bad);font-weight:700}#s2result .hskip,#s3health .hskip{color:var(--muted)}#s2result .hsum,#s3health .hsum{font-size:13px;margin:2px 0 6px}#s2result .hbad,#s3health .hbad{font-size:12px;color:var(--bad);margin-top:6px;display:grid;gap:2px;white-space:normal}.splitter{display:none;flex:none;height:12px;margin:-14px 0;cursor:row-resize;align-items:center;justify-content:center;touch-action:none;z-index:1}.splitter i{width:56px;height:4px;border-radius:2px;background:var(--line)}.splitter:hover i,body.dragging .splitter i{background:var(--blue)}body:not(.nosel):not(.nolog):not(.logmode) .splitter{display:flex}body.dragging{cursor:row-resize;user-select:none}body.dragging iframe{pointer-events:none}.scview>.sc{flex:1 1 auto;min-height:0;overflow:auto}.scgrid button.s1btn.on{background:var(--blue);border-color:var(--blue);color:#fff}.s1empty{flex:1;display:grid;place-items:center;color:var(--muted);font-size:28px;font-weight:700;letter-spacing:-.5px;text-align:center;padding:24px;opacity:.55}body:not(.nosel) .s1empty{display:none}.sclog{display:flex;flex-direction:column;flex:1;min-height:auto}body.nosel:not(.logmode) .sclog,body.nolog:not(.logmode) .sclog{display:none}.logtg{display:inline-flex;align-items:center;gap:5px;margin-left:6px;padding:5px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;cursor:pointer;user-select:none;background:#fff}.logtg input{margin:0;cursor:pointer}body.sel1:not(.logmode):not(.splitset) .sclog{flex:0 0 auto}body.sel1:not(.logmode):not(.splitset) #s1view{flex:1 1 0}body.sel1:not(.logmode):not(.splitset) .sclog #slog{flex:none;height:168px!important;min-height:0}body.sel1.splitset:not(.logmode) .sclog{flex:1 1 0}body.logmode header,body.logmode .scgrid,body.logmode .scview,body.logmode .s1empty{display:none!important}body.logmode main{padding:0}body.logmode .scwrap{display:block}body.logmode .scright{position:static;height:100vh;min-height:0}body.logmode .sclog{border-radius:0;border:0}.sclog #slog{flex:1 1 0;height:auto;min-height:168px}.sclog #splan{flex:none}@media(max-width:1000px){.scwrap{grid-template-columns:1fr}.scright{position:static;height:auto}#s1view iframe{height:640px}.sclog #slog{height:420px}}
.sc{padding:16px 18px;display:grid;gap:10px;align-content:start}.sc h2{font-size:15px;margin:0}.sc p{margin:0;color:var(--muted);font-size:13px}
.sc.s3{align-self:start;padding:12px 18px;gap:7px}.sc.s3 p{line-height:1.35}.sc.s3 .row{gap:5px}.sc.s3 .row button{white-space:nowrap;padding:4px 6px}
.sc input[type=text]{font:inherit;border:1px solid var(--line);border-radius:6px;padding:6px 10px;background:var(--panel);color:var(--ink);width:100%}
.sc .opts{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}.sc .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sc button.main{background:var(--blue);border-color:var(--blue);color:#fff}.jobstate{font-size:13px}
.s2links{font-size:13px;display:grid;gap:6px}.s2links a{color:var(--blue);text-decoration:none}
.s2links .head{display:flex;gap:12px;align-items:center;flex-wrap:nowrap}.s2links .head>span{white-space:nowrap}.s2links .head>span.az{min-width:0;overflow:hidden;text-overflow:ellipsis}.s2links .head button{padding:2px 8px;flex:none}.sc>*,.s2links>*{min-width:0}
@media(max-width:1250px){.s2links .head{flex-wrap:wrap}}@media(max-width:1400px){.sc.s3 .row{flex-wrap:wrap!important}}.s2links a,.s2links button{white-space:nowrap}
.jobstate.running{color:var(--warn)}.jobstate.ok{color:var(--ok)}.jobstate.fail{color:var(--bad)}
#splan{border-bottom:1px solid var(--line);padding:8px 14px;font-size:13px;display:grid;gap:5px}
#splan .ph{display:flex;gap:10px;align-items:center;flex-wrap:wrap}#splan .ph b{font-size:13px}
#splan .ph button{margin-left:auto;padding:1px 8px;font-size:12px}
#splan .chk{display:flex;gap:6px;flex-wrap:wrap}#splan .chip{border:1px solid var(--line);border-radius:10px;padding:0 8px;font-size:12px;background:var(--soft)}
#splan .chip.no{color:var(--warn);border-color:var(--warn)}
#splan ol{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:auto auto auto minmax(0,max-content) minmax(0,1fr);column-gap:10px;row-gap:1px}
#splan li{display:contents}#splan li>span{padding:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#splan li>span.ds.full{grid-column:span 2}
#splan .nm{font-family:Consolas,monospace;font-size:12px}#splan .nt{color:var(--muted);font-size:12px;text-align:right}
#splan li.planned>span{color:var(--muted)}#splan li.skipped>span{color:var(--off);text-decoration:line-through}
#splan li.running>span{color:var(--warn);font-weight:700}#splan li.done .ic{color:var(--ok)}#splan li.error>span{color:var(--bad)}
@media(max-width:820px){main{padding:12px}}
</style></head><body>
<main id="pane-scenarios">
<div id="err" class="err card" hidden></div>
<div class="scwrap">
<div class="scgrid">
<div class="brand"><h1>__TITLE__</h1><span class="time" id="time"></span></div>
<div class="card sc"><button class="s1btn" id="s1run" data-sc="1" onclick="runS1()" title="시나리오 1: Wiki 검색 화면을 오른쪽에 엽니다">Wiki 기반 가이드 생성</button>
<span class="jobstate" id="s1state"></span></div>
<div class="card sc"><button class="s1btn" id="s2open" data-sc="2" onclick="pickSc(2)" title="시나리오 2: Wiki 현황 문서와 Azure VM 비교·현행화">프록시 현황 점검</button></div>
<div class="card sc"><button class="s1btn" id="s3open" data-sc="3" onclick="pickSc(3)" title="시나리오 3: 프록시 로그 장애 감지와 자동 조치">프록시 Health Check</button></div>
</div>
<div class="scright">
<div class="card scview" id="s1view" hidden><div class="tabs"><span class="name" title="질의 입력·유형 선택·미리보기·수정 요청·Wiki 등록은 아래 화면에서 합니다. 서버 로그는 맨 아래에 나옵니다.">Wiki 기반 가이드 생성</span><span class="jobstate" id="s1cache" style="margin-left:auto"></span><button onclick="if(!openTop(this.dataset.url,'devops'))window.open(this.dataset.url,'_blank','noopener')" data-url="https://dev.azure.com/ai-master/AI%20Master/_wiki/wikis/AI-Master.wiki/1/AI-Master-%ED%94%84%EB%A1%9C%EC%A0%9D%ED%8A%B8" title="Azure DevOps Wiki(AI-Master 프로젝트 첫 페이지)를 새 창으로 엽니다">Azure Wiki 바로 가기</button><button onclick="clearGuideCache()" title="저장된 가이드(data/guide_cache.json)를 모두 지웁니다. 다음 질의부터 LLM이 새로 만듭니다. 직전 내용은 guide_cache.json.bak 으로 남습니다.">캐시 초기화</button><label class="logtg" title="로그를 화면 아래에 보이거나 숨깁니다"><input type="checkbox" class="logon-cb" checked onchange="logToggle(this.checked)"> 로그 보기</label></div><p class="s1desc">Wiki 문서를 의미 검색해 근거를 찾고, AI가 근거와 출처를 단 가이드를 만듭니다. 질의 입력부터 미리보기·수정 요청·Wiki 등록까지 아래 화면에서 합니다.</p><iframe id="s1frame" title="Wiki 검색" allow="clipboard-read; clipboard-write"></iframe></div>
<div class="card scview" id="s2view" hidden><div class="tabs"><span class="name">프록시 현황 점검</span><span style="margin-left:auto"></span><button id="s2servers" onclick="runJob('scenario2-servers')" title="test/scenario2/setup_env.py --servers: 꺼진 Proxy VM 을 켜고 az 로그인·VM 3대 이상·Azure DevOps PAT·승인 화면을 점검(문서·정답은 그대로)">서버 환경 점검</button><button id="s2setup" class="drillbtn" onclick="runJob('scenario2-setup')" title="test/scenario2/setup_env.py --seed: 실제 VM 으로 일부러 틀린 '현행화 전' 표와 정답을 만들어 Wiki 테스트 문서에 씀(VM 은 켜지 않음)">불일치 모의 훈련</button><button id="s2restore" onclick="restoreJob('scenario2-restore','테스트 문서를 실제 Azure VM 과 같은 정상 표로 되돌립니다(VM·정답은 그대로).')" disabled>원상 복구</button><label class="logtg" title="로그를 화면 아래에 보이거나 숨깁니다"><input type="checkbox" class="logon-cb" checked onchange="logToggle(this.checked)"> 로그 보기</label></div>
<div class="sc"><p>Wiki 현황 문서와 Azure VM을 비교하고, 차이가 있으면 승인(이 화면 또는 Teams) 후 Task·Wiki를 반영합니다. 점검 배치가 주기적으로 돌며 차이를 Teams로 알린다고 가정합니다(배치는 구성하지 않음).</p><div class="s2links" id="s2links"><span class="muted">Azure VM·Wiki 링크 불러오는 중…</span></div>
<div class="row"><button class="main" id="s2run" onclick="runJob('scenario2')">실행</button><span class="jobstate" id="s2state"></span></div>
<div id="s2ap"></div><div id="s2result"></div>
</div></div>
<div class="card scview" id="s3view" hidden><div class="tabs"><span class="name">프록시 Health Check</span><span style="margin-left:auto"></span><span class="drill" title="장애 모의 훈련: 고른 종류의 잘못된 설정을 배포합니다"><button id="s3inject" onclick="if(confirm('proxy-config 에 잘못된 설정을 커밋·push 하고 wjy-proxy01 에 배포합니다. 실제로 접속이 막힙니다. 계속할까요?'))runJob('scenario3-fault',{choice:document.getElementById('s3fault').value})" title="고른 종류의 잘못된 설정을 일부러 배포만 함(점검은 Health Check 실행으로) (proxy_fault_inject.py apply)">장애 모의 훈련</button><select id="s3fault" title="일부러 넣을 장애 종류">
<option value="whitelist-typo">허용 도메인 오타 (data → date)</option><option value="client-acl-typo">허용 클라이언트 IP 오타</option>
<option value="bad-dns">잘못된 DNS 서버</option></select></span><button id="s3restore" onclick="restoreJob('scenario3-restore','심어진 장애가 있으면 되돌리는 커밋을 push 하고, wjy-proxy01 에 지금 main 을 다시 배포합니다.')" title="심은 장애를 되돌리고(없으면 그대로) main 을 배포·확인. 수정 PR 을 병합한 뒤에도 이 버튼으로 배포 (proxy_fault_inject.py restore)">원상 복구</button><button id="s3nsg" onclick="if(confirm('Proxy VM NSG 의 허용 IP(SSH·프록시 포트)를 .env PROXY_ALLOWED_CLIENTS 의 IP + 이 PC 의 현재 공인 IP 로 맞춥니다. 그 밖의 예전 IP 는 지웁니다. 계속할까요?'))runJob('nsg-ip')" title="회선이 바뀌어 VM 에 접속이 안 될 때(SSH Connection timed out) 누르세요. Proxy VM 방화벽(NSG) 허용 목록에 이 PC 의 지금 공인 IP 를 넣고 예전 IP 는 지웁니다 (sync_nsg_ip.py)">내 IP 허용</button><button onclick="openServer('proxy-log')" title="프록시 로그 뷰어(proxy-log.local)를 새 창으로 엽니다">프록시 로그</button><label class="logtg" title="로그를 화면 아래에 보이거나 숨깁니다"><input type="checkbox" class="logon-cb" checked onchange="logToggle(this.checked)"> 로그 보기</label></div>
<div class="sc s3"><p>Wiki <b>Proxy Health Check 기준</b> 문서의 허용·차단 목적지로 VM 마다 실제 접속해 봅니다. 이상이 있으면 원인을 분석하고 승인(이 화면 또는 Teams) 후 Task·수정 PR·이력 Wiki를 남깁니다(VM 반영은 PR 병합 뒤 <b>VM 배포</b> 파이프라인).</p>
<div class="row"><button class="main" id="s3run" onclick="runJob('scenario3')" title="Health Check(Wiki 기준 목적지 실제 접속) → 최근 로그 탐지 → 이상이면 원인 분석·Teams 승인 → Task·수정 PR·이력 Wiki (incident_response.py)">Health Check 실행</button></div>
<div id="s3ap"></div><div id="s3health"></div>
<div class="jobstate" id="s3fault-state"></div><div class="jobstate" id="s3pipe"></div>
<div class="jobstate" id="s3state"></div></div></div>
<div class="card s1empty">Azure DevOps 기반 AI 운영 자동화</div>
<div class="splitter" id="split" title="끌어서 위아래 크기 조절 · 두 번 누르면 원래대로"><i></i></div>
<div class="card sclog"><div class="tabs"><span class="name" id="sname">로그를 고르세요</span><span class="muted" style="margin-left:auto" id="shint"></span>
<button onclick="clearLog(pageLog,loadSLog)" title="지금까지의 로그를 화면에서 숨긴다(파일은 그대로)">지우기</button>
<button id="sall" onclick="unclearLog(pageLog,loadSLog)" hidden>전체 보기</button></div>
<div id="splan" hidden></div>
<pre id="slog"></pre></div>
</div>
</div>
</main>
<script>
let servers=[];

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function since(iso){if(!iso)return'';const s=Math.max(0,(Date.now()-new Date(iso))/1000);
  if(s<60)return Math.floor(s)+'초';if(s<3600)return Math.floor(s/60)+'분';if(s<86400)return Math.floor(s/3600)+'시간 '+Math.floor(s%3600/60)+'분';return Math.floor(s/86400)+'일'}
async function act(key,action){
  const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Hub':'1'},body:JSON.stringify({key,action})});
  if(!r.ok){alert('요청 실패: '+r.status);return}setTimeout(refresh,300)}
// 머리글 시각: 서버 상태를 마지막으로 받은 때. 10초 넘게 못 받으면 허브가 죽었거나 멈춘 것 → 빨간 '허브 응답 없음'
// (그동안 표의 '실행 중'은 옛 정보). 요청이 매달려도 알 수 있게 응답과 따로 1초마다 본다.
let lastOk=Date.now(),lastOkText='';
function staleCheck(){const el=document.getElementById('time');const stale=Date.now()-lastOk>10000;el.classList.toggle('stale',stale);
  el.textContent=stale?'허브 응답 없음'+(lastOkText?' (마지막 확인 '+lastOkText+')':''):''}
async function refresh(){
  try{const r=await fetch('/api/status');const d=await r.json();if(d.error)throw new Error(d.error);
    servers=d.servers;lastOk=Date.now();lastOkText=d.time.slice(11);staleCheck();applyLogStart(d.log_start);
    document.getElementById('err').hidden=true}
  catch(e){const el=document.getElementById('err');el.hidden=false;el.textContent='상태를 읽지 못했습니다: '+e.message}}
// 로그 '지우기': 파일은 그대로 두고, 누른 시점의 파일 크기를 기억해 그 뒤만 보여 준다(브라우저에 저장, 새로고침해도 유지).
const CLEARED_KEY='hub.logCleared';let cleared={};try{cleared=JSON.parse(localStorage.getItem(CLEARED_KEY)||'{}')}catch(e){}
const logSize={};
// 허브를 새로 켠 뒤 처음 열면 모든 로그를 허브를 켠 시점으로 '지우기'한다. 새로고침해도 다시 지우지 않고, 전체 보기는 유지된다.
function applyLogStart(ls){if(!ls||!ls.at)return;let seen='';try{seen=localStorage.getItem('hub.logStartAt')||''}catch(e){}
  if(seen===ls.at)return;cleared={};Object.entries(ls.sizes||{}).forEach(([k,v])=>{if(v>0)cleared[k]=v});saveCleared();
  try{localStorage.setItem('hub.logStartAt',ls.at)}catch(e){}if(pageLog)loadSLog()}
function saveCleared(){try{localStorage.setItem(CLEARED_KEY,JSON.stringify(cleared))}catch(e){}}
async function fetchLog(key){const r=await fetch('/api/log?key='+encodeURIComponent(key)+'&from='+(cleared[key]||0));
  logSize[key]=+(r.headers.get('X-Log-Size')||0);return r.text()}
function clearLog(key,reload){if(!key)return;cleared[key]=logSize[key]||0;saveCleared();reload()}
function unclearLog(key,reload){delete cleared[key];saveCleared();reload()}
// 시나리오 2: Wiki 현황 문서와 Azure Portal VM 목록, 한 줄. 서버가 5분 캐시, '새로 읽기'는 즉시.
async function loadS2Links(refresh){const box=document.getElementById('s2links');if(refresh)box.innerHTML='<span class="muted">다시 읽는 중…</span>';
  let d;try{d=await (await fetch('/api/s2links'+(refresh?'?refresh=1':''))).json()}catch(e){box.innerHTML='<span class="muted">링크를 불러오지 못했습니다.</span>';return}
  // 링크도 화면 가로 전체 × 위쪽 40% 창으로 연다(openTop). 팝업이 막히면 새 탭으로 연다.
  const a=(u,t,name)=>u?`<a href="${esc(u)}" target="_blank" rel="noopener" onclick="return !openTop(this.href,'${name}')">${esc(t)}</a>`:'';
  const doc=x=>`${a(x.local,x.title,'wiki-viewer')}${x.devops?' · '+a(x.devops,'Azure DevOps','devops'):''}`;
  const sp=d.status_page;
  const up=(d.vms||[]).filter(v=>v.state==='운영').length,n=(d.vms||[]).length;
  box.innerHTML=`<div class="head">${sp?'<span>Wiki: '+doc(sp)+'</span>':''}<span class="az" title="${esc(d.error||"")}">Azure: ${a(d.portal_vms,'Proxy VM 목록','azure-portal')} <span class="muted">${d.error?esc(d.error):n?`${up}/${n}대 실행 중`:'Proxy VM 없음'}</span></span>`
    +`<button class="slog" style="margin-left:auto" onclick="loadS2Links(true)">새로 읽기</button></div>`}
// ---------------------------------------------------------------- 시나리오 실행
let jobs={}, slogKey=null, pageLog=null, approvalShown=null;
function wikiSearch(){return servers.find(s=>s.key==='wiki-search')}
async function clearGuideCache(){if(!confirm('저장된 가이드 캐시를 모두 지웁니다. 같은 질의도 다음부터 LLM이 새로 만듭니다(약 40초). 계속할까요?'))return;
  const el=document.getElementById('s1cache');  // s1state 는 2초마다 서버 주소로 다시 그려져서 따로 둔다
  try{const r=await fetch('/api/guide-cache/clear',{method:'POST',headers:{'Content-Type':'application/json','X-Hub':'1'},body:'{}'});const d=await r.json();
    el.innerHTML=d.error?`<span class="jobstate fail">캐시 초기화 실패: ${esc(d.error)}</span>`:`<span class="jobstate ok">캐시 초기화: 저장된 가이드 ${d.removed}건 삭제</span>`}
  catch(e){el.innerHTML=`<span class="jobstate fail">캐시 초기화 실패: ${esc(String(e))}</span>`}}
function openServer(key){const s=servers.find(x=>x.key===key);
  if(!s||s.state!=='running'){if(confirm((s?s.title:key)+' 서버가 꺼져 있습니다. 지금 시작할까요? 몇 초 뒤 다시 누르세요.'))act(key,'start');return}
  openTop(s.url+'/',key)}
// 화면 가로 전체 × 위쪽 40% 크기의 새 창으로 연다(허브 아래쪽이 보이도록). 같은 name 이면 그 창을 다시 쓴다.
// 팝업이 막히면 null 을 돌려준다(noopener 를 주면 늘 null 이라 대신 opener 를 끊는다).
function openTop(url,name){const w=screen.availWidth,h=Math.round(screen.availHeight*(/^https:\/\/dev\.azure\.com\//.test(url)?0.44:0.4)),l=screen.availLeft||0,t=screen.availTop||0;
  const win=window.open(url,name,`popup,width=${w},height=${h},left=${l},top=${t}`);if(win)win.opener=null;return win}
// 시나리오 1: 오른쪽에 Wiki 검색 화면, 그 아래에 Wiki 검색 서버 로그. 이미 열려 있으면 진행 중인 화면을 그대로 둔다.
// 왼쪽 버튼으로 시나리오를 고르면 오른쪽에 그 화면, 아래에 그 로그. '로그 보기'는 세 화면이 함께 쓰고 열 때마다 켜진 상태로 시작한다.
const SC_LOG={1:['wiki-search','Wiki 검색 서버 로그'],2:['scenario2','프록시 현황 점검 로그'],3:['scenario3','프록시 Health Check 로그']};
let curSc=0;
function pickSc(n){curSc=n;const b=document.body;b.classList.remove('nosel','sel1','sel2','sel3');b.classList.add('sel'+n);
  [1,2,3].forEach(i=>{document.getElementById('s'+i+'view').hidden=i!==n;document.querySelector('.scgrid [data-sc="'+i+'"]').classList.toggle('on',i===n)});
  applySplit();showLogHere(SC_LOG[n][0],SC_LOG[n][1],true);fit()}
function logToggle(on){document.body.classList.toggle('nolog',!on);document.querySelectorAll('.logon-cb').forEach(c=>c.checked=on);applySplit();if(on)setTimeout(fit,0)}
// 위 화면과 로그 사이 조절 막대: 끌면 위 화면 높이를 정하고 시나리오별로 브라우저에 기억(오른쪽 열 높이에 대한 비율). 두 번 누르면 기본값.
const SPLIT_KEY=n=>'hub.split2.'+n;  // 2: 시나리오 1 로그 8줄 기본값으로 바꾸며 예전 저장값을 버림(2026-11-11)
// 시나리오 2·3 은 위 화면이 내용보다 작아지지 않게 한다(안쪽 스크롤바가 생기지 않게). 시나리오 1(Wiki 검색 화면)은 제한 없음.
function contentH(n){if(n===1)return 90;const v=document.getElementById('s'+n+'view');
  const sc=v.querySelector('.sc'),last=sc.lastElementChild;if(!last)return 90;  // 칸 크기가 아니라 마지막 항목의 아래 끝으로 잰다
  const inner=last.getBoundingClientRect().bottom-sc.getBoundingClientRect().top+sc.scrollTop+parseFloat(getComputedStyle(sc).paddingBottom);
  return v.querySelector('.tabs').offsetHeight+Math.ceil(inner)+2}
function applySplit(){if(!curSc)return;const v=document.getElementById('s'+curSc+'view');let f=null;
  if(!document.body.classList.contains('nolog'))try{f=parseFloat(localStorage.getItem(SPLIT_KEY(curSc)))}catch(e){}
  const want=f>0?Math.max(contentH(curSc),Math.round(f*document.querySelector('.scright').clientHeight)):0;
  const flex=want?`0 1 ${want}px`:'';if(v.style.flex!==flex)v.style.flex=flex;document.body.classList.toggle('splitset',!!want)}
(function(){const sp=document.getElementById('split');let y0=0,h0=0,v=null;
  sp.addEventListener('pointerdown',e=>{if(!curSc)return;v=document.getElementById('s'+curSc+'view');y0=e.clientY;h0=v.getBoundingClientRect().height;
    sp.setPointerCapture(e.pointerId);document.body.classList.add('dragging','splitset');e.preventDefault()});
  sp.addEventListener('pointermove',e=>{if(!v)return;const H=document.querySelector('.scright').clientHeight;
    const h=Math.max(contentH(curSc),Math.min(H-150,h0+e.clientY-y0));v.style.flex=`0 0 ${h}px`});
  const end=()=>{if(!v)return;const H=document.querySelector('.scright').clientHeight;
    try{localStorage.setItem(SPLIT_KEY(curSc),String(v.getBoundingClientRect().height/H))}catch(e){}v=null;document.body.classList.remove('dragging');fit()};
  sp.addEventListener('pointerup',end);sp.addEventListener('pointercancel',end);
  sp.addEventListener('dblclick',()=>{try{localStorage.removeItem(SPLIT_KEY(curSc))}catch(e){}applySplit();fit()});
  addEventListener('resize',applySplit)})();
function runS1(){const s=wikiSearch();if(!s||s.state!=='running')return;const f=document.getElementById('s1frame'),u=s.url+'/?embed=1';
  if(f.dataset.src!==u){f.src=u;f.dataset.src=u}pickSc(1)}
// 누르는 즉시 같은 카드의 실행 버튼을 끈다(상태를 다시 읽기 전 두 번째 클릭이 '이미 실행 중' 팝업을 띄우던 문제).
const JOB_BTNS={scenario2:['s2servers','s2setup','s2run','s2restore'],scenario3:['s3inject','s3run','s3restore','s3nsg']};
async function runJob(job,options={}){slogFollow=true;const lk=JOBS_LOG[job];(JOB_BTNS[lk]||[]).forEach(id=>document.getElementById(id).disabled=true);
  pickSc(lk==='scenario3'?3:2);
  let d;try{d=await (await fetch('/api/job',{method:'POST',headers:{'Content-Type':'application/json','X-Hub':'1'},body:JSON.stringify({job,options})})).json()}
  catch(e){d={error:'허브에 요청하지 못했습니다: '+e.message}}
  if(d.error){await refreshJobs();alert(d.error);return}setTimeout(refreshJobs,300)}
async function decide(answer){if(answer==='no'&&!confirm('반영하지 않고 기록만 남깁니다. 계속할까요?'))return;
  let d;try{d=await (await fetch('/api/approval',{method:'POST',headers:{'Content-Type':'application/json','X-Hub':'1'},body:JSON.stringify({answer})})).json()}
  catch(e){d={error:'허브에 요청하지 못했습니다: '+e.message}}
  if(d.error)alert(d.error);setTimeout(refreshJobs,300)}
// 원상 복구: 같은 시나리오 작업이 실행 중이면 '멈추고 원상 복구할까요?'로 한 번만 묻고, 허브가 그 작업을 멈춘 뒤 복구한다.
function restoreJob(job,msg){const lk=JOBS_LOG[job],cur=Object.keys(JOBS_LOG).find(j=>JOBS_LOG[j]===lk&&jobs[j]&&jobs[j].state==='running');
  if(cur){if(confirm(`지금 '${jobs[cur].title.replace(/^시나리오 \d · /,'')}' 이(가) 실행 중입니다. 멈추고 원상 복구할까요?

${msg}`))runJob(job,{force:true});return}
  if(confirm(msg+' 계속할까요?'))runJob(job)}
const JOBS_LOG={scenario2:'scenario2','scenario2-servers':'scenario2','scenario2-setup':'scenario2','scenario2-restore':'scenario2',scenario3:'scenario3','scenario3-fault':'scenario3','scenario3-restore':'scenario3','nsg-ip':'scenario3'};
// 카드 안 상태 줄: 카드 제목과 겹치는 '시나리오 N · '는 빼고 PID·인자는 마우스를 올리면 보이게(노트북 폭에서 줄이 넘치던 문제).
function jobLine(j){if(!j||j.state==='idle')return'';const t=j.started.slice(11,16),arg=j.args.length?' '+j.args.join(' '):'';
  const name=esc(j.title.replace(/^시나리오 \d · /,'')),tip=esc(`PID ${j.pid} · ${j.started.replace('T',' ')} 시작${arg}`);
  if(j.state==='running')return`<span class="jobstate running" title="${tip}">● ${name} 실행 중 (${since(j.started)}째)</span>`;
  return`<span class="jobstate ${j.code===0?'ok':'fail'}" title="${tip} · 종료 코드 ${j.code}">${j.code===0?'✓':'✗'} ${name} ${j.code===0?'완료':'실패(코드 '+j.code+')'} (${t} 시작)</span>`}
// 시나리오 3 Health Check 점검표: data/health/latest.json(마지막 점검, 수동·자동 공통). 칸에 마우스를 올리면 자세한 결과.
let healthKey='';
async function loadHealth(){let h;try{h=await (await fetch('/api/health')).json()}catch(e){return}
  const box=document.getElementById('s3health'),run=jobs.scenario3&&jobs.scenario3.state==='running';
  const key=JSON.stringify([h&&h.finished,run,(jobs._restore||{}).scenario3]);if(key===healthKey)return;healthKey=key;
  if(!h||!h.vms){box.innerHTML='<div class="muted">아직 점검하지 않았습니다. "Health Check 실행"을 누르세요.</div>';return}
  const IT=[['vm','VM 상태'],['service','서비스'],['allow','허용 목적지 접속'],['deny','차단 목적지 접속']],M={ok:['✓ 정상','hok'],fail:['✗ 이상','hfail'],skip:['–','hskip']};
  const n=h.failed.length,t=(h.at||'').replace('T',' ').slice(5,16);
  // 점검 뒤에 원상 복구·VM 배포가 끝났으면 이 결과는 옛 상태다 → 다시 점검하라고 알린다
  const rs3=(jobs._restore||{}).scenario3||{},after=!rs3.need&&rs3.at&&new Date(rs3.at)>new Date(h.finished||h.at)?new Date(rs3.at):null;
  box.innerHTML=(after&&!run?`<div class="hsum"><span class="drilltag">다시 점검 필요</span> ${after.toTimeString().slice(0,5)} 원상 복구·배포 뒤 아직 점검하지 않았습니다. 아래는 그 전 결과입니다 → <b>Health Check 실행</b>으로 확인하세요.</div>`:'')
    +`<div class="hsum">${run?'<span class="jobstate running">● 점검 중 (아래는 지난 결과)</span> ':''}<b class="${n?'hfail':'hok'}">${n?'이상 '+n+'건':'모두 정상'}</b> <span class="muted">· ${esc(t)} ${h.trigger==='auto'?'자동':'수동'} 점검 · 기준: Wiki Proxy Health Check 기준</span></div>`
    +'<table><tr><th>VM</th>'+IT.map(([,x])=>`<th>${x}</th>`).join('')+'</tr>'
    +h.vms.map(v=>{const c=Object.fromEntries(v.checks.map(x=>[x.item,x]));return`<tr><td>${esc(v.name)} <span class="muted">${esc(v.kind)}</span></td>`
      +IT.map(([k])=>{const x=c[k]||{status:'skip',detail:''},m=M[x.status]||M.skip;return`<td class="${m[1]}" title="${esc(x.detail)}">${m[0]}</td>`}).join('')+'</tr>'}).join('')+'</table>'
    +(n?'<div class="hbad">'+h.failed.map(f=>`<div>✗ ${esc(f.vm)} · ${esc(f.item)}: ${esc(f.detail)}</div>`).join('')+'</div>':'')}
// 시나리오 2 비교표: data/scenario2/latest.json(마지막 실행). Wiki 문서와 Azure 실제 값을 차이 나는 것만 줄로.
let s2Key='';
async function loadS2Result(){let r;try{r=await (await fetch('/api/s2result')).json()}catch(e){return}
  const box=document.getElementById('s2result'),run=['scenario2','scenario2-setup'].some(k=>jobs[k]&&jobs[k].state==='running');
  const key=JSON.stringify([r&&r.at,r&&r.result,r&&r.approval,r&&r.task,run]);if(key===s2Key)return;s2Key=key;
  if(!r||!r.diffs){box.innerHTML='<div class="muted">아직 비교하지 않았습니다. "실행"을 누르면 Wiki 문서와 Azure 실제 VM을 비교합니다.</div>';return}
  const n=r.diffs.length,t=(r.at||'').replace('T',' ').slice(5,16),L=r.labels||{};
  const A=Object.fromEntries((r.azure||[]).map(v=>[v.name,v])),W=Object.fromEntries((r.wiki||[]).map(v=>[v.name,v]));
  const vmtxt=v=>v?[v.state,v.size,v.private_ip].filter(Boolean).join(' · '):'(없음)';
  const AP={wait:'<span class="jobstate running">승인 대기 중</span>',yes:'승인: Yes',no:'승인: No',timeout:'승인: 시간 초과'};
  const RS={applied:'<b class="hok">Wiki 반영 완료</b>',declined:'반영 안 함',manual:'문서 수동 반영 요청',error:'<b class="hfail">처리 실패</b>','dry-run':'분석만(dry-run)'};
  let h=`<div class="hsum">${run?'<span class="jobstate running">● 실행 중 (아래는 지난 결과)</span> ':''}<b class="${n?'hfail':'hok'}">${n?'차이 '+n+'건':'Wiki와 Azure가 같음'}</b>`
    +` <span class="muted">· ${esc(t)} 점검</span>`+(r.approval?' · '+(r.approval==='wait'&&!run?'<b class="hfail">승인 전에 중단됨</b>':AP[r.approval]||''):'')
    +(r.task?` → <a href="${esc(r.task_url)}" onclick="return !openTop(this.href,'devops')">Task #${r.task}</a>`:'')+(RS[r.result]?' · '+RS[r.result]:'')+'</div>';
  if(r.score){const sc=r.score,ok=sc.found===sc.planted&&!sc.extra.length;
    h+=`<div class="hsum"><span class="drilltag">모의 훈련 채점</span> 심은 차이 ${sc.planted}건 중 <b class="${ok?'hok':'hfail'}">${sc.found}건 찾음</b>`
      +(sc.extra.length?` · 정답 밖 ${sc.extra.length}건`:'')+(ok?' <span class="hok">✓</span>':'')+'</div>'}
  if(r.alert)h+=`<div class="hbad">${esc(r.alert)}</div>`;
  if(n){h+='<table><tr><th>VM</th><th>항목</th><th>Wiki 문서</th><th>Azure 실제</th><th>결과</th></tr>'
    +r.diffs.map(d=>d.type==='added'?`<tr><td>${esc(d.name)}</td><td>-</td><td class="hskip">(없음)</td><td>${esc(vmtxt(A[d.name]))}</td><td class="hok">＋ 신규</td></tr>`
      :d.type==='removed'?`<tr><td>${esc(d.name)}</td><td>-</td><td>${esc(vmtxt(W[d.name]))}</td><td class="hskip">(없음)</td><td class="hskip">－ 삭제</td></tr>`
      :`<tr><td>${esc(d.name)}</td><td>${esc(L[d.field]||d.field)}</td><td class="hfail">${esc(d.wiki)}</td><td>${esc(d.azure)}</td><td class="hfail">✗ 변경</td></tr>`).join('')+'</table>'}
  else h+='<table><tr><th>VM</th><th>Azure 실제</th><th>결과</th></tr>'+(r.azure||[]).map(v=>`<tr><td>${esc(v.name)}</td><td>${esc(vmtxt(v))}</td><td class="hok">✓ 같음</td></tr>`).join('')+'</table>';
  box.innerHTML=h}
async function refreshJobs(){try{jobs=await (await fetch('/api/jobs')).json()}catch(e){return}
  loadHealth();loadS2Result();
  const run=['scenario2','scenario2-servers','scenario2-setup','scenario2-restore'].some(k=>jobs[k]&&jobs[k].state==='running');['s2run','s2servers','s2setup'].forEach(id=>document.getElementById(id).disabled=run);
  const s3=['scenario3','scenario3-fault','scenario3-restore','nsg-ip'],run3=s3.some(k=>jobs[k]&&jobs[k].state==='running');
  ['s3run','s3inject','s3nsg'].forEach(id=>document.getElementById(id).disabled=run3);
  // 원상 복구는 로그로 보아 필요할 때만 켠다(테스트 환경 준비·장애 모의 훈련 뒤). 꺼진 이유는 툴팁에.
  const rs=jobs._restore||{};[['s2restore','scenario2',run,'test/scenario2/setup_env.py --restore: 테스트 문서를 실제 VM 과 같은 정상 표로'],
    ['s3restore','scenario3',run3,'심은 장애를 되돌리고(없으면 그대로) main 을 배포·확인 (proxy_fault_inject.py restore)']].forEach(([id,k,busy,tip])=>{
    const b=document.getElementById(id),x=rs[k]||{};// 시나리오 3 원상 복구는 언제나 누를 수 있다(심은 장애가 없으면 지금 main 을 VM 에 다시 배포해 맞춤). 시나리오 2 는 필요할 때만.
    b.disabled=(k==='scenario2'&&!x.need)||jobs[k+'-restore']?.state==='running';
    b.title=(busy&&x.need?'지금 실행 중인 작업을 멈추고 원상 복구합니다(누르면 한 번 확인) · ':'')+tip+(x.need?' · 지금 필요: ':' · 지금은 필요 없음: ')+(x.why||'')});
  // 지금 상태: 저장소에 심어진 장애(터미널에서 바꾼 것도 반영). 실행 기록은 가장 최근 것 하나만, 몇 분 전인지 함께.
  const f=jobs._fault||{};document.getElementById('s3fault-state').innerHTML=f.error?`<span class="muted">장애 상태를 읽지 못함: ${esc(f.error)}</span>`
    :(f.active&&f.active.length?`<span class="jobstate fail">⚠ 심어진 장애: ${esc(f.active.join(', '))} → 시험 후 원상 복구</span>`:'<span class="jobstate ok">저장소에 심어진 장애: 없음</span> <span class="muted" title="병합 후에는 Pipelines 의 proxy-config VM 배포 또는 원상 복구">(VM 은 배포해야 반영)</span>');
  const pl=jobs._pipeline;document.getElementById('s3pipe').innerHTML=!pl?'':pl.state!=='completed'
    ?`<span class="jobstate running">● VM 배포 파이프라인 #${esc(pl.name)} 실행 중</span> <a href="${esc(pl.url||'#')}" onclick="return !openTop(this.href,'pipeline')">Azure DevOps 에서 보기</a> <span class="muted">(로그는 아래 시나리오 3 로그에 [pipeline] 으로)</span>`
    :`<span class="jobstate ${pl.result==='succeeded'?'ok':'fail'}">${pl.result==='succeeded'?'✓':'✗'} VM 배포 파이프라인 #${esc(pl.name)} ${esc(pl.result||'')} (${pl.finished?new Date(pl.finished).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'}):''} 종료)</span>`;
  const last=s3.map(k=>jobs[k]).filter(j=>j&&j.state!=='idle').sort((a,b)=>b.started.localeCompare(a.started))[0];
  // 정상(심어진 장애 없음)이고 성공으로 끝난 지 10분이 지나면 '마지막 실행' 줄을 숨긴다. 실행 중·실패·장애 남음이면 계속 보인다.
  const quiet=last&&last.state==='finished'&&last.code===0&&!(f.active&&f.active.length)&&(Date.now()-new Date(last.finished||last.started))>10*60*1000;
  document.getElementById('s3state').innerHTML=(!last||quiet?'':(last.state==='running'?'':`<span class="muted">${since(last.started)} 전:</span> `)+jobLine(last))
    +(jobs._approval?` <button style="padding:1px 8px" onclick="openTop('${esc(jobs._approval)}','approval')">승인 화면</button>`:'');
  // 승인 대기 중이면 그 시나리오 화면(비교표·점검표 바로 위)에 Yes/No 버튼. Teams 버튼과 먼저 누른 쪽만 반영된다.
  // (예전에는 승인 화면 창을 자동으로 띄웠다. 이제는 화면 안 버튼으로 하고, 자세한 화면은 '자세히 보기'로 연다)
  const AW=jobs._approval_wait;[['s2ap','scenario2','✓ 현행화 (Yes)','기록만 (No)'],['s3ap','scenario3','✓ 조치 (Yes)','조치 안 함 (No)']].forEach(([id,k,y,n])=>{
    const box=document.getElementById(id),html=AW===k?`<div class="apbar"><b>● 승인 대기 중</b> <span class="muted">Teams 에도 보냈습니다. 먼저 누른 쪽이 반영됩니다.</span>`
      +`<button class="main" onclick="decide('yes')">${y}</button><button onclick="decide('no')">${n}</button>`
      +`<button onclick="openTop('${esc(jobs._approval||'')}','approval')" title="${k==='scenario2'?'프록시 아님 선택(이름으로만 추정한 VM)은 이 화면에서 합니다':'승인 화면을 새 창으로'}">자세히 보기</button></div>`:'';
    if(box.dataset.h!==html){box.dataset.h=html;box.innerHTML=html}});
  document.getElementById('s2state').innerHTML=[jobLine(jobs['scenario2']),jobLine(jobs['scenario2-servers']),jobLine(jobs['scenario2-setup']),jobLine(jobs['scenario2-restore'])].filter(Boolean).join(' <span class="muted">·</span> ')||'<span class="muted">아직 실행하지 않았습니다.</span>';
  const ws=wikiSearch(),ok=ws&&ws.state==='running';document.getElementById('s1run').disabled=!ok;
  // 서버 상태(/api/status)를 아직 못 받았으면 '꺼짐'이 아니라 '확인 중'(새로 고침 직후 잠깐 빨간 글자가 보이던 문제)
  document.getElementById('s1state').innerHTML=ok?'':!lastOkText?'<span class="muted">서버 상태 확인 중…</span>':'<span class="jobstate fail">Wiki 검색 서버가 꺼져 있습니다.</span> '+(ws&&ws.busy?`<span class="muted">${esc(ws.busy)}…</span>`:`<button style="padding:1px 8px" onclick="act('wiki-search','start')">서버 시작</button>`);
  if(pageLog)loadSLog()}
// 로그는 고른 시나리오 화면 아래에 보인다. ?log=<key> 로 열면 로그만 보이는 '로그 전용 모드'(README 캡처용).
const LOGQ=new URLSearchParams(location.search),LOGMODE=!!LOGQ.get('log');
function showLogHere(key,title,inline){pageLog=key;if(inline){loadSLog();setNames(key,title);return}slogKey=key;setNames(key,title);document.querySelectorAll('button.slog').forEach(b=>b.classList.toggle('logon',b.dataset.key===key));loadSLog()}
function setNames(key,title){document.getElementById('sname').textContent=title;document.getElementById('shint').textContent='data/logs/'+key+'.log'}
// 로그는 늘 맨 아래(최신)를 따라간다. 사용자가 직접 위로 올렸을 때만 멈추고, 다시 맨 아래로 내리면 또 따라간다.
// (예전에는 '지금 맨 아래인가'로만 봐서, 실행 계획 패널이 커지며 로그 칸이 줄면 따라가기가 풀려 위에 머물렀다)
let slogFollow=true,slogShown='';
const slogStick=()=>{const pre=document.getElementById('slog');if(slogFollow)pre.scrollTop=pre.scrollHeight};
document.getElementById('slog').addEventListener('scroll',e=>{const pre=e.target;slogFollow=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20});
async function loadSLog(){if(!pageLog)return;const pre=document.getElementById('slog'),key=pageLog;
  const text=await fetchLog(key);if(key!==pageLog)return;
  if(slogShown!==key){slogFollow=true;slogShown=key}
  if(pre.textContent!==text){const f=slogFollow;pre.textContent=text;slogFollow=f}
  document.getElementById('sall').hidden=!cleared[pageLog];slogStick();requestAnimationFrame(slogStick);loadPlan()}
// 실행 계획 패널: 로그가 빨리 흘러가도 계획·사전 점검·진행 단계를 로그 위에 고정해 보여 준다(/api/plan 이 로그를 해석).
let planFold=localStorage.getItem('hub-plan-fold')==='1';
let oldOpen='';  // 지난 실행 기록을 펼쳐 본 trace id (이 화면에서만)
function foldPlan(old,id){if(old)oldOpen=oldOpen===id?'':id;else{planFold=!planFold;localStorage.setItem('hub-plan-fold',planFold?'1':'0')}loadPlan()}
const toDate=s=>s&&s.length>8?new Date(s.replace(' ','T')):null;
function dur(a,b){const x=toDate(a),y=b?toDate(b):new Date();if(!x||!y)return'';const s=Math.max(0,Math.round((y-x)/1000));
  return s<60?s+'초':Math.floor(s/60)+'분 '+(s%60)+'초'}
async function loadPlan(){const box=document.getElementById('splan');const key=pageLog;if(!key){box.hidden=true;return}
  let p=null;try{p=await (await fetch('/api/plan?key='+encodeURIComponent(key)+'&from='+(cleared[key]||0))).json()}catch(e){}
  if(key!==pageLog)return;if(!p){box.hidden=true;fit();return}
  // 끝 줄(■) 없이 작업이 끝났으면(강제 종료 등) 진행 중이 아니라 중단으로 본다.
  // 시나리오 1(Wiki 검색 서버)은 작업이 아니라 서버라서, 서버가 켜져 있으면 진행 중으로, 기록은 언제나 이번 것으로 본다.
  const srv=key==='wiki-search',ws=srv&&wikiSearch();
  const live=srv?!!(ws&&ws.state==='running'):Object.entries(JOBS_LOG).some(([j,k])=>k===key&&jobs[j]&&jobs[j].state==='running');
  const st=p.status==='running'&&!live?'stopped':p.status;
  // 이번 허브에서 실행한 작업이 아니면(허브를 켜기 전 실행 등) 지난 기록으로 보고 한 줄로 접어 둔다.
  const ps=toDate(p.started),mine=srv||live||Object.entries(JOBS_LOG).some(([j,k])=>{const x=jobs[j];
    return k===key&&x&&x.started&&ps&&Math.abs(ps-new Date(x.started))<60000});
  const old=!mine,fold=old?oldOpen!==p.id:planFold;
  const ST={running:['● 실행 중','running'],ok:['✓ 완료','ok'],error:['✗ 오류','fail'],cancelled:['… 취소','fail'],stopped:['■ 중단(끝 기록 없음)','fail']}[st]||[st,''];
  const icon={planned:'○',running:'●',done:'✓',error:'✗',skipped:'–'};
  const total=dur(p.started,st==='running'?'':p.ended);
  let h=`<div class="ph"><b>${old?'지난 실행 기록':'실행 계획'}</b><span class="${old?'muted':'jobstate '+ST[1]}">${ST[0]}</span>`
    +`<span class="muted">${esc(p.started)} 시작${old&&ps?' ('+since(p.started.replace(' ','T'))+' 전)':''}${total?' · '+total+(st==='running'?'째':''):''} · trace ${esc(p.id)}</span>`
    +`<button onclick="foldPlan(${old},'${esc(p.id)}')">${fold?'펼치기':'접기'}</button></div>`;
  if(fold&&old){}
  else if(fold){const cur=p.steps.findIndex(s=>s.status==='running');
    h+=`<div class="muted">${cur>=0?`지금 ${cur+1}/${p.steps.length} 단계: ${esc(p.steps[cur].name)} — ${esc(p.steps[cur].desc)}`:esc(p.goal)}</div>`}
  else{if(p.goal)h+=`<div>목표: ${esc(p.goal)}</div>`;
    if(p.checks.length)h+=`<div class="chk"><span class="muted">사전 점검</span>`+p.checks.map(c=>`<span class="chip${c.ok?'':' no'}" title="${esc(c.note)}">${c.ok?'✓':'△'} ${esc(c.name)}${c.ok?'':' · '+esc(c.note)}</span>`).join('')+`</div>`;
    h+='<ol>'+p.steps.map((s,i)=>{const t=s.status==='running'?dur(s.started)+'째':(s.started&&s.ended?dur(s.started,s.ended):'');
      const note=s.note||(s.status==='skipped'?'건너뜀(재계획)':'');
      return`<li class="${s.status}"><span class="ic">${icon[s.status]||''}</span><span>${i+1}</span><span class="nm">${esc(s.name)}</span>`
        +(note||t?`<span class="ds" title="${esc(s.desc)}">${esc(s.desc)}</span><span class="nt" title="${esc(note)}">${esc(note)}${note&&t?' · ':''}${t}</span>`
          :`<span class="ds full" title="${esc(s.desc)}">${esc(s.desc)}</span>`)+'</li>'}).join('')+'</ol>'}
  box.innerHTML=h;box.hidden=false;fit();slogStick()}
// 로그 창은 화면 아래 끝까지만 늘려 페이지 전체 스크롤이 생기지 않게 한다.
// 높이를 바꾸기 전에 맨 아래를 보고 있었으면 바꾼 뒤에도 맨 아래에 붙인다(위 실행 계획 패널이 커지면 마지막 줄이 가려지던 문제).
// 로그 높이는 배치(CSS)가 정한다: 실행 계획 + 최소 8줄, 남는 높이는 위 화면과 나눔. 여기서는 맨 아래 따라가기만.
function fit(){const card=document.querySelector('.sclog');
  if(card&&!document.body.classList.contains('logmode')){const pl=document.getElementById('splan');
    const need=card.querySelector('.tabs').offsetHeight+(pl.hidden?0:pl.offsetHeight)+168+2;  // 머리 + 실행 계획 + 로그 8줄
    if(card.style.minHeight!==need+'px')card.style.minHeight=need+'px'}
  slogStick()}
addEventListener('resize',fit);setInterval(()=>{applySplit();fit()},500);
refresh();setInterval(refresh,3000);setInterval(staleCheck,1000);setInterval(refreshJobs,2000);
if(LOGMODE){document.body.classList.add('logmode');const k=LOGQ.get('log'),tt=LOGQ.get('t')||k;document.title=tt;
  showLogHere(k,tt);refreshJobs()}
else{document.body.classList.add('nosel');refreshJobs();loadS2Links()}
</script></body></html>"""


def port_owner(ip: str, port: int) -> int | None:
    return next((pid for pid, addrs in listeners().items() if f"{ip}:{port}" in addrs and pid != os.getpid()), None)


def replace_old_hub(ip: str, port: int) -> None:
    """이미 떠 있는 허브를 끈다. /T 를 쓰지 않으므로 그 허브가 띄운 서버들은 계속 돈다."""
    owner = port_owner(ip, port)
    if not owner:
        return
    log(f"이미 떠 있는 허브(PID {owner})를 끄고 새로 띄웁니다. 끄지 않으려면 --keep")
    subprocess.run(["taskkill", "/PID", str(owner), "/F"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
    for _ in range(20):
        if not port_owner(ip, port):
            return
        time.sleep(0.25)


def main():
    flags = set(sys.argv[1:])
    ip, port, _ = site_url(HUB_SITE, HUB_PORT)
    if "--keep" not in flags:
        replace_old_hub(ip, port)
    try:
        server = local_server(HUB_PORT, Handler, HUB_SITE)
    except SystemExit:
        owner = port_owner(ip, port)
        log(f"{HUB_TITLE} 를 띄우지 못했습니다: {ip}:{port} 를 이미 사용 중입니다"
            + (f" (PID {owner}). 이미 떠 있는 허브라면 브라우저로 http://{HUB_SITE} 를 열거나, "
               f"--keep 없이 다시 실행하세요(그 허브만 끄고 새로 띄움)." if owner else "."))
        raise SystemExit(1)
    log(f"{HUB_TITLE} 시작: {server.url} (수신 {ip}:{port}, PID {os.getpid()})")
    # 서버를 다시 시작하기 전에 잰다(다시 시작하며 찍는 '===== 시작' 줄부터 보이게)
    for key in [s["key"] for s in SERVERS] + sorted(JOB_LOGS):
        path = LOG_DIR / f"{key}.log"
        LOG_START["sizes"][key] = path.stat().st_size if path.is_file() else 0
    LOG_START["at"] = datetime.now().isoformat(timespec="seconds")
    log(f"  파이썬 {sys.executable}")
    log(f"  서버 출력 로그 폴더 {LOG_DIR}")
    for s in server_status(force=True):
        log(f"  [{s['key']}] {BY_KEY[s['key']]['dir'] / s['script']}")
        log(f"  [{s['key']}]   {describe(s)}")
    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=pipeline_watch, daemon=True).start()
    if "--restart-all" in flags:
        log("모든 서버를 다시 시작합니다 (--restart-all)")
        dispatch("all", "restart")
    elif "--no-restart" not in flags:  # 기본: 떠 있는 서버는 그대로, 꺼진 서버만 시작(--start-all 은 예전 옵션, 같은 동작)
        log("꺼져 있는 서버를 시작합니다 (기본 동작, 서버 코드를 고쳤으면 --restart-all)")
        dispatch("all", "start")
    if "--no-browser" not in flags:
        open_chrome(
            server.url + "/#servers",
            close_url=server.url + "/#servers",
            profile_dir=PROJECT_ROOT / "data" / "hub-chrome-profile",
        )
    log("요청을 기다립니다. 끄려면 Ctrl+C (서버들은 계속 실행됨)")
    try: server.serve_forever()
    except KeyboardInterrupt: log("Ctrl+C: 허브를 끕니다")
    finally: server.server_close()


if __name__ == "__main__": main()
