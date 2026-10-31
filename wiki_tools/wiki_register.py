"""시나리오 1 6단계: 생성한 가이드를 Azure DevOps Wiki 의 '02. 운영 > 00. AI 문서 생성' 아래에 등록한다.

rules/WIKI_DOCUMENT_RULES.md 를 따른다.
- 하위 문서 번호는 01, 02, 03 … 으로 고유하게 붙이고, 같은 업무명(번호를 뺀 제목)이 이미 있으면 새로 만들지 않고
  그 문서를 갱신한다(번호 유지, 이력은 git 커밋).
- 새 문서만 폴더의 .order 끝에 더한다. 기존 항목 순서는 바꾸지 않는다.
- UTF-8 로 쓰고 다시 읽어 깨짐(대체문자·물음표 반복)을 검사한다.
- '프록시(proxy)'처럼 두 표기를 묶어 쓴 곳은 '프록시' 하나로 정리한다.
문서 머리에 'AI 생성 · 모델 · 생성 일시 · 질의 · 근거 문서 수'를 적고, 로컬 뷰어 링크(http://wiki.local/?path=…)는
팀원도 열 수 있도록 Wiki 안 링크(/AI-Master-프로젝트/…)로 바꾼다. 커밋 후 Azure DevOps Wiki 로 push 한다.

사용 예(직접 실행):
    python wiki_tools/wiki_register.py guide.md --query "프록시 설정 가이드 알려줘" --model gpt-6.1-sol
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트(common 패키지)
from common.paths import ENV_FILE, WIKI_REPO  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

KST = timezone(timedelta(hours=9))
PARENT = WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "00.-AI-문서-생성"  # 이 폴더 안이 하위 문서
NUMBERED = re.compile(r"^(\d{2})\.-(.+)$")


def git(*args: str, repo: Path | None = None) -> str:
    # 임시 작업 트리(%TEMP%)는 폴더 소유자가 달라 git 이 'dubious ownership' 으로 거부할 수 있어(2026-10-31),
    # 그 폴더만 이 명령에서 안전한 폴더로 지정한다(전역 git 설정은 바꾸지 않음).
    safe = ["-c", f"safe.directory={Path(repo).as_posix()}"] if repo else []
    res = subprocess.run(["git", *safe, "-C", str(repo or WIKI_REPO), *args], capture_output=True)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


def file_title(title: str) -> str:
    """Wiki 파일 이름에 쓸 제목. 공백은 '-', Wiki 가 따로 인코딩하는 문자는 뺀다."""
    clean = re.sub(r"[^\w가-힣 ().·,]", " ", title)
    return re.sub(r"\s+", "-", clean.strip())[:80]


def page_url(page: Path) -> str:
    """Azure DevOps Wiki 화면 주소. 파일 경로의 '-' 는 제목의 공백이다."""
    org, project = os.environ.get("AZURE_DEVOPS_ORG", ""), os.environ.get("AZURE_DEVOPS_PROJECT", "").strip('"')
    wiki = os.environ.get("AZURE_DEVOPS_WIKI", "")
    rel = page.relative_to(WIKI_REPO).with_suffix("").as_posix()
    title_path = "/" + "/".join(unquote(p.replace("-", " ")) for p in rel.split("/"))
    try:  # 이미 올라간 문서면 읽기 쉬운 '문서 번호/제목' 주소(proxy_policy_publish.DevOps.wiki_url 과 같은 방식)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy_tools"))
        from proxy_policy_publish import DevOps
        return DevOps().wiki_url(page)
    except (ImportError, RuntimeError):
        return f"https://dev.azure.com/{quote(org)}/{quote(project)}/_wiki/wikis/{quote(wiki)}?pagePath={quote(title_path)}"


def wiki_links(markdown: str) -> str:
    """로컬 뷰어 링크를 Wiki 안 링크로 바꾼다: (http://…/?path=A%2FB) → (/A/B). 괄호는 링크가 끊기지 않게 인코딩."""
    def repl(m: re.Match) -> str:
        path = unquote(m.group(1))
        return "(/" + path.replace("(", "%28").replace(")", "%29").replace(" ", "%20") + ")"
    return re.sub(r"\(https?://[^)\s]+/\?path=([^)\s]+)\)", repl, markdown)


def tidy(markdown: str) -> str:
    """문서 규칙: 프록시 표기를 하나로(묶음 표기 금지)."""
    markdown = re.sub(r"프록시\s*\(\s*[Pp][Rr][Oo][Xx][Yy]\s*\)", "프록시", markdown)
    return re.sub(r"\b([Pp]roxy)\s*\(\s*프록시\s*\)", r"\1", markdown)


def existing(parent: Path = PARENT) -> dict[str, tuple[str, Path]]:
    """번호를 뺀 업무명 → (번호, 파일)."""
    found = {}
    if parent.is_dir():
        for f in parent.glob("*.md"):
            m = NUMBERED.match(f.stem)
            if m:
                found[m.group(2)] = (m.group(1), f)
    return found


def write_commit(root: Path, title: str, markdown: str, query: str, model: str | None, sources: int, web: int) -> dict:
    """root(wiki-repo 또는 원격 기준 임시 작업 트리)에 문서·.order 를 쓰고 그 두 파일만 커밋한다."""
    parent = root / PARENT.relative_to(WIKI_REPO)
    parent.mkdir(parents=True, exist_ok=True)
    name = file_title(title)
    docs = existing(parent)
    updated = name in docs
    number = docs[name][0] if updated else f"{max((int(n) for n, _ in docs.values()), default=0) + 1:02d}"
    page = parent / f"{number}.-{name}.md"

    when = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    body = tidy(wiki_links(markdown)).strip()
    body = re.sub(r"\A#\s+.*\n?", "", body).strip()  # 첫 제목은 번호를 붙여 다시 쓴다
    head = (f"# {number}. {name.replace('-', ' ')}\n\n"
            f"> AI 생성 문서 · 모델 {model or '-'} · 생성 {when} (KST) · 질의 \"{query}\"\n"
            f"> 근거: 위키 문서 {sources}건" + (f", 웹 {web}건" if web else "")
            + ". 적용 전에 아래 근거 문서를 확인하세요. 같은 제목으로 다시 등록하면 이 문서가 갱신됩니다(이력은 Wiki 기록).\n\n")
    text = head + body + "\n"
    page.write_text(text, encoding="utf-8", newline="\n")
    back = page.read_text(encoding="utf-8")
    if back != text or "\ufffd" in back or re.search(r"\?{3,}", back):
        raise RuntimeError(f"인코딩 검사 실패: {page}")

    order = parent / ".order"
    entries = order.read_text(encoding="utf-8-sig").split() if order.is_file() else []
    if page.stem not in entries:  # 새 문서만 끝에 더한다(기존 순서 유지)
        order.write_text("\n".join(entries + [page.stem]) + "\n", encoding="utf-8", newline="\n")

    warnings = []
    if "프록시(proxy)" in back.lower():
        warnings.append("프록시(proxy) 묶음 표기가 남아 있음")

    rel = [f.relative_to(root).as_posix() for f in (page, order) if f.exists()]
    git("add", "--", *rel, repo=root)
    action = "갱신" if updated else "등록"
    git("commit", "-q", "-m", f"AI 문서 {action}: {number}. {name.replace('-', ' ')}", "--", *rel, repo=root)
    return {"number": number, "title": f"{number}. {name.replace('-', ' ')}", "path": page.relative_to(root).as_posix(),
            "updated": updated, "commit": git("rev-parse", "--short", "HEAD", repo=root), "warnings": warnings}


def register(title: str, markdown: str, query: str = "", model: str | None = None, sources: int = 0, web: int = 0,
             push: bool = True) -> dict:
    """가이드를 등록하고 {number, path, updated, commit, pushed, url, warnings} 를 돌려준다.

    push 하면 로컬 브랜치가 아니라 원격 최신(upstream) 위의 임시 작업 트리에서 이 문서 커밋 하나만 만들어 올린다.
    로컬 wiki-repo 에는 올리지 않는 커밋(시나리오 2 테스트 등)이 있을 수 있어, 로컬 브랜치를 pull·push 하면
    갈라져서 실패하거나(--ff-only) 그 커밋까지 함께 올라간다. 올린 뒤 로컬은 원격 위로 다시 얹는다(rebase)."""
    if not (WIKI_REPO / ".git").exists():
        raise RuntimeError(f"wiki-repo 가 git 저장소가 아닙니다: {WIKI_REPO}")
    if not push:
        r = write_commit(WIKI_REPO, title, markdown, query, model, sources, web)
        return {**r, "pushed": False, "url": page_url(WIKI_REPO / r["path"])}

    upstream = git("rev-parse", "--abbrev-ref", "@{u}")  # 예: origin/wikiMaster
    remote, branch = upstream.split("/", 1)
    git("fetch", "-q", remote)  # 다른 사람이 먼저 올린 변경까지 보고 번호를 매긴다
    tmp = Path(tempfile.mkdtemp(prefix="wiki-register-"))
    try:
        git("worktree", "add", "-q", "--detach", str(tmp), upstream)
        r = write_commit(tmp, title, markdown, query, model, sources, web)
        git("push", "-q", remote, f"HEAD:{branch}", repo=tmp)
    finally:
        try:
            git("worktree", "remove", "--force", str(tmp))
        except RuntimeError:
            shutil.rmtree(tmp, ignore_errors=True)
            git("worktree", "prune")
    git("fetch", "-q", remote)
    try:  # 로컬 전용 커밋을 원격 위로 옮겨 로컬에서도 새 문서가 보이게 한다
        git("rebase", "-q", "--autostash", upstream)
    except RuntimeError as exc:
        try:
            git("rebase", "--abort")
        except RuntimeError:
            pass
        r["warnings"].append(f"Wiki 에는 올렸지만 로컬 wiki-repo 반영 실패(git rebase {upstream} 필요): {str(exc)[:200]}")
    return {**r, "pushed": True, "url": page_url(WIKI_REPO / r["path"])}


def main() -> int:
    p = argparse.ArgumentParser(description="가이드 Markdown 을 Wiki '02. 운영 > 00. AI 문서 생성' 아래에 등록")
    p.add_argument("file", type=Path, help="가이드 Markdown 파일(첫 줄 '# 제목')")
    p.add_argument("--query", default="")
    p.add_argument("--model")
    args = p.parse_args()
    text = args.file.read_text(encoding="utf-8-sig")
    m = re.match(r"#\s+(.+)", text)
    title = m.group(1).strip() if m else args.file.stem
    r = register(title, text, args.query, args.model, push=os.environ.get("WIKI_REGISTER_PUSH", "1") != "0")  # 화면 등록과 같은 .env 설정
    print(f"{'갱신' if r['updated'] else '등록'}: {r['title']} ({r['path']}) 커밋 {r['commit']}"
          + (" · push 완료" if r["pushed"] else " · 로컬"))
    print(r["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
