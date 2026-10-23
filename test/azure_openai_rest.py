"""Standard-library fallback client for Azure OpenAI smoke tests."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def load_project_env() -> None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _request(path: str, body: dict) -> dict:
    load_project_env()
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    version = os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview")
    if not endpoint or not key:
        raise RuntimeError(".env에 AZURE_OPENAI_ENDPOINT와 AZURE_OPENAI_API_KEY를 설정하세요.")
    url = f"{endpoint}{path}&api-version={urllib.parse.quote(version)}"
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json", "api-key": key})
    # 로컬 개발 환경의 사용 중지된 127.0.0.1:9 프록시를 피한다.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as response:
        return json.load(response)


def chat(model: str, text: str) -> dict:
    deployment = urllib.parse.quote(model, safe="")
    return _request(f"/openai/deployments/{deployment}/chat/completions?",
                    {"model": model, "messages": [{"role": "user", "content": text}]})


def embeddings(model: str, inputs: list[str]) -> dict:
    deployment = urllib.parse.quote(model, safe="")
    return _request(f"/openai/deployments/{deployment}/embeddings?",
                    {"model": model, "input": inputs})
