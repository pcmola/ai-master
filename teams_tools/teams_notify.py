"""Send a message to Microsoft Teams through a Workflows (Power Automate) webhook.

- Teams 의 Workflows 앱에서 "Teams 웹후크 요청을 받으면 채팅 또는 채널에 게시" 흐름을 만들고,
  발급된 URL 을 .env 의 TEAMS_WEBHOOK_URL 에 넣는다. 이 URL 자체가 인증 수단이므로 외부에 노출하지 않는다.
- 메시지는 Adaptive Card 로 보낸다. 웹후크는 정상 접수 시 202 를 돌려준다(실제 게시는 흐름이 비동기로 처리).
- 보낸 카드는 data/teams_cards/ 에 JSON 으로 남긴다(README 화면용, test/capture_readme.py --only teams 가 그린다).
  Teams 채널 이력은 Graph 로 읽으려면 관리자 동의 권한이 필요해서, 보내는 쪽에서 남긴다. TEAMS_CARD_ARCHIVE=0 이면 끈다.
- 채널 게시만으로는 Teams '활동' 피드에 알림이 오지 않는다. .env 의 TEAMS_MENTION(받을 사람 UPN)을 넣으면
  카드 첫 줄에 그 사람을 @멘션해서 활동 피드에 알림이 뜨게 한다(TEAMS_MENTION_NAME 은 표시 이름).

사용 예:
    python teams_tools/teams_notify.py "배포가 끝났습니다"
    python teams_tools/teams_notify.py --title "운영 점검" "수집 실패 알림이 7분 지연됨"
    echo 본문 | python teams_tools/teams_notify.py --title 알림
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE  # noqa: E402

CARD_ARCHIVE = DATA_DIR / "teams_cards"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass


def mention_entity() -> dict | None:
    """TEAMS_MENTION 이 있으면 Adaptive Card 멘션 항목을 만든다."""
    upn = os.environ.get("TEAMS_MENTION", "").strip()
    if not upn:
        return None
    name = os.environ.get("TEAMS_MENTION_NAME", "").strip() or upn.split("@")[0]
    return {"type": "mention", "text": f"<at>{name}</at>", "mentioned": {"id": upn, "name": name}}


DOMAIN = re.compile(r"(?<![A-Za-z0-9_.\-/\[(@`])((?:[A-Za-z0-9-]+\.)+(?:kr|com|net|org|io|test))(?![A-Za-z0-9_\-/.])")

DOT_DOMAIN = re.compile(r"(?<![A-Za-z0-9_`])(\.(?:[A-Za-z0-9-]+\.)+(?:kr|com|net|org|io|test))(?![A-Za-z0-9_\-/.])")


def linkify(text: str) -> str:
    """본문의 도메인을 [도메인](https://도메인) 링크로 만든다. Teams 가 스스로 링크를 걸면 뒤에 붙은 한글 조사까지
    링크에 들어가서(예: 'www.data.go.kr와') 미리 정확한 링크로 바꿔 둔다. `코드` 안과 기존 링크는 건드리지 않는다."""
    parts = text.split("`")
    for i in range(0, len(parts), 2):  # 짝수 조각만 코드 밖
        parts[i] = DOT_DOMAIN.sub(r"`\1`", parts[i])  # .data.go.kr 같은 허용 목록 표기는 링크 대신 코드로
        parts[i] = DOMAIN.sub(lambda m: f"[{m.group(1)}](https://{m.group(1)})", parts[i])
        # '11:06:22~11:06:24 … 11:30:53~11:31:11' 처럼 ~ 가 두 번 나오면 Teams 가 그 사이를 취소선으로 그린다.
        parts[i] = parts[i].replace("~", "\u223c")  # ∼ (모양은 같고 마크다운 문법이 아님)
    return "`".join(parts)


def build_card(text: str, title: str | None = None, facts: list[tuple[str, str]] | None = None,
               buttons: list[tuple[str, str]] | None = None) -> dict:
    """facts: (이름, 값) 표. buttons: (글자, URL) — 누르면 그 URL 을 브라우저로 연다(Action.OpenUrl)."""
    body = []
    mention = mention_entity()
    if mention:
        body.append({"type": "TextBlock", "text": mention["text"], "wrap": True})
    if title:
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True})
    body.append({"type": "TextBlock", "text": linkify(text), "wrap": True})
    if facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": str(v).replace("~", "\u223c")} for k, v in facts]})
    content = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if mention:
        content["msteams"] = {"entities": [mention]}
    if buttons:
        content["actions"] = [{"type": "Action.OpenUrl", "title": label, "url": url} for label, url in buttons]
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": content,
        }],
    }


def send(text: str, title: str | None = None, url: str | None = None,
         facts: list[tuple[str, str]] | None = None, buttons: list[tuple[str, str]] | None = None) -> int:
    url = url or os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        raise SystemExit("TEAMS_WEBHOOK_URL 이 설정되지 않았습니다. .env 에 Workflows 웹후크 URL 을 넣으세요.")
    card = build_card(text, title, facts, buttons)
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    status: int | str = "?"
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            status = res.status
            return res.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"Teams 전송 실패: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        status = f"network: {exc.reason}"
        raise SystemExit(f"Teams 전송 실패(네트워크): {exc.reason}")
    finally:
        archive(card, title, status)


def archive(card: dict, title: str | None, status) -> None:
    """보낸 카드를 data/teams_cards/YYYYMMDD-HHMMSS-ffffff.json 에 남긴다. 실패해도 전송에는 영향 없음."""
    if os.environ.get("TEAMS_CARD_ARCHIVE", "1") == "0":
        return
    try:
        now = datetime.now()
        CARD_ARCHIVE.mkdir(parents=True, exist_ok=True)
        (CARD_ARCHIVE / f"{now:%Y%m%d-%H%M%S-%f}.json").write_text(json.dumps(
            {"sent_at": now.isoformat(timespec="seconds"), "title": title, "status": status,
             "source": Path(sys.argv[0]).name, "card": card["attachments"][0]["content"]},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Teams Workflows 웹후크로 메시지를 보낸다.")
    parser.add_argument("text", nargs="?", help="본문. 생략하면 표준 입력에서 읽는다.")
    parser.add_argument("--title", help="굵게 표시할 제목")
    args = parser.parse_args()
    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        parser.error("보낼 본문이 없습니다.")
    status = send(text, args.title)
    print(f"전송 완료 (HTTP {status})")


if __name__ == "__main__":
    main()
