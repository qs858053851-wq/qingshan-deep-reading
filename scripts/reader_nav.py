#!/usr/bin/env python3
"""给 readings/*.html 注入「回书房 / 记为已读」入口，修补目录链接的默认蓝色与错误的 <title>，并生成 sitemap.xml。

幂等：所有注入内容都包在 qs-roomnav 标记里，重跑时先删后加，结果不变。
必须在 build_archive.py / build_fonts.py 之后运行（重新生成的读本会丢掉注入块）。
用法：
  python3 scripts/reader_nav.py          # 注入 / 更新
  python3 scripts/reader_nav.py --check  # 只检查，不写盘；有缺失时退出码 1
"""
import html, json, re, sys
from xml.sax.saxutils import escape as xesc
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent.parent
SITE = 'https://qs858053851-wq.github.io/qingshan-deep-reading/'
MARK = re.compile(r'\n?<!-- qs-roomnav:(\w+):start -->.*?<!-- qs-roomnav:\1:end -->', re.S)

STYLE = """<!-- qs-roomnav:style:start -->
<style data-qs="roomnav">
  .qs-roomnav { max-width: 880px; margin: 0 auto; display: flex; align-items: center;
    justify-content: space-between; gap: 16px; flex-wrap: wrap;
    font: 400 13px/1.6 "KingHwaOldSong", "Songti SC", "STSong", serif; letter-spacing: 1px;
    color: #6b6a64; }
  .qs-roomnav a { color: #1B365D; text-decoration: none; border-bottom: 1px solid transparent;
    transition: border-color .15s, color .15s; }
  .qs-roomnav a:hover { border-bottom-color: #1B365D; }
  .qs-roomnav-top { padding: 0 0 18px; margin-bottom: 26px; border-bottom: 1px solid #e8e6dc; }
  .qs-roomnav-end { padding: 26px 0 0; margin-top: 40px; border-top: 1px solid #e8e6dc; }
  .qs-roomnav-end .qs-done { color: #a63b2c; border: 1px solid #a63b2c; border-radius: 4px;
    padding: 6px 16px; }
  .qs-roomnav-end .qs-done:hover { background: #a63b2c; color: #f5f4ed; }
  /* 卡片式模板（.container 带 60px 70px 内边距）：导航与卡片内正文对齐 */
  .qs-roomnav.qs-card { max-width: 860px; padding-left: 71px; padding-right: 71px; }
  .qs-roomnav-top.qs-card { border-bottom: 0; margin-bottom: 14px; }
  .qs-roomnav-end.qs-card { border-top: 0; margin-top: 18px; }
  @media screen and (max-width: 640px) {
    .qs-roomnav.qs-card { padding-left: 20px; padding-right: 20px; }
  }
  @media print { .qs-roomnav { display: none !important; } }
</style>
<!-- qs-roomnav:style:end -->"""

TOC_FIX = """<!-- qs-roomnav:toc:start -->
<style data-qs="toc">
  a.toc-name { color: var(--brand, #1B365D); text-decoration: none; }
  a.toc-name:hover { text-decoration: underline; }
</style>
<!-- qs-roomnav:toc:end -->"""


CARD = re.compile(r'\.container\s*\{[^}]*padding:\s*60px\s+70px')


def blocks(title, card=False):
    q = quote(title, safe='')
    home = '../index.html#catalogue'
    done = f'../index.html?mark_read={q}#catalogue'
    t = html.escape(title)
    k = ' qs-card' if card else ''
    top = f"""<!-- qs-roomnav:top:start -->
<nav class="qs-roomnav qs-roomnav-top{k}" aria-label="书房导航">
  <a href="{home}">← 青山的十分钟书房</a>
  <a href="{done}" title="把《{t}》记为已读并回到书房">记为已读</a>
</nav>
<!-- qs-roomnav:top:end -->"""
    end = f"""<!-- qs-roomnav:end:start -->
<nav class="qs-roomnav qs-roomnav-end{k}" aria-label="读完以后">
  <span>读完《{t}》了？</span>
  <span><a href="{home}">回书房</a>&emsp;<a class="qs-done" href="{done}">记为已读，回书房</a></span>
</nav>
<!-- qs-roomnav:end:end -->"""
    return top, end


