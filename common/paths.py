"""프로젝트 경로. 스크립트가 어느 폴더에 있든 같은 .env·wiki-repo·data 를 쓰도록 한 곳에서 정한다."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
WIKI_REPO = PROJECT_ROOT / "wiki-repo"
# 실행 중에 생기는 파일(캐시·가상 로그·스냅샷·토큰). git 에 올리지 않는다.
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
