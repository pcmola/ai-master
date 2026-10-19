"""Azure DevOps Wiki 문서 관리 (Python 표준 라이브러리만 사용).

환경 변수: AZURE_DEVOPS_ORG, AZURE_DEVOPS_PROJECT, AZURE_DEVOPS_WIKI,
           AZURE_DEVOPS_PAT (Wiki Read & write 권한 필요)

python wiki_tools/wiki_documents.py list
python wiki_tools/wiki_documents.py create --parent '/AI Master 프로젝트/02. 운영/03. 업무 2' --title '샘플 문서'
python wiki_tools/wiki_documents.py create --parent '/AI Master 프로젝트/02. 운영' --title '업무 문서' --file content.md
python wiki_tools/wiki_documents.py delete --path '/AI Master 프로젝트/02. 운영/03. 업무 2/샘플 문서'
"""

import argparse
import atexit
import base64
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def load_env():
    """스크립트 옆 .env를 읽습니다. 기존 환경 변수 값을 우선합니다."""
    env_path = Path(__file__).resolve().parents[1] / '.env'  # 프로젝트 루트의 .env
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not key.strip().isidentifier():
            raise ValueError('.env는 KEY=VALUE 형식으로 작성하세요.')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


class WikiClient:
    _metrics = {
        'calls': 0,
        'status': {200: 0, 429: 0, 503: 0},
        'elapsed': [],
        'retry_after': 0.0,
        'rate_limit_delay': 0.0,
    }

    def __init__(self):
        load_env()
        names = ('AZURE_DEVOPS_ORG', 'AZURE_DEVOPS_PROJECT',
                 'AZURE_DEVOPS_WIKI', 'AZURE_DEVOPS_PAT')
        values = [os.environ.get(name, '').strip() for name in names]
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise ValueError('환경 변수를 설정하세요: ' + ', '.join(missing))
        org, project, wiki, pat = values
        self.url = ('https://dev.azure.com/' + quote(org, safe='') + '/'
                    + quote(project, safe='') + '/_apis/wiki/wikis/'
                    + quote(wiki, safe='') + '/pages')
        self.authorization = 'Basic ' + base64.b64encode(
            (':' + pat).encode('utf-8')).decode('ascii')

    @staticmethod
    def _header_seconds(value):
        if not value:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def print_metrics(cls):
        m = cls._metrics
        elapsed = m['elapsed']
        total = sum(m['status'].values())
        print(json.dumps({
            'api_calls': m['calls'],
            'http_200': m['status'][200],
            'http_429': m['status'][429],
            'http_503': m['status'][503],
            'avg_response_seconds': round(sum(elapsed) / len(elapsed), 4) if elapsed else 0,
            'max_response_seconds': round(max(elapsed), 4) if elapsed else 0,
            'retry_after_seconds': round(m['retry_after'], 4),
            'rate_limit_delay_seconds': round(m['rate_limit_delay'], 4),
            'documents_per_second': round(total / sum(elapsed), 4) if elapsed and sum(elapsed) else 0,
        }, ensure_ascii=True))

    def request(self, method, path, content=None, **query):
        params = {'path': path, 'api-version': '7.1', **query}
        headers = {'Authorization': self.authorization,
                   'Accept': 'application/json'}
        data = None
        if content is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
            data = json.dumps({'content': content}, ensure_ascii=False).encode('utf-8')
        request = Request(self.url + '?' + urlencode(params), data=data,
                          headers=headers, method=method)
        started = time.perf_counter()
        WikiClient._metrics['calls'] += 1
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
                elapsed = time.perf_counter() - started
                WikiClient._metrics['elapsed'].append(elapsed)
                status = response.status
                if status in WikiClient._metrics['status']:
                    WikiClient._metrics['status'][status] += 1
                WikiClient._metrics['retry_after'] += self._header_seconds(
                    response.headers.get('Retry-After'))
                WikiClient._metrics['rate_limit_delay'] += self._header_seconds(
                    response.headers.get('X-RateLimit-Delay'))
                return json.loads(body) if body else {}
        except HTTPError as error:
            elapsed = time.perf_counter() - started
            WikiClient._metrics['elapsed'].append(elapsed)
            if error.code in WikiClient._metrics['status']:
                WikiClient._metrics['status'][error.code] += 1
            WikiClient._metrics['retry_after'] += self._header_seconds(
                error.headers.get('Retry-After'))
            WikiClient._metrics['rate_limit_delay'] += self._header_seconds(
                error.headers.get('X-RateLimit-Delay'))
            # 인증 값이나 응답 전문을 출력하지 않습니다.
            hints = {401: 'PAT를 확인하세요.', 403: 'Wiki 쓰기 권한을 확인하세요.',
                     404: '조직, 프로젝트, Wiki 이름 및 페이지 경로를 확인하세요.',
                     409: '이미 존재하는 페이지이거나 충돌이 발생했습니다.',
                     412: '페이지가 이미 존재하거나 버전이 변경되었습니다.'}
            raise RuntimeError(f'HTTP {error.code}: '
                               + hints.get(error.code, 'Wiki API 요청에 실패했습니다.')) from None


