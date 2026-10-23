"""Local hub: one page to start, stop and watch the ai-master local web servers.

    python hub.py                # http://ai-master.local (hosts 등록 시), 없으면 http://127.0.0.1:8764
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
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.paths import DATA_DIR, PROJECT_ROOT  # noqa: E402
from common.server import local_server, open_chrome, site_url  # noqa: E402

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
            for line in log_tail(key, 8).splitlines():
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


def log_tail(key: str, lines: int = 300) -> str:
    log = LOG_DIR / f"{key}.log"
    if not log.is_file():
        return "(로그 없음: 허브에서 시작한 서버만 로그가 남습니다)"
    with open(log, "rb") as f:
        f.seek(max(0, log.stat().st_size - 128 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------- web

class Handler(BaseHTTPRequestHandler):
    def send_body(self, body: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
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
            self.send_body(PAGE.replace("__TITLE__", HUB_TITLE).encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/status":
            try:
                self.send_json({"servers": server_status(), "time": datetime.now().isoformat(timespec="seconds")})
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                log(f"상태 조회 실패: {type(exc).__name__}: {exc}")
                self.send_json({"error": str(exc)}, 500)
        elif url.path == "/api/log":
            key = parse_qs(url.query).get("key", [""])[0]
            if key not in BY_KEY:
                self.send_error(404)
                return
            self.send_body(log_tail(key).encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/action":
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
<title>__TITLE__</title>
<style>
:root{--bg:#eef2f7;--panel:#fff;--ink:#1d2939;--muted:#667085;--line:#d9e0ea;--top:#13213a;--blue:#2563eb;
--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--off:#98a2b3;--soft:#f5f7fb;--code:#101827;--codeink:#e6edf6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1520;--panel:#171f2c;--ink:#e4e8ef;--muted:#98a2b3;--line:#2a3445;
--top:#0b111b;--blue:#6ea0ff;--soft:#1d2636;--code:#0b111b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI","Malgun Gothic",sans-serif}
header{background:var(--top);color:#fff;display:flex;align-items:center;gap:12px;padding:12px 24px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:700}header .time{color:#aebbd0;font-size:12px;margin-right:auto}
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
.tabs button.on{background:var(--blue);border-color:var(--blue);color:#fff}.tabs .name{margin-left:8px;font-weight:600}
pre{margin:0;background:var(--code);color:var(--codeink);padding:12px 14px;height:420px;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap}
.err{color:var(--bad);padding:12px}
@media(max-width:820px){.hide-sm{display:none}main{padding:12px}}
</style></head><body>
<header><h1>__TITLE__</h1><span class="time" id="time"></span>
<button onclick="act('all','start')">모두 시작</button><button onclick="act('all','restart')">모두 재시작</button>
<button onclick="if(confirm('모든 서버를 끌까요?'))act('all','stop')">모두 중지</button></header>
<main>
<div class="card"><table><thead><tr><th>상태</th><th>서버</th><th>주소</th><th class="hide-sm">PID · 실행 시간</th><th>관리</th></tr></thead>
<tbody id="rows"><tr><td colspan="5" class="muted">불러오는 중…</td></tr></tbody></table><div id="err" class="err" hidden></div></div>
<div class="card" id="panel" hidden><div class="tabs"><button id="tab-log" class="on">로그</button><span class="name" id="pname"></span>
<span class="muted" style="margin-left:auto" id="phint"></span></div>
<pre id="log"></pre></div>
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
<button onclick="pick('${s.key}')">로그</button><button ${s.state!=='running'?'disabled':''} onclick="window.open('${esc(s.url)}','_blank','noopener')" title="새 창으로 열기">화면</button></div></td></tr>`}).join('')}
async function refresh(){
  try{const r=await fetch('/api/status');const d=await r.json();if(d.error)throw new Error(d.error);
    servers=d.servers;document.getElementById('time').textContent='갱신 '+d.time.slice(11);
    document.getElementById('err').hidden=true;render();if(selected)loadLog()}
  catch(e){const el=document.getElementById('err');el.hidden=false;el.textContent='상태를 읽지 못했습니다: '+e.message}}
async function loadLog(){const pre=document.getElementById('log');const atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
  pre.textContent=await (await fetch('/api/log?key='+encodeURIComponent(selected))).text();if(atEnd)pre.scrollTop=pre.scrollHeight}
// 하단 패널은 로그만 보여 준다. 서버 화면은 '화면' 버튼으로 새 창에서 연다.
function pick(key){selected=key;const s=servers.find(x=>x.key===key);if(!s)return;document.getElementById('panel').hidden=false;
  document.getElementById('pname').textContent=s.title;document.getElementById('phint').textContent='data/logs/'+s.key+'.log';loadLog();render()}
refresh();setInterval(refresh,3000);
</script></body></html>"""


def main():
    flags = set(sys.argv[1:])
    ip, port, _ = site_url(HUB_SITE, HUB_PORT)
    try:
        server = local_server(HUB_PORT, Handler, HUB_SITE)
    except SystemExit:
        owner = next((pid for pid, addrs in listeners().items() if f"{ip}:{port}" in addrs), None)
        log(f"{HUB_TITLE} 를 띄우지 못했습니다: {ip}:{port} 를 이미 사용 중입니다"
            + (f" (PID {owner}). 이미 떠 있는 허브라면 브라우저로 http://{HUB_SITE} 를 열거나, "
               f"끄고 다시 실행하세요: taskkill /PID {owner} /T /F" if owner else "."))
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
        open_chrome(server.url)
    log("요청을 기다립니다. 끄려면 Ctrl+C (서버들은 계속 실행됨)")
    try: server.serve_forever()
    except KeyboardInterrupt: log("Ctrl+C: 허브를 끕니다")
    finally: server.server_close()


if __name__ == "__main__": main()
