"""Local dashboard for daily Azure cost (Cost Management Query API, read-only).

- 인증은 az CLI 로그인 토큰(az account get-access-token)을 쓴다. 별도 키가 필요 없다.
- 필요한 권한: 구독에 대한 Cost Management Reader (또는 Reader 이상).
- 비용 데이터는 Azure 쪽에서 보통 8~24시간 늦게 반영된다. 오늘 값은 부분 집계다.
- Cost Management API 는 호출 빈도 제한이 있어 같은 조회는 5분간 캐시한다.

사용 예:
    python cost_monitor.py                    # http://127.0.0.1:8769
    python cost_monitor.py --subscription wonjy
"""

from __future__ import annotations

import argparse
import calendar
import json
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from wiki_search import local_server, open_chrome

API = "https://management.azure.com/subscriptions/{sub}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
CACHE_SECONDS = 300
GROUPS = {"rg": "ResourceGroupName", "service": "ServiceName", "resource": "ResourceId"}

SUBSCRIPTION: dict = {}
_token: dict = {"value": None, "expires": 0.0}
_cache: dict = {}
_lock = threading.Lock()


def az_json(*args: str):
    az = shutil.which("az")
    if not az:
        raise RuntimeError("az CLI 를 찾을 수 없습니다.")
    res = subprocess.run([az, *args, "-o", "json"], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", errors="replace").strip() or "az 실패")
    return json.loads(res.stdout)


def access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["expires"] - 120:
            return _token["value"]
        data = az_json("account", "get-access-token", "--resource", "https://management.azure.com",
                       "--subscription", SUBSCRIPTION["id"])
        _token["value"] = data["accessToken"]
        _token["expires"] = float(data.get("expires_on") or time.time() + 1800)
        return _token["value"]


def period(range_key: str) -> tuple[date, date]:
    today = date.today()
    if range_key == "last30":
        return today - timedelta(days=29), today
    if range_key == "lastmonth":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    return today.replace(day=1), today  # 이번 달


def query(range_key: str, group: str) -> dict:
    key = (range_key, group)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    start, end = period(range_key)
    body = {"type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
            "dataset": {"granularity": "Daily", "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": GROUPS[group]}]}}
    for attempt in range(3):
        req = Request(API.format(sub=SUBSCRIPTION["id"]), data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=60) as res:
                props = json.loads(res.read())["properties"]
            break
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 429:
                # 호출 한도 초과: Azure 가 알려 주는 대기 시간(초)만큼 쉬고 다시 시도한다.
                wait = max((int(v) for k, v in exc.headers.items()
                            if k.lower().endswith("retry-after") and str(v).isdigit()), default=20)
                if attempt < 2 and wait <= 60:
                    print(f"[cost] 429, {wait}초 후 재시도", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Cost Management 호출 한도 초과(429). 약 {wait}초 뒤 다시 시도하세요.")
            if exc.code in (401, 403):
                raise RuntimeError("권한 없음: 구독에 Cost Management Reader 이상 역할이 필요합니다. " + detail)
            raise RuntimeError(f"Cost Management 오류 HTTP {exc.code}: {detail}")
        except URLError as exc:
            raise RuntimeError(f"연결 실패: {exc.reason}")
    cols = [c["name"] for c in props["columns"]]
    rows = []
    for r in props["rows"]:
        rec = dict(zip(cols, r))
        name = rec.get(GROUPS[group]) or "(미지정)"
        if group == "resource":
            name = name.rsplit("/", 1)[-1]
        d = str(rec["UsageDate"])
        rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "group": name, "cost": float(rec["Cost"])})
    currency = next((dict(zip(cols, r)).get("Currency") for r in props["rows"]), "KRW")
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    result = {"rows": rows, "currency": currency, "from": str(start), "to": str(end), "range": range_key,
              "today": str(date.today()), "days_in_month": days_in_month,
              "fetched": datetime.now().strftime("%Y-%m-%d %H:%M"), "subscription": SUBSCRIPTION["name"]}
    _cache[key] = (time.time(), result)
    return result


