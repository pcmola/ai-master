"""Teams 연동 테스트: Workflows 웹후크와 Graph API(내 이름으로 채팅 보내기)를 차례로 시험한다.

준비(.env, 비밀번호는 넣지 않는다)
    TEAMS_WEBHOOK_URL   Teams Workflows 의 "Send webhook alerts to a channel/chat" 흐름에서 받은 URL
    TEAMS_CLIENT_ID     Entra ID 앱 등록의 Application (client) ID
    TEAMS_TENANT_ID     Entra ID 앱 등록의 Directory (tenant) ID
설정된 것만 시험하고 나머지는 '건너뜀'으로 표시한다. Graph 는 처음 한 번 장치 코드로 로그인한다
(https://microsoft.com/devicelogin 에서 코드 입력). 토큰은 data/teams_token_cache.json 에 남는다.

사용 예:
    python test/teams_test.py                      # 설정된 방식 모두
    python test/teams_test.py --webhook            # 웹후크만
    python test/teams_test.py --graph              # Graph 만 ('나와의 채팅'으로 보냄)
    python test/teams_test.py --graph --to me@corp.onmicrosoft.com   # 1:1 채팅으로 보냄
    python test/teams_test.py --graph --list       # 최근 채팅 ID 만 출력
종료 코드: 0 = 시험한 것 모두 성공, 1 = 하나 이상 실패, 2 = 시험할 설정이 없음
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트
from common.paths import ENV_FILE  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

from teams_tools import teams_graph, teams_notify  # noqa: E402

# 흔한 오류 → 확인할 것
HINTS = {
    "HTTP 400": "웹후크 흐름이 받는 형식과 다를 수 있습니다. Workflows 템플릿 'Send webhook alerts to a channel/chat' 으로 만들었는지 확인하세요.",
    "HTTP 401": "URL 이 잘렸거나 만료됐습니다. Workflows 에서 URL 을 다시 복사하세요(sig= 까지 전부).",
    "HTTP 403": "권한 문제입니다. 흐름 소유자 계정·조직 정책(외부 웹후크 차단)을 확인하세요.",
    "HTTP 404": "흐름이 삭제·비활성화됐거나 URL 이 틀렸습니다.",
    "Notes is not flighted": "이 테넌트는 '나와의 채팅'(48:notes)을 지원하지 않습니다. --to 본인메일 또는 --chat ID 로 다시 시험하세요.",
    "AADSTS7000218": "앱 등록 > Authentication > 'Allow public client flows' 를 Yes 로 바꾸세요.",
    "AADSTS700016": "TEAMS_CLIENT_ID 가 틀렸거나 다른 테넌트의 앱입니다. 앱 등록 Overview 의 값을 다시 복사하세요.",
    "AADSTS90002": "TEAMS_TENANT_ID 가 틀렸습니다. 앱 등록 Overview 의 Directory (tenant) ID 를 넣으세요.",
    "Unable to get authority configuration": "TEAMS_TENANT_ID 가 틀렸습니다. 앱 등록 Overview 의 Directory (tenant) ID 를 넣으세요.",
    "AADSTS65001": "권한 동의가 필요합니다. 앱 등록 > API permissions 에서 Grant admin consent 를 누르거나 로그인 때 동의하세요.",
    "Forbidden": "API permissions 에 ChatMessage.Send, Chat.ReadBasic, Chat.Create(Delegated)가 있는지 확인하세요.",
}


def hint(message: str) -> str:
    return next((h for k, h in HINTS.items() if k in message), "")


def masked(value: str | None) -> str:
    if not value:
        return "(비어 있음)"
    if value.startswith("http"):  # 웹후크 URL 은 서명(sig=)이 비밀번호 역할이라 호스트만 보인다
        return value.split("/")[2][:24] + "/… (설정됨)"
    return value[:6] + "…" + value[-4:] if len(value) > 14 else "(설정됨)"


def stamp() -> str:
    return f"{datetime.now():%Y-%m-%d %H:%M:%S} · {platform.node()}"


def test_webhook() -> bool:
    print("\n[1] Workflows 웹후크")
    text = (f"ai-master Teams 연동 테스트 메시지입니다. ({stamp()})\n\n"
            "예시 요약 — 시나리오 2 Proxy 현황 변경 감지\n"
            "- 신규: wjy-proxy03\n- 삭제: wjy-prxy04\n- IP 변경: wjy-proxy02")
    try:
        status = teams_notify.send(text, "[ai-master] Teams 웹후크 테스트")
    except (SystemExit, Exception) as exc:  # Graph·msal 오류는 SystemExit, 잘못된 테넌트 등은 ValueError
        print(f"  [실패] {exc}")
        if hint(str(exc)):
            print(f"         → {hint(str(exc))}")
        return False
    ok = status in (200, 202)
    print(f"  [{'통과' if ok else '확인 필요'}] HTTP {status} — 202 면 접수된 것이고, 몇 초 안에 채널/채팅에 카드가 보여야 합니다.")
    return ok


def test_graph(to: str | None, chat: str | None, list_only: bool) -> bool:
    print("\n[2] Graph API (내 이름으로 채팅 보내기)")
    try:
        print("  로그인 확인 중… (처음이면 아래 안내대로 브라우저에서 코드를 입력하세요)", flush=True)
        token = teams_graph.access_token()
        chats = teams_graph.graph("GET", "/me/chats?$top=5", token).get("value", [])
        print(f"  [통과] 로그인·채팅 목록 조회: 최근 채팅 {len(chats)}개")
        if list_only:
            teams_graph.list_chats()
            return True
        where = f"1:1 채팅({to})" if to else f"채팅 {chat}" if chat else "나와의 채팅(48:notes)"
        msg = teams_graph.send(f"ai-master Graph 연동 테스트 메시지입니다. ({stamp()})",
                               "[ai-master] Teams Graph 테스트", chat_id=chat, to=to)
        print(f"  [통과] {where} 로 전송: message id {msg.get('id')}")
        return True
    except (SystemExit, Exception) as exc:  # Graph·msal 오류는 SystemExit, 잘못된 테넌트 등은 ValueError
        print(f"  [실패] {exc}")
        if hint(str(exc)):
            print(f"         → {hint(str(exc))}")
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="Teams 연동 테스트 (웹후크 / Graph)")
    p.add_argument("--webhook", action="store_true", help="웹후크만 시험")
    p.add_argument("--graph", action="store_true", help="Graph 만 시험")
    p.add_argument("--to", metavar="EMAIL", help="Graph: 이 사람과의 1:1 채팅으로 보냄")
    p.add_argument("--chat", metavar="CHAT_ID", help="Graph: 이 채팅 ID 로 보냄")
    p.add_argument("--list", action="store_true", help="Graph: 최근 채팅 ID 만 출력")
    args = p.parse_args()
    both = not (args.webhook or args.graph)

    url = os.environ.get("TEAMS_WEBHOOK_URL")
    client, tenant = os.environ.get("TEAMS_CLIENT_ID"), os.environ.get("TEAMS_TENANT_ID")
    print("Teams 설정 (.env)")
    print(f"  TEAMS_WEBHOOK_URL = {masked(url)}")
    print(f"  TEAMS_CLIENT_ID   = {masked(client)}")
    print(f"  TEAMS_TENANT_ID   = {masked(tenant)}")

    results = []
    if both or args.webhook:
        if url:
            results.append(test_webhook())
        else:
            print("\n[1] Workflows 웹후크: 건너뜀 (TEAMS_WEBHOOK_URL 없음)")
    if both or args.graph:
        if client and tenant:
            results.append(test_graph(args.to, args.chat, args.list))
        else:
            print("\n[2] Graph API: 건너뜀 (TEAMS_CLIENT_ID / TEAMS_TENANT_ID 없음)")

    if not results:
        print("\n결과: 시험할 설정이 없습니다. .env 에 값을 넣은 뒤 다시 실행하세요.")
        return 2
    print(f"\n결과: {sum(results)}/{len(results)} 통과")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
