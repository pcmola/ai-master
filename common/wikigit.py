"""wiki-repo 의 끊긴 git 작업(rebase·merge 중단)을 정리한다.

Wiki 를 올리는 도구는 올린 뒤 로컬 전용 커밋(시나리오 2 테스트 등)을 원격 위로 다시 얹는다(rebase). 커밋이 많으면
십수 초 걸리는데, 그 사이 허브가 작업을 멈추면(예: Health Check 중 '원상 복구') 저장소가 브랜치 없는 rebase 도중 상태로
남는다. 그러면 다음 등록이 'HEAD does not point to a branch' 로 실패한다. wiki-repo 에 쓰기 전에 heal() 을 부른다.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

STALE_S = 30  # 이 시간 동안 rebase 상태 파일이 바뀌지 않았으면 끊긴 것으로 본다(진행 중이면 커밋마다 바뀜)


def heal(repo: Path, log=print) -> str | None:
    """끊긴 rebase·merge 가 있으면 abort 해 원래 브랜치로 되돌리고 무엇을 했는지 돌려준다. 없으면 None.
    다른 프로세스가 지금 rebase 중이면(상태 파일이 최근에 바뀜) 건드리지 않고 RuntimeError."""
    git_dir = repo / ".git"
    for name, cmd in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge")):
        p = git_dir / name
        if not p.exists():
            continue
        files = [p] + (list(p.iterdir()) if p.is_dir() else [])
        age = time.time() - max(f.stat().st_mtime for f in files)
        if age < STALE_S:
            raise RuntimeError(f"wiki-repo 에서 다른 작업이 git {cmd} 중입니다({int(age)}초 전 진행). 잠시 뒤 다시 시도하세요.")
        res = subprocess.run(["git", "-C", str(repo), cmd, "--abort"], capture_output=True)
        if res.returncode != 0:
            out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"wiki-repo 의 끊긴 git {cmd} 정리 실패: {out[:200]}")
        msg = f"wiki-repo 에 끊긴 git {cmd}({int(age)}초 전 멈춤)가 있어 abort 하고 원래 브랜치로 되돌렸습니다."
        log("  " + msg)
        return msg
    return None