HTML_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Azure Cost Monitor</title><style>
.viz-root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--other:#b5b3ab;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--other:#5f5e59}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--other:#5f5e59}
*{box-sizing:border-box}body{margin:0}
.viz-root{min-height:100vh;background:var(--page);color:var(--text-primary);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
.top{height:58px;background:#13213a;color:#fff;display:flex;align-items:center;padding:0 24px;gap:14px}.mark{width:28px;height:28px;border-radius:8px;background:#42b883;display:grid;place-items:center;font-weight:800}.brand{font-size:16px;font-weight:700}.crumb{color:#aebbd0;font-size:12px}
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.seg{display:flex}.seg button{font:inherit;border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);padding:6px 12px;cursor:pointer;margin-left:-1px}.seg button:first-child{border-radius:8px 0 0 8px;margin-left:0}.seg button:last-child{border-radius:0 8px 8px 0}.seg button.on{background:var(--text-primary);color:var(--surface-1)}
.bar .sp{flex:1}.meta{color:var(--muted);font-size:12px}
button.plain{font:inherit;border:1px solid var(--border);background:var(--surface-1);color:var(--text-primary);border-radius:8px;padding:6px 12px;cursor:pointer}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}.tile .k{color:var(--text-secondary);font-size:12px}.tile .v{font-size:24px;font-weight:650;margin-top:2px}.tile .s{color:var(--muted);font-size:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}.card h2{font-size:14px;margin:0 0 4px}.card .sub{color:var(--muted);font-size:12px;margin-bottom:8px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:6px 0 4px;font-size:12px;color:var(--text-secondary)}.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:10px;height:10px;border-radius:3px;display:inline-block}
.chart{position:relative}svg{display:block;width:100%;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.12);white-space:nowrap;display:none;z-index:2}.tip b{display:block;margin-bottom:2px}.tip .row{display:flex;gap:10px;justify-content:space-between}.tip i{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:6px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:7px 6px;border-bottom:1px solid var(--grid);text-align:left}th{color:var(--muted);font-weight:500}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
details summary{cursor:pointer;color:var(--text-secondary);font-size:13px;margin-top:4px}
.err{background:#fdecec;color:#9b2c2c;border-radius:8px;padding:10px 12px;font-size:13px;margin-top:12px}.empty{color:var(--muted);text-align:center;padding:40px 0}
@media(max-width:820px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><div class="viz-root">
<header class="top"><div class="mark">₩</div><div class="brand">Azure Cost Monitor</div><div class="crumb" id="sub">일별 비용 · 읽기 전용</div></header>
<div class="wrap">
<div class="bar">
<div class="seg" id="range"><button data-v="month" class="on">이번 달</button><button data-v="last30">최근 30일</button><button data-v="lastmonth">지난 달</button></div>
<div class="seg" id="group"><button data-v="rg" class="on">리소스 그룹</button><button data-v="service">서비스</button><button data-v="resource">리소스</button></div>
<div class="sp"></div><span class="meta" id="meta"></span><button class="plain" id="refresh">새로고침</button>
</div>
<div id="err"></div>
<div class="tiles" id="tiles"></div>
<div class="card"><h2>일별 비용</h2><div class="sub">비용은 보통 8~24시간 늦게 반영되며, 오늘 값은 부분 집계입니다.</div>
<div class="legend" id="legend"></div><div class="chart" id="chart"><div class="tip" id="tip"></div></div></div>
<div class="card"><h2 id="tblTitle">항목별 합계</h2><table><thead><tr><th>항목</th><th class="num">합계</th><th class="num">비중</th><th class="num">일평균</th><th class="num">최근일</th></tr></thead><tbody id="tbl"></tbody></table>
<details><summary>일별 표 보기</summary><table><thead><tr><th>날짜</th><th class="num">합계</th><th>가장 큰 항목</th></tr></thead><tbody id="daily"></tbody></table></details></div>
</div></div>
<script>
const $=s=>document.querySelector(s);const SLOTS=7;let state={range:'month',group:'rg'},data=null;
const colorOf={};let nextSlot=0; // 색은 항목(entity)을 따라간다: 한 번 정해진 색은 필터를 바꿔도 유지
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let fmt=v=>v.toLocaleString();
function money(cur){const f=new Intl.NumberFormat('ko-KR',{style:'currency',currency:cur,maximumFractionDigits:0});const f2=new Intl.NumberFormat('ko-KR',{style:'currency',currency:cur,maximumFractionDigits:2});return v=>Math.abs(v)<10&&v!==0?f2.format(v):f.format(v)}
// 로컬 날짜로 만든다(toISOString 은 UTC 라 한국 시간에서는 하루 밀린다)
const ymd=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
function dates(from,to){const out=[];for(let d=new Date(from+'T00:00:00');d<=new Date(to+'T00:00:00');d.setDate(d.getDate()+1))out.push(ymd(d));return out}
function niceMax(v){if(v<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(v)));const n=v/p;return(n<=1?1:n<=2?2:n<=5?5:10)*p}
function render(){if(!data)return;fmt=money(data.currency);
const days=dates(data.from,data.to);const tot={};for(const r of data.rows)tot[r.group]=(tot[r.group]||0)+r.cost;
const ranked=Object.keys(tot).sort((a,b)=>tot[b]-tot[a]).filter(g=>tot[g]>0);
const top=ranked.slice(0,SLOTS),rest=ranked.slice(SLOTS);const key=g=>top.includes(g)?g:'기타';
for(const g of top)if(!(g in colorOf)){colorOf[g]=nextSlot<SLOTS?`var(--s${++nextSlot})`:'var(--other)'}
const col=g=>g==='기타'?'var(--other)':colorOf[g];const series=[...top,...(rest.length?['기타']:[])];
const byDay={};for(const d of days)byDay[d]={};for(const r of data.rows){if(!byDay[r.date])continue;const k=key(r.group);byDay[r.date][k]=(byDay[r.date][k]||0)+r.cost}
const dayTot=d=>Object.values(byDay[d]).reduce((a,b)=>a+b,0);const total=days.reduce((a,d)=>a+dayTot(d),0);
// KPI
const today=data.today,last=[...days].reverse().find(d=>d<today&&dayTot(d)>0)||days[days.length-1];
const full=days.filter(d=>d<today),avg=full.length?full.reduce((a,d)=>a+dayTot(d),0)/full.length:total;
const tiles=[['기간 합계',fmt(total),`${data.from} ~ ${data.to}`],[`최근일 (${last.slice(5)})`,fmt(dayTot(last)),'반영 완료된 가장 최근 날짜'],['일평균',fmt(avg),`오늘 제외 ${full.length}일 기준`]];
if(data.range==='month'){// 최근 7일(오늘 제외) 평균으로 남은 날을 채운다. 오늘은 부분 집계라 평균과의 차이만큼 보탠다.
const recent=full.slice(-7),rate=recent.length?recent.reduce((a,d)=>a+dayTot(d),0)/recent.length:avg;
const left=data.days_in_month-full.length-1,todayCost=days.includes(today)?dayTot(today):0;
tiles.push(['월말 예상',fmt(total+Math.max(rate-todayCost,0)+rate*Math.max(left,0)),`최근 ${recent.length}일 평균 ${fmt(rate)} × 남은 일수 (단순 추정)`])}
else tiles.push(['항목 수',String(ranked.length),state.group==='rg'?'리소스 그룹':state.group==='service'?'서비스':'리소스']);
$('#tiles').innerHTML=tiles.map(t=>`<div class="tile"><div class="k">${t[0]}</div><div class="v">${t[1]}</div><div class="s">${esc(t[2])}</div></div>`).join('');
$('#meta').textContent=`${data.subscription} · ${data.currency} · ${data.fetched} 조회`;
// legend: >=2 series → 항상 표시, 값 포함(색만으로 구분하지 않도록)
$('#legend').innerHTML=series.length>1?series.map(g=>`<span><i style="background:${col(g)}"></i>${esc(g)} <b>${fmt(g==='기타'?rest.reduce((a,x)=>a+tot[x],0):tot[g])}</b></span>`).join(''):'';
// chart
const W=$('#chart').clientWidth||900,H=280,m={l:64,r:8,t:10,b:28},iw=W-m.l-m.r,ih=H-m.t-m.b;
const max=niceMax(Math.max(...days.map(dayTot),0));const y=v=>m.t+ih-v/max*ih;const band=iw/days.length,bw=Math.min(24,band*0.6);
let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="일별 비용 누적 막대">`;
for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);s+=`<line x1="${m.l}" x2="${W-m.r}" y1="${yy}" y2="${yy}" stroke="var(--${i?'grid':'axis'})" stroke-width="1"/><text x="${m.l-8}" y="${yy+4}" text-anchor="end" font-size="11" fill="var(--muted)" style="font-variant-numeric:tabular-nums">${fmt(v)}</text>`}
const every=Math.ceil(days.length/12);
days.forEach((d,i)=>{const cx=m.l+band*i+band/2,x=cx-bw/2;let acc=0;const segs=series.filter(g=>byDay[d][g]>0);
// 조각 사이 2px 표면 간격(위아래 1px씩), 맨 위 조각만 4px 둥근 끝, 바닥은 직각
segs.forEach((g,j)=>{const v=byDay[d][g],isTop=j===segs.length-1;const yb=y(acc)-(j?1:0),yt=y(acc+v)+(isTop?0:1);acc+=v;
const h=Math.max(yb-yt,0.6),r=isTop?Math.min(4,h,bw/2):0,t=yb-h;
const p=r?`M${x},${yb} V${t+r} Q${x},${t} ${x+r},${t} H${x+bw-r} Q${x+bw},${t} ${x+bw},${t+r} V${yb} Z`:`M${x},${yb} V${t} H${x+bw} V${yb} Z`;
s+=`<path d="${p}" fill="${col(g)}"/>`});
if(i%every===0||i===days.length-1)s+=`<text x="${cx}" y="${H-8}" text-anchor="middle" font-size="11" fill="var(--muted)">${d.slice(5)}</text>`;
s+=`<rect class="hit" data-d="${d}" x="${m.l+band*i}" y="${m.t}" width="${band}" height="${ih}" fill="transparent"/>`});
s+='</svg>';const tip=$('#tip');$('#chart').innerHTML=s;$('#chart').appendChild(tip);
$('#chart').querySelectorAll('.hit').forEach(el=>{el.onmousemove=e=>{const d=el.dataset.d;const rows=series.filter(g=>byDay[d][g]>0).map(g=>[g,byDay[d][g]]).sort((a,b)=>b[1]-a[1]);
tip.innerHTML=`<b>${d} · ${fmt(dayTot(d))}</b>`+(rows.length?rows.map(([g,v])=>`<div class="row"><span><i style="background:${col(g)}"></i>${esc(g)}</span><span>${fmt(v)}</span></div>`).join(''):'<div class="row">비용 없음</div>');
const b=$('#chart').getBoundingClientRect();let lx=e.clientX-b.left+14;if(lx+tip.offsetWidth>b.width)lx=e.clientX-b.left-tip.offsetWidth-14;tip.style.left=lx+'px';tip.style.top=Math.max(0,e.clientY-b.top-20)+'px';tip.style.display='block'};el.onmouseleave=()=>tip.style.display='none'});
// tables (색에 기대지 않는 표 보기)
const lastAll=g=>data.rows.filter(r=>r.group===g&&r.date===last).reduce((a,r)=>a+r.cost,0);
$('#tbl').innerHTML=ranked.length?ranked.map(g=>`<tr><td><i style="display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;background:${col(key(g))}"></i>${esc(g)}</td><td class="num">${fmt(tot[g])}</td><td class="num">${total?(tot[g]/total*100).toFixed(1):0}%</td><td class="num">${fmt(tot[g]/Math.max(days.length,1))}</td><td class="num">${fmt(lastAll(g))}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">비용 데이터가 없습니다.</td></tr>';
$('#daily').innerHTML=[...days].reverse().map(d=>{const big=Object.entries(byDay[d]).sort((a,b)=>b[1]-a[1])[0];return `<tr><td>${d}</td><td class="num">${fmt(dayTot(d))}</td><td>${big?esc(big[0]):'-'}</td></tr>`}).join('')}
async function load(){$('#refresh').disabled=true;$('#err').innerHTML='';
try{const r=await fetch(`/api/cost?range=${state.range}&group=${state.group}`);const d=await r.json();if(d.error){$('#err').innerHTML=`<div class="err">${esc(d.error)}</div>`;return}data=d;render()}
catch(e){$('#err').innerHTML=`<div class="err">${esc(e.message)}</div>`}finally{$('#refresh').disabled=false}}
for(const id of ['range','group'])document.querySelectorAll(`#${id} button`).forEach(b=>b.onclick=()=>{state[id]=b.dataset.v;document.querySelectorAll(`#${id} button`).forEach(x=>x.classList.toggle('on',x===b));load()});
$('#refresh').onclick=()=>fetch('/api/flush').then(load);window.onresize=()=>render();load();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_body(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
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
        elif url.path == "/api/cost":
            qs = parse_qs(url.query)
            range_key = (qs.get("range") or ["month"])[0]
            group = (qs.get("group") or ["rg"])[0]
            if range_key not in ("month", "last30", "lastmonth") or group not in GROUPS:
                self.send_json({"error": "잘못된 조회 조건"})
                return
            try:
                self.send_json(query(range_key, group))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)})
        elif url.path == "/api/flush":
            _cache.clear()
            self.send_json({"ok": True})
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


def main():
    p = argparse.ArgumentParser(description="Azure 일별 비용 대시보드")
    p.add_argument("port", nargs="?", type=int, default=8769)
    p.add_argument("--subscription", default=None, help="구독 이름 또는 ID (기본: az 의 현재 구독)")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    sub = az_json("account", "show", *(["--subscription", args.subscription] if args.subscription else []))
    SUBSCRIPTION.update(id=sub["id"], name=sub["name"])
    print(f"구독: {sub['name']} ({sub['id']})")
    server = local_server(args.port, Handler)
    print(f"Azure cost monitor: http://127.0.0.1:{args.port}")
    if not args.no_browser:
        open_chrome(f"http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
