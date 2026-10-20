"""Local viewer for Squid access logs on the Azure proxy VMs (read-only).

- 프록시 VM 목록은 az 로 리소스 그룹(wjy-proxy-rg)에서 tags.proxy=squid 인 VM 의 공용 IP 를 찾는다.
  --host 1.2.3.4 로 직접 지정할 수도 있다.
- 로그는 SSH(azureuser + 이 PC 의 기본 키)로 squid 컨테이너의 /var/log/squid/access.log 끝부분을 읽는다.
- 화면에서 허용/차단 필터, 검색, 도메인별 집계, 자동 새로고침을 제공한다.

사용 예:
    python proxy_tools/proxy_log_viewer.py                 # http://127.0.0.1:8768
    python proxy_tools/proxy_log_viewer.py --host <VM_IP>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR  # noqa: E402
from common.server import local_server, open_chrome  # noqa: E402

RESOURCE_GROUP = "wjy-proxy-rg"
SSH_USER = "azureuser"
LOG_PATH = "/var/log/squid/access.log"
MAX_LINES = 5000
# VM 에서 squid 컨테이너를 띄운 런타임. wjy-proxy01 은 podman(Quadlet), 예전 구성은 docker.
RUNTIME = os.environ.get("SQUID_RUNTIME") if os.environ.get("SQUID_RUNTIME") in ("docker", "podman") else "podman"

HOSTS: list[dict] = []  # [{"name": "wjy-proxy01", "ip": "203.0.113.20"}]


def discover_hosts(resource_group: str) -> list[dict]:
    az = shutil.which("az")
    if not az:
        raise SystemExit("az 를 찾을 수 없습니다. --host 로 프록시 IP 를 지정하세요.")
    res = subprocess.run([az, "vm", "list", "-g", resource_group, "-d", "-o", "json",
                          "--query", "[?tags.proxy=='squid' || tags.proxy=='nginx']"
                                     ".{name:name, ip:publicIps, state:powerState, kind:tags.proxy}"],
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


def parse_nginx(line: str) -> dict | None:
    # azure_proxy_vm.NGINX_LOG_FORMAT: time|client|host|method|uri|status|bytes|request_time|upstream_addr|upstream_status
    parts = line.strip().split("|")
    if len(parts) != 10:
        return None
    t, client, host, method, uri, status, size, rtime, upstream, _ = parts
    try:
        ts = datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None
    proxied = upstream not in ("", "-")
    denied = status == "403" and not proxied  # allow/deny 규칙에 걸려 nginx 가 바로 거절
    return {
        "ts": ts,
        "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "ms": int(float(rtime) * 1000) if rtime.replace(".", "", 1).isdigit() else 0,
        "client": client,
        "code": "PROXY" if proxied else ("DENIED" if denied else "LOCAL"),
        "status": status,
        "bytes": int(size) if size.isdigit() else 0,
        "method": method,
        "url": f"{host}{uri}",
        "host": host,
        "peer": upstream,
        "denied": denied,
    }


LOCAL_SIM = DATA_DIR / "sim_access.log"  # proxy_log_detect.py simulate 결과
REMOTE_SIM = "sim_access.log"  # simulate --upload 로 올린 VM 의 ~/sim_access.log


def read_logs(ip: str, lines: int, source: str = "real", kind: str = "squid") -> dict:
    """source: real(VM 의 실제 로그) | sim(VM 의 가상 로그) | local(이 PC 의 가상 로그 파일)
    kind: squid | nginx. nginx 는 접속 로그를 표준 출력으로 내므로 podman logs 로 읽는다(가상 로그는 squid 형식만)."""
    lines = max(1, min(lines, MAX_LINES))
    parser = parse_line
    if kind == "nginx":
        if source != "real":
            return {"error": "가상 로그는 squid 형식만 있습니다. nginx 프록시는 '실제 로그'로 보세요."}
        parser = parse_nginx
        # 표준 오류(nginx error log)는 버리고 접속 로그만 읽는다. 알림 줄이 섞이므로 넉넉히 가져온다.
        remote = f"sudo {RUNTIME} logs --tail {lines * 2} nginx 2>/dev/null"
        try:
            res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                  "-o", "StrictHostKeyChecking=accept-new", f"{SSH_USER}@{ip}", remote],
                                 capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return {"error": f"{ip} SSH 응답 없음(30초). VM 이 켜져 있는지, 이 PC IP 가 NSG 에 허용됐는지 확인하세요."}
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            return {"error": f"{ip} 로그 읽기 실패: {err or 'exit ' + str(res.returncode)}"}
        text = res.stdout.decode("utf-8", errors="replace")
    elif source == "local":
        if not LOCAL_SIM.is_file():
            return {"error": f"{LOCAL_SIM.name} 이 없습니다. python proxy_tools/proxy_log_detect.py simulate 로 먼저 만드세요."}
        text = "\n".join(LOCAL_SIM.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    else:
        remote = (f"tail -n {lines} {REMOTE_SIM}" if source == "sim"
                  else f"sudo {RUNTIME} exec squid tail -n {lines} {LOG_PATH}")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
               f"{SSH_USER}@{ip}", remote]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return {"error": f"{ip} SSH 응답 없음(30초). VM 이 켜져 있는지, 이 PC IP 가 NSG 에 허용됐는지 확인하세요."}
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            if source == "sim" and "No such file" in err:
                err = "VM 에 가상 로그가 없습니다. python proxy_tools/proxy_log_detect.py simulate --upload <IP> 로 올리세요."
            return {"error": f"{ip} 로그 읽기 실패: {err or 'exit ' + str(res.returncode)}"}
        text = res.stdout.decode("utf-8", errors="replace")
    rows = [r for r in map(parser, text.splitlines()) if r][-lines:]
    rows.sort(key=lambda r: r["ts"], reverse=True)  # 최신이 위로
    from proxy_log_detect import ALLOWED_CLIENTS, WHITELIST, detect  # 순환 import 를 피하려고 여기서 불러온다
    findings = [asdict(f) for f in detect(rows, ALLOWED_CLIENTS, WHITELIST)]
    return {"rows": rows, "findings": findings, "fetched": datetime.now(timezone.utc).isoformat()}


def read_policy(ip: str, kind: str) -> dict:
    """VM 의 /etc/proxy 설정에서 허용 목록을 읽는다. squid: 허용 도메인 + 클라이언트, nginx: 가상 도메인 + 클라이언트."""
    remote = "cat /etc/proxy/proxy.conf; echo '---WHITELIST---'; cat /etc/proxy/lists/whitelist.txt 2>/dev/null"
    try:
        res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                              "-o", "StrictHostKeyChecking=accept-new", f"{SSH_USER}@{ip}", remote],
                             capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"error": f"{ip} SSH 응답 없음(30초)"}
    if res.returncode != 0:
        return {"error": f"{ip} 설정 읽기 실패: {res.stderr.decode('utf-8', errors='replace').strip()}"}
    conf, _, wl = res.stdout.decode("utf-8", errors="replace").partition("---WHITELIST---")
    lines = [ln.strip() for ln in conf.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if kind == "nginx":
        clients = [ln.split()[1].rstrip(";") for ln in lines if ln.startswith("allow ")]
        routes, name = [], None
        for ln in lines:
            if ln.startswith("server_name "):
                name = ln.split(None, 1)[1].rstrip(";")
            elif ln.startswith("set $upstream ") or (ln.startswith("proxy_pass ") and "$" not in ln):
                url = ln.split()[-1].rstrip(";")
                routes.append({"domain": "(기본: IP·기타 도메인)" if name == "_" else name, "target": url})
        return {"kind": kind, "clients": clients, "routes": routes}
    clients = [t for ln in lines if ln.startswith("acl allowed_clients src") for t in ln.split()[3:]]
    restricted = any(ln.startswith("acl whitelist dstdomain") for ln in lines)
    domains = [d.strip() for d in wl.splitlines() if d.strip() and not d.strip().startswith("#")]
    return {"kind": kind, "clients": clients, "restricted": restricted,
            "domains": [{"domain": d.lstrip("."), "subdomains": d.startswith(".")} for d in domains]}


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
.policy h3{font-size:12px;color:var(--muted);font-weight:500;margin:10px 0 4px}.policy h3:first-child{margin-top:0}.policy ul{list-style:none;margin:0;padding:0}.policy li{display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-top:1px solid var(--line);font-size:12.5px;word-break:break-all}.policy li:first-child{border-top:0}.policy li small{color:var(--muted);white-space:nowrap}.policy .note{color:var(--muted);font-size:12px}
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
<header class="top"><div class="mark">P</div><div class="brand">Proxy Log Viewer</div><div class="crumb">Squid · nginx 접속 로그 · 읽기 전용</div></header>
<div class="wrap">
<div class="bar">
<select id="src" title="로그 소스"><option value="real">실제 로그</option><option value="sim">가상 로그 (VM ~/sim_access.log)</option><option value="local">가상 로그 (로컬 파일)</option></select>
<select id="host"></select>
<label for="lines">표시 개수</label><select id="lines" aria-label="로그 표시 개수"><option>10</option><option selected>200</option><option>500</option><option>1000</option></select>
<div class="seg" id="seg"><button data-f="all" class="on">전체</button><button data-f="ok">허용</button><button data-f="bad">차단</button></div>
<input type="search" id="q" placeholder="도메인·클라이언트 IP·결과 코드 검색">
<label class="auto"><input type="checkbox" id="auto"> 5초 자동 새로고침</label>
<button id="refresh">새로고침</button>
</div>
<div id="err"></div>
<div class="summary" id="summary">불러오는 중…</div>
<div class="grid">
<div class="card"><div class="scroll"><table><thead><tr><th>시각</th><th>클라이언트</th><th>결과</th><th>메서드</th><th>대상</th><th class="num">크기</th><th class="num">ms</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<div><div class="card" style="margin-bottom:16px"><h2>허용 목록</h2><div id="policy" class="policy"><div class="empty" style="padding:10px 0">불러오는 중…</div></div></div>
<div class="card" style="margin-bottom:16px"><h2>탐지 결과</h2><div id="findings" class="findings"></div></div>
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
async function loadPolicy(){const host=$('#host').value;if(!host)return;const el=$('#policy');
try{const p=await (await fetch(`/api/policy?host=${encodeURIComponent(host)}`)).json();
if(p.error){el.innerHTML=`<div class="note">${esc(p.error)}</div>`;return}
const li=(a,b)=>`<li><span>${esc(a)}</span><small>${esc(b)}</small></li>`;let h='';
if(p.kind==='nginx'){h+='<h3>가상 도메인 → 연결 대상</h3><ul>'+(p.routes.map(r=>li(r.domain,r.target)).join('')||'<li class="note">없음</li>')+'</ul>'}
else{h+='<h3>접속 허용 도메인</h3>'+(p.restricted?'<ul>'+(p.domains.map(d=>li(d.domain,d.subdomains?'하위 도메인 포함':'해당 도메인만')).join('')||'<li class="note">비어 있음 (모두 차단)</li>')+'</ul>':'<div class="note">목적지 제한 없음 (모든 사이트 허용)</div>')}
h+='<h3>접속 허용 클라이언트</h3><ul>'+(p.clients.map(c=>li(c,'')).join('')||'<li class="note">제한 없음</li>')+'</ul>';el.innerHTML=h}
catch(e){el.innerHTML=`<div class="note">${esc(e.message)}</div>`}}
$('#q').oninput=render;$('#refresh').onclick=()=>{load();loadPolicy()};$('#host').onchange=()=>{load();loadPolicy()};$('#src').onchange=load;$('#lines').onchange=load;
$('#auto').onchange=e=>{clearInterval(timer);if(e.target.checked)timer=setInterval(load,5000)};
fetch('/api/hosts').then(r=>r.json()).then(hs=>{$('#host').innerHTML=hs.map(h=>`<option value="${esc(h.ip)}">${esc(h.name)} · ${esc(h.kind||'squid')} (${esc(h.ip)})${h.state&&h.state!=='VM running'?' - '+esc(h.state):''}</option>`).join('');
if(!hs.length){$('#summary').textContent='프록시 VM 이 없습니다.';return}load();loadPolicy()});
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
        elif url.path == "/api/policy":
            ip = (parse_qs(url.query).get("host") or [""])[0]
            host = next((h for h in HOSTS if h["ip"] == ip), None)
            if not host:
                self.send_json({"error": f"알 수 없는 호스트: {ip}"})
                return
            self.send_json(read_policy(ip, host.get("kind") or "squid"))
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
            kind = next((h.get("kind") or "squid" for h in HOSTS if h["ip"] == ip), "squid")
            self.send_json(read_logs(ip, lines, source, kind))
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


def main():
    global HOSTS
    p = argparse.ArgumentParser(description="Azure 프록시 VM 의 Squid 접속 로그를 브라우저로 본다.")
    p.add_argument("port", nargs="?", type=int, default=8768)
    p.add_argument("--host", action="append", help="프록시 공용 IP (여러 번 지정 가능). 생략하면 az 로 찾는다.")
    p.add_argument("--kind", choices=["squid", "nginx"], default="squid", help="--host 로 지정한 프록시의 종류")
    p.add_argument("--resource-group", default=RESOURCE_GROUP)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    if args.host:
        HOSTS = [{"name": ip, "ip": ip, "kind": args.kind} for ip in args.host]
    else:
        print(f"{args.resource_group} 에서 Squid 프록시 VM 을 찾는 중 ...", flush=True)
        HOSTS = discover_hosts(args.resource_group)
    for h in HOSTS:
        print(f"  {h['name']}  {h['ip']}  {h.get('state', '')}")

    server = local_server(args.port, Handler, "proxy-log.local")
    print(f"Proxy log viewer: {server.url}")
    if not args.no_browser:
        open_chrome(server.url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
