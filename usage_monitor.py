"""Local dashboard for Claude / Codex remaining usage limits.

- Claude: ~/.claude/.credentials.json 의 OAuth 토큰으로 사용량 API(/api/oauth/usage)를 조회한다.
  비공개 API라 응답 형식이 바뀌거나 막힐 수 있다. 토큰은 이 서버 밖으로 내보내지 않는다.
- Codex: ~/.codex/sessions 로그의 마지막 token_count 이벤트에서 한도 사용률을 읽는다.
- 두 도구 모두 로컬 세션 기록에서 최근 5시간 / 7일 토큰 사용량을 집계한다.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from wiki_search import open_chrome

HOME = Path.home()
CLAUDE_CREDENTIALS = HOME / ".claude" / ".credentials.json"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_CACHE_SECONDS = 60
WINDOWS = {"5h": 5 * 3600, "7d": 7 * 86400}


CLAUDE_LABELS = {
    "five_hour": "5시간 세션",
    "seven_day": "주간 (전체 모델)",
    "seven_day_opus": "주간 (Opus)",
    "seven_day_sonnet": "주간 (Sonnet)",
    "seven_day_oauth_apps": "주간 (OAuth 앱)",
}

_api_cache: dict = {"at": 0.0, "value": None}
_talentlab_cache: dict = {"at": 0.0, "value": None}


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def empty_totals() -> dict:
    return {k: {"total": 0, "output": 0, "requests": 0} for k in WINDOWS}


def add_usage(totals: dict, ts: float, now: float, total: int, output: int):
    for key, span in WINDOWS.items():
        if now - ts <= span:
            totals[key]["total"] += total
            totals[key]["output"] += output
            totals[key]["requests"] += 1


# ---------------------------------------------------------------- Claude

def claude_limits() -> dict:
    now = time.time()
    if _api_cache["value"] and now - _api_cache["at"] < API_CACHE_SECONDS:
        return _api_cache["value"]
    result = {"limits": [], "plan": None, "error": None}
    try:
        oauth = json.loads(CLAUDE_CREDENTIALS.read_text(encoding="utf-8"))["claudeAiOauth"]
        result["plan"] = oauth.get("subscriptionType")
        if oauth.get("expiresAt", 0) / 1000 < now:
            raise PermissionError("OAuth 토큰이 만료되었습니다. Claude Code를 한 번 실행하면 갱신됩니다.")
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
        for key, value in data.items():
            if isinstance(value, dict) and value.get("utilization") is not None:
                result["limits"].append({
                    "id": key,
                    "label": CLAUDE_LABELS.get(key, key),
                    "used": float(value["utilization"]),
                    "resets_at": value.get("resets_at"),
                })
        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled") and extra.get("utilization") is not None:
            result["limits"].append({"id": "extra_usage", "label": "추가 사용량 (월)",
                                     "used": float(extra["utilization"]), "resets_at": None})
    except FileNotFoundError:
        result["error"] = f"자격 증명 파일이 없습니다: {CLAUDE_CREDENTIALS}"
    except PermissionError as exc:
        result["error"] = str(exc)
    except urllib.error.HTTPError as exc:
        result["error"] = ("인증 실패(401): Claude Code를 다시 실행해 토큰을 갱신하세요." if exc.code == 401
                           else f"사용량 API 오류: HTTP {exc.code}")
    except Exception as exc:  # 네트워크 오류, 응답 형식 변경 등
        result["error"] = f"사용량 조회 실패: {exc}"
    _api_cache.update(at=now, value=result)
    return result


def claude_local_usage(now: float) -> dict:
    totals = empty_totals()
    seen = set()
    last = None
    if CLAUDE_PROJECTS.is_dir():
        for file in CLAUDE_PROJECTS.rglob("*.jsonl"):
            if now - file.stat().st_mtime > WINDOWS["7d"]:
                continue
            with file.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = row.get("message") or {}
                    usage = msg.get("usage")
                    if row.get("type") != "assistant" or not usage or not row.get("timestamp"):
                        continue
                    # 스트리밍 중 같은 응답이 여러 줄로 기록되므로 메시지 단위로 한 번만 센다.
                    key = (msg.get("id"), row.get("requestId"))
                    if key in seen:
                        continue
                    seen.add(key)
                    ts = parse_time(row["timestamp"])
                    last = max(last or ts, ts)
                    output = usage.get("output_tokens", 0) or 0
                    total = output + sum(usage.get(k, 0) or 0 for k in
                                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    add_usage(totals, ts, now, total, output)
    return {"windows": totals, "last_activity": iso(last)}


# ---------------------------------------------------------------- Codex

def codex_usage(now: float) -> dict:
    totals = empty_totals()
    latest = None  # (ts, rate_limits)
    last = None
    if CODEX_SESSIONS.is_dir():
        for file in CODEX_SESSIONS.rglob("*.jsonl"):
            recent = now - file.stat().st_mtime <= WINDOWS["7d"]
            if not recent and latest:
                continue
            prev_total = None
            with file.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = row.get("payload") or {}
                    if payload.get("type") != "token_count" or not row.get("timestamp"):
                        continue
                    ts = parse_time(row["timestamp"])
                    if payload.get("rate_limits") and (not latest or ts > latest[0]):
                        latest = (ts, payload["rate_limits"])
                    info = payload.get("info") or {}
                    cumulative = (info.get("total_token_usage") or {}).get("total_tokens")
                    # 같은 누적값이 반복 기록되면 새 요청이 아니므로 건너뛴다.
                    if cumulative is None or cumulative == prev_total:
                        continue
                    prev_total = cumulative
                    turn = info.get("last_token_usage") or {}
                    last = max(last or ts, ts)
                    add_usage(totals, ts, now, turn.get("total_tokens", 0) or 0, turn.get("output_tokens", 0) or 0)

    result = {"limits": [], "plan": None, "as_of": None, "error": None,
              "local": {"windows": totals, "last_activity": iso(last)}}
    if not latest:
        result["error"] = f"Codex 세션 기록이 없습니다: {CODEX_SESSIONS}"
        return result
    ts, limits = latest
    result["plan"] = limits.get("plan_type")
    result["as_of"] = iso(ts)
    for key, label in (("primary", None), ("secondary", None)):
        window = limits.get(key)
        if not window:
            continue
        minutes = window.get("window_minutes") or 0
        label = "5시간 세션" if minutes == 300 else "주간" if minutes == 10080 else f"{minutes // 60}시간 창"
        resets_at = window.get("resets_at")
        expired = bool(resets_at and resets_at < now)
        result["limits"].append({
            "id": key,
            "label": label,
            # 리셋 시각이 지났으면 기록 이후 사용분이 없으므로 0%로 본다.
            "used": 0.0 if expired else float(window.get("used_percent") or 0),
            "resets_at": None if expired else iso(resets_at),
            "note": "기록 이후 리셋됨" if expired else None,
        })
    return result


# ---------------------------------------------------------------- AI Talent Lab / Azure OpenAI gateway

def talentlab_usage() -> dict:
    """Read a gateway-provided quota endpoint when one is explicitly configured.

    Azure OpenAI-compatible inference endpoints do not expose account quota through
    the normal chat/embedding API.  The monitor therefore never guesses a private
    route or sends a billable inference request.  Set TALENTLAB_USAGE_URL when the
    service owner provides a read-only usage endpoint.
    """
    now = time.time()
    if _talentlab_cache["value"] and now - _talentlab_cache["at"] < API_CACHE_SECONDS:
        return _talentlab_cache["value"]

    result = {
        "endpoint": TALENTLAB_ENDPOINT or None,
        "usage_url": TALENTLAB_USAGE_URL or None,
        "limits": [],
        "raw": None,
        "error": None,
    }
    if not TALENTLAB_ENDPOINT:
        result["error"] = "AZURE_OPENAI_ENDPOINT가 설정되지 않았습니다."
    elif not TALENTLAB_USAGE_URL:
        result["error"] = (
            "Azure OpenAI 호환 API에는 표준 잔여량 조회가 없습니다. "
            "서비스 관리자에게 읽기 전용 사용량 URL을 받아 TALENTLAB_USAGE_URL로 설정하세요."
        )
    else:
        parsed = urlparse(TALENTLAB_USAGE_URL)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            result["error"] = "TALENTLAB_USAGE_URL은 http(s) URL이어야 합니다."
        else:
            try:
                headers = {"Accept": "application/json"}
                if TALENTLAB_USAGE_TOKEN:
                    headers[TALENTLAB_USAGE_AUTH_HEADER or "api-key"] = TALENTLAB_USAGE_TOKEN
                req = urllib.request.Request(TALENTLAB_USAGE_URL, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                result["raw"] = payload if isinstance(payload, dict) else {"value": payload}
                result["limits"] = normalize_talentlab_limits(payload)
                if not result["limits"]:
                    result["error"] = "응답에서 잔여량 필드를 찾지 못했습니다."
            except urllib.error.HTTPError as exc:
                result["error"] = f"사용량 API 오류: HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError) as exc:
                result["error"] = f"사용량 API 연결 실패: {exc.reason if hasattr(exc, 'reason') else exc}"
            except (json.JSONDecodeError, UnicodeDecodeError):
                result["error"] = "사용량 API가 JSON 응답을 반환하지 않았습니다."
            except Exception as exc:
                result["error"] = f"사용량 조회 실패: {exc}"
    _talentlab_cache.update(at=now, value=result)
    return result


def normalize_talentlab_limits(payload) -> list[dict]:
    """Normalize common quota response shapes into the dashboard shape."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("limits") or payload.get("quotas") or payload.get("usage")
    if isinstance(rows, dict):
        rows = [{"id": key, **value} for key, value in rows.items() if isinstance(value, dict)]
    if not isinstance(rows, list):
        rows = [payload]
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        used = row.get("used_percent", row.get("utilization"))
        remaining = row.get("remaining_percent", row.get("remaining_percentage"))
        if used is None and remaining is not None:
            used = 100 - float(remaining)
        if used is None:
            continue
        try:
            used = max(0.0, min(100.0, float(used)))
        except (TypeError, ValueError):
            continue
        result.append({
            "id": str(row.get("id") or row.get("name") or f"quota-{index}"),
            "label": str(row.get("label") or row.get("name") or f"Talent Lab 한도 {index + 1}"),
            "used": used,
            "resets_at": row.get("resets_at") or row.get("reset_at"),
            "note": row.get("note"),
        })
    return result


