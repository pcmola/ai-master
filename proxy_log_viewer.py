"""Local viewer for Squid access logs on the Azure proxy VMs (read-only).

- 프록시 VM 목록은 az 로 리소스 그룹(wjy-proxy-rg)에서 tags.proxy=squid 인 VM 의 공용 IP 를 찾는다.
  --host 1.2.3.4 로 직접 지정할 수도 있다.
- 로그는 SSH(azureuser + 이 PC 의 기본 키)로 squid 컨테이너의 /var/log/squid/access.log 끝부분을 읽는다.
- 화면에서 허용/차단 필터, 검색, 도메인별 집계, 자동 새로고침을 제공한다.

사용 예:
    python proxy_log_viewer.py                 # http://127.0.0.1:8768
    python proxy_log_viewer.py --host 72.155.73.113
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from wiki_search import open_chrome

RESOURCE_GROUP = "wjy-proxy-rg"
SSH_USER = "azureuser"
LOG_PATH = "/var/log/squid/access.log"
MAX_LINES = 5000

HOSTS: list[dict] = []  # [{"name": "wjy-proxy01", "ip": "72.155.73.113"}]


def discover_hosts(resource_group: str) -> list[dict]:
    az = shutil.which("az")
    if not az:
        raise SystemExit("az 를 찾을 수 없습니다. --host 로 프록시 IP 를 지정하세요.")
    res = subprocess.run([az, "vm", "list", "-g", resource_group, "-d", "-o", "json",
                          "--query", "[?tags.proxy=='squid'].{name:name, ip:publicIps, state:powerState}"],
                         capture_output=True)
    if res.returncode != 0:
        raise SystemExit("az vm list 실패. az login 상태를 확인하거나 --host 로 지정하세요.\n"
                         + res.stderr.decode("utf-8", errors="replace"))
    vms = [v for v in json.loads(res.stdout or b"[]") if v.get("ip")]
    return sorted(vms, key=lambda v: v["name"])


def parse_line(line: str) -> dict | None:
    # Squid 기본(native) 형식: time elapsed client code/status bytes method url user hierarchy/peer type
    parts = line.split()
    if len(parts) < 7:
        return None
    try:
        ts = float(parts[0])
    except ValueError:
        return None
    code, _, status = parts[3].partition("/")
    url = parts[6]
    host = url.rsplit(":", 1)[0] if parts[5] == "CONNECT" else (urlparse(url).hostname or url)
    return {
        "ts": ts,
        "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "ms": int(parts[1]) if parts[1].isdigit() else 0,
        "client": parts[2],
        "code": code,
        "status": status,
        "bytes": int(parts[4]) if parts[4].isdigit() else 0,
        "method": parts[5],
        "url": url,
        "host": host,
        "peer": parts[8] if len(parts) > 8 else "",
        "denied": "DENIED" in code,
    }


LOCAL_SIM = Path(__file__).resolve().parent / "sim_access.log"  # proxy_log_detect.py simulate 결과
REMOTE_SIM = "sim_access.log"  # simulate --upload 로 올린 VM 의 ~/sim_access.log


def read_logs(ip: str, lines: int, source: str = "real") -> dict:
    """source: real(VM 의 실제 squid 로그) | sim(VM 의 가상 로그) | local(이 PC 의 가상 로그 파일)"""
    lines = max(1, min(lines, MAX_LINES))
    if source == "local":
        if not LOCAL_SIM.is_file():
            return {"error": f"{LOCAL_SIM.name} 이 없습니다. python proxy_log_detect.py simulate 로 먼저 만드세요."}
        text = "\n".join(LOCAL_SIM.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    else:
        remote = (f"tail -n {lines} {REMOTE_SIM}" if source == "sim"
                  else f"sudo docker exec squid tail -n {lines} {LOG_PATH}")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
               f"{SSH_USER}@{ip}", remote]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return {"error": f"{ip} SSH 응답 없음(30초). VM 이 켜져 있는지, 이 PC IP 가 NSG 에 허용됐는지 확인하세요."}
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            if source == "sim" and "No such file" in err:
                err = "VM 에 가상 로그가 없습니다. python proxy_log_detect.py simulate --upload <IP> 로 올리세요."
            return {"error": f"{ip} 로그 읽기 실패: {err or 'exit ' + str(res.returncode)}"}
        text = res.stdout.decode("utf-8", errors="replace")
    rows = [r for r in map(parse_line, text.splitlines()) if r]
    rows.sort(key=lambda r: r["ts"], reverse=True)  # 최신이 위로
    from proxy_log_detect import ALLOWED_CLIENTS, WHITELIST, detect  # 순환 import 를 피하려고 여기서 불러온다
    findings = [asdict(f) for f in detect(rows, ALLOWED_CLIENTS, WHITELIST)]
    return {"rows": rows, "findings": findings, "fetched": datetime.now(timezone.utc).isoformat()}


HTML_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proxy Log Viewer</title><style>
:root{--ink:#172033;--muted:#718096;--line:#e5eaf0;--panel:#fff;--bg:#eef2f7;--ok:#2f9e6b;--bad:#d64545;--blue:#2563eb}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px/1.6 "Segoe UI","Malgun Gothic",sans-serif;background:var(--bg)}
.top{height:58px;background:#13213a;color:#fff;display:flex;align-items:center;padding:0 24px;gap:14px}.mark{width:28px;height:28px;border-radius:8px;background:#42b883;display:grid;place-items:center;font-weight:800}.brand{font-size:16px;font-weight:700}.crumb{color:#aebbd0;font-size:12px}
.wrap{max-width:1280px;margin:0 auto;padding:20px 16px 60px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
select,input,button{font:inherit;border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:#fff;color:var(--ink)}
input[type=search]{flex:1;min-width:180px}button{cursor:pointer}button.on{background:#13213a;color:#fff;border-color:#13213a}
.seg{display:flex}.seg button{border-radius:0}.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0}
label.auto{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}
.summary{margin:12px 2px;color:var(--muted);font-size:13px}.summary b{color:var(--ink)}.summary .bad{color:var(--bad)}
.findings .f{padding:8px 0;border-top:1px solid var(--line);font-size:12.5px}.findings .f:first-child{border-top:0}.findings .f b{display:block;font-size:13px}.findings .f small{color:var(--muted)}.findings .f a{cursor:pointer}
.sev{display:inline-block;padding:0 6px;border-radius:4px;font-size:10.5px;font-weight:700;margin-right:6px;vertical-align:1px}.sev.high{background:#d64545;color:#fff}.sev.medium{background:#f2a33a;color:#fff}.sev.low{background:#e2e8f0;color:#4a5568}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:16px;align-items:start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;overflow:hidden}.card h2{font-size:14px;margin:0 0 8px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}th,td{padding:6px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--muted);font-weight:500;position:sticky;top:0;background:#fff}
td.host{white-space:normal;word-break:break-all}td.num,th.num{text-align:right}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600}.tag.ok{background:#e3f5ec;color:#1e7a4f}.tag.bad{background:#fdecec;color:#b42318}
.scroll{max-height:calc(100vh - 230px);overflow:auto}
.err{background:#fdecec;color:#9b2c2c;border-radius:8px;padding:10px 12px;font-size:13px;margin-top:12px}.empty{color:var(--muted);text-align:center;padding:30px 0}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header class="top"><div class="mark">P</div><div class="brand">Proxy Log Viewer</div><div class="crumb">Squid access.log · 읽기 전용</div></header>
<div class="wrap">
<div class="bar">
<select id="src" title="로그 소스"><option value="real">실제 로그</option><option value="sim">가상 로그 (VM ~/sim_access.log)</option><option value="local">가상 로그 (로컬 파일)</option></select>
<select id="host"></select>
<select id="lines"><option>200</option><option selected>500</option><option>2000</option><option>5000</option></select>
<div class="seg" id="seg"><button data-f="all" class="on">전체</button><button data-f="ok">허용</button><button data-f="bad">차단</button></div>
<input type="search" id="q" placeholder="도메인·클라이언트 IP·결과 코드 검색">
<label class="auto"><input type="checkbox" id="auto"> 5초 자동 새로고침</label>
<button id="refresh">새로고침</button>
</div>
<div id="err"></div>
<div class="summary" id="summary">불러오는 중…</div>
<div class="grid">
<div class="card"><div class="scroll"><table><thead><tr><th>시각</th><th>클라이언트</th><th>결과</th><th>메서드</th><th>대상</th><th class="num">크기</th><th class="num">ms</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<div><div class="card" style="margin-bottom:16px"><h2>탐지 결과</h2><div id="findings" class="findings"></div></div>
<div class="card"><h2>도메인별</h2><table><thead><tr><th>도메인</th><th class="num">요청</th><th class="num">차단</th></tr></thead><tbody id="domains"></tbody></table></div></div>
</div></div>
<script>
const $=s=>document.querySelector(s);let data=[],findings=[],filter='all',timer=null;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const size=n=>n>=1048576?(n/1048576).toFixed(1)+' MB':n>=1024?(n/1024).toFixed(1)+' KB':n+' B';
const when=iso=>new Date(iso).toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
function render(){const q=$('#q').value.trim().toLowerCase();
const rows=data.filter(r=>(filter==='all'||(filter==='bad')===r.denied)&&(!q||[r.host,r.client,r.code,r.status,r.url].join(' ').toLowerCase().includes(q)));
const denied=data.filter(r=>r.denied).length,bytes=data.reduce((a,r)=>a+r.bytes,0);
$('#summary').innerHTML=`최근 <b>${data.length}</b>건 중 허용 <b>${data.length-denied}</b> · <span class="bad">차단 <b>${denied}</b></span> · 전송 <b>${size(bytes)}</b>`+(rows.length!==data.length?` · 표시 <b>${rows.length}</b>건`:'');
$('#rows').innerHTML=rows.length?rows.map(r=>`<tr><td>${when(r.time)}</td><td>${esc(r.client)}</td><td><span class="tag ${r.denied?'bad':'ok'}">${esc(r.code)}/${esc(r.status)}</span></td><td>${esc(r.method)}</td><td class="host" title="${esc(r.url)}">${esc(r.host)}</td><td class="num">${size(r.bytes)}</td><td class="num">${r.ms}</td></tr>`).join(''):'<tr><td colspan="7" class="empty">로그가 없습니다.</td></tr>';
const dom={};for(const r of data){const d=dom[r.host]||(dom[r.host]={n:0,bad:0});d.n++;if(r.denied)d.bad++}
$('#domains').innerHTML=Object.entries(dom).sort((a,b)=>b[1].n-a[1].n).slice(0,30).map(([h,d])=>`<tr><td class="host"><a href="#" data-h="${esc(h)}">${esc(h)}</a></td><td class="num">${d.n}</td><td class="num" style="color:${d.bad?'var(--bad)':'inherit'}">${d.bad}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">-</td></tr>';
const lbl={high:'HIGH',medium:'MED',low:'LOW'};$('#findings').innerHTML=findings.length?findings.map(f=>`<div class="f"><b><span class="sev ${f.severity}">${lbl[f.severity]}</span>${esc(f.title)}</b>${esc(f.detail)}<br><small>${esc(f.first===f.last?f.first:f.first+' ~ '+f.last)} · 관련 ${f.count}건</small></div>`).join(''):'<div class="empty" style="padding:10px 0">탐지된 이상 징후가 없습니다.</div>';
document.querySelectorAll('#domains a').forEach(a=>a.onclick=e=>{e.preventDefault();$('#q').value=a.dataset.h;render()})}
async function load(){const host=$('#host').value;if(!host)return;$('#refresh').disabled=true;
try{const r=await fetch(`/api/logs?host=${encodeURIComponent(host)}&lines=${$('#lines').value}&source=${$('#src').value}`);const d=await r.json();
if(d.error){$('#err').innerHTML=`<div class="err">${esc(d.error)}</div>`;data=[];findings=[];render();return}$('#err').innerHTML='';data=d.rows;findings=d.findings||[];render()}
catch(e){$('#err').innerHTML=`<div class="err">${esc(e.message)}</div>`}finally{$('#refresh').disabled=false}}
document.querySelectorAll('#seg button').forEach(b=>b.onclick=()=>{filter=b.dataset.f;document.querySelectorAll('#seg button').forEach(x=>x.classList.toggle('on',x===b));render()});
$('#q').oninput=render;$('#refresh').onclick=load;$('#host').onchange=load;$('#src').onchange=load;$('#lines').onchange=load;
$('#auto').onchange=e=>{clearInterval(timer);if(e.target.checked)timer=setInterval(load,5000)};
fetch('/api/hosts').then(r=>r.json()).then(hs=>{$('#host').innerHTML=hs.map(h=>`<option value="${esc(h.ip)}">${esc(h.name)} (${esc(h.ip)})${h.state&&h.state!=='VM running'?' - '+esc(h.state):''}</option>`).join('');
if(!hs.length){$('#summary').textContent='프록시 VM 이 없습니다.';return}load()});
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_body(self, data: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value):
        self.send_body(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_body(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/hosts":
            self.send_json(HOSTS)
        elif url.path == "/api/logs":
            qs = parse_qs(url.query)
            ip = (qs.get("host") or [""])[0]
            if ip not in {h["ip"] for h in HOSTS}:  # 등록된 프록시 외의 주소로 SSH 하지 않는다
                self.send_json({"error": f"알 수 없는 호스트: {ip}"})
                return
            lines = int((qs.get("lines") or ["500"])[0] or 500)
            source = (qs.get("source") or ["real"])[0]
            if source not in ("real", "sim", "local"):
                source = "real"
            self.send_json(read_logs(ip, lines, source))
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


def main():
    global HOSTS
    p = argparse.ArgumentParser(description="Azure 프록시 VM 의 Squid 접속 로그를 브라우저로 본다.")
    p.add_argument("port", nargs="?", type=int, default=8768)
    p.add_argument("--host", action="append", help="프록시 공용 IP (여러 번 지정 가능). 생략하면 az 로 찾는다.")
    p.add_argument("--resource-group", default=RESOURCE_GROUP)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    if args.host:
        HOSTS = [{"name": ip, "ip": ip} for ip in args.host]
    else:
        print(f"{args.resource_group} 에서 Squid 프록시 VM 을 찾는 중 ...", flush=True)
        HOSTS = discover_hosts(args.resource_group)
    for h in HOSTS:
        print(f"  {h['name']}  {h['ip']}  {h.get('state', '')}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Proxy log viewer: http://127.0.0.1:{args.port}")
    if not args.no_browser:
        open_chrome(f"http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
