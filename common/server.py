"""로컬 웹 화면용 공통 함수: 중복 실행을 막는 HTTP 서버와 크롬 열기.

서버 이름 주소는 hosts 파일(C:\\Windows\\System32\\drivers\\etc\\hosts)에 적힌 IP 로 정한다.
hosts 는 포트를 정할 수 없으므로 포트 없는 주소를 쓰려면 서버마다 다른 루프백 IP 의 80 포트에 띄운다.
    hosts 에 없음          → 127.0.0.1:포트   http://127.0.0.1:포트
    127.0.0.1 이름         → 127.0.0.1:포트   http://이름:포트
    127.0.87.65 wiki.local → 127.0.87.65:80   http://wiki.local
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

HOSTS_FILE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"


class LocalServer(ThreadingHTTPServer):
    # Windows 에서는 SO_REUSEADDR 때문에 같은 포트에 두 번째 서버가 오류 없이 함께 떠서
    # 예전 서버가 계속 응답하는 일이 생긴다. 재사용을 끄면 두 번째 실행이 바로 실패한다.
    allow_reuse_address = False
    url = ""


def hosts_entries() -> dict[str, str]:
    """hosts 파일의 이름 → IP. DNS 조회 대신 파일을 직접 읽어 .local 이름의 조회 지연을 피한다."""
    out: dict[str, str] = {}
    try:
        text = HOSTS_FILE.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        for name in fields[1:]:
            out.setdefault(name.lower(), fields[0])
    return out


def site_url(name: str | None, port: int) -> tuple[str, int, str]:
    """(바인딩 IP, 포트, 브라우저 URL). 규칙은 모듈 설명 참고."""
    ip = hosts_entries().get((name or "").lower())
    try:
        loopback = ip is not None and ipaddress.ip_address(ip).is_loopback and ip != "127.0.0.1"
    except ValueError:
        loopback = False
    if loopback:
        return ip, 80, f"http://{name}"
    if ip == "127.0.0.1":
        return "127.0.0.1", port, f"http://{name}:{port}"
    return "127.0.0.1", port, f"http://127.0.0.1:{port}"


def local_server(port: int, handler, name: str | None = None) -> LocalServer:
    """name 을 주면 hosts 에 따라 이름 주소로 뜬다. 브라우저로 열 주소는 server.url."""
    host, bind_port, url = site_url(name, port)
    try:
        server = LocalServer((host, bind_port), handler)
    except OSError:
        raise SystemExit(f"{host}:{bind_port} 가 이미 사용 중입니다. 이미 떠 있는 서버를 끄고(Ctrl+C) 다시 실행하세요.")
    server.url = url
    return server


def open_chrome(url: str):
    """크롬으로 페이지를 연다. 크롬이 없으면 기본 브라우저를 쓴다."""
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    chrome = next((p for p in candidates if p.is_file()), None)
    if chrome:
        subprocess.Popen([str(chrome), url])
    else:
        webbrowser.open(url)