def collect() -> dict:
    now = time.time()
    claude = claude_limits()
    return {
        "now": iso(now),
        "claude": {**claude, "local": claude_local_usage(now)},
        "codex": codex_usage(now),
    }


HTML_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Usage Monitor</title><style>
:root{--ink:#172033;--muted:#718096;--line:#e5eaf0;--panel:#fff;--bg:#eef2f7;--track:#e8edf3;--ok:#2f9e6b;--warn:#d98a1c;--bad:#d64545;--claude:#d97757;--codex:#10a37f}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px/1.6 "Segoe UI","Malgun Gothic",sans-serif;background:var(--bg)}
.top{height:58px;background:#13213a;color:#fff;display:flex;align-items:center;padding:0 24px;gap:14px}.mark{width:28px;height:28px;border-radius:8px;background:#42b883;display:grid;place-items:center;font-weight:800}.brand{font-size:16px;font-weight:700}.crumb{color:#aebbd0;font-size:12px}.top .sp{flex:1}
button,select{font:inherit;border:1px solid #33476b;background:transparent;color:#fff;border-radius:8px;padding:5px 12px;cursor:pointer}button:hover,select:hover{background:#1f3152}select option{color:var(--ink);background:#fff}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 60px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;border-top:4px solid var(--c)}
.head{display:flex;align-items:center;gap:10px;margin-bottom:14px}.logo{width:30px;height:30px;border-radius:8px;background:var(--c);color:#fff;display:grid;place-items:center;font-weight:800}.head h2{margin:0;font-size:18px}.plan{margin-left:auto;font-size:12px;background:#f1f4f8;border-radius:999px;padding:2px 10px;color:var(--muted);text-transform:capitalize}
.limit{padding:12px 0;border-top:1px solid var(--line)}.limit:first-of-type{border-top:0}
.row{display:flex;align-items:baseline;gap:8px}.lbl{font-weight:600}.left{margin-left:auto;font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}.left small{font-size:12px;color:var(--muted);font-weight:500;margin-left:2px}
.bar{height:10px;background:var(--track);border-radius:5px;overflow:hidden;margin:6px 0 4px}.bar i{display:block;height:100%;border-radius:5px;transition:width .4s}
.sub{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.err{background:#fdecec;color:#9b2c2c;border-radius:8px;padding:10px 12px;font-size:13px}
h3{font-size:13px;color:var(--muted);margin:18px 0 6px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:6px 4px;border-bottom:1px solid var(--line);text-align:right}th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:500}
.foot{color:var(--muted);font-size:12px;margin-top:10px}.status{color:var(--muted);font-size:12px;text-align:right;margin-bottom:10px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header class="top"><div class="mark">U</div><div class="brand">AI Usage Monitor</div><div class="crumb">Claude · Codex 잔여 한도</div><div class="sp"></div><select id="refreshInterval" title="자동 갱신 주기"><option value="5">5분</option><option value="15">15분</option><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option><option value="0">수동</option></select><button id="refresh">새로고침</button></header>
<div class="wrap"><div class="status" id="status">불러오는 중…</div><div class="grid">
<section class="card" style="--c:var(--claude)" id="claude"></section>
<section class="card" style="--c:var(--codex)" id="codex"></section>
</div></div>
<script>
const $=s=>document.querySelector(s);let data=null,refreshTimer=null;const refreshKey='ai-usage-monitor-refresh-minutes';
function refreshMinutes(){return Number($('#refreshInterval').value)||0}
function refreshText(){const m=refreshMinutes();return m?m+'분마다 자동 갱신':'수동 갱신'}
function configureRefresh(){if(refreshTimer)clearInterval(refreshTimer);const m=refreshMinutes();localStorage.setItem(refreshKey,String(m));if(m)refreshTimer=setInterval(load,m*60000)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmtNum(n){return n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n)}
function until(iso){if(!iso)return '';let s=Math.max(0,(new Date(iso)-Date.now())/1000);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'일 ':'')+(d||h?h+'시간 ':'')+m+'분 후 리셋'}
function clock(iso){return iso?new Date(iso).toLocaleString('ko-KR',{month:'numeric',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit'}):''}
function ago(iso){if(!iso)return '기록 없음';const m=Math.round((Date.now()-new Date(iso))/60000);return m<1?'방금 전':m<60?m+'분 전':m<1440?Math.round(m/60)+'시간 전':Math.round(m/1440)+'일 전'}
function color(used){return used>=90?'var(--bad)':used>=70?'var(--warn)':'var(--ok)'}
function limits(list){return list.map(l=>{const left=Math.max(0,100-l.used);return `<div class="limit"><div class="row"><span class="lbl">${esc(l.label)}</span><span class="left">${left.toFixed(0)}%<small>남음</small></span></div><div class="bar"><i style="width:${Math.min(100,l.used)}%;background:${color(l.used)}"></i></div><div class="sub"><span>${l.used.toFixed(0)}% 사용${l.note?' · '+esc(l.note):''}</span><span>${l.resets_at?esc(until(l.resets_at))+' · '+esc(clock(l.resets_at)):''}</span></div></div>`}).join('')}
function local(lc){const w=lc.windows;return `<h3>로컬 기록 기준 토큰 사용량</h3><table><tr><th>구간</th><th>요청</th><th>출력 토큰</th><th>총 토큰(캐시 포함)</th></tr>${[['최근 5시간','5h'],['최근 7일','7d']].map(([n,k])=>`<tr><td>${n}</td><td>${w[k].requests}</td><td>${fmtNum(w[k].output)}</td><td>${fmtNum(w[k].total)}</td></tr>`).join('')}</table><div class="foot">마지막 사용: ${esc(ago(lc.last_activity))}</div>`}
function card(el,name,logo,d,extra){el.innerHTML=`<div class="head"><div class="logo">${logo}</div><h2>${name}</h2>${d.plan?`<span class="plan">${esc(d.plan)}</span>`:''}</div>${d.error?`<div class="err">${esc(d.error)}</div>`:''}${limits(d.limits||[])}${extra||''}${local(d.local)}`}
function render(){if(!data)return;card($('#claude'),'Claude','C',data.claude,'');
card($('#codex'),'Codex','X',data.codex,data.codex.as_of?`<div class="foot">한도 기준 시각: ${esc(clock(data.codex.as_of))} (${esc(ago(data.codex.as_of))}, 마지막 Codex 응답)</div>`:'')}
async function load(){$('#status').textContent='불러오는 중…';try{const r=await fetch('/api/usage');data=await r.json();render();$('#status').textContent='업데이트: '+new Date().toLocaleTimeString('ko-KR')+' · '+refreshText()}catch(e){$('#status').textContent='불러오기 실패: '+e.message}}
$('#refresh').onclick=load;const saved=localStorage.getItem(refreshKey);if(saved!==null&&[5,15,30,60,120,0].includes(Number(saved)))$('#refreshInterval').value=saved;$('#refreshInterval').onchange=()=>{configureRefresh();load()};configureRefresh();load();setInterval(render,30000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_body(self, data: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_body(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/usage":
            self.send_body(json.dumps(collect(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 8767
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"AI usage monitor: http://127.0.0.1:{port}")
    if "--no-browser" not in sys.argv:
        open_chrome(f"http://127.0.0.1:{port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
