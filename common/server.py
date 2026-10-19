"""로컬 웹 화면용 공통 함수: 중복 실행을 막는 HTTP 서버와 크롬 열기."""

from __future__ import annotations

import os
import subprocess
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path


class LocalServer(ThreadingHTTPServer):
    # Windows 에서는 SO_REUSEADDR 때문에 같은 포트에 두 번째 서버가 오류 없이 함께 떠서
    # 예전 서버가 계속 응답하는 일이 생긴다. 재사용을 끄면 두 번째 실행이 바로 실패한다.
    allow_reuse_address = False


def local_server(port: int, handler) -> ThreadingHTTPServer:
    try:
        return LocalServer(("127.0.0.1", port), handler)
    except OSError:
        raise SystemExit(f"포트 {port} 가 이미 사용 중입니다. 이미 떠 있는 서버를 끄고(Ctrl+C) 다시 실행하세요.")


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
