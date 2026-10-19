"""Local, read-only Azure DevOps Wiki viewer."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = (Path(__file__).resolve().parents[1] / "wiki-repo").resolve()  # 프로젝트 루트의 wiki-repo


def safe_path(value: str) -> Path:
    value = unquote(value or "").replace("\\", "/").lstrip("/")
    candidate = (ROOT / value).resolve()
    if candidate != ROOT and ROOT not in candidate.parents:
        raise ValueError("path outside wiki root")
    return candidate


def ordered_names(folder: Path) -> list[str]:
    entries = []
    order_file = folder / ".order"
    if order_file.is_file():
        entries = [line.strip() for line in order_file.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    available = {p.name for p in folder.iterdir() if p.name != ".order" and not p.name.startswith(".")}
    by_key = {}
    for name in available:
        key = name[:-3] if name.lower().endswith(".md") else name
        by_key.setdefault(key, []).append(name)
    result = []
    used = set()
    for entry in entries:
        choices = by_key.get(entry, [])
        chosen = next((x for x in choices if x in available and not x.lower().endswith(".md")), None)
        chosen = chosen or next((x for x in choices if x not in used), None)
        if chosen:
            result.append(chosen); used.update(choices)
    for key in sorted(by_key, key=str.casefold):
        choices = sorted(by_key[key], key=lambda x: (x.lower().endswith(".md"), x.casefold()))
        chosen = next((x for x in choices if x not in used), None)
        if chosen:
            result.append(chosen); used.update(choices)

    return result


def display_name(name: str, is_dir: bool) -> str:
    if not is_dir and name.lower().endswith(".md"):
        name = name[:-3]
    match = re.match(r"^(\d{1,2})-(.*)$", name)
    if match:
        return f"{match.group(1)}. {match.group(2).replace('-', ' ')}"
    return name.replace("-", " ")


def build_tree(folder: Path = ROOT, rel: str = "") -> list[dict]:
    nodes = []
    for name in ordered_names(folder):
        path = folder / name
        is_dir = path.is_dir()
        md = (path.parent / (path.name + ".md")) if is_dir else path
        if not is_dir and path.suffix.lower() != ".md":
            continue
        node_rel = f"{rel}/{name}".strip("/")
        page_rel = node_rel[:-3] if not is_dir else (f"{node_rel}.md" if md.is_file() else node_rel)
        nodes.append({
            "name": display_name(name, is_dir),
            "path": page_rel,
            "page": md.is_file(),
            "children": build_tree(path, node_rel) if is_dir else [],
        })
    return nodes


HTML_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI-Master Wiki</title><style>
:root{--ink:#172033;--muted:#718096;--line:#e5eaf0;--blue:#2563eb;--soft:#f5f8fc;--panel:#fff}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px/1.65 "Segoe UI","Malgun Gothic",sans-serif;background:#eef2f7}
.app{height:100vh;display:grid;grid-template-columns:310px 1fr;grid-template-rows:58px 1fr}.top{grid-column:1/-1;background:#13213a;color:#fff;display:flex;align-items:center;padding:0 24px;gap:14px}.mark{width:28px;height:28px;border-radius:8px;background:#42b883;display:grid;place-items:center;font-weight:800}.brand{font-size:16px;font-weight:700}.crumb{color:#aebbd0;font-size:12px;margin-left:8px}.sidebar{background:#fff;border-right:1px solid var(--line);overflow:auto;padding:16px 10px}.tree{list-style:none;margin:0;padding:0}.tree ul{list-style:none;margin:0;padding-left:18px}.tree li{margin:2px 0}.row{display:flex;align-items:center;gap:5px;min-height:34px;padding:4px 8px;border-radius:6px;cursor:pointer}.row:hover{background:#edf4ff}.row.active{background:#dceaff;color:#174ea6;font-weight:650}.twisty{width:14px;color:#66758a;text-align:center}.docicon{color:#5681b8;font-size:13px}.main{overflow:auto;background:var(--panel);padding:42px 8vw 80px}.article{max-width:880px;margin:auto}.article h1{font-size:34px;line-height:1.2;margin:0 0 12px;letter-spacing:-.03em}.article h2{font-size:23px;margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:7px}.article h3{font-size:18px;margin-top:28px}.article p{margin:13px 0}.article ul,.article ol{padding-left:26px}.article code{background:#eef2f7;border-radius:4px;padding:2px 5px;color:#a03}.article pre{background:#101827;color:#e6edf6;padding:16px;border-radius:9px;overflow:auto}.article a{color:var(--blue);text-decoration:none}.article a:hover{text-decoration:underline}.meta{color:var(--muted);font-size:12px;margin-bottom:28px}.article .tbl{overflow-x:auto;margin:14px 0}.article table{border-collapse:collapse;width:100%;font-size:13.5px}.article th,.article td{border:1px solid var(--line);padding:7px 10px;vertical-align:top}.article th{background:var(--soft);font-weight:650}.article blockquote{margin:14px 0;padding:6px 16px;border-left:4px solid #c7d5ea;background:var(--soft);color:#4a5568}.article blockquote p{margin:6px 0}.article hr{border:0;border-top:1px solid var(--line);margin:28px 0}.article del{color:var(--muted)}.tosp{display:inline-block;min-width:280px;border:1px solid var(--line);border-radius:6px;padding:12px 18px;margin:8px 0 16px}.tosp b{display:block;margin-bottom:6px}.tosp ul{margin:0;padding-left:20px}.mermaid{margin:16px 0;text-align:center;overflow-x:auto}.muted{color:var(--muted);font-size:12px}.raw code{white-space:pre-wrap}.article pre code{background:none;color:inherit;padding:0;border-radius:0}.empty{color:var(--muted);padding-top:20vh;text-align:center}.empty strong{display:block;color:var(--ink);font-size:22px;margin-bottom:8px}@media(max-width:760px){.app{grid-template-columns:1fr}.sidebar{position:fixed;z-index:3;top:58px;bottom:0;width:290px;transform:translateX(-100%);transition:.2s}.sidebar.open{transform:none}.main{padding:30px 22px}.top:before{content:"☰";font-size:20px;cursor:pointer}.top{padding-left:16px}}
</style></head><body><div class="app"><header class="top"><div class="mark">W</div><div class="brand">AI-Master Wiki</div><div class="crumb">로컬 읽기 전용 뷰어</div></header><aside class="sidebar"><ul id="tree" class="tree"></ul></aside><main class="main"><article id="article" class="article"><div class="empty"><strong>문서를 선택하세요</strong>왼쪽 트리에서 Wiki 페이지를 열 수 있습니다.</div></article></main></div>
<script>
const treeEl=document.querySelector('#tree'), article=document.querySelector('#article'), nodeByPath={};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
// 위키 링크: 상대 경로는 현재 문서 기준, '/AI-Master-프로젝트/...' 같은 위키 절대 경로도 뷰어 주소로 바꾼다.
function href(u,current){u=u.replace(/&amp;/g,'&');if(u.startsWith('#')||/^(https?:|mailto:)/.test(u))return u;let base=current.split('/').slice(0,-1).join('/');let p=u.startsWith('/')?u.slice(1):(base?base+'/':'')+u;try{p=decodeURIComponent(p)}catch(e){}return '?'+new URLSearchParams({path:p.replace(/\.md$/,'').replace(/^\/+/,'')})}
function inline(s,current){let x=esc(s);const codes=[];x=x.replace(/`([^`]+)`/g,(m,c)=>{codes.push(c);return '\u0000'+(codes.length-1)+'\u0000'});
x=x.replace(/!?\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g,(m,t,u)=>{const h=href(u,current);return `<a href="${esc(h)}"${/^https?:/.test(h)?' target="_blank" rel="noopener"':''}>${t}</a>`});
x=x.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g,'$1<em>$2</em>').replace(/~~([^~]+)~~/g,'<del>$1</del>').replace(/&lt;br\s*\/?&gt;/gi,'<br>');
return x.replace(/\u0000(\d+)\u0000/g,(m,i)=>'<code>'+codes[i]+'</code>')}
function list(items,ordered,path){const tag=ordered?'ol':'ul';let h='',st=[];for(const [lv,txt] of items){if(!st.length||lv>st[st.length-1]){h+=`<${tag}>`;st.push(lv)}else{h+='</li>';while(st.length>1&&lv<st[st.length-1]){h+=`</${tag}></li>`;st.pop()}}h+='<li>'+txt.split('\n').map(l=>inline(l,path)).join('<br>')}while(st.length){h+=`</li></${tag}>`;st.pop()}return h}
// [[_TOSP_]]: Azure DevOps 의 하위 페이지 목록. 왼쪽 트리(.order 순서)를 그대로 쓴다.
function tosp(path){const n=nodeByPath[path]||nodeByPath[path+'.md']||nodeByPath[path.replace(/\.md$/,'')];const kids=(n&&n.children)||[];
return '<div class="tosp"><b>Child Pages</b>'+(kids.length?'<ul>'+kids.map(c=>`<li><a href="?${new URLSearchParams({path:c.path})}">${esc(c.name)}</a></li>`).join('')+'</ul>':'<div class="muted">하위 페이지가 없습니다.</div>')+'</div>'}
function toc(heads,path){return '<div class="tosp"><b>목차</b>'+(heads.length?heads.map(([n,t,id])=>`<div style="padding-left:${(n-1)*14}px"><a href="#${id}">${inline(t,path)}</a></div>`).join(''):'<div class="muted">제목이 없습니다.</div>')+'</div>'}
const LIST=/^\s*([-*+]|\d+[.)])\s+/;
function md(src,path,nested){src=src.replace(/\r/g,'');if(!nested)src=src.replace(/^\s*#\s+[^\n]*(?:\n|$)/,'');const lines=src.split('\n'),heads=[],para=[];let out='',i=0;
const flush=()=>{if(para.length){out+='<p>'+para.map(l=>inline(l,path)).join('<br>')+'</p>';para.length=0}};
const row=s=>s.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim());
while(i<lines.length){const line=lines[i],t=line.trim();let m;
if(/^```/.test(t)){flush();const lang=t.slice(3).trim(),buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i].trim()))buf.push(lines[i++]);i++;out+=lang==='mermaid'?`<div class="mermaid">${esc(buf.join('\n'))}</div>`:`<pre><code>${esc(buf.join('\n'))}</code></pre>`;continue}
if(/^:::\s*mermaid\s*$/i.test(t)){flush();const buf=[];i++;while(i<lines.length&&lines[i].trim()!==':::')buf.push(lines[i++]);i++;out+=`<div class="mermaid">${esc(buf.join('\n'))}</div>`;continue}
if(/^\[\[_TOC_\]\]$/i.test(t)){flush();out+='\u0001TOC\u0001';i++;continue}
if(/^\[\[_TOSP_\]\]$/i.test(t)){flush();out+=tosp(path);i++;continue}
if(m=t.match(/^(#{1,6})\s+(.+)$/)){flush();const n=m[1].length,id='h-'+heads.length;heads.push([n,m[2],id]);out+=`<h${n} id="${id}">${inline(m[2],path)}</h${n}>`;i++;continue}
if(/^(-{3,}|\*{3,}|_{3,})$/.test(t)){flush();out+='<hr>';i++;continue}
if(t.startsWith('|')&&i+1<lines.length&&/^\|?\s*:?-{2,}/.test(lines[i+1].trim())){flush();const hdr=row(t),al=row(lines[i+1]).map(c=>c.startsWith(':')&&c.endsWith(':')?'center':c.endsWith(':')?'right':'left');i+=2;const body=[];while(i<lines.length&&lines[i].trim().startsWith('|'))body.push(row(lines[i++]));
out+='<div class="tbl"><table><thead><tr>'+hdr.map((c,j)=>`<th style="text-align:${al[j]||'left'}">${inline(c,path)}</th>`).join('')+'</tr></thead><tbody>'+body.map(r=>'<tr>'+hdr.map((_,j)=>`<td style="text-align:${al[j]||'left'}">${inline(r[j]||'',path)}</td>`).join('')+'</tr>').join('')+'</tbody></table></div>';continue}
if(t.startsWith('>')){flush();const buf=[];while(i<lines.length&&lines[i].trim().startsWith('>'))buf.push(lines[i++].trim().replace(/^>\s?/,''));out+='<blockquote>'+md(buf.join('\n'),path,true)+'</blockquote>';continue}
if(LIST.test(line)){flush();const ordered=/^\s*\d+[.)]/.test(line),items=[];while(i<lines.length&&LIST.test(lines[i])){items.push([lines[i].match(/^\s*/)[0].length,lines[i].replace(LIST,'')]);i++;while(i<lines.length&&/^\s{2,}\S/.test(lines[i])&&!LIST.test(lines[i]))items[items.length-1][1]+='\n'+lines[i++].trim()}out+=list(items,ordered,path);continue}
if(!t){flush();i++;continue}
para.push(line);i++}
flush();return out.replace('\u0001TOC\u0001',toc(heads,path))}
let mermaidLib;async function diagrams(){const els=[...article.querySelectorAll('.mermaid')];if(!els.length)return;
try{mermaidLib=mermaidLib||(await import('https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs')).default;mermaidLib.initialize({startOnLoad:false,securityLevel:'strict'});await mermaidLib.run({nodes:els,suppressErrors:true})}catch(e){console.warn('mermaid',e)}
els.filter(el=>!el.querySelector('svg')).forEach(el=>{el.outerHTML='<pre><code>'+esc(el.textContent)+'</code></pre><p class="muted">다이어그램을 그리지 못해 원문을 표시합니다.</p>'})}
function render(nodes,parent){for(const n of nodes){nodeByPath[n.path]=n;let li=document.createElement('li'),row=document.createElement('div');row.className='row';let twist=document.createElement('span');twist.className='twisty';twist.textContent=n.children.length?'▸':'';let icon=document.createElement('span');icon.className='docicon';icon.textContent=n.page?'▤':'▱';let label=document.createElement('span');label.textContent=n.name;row.append(twist,icon,label);li.append(row);let child;if(n.children.length){child=document.createElement('ul');child.hidden=true;render(n.children,child);li.append(child);row.addEventListener('click',e=>{if(e.target===twist||n.children.length){child.hidden=!child.hidden;twist.textContent=child.hidden?'▸':'▾'}if(n.page)openDoc(n.path)})}else if(n.page)row.addEventListener('click',()=>openDoc(n.path));parent.append(li)}}
let current=null;
async function openDoc(path){history.replaceState(null,'','?path='+encodeURIComponent(path));document.querySelectorAll('.row.active').forEach(x=>x.classList.remove('active'));let r=await fetch('/api/document?path='+encodeURIComponent(path));if(!r.ok){article.innerHTML='<div class="empty"><strong>문서를 열 수 없습니다</strong>파일을 찾지 못했습니다.</div>';return}current=await r.json();show(false)}
function show(raw){const d=current;article.innerHTML='<h1>'+esc(d.title)+'</h1><div class="meta">'+esc(d.path)+` · <a href="#" id="rawBtn">${raw?'렌더링 보기':'Markdown 원문 보기'}</a></div>`+(raw?'<pre class="raw"><code>'+esc(d.content)+'</code></pre>':md(d.content,d.path));
document.querySelector('#rawBtn').onclick=e=>{e.preventDefault();show(!raw)};document.querySelector('.main').scrollTop=0;if(!raw)diagrams()}
article.addEventListener('click',e=>{const a=e.target.closest('a');if(!a)return;const h=a.getAttribute('href')||'';if(h.startsWith('?path=')){e.preventDefault();openDoc(new URLSearchParams(h.slice(1)).get('path'))}else if(h.startsWith('#h-')){e.preventDefault();const t=document.getElementById(h.slice(1));if(t)t.scrollIntoView({behavior:'smooth'})}});
fetch('/api/tree').then(r=>r.json()).then(data=>{render(data,treeEl);let p=new URLSearchParams(location.search).get('path');if(p)openDoc(p)});
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                data = HTML_PAGE.encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if parsed.path == "/api/tree":
                self.send_json(build_tree()); return
            if parsed.path == "/api/document":
                value = parse_qs(parsed.query).get("path", [""])[0]
                path = safe_path(value)
                if path.suffix.lower() != ".md":
                    path = safe_path(value.rstrip("/") + ".md")
                if path.suffix.lower() != ".md" or not path.is_file(): raise FileNotFoundError
                self.send_json({"title": path.stem.replace("-", " "), "path": value, "content": path.read_text(encoding="utf-8-sig")}); return
            self.send_error(404)
        except (ValueError, FileNotFoundError, UnicodeError):
            self.send_error(404)

    def log_message(self, *_):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if not ROOT.is_dir(): raise SystemExit(f"Wiki root not found: {ROOT}")
    # Windows 의 SO_REUSEADDR 로 같은 포트에 서버가 중복으로 뜨지 않도록 재사용을 끈다.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(f"포트 {port} 가 이미 사용 중입니다. 이미 떠 있는 뷰어를 끄고(Ctrl+C) 다시 실행하세요.")
    print(f"Wiki viewer: http://127.0.0.1:{port}")
    print(f"Read-only root: {ROOT}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
