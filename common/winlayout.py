"""Windows 데스크톱 창을 화면 가로 전체 × 위쪽으로 옮긴다(Teams 창은 50%, 처리 결과 브라우저 창은 내용 영역 42%).

웹 페이지 스크립트로는 일반 탭 창의 크기를 바꿀 수 없어서, 프로세스 이름과 창 제목으로 창을 찾아 Win32 API 로 옮긴다.
Windows 가 아니거나 창을 못 찾으면 아무것도 하지 않는다. WINDOW_ARRANGE=0 이면 끈다.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

BROWSERS = ("chrome.exe", "msedge.exe")
TOP_RATIO = 0.5  # Teams 창: 창 전체가 화면 높이의 이만큼(위쪽 절반)
BROWSER_RATIO = 0.42  # 처리 결과 등 브라우저 창: 페이지 내용 영역이 화면 높이의 이만큼(탭 줄·주소창은 따로 더함)


def arrange_top_half(exes: tuple[str, ...], title_prefixes: tuple[str, ...] = ()) -> bool:
    """exes 프로세스의 보이는 최상위 창 중(제목 앞부분 조건이 있으면 그것에 맞는 것 중) 가장 큰 창을
    그 모니터 작업 영역의 가로 전체 × 위쪽(TOP_RATIO, 브라우저는 내용 영역 BROWSER_RATIO)으로 옮기고 앞으로 가져온다. 옮겼으면 True."""
    if sys.platform != "win32" or os.environ.get("WINDOW_ARRANGE", "1") == "0":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        try:  # 배율(125% 등)이 있어도 실제 픽셀로 계산하도록
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass

        def exe_name(hwnd) -> str:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                buf, size = ctypes.create_unicode_buffer(260), wintypes.DWORD(260)
                ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                return Path(buf.value).name.lower() if ok else ""
            finally:
                kernel32.CloseHandle(h)

        found = []  # (넓이, hwnd)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            n = user32.GetWindowTextLengthW(hwnd)
            if user32.IsWindowVisible(hwnd) and n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if (not title_prefixes or buf.value.startswith(title_prefixes)) and exe_name(hwnd) in exes:
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
            return True

        user32.EnumWindows(each, 0)
        if not found:
            return False
        hwnd = max(found)[1]
        browser = exe_name(hwnd) in BROWSERS
        if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE: 최대화·최소화 상태에서는 크기를 바꿀 수 없다

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))  # MONITOR_DEFAULTTONEAREST
        w = mi.rcWork
        # window.open 팝업은 페이지 내용 영역이 정한 비율이고 주소창은 그 위에 더해진다. 브라우저 창도 내용 영역 기준으로 맞추게
        # 탭 줄·주소창·테두리 높이만큼 창을 키운다(그렇지 않으면 처리 결과 창만 내용이 덜 보인다).
        extra = browser_frame_height(user32, hwnd) if browser else 0
        user32.SetWindowPos(hwnd, 0, w.left, w.top, w.right - w.left,
                            min(w.bottom - w.top, round((w.bottom - w.top) * (BROWSER_RATIO if browser else TOP_RATIO)) + extra), 0x0040)  # SWP_SHOWWINDOW
        user32.SetForegroundWindow(hwnd)  # 다른 프로그램이 앞에 있으면 Windows 가 막을 수 있다(작업 표시줄 깜빡임)
        return True
    except Exception:
        return False


def browser_frame_height(user32, hwnd) -> int:
    """브라우저 창에서 페이지 내용 영역 바깥(탭 줄·주소창·아래 테두리)의 높이(실제 픽셀).
    크롬·엣지는 페이지를 Chrome_RenderWidgetHostHWND 자식 창에 그리므로 그 창과 바깥 창의 차이로 잰다.
    못 찾으면(창이 막 떠서 아직 없을 때 등) 배율에 맞춘 대략값을 쓴다."""
    import ctypes
    from ctypes import wintypes
    outer = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    best = []  # (넓이, 위, 아래)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(child, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, buf, 64)
        if buf.value == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(child):
            r = wintypes.RECT()
            user32.GetWindowRect(child, ctypes.byref(r))
            best.append(((r.right - r.left) * (r.bottom - r.top), r.top, r.bottom))
        return True

    user32.EnumChildWindows(hwnd, each, 0)
    if best:
        _, top, bottom = max(best)
        if outer.top <= top < bottom <= outer.bottom:
            return (top - outer.top) + (outer.bottom - bottom)
    try:
        dpi = user32.GetDpiForWindow(hwnd) or 96
    except (AttributeError, OSError):
        dpi = 96
    return round(88 * dpi / 96)  # 탭 줄 + 주소창 + 테두리, 100% 배율 기준 약 88px


def arrange_later(exes: tuple[str, ...], title_prefixes: tuple[str, ...] = (), tries: int = 6, every: float = 0.5):
    """페이지가 뜨고 창 제목이 바뀔 때까지 잠깐 기다리며 몇 번 시도한다(요청 처리를 막지 않게 백그라운드로)."""
    def run():
        for _ in range(tries):
            time.sleep(every)
            if arrange_top_half(exes, title_prefixes):
                return
    threading.Thread(target=run, daemon=True).start()
