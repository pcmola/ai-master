"""Send a Teams chat message as yourself through Microsoft Graph (delegated permission).

- Azure AD(Entra ID)에 앱을 등록하고 .env 에 TEAMS_CLIENT_ID / TEAMS_TENANT_ID 를 넣는다.
- 처음 한 번은 장치 코드로 로그인한다(브라우저에서 코드 입력). 토큰은 data/teams_token_cache.json 에
  저장되어 이후에는 자동으로 갱신된다. 이 파일은 로그인 정보이므로 공유하거나 저장소에 올리지 않는다.
- 권한(위임): ChatMessage.Send, Chat.ReadBasic, Chat.Create — 모두 관리자 동의 없이 사용자 동의로 쓸 수 있다.

사용 예:
    python teams_tools/teams_graph.py "본문"                         # 나와의 채팅(48:notes, 비공식 ID)
    python teams_tools/teams_graph.py --to user@corp.com "본문"       # 1:1 채팅(없으면 만든다)
    python teams_tools/teams_graph.py --chat 19:xxx@thread.v2 "본문"  # 채팅 ID 지정
    python teams_tools/teams_graph.py --list                          # 최근 채팅 ID 확인
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import msal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE  # noqa: E402

TOKEN_CACHE = DATA_DIR / "teams_token_cache.json"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = [f"https://graph.microsoft.com/{s}" for s in ("ChatMessage.Send", "Chat.ReadBasic", "Chat.Create")]
# '나와의 채팅' ID. Microsoft 공식 문서에는 없고 테넌트에 따라 "Notes is not flighted" 오류가 날 수 있다.
SELF_CHAT_ID = "48:notes"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass


def access_token() -> str:
    client_id, tenant = os.environ.get("TEAMS_CLIENT_ID"), os.environ.get("TEAMS_TENANT_ID")
    if not client_id or not tenant:
        raise SystemExit("TEAMS_CLIENT_ID / TEAMS_TENANT_ID 가 없습니다. .env 에 앱 등록 정보를 넣으세요.")
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE.is_file():
        cache.deserialize(TOKEN_CACHE.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}",
                                       token_cache=cache)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"로그인 시작 실패: {flow.get('error_description', flow)}")
        print(flow["message"], flush=True)  # "https://microsoft.com/devicelogin 에서 코드 XXXX 입력"
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"로그인 실패: {result.get('error_description', result)}")
    if cache.has_state_changed:
        TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")
    return result["access_token"]


def graph(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(GRAPH + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            raw = res.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise SystemExit(f"Graph 오류: HTTP {exc.code} {method} {path}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Graph 호출 실패(네트워크): {exc.reason}")


def one_on_one_chat(token: str, email: str) -> str:
    """상대와의 1:1 채팅 ID. 이미 있으면 기존 채팅을 돌려준다."""
    me = graph("GET", "/me?$select=id", token)["id"]
    member = lambda ref: {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": ["owner"],
                          "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{ref}')"}
    chat = graph("POST", "/chats", token, {"chatType": "oneOnOne", "members": [member(me), member(email)]})
    return chat["id"]


def to_html(text: str, title: str | None = None) -> str:
    body = html.escape(text).replace("\n", "<br>")
    return f"<b>{html.escape(title)}</b><br>{body}" if title else body


def send(text: str, title: str | None = None, chat_id: str | None = None, to: str | None = None) -> dict:
    token = access_token()
    if to:
        chat_id = one_on_one_chat(token, to)
    chat_id = chat_id or SELF_CHAT_ID
    return graph("POST", f"/chats/{urllib.parse.quote(chat_id, safe='')}/messages", token,
                 {"body": {"contentType": "html", "content": to_html(text, title)}})


def list_chats():
    token = access_token()
    data = graph("GET", "/me/chats?$top=30&$expand=members&$orderby=lastMessagePreview/createdDateTime desc", token)
    for chat in data.get("value", []):
        names = ", ".join(m.get("displayName") or "?" for m in chat.get("members", []))
        print(f"{chat['chatType']:<9} {chat['id']}\n          {chat.get('topic') or names}")


def main():
    parser = argparse.ArgumentParser(description="Microsoft Graph 로 내 계정 이름의 Teams 채팅 메시지를 보낸다.")
    parser.add_argument("text", nargs="?", help="본문. 생략하면 표준 입력에서 읽는다.")
    parser.add_argument("--title", help="굵게 표시할 제목")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--to", metavar="EMAIL", help="1:1 채팅 상대(UPN/메일)")
    target.add_argument("--chat", metavar="CHAT_ID", help="보낼 채팅 ID (--list 로 확인)")
    target.add_argument("--list", action="store_true", help="최근 채팅 목록과 ID 출력")
    args = parser.parse_args()
    if args.list:
        list_chats()
        return
    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        parser.error("보낼 본문이 없습니다.")
    msg = send(text, args.title, chat_id=args.chat, to=args.to)
    print(f"전송 완료 (message id {msg.get('id')})")


if __name__ == "__main__":
    main()
