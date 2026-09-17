#!/usr/bin/env python3
"""生成并接入书库用的 WOFF2 子集字体。

公开书库的 HTML 里，@font-face 指向 ../fonts/*.woff2。这两个文件不是原字体，
而是**只保留书库实际用到的字形**的子集：从 28.6 MB 压到 7.5 MB。

为什么要子集化
--------------
全书库 784 篇加起来只用得到约 5700 个字符，而京华老宋体本身有三万九千多个字形。
剩下的三万个字形读者一辈子用不到，却要手机全量下载。子集化把这一块省掉。

什么时候要重跑
--------------
新增书之后跑一次。否则新书里的生僻字不在子集里，会回落到系统宋体，
一篇文章里蹦出一个字形不同的汉字，看起来像 bug。

    python3 scripts/build_fonts.py            # 重建子集 + 校正 HTML 引用
    python3 scripts/build_fonts.py --check    # 只体检，不写盘

流水线里的位置
--------------
    build_archive.py  →  把本地原稿脱敏成公开稿（这一步会**删掉**本地字体路径）
    build_fonts.py    →  重建子集，并把 ../fonts/ 引用补回公开稿（本脚本）

顺序不能反，也不能只跑前一个：build_archive.py 会移除 @font-face 里的 url()，
跑完不接本脚本，公开稿就会失去网页字体。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
READINGS_DIR = REPO_ROOT / "readings"
FONTS_DIR = REPO_ROOT / "fonts"
INDEX = REPO_ROOT / "index.html"
CHARSET_CACHE = REPO_ROOT / ".font-charset.txt"

# family 关键字 -> 输出文件名。顺序敏感：KingHwa 必须排在 KingHwa Bold 之前。
FONT_FACES = [
    ("KingHwa", "KingHwaOldSong.woff2"),
    ("Huiwen", "Huiwen-mincho.woff2"),
]

# 源字体。公版/免费字面放在用户字体目录；方正博雅方刊宋是商业字体，
# **故意不在这里**：它在 --title-font 链里排第三，前面两个一嵌进去它永远轮不到，
# 嵌进来只是白占体积并带来授权风险。
SOURCE_FONTS = {
    "KingHwaOldSong.woff2": Path.home() / "Library/Fonts/京华老宋体v3.0.ttf",
    "Huiwen-mincho.woff2": Path.home() / "Library/Fonts/Huiwenmincho-improved.otf",
}

# 子集之外的兜底字符：ASCII、常用中文标点、全角符号。
SAFETY_CHARS = (
    "".join(chr(c) for c in range(0x20, 0x7F))
    + "　·—…“”‘’《》〈〉「」『』【】〔〕（）［］｛｝：；、，。！？～＋－×÷≈≠≤≥←→↑↓√∑∏∫°′″㎡‖｜"
)

FONT_FACE_RE = re.compile(r"@font-face\s*\{(.*?)\n(\s*)\}", re.S)
SRC_RE = re.compile(r"src:\s*.*?;", re.S)
FAMILY_RE = re.compile(r'font-family:\s*"([^"]+)"')
LOCAL_FONT_URL_RE = re.compile(
    r",\s*url\([\"']?(?:/Users/|/home/|file://|[A-Za-z]:\\\\)[^)\"']+[\"']?\)\s*(?:format\([^)]*\))?",
    re.I,
)


def target_file_for(family: str) -> str | None:
    for key, name in FONT_FACES:
        if key in family:
            return name
    return None


def collect_charset() -> str:
    """汇集公开稿 + 首页里出现过的所有字符。"""
    chars: set[str] = set()
    pages = sorted(READINGS_DIR.glob("*.html"))
    if INDEX.exists():
        pages.append(INDEX)
    for path in pages:
        text = path.read_text(encoding="utf-8", errors="replace")
        style = " ".join(re.findall(r"<style>(.*?)</style>", text, re.S))
        for quote in ('"', "'"):
            for value in re.findall(r"content:\s*%s([^%s]*)%s" % (quote, quote, quote), style):
                chars |= set(value)
        body = re.sub(r"<(style|script)\b.*?</\1>", "", text, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        for entity, plain in (
            ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
            ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
        ):
            body = body.replace(entity, plain)
        chars |= set(body)
    chars |= set(SAFETY_CHARS)
    chars -= {"\r", "\n", "\t"}
    return "".join(sorted(chars))


def ensure_references(text: str) -> tuple[str, int]:
    """把 ../fonts/*.woff2 与 font-display: swap 补回 @font-face。

    幂等：已经正确的块原样返回。build_archive.py 删掉的 url() 在这里被加回来。
    """
    changed = 0

    def fix_block(match: re.Match[str]) -> str:
        nonlocal changed
        block, closing_indent = match.group(1), match.group(2)
        family_match = FAMILY_RE.search(block)
        if not family_match:
            return match.group(0)
        target = target_file_for(family_match.group(1))
        block = LOCAL_FONT_URL_RE.sub("", block)          # 清掉残留的本机绝对路径
        src_match = SRC_RE.search(block)
        if src_match and target:
            local_part = src_match.group(0).split("url(")[0].rstrip().rstrip(",;")
            replacement = '%s,\n         url("../fonts/%s") format("woff2");' % (local_part, target)
            block = block[: src_match.start()] + replacement + block[src_match.end():]
        if "font-display" not in block:
            block = re.sub(r"(font-weight:\s*[^;]+;)", r"\1\n    font-display: swap;", block, count=1)
        new = "@font-face {%s\n%s}" % (block, closing_indent)
        if new != match.group(0):
            changed += 1
        return new

    return FONT_FACE_RE.sub(fix_block, text), changed


def run_subset(source: Path, target: Path, charset_file: Path) -> None:
    from fontTools import subset  # 延迟导入，--check 时不需要

    args = [
        str(source),
        "--text-file=%s" % charset_file,
        "--flavor=woff2",
        "--output-file=%s" % target,
        "--layout-features=*",
        "--no-hinting",
        "--desubroutinize",
        "--drop-tables+=DSIG",
    ]
    subset.main(args)


def verify_coverage(charset: str) -> bool:
    from fontTools.ttLib import TTFont

    ok = True
    wanted = {ord(c) for c in charset}
    for name, source in SOURCE_FONTS.items():
        shipped = FONTS_DIR / name
        if not shipped.exists():
            print("  ✗ 缺文件 fonts/%s" % name)
            ok = False
            continue
        src_map = set(TTFont(source).getBestCmap())
        out_map = set(TTFont(shipped).getBestCmap())
        lost = (wanted & src_map) - out_map
        absent = wanted - src_map
        print("  %-24s 源 %6d 字形 → 子集 %5d；丢失 %d；源字体本身没有 %d"
              % (name, len(src_map), len(out_map), len(lost), len(absent)))
        if lost:
            print("    ✗ 子集漏字：" + "".join(chr(c) for c in sorted(lost)[:20]))
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="生成书库用的 WOFF2 子集字体")
    parser.add_argument("--check", action="store_true", help="只体检，不写盘")
    args = parser.parse_args()

    if not READINGS_DIR.is_dir():
        print("找不到 %s" % READINGS_DIR, file=sys.stderr)
        return 1

    charset = collect_charset()
    print("字表：%d 个字符（其中汉字 %d）"
          % (len(charset), sum(1 for c in charset if "\u3400" <= c <= "\u9fff")))

    if args.check:
        print("覆盖率体检：")
        return 0 if verify_coverage(charset) else 1

    CHARSET_CACHE.write_text(charset, encoding="utf-8")
    FONTS_DIR.mkdir(exist_ok=True)

    print("生成子集：")
    for name, source in SOURCE_FONTS.items():
        if not source.exists():
            print("  ✗ 找不到源字体 %s" % source, file=sys.stderr)
            return 1
        target = FONTS_DIR / name
        run_subset(source, target, CHARSET_CACHE)
        print("  %-24s %5.1f MB" % (name, target.stat().st_size / 1048576))

    total = sum((FONTS_DIR / n).stat().st_size for n in SOURCE_FONTS)
    print("  合计 %.1f MB" % (total / 1048576))

    print("\n校正 HTML 的字体引用：")
    touched = 0
    blocks = 0
    for page in sorted(READINGS_DIR.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        new, changed = ensure_references(text)
        blocks += changed
        if new != text:
            page.write_text(new, encoding="utf-8")
            touched += 1
    print("  扫描 %d 篇，改写 %d 篇，修正 %d 个 @font-face 块"
          % (len(list(READINGS_DIR.glob("*.html"))), touched, blocks))

    print("\n覆盖率复核：")
    if not verify_coverage(charset):
        return 1
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
