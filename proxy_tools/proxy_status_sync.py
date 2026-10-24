"""시나리오 2: Proxy 현황 Wiki 현행화. Wiki 의 Proxy VM 표와 Azure 의 실제 VM 을 비교해, 차이가 있으면
Teams 로 승인을 받아 Azure DevOps Task 를 만들고 Wiki 표를 고친 뒤 결과를 Teams 로 알린다.

단계(노드별 트레이스는 data/traces/scenario2-YYYYMMDD.jsonl 과 콘솔에 남는다)
1. trigger      직접 실행(허브 '시나리오 실행' 탭 또는 명령줄)
2. wiki_read    Wiki 문서의 'VM 목록' 표를 규칙으로 읽는다. 그 형식이 아니면 wiki_parse_llm: LLM 이 표준 형식으로
                바꾸고, VM 이름이 원문에 실제로 있는지 대조한다(없으면 버림, IP 도 원문에 없으면 비움).
                LLM 으로 읽은 문서는 형식이 제각각이라 7단계에서 표를 자동으로 고치지 않고 Task 에 수동 반영을 남긴다
3. azure_read   리소스 그룹의 VM 중 태그 SCENARIO2_TAG(기본 role=proxy)가 있거나, 이름이 proxy·prxy·pxy(+숫자)로
                끝나는 VM(프록시로 추정)의 IP·상태·SIZE·리전을 읽는다. SCENARIO2_EXCLUDE_TAG(기본 role=not-proxy)는 뺀다
   proxy_confirm  이름 규칙으로만 찾은 VM 은 5단계 승인 카드에서 한 번 확인받는다. Yes 면 확인한 VM 에 SCENARIO2_TAG,
                '프록시 아님'으로 고른 VM 에 SCENARIO2_EXCLUDE_TAG 태그를 붙여 다음부터 묻지 않고, 뺀 VM 은 비교에서 제외한다
4. compare      신규·삭제·변경(사설/공인 IP, 상태, SIZE, 리전). 차이가 없으면 여기서 끝난다
   llm_summary  (AI) 차이 전체를 LLM(OPENAI_MODEL)으로 2문장 이내로 요약해 승인 카드·화면·Task 맨 위에 '참고용'으로 붙인다.
                비교 자체는 규칙으로 하고 AI 는 설명만 한다. 실패하거나 LLM 설정이 없으면 규칙 목록만 쓴다
5. approval     Teams 채널에 요약 카드(Yes/No 버튼)를 보내고 이 PC 의 승인 서버에서 답을 기다린다.
                TEAMS_WEBHOOK_URL 이 없으면 승인 화면을 브라우저로 연다. No·시간 초과면 기록하고 끝낸다
6. task_create  Azure DevOps 백로그 아래 Task: Epic(SCENARIO2_EPIC_ID) → Feature → 그 달 PBI → 'NN. …' Task
                (Feature·PBI 는 있으면 다시 쓰고 없으면 만든다. 담당자, 상위 항목의 Area·Iteration)
7. wiki_update  표를 Azure 값으로 고치고 변경 이력 행을 더해 wiki-repo 에 커밋한다(SCENARIO2_PUSH=1 일 때만 push)
8. report       Task 를 완료 상태로 바꾸고 Teams 에 결과를 알린다

설정(.env): AZURE_DEVOPS_ORG/PROJECT/PAT, TEAMS_WEBHOOK_URL,
            SCENARIO2_RESOURCE_GROUP(기본 WJY-PROXY-RG), SCENARIO2_ASSIGNEE(비우면 PAT 소유자),
            SCENARIO2_EPIC_ID(기본 1 '01. 공통'), SCENARIO2_FEATURE(기본 '[공통] Proxy 현황 관리'),
            SCENARIO2_PBI(기본 'Proxy VM 현황 Wiki 현행화', 앞에 'YYMM) ' 이 붙는다),
            SCENARIO2_PUSH(기본 0: wiki-repo 로컬 커밋만, 1: Azure DevOps Wiki 로 push),
            SCENARIO2_TIMEOUT(승인 대기 초, 기본 900), SCENARIO2_WIKI_PAGE(비교할 문서, 비우면 테스트 문서)
테스트 문서를 비교하면 테스트 정답(data/scenario2/expected_diff.json)과 자동으로 대조한다.

사용 예:
    python proxy_tools/proxy_status_sync.py               # Teams 승인 후 Task·Wiki 반영
    python proxy_tools/proxy_status_sync.py --dry-run     # 1~4단계만 하고 할 일을 출력(아무것도 바꾸지 않음)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 프로젝트 루트
sys.path.insert(0, str(Path(__file__).resolve().parent))      # proxy_tools
from common.paths import DATA_DIR, ENV_FILE, WIKI_REPO  # noqa: E402
from common.trace import span, trace  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

KST = timezone(timedelta(hours=9))
RESOURCE_GROUP = os.environ.get("SCENARIO2_RESOURCE_GROUP", "WJY-PROXY-RG")
NAME_PATTERN = r"(?i)(proxy|prxy|pxy)\d*$"
TEST_PAGE = (WIKI_REPO / "AI-Master-프로젝트" / "02.-운영" / "01.-공통" / "06-네트워크-및-Proxy"
             / "01-네트워크-및-Proxy-현황-파악" / "Proxy-VM-현황-(테스트-구독).md")
WIKI_PAGE = WIKI_REPO / os.environ["SCENARIO2_WIKI_PAGE"] if os.environ.get("SCENARIO2_WIKI_PAGE") else TEST_PAGE
PUSH = os.environ.get("SCENARIO2_PUSH", "0") == "1"
APPROVAL_TIMEOUT = float(os.environ.get("SCENARIO2_TIMEOUT") or 900)
COLUMNS = ["NAME", "RESOURCE GROUP", "LOCATION", "사설 IP", "공인 IP", "SIZE", "Proxy", "상태"]
FIELDS = ["name", "resource_group", "location", "private_ip", "public_ip", "size", "proxy", "state"]
COMPARE = {"private_ip": "사설 IP", "public_ip": "공인 IP", "state": "상태", "size": "SIZE",
           "location": "LOCATION", "resource_group": "RESOURCE GROUP"}  # Proxy(용도) 칸은 사람이 관리한다
LOCATIONS = {"koreacentral": "Korea Central", "eastus": "East US"}
EXPECTED = DATA_DIR / "scenario2" / "expected_diff.json"


# ---------------------------------------------------------------- 2·3 읽기

def az(*args: str):
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        raise RuntimeError("az CLI 를 찾지 못했습니다.")
    out = subprocess.run([exe, *args, "-o", "json"], capture_output=True, text=True, encoding="utf-8", timeout=180,
                         stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"az {' '.join(args)} 실패 (az login 필요할 수 있음)")
    return json.loads(out.stdout)


PROXY_TAG = os.environ.get("SCENARIO2_TAG", "role=proxy").strip()
EXCLUDE_TAG = os.environ.get("SCENARIO2_EXCLUDE_TAG", "role=not-proxy").strip()


def tag_match(tags: dict | None, spec: str | None = None) -> bool:
    """spec(기본 SCENARIO2_TAG)과 맞는 태그가 있는가. 'key=value' 또는 'key'(값 상관없음). 대소문자 무시."""
    spec = PROXY_TAG if spec is None else spec
    if not spec or not tags:
        return False
    key, _, value = spec.partition("=")
    lowered = {k.lower(): str(v).lower() for k, v in tags.items()}
    return key.lower() in lowered and (not value or lowered[key.lower()] == value.lower())


def proxy_vms(resource_group: str = RESOURCE_GROUP) -> list[dict]:
    """Wiki 표와 같은 모양으로 정리한 Proxy VM 목록(이름순). 태그가 맞거나 이름 규칙이 맞으면 Proxy 로 본다.
    matched_by: 'tag' | 'name' (비교 대상 칸이 아니라 기록용)."""
    raw = az("vm", "list", "-d", "-g", resource_group, "--query",
             "[].{name:name,rg:resourceGroup,loc:location,priv:privateIps,pub:publicIps,"
             "size:hardwareProfile.vmSize,power:powerState,tags:tags}")
    vms = []
    for v in raw:
        by_tag, by_name = tag_match(v.get("tags")), bool(re.search(NAME_PATTERN, v["name"]))
        if not (by_tag or by_name) or (not by_tag and tag_match(v.get("tags"), EXCLUDE_TAG)):  # '프록시 아님'으로 확인된 VM
            continue
        tags = v.get("tags") or {}
        vms.append({"name": v["name"], "resource_group": v["rg"], "location": LOCATIONS.get(v["loc"], v["loc"]),
                    "private_ip": v.get("priv") or "-", "public_ip": v.get("pub") or "-", "size": v["size"],
                    "proxy": f"{tags.get('proxy', '-')} ({tags.get('install', '-')})",
                    "state": "운영" if v.get("power") == "VM running" else "중지",
                    "matched_by": "tag" if by_tag else "name"})
    return sorted(vms, key=lambda v: v["name"])


def set_tag(name: str, spec: str):
    """VM 에 태그 하나를 더한다(다른 태그는 그대로). 'key=value', 값이 없으면 'true'. Tag Contributor 권한이면 된다."""
    key, _, value = spec.partition("=")
    vm_id = az("vm", "show", "-g", RESOURCE_GROUP, "-n", name, "--query", "id")
    az("tag", "update", "--resource-id", vm_id, "--operation", "Merge", "--tags", f"{key}={value or 'true'}")


def confirm_proxies(ap, azure: list[dict]) -> list[dict]:
    """승인(Yes)과 함께, 이름 규칙으로만 찾은 VM 을 태그로 확정한다. '프록시 아님'으로 고른 VM 은 빼고 돌려준다."""
    guessed = [v["name"] for v in azure if v["matched_by"] == "name"]
    if not guessed:
        return azure
    excluded = [n for n in (ap.excluded if ap else []) if n in guessed]
    step = ap.step if ap else (lambda *a, **k: None)
    with span("proxy_confirm", guessed=guessed, excluded=excluded or "없음") as s:
        failed = []
        for name in guessed:
            spec = EXCLUDE_TAG if name in excluded else PROXY_TAG
            try:
                set_tag(name, spec)
            except RuntimeError as exc:
                failed.append(f"{name}: {str(exc)[:120]}")
        s.set(tagged=len(guessed) - len(failed), failed=failed or "없음")
    kept = [n for n in guessed if n not in excluded]
    msg = (f"프록시로 확정: {', '.join(kept) or '없음'} ({PROXY_TAG} 태그)"
           + (f" · 프록시 아님: {', '.join(excluded)} ({EXCLUDE_TAG} 태그, 비교에서 뺌)" if excluded else ""))
    print(msg + (f" · 태그 실패 {len(failed)}건(다음 실행 때 다시 묻습니다): {'; '.join(failed)}" if failed else ""))
    step(msg + (f" · 태그 실패 {len(failed)}건" if failed else ""), "fail" if failed else "ok")
    return [v for v in azure if v["name"] not in excluded]


def table_rows(rows: list[dict]) -> str:
    head = f"| {' | '.join(COLUMNS)} |\n|{'---|' * len(COLUMNS)}\n"
    return head + "\n".join("| " + " | ".join(r[f] for f in FIELDS) + " |" for r in rows)


def read_wiki(text: str) -> list[dict]:
    """'## VM 목록' 아래 첫 표를 읽는다. 머리글 이름으로 칸을 찾으므로 칸 순서가 바뀌어도 된다."""
    section = text.split("## VM 목록", 1)
    if len(section) < 2:
        raise RuntimeError("Wiki 문서에 '## VM 목록' 절이 없습니다.")
    lines = [l.strip() for l in section[1].splitlines()]
    start = next((i for i, l in enumerate(lines) if l.startswith("|")), None)
    if start is None:
        raise RuntimeError("'## VM 목록' 아래에 표가 없습니다.")
    header = [c.strip() for c in lines[start].strip("|").split("|")]
    index = {f: header.index(c) for c, f in zip(COLUMNS, FIELDS) if c in header}
    if "name" not in index:
        raise RuntimeError(f"표에 NAME 칸이 없습니다: {header}")
    rows = []
    for l in lines[start + 2:]:
        if not l.startswith("|"):
            break
        cells = [c.strip() for c in l.strip("|").split("|")]
        rows.append({f: cells[i] if i < len(cells) else "" for f, i in index.items()})
    return rows


def llm_client():
    from openai import AzureOpenAI, OpenAI
    if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
        return AzureOpenAI(api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"), timeout=120, max_retries=1)
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAI(timeout=120, max_retries=1)
    raise RuntimeError("LLM 설정 없음(AZURE_OPENAI_* 또는 OPENAI_API_KEY)")


def llm_parse(text: str) -> list[dict]:
    """정해진 표 형식이 아닌 현황 문서를 LLM 으로 표준 형식(FIELDS)으로 바꾼다. 결과는 원문 대조로 검증한다:
    VM 이름이 원문에 없으면 버리고, IP·SIZE 가 원문에 없으면 비운다(LLM 이 값을 지어내거나 잘못 옮기는 것을 막는다)."""
    model = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")
    prompt = ("아래 Wiki 문서에서 Proxy VM 목록을 뽑아 JSON 으로만 답하라.\n"
              '형식: {"rows": [{"name": str, "resource_group": str, "location": str, "private_ip": str, '
              '"public_ip": str, "size": str, "proxy": str, "state": str, "note": str}]}\n'
              "- name 은 VM 이름만(괄호 속 설명 제외), 문서에 적힌 글자 그대로.\n"
              "- private_ip 는 사설 IP 숫자만(예: 'LB 10.20.40.10' → '10.20.40.10'). 없으면 빈 문자열.\n"
              "- location 은 'Korea Central'·'East US'처럼 문서 표기 그대로. proxy 는 용도 칸 내용.\n"
              "- state 는 '운영' | '중지' | '폐기 예정' | '확인 필요' 중 하나로 정규화하고, 원래 설명(예: 'IP 변경', '2026-11')은 note 에.\n"
              "- 문서에 없는 값은 지어내지 말고 빈 문자열.\n\n" + text)
    with span("wiki_parse_llm", model=model, chars=len(text)) as s:
        res = llm_client().chat.completions.create(model=model, response_format={"type": "json_object"},
                                                   messages=[{"role": "user", "content": prompt}])
        s.set(prompt_tokens=res.usage.prompt_tokens, completion_tokens=res.usage.completion_tokens)
        raw = json.loads(res.choices[0].message.content or "{}").get("rows", [])
        flat = re.sub(r"\*\*", "", text)
        rows, dropped = [], []
        for r in raw:
            row = {f: str(r.get(f) or "").strip() for f in FIELDS}
            if not row["name"] or row["name"] not in flat:
                dropped.append(row["name"] or "?")
                continue
            for f in ("private_ip", "public_ip", "size"):
                if row[f] and row[f] not in flat:
                    row[f] = ""  # 원문에 없는 값은 쓰지 않는다
            if row["state"] not in ("운영", "중지", "폐기 예정", "확인 필요"):
                row["state"] = ""
            rows.append(row)
        s.set(rows=len(rows), dropped=dropped)
        if not rows:
            raise RuntimeError("LLM 이 문서에서 VM 을 찾지 못했습니다(또는 모두 원문 대조에 실패).")
        return rows


# ---------------------------------------------------------------- 4 비교

def compare(wiki: list[dict], azure: list[dict]) -> list[dict]:
    w, a = {r["name"]: r for r in wiki}, {r["name"]: r for r in azure}
    diffs = [{"type": "added", "name": n} for n in sorted(set(a) - set(w))]
    diffs += [{"type": "removed", "name": n} for n in sorted(set(w) - set(a))]
    for n in sorted(set(w) & set(a)):
        for f in COMPARE:
            if not w[n].get(f) or (f == "state" and w[n][f] not in ("운영", "중지")):
                continue  # Wiki 에 값이 없거나(LLM 파싱 문서) 상태가 '폐기 예정' 같은 설명이면 비교하지 않는다
            if w[n][f] != a[n][f]:
                diffs.append({"type": "changed", "name": n, "field": f, "wiki": w[n][f], "azure": a[n][f]})
    return diffs


def describe(d: dict) -> str:
    if d["type"] == "added":
        return f"신규(Azure 에만 있음): {d['name']}"
    if d["type"] == "removed":
        return f"삭제(Wiki 에만 있음): {d['name']}"
    alert = " ⚠ 이상 징후: VM 중지" if d["field"] == "state" and d["azure"] == "중지" else ""
    return f"{COMPARE[d['field']]} 변경: {d['name']} ({d['wiki']} → {d['azure']}){alert}"


def stopped_alert(azure: list[dict]) -> str:
    """실제 운영에서 Proxy VM 은 항상 켜져 있어야 하므로, 꺼진 VM 은 문서 갱신과 별도로 알린다."""
    off = [v["name"] for v in azure if v["state"] != "운영"]
    return f"⚠ 이상 징후: 중지된 Proxy VM {len(off)}대 ({', '.join(off)}) — 장애·의도 여부를 확인해 주세요." if off else ""


def verify_target() -> bool:
    """정답 파일이 지금 비교하는 문서의 것일 때만 대조한다(테스트 문서)."""
    if not EXPECTED.is_file():
        return False
    page = json.loads(EXPECTED.read_text(encoding="utf-8")).get("wiki_page", "")
    return (WIKI_REPO / page).resolve() == WIKI_PAGE.resolve()


def verify(diffs: list[dict]) -> bool:
    if not EXPECTED.is_file():
        print(f"  [verify] 정답 파일이 없습니다: {EXPECTED} (test/scenario2/setup_env.py 를 먼저 실행)")
        return False
    want = json.loads(EXPECTED.read_text(encoding="utf-8"))["differences"]
    key = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)
    got_k, want_k = {key(d) for d in diffs}, {key(d) for d in want}
    ok = got_k == want_k
    print(f"  [verify] 정답 {len(want_k)}건 · 찾은 차이 {len(got_k)}건 → {'일치' if ok else '불일치'}")
    for k in sorted(want_k - got_k):
        print(f"    못 찾음: {k}")
    for k in sorted(got_k - want_k):
        print(f"    더 찾음: {k}")
    return ok


# ---------------------------------------------------------------- 4.5 AI 요약

def ai_summary(diffs: list[dict], wiki: list[dict], azure: list[dict]) -> dict | None:
    """차이 전체를 LLM 으로 짧게(2문장·120자 이내, 존댓말) 요약한다. 비교 결과(diffs)는 바꾸지 않는다.
    위험도는 정해진 기준 없이 AI 가 고르게 되어 표시하지 않는다(2026-10-24).
    돌려주는 값: {"model", "summary"} 또는 실패 시 None."""
    from openai import AzureOpenAI, OpenAI
    model = os.environ.get("OPENAI_MODEL", "gpt-6.1-sol")
    # 판단에 필요한 정보만 보낸다(공인 IP 는 바뀐 경우가 아니면 넣지 않는다).
    vm = lambda r: {k: r.get(k) for k in ("name", "state", "size", "location", "proxy", "private_ip")}
    payload = {"resource_group": RESOURCE_GROUP,
               "differences": [{"index": i, **d} for i, d in enumerate(diffs)],
               "wiki_rows": [vm(r) for r in wiki], "azure_vms": [vm(r) for r in azure]}
    prompt = ("아래는 사내 Wiki 의 Proxy VM 현황 표와 Azure 의 실제 VM 을 비교한 결과다. 운영자가 Wiki 현행화 승인을 "
              "판단하도록 핵심만 한국어 존댓말(~습니다, ~해 주세요)로 요약하라.\n"
              "- summary: 2문장 이내, 120자 이내. 가장 중요한 변경과 승인 전에 확인할 것 하나.\n"
              "- 상태 '운영'=실행 중, '중지'=중지·할당 해제. Proxy VM 은 항상 켜져 있어야 하므로 중지는 이상 징후로 먼저 언급하라.\n"
              "- 신규=Azure 에만 있음, 삭제=Wiki 에만 있음.\n"
              "- 주어진 데이터에 없는 사실(사용 서비스, 원인 등)은 지어내지 마라.\n"
              'JSON 으로만 답하라: {"summary": str}\n\n'
              + json.dumps(payload, ensure_ascii=False))
    with span("llm_summary", model=model, diffs=len(diffs), prompt_chars=len(prompt)) as s:
        try:
            if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
                client = AzureOpenAI(api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"),
                                     timeout=60, max_retries=1)
            elif os.environ.get("OPENAI_API_KEY"):
                client = OpenAI(timeout=60, max_retries=1)
            else:
                s.fail("LLM 설정 없음(AZURE_OPENAI_* 또는 OPENAI_API_KEY) → 규칙 목록만 사용")
                return None
            res = client.chat.completions.create(
                model=model, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": "너는 클라우드 인프라 변경을 검토하는 운영 엔지니어다. 사실만 짧게 쓴다."},
                          {"role": "user", "content": prompt}])
            s.set(prompt_tokens=res.usage.prompt_tokens, completion_tokens=res.usage.completion_tokens)
            data = json.loads(res.choices[0].message.content or "{}")
            summary = str(data.get("summary", "")).strip()
            if not summary:
                s.fail("응답에 summary 가 없음 → 규칙 목록만 사용")
                return None
            s.set(summary_chars=len(summary))
            return {"model": model, "summary": summary}
        except Exception as exc:  # 게이트웨이 오류·JSON 오류 등: 승인 흐름은 막지 않는다
            s.fail(f"AI 요약 실패 → 규칙 목록만 사용: {type(exc).__name__}: {exc}")
            return None


def explained(diffs: list[dict], ai: dict | None = None) -> list[str]:
    """승인 화면·카드·Task 에 보일 차이 목록. 짧게 유지하려고 규칙 결과만 쓴다(AI 는 위의 요약 한 줄만)."""
    return [describe(d) for d in diffs]


# ---------------------------------------------------------------- 5 승인

def wiki_fact(links: list[tuple[str, str]]) -> str:
    """Teams 카드 표(FactSet)에 넣을 문서 링크: 'Proxy VM 현황 (테스트 구독)' (Azure DevOps Wiki) · 로컬 뷰어."""
    title = WIKI_PAGE.stem.replace("-", " ")
    devops_url = next((u for l, u in links if u and l.startswith("Azure DevOps")), "")
    local_url = next((u for l, u in links if u and "로컬" in l), "")
    head = f"[{title}]({devops_url})" if devops_url else f"{title} (Azure DevOps Wiki 에 없음)"
    return head + (f" · [로컬 뷰어]({local_url})" if local_url else "")


def wiki_links(dv) -> list[tuple[str, str]]:
    """확인용 Wiki 링크. 로컬 뷰어(이 PC 의 wiki-repo)와, 문서가 원격에 push 되어 있을 때만 Azure DevOps Wiki."""
    from urllib.parse import quote
    from common.server import site_url
    rel = WIKI_PAGE.relative_to(WIKI_REPO).with_suffix("").as_posix()
    viewer = os.environ.get("WIKI_VIEWER_URL") or site_url("wiki.local", 8765)[2]
    links = [("Wiki 문서 보기 (로컬 뷰어)", f"{viewer}/?path={quote(rel)}")]
    try:
        pushed = bool(git("ls-tree", "-r", "--name-only", "@{u}", "--", WIKI_PAGE.relative_to(WIKI_REPO).as_posix()))
    except RuntimeError:
        pushed = False
    links.append(("Azure DevOps Wiki", dv.wiki_url(WIKI_PAGE)) if pushed
                 else ("Azure DevOps Wiki 에는 없음 (로컬 전용 문서, SCENARIO2_PUSH=1 이면 올림)", ""))
    return links


def ask_approval(diffs: list[dict], when: str, timeout: float, links: list[tuple[str, str]], ai: dict | None = None,
                 alert: str = "", guessed: list[str] | None = None):
    """Teams 카드(또는 브라우저)로 승인을 받는다. 승인 서버는 처리가 끝날 때까지 열어 두고 진행 상황을 보여 준다.
    alert: 중지된 VM 같은 이상 징후(맨 위에 보인다). 돌려주는 값: (결정 True/False/None, Approval)."""
    from teams_tools import teams_notify
    from teams_tools.approval_local import Approval

    items = explained(diffs, ai)
    head = (f"AI 요약 ({ai['model']}, 참고용): {ai['summary']}" if ai
            else "AI 요약 없음 (규칙 비교 결과만 표시)")
    if alert:
        head = alert + "\n\n" + head
    webhook = bool(os.environ.get("TEAMS_WEBHOOK_URL"))
    ap = Approval(f"Proxy 현황 변경 {len(diffs)}건", ([alert] if alert else []) + [head.split("\n\n")[-1]] + items,
                  "Wiki 현행화와 Task 등록을 진행할까요?", links, guessed)
    with span("approval", channel="teams" if webhook else "local", timeout_s=timeout) as s:
        page_url, yes_url, no_url = ap.start(open_browser=not webhook)
        if webhook:
            with span("teams_notify", kind="approval_request", buttons=3 + sum(1 for _, u in links if u)) as n:
                n.set(http=teams_notify.send(
                    "Wiki 의 Proxy 현황과 Azure 의 실제 VM 이 다릅니다. 현행화할까요?\n\n" + head + "\n\n"
                    + "\n".join(f"- {i}" for i in items)
                    + (f"\n\n**프록시로 추정한 VM {len(guessed)}대(한 번만 확인)**: {', '.join(guessed)}. "
                       "Yes 를 누르면 모두 프록시로 확정합니다. 프록시가 아닌 VM 이 있으면 '자세히 보기'에서 빼고 승인하세요."
                       if guessed else "")
                    + f"\n\n버튼은 이 알림을 보낸 PC({os.environ.get('COMPUTERNAME', '-')})에서 눌러야 합니다. "
                      f"{int(timeout // 60)}분 안에 답이 없으면 취소됩니다.",
                    f"[Proxy 현황] 변경 {len(diffs)}건 · 승인 요청",
                    facts=[("리소스 그룹", RESOURCE_GROUP), ("감지 시각", when), ("Wiki 문서", wiki_fact(links))]
                          + [("", label) for label, url in links if not url],
                    buttons=[("Yes · 현행화", yes_url), ("No · 기록만", no_url), ("자세히 보기", page_url)]
                            + [(label.split(" (")[0], url) for label, url in links if url]))
        result = ap.wait(timeout)
        s.set(decision={True: "yes", False: "no", None: "timeout"}[result])
    return result, ap


# ---------------------------------------------------------------- 6·8 Azure DevOps

def devops():
    from proxy_policy_publish import DevOps
    return DevOps()


def assignee(dv) -> str | None:
    """SCENARIO2_ASSIGNEE, 없으면 PAT 소유자의 계정(메일)."""
    if os.environ.get("SCENARIO2_ASSIGNEE"):
        return os.environ["SCENARIO2_ASSIGNEE"]
    from urllib.parse import quote
    from urllib.request import Request, urlopen
    try:  # connectionData 는 프로젝트가 아니라 조직 주소에서만 동작한다
        req = Request(f"https://dev.azure.com/{quote(dv.org)}/_apis/connectionData", headers={"Authorization": dv.auth})
        with urlopen(req, timeout=20) as res:
            me = json.load(res)["authenticatedUser"]
        return me.get("properties", {}).get("Account", {}).get("$value") or me.get("providerDisplayName")
    except (OSError, ValueError, KeyError):
        return None


def work_item(dv, item_id: int, relations: bool = False) -> dict:
    return dv.request("GET", f"/_apis/wit/workitems/{item_id}?api-version=7.1"
                      + ("&$expand=relations" if relations else ""), None)


def children(dv, item_id: int) -> list[dict]:
    """하위 작업 항목(id, 유형, 제목)."""
    rels = work_item(dv, item_id, True).get("relations") or []
    ids = [r["url"].rsplit("/", 1)[1] for r in rels if r["rel"] == "System.LinkTypes.Hierarchy-Forward"]
    if not ids:
        return []
    items = dv.request("GET", f"/_apis/wit/workitems?ids={','.join(ids[:200])}"
                              "&fields=System.Id,System.WorkItemType,System.Title&api-version=7.1", None)["value"]
    return [{"id": w["id"], "type": w["fields"]["System.WorkItemType"], "title": w["fields"]["System.Title"]} for w in items]


def parent_link(dv, parent_id: int) -> dict:
    return {"op": "add", "path": "/relations/-",
            "value": {"rel": "System.LinkTypes.Hierarchy-Reverse",
                      "url": f"https://dev.azure.com/{dv.org}/_apis/wit/workItems/{parent_id}"}}


def ensure_child(dv, parent_id: int, kind: str, title: str, description: str) -> tuple[int, bool]:
    """parent 아래에 같은 유형·제목의 항목이 있으면 그 id, 없으면 만들고 (id, 만들었는지)."""
    for c in children(dv, parent_id):
        if c["type"] == kind and c["title"] == title:
            return c["id"], False
    pf = work_item(dv, parent_id)["fields"]
    ops = [{"op": "add", "path": "/fields/System.Title", "value": title},
           {"op": "add", "path": "/fields/System.Description", "value": description},
           {"op": "add", "path": "/fields/System.AreaPath", "value": pf["System.AreaPath"]},
           {"op": "add", "path": "/fields/System.IterationPath", "value": pf["System.IterationPath"]},
           {"op": "add", "path": "/fields/System.Tags", "value": "proxy-status; scenario2"},
           parent_link(dv, parent_id)]
    item = dv.request("POST", f"/_apis/wit/workitems/${quote_type(kind)}?api-version=7.1", ops, "application/json-patch+json")
    return item["id"], True


def quote_type(kind: str) -> str:
    from urllib.parse import quote
    return quote(kind)


def backlog(dv, when: str) -> dict:
    """Epic → Feature → 그 달 PBI 를 찾거나 만들고, PBI 아래 다음 Task 순번을 돌려준다."""
    epic = int(os.environ.get("SCENARIO2_EPIC_ID", "1"))
    feature_title = os.environ.get("SCENARIO2_FEATURE", "[공통] Proxy 현황 관리")
    pbi_title = f"{when[2:4]}{when[5:7]}) " + os.environ.get("SCENARIO2_PBI", "Proxy VM 현황 Wiki 현행화")
    feature, f_new = ensure_child(dv, epic, "Feature", feature_title,
                                  "<p>Wiki 의 Proxy 현황 문서를 Azure 실제 VM 과 맞추는 운영 업무(시나리오 2).</p>")
    pbi, p_new = ensure_child(dv, feature, "Product Backlog Item", pbi_title,
                              "<p>이번 달 Proxy VM 현황 Wiki 현행화 건. 실행마다 아래에 Task 가 하나씩 붙는다 "
                              "(proxy_tools/proxy_status_sync.py).</p>")
    numbers = [int(c["title"][:2]) for c in children(dv, pbi) if c["type"] == "Task" and c["title"][:2].isdigit()]
    return {"epic": epic, "feature": feature, "feature_new": f_new, "pbi": pbi, "pbi_title": pbi_title,
            "pbi_new": p_new, "next": max(numbers, default=0) + 1}


def create_task(dv, diffs: list[dict], when: str, links: list[tuple[str, str]], owner: str | None,
                parent: dict | None = None, ai: dict | None = None) -> int:
    rows = "".join(f"<li>{html.escape(line)}</li>" for line in explained(diffs, ai))
    title = (f"{parent['next']:02d}. " if parent else "") + f"[Proxy 현황] {RESOURCE_GROUP} 변경 {len(diffs)}건 현행화 ({when})"
    body = (f"<p>Wiki Proxy 현황과 Azure 실제 VM 비교 결과 차이 {len(diffs)}건이 {html.escape(when)} 에 감지되어 "
            f"Teams 에서 승인되었습니다.</p><ul>{rows}</ul>"
            + "<p>현황 문서 " + html.escape(WIKI_PAGE.stem) + ": "
            + " · ".join(f'<a href="{html.escape(u)}">{html.escape(l)}</a>' if u else html.escape(l) for l, u in links)
            + "</p>"
            + (f"<p><b>AI 요약</b> ({html.escape(ai['model'])}, 참고용): "
               f"{html.escape(ai['summary'])}</p>" if ai else "")
            + "<p>등록: proxy_status_sync.py (시나리오 2)</p>")
    ops = [{"op": "add", "path": "/fields/System.Title", "value": title[:250]},
           {"op": "add", "path": "/fields/System.Description", "value": body},
           {"op": "add", "path": "/fields/System.Tags", "value": "proxy-status; scenario2"}]
    if owner:
        ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": owner})
    if parent:
        pf = work_item(dv, parent["pbi"])["fields"]
        ops += [{"op": "add", "path": "/fields/System.AreaPath", "value": pf["System.AreaPath"]},
                {"op": "add", "path": "/fields/System.IterationPath", "value": pf["System.IterationPath"]},
                parent_link(dv, parent["pbi"])]
    return dv.request("POST", "/_apis/wit/workitems/$Task?api-version=7.1", ops, "application/json-patch+json")["id"]


def complete_task(dv, task_id: int, note: str) -> str:
    """프로세스(Basic·Agile·Scrum)마다 완료 상태 이름이 달라 'Completed' 범주의 상태를 찾아 쓴다."""
    states = dv.request("GET", "/_apis/wit/workitemtypes/Task/states?api-version=7.1", None).get("value", [])
    done = next((s["name"] for s in states if s.get("category") == "Completed"), "Done")
    dv.request("PATCH", f"/_apis/wit/workitems/{task_id}?api-version=7.1",
               [{"op": "add", "path": "/fields/System.State", "value": done}], "application/json-patch+json")
    dv.add_comment(task_id, note)
    return done


# ---------------------------------------------------------------- 7 Wiki

def git(*args: str) -> str:
    res = subprocess.run(["git", "-C", str(WIKI_REPO), *args], capture_output=True)
    out = (res.stdout + res.stderr).decode("utf-8", errors="replace").strip()
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out}")
    return out


def updated_page(text: str, wiki: list[dict], azure: list[dict], diffs: list[dict], when: str, task: str) -> str:
    keep = {r["name"]: r.get("proxy") for r in wiki}  # Proxy(용도) 칸은 Wiki 값을 살린다
    rows = [{**a, "proxy": keep.get(a["name"]) or a["proxy"]} for a in azure]
    head, rest = text.split("## VM 목록", 1)
    lines = rest.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("|"))
    end = next((i for i in range(start, len(lines)) if not lines[i].strip().startswith("|")), len(lines))
    rest = "\n".join(lines[:start] + table_rows(rows).splitlines() + lines[end:]) + ("\n" if text.endswith("\n") else "")
    page = head + "## VM 목록" + rest
    page = re.sub(r"(> 최종 수정: )[^·\n]+", rf"\g<1>{when[:10]} ", page, count=1)
    # '## 변경 이력' 표의 마지막 행 뒤에 한 줄을 더한다(표가 없으면 절과 표를 만든다).
    row = f"| {when} | {', '.join(describe(d) for d in diffs)} | {task} |"
    lines = page.rstrip("\n").splitlines()
    sec = next((i for i, l in enumerate(lines) if l.strip() == "## 변경 이력"), None)
    if sec is None:
        lines += ["", "## 변경 이력", "", "| 일시 | 변경 내용 | Task |", "|---|---|---|", row]
    else:
        first = next((i for i in range(sec + 1, len(lines)) if lines[i].strip().startswith("|")), None)
        if first is None:
            lines[sec + 1:sec + 1] = ["", "| 일시 | 변경 내용 | Task |", "|---|---|---|", row]
        else:
            last = first
            while last + 1 < len(lines) and lines[last + 1].strip().startswith("|"):
                last += 1
            lines.insert(last + 1, row)
    return "\n".join(lines) + "\n"


def write_wiki(text: str, message: str, push: bool) -> str:
    WIKI_PAGE.write_text(text, encoding="utf-8", newline="\n")
    back = WIKI_PAGE.read_text(encoding="utf-8")  # WIKI_DOCUMENT_RULES: 다시 읽어 깨짐 검사
    if back != text or "�" in back or "???" in back:
        raise RuntimeError("Wiki 문서 인코딩 검사 실패")
    rel = WIKI_PAGE.relative_to(WIKI_REPO).as_posix()
    git("add", "--", rel)
    git("commit", "-q", "-m", message, "--", rel)
    commit = git("rev-parse", "--short", "HEAD")
    if push:
        git("push")
    return commit


# ---------------------------------------------------------------- main

def run(args) -> int:
    when = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    with span("trigger", mode="dry-run" if args.dry_run else "teams", push=PUSH):
        pass
    with span("wiki_read", page=WIKI_PAGE.name) as s:
        if not WIKI_PAGE.is_file():
            raise RuntimeError(f"Wiki 현황 문서가 없습니다: {WIKI_PAGE.relative_to(WIKI_REPO)}. "
                               "테스트라면 python test/scenario2/setup_env.py 로 만드세요.")
        text = WIKI_PAGE.read_text(encoding="utf-8-sig")
        try:
            wiki, parsed_by = read_wiki(text), "rule"
        except RuntimeError as exc:  # 정해진 표 형식이 아님 → LLM 파싱
            s.set(rule_failed=str(exc))
            wiki, parsed_by = llm_parse(text), "llm"
        s.set(parsed_by=parsed_by, rows=len(wiki), names=[r["name"] for r in wiki])
    if parsed_by == "llm":
        print(f"Wiki 문서를 LLM 으로 읽었습니다(정해진 표 형식이 아님): VM {len(wiki)}대. 이 문서는 7단계에서 자동으로 고치지 않습니다.")
    with span("azure_read", resource_group=RESOURCE_GROUP, pattern=NAME_PATTERN) as s:
        azure = proxy_vms()
        s.set(vms=len(azure), names=[v["name"] for v in azure], tag=os.environ.get("SCENARIO2_TAG", "role=proxy"),
              by_tag=sum(v["matched_by"] == "tag" for v in azure), by_name=sum(v["matched_by"] == "name" for v in azure))
    guessed = [v["name"] for v in azure if v["matched_by"] == "name"]
    if guessed:
        print(f"프록시로 추정한 VM {len(guessed)}대(이름 규칙, 태그 없음): {', '.join(guessed)} → 승인 때 한 번 확인합니다.")
    with span("compare") as s:
        diffs = compare(wiki, azure)
        s.set(diffs=len(diffs), summary=[describe(d) for d in diffs][:6])
    if verify_target():  # 테스트 문서면 정답과 자동 대조
        verify(diffs)
    alert = stopped_alert(azure)  # 실제 운영 가정: Proxy VM 은 항상 켜져 있어야 한다
    if alert:
        print(f"\n{alert}")
    if not diffs:
        print("Wiki 와 Azure 가 같습니다. 종료합니다.")
        if alert and not args.dry_run and os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            with span("teams_notify", kind="stopped_alert"):  # 문서는 맞아도 VM 이 꺼져 있으면 알린다
                teams_notify.send(alert, "[Proxy 현황] 이상 징후", facts=[("리소스 그룹", RESOURCE_GROUP), ("감지 시각", when)])
        return 0
    ai = ai_summary(diffs, wiki, azure)
    if ai:
        print(f"\nAI 요약 ({ai['model']}, 참고용): {ai['summary']}")
    print(f"\n차이 {len(diffs)}건" + "".join(f"\n  - {line}" for line in explained(diffs, ai)))
    if args.dry_run:
        print("[dry-run] Teams 승인 요청 → Task 생성 → " + (f"Wiki 표 {len(azure)}행으로 갱신 → 완료 보고" if parsed_by == "rule"
              else "문서 수동 반영 요청(형식이 달라 자동 수정 안 함)") + " (아무것도 바꾸지 않음)")
        return 0

    dv = devops()
    links = wiki_links(dv)
    decision, ap = ask_approval(diffs, when, APPROVAL_TIMEOUT, links, ai, alert, guessed)
    try:
        if decision:
            kept = confirm_proxies(ap, azure)
            if len(kept) != len(azure):  # '프록시 아님'으로 뺀 VM 이 있으면 다시 비교한다
                azure, diffs = kept, compare(wiki, kept)
                print(f"뺀 VM 을 제외하고 다시 비교: 차이 {len(diffs)}건")
                if not diffs:
                    ap.finish("프록시가 아닌 VM 을 빼고 나니 바꿀 내용이 없습니다. Wiki 문서와 Task 는 그대로입니다.",
                              [("Wiki 문서 보기", links[0][1])])
                    return 0
        return apply(args, dv, links, decision, ap, text, wiki, azure, diffs, when, ai, parsed_by)
    except Exception as exc:
        if ap:
            ap.step(f"실패: {exc}", "fail")
            ap.finish(f"처리 중 오류가 났습니다: {exc}", ok=False)
        raise
    finally:
        if ap:
            ap.close()


def apply(args, dv, links, decision, ap, text, wiki, azure, diffs, when, ai=None, parsed_by="rule") -> int:
    """5단계 이후: 거절이면 알리고 끝, 승인이면 Task → Wiki → 완료 보고. ap 가 있으면 승인 화면에 진행 상황을 보인다."""
    step = ap.step if ap else (lambda *a, **k: None)
    viewer = links[0][1]  # 로컬 뷰어 링크(wiki_links 첫 항목)
    if not decision:
        reason = "거절(No)" if decision is False else "시간 초과"
        print(f"승인되지 않아 종료합니다: {reason}. 기록은 data/traces/scenario2-*.jsonl 에 남았습니다.")
        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            with span("teams_notify", kind="declined"):
                teams_notify.send(f"Proxy 현황 변경 {len(diffs)}건은 {reason}로 반영하지 않았습니다.",
                                  "[Proxy 현황] 반영 안 함", facts=[("감지 시각", when)])
            step("Teams 에 '반영 안 함' 알림을 보냈습니다", "ok")
        if ap:
            ap.finish(f"{reason}: 아무것도 바꾸지 않았습니다. Wiki 문서와 Task 는 그대로입니다.", [("Wiki 문서 보기", viewer)])
        return 1

    step("Azure DevOps 백로그(Feature·PBI) 확인 중…")
    with span("backlog") as s:
        parent = backlog(dv, when)
        s.set(epic=parent["epic"], feature=parent["feature"], feature_new=parent["feature_new"],
              pbi=parent["pbi"], pbi_new=parent["pbi_new"], next_task=parent["next"])
    made = [n for n, new in (("Feature", parent["feature_new"]), ("PBI", parent["pbi_new"])) if new]
    print(f"백로그: Epic #{parent['epic']} → Feature #{parent['feature']} → PBI #{parent['pbi']} {parent['pbi_title']}"
          + (f" ({', '.join(made)} 새로 만듦)" if made else ""))
    step(f"백로그: Feature #{parent['feature']} → PBI #{parent['pbi']} {parent['pbi_title']}"
         + (f" ({', '.join(made)} 새로 만듦)" if made else ""), "ok", (f"PBI #{parent['pbi']} 열기", dv.task_url(parent["pbi"])))
    step("Azure DevOps Task 생성 중…")
    with span("task_create") as s:
        owner = assignee(dv)
        task_id = create_task(dv, diffs, when, links, owner, parent, ai)
        s.set(task_id=task_id, parent_pbi=parent["pbi"], number=parent["next"], assigned_to=owner)
    print(f"Task #{task_id} 생성 (PBI #{parent['pbi']} 아래 {parent['next']:02d}번, 담당 {owner or '-'}): {dv.task_url(task_id)}")
    step(f"Task #{task_id} 생성 ({parent['next']:02d}번, 담당 {owner or '-'})", "ok", (f"Task #{task_id} 열기", dv.task_url(task_id)))
    if parsed_by == "llm":  # 형식이 제각각인 문서는 자동으로 고치지 않는다
        with span("wiki_update", skipped="LLM 으로 읽은 문서(정해진 표 형식 아님)"):
            dv.add_comment(task_id, "<p><b>문서 수동 반영 필요</b>: 이 현황 문서는 정해진 표 형식이 아니어서(LLM 으로 읽음) "
                                    "자동으로 고치지 않았습니다. 위 변경 내역을 문서에 반영한 뒤 이 Task 를 완료 처리해 주세요.</p>")
        step("Wiki 문서는 형식이 달라 자동으로 고치지 않음 → Task 에 수동 반영 요청을 남김", "ok", (f"Task #{task_id} 열기", dv.task_url(task_id)))
        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            with span("teams_notify", kind="manual_update"):
                teams_notify.send(f"Proxy 현황 변경 {len(diffs)}건으로 Task 를 만들었습니다. 이 문서는 형식이 달라 자동으로 고치지 않았으니 "
                                  "Task 를 보고 문서를 고쳐 주세요.", "[Proxy 현황] 문서 수동 반영 필요",
                                  facts=[("Wiki 문서", wiki_fact(links)), ("Task", f"[#{task_id}]({dv.task_url(task_id)})"), ("담당", owner or "-")],
                                  buttons=[("Task 열기", dv.task_url(task_id))])
        print(f"Wiki 문서는 자동으로 고치지 않았습니다(LLM 파싱 문서). Task #{task_id} 에 수동 반영 요청을 남겼습니다.")
        if ap:
            ap.finish("Task 를 만들었습니다. 이 문서는 형식이 달라 자동으로 고치지 않았으니 Task 를 보고 문서를 고쳐 주세요.",
                      [(f"Task #{task_id} 열기", dv.task_url(task_id)), ("Wiki 문서 보기", viewer)])
        return 0
    step("Wiki 문서 현행화 중…")

    with span("wiki_update", push=PUSH) as s:
        page = updated_page(text, wiki, azure, diffs, when, f"[#{task_id}]({dv.task_url(task_id)})")
        commit = write_wiki(page, f"Proxy 현황 현행화: 변경 {len(diffs)}건 (Task #{task_id})", PUSH)
        s.set(commit=commit, rows=len(azure))
    print(f"Wiki 현행화 커밋 {commit}" + (" · push 완료" if PUSH else " (로컬 커밋, SCENARIO2_PUSH=1 이면 Azure DevOps Wiki 에 반영)"))
    step(f"Wiki 표 {len(azure)}행으로 현행화, 변경 이력 추가 (커밋 {commit}" + (", push 완료)" if PUSH else ", 로컬)"),
         "ok", ("수정된 문서 열기", viewer))
    step("Task 완료 처리와 Teams 결과 알림 중…")

    with span("report") as s:
        state = complete_task(dv, task_id, f"Wiki 현행화 완료: wiki-repo 커밋 {commit}"
                                           + (" (Azure DevOps Wiki push 완료)" if PUSH else " (로컬 커밋)"))
        s.set(task_state=state)
        if os.environ.get("TEAMS_WEBHOOK_URL"):
            from teams_tools import teams_notify
            teams_notify.send(f"Proxy 현황 변경 {len(diffs)}건을 반영했습니다.\n\n" + "\n".join(f"- {describe(d)}" for d in diffs),
                              "[Proxy 현황] 현행화 완료",
                              facts=[("Wiki 문서", wiki_fact(links)), ("Task", f"[#{task_id}]({dv.task_url(task_id)}) ({state})"), ("Wiki 커밋", commit + (" · push" if PUSH else " · 로컬")),
                                     ("담당", owner or "-")],
                              buttons=[("수정된 Wiki 문서 열기", viewer), ("Task 열기", dv.task_url(task_id))])
            s.set(teams="sent")
    print(f"완료 보고: Task #{task_id} → {state}" + (", Teams 알림 보냄" if os.environ.get("TEAMS_WEBHOOK_URL") else ""))
    step(f"Task #{task_id} → {state}" + (", Teams 결과 알림 보냄" if os.environ.get("TEAMS_WEBHOOK_URL") else ""), "ok")
    if ap:
        ap.finish("Wiki 문서가 수정되었습니다. 이미 열어 둔 문서 창은 새로고침(F5) 하세요.",
                  [("수정된 Wiki 문서 열기", viewer), (f"Task #{task_id} 열기", dv.task_url(task_id))]
                  + [(l, u) for l, u in links[1:] if u])
    return 0


LOG_FILE = DATA_DIR / "logs" / "scenario2.log"  # 허브의 '작업 로그'에서 본다


class Tee:
    """화면 출력을 그대로 로그 파일에도 쓴다."""

    def __init__(self, stream, file):
        self.stream, self.file = stream, file

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def open_log():
    """data/logs/scenario2.log 를 연다(2MB 가 넘으면 .old.log 로 넘긴다). 실행마다 머리줄을 쓴다."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.is_file() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
        LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    f = LOG_FILE.open("a", encoding="utf-8")
    f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: proxy_status_sync.py {' '.join(sys.argv[1:])} =====\n")
    return f


def main() -> int:
    log = open_log()
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(out, log), Tee(err, log)
    try:
        return _main()
    finally:
        sys.stdout, sys.stderr = out, err
        log.close()


def _main() -> int:
    p = argparse.ArgumentParser(description="시나리오 2: Proxy 현황 Wiki 현행화")
    p.add_argument("--dry-run", action="store_true", help="1~4단계만 하고 할 일을 출력(아무것도 바꾸지 않음)")
    args = p.parse_args()
    try:
        with trace("scenario2", "proxy_status_sync", resource_group=RESOURCE_GROUP, dry_run=args.dry_run) as t:
            code = run(args)
            t.set(exit=code)
            return code
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
