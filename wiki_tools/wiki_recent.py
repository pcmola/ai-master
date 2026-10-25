"""Azure DevOps Wiki 첫 페이지('AI Master 프로젝트')에 최근 수정 문서 5개를 넣는다.

프로젝트 메인(Overview › Summary)의 'About this project' 가 Wiki 첫 페이지를 보여 주므로, 여기에 넣으면
프로젝트 메인에서 바로 보인다.

- 원격 Wiki 저장소(origin)의 커밋 이력에서 내용이 바뀐 .md 문서를 최신순으로, 문서마다 한 번만 고른다.
  이름만 바꾼 커밋(R100)과 첫 페이지 자신, 지금은 없는 문서는 뺀다.
- 첫 페이지의 <!-- recent:start --> … <!-- recent:end --> 사이만 다시 쓴다(없으면 맨 위에 넣는다).
  나머지 내용([[_TOSP_]] 하위 문서 목록 등)은 그대로 둔다.
- 목록이 바뀌지 않았으면 커밋하지 않는다. push 는 다른 로컬 커밋을 섞지 않는 publish_files 로 한다.
- Azure Pipelines(pipelines/wiki-recent.yml)가 Wiki 저장소에 push 가 생길 때마다 실행한다.
  우리 도구가 올린 문서든 Azure DevOps 화면에서 직접 고친 문서든 1~2분 안에 반영된다.
  이 갱신 커밋도 파이프라인을 다시 부르지만, 목록이 그대로라 커밋 없이 끝난다.

    python wiki_tools/wiki_recent.py            # 갱신(바뀌었으면 push)
    python wiki_tools/wiki_recent.py --dry-run  # 만들 목록만 출력
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy_tools"))
from common.paths import WIKI_REPO  # noqa: E402

HOME = WIKI_REPO / "AI-Master-프로젝트.md"
START, END = "<!-- recent:start -->", "<!-- recent:end -->"
COUNT = 5


def git(*args: str) -> str:
    res = subprocess.run(["git", "-C", str(WIKI_REPO), *args], capture_output=True, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {res.stderr.decode('utf-8', 'replace').strip()}")
    return res.stdout.decode("utf-8", "replace")


def title_of(part: str) -> str:
    """파일·폴더 이름 → Wiki 제목('-' 는 공백, %2D 는 실제 하이픈)."""
    return unquote(part.removesuffix(".md").replace("-", " "))


def recent(n: int = COUNT) -> tuple[str, list[dict]]:
    """(원격 브랜치 이름, [{path, title, where, when, who, commit}])"""
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    git("fetch", "-q", "origin", branch)
    ref = f"origin/{branch}"
    out = git("log", ref, "-M", "--diff-filter=AMR", "--name-status", "--date=format-local:%Y-%m-%d %H:%M",  # 이 PC 시간대(KST)로. Azure 화면에서 고친 문서는 UTC 로 기록됨
              "--format=@@%h|%an|%ad", "-300", "--", "*.md")
    alive = set(git("ls-tree", "-r", "--name-only", ref).splitlines())
    home = HOME.relative_to(WIKI_REPO).as_posix()
    seen, items, cur = set(), [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            commit, who, when = line[2:].split("|", 2)
            cur = {"commit": commit, "who": who, "when": when}
            continue
        if not line.strip() or cur is None:
            continue
        status, *paths = line.split("\t")
        if status == "R100":  # 이름만 바뀜(내용 변경 없음)
            continue
        path = paths[-1]
        if path == home or path in seen or path not in alive:
            continue
        seen.add(path)
        parts = path.split("/")
        items.append({**cur, "path": path, "title": title_of(parts[-1]),
                      "where": " › ".join(title_of(p) for p in parts[1:-1][-2:]) or "-"})
        if len(items) >= n:
            break
    return ref, items


def block(items: list[dict]) -> str:
    rows = [f"| [{i['title']}](/{i['path'].removesuffix('.md')}) | {i['where']} | {i['when']} | {i['who']} |" for i in items]
    return "\n".join([START, "## 최근 수정 문서", "",
                      "| 문서 | 위치 | 수정 시각 | 수정자 |", "|---|---|---|---|", *rows, "",
                      f"<sub>최근 {len(items)}건 · Wiki 저장소 커밋 기준, Wiki 가 바뀔 때마다 Azure Pipelines 가 자동 갱신 (wiki_tools/wiki_recent.py)</sub>",
                      END])


def render(current: str, items: list[dict]) -> str:
    new = block(items)
    if START in current and END in current:
        a, b = current.index(START), current.index(END) + len(END)
        return current[:a] + new + current[b:]
    return new + "\n\n" + current.lstrip("\n")


def refresh(dry_run: bool = False) -> str | None:
    """첫 페이지를 갱신한다. 바뀌어 push 했으면 커밋 해시, 아니면 None."""
    ref, items = recent()
    current = git("show", f"{ref}:{HOME.relative_to(WIKI_REPO).as_posix()}").replace("\r\n", "\n")
    page = render(current, items)
    if dry_run:
        print(block(items))
        return None
    if page == current:
        print("Wiki 첫 페이지 최근 수정 목록: 바뀐 것 없음")
        return None
    from proxy_policy_publish import publish_files
    commit = publish_files({HOME: page}, "Wiki 첫 페이지 최근 수정 문서 갱신")
    print(f"Wiki 첫 페이지 최근 수정 목록 갱신: {commit}")
    return commit


def main() -> int:
    p = argparse.ArgumentParser(description="Wiki 첫 페이지에 최근 수정 문서 5개를 넣는다")
    p.add_argument("--dry-run", action="store_true", help="만들 목록만 출력")
    args = p.parse_args()
    try:
        refresh(args.dry_run)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
