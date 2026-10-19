"""Local hub: one page to start, stop and watch the local web servers (ai-master + ai-monitor).

    python hub.py                # http://ai-master.local (hosts 등록 시), 없으면 http://127.0.0.1:8764
    python hub.py --start-all    # 허브를 켜면서 꺼져 있는 서버를 모두 시작
    python hub.py --restart-all  # 허브를 켜면서 모든 서버를 다시 시작(중복 실행도 정리)
    python hub.py --no-browser

- 서버는 창 없이 띄우고 출력은 data/logs/<key>.log 에 남긴다. 허브를 꺼도 서버는 계속 돈다.
- 실행 여부는 프로세스 명령줄의 스크립트 이름으로 찾는다. 허브 밖에서 직접 띄운 서버도 보이고 끌 수 있다.
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

HUB_PORT, HUB_SITE = 8764, "ai-master.local"
MONITOR_DIR = Path(os.environ.get("AI_MONITOR_DIR", PROJECT_ROOT.parent / "ai-monitor"))
LOG_DIR = DATA_DIR / "logs"
LOG_ROTATE_BYTES = 2 * 1024 * 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACH = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

SERVERS = [s for s in [
    {"key": "wiki-viewer", "title": "Wiki 뷰어", "dir": PROJECT_ROOT, "script": "wiki_tools/wiki_viewer.py",
     "port": 8765, "site": "wiki.local"},
    {"key": "wiki-search", "title": "Wiki 검색", "dir": PROJECT_ROOT, "script": "wiki_tools/wiki_search.py",
     "port": 8766, "site": "wiki-search.local"},
    {"key": "usage-monitor", "title": "사용량 모니터", "dir": MONITOR_DIR, "script": "usage_monitor.py",
     "port": 8767, "site": "usage-monitor.local"},
    {"key": "proxy-log", "title": "프록시 로그 뷰어", "dir": PROJECT_ROOT, "script": "proxy_tools/proxy_log_viewer.py",
     "port": 8768, "site": "proxy-log.local"},
    {"key": "cost-monitor", "title": "비용 모니터", "dir": MONITOR_DIR, "script": "cost_monitor.py",
     "port": 8769, "site": "cost-monitor.local"},
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
    log = LOG_DIR / f"{s['key']}.log"
    if log.is_file() and log.stat().st_size > LOG_ROTATE_BYTES:
        try:
            log.replace(log.with_suffix(".old.log"))
        except OSError:
            pass  # 다른 프로세스가 쓰고 있으면 그대로 이어 쓴다
    cmd = [sys.executable, s["script"], "--no-browser"]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    with open(log, "ab") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {' '.join(cmd)} =====\n".encode("utf-8"))
        f.flush()
        subprocess.Popen(cmd, cwd=s["dir"], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, creationflags=DETACH, close_fds=True)


def stop(key: str):
    current = next(x for x in server_status(force=True) if x["key"] == key)
    for inst in current["instances"]:
        subprocess.run(["taskkill", "/PID", str(inst["pid"]), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        now = next(x for x in server_status(force=True) if x["key"] == key)
        if not now["instances"]:
            return
        time.sleep(0.5)


def run_action(key: str, action: str):
    s = BY_KEY[key]
    _busy[key] = {"start": "시작 중", "stop": "중지 중", "restart": "재시작 중"}[action]
    try:
        if action in ("stop", "restart"):
            stop(key)
        if action == "start" and next(x for x in server_status(force=True) if x["key"] == key)["instances"]:
            return  # 이미 떠 있으면 하나 더 띄우지 않는다
        if action in ("start", "restart"):
            start(s)
            time.sleep(1)
    finally:
        _busy.pop(key, None)
        _cache["at"] = 0.0


def dispatch(key: str, action: str) -> list[str]:
    keys = list(BY_KEY) if key == "all" else [key]
    keys = [k for k in keys if k in BY_KEY and k not in _busy]
    for k in keys:
        threading.Thread(target=run_action, args=(k, action), daemon=True).start()
    return keys


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
            self.send_body(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/status":
            try:
                self.send_json({"servers": server_status(), "time": datetime.now().isoformat(timespec="seconds")})
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
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
        if not self.trusted():
            self.send_json({"error": "forbidden"}, 403)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            self.send_json({"error": "bad json"}, 400)
            return
        key, action = body.get("key"), body.get("action")
        if action not in ("start", "stop", "restart") or (key != "all" and key not in BY_KEY):
            self.send_json({"error": "bad request"}, 400)
            return
        self.send_json({"accepted": dispatch(key, action)})

    def log_message(self, *_):
        pass


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Master 허브</title>
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
iframe{width:100%;height:640px;border:0;background:#fff;display:block}
.err{color:var(--bad);padding:12px}
@media(max-width:820px){.hide-sm{display:none}main{padding:12px}}
</style></head><body>
<header><h1>AI Master 서버 허브</h1><span class="time" id="time"></span>
<button onclick="act('all','start')">모두 시작</button><button onclick="act('all','restart')">모두 재시작</button>
<button onclick="if(confirm('모든 서버를 끌까요?'))act('all','stop')">모두 중지</button></header>
<main>
<div class="card"><table><thead><tr><th>상태</th><th>서버</th><th>주소</th><th class="hide-sm">PID · 실행 시간</th><th>관리</th></tr></thead>
<tbody id="rows"><tr><td colspan="5" class="muted">불러오는 중…</td></tr></tbody></table><div id="err" class="err" hidden></div></div>
<div class="card" id="panel" hidden><div class="tabs"><button id="tab-log" onclick="show('log')">로그</button>
<button id="tab-view" onclick="show('view')">화면</button><span class="name" id="pname"></span>
<span class="muted" style="margin-left:auto" id="phint"></span></div>
<pre id="log"></pre><iframe id="view" hidden></iframe></div>
</main>
<script>
let servers=[], selected=null, tab='log';
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
<button onclick="pick('${s.key}','log')">로그</button><button ${s.state!=='running'?'disabled':''} onclick="pick('${s.key}','view')">화면</button></div></td></tr>`}).join('')}
async function refresh(){
  try{const r=await fetch('/api/status');const d=await r.json();if(d.error)throw new Error(d.error);
    servers=d.servers;document.getElementById('time').textContent='갱신 '+d.time.slice(11);
    document.getElementById('err').hidden=true;render();if(selected&&tab==='log')loadLog()}
  catch(e){const el=document.getElementById('err');el.hidden=false;el.textContent='상태를 읽지 못했습니다: '+e.message}}
async function loadLog(){const pre=document.getElementById('log');const atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
  pre.textContent=await (await fetch('/api/log?key='+encodeURIComponent(selected))).text();if(atEnd)pre.scrollTop=pre.scrollHeight}
function pick(key,t){selected=key;document.getElementById('panel').hidden=false;show(t);render()}
function show(t){tab=t;const s=servers.find(x=>x.key===selected);if(!s)return;
  document.getElementById('pname').textContent=s.title;
  document.getElementById('tab-log').classList.toggle('on',t==='log');document.getElementById('tab-view').classList.toggle('on',t==='view');
  document.getElementById('log').hidden=t!=='log';const v=document.getElementById('view');v.hidden=t!=='view';
  document.getElementById('phint').textContent=t==='log'?'data/logs/'+s.key+'.log':s.url;
  if(t==='log')loadLog();else if(v.dataset.src!==s.url){v.src=s.url;v.dataset.src=s.url}}
refresh();setInterval(refresh,3000);
</script></body></html>"""


def main():
    flags = set(sys.argv[1:])
    server = local_server(HUB_PORT, Handler, HUB_SITE)
    print(f"Server hub: {server.url}")
    for s in server_status(force=True):
        print(f"  {s['title']:10} {s['state']:8} {s['url']}" + (f"  (중복 {len(s['instances'])}개)" if len(s["instances"]) > 1 else ""))
    if "--restart-all" in flags:
        print("모든 서버를 다시 시작합니다.")
        dispatch("all", "restart")
    elif "--start-all" in flags:
        print("꺼져 있는 서버를 시작합니다.")
        dispatch("all", "start")
    if "--no-browser" not in flags:
        open_chrome(server.url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
