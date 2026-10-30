"""README 화면 재촬영. 조회·가이드 생성만 실행하며 승인·배포·Wiki 등록은 하지 않는다.

사용: python test/capture_readme.py [--inspect] [--only wiki|viewer|hub|proxy|approval|teams|wikidiff|devops]
필요: pip install playwright, 실행 중인 로컬 서버, 설치된 Google Chrome.
승인/완료 화면은 실제 렌더러에 예시 데이터를 넣어 별도 읽기 전용 서버로 촬영한다.
Teams 카드(--only teams)는 teams_notify 가 보낼 때 남긴 data/teams_cards/*.json 중 종류별 최신 카드를
Adaptive Cards 공식 렌더러로 그린다. Teams 에 새 메시지를 보내지 않는다.
--only wikidiff: 시나리오 2 가 마지막으로 고친 현황 문서의 수정 전/후를 wiki-repo 커밋에서 꺼내 나란히 그리고 바뀐 곳을 빨갛게 표시.
--only devops: 허브가 띄운 Chrome(전용 프로필, 개발자 도구 포트 9222, Azure DevOps 로그인 필요)에 붙어 시나리오 2 의
  PBI·Task, 시나리오 3 의 Task·PR(개요·파일 비교) 화면을 찍는다. 승인 기록(data/approvals/)의 처리 단계 링크를 쓴다.
  이름·이메일·IP 는 가린다.
"""

from __future__ import annotations

import argparse
import difflib
import html as html_mod
import json
import os
import re
import subprocess
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


def guide(page, label, *, fresh):
    """유형 버튼을 눌러 가이드를 받는다. fresh 면 캐시로 나온 경우 '새로 만들기'로 LLM 생성을 강제한다."""
    page.locator("#ask .opt").filter(has_text=label).first.click()
    page.wait_for_function("state.done !== null", timeout=300000)
    if fresh and page.evaluate("!!state.done.cached"):
        page.locator("#regen").click()
        page.wait_for_function("state.done !== null && !state.done.cached", timeout=300000)
    page.locator("#tools").wait_for(state="visible")
    # 다이어그램(mermaid)은 가이드가 나온 뒤 따로 그려진다. 다 그려지기 전에 찍으면 빈 칸으로 나온다.
    page.wait_for_function("[...document.querySelectorAll('#guide .mermaid')].every(e => e.querySelector('svg'))",
                           timeout=60000)
    print(f"guide {label}: " + page.locator("#gen").inner_text(), flush=True)


# README 설명이 가리키는 곳에 빨간 테두리를 그린다. 각 식은 요소 하나 또는 배열을 돌려주고, 식마다 상자 하나(합친 영역).
MARK_JS = r"""(exprs) => {
  document.querySelectorAll('.readme-mark').forEach(e => e.remove());
  for (const expr of exprs) {
    let els = new Function('return (' + expr + ')')();
    els = (Array.isArray(els) ? els : [els]).filter(Boolean);
    if (!els.length) continue;
    const r = els.map(e => e.getBoundingClientRect());
    const x1 = Math.min(...r.map(a => a.left)), y1 = Math.min(...r.map(a => a.top));
    const x2 = Math.max(...r.map(a => a.right)), y2 = Math.max(...r.map(a => a.bottom));
    const d = document.createElement('div');
    d.className = 'readme-mark';
    Object.assign(d.style, {position: 'absolute', left: (x1 + scrollX - 6) + 'px', top: (y1 + scrollY - 6) + 'px',
      width: (x2 - x1 + 12) + 'px', height: (y2 - y1 + 12) + 'px', border: '3px solid #e11d48',
      borderRadius: '6px', pointerEvents: 'none', zIndex: 9999});
    document.body.appendChild(d);
  }
}"""
BADGE = "document.querySelector('#gen')"
WEB_CHIP = "[...document.querySelectorAll('#analysis .chip')].filter(e => e.textContent.includes('웹 검색'))"
# 본문 문단만 본다(다이어그램 글자 '웹 서버' 등은 제외). 웹 자료로 보완했다는 문장.
WEB_NOTE = "[...document.querySelectorAll('#guide > p')].find(p => p.textContent.includes('웹') && p.textContent.includes('보완'))"
# 왼쪽 '웹 검색 결과' 제목과 그 뒤 결과들, 가이드 끝 '웹 참고 링크' 제목과 목록
WEB_RESULTS = ("(() => { const h = [...document.querySelectorAll('#results h2')].find(e => e.textContent.includes('웹 검색 결과'));"
               " if (!h) return []; const out = [h]; let n = h.nextElementSibling;"
               " while (n && n.classList.contains('res')) { out.push(n); n = n.nextElementSibling; } return out; })()")
