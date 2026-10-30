"""README 화면 재촬영. 조회·가이드 생성만 실행하며 승인·배포·Wiki 등록은 하지 않는다.

사용: python test/capture_readme.py [--inspect] [--only wiki|viewer|hub|proxy|approval|teams]
필요: pip install playwright, 실행 중인 로컬 서버, 설치된 Google Chrome.
승인/완료 화면은 실제 렌더러에 예시 데이터를 넣어 별도 읽기 전용 서버로 촬영한다.
Teams 카드(--only teams)는 teams_notify 가 보낼 때 남긴 data/teams_cards/*.json 중 종류별 최신 카드를
Adaptive Cards 공식 렌더러로 그린다. Teams 에 새 메시지를 보내지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from teams_tools.approval_local import ask_page, progress_page  # noqa: E402
from common.paths import ENV_FILE  # noqa: E402

try:  # Teams 카드에서 가릴 멘션 이름·계정(TEAMS_MENTION_NAME·TEAMS_MENTION)
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

OUT = ROOT / "docs/images"
EVIDENCE = ROOT / "data/readme-capture"
SITES = {
    "hub": "http://ai-master.local",
    "wiki": "http://wiki-search.local",
    "viewer": "http://wiki.local",
    "proxy": "http://proxy-log.local",
}

# 표시된 글자만 가린다. 요청 주소·option 값·실제 데이터는 바꾸지 않는다.
REDACTOR = r"""() => {
  const clean = s => s
    .replace(/\b(?!127\.)(?:\d{1,3}\.){3}\d{1,3}\b/g, 'x.x.x.x')
    .replace(/\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b/gi, '[ID 숨김]')
    .replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi, '[이메일 숨김]')
    .replace(/([A-Z]:\\Users\\)[^\\\s]+/gi, '$1[사용자]');
  const sweep = () => {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = []; while(w.nextNode()) nodes.push(w.currentNode);
    for (const n of nodes) {
      if (['SCRIPT','STYLE'].includes(n.parentElement?.tagName)) continue;
      const s = clean(n.nodeValue); if(s !== n.nodeValue) n.nodeValue = s;
    }
    for (const el of document.querySelectorAll('[title],[placeholder]')) {
      for (const a of ['title','placeholder']) {
        if(el.hasAttribute(a)) el.setAttribute(a,clean(el.getAttribute(a)));
      }
    }
  };
  sweep();
  if (!window.readmeRedactor) {
    window.readmeRedactor = new MutationObserver(sweep);
    window.readmeRedactor.observe(document.body,{subtree:true,childList:true,characterData:true});
  }
}"""


def ready(page, url):
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.evaluate(REDACTOR)


def shot(page, name, *, full_page=False):
    page.evaluate(REDACTOR)
    # 원격 다이어그램 렌더러와 주기적 갱신은 앱 자체 동작을 그대로 사용한다.
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(OUT / name), full_page=full_page, animations="disabled")
    (EVIDENCE / f"{Path(name).stem}.txt").write_text(
        page.locator("body").inner_text(), encoding="utf-8"
    )
    print(f"captured {name}", flush=True)


def wiki(page):
    ready(page, SITES["wiki"])
    page.locator("#q").fill("프록시 설정 가이드 알려줘")
    page.locator("#go").click()
    page.locator("#ask .opt").first.wait_for(timeout=60000)
    shot(page, "wiki-search-type.png")
    # 실제 보이는 유형 버튼을 선택한다. Wiki 등록 버튼은 누르지 않는다.
    page.locator("#ask .opt").filter(has_text="포워드").first.click()
    page.wait_for_function("state.done !== null", timeout=240000)
    page.locator("#tools").wait_for(state="visible")
    print("guide: " + page.locator("#gen").inner_text(), flush=True)
    shot(page, "wiki-search-guide.png")
    page.locator("#revise").scroll_into_view_if_needed()
    shot(page, "wiki-search-revise.png")


def viewer(page):
    ready(page, SITES["viewer"])
    page.locator("#tree .row").first.wait_for()
    if not page.locator("#article h1").count():
        page.locator("#tree .row").first.click()
    page.locator("#article h1").wait_for()
    shot(page, "wiki-viewer.png")


def hub(page):
    ready(page, SITES["hub"])
    page.locator("#rows button").first.wait_for()
    shot(page, "hub-servers.png")
    page.locator("#mt-scenarios").click()
    with page.expect_response(lambda r: "/api/log?" in r.url and "scenario2" in r.url):
        page.locator('button.slog[data-key="scenario2"]').click()
    page.wait_for_function("document.querySelector('#slog').textContent.length > 100")
    # 끝부분에 방금 수행한 dry-run 비교 결과가 나온다.
    page.locator("#slog").evaluate("el => el.scrollTop = el.scrollHeight")
    shot(page, "hub-scenarios.png")
    with page.expect_response(lambda r: "/api/log?" in r.url and "scenario3" in r.url):
        page.locator('button.slog[data-key="scenario3"]').click()
    page.wait_for_function("document.querySelector('#slog').textContent.includes('incident_response')")
    page.locator("#slog").evaluate("el => el.scrollTop = el.scrollHeight")
    shot(page, "scenario3-run.png")


def proxy(page):
    ready(page, SITES["proxy"])
    page.wait_for_function("document.querySelector('#host').options.length >= 3")
    page.wait_for_function("!document.body.classList.contains('loading')", timeout=90000)
    page.locator("#rows tr").first.wait_for()
    shot(page, "proxy-log.png")
    # NGINX도 별도 화면으로 촬영한다. 실제 서버의 읽기만 수행한다.
    page.locator("#host").select_option(index=2)
    page.wait_for_function("!document.body.classList.contains('loading')", timeout=90000)
    page.locator("#rows tr").first.wait_for()
    shot(page, "proxy-log-nginx.png")


def example_server(number):
    if number == 2:
        title = "시나리오 2 · 프록시 현황 Wiki 현행화"
        items = [
            "신규 VM: wjy-proxy03 (Azure에만 있음)",
            "삭제 VM: wjy-prxy04 (Wiki에만 있음)",
            "상태 변경: wjy-proxy01 · stopped → running",
            "SIZE 변경: wjy-proxy01 · Standard_B1s → Standard_B2ats_v2",
            "사설 IP 변경: wjy-proxy02 · 이전 값 → 현재 값",
        ]
        question = "위 변경 사항을 Wiki에 현행화할까요?"
        candidates = ["wjy-proxy01", "wjy-proxy02", "wjy-proxy03"]
        steps = ["변경 목록 검증", "프록시 후보 확인", "DevOps Task 생성 (예시)",
                 "Wiki 표·변경 이력 갱신 (예시)", "Task 완료·Teams 결과 알림 (예시)"]
        message = "현행화 완료 화면 예시입니다. 실제 문서·Task는 변경하지 않았습니다."
    else:
        title = "시나리오 3 · 프록시 장애 대응"
        items = [
            "탐지: 허용 대상 도메인에 대한 CONNECT 차단 반복",
            "원인 예시: Squid 허용 목록의 도메인 오타",
            "수정안 예시: .date.go.kr → .data.go.kr",
            "승인 후: Task·수정 PR·장애 대응 이력 Wiki 생성",
            "PR 병합·실제 VM 배포는 검토자가 별도로 수행",
        ]
        question = "수정 PR을 생성할까요? (병합·배포는 별도)"
        candidates = None
        steps = ["로그 수집·원인 분석", "Wiki 사례·설정 변경 이력 확인", "수정 줄 검증",
                 "DevOps Task·수정 PR 생성 (예시)", "장애 대응 이력 Wiki·Teams 알림 (예시)"]
        message = "PR 생성 완료 화면 예시입니다. 실제 복구는 PR 검토·병합·배포 후 확인합니다."
    banner = ('<div style="padding:10px 18px;background:#fff3cd;color:#664d03;'
              'font-weight:600">화면 예시 · 외부 반영 없음</div>')

    def mark(body):
        return body.replace(b'<div class="body">', ('<div class="body">' + banner).encode("utf-8"), 1)

    ask = mark(ask_page(title, items, question, [("Wiki 뷰어", SITES["viewer"])], candidates))
    progress = mark(progress_page(title))
    status = json.dumps({
        "decision": "yes", "done": True, "ok": True, "message": message,
        "steps": [{"text": s, "state": "ok", "link": None} for s in steps],
        "links": [["Wiki 뷰어", SITES["viewer"]], ["허브로 돌아가기", SITES["hub"]]],
    }, ensure_ascii=False).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data, kind = (status, "application/json") if self.path == "/status" else (
                progress if self.path == "/progress" else ask, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.send_error(405, "Read-only screenshot preview")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def approval(page):
    page.set_viewport_size({"width": 960, "height": 860})
    for number, prefix in [(2, "approval"), (3, "scenario3-approval")]:
        server = example_server(number)
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            ready(page, url)
            shot(page, prefix + ".png")
            ready(page, url + "/progress")
            page.locator("#msg.ok").wait_for()
            shot(page, prefix + "-progress.png")
        finally:
            server.shutdown()
            server.server_close()


# (파일 이름, 카드 제목 패턴). 각 패턴에 맞는 카드 중 전송에 성공한 가장 최근 카드를 그린다.
TEAMS_SHOTS = [
    ("teams-scenario2-approval.png", r"\[Proxy 현황\] 변경 \d+건 · 승인 요청"),
    ("teams-scenario2-result.png", r"\[Proxy 현황\] 현행화 완료"),
    ("teams-scenario3-approval.png", r"\[프록시 장애\] .+ · 조치 승인 요청"),
    ("teams-scenario3-result.png", r"\[프록시 장애\] 조치 진행"),
]
CARD_DIR = ROOT / "data/teams_cards"
CARD_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/markdown-it@14.1.0/dist/markdown-it.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/adaptivecards@3.0.5/dist/adaptivecards.min.js"></script>
<style>body{margin:0;padding:20px;background:#f5f5f5;font-family:"Segoe UI","Malgun Gothic",sans-serif}
#frame{width:600px;background:#fff;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.18);padding:14px 18px}
.meta{font-size:12px;color:#616161;margin-bottom:10px}.meta b{color:#242424}
.ac-pushButton{border:1px solid #d1d1d1!important;background:#fff!important;border-radius:4px!important;font-weight:600}</style>
</head><body><div id="frame"><div class="meta"><b>Workflows</b> · Teams 일반 채널 · __WHEN__</div><div id="card"></div></div>
<script>
const md = window.markdownit();
AdaptiveCards.AdaptiveCard.onProcessMarkdown = (text, result) => { result.outputHtml = md.render(text); result.didProcess = true; };
const card = new AdaptiveCards.AdaptiveCard();
card.hostConfig = new AdaptiveCards.HostConfig({fontFamily: "Segoe UI, Malgun Gothic, sans-serif"});
card.parse(__CARD__);
document.getElementById("card").appendChild(card.render());
window.rendered = true;
</script></body></html>"""