atexit.register(WikiClient.print_metrics)


def normalize_path(value):
    parts = value.strip('/').split('/')
    if value == '/':
        return '/'
    if any(not part.strip() or part in ('.', '..') for part in parts):
        raise ValueError('페이지 경로의 각 이름은 비어 있을 수 없습니다. . 또는 ..도 사용할 수 없습니다.')
    return '/' + '/'.join(parts)


def validate_markdown_content(content):
    """생성 문서에 제목 중복·형식적인 수행 내용이 들어가지 않게 검증합니다."""
    lines = content.replace('\r\n', '\n').split('\n')
    title = next((line[2:].strip() for line in lines
                  if line.startswith('# ') and not line.startswith('## ')), '')
    for phrase in ('기준 정리', '현황 확인'):
        if f'{phrase} {phrase}' in title:
            raise ValueError('문서 제목에 동일한 업무 단계가 중복되어 있습니다.')
    if '??' in title:
        raise ValueError('문서 제목에 인코딩 오류로 보이는 문자가 있습니다.')

    try:
        purpose_index = lines.index('## 목적')
    except ValueError:
        return

    purpose = next((line.strip() for line in lines[purpose_index + 1:] if line.strip()), '')
    purpose_suffix = '업무 현황과 변경 이력을 관리한다.'
    if re.match(r'^\d{1,2}\s', purpose) and purpose.endswith(purpose_suffix):
        raise ValueError('목적에는 문서 번호·제목을 포함하지 말고 업무 목적만 작성하세요.')

    try:
        perform_index = lines.index('## 수행 내용')
    except ValueError:
        return
    perform = []
    for line in lines[perform_index + 1:]:
        if line.startswith('## '):
            break
        if line.strip():
            perform.append(line.strip())
    generic = {
        '- 관련 자료와 담당자를 확인한다.',
        '- 처리 기준과 검증 결과를 기록한다.',
        '- 미해결 항목의 후속 일정을 공유한다.',
    }
    if perform and set(perform).issubset(generic):
        raise ValueError('근거 없는 형식적인 수행 내용을 문서에 반복해서 넣지 마세요.')


def print_tree(page, depth=0):
    print('  ' * depth + page.get('path', '/'))
    for child in page.get('subPages') or []:
        print_tree(child, depth + 1)


def main():
    parser = argparse.ArgumentParser(description='Azure DevOps Wiki 문서 생성·삭제·목록 조회')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='페이지 경로 목록 조회')
    listing.add_argument('--path', default='/')
    create = commands.add_parser('create', help='상위 페이지 아래 새 문서 생성')
    create.add_argument('--parent', required=True)
    create.add_argument('--title', required=True)
    source = create.add_mutually_exclusive_group()
    source.add_argument('--file', type=Path, help='UTF-8 Markdown 파일')
    source.add_argument('--content', help='직접 지정할 Markdown 본문')
    delete = commands.add_parser('delete', help='지정한 문서 삭제 (하위 페이지가 있으면 중단)')
    delete.add_argument('--path', required=True)
    args = parser.parse_args()

    try:
        client = WikiClient()
        if args.command == 'list':
            print_tree(client.request('GET', normalize_path(args.path), recursionLevel='full'))
        elif args.command == 'create':
            title = args.title.strip()
            if not title or '/' in title or '\\' in title or title in ('.', '..'):
                raise ValueError('제목은 비어 있거나 /, \\를 포함할 수 없습니다.')
            parent = normalize_path(args.parent)
            client.request('GET', parent)  # 실제 존재하는 상위 페이지인지 확인
            path = parent.rstrip('/') + '/' + title
            content = (args.file.read_text(encoding='utf-8-sig') if args.file
                       else args.content if args.content is not None
                       else f'# {title}\n\n임의로 생성한 샘플 문서입니다.\n')
            validate_markdown_content(content)
            # 기존 페이지 수정에 필요한 If-Match를 보내지 않아 덮어쓰기를 방지합니다.
            page = client.request('PUT', path, content)
            print('생성 완료:', page.get('path', path))
        else:
            path = normalize_path(args.path)
            if path == '/':
                raise ValueError('Wiki 루트는 삭제할 수 없습니다.')
            page = client.request('GET', path, recursionLevel='oneLevel')
            if page.get('subPages'):
                raise ValueError('하위 페이지가 있습니다. 하위 문서부터 개별 삭제하세요.')
            client.request('DELETE', path)
            print('삭제 완료:', path)
        return 0
    except (ValueError, RuntimeError, OSError, URLError) as error:
        print('오류:', error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