WEB_LINKS = ("(() => { const h = [...document.querySelectorAll('#guide h2')].find(e => e.textContent.includes('웹 참고 링크'));"
             " return h ? [h, h.nextElementSibling] : []; })()")


def mark(page, *exprs):
    page.evaluate(MARK_JS, list(exprs))


def reselect(page):
    """'다시 선택'으로 같은 질의를 다시 검색해 유형 버튼을 띄운다."""
    page.locator("#re").click()
    page.locator("#ask .opt").first.wait_for(timeout=60000)


def wiki(page):
    ready(page, SITES["wiki"])
    page.locator("#q").fill("프록시 설정 가이드 알려줘")
    page.locator("#go").click()
    page.locator("#ask .opt").first.wait_for(timeout=60000)
    shot(page, "wiki-search-type.png")
    # 캐시 저장 전: 캐시가 있으면 '새로 만들기'로 LLM 이 새로 쓰게 한다(이때 캐시에 저장됨). Wiki 등록 버튼은 누르지 않는다.
    guide(page, "포워드", fresh=True)
    mark(page, BADGE)
    shot(page, "wiki-search-guide.png")
    mark(page)
    page.locator("#revise").scroll_into_view_if_needed()
    shot(page, "wiki-search-revise.png")
    # 캐시 저장 후: 같은 질의·유형을 다시 고르면 근거 지문이 같아 LLM 없이 저장된 가이드가 나온다.
    reselect(page)
    guide(page, "포워드", fresh=False)
    page.evaluate("window.scrollTo(0, 0)")
    mark(page, BADGE)
    shot(page, "wiki-search-cache.png")
    # 웹 검색: 리버스 프록시는 위키 근거가 약해(유형 유사도 0.41 < 0.45) 웹 검색 결과를 근거에 보탠다.
    reselect(page)
    guide(page, "리버스", fresh=True)
    page.evaluate("window.scrollTo(0, 0)")
    mark(page, WEB_CHIP, WEB_NOTE)
    shot(page, "wiki-search-web.png")
    links = page.locator("#guide h2").filter(has_text="웹 참고 링크")
    if links.count():
        links.first.scroll_into_view_if_needed()
        mark(page, WEB_RESULTS, WEB_LINKS)
        shot(page, "wiki-search-web-links.png")
    mark(page)


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


APPROVAL_DIR = ROOT / "data/approvals"
APPROVAL_SOURCE = {2: "proxy_status_sync.py", 3: "incident_response.py"}


def latest_approval(number: int) -> dict | None:
    """그 시나리오가 남긴 승인 기록 중 Yes 로 끝까지 처리된 가장 최근 것."""
    for f in sorted(APPROVAL_DIR.glob("*.json"), reverse=True):
        d = json.loads(f.read_text(encoding="utf-8"))
        s = d.get("state") or {}
        if d.get("source") == APPROVAL_SOURCE[number] and s.get("decision") == "yes" and s.get("done"):
            return d
    return None


def preview_server(ask: bytes, progress: bytes, status: bytes):
    """승인·진행 화면을 읽기 전용으로 띄운다(버튼을 눌러도 아무것도 하지 않음)."""
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


def recorded_server(saved: dict):
    """실제 실행 때 저장한 승인 화면 내용과 최종 진행 상태로 같은 화면을 그린다(예시 띠 없음)."""
    links = [tuple(l) for l in saved.get("links") or []]
    ask = ask_page(saved["title"], saved["items"], saved["question"], links, saved.get("candidates"))
    status = json.dumps(saved["state"], ensure_ascii=False).encode("utf-8")
    return preview_server(ask, progress_page(saved["title"]), status)


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
    return preview_server(ask, progress, status)


