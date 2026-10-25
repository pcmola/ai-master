"""Send a message to Microsoft Teams through a Workflows (Power Automate) webhook.

- Teams 의 Workflows 앱에서 "Teams 웹후크 요청을 받으면 채팅 또는 채널에 게시" 흐름을 만들고,
  발급된 URL 을 .env 의 TEAMS_WEBHOOK_URL 에 넣는다. 이 URL 자체가 인증 수단이므로 외부에 노출하지 않는다.
- 메시지는 Adaptive Card 로 보낸다. 웹후크는 정상 접수 시 202 를 돌려준다(실제 게시는 흐름이 비동기로 처리).
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import ENV_FILE  # noqa: E402

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
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]})
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
    data = json.dumps(build_card(text, title, facts, buttons), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"Teams 전송 실패: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Teams 전송 실패(네트워크): {exc.reason}")


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