def latest_card(pattern: str) -> dict | None:
    for f in sorted(CARD_DIR.glob("*.json"), reverse=True):
        d = json.loads(f.read_text(encoding="utf-8"))
        if re.fullmatch(pattern, d.get("title") or "") and str(d.get("status")).startswith("2"):
            return d
    return None


def mask_card(card: dict) -> dict:
    """멘션(<at>이름</at>)·담당자 계정·PC 이름은 그리기 전에 가린다. IP·이메일은 REDACTOR 가 화면에서 가린다."""
    secrets = {os.environ.get("TEAMS_MENTION_NAME", ""): "담당자", os.environ.get("TEAMS_MENTION", ""): "[계정 숨김]",
               os.environ.get("COMPUTERNAME", ""): "[PC]"}
    text = json.dumps(card, ensure_ascii=False)
    text = re.sub(r"<at>[^<]*</at>", "@담당자", text)
    for value, label in secrets.items():
        if value:
            text = text.replace(value, label)
    return json.loads(text)


def render_card(page, saved: dict, out: Path):
    html = (CARD_PAGE.replace("__WHEN__", saved["sent_at"].replace("T", " "))
            .replace("__CARD__", json.dumps(mask_card(saved["card"]), ensure_ascii=False)))
    page.set_content(html, wait_until="networkidle")
    page.wait_for_function("window.rendered === true")
    page.evaluate(REDACTOR)
    page.evaluate("document.fonts.ready")
    page.locator("#frame").screenshot(path=str(out), animations="disabled")
    (EVIDENCE / f"{out.stem}.txt").write_text(page.locator("#frame").inner_text(), encoding="utf-8")
    print(f"captured {out.name} ({saved['title']} · {saved['sent_at']})", flush=True)


