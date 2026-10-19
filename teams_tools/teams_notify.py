"""Send a message to Microsoft Teams through a Workflows (Power Automate) webhook.

- Teams 의 Workflows 앱에서 "Teams 웹후크 요청을 받으면 채팅 또는 채널에 게시" 흐름을 만들고,
  발급된 URL 을 .env 의 TEAMS_WEBHOOK_URL 에 넣는다. 이 URL 자체가 인증 수단이므로 외부에 노출하지 않는다.
- 메시지는 Adaptive Card 로 보낸다. 웹후크는 정상 접수 시 202 를 돌려준다(실제 게시는 흐름이 비동기로 처리).

사용 예:
    python teams_tools/teams_notify.py "배포가 끝났습니다"
    python teams_tools/teams_notify.py --title "운영 점검" "수집 실패 알림이 7분 지연됨"
    echo 본문 | python teams_tools/teams_notify.py --title 알림
"""

from __future__ import annotations

import argparse
import json
import os
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


def build_card(text: str, title: str | None = None) -> dict:
    body = []
    if title:
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True})
    body.append({"type": "TextBlock", "text": text, "wrap": True})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


def send(text: str, title: str | None = None, url: str | None = None) -> int:
    url = url or os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        raise SystemExit("TEAMS_WEBHOOK_URL 이 설정되지 않았습니다. .env 에 Workflows 웹후크 URL 을 넣으세요.")
    data = json.dumps(build_card(text, title), ensure_ascii=False).encode("utf-8")
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
