"""Teams 승인 카드 대신 로컬 웹 화면으로 Yes/No 승인을 받고, 승인 뒤 처리 진행 상황과 결과 링크를 보여 준다.

- Approval: start() → wait() → (처리하면서) step() → finish() → close().
  승인한 브라우저 창은 /progress 로 바뀌어 단계별 진행을 1초마다 갱신하고, 끝나면 결과 메시지와
  '수정된 문서 열기' 같은 링크를 보여 준다. close() 는 브라우저가 최종 결과를 받아 갈 때까지(최대 linger 초) 기다린다.
- Teams 카드 버튼용 주소도 만든다: /decide?answer=yes|no&token=… 를 브라우저로 열면 바로 결정된다.
  token 은 요청마다 새로 만들어 다른 승인 요청의 버튼으로 결정되는 일을 막는다. 같은 PC 에서 눌러야 한다(127.0.0.1).
- 주소는 hosts 에 approval.local 이 있으면 그 이름으로, 없으면 http://127.0.0.1:8771 로 뜬다.
- request_approval() 은 진행 표시 없이 Yes/No 만 받는 간단한 형태다.

사용 예:
    python teams_tools/approval_local.py --title "Proxy 현황 변경 감지" "신규: wjy-proxy03" "삭제: wjy-prxy04"
    (종료 코드: 0 = Yes, 1 = No, 2 = 시간 초과)

    ap = Approval("Proxy 현황 변경 감지", ["신규: wjy-proxy03"], "현행화할까요?", links=[("문서", url)])
    page_url, yes_url, no_url = ap.start(open_browser=False)   # 여기서 Teams 카드를 보낸다
    if ap.wait(900):
        ap.step("Task 생성 중…"); ...; ap.step("Task #12 생성", "ok", ("Task 열기", task_url))
        ap.finish("완료했습니다. 열어 둔 문서는 새로고침하세요.", links=[("수정된 문서 열기", doc_url)])
    ap.close()
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.server import local_server, open_chrome  # noqa: E402

PORT = 8771

STYLE = """
body{margin:0;background:#f5f5f5;font:14px/1.6 "Segoe UI","Malgun Gothic",sans-serif;color:#242424}
.card{max-width:560px;margin:40px auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px #0002;overflow:hidden}
.head{background:#5b5fc7;color:#fff;padding:12px 18px;font-weight:600}.head small{opacity:.8;font-weight:400;margin-left:8px}
.body{padding:18px}h1{font-size:17px;margin:0 0 10px}ul{margin:0 0 14px;padding-left:20px}
.q{font-weight:600;margin-bottom:14px}.btns{display:flex;gap:10px;flex-wrap:wrap}
button,.btn{flex:1;padding:10px;border-radius:6px;border:1px solid #d1d1d1;font:inherit;font-weight:600;cursor:pointer;background:#fff;
 color:#242424;text-align:center;text-decoration:none}
button.yes,.btn.main{background:#5b5fc7;color:#fff;border-color:#5b5fc7}
.steps{list-style:none;padding:0;margin:0 0 14px}.steps li{padding:6px 0;border-bottom:1px solid #eee;display:flex;gap:8px}
.steps .ic{width:18px;text-align:center}.run .ic{animation:spin 1s linear infinite;display:inline-block}
.ok .ic{color:#1e7a4f}.fail .ic{color:#b42318}.steps a{margin-left:auto;font-size:13px}
.msg{padding:12px;border-radius:6px;margin-bottom:12px;font-weight:600}.msg.ok{background:#e3f5ec;color:#1e7a4f}
.msg.fail{background:#fdecec;color:#b42318}.msg.wait{background:#eef2ff;color:#3730a3}
.guess{border:1px solid #f5c26b;background:#fff8eb;border-radius:6px;padding:10px 12px;margin:0 0 14px;font-size:13px}
.guess b{display:block;margin-bottom:4px}.guess label{display:flex;gap:8px;align-items:center;padding:2px 0}
@keyframes spin{to{transform:rotate(360deg)}}
"""


def link_html(links: list[tuple[str, str]] | None) -> str:
    """확인용 링크(글자, URL). URL 이 없으면 글자만 회색으로 보인다(예: Azure DevOps Wiki 에 아직 없음)."""
    if not links:
        return ""
    parts = [f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(l)}</a>' if u
             else f'<span style="color:#888">{html.escape(l)}</span>' for l, u in links]
    return '<div style="margin:0 0 14px;font-size:13px">확인: ' + " · ".join(parts) + "</div>"


def shell(title: str, inner: str, script: str = "") -> bytes:
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{STYLE}</style></head><body><div class="card"><div class="head">{html.escape(title)}'
            f'<small>로컬 승인 화면 (Teams 대체)</small></div><div class="body">{inner}</div></div>'
            f'<script>{script}</script></body></html>').encode("utf-8")


def guess_html(candidates: list[str] | None) -> str:
    """이름 규칙으로만 찾은 VM. 프록시가 아니면 체크해서 빼고 승인한다(체크 안 한 VM 은 프록시로 확정)."""
    if not candidates:
        return ""
    boxes = "".join(f'<label><input type="checkbox" class="notproxy" value="{html.escape(n)}"> {html.escape(n)} '
                    f'<span style="color:#888">— 프록시 아님</span></label>' for n in candidates)
    return (f'<div class="guess"><b>프록시로 추정한 VM {len(candidates)}대 (이름 규칙으로 찾음, 한 번만 확인)</b>'
            f'프록시가 아닌 VM 이 있으면 체크하세요. Yes 를 누르면 체크하지 않은 VM 은 프록시로, 체크한 VM 은 '
            f'프록시 아님으로 Azure 태그를 붙여 다음부터 묻지 않습니다.{boxes}</div>')


def ask_page(title: str, items: list[str], question: str, links, candidates: list[str] | None = None) -> bytes:
    rows = "".join(f"<li>{html.escape(i)}</li>" for i in items) or "<li>(요약 없음)</li>"
    return shell("승인 요청", f"""<h1>{html.escape(title)}</h1><ul>{rows}</ul>{guess_html(candidates)}{link_html(links)}
<div class="q">{html.escape(question)}</div>
<div class="btns" id="btns"><button class="yes" onclick="send('yes')">Yes</button><button onclick="send('no')">No</button></div>""",
        """async function send(a){document.getElementById('btns').innerHTML='<div class="msg wait" style="flex:1">전송 중…</div>';
const exclude=[...document.querySelectorAll('.notproxy:checked')].map(c=>c.value);
const r=await fetch('/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:a,exclude})});
location.href=r.ok?'/progress':'/progress?late=1'}""")


def progress_page(title: str) -> bytes:
    # /status 를 1초마다 읽어 단계·결과를 그린다. 서버가 이미 닫혔으면 Teams 알림을 보라고 안내한다.
    return shell("처리 결과", f"""<h1>{html.escape(title)}</h1><div id="msg" class="msg wait">처리 중…</div>
<ul class="steps" id="steps"></ul><div class="btns" id="links"></div>""", """
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ic={run:'↻',ok:'✓',fail:'✗',info:'·'};let timer;
async function poll(){let d;try{d=await (await fetch('/status',{cache:'no-store'})).json()}
catch(e){clearInterval(timer);const m=document.getElementById('msg');if(m.classList.contains('wait')){m.className='msg fail';
m.textContent='진행 상태를 더 받을 수 없습니다(처리가 끝나 서버가 닫혔을 수 있음). Teams 결과 알림을 확인하세요.'}return}
document.getElementById('steps').innerHTML=d.steps.map(s=>`<li class="${s.state}"><span class="ic">${ic[s.state]||'·'}</span>
<span>${esc(s.text)}</span>${s.link?`<a href="${esc(s.link[1])}" target="_blank" rel="noopener">${esc(s.link[0])}</a>`:''}</li>`).join('');
const m=document.getElementById('msg');
if(d.done){clearInterval(timer);m.className='msg '+(d.ok?'ok':'fail');m.textContent=d.message;
document.getElementById('links').innerHTML=d.links.map((l,i)=>`<a class="btn${i?'':' main'}" href="${esc(l[1])}" target="_blank" rel="noopener">${esc(l[0])}</a>`).join('')}
else m.textContent=d.decision==='yes'?'승인했습니다. 처리 중…':d.decision==='no'?'거절했습니다. 정리 중…':'답을 기다리는 중…'}
poll();timer=setInterval(poll,1000);""")


def kill_port_owner(port: int, wait: float = 5.0) -> bool:
    """port 에서 수신 중인 다른 프로세스를 강제로 끈다. 끈 것이 있으면 True."""
    import time
    nw = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def owners() -> set[int]:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=30, creationflags=nw)
        return {int(c[4]) for c in (line.split() for line in out.stdout.decode("utf-8", "replace").splitlines())
                if len(c) == 5 and c[0] == "TCP" and c[3] == "LISTENING" and c[1].endswith(f":{port}")
                and c[4].isdigit() and int(c[4]) != os.getpid()}

    pids = owners()
    for pid in pids:
        print(f"[approval] {datetime.now():%H:%M:%S} 포트 {port} 를 잡고 있는 이전 프로세스(PID {pid})를 강제로 끕니다.", flush=True)
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30, creationflags=nw)
    deadline = time.monotonic() + wait
    while pids and owners() and time.monotonic() < deadline:
        time.sleep(0.25)
    return bool(pids)


class Approval:
    def __init__(self, title: str, items: list[str], question: str = "현행화할까요?",
                 links: list[tuple[str, str]] | None = None, candidates: list[str] | None = None):
        self.title, self.items, self.question, self.links = title, items, question, links
        self.candidates = candidates or []  # 확인받을 VM(프록시로 추정). 승인 화면에서 '프록시 아님'을 고를 수 있다
        self.excluded: list[str] = []
        self.token = secrets.token_urlsafe(9)
        self.decided, self.final_seen = threading.Event(), threading.Event()
        self.answer: bool | None = None
        self.state = {"decision": None, "steps": [], "done": False, "ok": True, "message": "", "links": []}
        self.lock = threading.Lock()
        self.server = None
        self.teams = False  # Teams 카드로 물었으면 True: 버튼을 누르면 짧은 확인 화면만 보이고 결과는 Teams 로 알린다

    # -- 결정
    def decide(self, value: str, exclude: list[str] | None = None) -> bool:
        """Teams 카드의 Yes 버튼은 exclude 없이 온다(추정 VM 을 모두 프록시로 확정)."""
        with self.lock:
            if value not in ("yes", "no") or self.decided.is_set():
                return False
            self.answer = value == "yes"
            self.excluded = [n for n in (exclude or []) if n in self.candidates]
            self.state["decision"] = value
            self.decided.set()
            return True

    # -- 진행 상황
    def step(self, text: str, state: str = "run", link: tuple[str, str] | None = None):
        """같은 문구로 시작하는 진행 중(run) 단계가 있으면 그 줄을 바꾸고, 없으면 새 줄을 더한다."""
        with self.lock:
            steps = self.state["steps"]
            if steps and steps[-1]["state"] == "run":
                steps[-1] = {"text": text, "state": state, "link": link}
            else:
                steps.append({"text": text, "state": state, "link": link})

    def steps_text(self) -> str:
        """진행 단계를 Teams 카드에 넣을 목록으로(✓ 완료, ✗ 실패, ↻ 진행 중, · 안내)."""
        icon = {"ok": "✓", "fail": "✗", "run": "↻", "info": "·"}
        with self.lock:
            return "\n".join(f"- {icon.get(s['state'], '·')} {s['text']}" for s in self.state["steps"])

    def finish(self, message: str, links: list[tuple[str, str]] | None = None, ok: bool = True):
        with self.lock:
            self.state.update(done=True, ok=ok, message=message, links=[list(l) for l in (links or []) if l[1]])

    # -- 서버
    def start(self, open_browser: bool = True) -> tuple[str, str, str]:
        ap = self
        ask = ask_page(self.title, self.items, self.question, self.links, self.candidates)
        progress = progress_page(self.title)

        class Handler(BaseHTTPRequestHandler):
            def reply(self, data: bytes, status: int = 200, kind: str = "text/html; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/status":
                    with ap.lock:
                        data = json.dumps(ap.state, ensure_ascii=False).encode("utf-8")
                        if ap.state["done"]:
                            ap.final_seen.set()
                    return self.reply(data, kind="application/json; charset=utf-8")
                if url.path == "/progress":
                    return self.reply(progress)
                if url.path == "/decide":  # Teams 카드 버튼
                    qs = parse_qs(url.query)
                    if (qs.get("token") or [""])[0] != ap.token:
                        return self.reply(shell("처리 결과", "<div class='msg fail'>만료되었거나 다른 승인 요청의 버튼입니다.</div>"), 403)
                    answer = (qs.get("answer") or [""])[0]
                    first = ap.decide(answer)
                    if ap.teams:  # 결과는 Teams 결과 카드로 가므로 여기서는 받았다는 것만 알린다
                        said = "Yes(승인)" if answer == "yes" else "No(반영 안 함)"
                        msg = (f"<div class='msg ok'>{said} 를 받았습니다.</div><p>처리 단계와 결과는 <b>Teams 결과 카드</b>로 알려 드립니다. "
                               "이 창은 닫아도 됩니다.</p>" if first else
                               "<div class='msg wait'>이미 처리된 승인 요청입니다.</div><p>결과는 Teams 결과 카드를 확인하세요.</p>")
                        return self.reply(shell("승인 접수", msg + "<p style='font-size:13px'><a href='/progress'>진행 상황 보기</a></p>"))
                    return self.reply(progress)
                if ap.decided.is_set():
                    return self.reply(progress)
                return self.reply(ask)

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    body = {}
                exclude = body.get("exclude") if isinstance(body.get("exclude"), list) else []
                ok = urlparse(self.path).path == "/decide" and ap.decide(body.get("answer"), [str(x) for x in exclude])
                self.reply(b"", 200 if ok else 409, "text/plain")

            def log_message(self, *_):
                pass

        try:
            self.server = local_server(PORT, Handler, "approval.local")
        except SystemExit:
            # 이전 실행이 승인 대기 중에 남아 포트를 잡고 있으면 그 프로세스를 끄고 다시 띄운다.
            if not kill_port_owner(PORT):
                raise
            self.server = local_server(PORT, Handler, "approval.local")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        url = self.server.url
        self.teams = not open_browser  # 브라우저를 열지 않는 경우 = Teams 카드로 묻는 경우
        print(f"[approval] {datetime.now():%H:%M:%S} 승인 대기: {url}", flush=True)
        if open_browser:
            open_chrome(url)
        return url, f"{url}/decide?answer=yes&token={self.token}", f"{url}/decide?answer=no&token={self.token}"

    def wait(self, timeout: float) -> bool | None:
        """Yes=True, No=False, 시간 초과=None."""
        self.decided.wait(timeout)
        if not self.decided.is_set():
            with self.lock:
                self.decided.set()  # 늦게 누른 버튼은 '이미 처리됨'
                self.state["decision"] = "timeout"
        print(f"[approval] {datetime.now():%H:%M:%S} 결과: "
              f"{'Yes' if self.answer else 'No' if self.answer is False else '시간 초과'}", flush=True)
        return self.answer

    def close(self, linger: float = 20):
        """브라우저가 최종 결과를 받아 갈 때까지 최대 linger 초 기다린 뒤 서버를 닫는다."""
        if not self.server:
            return
        if self.state["done"] and self.state["decision"] in ("yes", "no"):
            self.final_seen.wait(linger)
        self.server.shutdown()
        self.server.server_close()
        self.server = None


def request_approval(title: str, items: list[str], question: str = "현행화할까요?",
                     timeout: float = 600, open_browser: bool = True, on_ready=None,
                     links: list[tuple[str, str]] | None = None) -> bool | None:
    """승인 화면을 띄우고 답을 기다린다(진행 표시 없음). Yes=True, No=False, 시간 초과=None."""
    ap = Approval(title, items, question, links)
    page_url, yes_url, no_url = ap.start(open_browser)
    try:
        if on_ready:
            on_ready(page_url, yes_url, no_url)
        result = ap.wait(timeout)
        ap.finish("승인했습니다." if result else "거절했습니다." if result is False else "시간이 지나 취소했습니다.",
                  ok=result is not None)
        return result
    finally:
        ap.close(linger=5)


def main() -> int:
    p = argparse.ArgumentParser(description="로컬 승인 화면(Teams 대체)")
    p.add_argument("--title", default="승인 요청")
    p.add_argument("--question", default="현행화할까요?")
    p.add_argument("--timeout", type=float, default=600, help="응답 대기 시간(초)")
    p.add_argument("--no-browser", action="store_true", help="크롬을 열지 않음")
    p.add_argument("items", nargs="*", help="요약 항목")
    args = p.parse_args()
    result = request_approval(args.title, args.items, args.question, args.timeout, not args.no_browser)
    return 0 if result else 1 if result is False else 2


if __name__ == "__main__":
    raise SystemExit(main())
