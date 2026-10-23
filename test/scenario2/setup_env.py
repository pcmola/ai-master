"""시나리오 2(Proxy 현황 Wiki 현행화) 테스트 환경을 점검하고, 테스트용 Wiki 문서를 '현행화 전' 상태로 되돌린다.

Azure 는 바꾸지 않는다. 실제 Proxy VM 을 읽어 일부러 틀린 값을 넣은 Wiki 문서를 만들고,
비교 단계가 찾아야 할 차이(신규·삭제·IP·상태·스펙 변경)를 정답으로 저장한다.
저장소가 공개라 구독 ID·IP 는 코드에 두지 않고, 실행할 때 읽어 data/scenario2/(Git 제외)에 저장한다.

하는 일
1. 사전 조건 점검: az 로그인, 테스트 Proxy VM(3대 이상), Azure DevOps PAT, 승인 화면 모듈
2. 처음 실행(또는 --reseed) 때 실제 VM 으로 '현행화 전' 표와 정답을 만든다
     1번째 VM: 상태·스펙을 다르게 → 상태 변경, 스펙 변경
     2번째 VM: 사설 IP 를 다르게   → IP 변경
     3번째 VM: 표에서 뺀다         → 신규
     없는 VM 하나를 표에 넣는다     → 삭제
3. wiki-repo 의 테스트 문서를 '현행화 전' 표로 다시 쓰고, 바뀌었으면 wiki-repo 에 로컬 커밋(push 하지 않음)

data/scenario2/
    azure_vms.json      Azure 조회 결과 스냅샷(오프라인 테스트용)
    seed.json           '현행화 전' Wiki 표
    expected_diff.json  정답 차이 목록

사용 예:
    python test/scenario2/setup_env.py            # 점검 + 문서 초기화(시나리오 실행 전마다)
    python test/scenario2/setup_env.py --check    # 점검만
    python test/scenario2/setup_env.py --reseed   # VM 이 바뀌었을 때 표·정답을 다시 만든다
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 프로젝트 루트(common 패키지)
from common.paths import DATA_DIR, ENV_FILE, PROJECT_ROOT, WIKI_REPO  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

OUT = DATA_DIR / "scenario2"
# Azure 조회·표 형식·이름 규칙은 시나리오 2 실행기와 같아야 하므로 실행기에서 가져온다.
sys.path.insert(0, str(PROJECT_ROOT / "proxy_tools"))
from proxy_status_sync import (  # noqa: E402,F401
    COLUMNS, FIELDS, NAME_PATTERN, RESOURCE_GROUP, WIKI_PAGE, proxy_vms,
)


def make_seed(vms: list[dict]) -> tuple[list[dict], list[dict]]:
    """실제 VM 에 일부러 차이를 넣은 '현행화 전' 표와 정답 차이 목록."""
    first, second, third = (dict(v) for v in vms[:3])
    other_state = "중지" if first["state"] == "운영" else "운영"
    other_size = "Standard_B1s" if first["size"] != "Standard_B1s" else "Standard_B1ms"
    a, b, c, d = second["private_ip"].split(".")
    wrong_ip = f"{a}.{b}.{c}.{(int(d) + 10) % 250 + 2}"
    removed = {**first, "name": first["name"].split("-")[0] + "-prxy" + f"{len(vms) + 1:02d}",
               "private_ip": f"{a}.{b}.{c}.{(int(d) + 30) % 250 + 2}", "public_ip": "-",
               "size": "Standard_B1s", "state": "운영"}
    expected = [
        {"type": "added", "name": third["name"]},
        {"type": "removed", "name": removed["name"]},
        {"type": "changed", "name": first["name"], "field": "state", "wiki": other_state, "azure": first["state"]},
        {"type": "changed", "name": first["name"], "field": "size", "wiki": other_size, "azure": first["size"]},
        {"type": "changed", "name": second["name"], "field": "private_ip", "wiki": wrong_ip, "azure": second["private_ip"]},
    ]
    first.update(state=other_state, size=other_size)
    second.update(private_ip=wrong_ip)
    # 3번째 이후 VM 은 표에 그대로 둔다(3번째만 빼서 '신규'를 만든다).
    seed = [first, second, *(dict(v) for v in vms[3:]), removed]
    return seed, expected


def render_page(seed: list[dict]) -> str:
    rows = "\n".join("| " + " | ".join(r[f] for f in FIELDS) + " |" for r in seed)
    return f"""# Proxy VM 현황 (테스트 구독)

> 기준 리소스 그룹: {RESOURCE_GROUP}
> 최종 수정: 2026-10-01 · 작성: 인프라운영팀

시나리오 2(Proxy 현황 Wiki 현행화) 검증용 문서다. 테스트 구독에서 이름이 proxy·prxy·pxy 로 끝나는 VM 을 관리하며,
점검 도구가 Azure Portal 의 실제 VM 과 이 표를 비교해 다른 값이 있으면 승인 후 표를 고친다.

## VM 목록

| {" | ".join(COLUMNS)} |
|{"---|" * len(COLUMNS)}
{rows}

- 상태는 Azure 전원 상태 기준이다: 운영(running), 중지(stopped·deallocated).
- 공인 IP 가 없는 VM 은 `-` 로 적는다.

## 변경 이력

