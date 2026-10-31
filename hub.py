"""Local hub: one page to start, stop and watch the ai-master local web servers.

    python hub.py                # http://ai-master.local (hosts 등록 시), 없으면 http://127.0.0.1:8764
                                 # 이미 떠 있는 허브가 있으면 그 허브만 끄고 새로 띄운다(서버들은 그대로)
    python hub.py --keep         # 이미 떠 있는 허브가 있으면 끄지 않고 종료
    python hub.py --start-all    # 허브를 켜면서 꺼져 있는 서버를 모두 시작
    python hub.py --restart-all  # 허브를 켜면서 모든 서버를 다시 시작(중복 실행도 정리)
    python hub.py --no-browser

- 서버는 창 없이 띄우고 출력은 data/logs/<key>.log 에 남긴다. 허브를 꺼도 서버는 계속 돈다.
- 실행 여부는 프로세스 명령줄의 스크립트 이름으로 찾는다. 허브 밖에서 직접 띄운 서버도 보이고 끌 수 있다.
- 콘솔에는 시각과 함께 시작 설정, 요청, 생성·종료한 PID, 상태 변화(허브 밖에서 끄거나 죽은 경우 포함), 오류를 남긴다.
- 서버 주소 규칙(hosts 의 IP 에 따라 포트 없는 주소)은 common/server.py 설명 참고.
"""

from __future__ import annotations

import json
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

HUB_PORT, HUB_SITE, HUB_TITLE = 8764, "ai-master.local", "AI Master 서버 허브"
LOG_DIR = DATA_DIR / "logs"
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


# 서버가 아니라 실행하고 끝나는 작업(시나리오 실행 탭). log 는 data/logs/<log>.log.
# self_log=True 인 작업은 스스로 그 파일에 출력을 남기므로 허브는 출력을 버린다(중복 방지).
JOBS = {
    "scenario2": {"title": "시나리오 2 · Wiki 현행화", "script": "proxy_tools/proxy_status_sync.py", "log": "scenario2",
                  "self_log": True,
                  "options": {}},
    "scenario3": {"title": "시나리오 3 · 장애 감지 및 조치", "script": "proxy_tools/incident_response.py", "log": "scenario3",
                  "self_log": True, "options": {"dry_run": "--dry-run"}},
    # 시험용: proxy-config 에 잘못된 설정을 넣어 wjy-proxy01 에 배포(장애 재현), 그리고 되돌리기
    "scenario3-fault": {"title": "시나리오 3 · 장애 재현(시험용)", "script": "proxy_tools/proxy_fault_inject.py", "log": "scenario3",
                        "self_log": False, "options": {}, "base": ["apply"],
                        "choice": ["whitelist-typo", "client-acl-typo", "bad-dns"],
                        "tail": ["--push", "--deploy", "wjy-proxy01"]},
    "scenario3-restore": {"title": "시나리오 3 · 장애 원상 복구", "script": "proxy_tools/proxy_fault_inject.py", "log": "scenario3",
                          "self_log": False, "options": {}, "base": ["restore", "--push", "--deploy", "wjy-proxy01"]},
    # 공인 IP 가 바뀌어 SSH 가 타임아웃 나면: Proxy VM NSG 의 단일 IP 허용 규칙을 이 PC 의 현재 IP 로 교체
    "nsg-ip": {"title": "NSG 허용 IP 맞추기", "script": "proxy_tools/sync_nsg_ip.py", "log": "scenario3",
               "self_log": False, "options": {}},
    "scenario2-setup": {"title": "시나리오 2 · 테스트 환경 준비", "script": "test/scenario2/setup_env.py", "log": "scenario2",
                        "self_log": False, "options": {}},  # 꺼진 VM 만 켜고 표·정답을 다시 만든다
}
JOB_LOGS = {j["log"] for j in JOBS.values()}
RUNNING: dict[str, dict] = {}  # 작업 → {proc, pid, started, args, finished, code}
_job_lock = threading.Lock()