def teams(page):
    page.set_viewport_size({"width": 700, "height": 1200})
    for name, pattern in TEAMS_SHOTS:
        saved = latest_card(pattern)
        if saved:
            render_card(page, saved, OUT / name)
        else:
            print(f"skip {name}: data/teams_cards 에 '{pattern}' 카드 없음 (해당 시나리오를 실행하면 생김)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="DOM 확인만; 검색·촬영 안 함")
    parser.add_argument("--only", choices=["wiki", "viewer", "hub", "proxy", "approval", "teams"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1040}, device_scale_factor=1)
        page.set_default_timeout(30000)
        if args.inspect:
            for name, url in SITES.items():
                ready(page, url)
                print(name, page.title(), flush=True)
                print(page.locator("body").inner_text()[:1800], flush=True)
                print(page.locator("button, input, select").evaluate_all(
                    "els => els.map(e => ({tag:e.tagName,id:e.id,text:e.textContent.slice(0,100)}))"
                ), flush=True)
        else:
            for name, capture in [("wiki", wiki), ("viewer", viewer), ("hub", hub), ("proxy", proxy), ("approval", approval),
                                  ("teams", teams)]:
                if not args.only or args.only == name:
                    capture(page)
            print("Finished:", datetime.now().astimezone().isoformat(timespec="seconds"), flush=True)
        browser.close()


if __name__ == "__main__":
    main()