def needs_toc_fix(s):
    if '<a class="toc-name"' not in s:
        return False
    css = ''.join(re.findall(r'<style(?![^>]*data-qs)[^>]*>(.*?)</style>', s, re.S))
    return not re.search(r'\.toc-name[^{]*\{[^}]*color', css)


NORM = re.compile(r'[\s《》·:：_\-（）()，,、"“”]|修订版')


def fix_title(s, title):
    """<title> 与书目不符、而封面书名与书目相符时，按封面书名改正 <title>。"""
    m = re.search(r'<title>(.*?)</title>', s, re.S)
    c = re.search(r'class="cover-title"[^>]*>(.*?)<', s, re.S)
    if not m or not c:
        return s
    key = NORM.sub('', title)[:4]
    cover = html.unescape(c.group(1)).strip()
    if key in NORM.sub('', html.unescape(m.group(1))) or key not in NORM.sub('', cover):
        return s
    name = cover.strip('《》')
    return s[:m.start()] + f'<title>{html.escape(name, quote=False)} · 深度解读</title>' + s[m.end():]


def render(s, title):
    s = MARK.sub('', s)
    s = fix_title(s, title)
    css = ''.join(re.findall(r'<style(?![^>]*data-qs)[^>]*>(.*?)</style>', s, re.S))
    top, end = blocks(title, card=bool(CARD.search(css)))
    # 每个注入块都以一个换行开头，MARK 删除时连同这个换行一起删，保证可逆、幂等
    head_add = '\n' + STYLE + ('\n' + TOC_FIX if needs_toc_fix(s) else '') + '\n'
    if '</head>' in s:
        s = s.replace('</head>', head_add.rstrip('\n') + '</head>', 1)
    else:  # 少数旧稿缺 </head>，浏览器会在 <body> 前自动闭合
        s = re.sub(r'(<body[^>]*>)', lambda m: head_add.rstrip('\n') + m.group(1), s, count=1)
    s = re.sub(r'(<body[^>]*>)', lambda m: m.group(1) + '\n' + top, s, count=1)
    i = s.rfind('</body>')
    s = s[:i] + '\n' + end + s[i:]
    return s


def sitemap(entries):
    """首页 + 全部网页版读本；lastmod 取条目日期。"""
    rows = [f'  <url><loc>{SITE}</loc><lastmod>{max(e["date"] for e in entries)}</lastmod></url>']
    for e in sorted((e for e in entries if e.get('html')), key=lambda e: (e['date'], e['html']), reverse=True):
        rows.append(f'  <url><loc>{xesc(SITE + e["html"])}</loc><lastmod>{e["date"]}</lastmod></url>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + '\n'.join(rows) + '\n</urlset>\n')


def main():
    check = '--check' in sys.argv
    entries = json.loads((ROOT / 'readings.json').read_text(encoding='utf-8'))['entries']
    titles = {unquote(e['html']): e['title'] for e in entries if e.get('html')}
    changed, missing, bad = 0, [], []
    for path, title in sorted(titles.items()):
        f = ROOT / path
        if not f.exists():
            missing.append(path); continue
        s = f.read_text(encoding='utf-8')
        if '<head' not in s or '<body' not in s or '</body>' not in s:
            bad.append(path); continue
        new = render(s, title)
        if new != s:
            changed += 1
            if not check:
                f.write_text(new, encoding='utf-8')
    sm, smf = sitemap(entries), ROOT / 'sitemap.xml'
    sm_stale = not smf.exists() or smf.read_text(encoding='utf-8') != sm
    if sm_stale and not check:
        smf.write_text(sm, encoding='utf-8')
    verb = '需要更新' if check else '已更新'
    print(f'读本 {len(titles)} 篇，{verb} {changed} 篇，文件缺失 {len(missing)}，结构异常 {len(bad)}')
    for p in missing + bad:
        print('  !', p)
    print(f'sitemap.xml：{"需要更新" if check and sm_stale else ("已更新" if sm_stale else "无变化")}')
    if check and (changed or missing or bad or sm_stale):
        sys.exit(1)


if __name__ == '__main__':
    main()