| 일시 | 변경 내용 | Task |
|---|---|---|
| 2026-10-01 | 테스트 구독 Proxy VM 목록 최초 작성 | - |
"""


def check_azure(vms: list[dict] | None) -> bool:
    if vms is None:
        return False
    if len(vms) < 3:
        print(f"  [실패] Azure: {RESOURCE_GROUP} 의 Proxy VM 이 {len(vms)}대입니다. 3대 이상 필요합니다.")
        return False
    snap = OUT / "azure_vms.json"
    if snap.is_file() and json.loads(snap.read_text(encoding="utf-8")) != vms:
        print("  [실패] Azure: VM 값이 저장된 스냅샷과 다릅니다. 정답이 틀어졌으니 --reseed 로 다시 만드세요.")
        return False
    print(f"  [통과] Azure: Proxy VM {len(vms)}대 ({', '.join(v['name'] for v in vms)})")
    return True


def check_devops() -> bool:
    org, project, pat = (os.environ.get(k, "") for k in ("AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_PAT"))
    if not (org and project and pat):
        print("  [실패] Azure DevOps: .env 의 AZURE_DEVOPS_ORG / PROJECT / PAT 를 채우세요.")
        return False
    url = f"https://dev.azure.com/{org}/_apis/projects/{urllib.parse.quote(project)}?api-version=7.1"
    auth = base64.b64encode(f":{pat}".encode()).decode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"}), timeout=20) as r:
            name = json.load(r)["name"]
    except Exception as exc:
        print(f"  [실패] Azure DevOps 접속: {exc}")
        return False
    print(f"  [통과] Azure DevOps: 프로젝트 '{name}' 접속 (Task 생성 권한은 시나리오 실행 때 확인)")
    return True


def check_approval() -> bool:
    if (PROJECT_ROOT / "teams_tools" / "approval_local.py").is_file():
        print("  [통과] 승인 화면: teams_tools/approval_local.py (Teams 대체)")
        return True
    print("  [실패] 승인 화면 모듈이 없습니다: teams_tools/approval_local.py")
    return False


def reset_wiki(seed: list[dict]) -> bool:
    text = render_page(seed)
    if WIKI_PAGE.is_file() and WIKI_PAGE.read_text(encoding="utf-8-sig") == text:
        print("  [유지] Wiki 테스트 문서가 이미 '현행화 전' 상태입니다.")
        return True
    WIKI_PAGE.write_text(text, encoding="utf-8", newline="\n")
    back = WIKI_PAGE.read_text(encoding="utf-8")
    if back != text or "�" in back or "??" in back:
        print("  [실패] Wiki 테스트 문서를 다시 읽었더니 내용이 다르거나 깨졌습니다.")
        return False
    rel = WIKI_PAGE.relative_to(WIKI_REPO).as_posix()
    git = ["git", "-C", str(WIKI_REPO)]
    subprocess.run(git + ["add", "--", rel], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet", "--", rel]).returncode:
        subprocess.run(git + ["commit", "-q", "-m", "시나리오 2 테스트: Proxy VM 현황 문서를 현행화 전 상태로 초기화", "--", rel],
                       check=True)
        print("  [초기화] Wiki 테스트 문서를 '현행화 전' 상태로 다시 쓰고 wiki-repo 에 로컬 커밋했습니다 (push 안 함).")
    return True


def save(name: str, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="시나리오 2 테스트 환경 점검·초기화")
    p.add_argument("--check", action="store_true", help="점검만 하고 문서·정답은 바꾸지 않음")
    p.add_argument("--reseed", action="store_true", help="실제 VM 으로 '현행화 전' 표와 정답을 다시 만든다")
    args = p.parse_args()

    print("시나리오 2 테스트 환경 점검")
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        vms = proxy_vms()
    except Exception as exc:
        print(f"  [실패] Azure VM 조회: {exc}")
        vms = None
    if args.reseed and vms:
        for name in ("azure_vms.json", "seed.json", "expected_diff.json"):
            (OUT / name).unlink(missing_ok=True)
    results = [check_azure(vms), check_devops(), check_approval()]

    if args.check:
        same = WIKI_PAGE.is_file() and (OUT / "seed.json").is_file() and WIKI_PAGE.read_text(encoding="utf-8-sig") == \
            render_page(json.loads((OUT / "seed.json").read_text(encoding="utf-8")))
        print(f"  [{'통과' if same else '안내'}] Wiki 테스트 문서: "
              f"{'현행화 전 상태' if same else '현행화 전 상태가 아니거나 아직 없음 (시나리오 실행 후라면 정상)'}")
    elif vms and results[0]:
        if not (OUT / "seed.json").is_file():
            seed, expected = make_seed(vms)
            save("azure_vms.json", vms)
            save("seed.json", seed)
            save("expected_diff.json", {"resource_group": RESOURCE_GROUP, "name_pattern": NAME_PATTERN,
                                        "wiki_page": WIKI_PAGE.relative_to(WIKI_REPO).as_posix(),
                                        "differences": expected})
            print(f"  [생성] '현행화 전' 표와 정답 차이 {len(expected)}건을 data/scenario2/ 에 저장했습니다.")
        results.append(reset_wiki(json.loads((OUT / "seed.json").read_text(encoding="utf-8"))))
    print("결과:", "준비 완료" if all(results) else "준비 안 됨 (위 [실패] 항목 확인)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