def approval(page):
    page.set_viewport_size({"width": 960, "height": 860})
    for number, prefix in [(2, "approval"), (3, "scenario3-approval")]:
        # 실제 실행 기록(data/approvals/)이 있으면 그것으로, 없으면 예시 데이터로 그린다(예시는 노란 띠 표시)
        saved = latest_approval(number)
        print(f"{prefix}: " + (f"실제 실행 기록 ({saved.get('asked_at')}, {saved['title']})" if saved else "기록 없음 → 예시 데이터"),
              flush=True)
        server = recorded_server(saved) if saved else example_server(number)
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
#card{border:1px solid #e0e0e0;border-radius:6px;background:#fff}  /* Teams 채널의 카드 테두리 */
#card .ac-adaptiveCard{border-radius:6px}
#card .ac-actionSet{flex-wrap:wrap!important;row-gap:8px}  /* 버튼이 넘치면 다음 줄로(실제 Teams 처럼) */
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
    # set_content 는 document.write 로 써서, 두 번째 카드부터 외부 렌더러 스크립트가 막혀 본문이 빈 채로 찍혔다 → 파일로 연다
    src = EVIDENCE / f"{out.stem}.html"
    src.write_text(html, encoding="utf-8")
    page.goto(src.as_uri(), wait_until="networkidle")
    page.wait_for_function("window.rendered === true && document.getElementById('card').children.length > 0")
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


# ---------------------------------------------------------------- 시나리오 2 Wiki 수정 전/후

WIKI_REPO = ROOT / "wiki-repo"
STATUS_DOC = "AI-Master-프로젝트/02.-운영/01.-공통/06.-네트워크-및-Proxy/01.-네트워크-및-Proxy-현황-파악/Proxy-VM-현황.md"
DIFF_CSS = """body{margin:0;padding:20px;background:#f5f5f5;font:13px/1.55 "Segoe UI","Malgun Gothic",sans-serif;color:#242424}
.pane{background:#fff;border-radius:8px;box-shadow:0 1px 3px #0002;padding:14px 20px;margin-bottom:16px}
.cap{font-weight:700;font-size:14px;margin-bottom:8px}.cap span{font-weight:400;color:#666;margin-left:8px}
h1{font-size:20px;margin:6px 0}h2{font-size:15px;margin:14px 0 6px;border-bottom:1px solid #eee;padding-bottom:4px}
blockquote{margin:4px 0;padding:4px 10px;border-left:3px solid #c7d2fe;background:#f8fafc;color:#475569}
table{border-collapse:collapse;width:100%;font-size:12px;margin:6px 0}td,th{border:1px solid #ddd;padding:4px 6px;text-align:left}
th{background:#f3f4f6}p,li{margin:3px 0}ul{margin:4px 0;padding-left:20px}code{background:#f1f5f9;padding:0 3px;border-radius:3px}
.chg{background:#fee2e2;color:#b91c1c;font-weight:700}.add,.add td{background:#fee2e2;color:#b91c1c;font-weight:700}
.del,.del td{background:#fee2e2;color:#b91c1c;text-decoration:line-through}
.legend{font-size:12px;color:#666;margin-bottom:12px}.legend b{color:#b91c1c}"""


def git_show(rev: str, path: str) -> str:
    out = subprocess.run(["git", "-C", str(WIKI_REPO), "show", f"{rev}:{path}"], capture_output=True)
    return out.stdout.decode("utf-8")


def inline(s: str) -> str:
    s = html_mod.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)


def blocks(md: str) -> list:
    """('table', [행들]) 또는 ('line', 줄). 표는 머리글·구분 줄을 포함한 행 목록."""
    out, table = [], []
    for line in md.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        if table:
            out.append(("table", table)); table = []
        out.append(("line", line))
    if table:
        out.append(("table", table))
    return out


def cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def render_side(md: str, other: str, side: str) -> str:
    """side='before' 면 삭제·변경을, 'after' 면 추가·변경을 빨갛게 표시한다. 표는 첫 칸(이름·일시)으로 행을 맞춘다."""
    mine, theirs = blocks(md), blocks(other)
    other_lines = {x for k, x in theirs if k == "line"}
    other_tables = [x for k, x in theirs if k == "table"]
    html, ti, in_list = [], 0, False
    for kind, x in mine:
        if kind == "line":
            line = x
            mark = ' class="chg"' if line.strip() and line not in other_lines else ""
            if in_list and not line.startswith("- "):
                html.append("</ul>"); in_list = False
            if line.startswith("# "):
                html.append(f"<h1{mark}>{inline(line[2:])}</h1>")
            elif line.startswith("## "):
                html.append(f"<h2{mark}>{inline(line[3:])}</h2>")
            elif line.startswith(">"):
                html.append(f"<blockquote><span{mark}>{inline(line.lstrip('> '))}</span></blockquote>")
            elif line.startswith("- "):
                if not in_list:
                    html.append("<ul>"); in_list = True
                html.append(f"<li><span{mark}>{inline(line[2:])}</span></li>")
            elif line.strip():
                html.append(f"<p><span{mark}>{inline(line)}</span></p>")
            continue
        if in_list:
            html.append("</ul>"); in_list = False
        theirs_t = other_tables[ti] if ti < len(other_tables) else []
        ti += 1
        head, rows = cells(x[0]), [cells(r) for r in x[2:]]
        other_rows = {r[0]: r for r in (cells(r) for r in theirs_t[2:])}
        body = "".join(f"<th>{inline(h)}</th>" for h in head)
        trs = [f"<tr>{body}</tr>"]
        for r in rows:
            o = other_rows.get(r[0])
            if o is None:
                trs.append(f'<tr class="{"del" if side == "before" else "add"}">' + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            else:
                trs.append("<tr>" + "".join(f'<td{" class=chg" if i >= len(o) or c != o[i] else ""}>{inline(c)}</td>'
                                            for i, c in enumerate(r)) + "</tr>")
        html.append("<table>" + "".join(trs) + "</table>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)


def wikidiff(page):
    log = subprocess.run(["git", "-C", str(WIKI_REPO), "log", "-1", "--format=%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M",
                          "--grep=Proxy 현황 현행화", "--", STATUS_DOC], capture_output=True).stdout.decode("utf-8").strip()
    if not log:
        print("skip wiki-scenario2-diff.png: 시나리오 2 현행화 커밋 없음", flush=True)
        return
    rev, when, subject = log.split("|", 2)
    before, after = git_show(f"{rev}^", STATUS_DOC), git_show(rev, STATUS_DOC)
    prev = subprocess.run(["git", "-C", str(WIKI_REPO), "log", "-1", "--format=%h", f"{rev}^"],
                          capture_output=True).stdout.decode().strip()
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><style>{DIFF_CSS}</style></head><body>"
           f"<div class='legend'>Wiki <b>Proxy VM 현황</b> · 시나리오 2 현행화 전/후 비교 · <b>빨간색</b> = 바뀐 곳"
           f"(수정 전: 지워지거나 바뀐 값, 수정 후: 새로 생기거나 바뀐 값)</div>"
           f"<div class='pane'><div class='cap'>수정 전<span>커밋 {prev} · 테스트 환경 준비가 '현행화 전'으로 되돌린 상태</span></div>"
           f"{render_side(before, after, 'before')}</div>"
           f"<div class='pane'><div class='cap'>수정 후<span>커밋 {rev} · {when} · {html_mod.escape(subject)}</span></div>"
           f"{render_side(after, before, 'after')}</div></body></html>")
    src = EVIDENCE / "wiki-scenario2-diff.html"
    src.write_text(doc, encoding="utf-8")
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(src.as_uri(), wait_until="load")
    page.evaluate(REDACTOR)
    page.screenshot(path=str(OUT / "wiki-scenario2-diff.png"), full_page=True, animations="disabled")
    (EVIDENCE / "wiki-scenario2-diff.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
    print(f"captured wiki-scenario2-diff.png ({prev} → {rev}, {when})", flush=True)


# ---------------------------------------------------------------- 시나리오 2 Azure DevOps PBI·Task

NAME_REDACTOR = r"""(names) => {
  const fix = s => names.reduce((a, n) => n ? a.split(n).join('담당자') : a, s);
  const sweep = () => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const ns = []; while (w.nextNode()) ns.push(w.currentNode);
    for (const n of ns) { const s = fix(n.nodeValue); if (s !== n.nodeValue) n.nodeValue = s; }
    for (const el of document.querySelectorAll('input, textarea')) { const s = fix(el.value); if (s !== el.value) el.value = s; }
    for (const el of document.querySelectorAll('[title],[aria-label],[placeholder]'))
      for (const a of ['title', 'aria-label', 'placeholder']) if (el.hasAttribute(a)) el.setAttribute(a, fix(el.getAttribute(a))); };
  // 이름 첫 글자가 보이는 프로필 동그라미는 흐리게
  const st = document.createElement('style');
  st.textContent = '[class*="bolt-coin"], [class*="identity-picture"], [class*="avatar"] { filter: blur(5px) !important; }';
  document.head.appendChild(st);
  // 오른쪽 위 로그인 계정 동그라미(클래스가 달라 위치로 찾는다)
  const me = document.elementFromPoint(innerWidth - 32, 24); if (me) me.style.filter = 'blur(5px)';
  sweep(); new MutationObserver(sweep).observe(document.body, {subtree: true, childList: true, characterData: true});
}"""
DEVOPS_SECTION = ("(t => { const e = [...document.querySelectorAll('span')].find(x => x.textContent.trim() === t);"
                  " return e && e.closest('.work-item-form-collapsible-section'); })")


def section(title: str) -> str:
    return f"{DEVOPS_SECTION}('{title}')"


PR_DESCRIPTION = "document.querySelector('.repos-pr-description-card')"
PR_WORKITEMS = ("(() => { const e = [...document.querySelectorAll('div')].find(x => x.children.length === 0 && x.textContent.trim() === 'Work items');"
                " return e && e.closest('.rhythm-vertical-8'); })()")
PR_DIFF = ("(() => { const v = [...document.querySelectorAll('button, a')].find(x => x.textContent.trim() === 'View');"
           " return v && v.closest('.bolt-card'); })()")


def devops_shots() -> list[tuple]:
    """(파일 이름, 주소, 빨간 테두리 칠 절 이름) 목록. 시나리오별 승인 기록의 처리 단계 링크(PBI·Task·PR 열기)에서 만든다."""
    shots = []
    for number in (2, 3):
        saved = latest_approval(number)
        steps = ((saved or {}).get("state") or {}).get("steps", [])
        for s in steps:
            link = (s.get("link") or [None, None])[1]
            text = s.get("text", "")
            if not link:
                continue
            if re.search(r"PBI #\d+", text) and "백로그" in text:
                shots.append((f"devops-scenario{number}-pbi.png", link, "Related Work", [section("Related Work")]))
            elif re.search(r"Task #\d+ 생성", text) or (number == 3 and "/_workitems/" in link):
                marks = [section("Description")] + ([section("Development")] if number == 3 else [])
                shots.append((f"devops-scenario{number}-task.png", link, "Description", marks))
            elif "/pullrequest/" in link:
                pr = link.split("?")[0]
                shots.append((f"devops-scenario{number}-pr.png", pr, "Description", [PR_DESCRIPTION, PR_WORKITEMS]))
                shots.append((f"devops-scenario{number}-pr-files.png", pr + "?_a=files", "View", [PR_DIFF]))
    seen, out = set(), []
    for shot in shots:  # 같은 파일 이름은 처음 것만
        if shot[0] not in seen:
            seen.add(shot[0]); out.append(shot)
    return out


def devops(own_page):
    shots = devops_shots()
    if not shots:
        print("skip devops: 승인 기록(data/approvals/)에 PBI·Task·PR 링크 없음", flush=True)
        return
    names = [n for n in {os.environ.get("TEAMS_MENTION_NAME", ""), os.environ.get("DEVOPS_DISPLAY_NAME", "")} if n]
    try:  # 이미 떠 있는 Playwright 로 허브 Chrome 에 붙는다(sync_playwright 를 겹쳐 띄우면 오류)
        browser = own_page.context.browser.browser_type.connect_over_cdp("http://127.0.0.1:9222")
    except Exception as exc:
        print(f"skip devops: 허브 Chrome(포트 9222)에 붙지 못함 — 허브를 띄우고 그 창에서 Azure DevOps 에 로그인하세요 ({exc})", flush=True)
        return
    page = browser.contexts[0].new_page()
    try:
        page.set_viewport_size({"width": 1600, "height": 1150})
        for name, url, wait_text, marks in shots:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if "login" in page.url or "signin" in page.url:
                print("skip devops: Azure DevOps 로그인 필요(허브 Chrome 창에서 로그인)", flush=True)
                return
            page.get_by_text(wait_text, exact=True).first.wait_for(timeout=60000)
            page.wait_for_timeout(3000)  # 링크 목록·설명·비교 화면이 늦게 그려짐
            page.evaluate(REDACTOR)
            page.evaluate(NAME_REDACTOR, names)
            mark(page, *marks)
            page.screenshot(path=str(OUT / name), animations="disabled")
            (EVIDENCE / f"{Path(name).stem}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
            print(f"captured {name} ({url.rsplit('/', 1)[-1]})", flush=True)
    finally:
        page.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="DOM 확인만; 검색·촬영 안 함")
    parser.add_argument("--only", choices=["wiki", "viewer", "hub", "proxy", "approval", "teams", "wikidiff", "devops"])
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
                                  ("teams", teams), ("wikidiff", wikidiff), ("devops", devops)]:
                if not args.only or args.only == name:
                    capture(page)
            print("Finished:", datetime.now().astimezone().isoformat(timespec="seconds"), flush=True)
        browser.close()


if __name__ == "__main__":
    main()
