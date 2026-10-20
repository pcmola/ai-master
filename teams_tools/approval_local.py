"""Teams 승인 카드 대신 로컬 웹 화면으로 Yes/No 승인을 받는다(Teams 를 연결하기 전 테스트용).

- request_approval() 을 부르면 임시 웹서버를 띄우고 크롬으로 승인 카드를 연다.
  사용자가 Yes 또는 No 를 누르면 서버를 닫고 True/False 를 돌려준다. 시간 안에 답이 없으면 None.
- 주소는 hosts 에 approval.local 이 있으면 그 이름으로, 없으면 http://127.0.0.1:8771 로 뜬다.
- 나중에 Teams 로 바꿀 때는 같은 인자(title, items, question)로 Teams 승인 카드를 보내는 함수로 교체한다.

사용 예:
    python teams_tools/approval_local.py --title "Proxy 현황 변경 감지" "신규: wjy-proxy03" "삭제: wjy-prxy04"
    (종료 코드: 0 = Yes, 1 = No, 2 = 시간 초과)

    from teams_tools.approval_local import request_approval
    if request_approval("Proxy 현황 변경 감지", ["신규: wjy-proxy03"], "현행화할까요?"):
        ...
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.server import local_server, open_chrome  # noqa: E402

PORT = 8771


def page(title: str, items: list[str], question: str) -> str:
    rows = "".join(f"<li>{html.escape(i)}</li>" for i in items) or "<li>(요약 없음)</li>"
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>승인 요청</title><style>
body{{margin:0;background:#f5f5f5;font:14px/1.6 "Segoe UI","Malgun Gothic",sans-serif;color:#242424}}
.card{{max-width:560px;margin:40px auto;background:#fff;border-radius:8px;box-shadow:0 2px 8px #0002;overflow:hidden}}
.head{{background:#5b5fc7;color:#fff;padding:12px 18px;font-weight:600}}.head small{{opacity:.8;font-weight:400;margin-left:8px}}
.body{{padding:18px}}h1{{font-size:17px;margin:0 0 10px}}ul{{margin:0 0 14px;padding-left:20px}}
.q{{font-weight:600;margin-bottom:14px}}.btns{{display:flex;gap:10px}}
button{{flex:1;padding:10px;border-radius:6px;border:1px solid #d1d1d1;font:inherit;font-weight:600;cursor:pointer;background:#fff}}
button.yes{{background:#5b5fc7;color:#fff;border-color:#5b5fc7}}.done{{padding:14px;text-align:center;font-weight:600}}
</style></head><body><div class="card"><div class="head">승인 요청<small>로컬 승인 화면 (Teams 대체)</small></div>
<div class="body"><h1>{html.escape(title)}</h1><ul>{rows}</ul><div class="q">{html.escape(question)}</div>
<div class="btns" id="btns"><button class="yes" onclick="send('yes')">Yes</button><button onclick="send('no')">No</button></div></div></div>
<script>async function send(a){{document.getElementById('btns').outerHTML='<div class="done">전송 중…</div>';
const r=await fetch('/decide',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{answer:a}})}});
document.querySelector('.done').textContent=r.ok?(a==='yes'?'승인했습니다. 창을 닫아도 됩니다.':'거절했습니다. 창을 닫아도 됩니다.'):'이미 처리된 요청입니다.'}}</script>
</body></html>"""


def request_approval(title: str, items: list[str], question: str = "현행화할까요?",
                     timeout: float = 600, open_browser: bool = True) -> bool | None:
    """승인 화면을 띄우고 답을 기다린다. Yes=True, No=False, 시간 초과=None."""
    decided = threading.Event()
    answer: dict[str, bool] = {}
    body = page(title, items, question).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                value = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}").get("answer")
            except ValueError:
                value = None
            ok = self.path == "/decide" and value in ("yes", "no") and not decided.is_set()
            if ok:
                answer["value"] = value == "yes"
                decided.set()
            self.send_response(200 if ok else 409)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = local_server(PORT, Handler, "approval.local")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[approval] {datetime.now():%H:%M:%S} 승인 대기: {server.url}", flush=True)
    if open_browser:
        open_chrome(server.url)
    try:
        decided.wait(timeout)
    finally:
        server.shutdown()
        server.server_close()
    result = answer.get("value")
    print(f"[approval] {datetime.now():%H:%M:%S} 결과: {'Yes' if result else 'No' if result is False else '시간 초과'}",
          flush=True)
    return result


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
