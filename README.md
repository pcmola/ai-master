# AI Master

Azure DevOps Wiki를 관리하고, 사내 Azure OpenAI 게이트웨이와 Claude/Codex 사용량을 확인하기 위한 도구 모음입니다.

## 구성

- `wiki_search.py`: Wiki 문서 검색 및 가이드 생성
- `wiki_documents.py`: Azure DevOps Wiki 문서 관리
- `wiki_viewer.py`: Wiki 로컬 뷰어
- `usage_monitor.py`: Claude/Codex 사용량 대시보드
- `teams_notify.py`, `teams_graph.py`: Microsoft Teams 알림 도구
- `test/`: Azure OpenAI 채팅, 임베딩, 스트리밍 테스트

## 설정

`.env.sample`을 복사해 `.env`를 만들고 실제 인증정보를 입력합니다.

```powershell
Copy-Item .env.sample .env
```

`.env`에는 API 키와 PAT가 포함될 수 있으므로 Git에 커밋하지 않습니다. `.env.sample`에는 민감한 값 대신 placeholder를 사용합니다.

## 실행 예시

```powershell
python wiki_search.py
python wiki_documents.py list
python usage_monitor.py
```

사용량 모니터는 기본적으로 `http://127.0.0.1:8767`에서 실행됩니다.

## 주의사항

- `AZURE_OPENAI_API_KEY`, `AZURE_DEVOPS_PAT`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`를 소스에 직접 작성하지 마세요.
- 이미 노출된 인증정보는 폐기하고 재발급하세요.
- `wiki-repo`, `wiki-backup`, `wiki/AI-Master.wiki`는 별도 Git 저장소를 포함합니다.