def run_job(job: str, options: dict) -> dict:
    """작업을 창 없이 실행한다. 같은 로그를 쓰는 작업(예: 시나리오 2 실행과 테스트 환경 준비)은 동시에 돌리지 않는다."""
    spec = JOBS[job]
    with _job_lock:
        busy = [k for k, r in RUNNING.items() if JOBS[k]["log"] == spec["log"] and r["proc"].poll() is None]
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
            with open(LOG_DIR / f"{spec['log']}.log", "ab") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {spec['script']} {' '.join(args)} =====\n".encode("utf-8"))
                f.flush()
                proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=f, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
        RUNNING[job] = {"proc": proc, "pid": proc.pid, "started": datetime.now().isoformat(timespec="seconds"),
                        "args": args, "finished": None, "code": None}
    log(f"[{job}] 작업 실행 PID {proc.pid}: {' '.join(cmd)}")
    return {"pid": proc.pid, "args": args}


def fault_state() -> dict:
    """시나리오 3 시험용 장애가 proxy-config(로컬 main)에 심어져 있는지. 터미널에서 바꾼 것도 보이도록 파일을 직접 본다."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "proxy_tools"))
        import proxy_fault_inject as fi
        names = {"whitelist-typo": "허용 도메인 오타", "client-acl-typo": "허용 클라이언트 IP 오타", "bad-dns": "잘못된 DNS 서버"}
        return {"active": [names.get(n, n) for n in fi.active()]}
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
                self.send_json({"servers": server_status(), "time": datetime.now().isoformat(timespec="seconds")})
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
        elif url.path == "/api/s2links":
            try:
                self.send_json(s2_links(parse_qs(url.query).get("refresh") == ["1"]))
            except Exception as exc:
                log(f"시나리오 2 링크 조회 실패: {type(exc).__name__}: {exc}")
                self.send_json({"error": str(exc)}, 500)
        elif url.path == "/api/jobs":
            self.send_json({**job_status(), "_fault": fault_state()})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/action", "/api/job"):
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
nav.maintabs{display:flex;gap:4px;padding:0 24px;background:var(--top);border-top:1px solid #22324f}
nav.maintabs button{background:none;border:0;border-bottom:3px solid transparent;border-radius:0;color:#aebbd0;padding:10px 14px;font-weight:600}
nav.maintabs button.on{color:#fff;border-bottom-color:#4f8cff}
.scgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
.sc{padding:16px 18px;display:grid;gap:10px;align-content:start}.sc h2{font-size:15px;margin:0}.sc p{margin:0;color:var(--muted);font-size:13px}
.sc input[type=text]{font:inherit;border:1px solid var(--line);border-radius:6px;padding:6px 10px;background:var(--panel);color:var(--ink);width:100%}
.sc .opts{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}.sc .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sc button.main{background:var(--blue);border-color:var(--blue);color:#fff}.jobstate{font-size:13px}
.s2links{font-size:13px;border-top:1px solid var(--line);padding-top:8px;display:grid;gap:6px}.s2links a{color:var(--blue);text-decoration:none}
.s2links .head{display:flex;gap:16px;align-items:center}.s2links a,.s2links button{white-space:nowrap}
.jobstate.running{color:var(--warn)}.jobstate.ok{color:var(--ok)}.jobstate.fail{color:var(--bad)}
@media(max-width:820px){.hide-sm{display:none}main{padding:12px}}
</style></head><body>
<header><h1>__TITLE__</h1><span class="time" id="time"></span>
<span id="allbtns"><button onclick="act('all','start')">모두 시작</button> <button onclick="act('all','restart')">모두 재시작</button>
<button onclick="if(confirm('모든 서버를 끌까요?'))act('all','stop')">모두 중지</button></span></header>
<nav class="maintabs"><button id="mt-servers" onclick="tab('servers')">로컬 서버</button><button id="mt-scenarios" onclick="tab('scenarios')">시나리오 실행</button></nav>
<main id="pane-servers">
<div class="card"><table><thead><tr><th>상태</th><th>서버</th><th>주소</th><th class="hide-sm">PID · 실행 시간</th><th>관리</th></tr></thead>
<tbody id="rows"><tr><td colspan="5" class="muted">불러오는 중…</td></tr></tbody></table><div id="err" class="err" hidden></div></div>
<div class="card" id="panel" hidden><div class="tabs"><button id="tab-log" class="on">로그</button><span class="name" id="pname"></span>
<span class="muted" style="margin-left:auto" id="phint"></span>
<button onclick="clearLog(selected,loadLog)" title="지금까지의 로그를 화면에서 숨긴다(파일은 그대로)">지우기</button>
<button id="pall" onclick="unclearLog(selected,loadLog)" hidden>전체 보기</button></div>
<pre id="log"></pre></div>
</main>
<main id="pane-scenarios" hidden>
<div class="scgrid">
<div class="card sc"><h2>시나리오 1 · 프록시 설정 가이드 자동 생성</h2>
<p>Wiki 검색 화면을 새 창으로 엽니다. 질의 입력·유형 선택·미리보기·수정 요청·Wiki 등록은 그 화면에서 합니다.</p>
<div class="row"><button class="main" id="s1run" onclick="runS1()">실행 (Wiki 검색 화면 열기)</button><button class="slog" data-key="wiki-search" onclick="showLog('wiki-search','시나리오 1 · Wiki 검색 서버 로그')">로그</button>
<span class="jobstate" id="s1state"></span></div></div>
<div class="card sc"><h2>시나리오 2 · 프록시 현황 Wiki 현행화</h2>
<p>Wiki 현황 문서와 Azure VM을 비교하고, 차이가 있으면 Teams 승인 후 Task·Wiki를 반영합니다. 승인 카드는 Teams 일반 채널로 갑니다.</p>
<div class="row"><button id="s2setup" onclick="runJob('scenario2-setup')" title="test/scenario2/setup_env.py: 꺼진 Proxy VM을 켜고, 테스트 문서를 '현행화 전'으로 만들고 정답을 다시 만든다">테스트 환경 준비</button>
<button class="main" id="s2run" onclick="runJob('scenario2')">실행</button>
<button onclick="openServer('proxy-log')" title="프록시 로그 뷰어(proxy-log.local)를 새 창으로 엽니다">로그 뷰어</button>
<button class="slog" data-key="scenario2" onclick="showLog('scenario2','시나리오 2 로그')">로그</button></div>
<div class="jobstate" id="s2state"></div>
<div class="s2links" id="s2links"><span class="muted">Azure VM·Wiki 링크 불러오는 중…</span></div></div>
<div class="card sc"><h2>시나리오 3 · 장애 감지 및 자동 조치</h2>
<p>프록시 로그에서 장애를 찾아 원인·수정안을 만들고, Teams 승인 후 Task·수정 PR·장애 대응 이력 Wiki를 남깁니다. 시험: wjy-proxy01 (실제 저장소·VM을 바꿈)</p>
<div class="row" style="flex-wrap:nowrap"><b>①</b><select id="s3fault" title="일부러 넣을 장애 종류" style="font:inherit;border:1px solid var(--line);border-radius:6px;padding:4px 6px;background:var(--panel);color:var(--ink);flex:1;min-width:0">
<option value="whitelist-typo">허용 도메인 오타 (data → date)</option><option value="client-acl-typo">허용 클라이언트 IP 오타</option>
<option value="bad-dns">잘못된 DNS 서버</option></select>
<button id="s3inject" onclick="if(confirm('proxy-config 에 잘못된 설정을 커밋·push 하고 wjy-proxy01 에 배포합니다. 실제로 접속이 막힙니다. 계속할까요?'))runJob('scenario3-fault',{choice:document.getElementById('s3fault').value})" title="고른 종류의 잘못된 설정을 일부러 배포하고 시험 접속을 보내 차단 로그를 남김 (proxy_fault_inject.py apply)">장애 재현</button></div>
<div class="row"><b>②</b><button class="main" id="s3run" onclick="runJob('scenario3')" title="최근 60분 로그 점검 → 장애가 있으면 Teams 승인 → Task·수정 PR·이력 Wiki (incident_response.py)">실행</button>
<b>③</b><button id="s3restore" onclick="if(confirm('심어진 장애를 되돌리는 커밋을 push 하고 wjy-proxy01 에 다시 배포합니다. 계속할까요?'))runJob('scenario3-restore')" title="심은 장애를 되돌려 배포·확인 (또는 수정 PR 병합 후 배포) (proxy_fault_inject.py restore)">원상 복구</button>
<button id="s3nsg" onclick="if(confirm('Proxy VM NSG 의 허용 IP(SSH·프록시 포트)를 .env PROXY_ALLOWED_CLIENTS 의 IP + 이 PC 의 현재 공인 IP 로 맞춥니다. 그 밖의 예전 IP 는 지웁니다. 계속할까요?'))runJob('nsg-ip')" title="SSH 가 Connection timed out 으로 실패할 때: NSG 허용 규칙을 고정 허용 IP + 현재 IP 로 맞춤 (sync_nsg_ip.py)">허용 IP 맞추기</button>
<button class="slog" data-key="scenario3" onclick="showLog('scenario3','시나리오 3 로그')" style="margin-left:auto">로그</button></div>
<div class="jobstate" id="s3fault-state"></div>
<div class="jobstate" id="s3state"></div></div>
</div>
<div class="card"><div class="tabs"><span class="name" id="sname">로그를 고르세요</span><span class="muted" style="margin-left:auto" id="shint"></span>
<button onclick="clearLog(slogKey,loadSLog)" title="지금까지의 로그를 화면에서 숨긴다(파일은 그대로)">지우기</button>
<button id="sall" onclick="unclearLog(slogKey,loadSLog)" hidden>전체 보기</button></div>
<pre id="slog"></pre></div>
</main>
<script>
let servers=[], selected=null;

const LABEL={running:'실행 중',starting:'시작 중',stopped:'꺼짐'};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function since(iso){if(!iso)return'';const s=Math.max(0,(Date.now()-new Date(iso))/1000);
  if(s<60)return Math.floor(s)+'초';if(s<3600)return Math.floor(s/60)+'분';if(s<86400)return Math.floor(s/3600)+'시간 '+Math.floor(s%3600/60)+'분';return Math.floor(s/86400)+'일'}
async function act(key,action){
  const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Hub':'1'},body:JSON.stringify({key,action})});
  if(!r.ok){alert('요청 실패: '+r.status);return}setTimeout(refresh,300)}
function render(){
  document.getElementById('rows').innerHTML=servers.map(s=>{
    const n=s.instances.length, busy=s.busy;
    const label=busy||LABEL[s.state];
    const warn=[n>1?`중복 실행 ${n}개 (재시작하면 하나로 정리)`:'',
      s.stale?`예전 주소로 실행 중 → 재시작하면 ${esc(s.address)} 로 뜸`:'',
      s.conflict?`${esc(s.address)} 를 다른 프로그램이 사용 중 (PID ${s.conflict.pid} ${esc(s.conflict.name)})`:''].filter(Boolean);
    const pid=s.instances.map(i=>`${i.pid} · ${since(i.started)}`).join('<br>')||'-';
    const link=s.state==='running'?`<a href="${esc(s.url)}" target="_blank">${esc(s.url)}</a>`:`<span class="muted">${esc(s.url)}</span>`;
    return `<tr class="${s.key===selected?'sel':''}"><td><span class="state ${busy?'starting':s.state}"><span class="dot"></span>${esc(label)}</span></td>
<td><div class="title">${esc(s.title)}</div><div class="script">${esc(s.script)}</div>${warn.map(w=>`<div class="warn">${w}</div>`).join('')}</td>
<td>${link}</td><td class="hide-sm muted">${pid}</td>
<td><div class="acts"><button ${busy||n?'disabled':''} onclick="act('${s.key}','start')">시작</button>
<button ${busy?'disabled':''} onclick="act('${s.key}','restart')">재시작</button>
<button ${busy||!n?'disabled':''} onclick="act('${s.key}','stop')">중지</button>
<button class="${s.key===selected?'logon':''}" onclick="pick('${s.key}')">로그</button></div></td></tr>`}).join('')}
// 머리글 시각: 서버 상태를 마지막으로 받은 때. 10초 넘게 못 받으면 허브가 죽었거나 멈춘 것 → 빨간 '허브 응답 없음'
// (그동안 표의 '실행 중'은 옛 정보). 요청이 매달려도 알 수 있게 응답과 따로 1초마다 본다.
let lastOk=Date.now(),lastOkText='';
function staleCheck(){const el=document.getElementById('time');const stale=Date.now()-lastOk>10000;el.classList.toggle('stale',stale);
  el.textContent=stale?'허브 응답 없음'+(lastOkText?' (마지막 확인 '+lastOkText+')':''):(lastOkText?'서버 상태 확인 '+lastOkText:'')}
async function refresh(){
  try{const r=await fetch('/api/status');const d=await r.json();if(d.error)throw new Error(d.error);
    servers=d.servers;lastOk=Date.now();lastOkText=d.time.slice(11);staleCheck();
    document.getElementById('err').hidden=true;render();if(selected)loadLog()}
  catch(e){const el=document.getElementById('err');el.hidden=false;el.textContent='상태를 읽지 못했습니다: '+e.message}}
// 로그 '지우기': 파일은 그대로 두고, 누른 시점의 파일 크기를 기억해 그 뒤만 보여 준다(브라우저에 저장, 새로고침해도 유지).
const CLEARED_KEY='hub.logCleared';let cleared={};try{cleared=JSON.parse(localStorage.getItem(CLEARED_KEY)||'{}')}catch(e){}
const logSize={};
function saveCleared(){try{localStorage.setItem(CLEARED_KEY,JSON.stringify(cleared))}catch(e){}}
async function fetchLog(key){const r=await fetch('/api/log?key='+encodeURIComponent(key)+'&from='+(cleared[key]||0));
  logSize[key]=+(r.headers.get('X-Log-Size')||0);return r.text()}
function clearLog(key,reload){if(!key)return;cleared[key]=logSize[key]||0;saveCleared();reload()}
function unclearLog(key,reload){delete cleared[key];saveCleared();reload()}
async function loadLog(){const pre=document.getElementById('log');const atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
  pre.textContent=await fetchLog(selected);document.getElementById('pall').hidden=!cleared[selected];if(atEnd)pre.scrollTop=pre.scrollHeight}
// 하단 패널은 로그만 보여 준다. 서버 화면은 주소를 눌러 새 창에서 연다. 보고 있는 로그의 버튼은 파랗게 표시한다.
function pick(key){const s=servers.find(x=>x.key===key);const title=s&&s.title;if(!title)return;selected=key;
  document.getElementById('panel').hidden=false;document.getElementById('pname').textContent=title;
  document.getElementById('phint').textContent='data/logs/'+key+'.log';loadLog();render()}
// ---------------------------------------------------------------- 탭
function tab(t){location.hash=t;document.getElementById('pane-servers').hidden=t!=='servers';
  document.getElementById('pane-scenarios').hidden=t!=='scenarios';document.getElementById('allbtns').style.visibility=t==='servers'?'':'hidden';
  document.getElementById('mt-servers').classList.toggle('on',t==='servers');document.getElementById('mt-scenarios').classList.toggle('on',t==='scenarios');
  if(t==='scenarios'){refreshJobs();loadS2Links();if(!slogKey)showLog('scenario2','시나리오 2 로그')}}
// 시나리오 2: Wiki 현황 문서와 Azure Portal VM 목록, 한 줄. 서버가 5분 캐시, '새로 읽기'는 즉시.
async function loadS2Links(refresh){const box=document.getElementById('s2links');if(refresh)box.innerHTML='<span class="muted">다시 읽는 중…</span>';
  let d;try{d=await (await fetch('/api/s2links'+(refresh?'?refresh=1':''))).json()}catch(e){box.innerHTML='<span class="muted">링크를 불러오지 못했습니다.</span>';return}
  const a=(u,t)=>u?`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`:'';
  const doc=x=>`${a(x.local,x.title)}${x.devops?' · '+a(x.devops,'Azure DevOps'):''}`;
  const sp=d.status_page;
  const up=(d.vms||[]).filter(v=>v.state==='운영').length,n=(d.vms||[]).length;
  box.innerHTML=`<div class="head">${sp?'<span>Wiki: '+doc(sp)+'</span>':''}<span>Azure: ${a(d.portal_vms,'Proxy VM 목록')} <span class="muted">${d.error?esc(d.error):n?`${n}대 중 ${up}대 실행 중`:'Proxy VM 없음'}</span></span>`
    +`<button class="slog" style="margin-left:auto" onclick="loadS2Links(true)">새로 읽기</button></div>`}
// ---------------------------------------------------------------- 시나리오 실행
let jobs={}, slogKey=null;
function wikiSearch(){return servers.find(s=>s.key==='wiki-search')}
function openServer(key){const s=servers.find(x=>x.key===key);
  if(!s||s.state!=='running'){alert((s?s.title:key)+' 서버가 꺼져 있습니다. 서버 탭에서 먼저 시작하세요.');return}
  // 화면 가로 전체 × 위쪽 절반 크기의 새 창으로 연다(허브 아래쪽이 보이도록)
  const w=screen.availWidth,h=Math.round(screen.availHeight/2),l=screen.availLeft||0,t=screen.availTop||0;
  window.open(s.url+'/',key,`popup,noopener,width=${w},height=${h},left=${l},top=${t}`)}
function runS1(){const s=wikiSearch();if(!s||s.state!=='running')return;
  window.open(s.url+'/','_blank','noopener');showLog('wiki-search','시나리오 1 · Wiki 검색 서버 로그')}
async function runJob(job,options={}){
  const r=await fetch('/api/job',{method:'POST',headers:{'Content-Type':'application/json','X-Hub':'1'},body:JSON.stringify({job,options})});
  const d=await r.json();if(d.error){alert(d.error);return}const lk=JOBS_LOG[job];showLog(lk,{scenario3:'시나리오 3 로그'}[lk]||'시나리오 2 로그');setTimeout(refreshJobs,300)}
const JOBS_LOG={scenario2:'scenario2','scenario2-setup':'scenario2',scenario3:'scenario3','scenario3-fault':'scenario3','scenario3-restore':'scenario3','nsg-ip':'scenario3'};
function jobLine(j){if(!j||j.state==='idle')return'';const t=j.started.slice(11),arg=j.args.length?' '+j.args.join(' '):'';
  if(j.state==='running')return`<span class="jobstate running">● ${esc(j.title)} 실행 중 (PID ${j.pid}, ${since(j.started)}째)${esc(arg)}</span>`;
  return`<span class="jobstate ${j.code===0?'ok':'fail'}">${j.code===0?'✓':'✗'} ${esc(j.title)} 종료 코드 ${j.code} (${t} 시작${esc(arg)})</span>`}
async function refreshJobs(){try{jobs=await (await fetch('/api/jobs')).json()}catch(e){return}
  const run=['scenario2','scenario2-setup'].some(k=>jobs[k]&&jobs[k].state==='running');document.getElementById('s2run').disabled=run;document.getElementById('s2setup').disabled=run;
  const s3=['scenario3','scenario3-fault','scenario3-restore','nsg-ip'],run3=s3.some(k=>jobs[k]&&jobs[k].state==='running');
  ['s3run','s3inject','s3restore','s3nsg'].forEach(id=>document.getElementById(id).disabled=run3);
  // 지금 상태: 저장소에 심어진 장애(터미널에서 바꾼 것도 반영). 실행 기록은 가장 최근 것 하나만, 몇 분 전인지 함께.
  const f=jobs._fault||{};document.getElementById('s3fault-state').innerHTML=f.error?`<span class="muted">장애 상태를 읽지 못함: ${esc(f.error)}</span>`
    :(f.active&&f.active.length?`<span class="jobstate fail">⚠ 현재 심어진 장애: ${esc(f.active.join(', '))} → 시험이 끝나면 원상 복구</span>`:'<span class="jobstate ok">현재 심어진 장애: 없음 (정상)</span>');
  const last=s3.map(k=>jobs[k]).filter(j=>j&&j.state!=='idle').sort((a,b)=>b.started.localeCompare(a.started))[0];
  // 정상(심어진 장애 없음)이고 성공으로 끝난 지 10분이 지나면 '마지막 실행' 줄을 숨긴다. 실행 중·실패·장애 남음이면 계속 보인다.
  const quiet=last&&last.state==='finished'&&last.code===0&&!(f.active&&f.active.length)&&(Date.now()-new Date(last.finished||last.started))>10*60*1000;
  document.getElementById('s3state').innerHTML=!last||quiet?'':`<span class="muted">마지막 실행 (${since(last.started)} 전):</span> `+jobLine(last);
  document.getElementById('s2state').innerHTML=[jobLine(jobs['scenario2']),jobLine(jobs['scenario2-setup'])].filter(Boolean).join('<br>')||'<span class="muted">아직 실행하지 않았습니다.</span>';
  const ws=wikiSearch(),ok=ws&&ws.state==='running';document.getElementById('s1run').disabled=!ok;
  document.getElementById('s1state').innerHTML=ok?`<span class="muted">${esc(ws.url)}</span>`:'<span class="jobstate fail">Wiki 검색 서버가 꺼져 있습니다. 로컬 서버 탭에서 시작하세요.</span>';
  if(slogKey)loadSLog()}
function showLog(key,title){slogKey=key;document.querySelectorAll('button.slog').forEach(b=>b.classList.toggle('logon',b.dataset.key===key));document.getElementById('sname').textContent=title;document.getElementById('shint').textContent='data/logs/'+key+'.log';loadSLog()}
async function loadSLog(){const pre=document.getElementById('slog');const atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
  pre.textContent=await fetchLog(slogKey);document.getElementById('sall').hidden=!cleared[slogKey];if(atEnd)pre.scrollTop=pre.scrollHeight}
// 로그 창은 화면 아래 끝까지만 늘려 페이지 전체 스크롤이 생기지 않게 한다.
function fit(){for(const id of ['log','slog']){const pre=document.getElementById(id);const top=pre.getBoundingClientRect().top;
  if(top>0)pre.style.height=Math.max(160,innerHeight-top-22)+'px'}}
addEventListener('resize',fit);setInterval(fit,500);
refresh();setInterval(refresh,3000);setInterval(staleCheck,1000);setInterval(()=>{if(!document.getElementById('pane-scenarios').hidden)refreshJobs()},2000);
tab(location.hash==='#scenarios'?'scenarios':'servers');
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
    log(f"  파이썬 {sys.executable}")
    log(f"  서버 출력 로그 폴더 {LOG_DIR}")
    for s in server_status(force=True):
        log(f"  [{s['key']}] {BY_KEY[s['key']]['dir'] / s['script']}")
        log(f"  [{s['key']}]   {describe(s)}")
    threading.Thread(target=watch, daemon=True).start()
    if "--restart-all" in flags:
        log("모든 서버를 다시 시작합니다 (--restart-all)")
        dispatch("all", "restart")
    elif "--start-all" in flags:
        log("꺼져 있는 서버를 시작합니다 (--start-all)")
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
