#!/usr/bin/env python3
"""Build and audit the public reading archive from the private source folder."""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

SOURCE_DEFAULT = Path.home() / "Documents/青山/02-阅读"
REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_ENV = "QINGSHAN_PUBLIC_RULES"
PRIVATE_RULES_DEFAULT = REPO_ROOT.parent / ".qingshan-public-rules.json"
READINGS_DIR = REPO_ROOT / "readings"
MARKDOWN_DIR = REPO_ROOT / "markdown"
INDEX_DATA = REPO_ROOT / "readings.json"
SANITIZATION_REPORT = REPO_ROOT / "SANITIZATION.md"

DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
# 新解书流水线的文件名形如 20260822T001901--解书-法言，日期藏在紧凑时间戳里。
STAMP_DATE_RE = re.compile(r"\b(20\d{2})(\d{2})(\d{2})T\d{6}\b")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")

# Specific names and one-to-one identity phrases are loaded from a private rules
# file kept outside this repository. Public builds fail when that file is absent.

# Public builds require an external rule file. It carries both targeted identity
# substitutions and local-environment checks without recording private values here.
BLOCKING_PATTERNS = {
    "credential-like string": re.compile(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|authorization:|"
        r"bearer\s+[A-Za-z0-9._-]{12,}|sk-[A-Za-z0-9_-]{12,}|gh[opsu]_[A-Za-z0-9]{20,})"
    ),
}

# 分类规则在 categories.py，按书名关键词把书房分得更细。新增书目时
# 优先给对应类别补关键词，而不是扩大“其他阅读”。
try:
    from categories import CATEGORY_RULES  # type: ignore
except ModuleNotFoundError:
    CATEGORY_RULES = [("其他阅读", ())]


@dataclass
class Entry:
    stem: str
    title: str
    date: str
    category: str
    html_name: str | None
    md_name: str | None
    html_bytes: int
    md_bytes: int

    def as_dict(self) -> dict[str, object]:
        html_href = f"readings/{quote(self.html_name)}" if self.html_name else None
        md_href = f"markdown/{quote(self.md_name)}" if self.md_name else None
        return {
            "title": self.title,
            "date": self.date,
            "category": self.category,
            "html": html_href,
            "markdown": md_href,
            "htmlBytes": self.html_bytes,
            "markdownBytes": self.md_bytes,
        }


def load_private_rules(path: Path | None = None) -> tuple[list[tuple[str, str]], list[tuple[re.Pattern[str], str]], dict[str, re.Pattern[str]]]:
    """Load identity rules kept outside the repository.

    The public repository contains the safety mechanism, but not the private
    names it is designed to remove.
    """
    configured = os.environ.get(RULES_ENV)
    rules_path = path or (Path(configured).expanduser() if configured else PRIVATE_RULES_DEFAULT)
    if not rules_path.is_file():
        print(
            f"Private sanitization rules are required: {rules_path}\n"
            f"Set {RULES_ENV} to an alternate rules file.",
            file=sys.stderr,
        )
        raise FileNotFoundError(rules_path)

    payload = json.loads(rules_path.read_text(encoding="utf-8"))

    def flags(value: str) -> re.RegexFlag:
        result = re.RegexFlag(0)
        if "I" in value:
            result |= re.I
        if "M" in value:
            result |= re.M
        if "S" in value:
            result |= re.S
        return result

    literal = [(str(old), str(new)) for old, new in payload.get("literal_replacements", [])]
    regex = [
        (re.compile(str(item["pattern"]), flags(str(item.get("flags", "")))), str(item["replacement"]))
        for item in payload.get("regex_replacements", [])
    ]
    blocking = {
        str(item["label"]): re.compile(str(item["pattern"]), flags(str(item.get("flags", ""))))
        for item in payload.get("blocking_patterns", [])
    }
    if not literal or not blocking:
        raise ValueError(f"Sanitization rules are incomplete: {rules_path}")
    return literal, regex, blocking


def private_rules() -> tuple[list[tuple[str, str]], list[tuple[re.Pattern[str], str]], dict[str, re.Pattern[str]]]:
    if not hasattr(private_rules, "cache"):
        private_rules.cache = load_private_rules()  # type: ignore[attr-defined]
    return private_rules.cache  # type: ignore[attr-defined]


def normalize_text(text: str) -> tuple[str, Counter[str]]:
    changes: Counter[str] = Counter()
    text = unicodedata.normalize("NFC", text)
    literal_replacements, regex_replacements, _ = private_rules()
    for old, new in literal_replacements:
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            changes["private text edit"] += count
    for pattern, replacement in regex_replacements:
        text, count = pattern.subn(replacement, text)
        if count:
            changes["private contextual edit"] += count
    return text, changes


def clean_title(raw: str, fallback: str) -> str:
    raw = html_lib.unescape(TAG_RE.sub("", raw)).strip()
    raw = re.sub(r"\s*[|｜·]\s*(?:青山)?(?:深度阅读|九层解书|解读).*$", "", raw)
    raw = re.sub(r"\s*[—-]{1,2}\s*(?:九层解书)?(?:修订版|解读).*$", "", raw)
    return raw.strip() or fallback


def extract_title(path: Path, text: str, fallback: str) -> str:
    match = H1_RE.search(text) or TITLE_RE.search(text)
    return clean_title(match.group(1), fallback) if match else fallback


def display_title_from_stem(stem: str) -> str:
    title = DATE_RE.sub("", stem)
    title = STAMP_DATE_RE.sub("", title)
    title = re.sub(r"^-*(?:解书)?-*", "", title)
    title = re.sub(r"_?(?:标准)?修订版_?", "", title)
    title = re.sub(r"_+$", "", title)
    return title.replace("_", " ").strip() or stem


def infer_category(title: str) -> str:
    for category, keywords in CATEGORY_RULES:
        if any(keyword in title for keyword in keywords):
            return category
    return "其他阅读"


def source_files(source: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    html_files = {p.stem: p for p in source.glob("*.html") if p.is_file()}
    md_root = source / "md"
    md_files = {p.stem: p for p in md_root.glob("*.md") if p.is_file()}
    return html_files, md_files


def reset_output() -> None:
    for directory in (READINGS_DIR, MARKDOWN_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        for child in directory.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)


def remove_active_content(text: str) -> tuple[str, int]:
    """Remove executable blocks while keeping the reading text unchanged."""
    text, script_count = re.subn(r"<script\b[^>]*>.*?</script\s*>", "", text, flags=re.I | re.S)
    text, iframe_count = re.subn(r"<iframe\b[^>]*>.*?</iframe\s*>", "", text, flags=re.I | re.S)
    return text, script_count + iframe_count


def remove_remote_font_loads(text: str) -> tuple[str, int]:
    """Keep the archive readable without third-party font requests."""
    patterns = [
        r"@import\s+url\([^)]*fonts\.googleapis\.com[^)]*\)\s*;?",
        r"<link\b[^>]*href=[\"'][^\"']*fonts\.(?:googleapis|gstatic)\.com[^\"']*[\"'][^>]*>\s*",
        r"<link\b[^>]*href=[\"']https://fonts\.googleapis\.com[\"'][^>]*>\s*",
    ]
    removed = 0
    for pattern in patterns:
        text, count = re.subn(pattern, "", text, flags=re.I)
        removed += count
    return text, removed


def remove_local_font_paths(text: str) -> tuple[str, int]:
    """Remove local filesystem font paths from CSS @font-face rules."""
    pattern = re.compile(
        r",\s*url\([\"']?(?:/Users/|/home/|file://|[A-Za-z]:\\\\)[^)\"']+[\"']?\)\s*(?:format\([^)]*\))?",
        re.I,
    )
    return pattern.subn("", text)


def rewrite_markdown_html_link(text: str, html_name: str | None) -> tuple[str, int]:
    if not html_name:
        pattern = re.compile(r"\[[^\]]*(?:HTML|网页|可视化)[^\]]*\]\(\.\./[^)]+\.html\)", re.I)
        return pattern.subn("_本条目暂只有 Markdown 版本_", text)
    target = f"../readings/{quote(html_name)}"
    patterns = [
        re.compile(r"(\[[^\]]*(?:HTML|网页|可视化)[^\]]*\]\()\.\./[^)]+\.html(\))", re.I),
        re.compile(r"(href=[\"'])\.\./[^\"']+\.html([\"'])", re.I),
    ]
    rewritten = 0
    for pattern in patterns:
        text, count = pattern.subn(lambda match: f"{match.group(1)}{target}{match.group(2)}", text)
        rewritten += count
    return text, rewritten


def remove_internal_workflow_notes(text: str) -> tuple[str, int]:
    """Drop drafting instructions that are not part of the public essay."""
    patterns = [
        r"<p>\s*本层预算[^<]*?(?:计划|准备)[^<]*?</p>\s*",
        r"^\s*本层预算[^\n]*(?:计划|准备)[^\n]*\n?",
        r"<p>\s*\*\*本层预算\*\*\s*[：:]\s*[^<]*?(?:<br\s*/?>)?\s*(?=\*\*核心概念\*\*)",
        r"^\s*\*\*本层预算\*\*\s*[：:]\s*[^\n]*\n?",
    ]
    removed = 0
    for pattern in patterns:
        text, count = re.subn(pattern, "", text, flags=re.I | re.M)
        removed += count

    # Some older drafts left word-count budgets inside section headings.
    text, count = re.subn(r"\s*[（(]预算\s*[0-9０-９]+\s*字[）)]", "", text)
    removed += count
    return text, removed


def copy_clean(path: Path, destination: Path, html_name: str | None = None) -> tuple[str, Counter[str]]:
    original = path.read_text(encoding="utf-8", errors="strict")
    cleaned, changes = normalize_text(original)
    cleaned, removed_notes = remove_internal_workflow_notes(cleaned)
    if removed_notes:
        changes["removed internal drafting notes"] += removed_notes
    if path.suffix.lower() == ".html":
        cleaned, removed = remove_active_content(cleaned)
        if removed:
            changes["removed active HTML blocks"] += removed
        cleaned, removed_fonts = remove_remote_font_loads(cleaned)
        if removed_fonts:
            changes["removed remote font requests"] += removed_fonts
        cleaned, removed_local_fonts = remove_local_font_paths(cleaned)
        if removed_local_fonts:
            changes["removed local font paths"] += removed_local_fonts
    elif path.suffix.lower() == ".md":
        cleaned, rewritten_links = rewrite_markdown_html_link(cleaned, html_name)
        if rewritten_links:
            changes["rewrote Markdown HTML links"] += rewritten_links
    destination.write_text(cleaned, encoding="utf-8", newline="\n")
    return cleaned, changes


def scan_files(paths: list[Path]) -> list[tuple[str, str, int, str]]:
    findings: list[tuple[str, str, int, str]] = []
    _, _, private_blocking = private_rules()
    patterns = {**BLOCKING_PATTERNS, **private_blocking}
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in patterns.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                excerpt = " ".join(text[max(0, match.start() - 30) : match.end() + 30].split())
                findings.append((str(path.relative_to(REPO_ROOT)), label, line, excerpt))
    return findings


def validate_html(path: Path, text: str) -> list[str]:
    errors = []
    lower = text.lower()
    if "<!doctype html" not in lower:
        errors.append(f"{path.name}: missing doctype")
    if "<html" not in lower or "</html>" not in lower:
        errors.append(f"{path.name}: incomplete html element")
    if "<title" not in lower:
        errors.append(f"{path.name}: missing title")
    if "<script" in lower or "<iframe" in lower:
        errors.append(f"{path.name}: active embedded content is not allowed")
    return errors


def build(source: Path) -> int:
    if not source.is_dir():
        print(f"Source directory does not exist: {source}", file=sys.stderr)
        return 2

    html_sources, md_sources = source_files(source)
    reset_output()
    entries: list[Entry] = []
    changes: Counter[str] = Counter()
    html_errors: list[str] = []

    for stem in sorted(set(html_sources) | set(md_sources)):
        html_name = None
        md_name = None
        html_text = ""
        title = display_title_from_stem(stem)
        html_bytes = 0
        md_bytes = 0

        if stem in html_sources:
            source_path = html_sources[stem]
            html_name = source_path.name
            destination = READINGS_DIR / html_name
            html_text, file_changes = copy_clean(source_path, destination)
            changes.update(file_changes)
            title = extract_title(destination, html_text, title)
            html_bytes = destination.stat().st_size
            html_errors.extend(validate_html(destination, html_text))

        if stem in md_sources:
            source_path = md_sources[stem]
            md_name = source_path.name
            destination = MARKDOWN_DIR / md_name
            matching_html_name = html_sources[stem].name if stem in html_sources else None
            md_text, file_changes = copy_clean(
                source_path, destination, html_name=matching_html_name
            )
            changes.update(file_changes)
            md_bytes = destination.stat().st_size
            if not html_text:
                heading = re.search(r"^#\s+(.+)$", md_text, re.M)
                if heading:
                    title = heading.group(1).strip()

        date_match = DATE_RE.search(stem)
        if date_match:
            published = date_match.group(1)
        else:
            stamp = STAMP_DATE_RE.search(stem)
            published = "-".join(stamp.groups()) if stamp else "日期未标"
        entries.append(
            Entry(
                stem=stem,
                title=title,
                date=published,
                category=infer_category(title),
                html_name=html_name,
                md_name=md_name,
                html_bytes=html_bytes,
                md_bytes=md_bytes,
            )
        )

    published_files = list(READINGS_DIR.glob("*.html")) + list(MARKDOWN_DIR.glob("*.md"))
    findings = scan_files(published_files)
    if findings or html_errors:
        for error in html_errors:
            print(f"HTML ERROR: {error}", file=sys.stderr)
        for path, label, line, excerpt in findings[:200]:
            print(f"PRIVACY ERROR: {path}:{line} [{label}] {excerpt}", file=sys.stderr)
        print(
            f"Build stopped: {len(html_errors)} HTML errors, {len(findings)} privacy findings.",
            file=sys.stderr,
        )
        return 1

    entries.sort(key=lambda item: (item.date, item.title), reverse=True)
    payload = {
        "updated": str(date.today()),
        "count": len(entries),
        "htmlCount": sum(bool(entry.html_name) for entry in entries),
        "markdownCount": sum(bool(entry.md_name) for entry in entries),
        "categories": dict(sorted(Counter(entry.category for entry in entries).items())),
        "entries": [entry.as_dict() for entry in entries],
    }
    INDEX_DATA.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    html_only = sorted(set(html_sources) - set(md_sources))
    md_only = sorted(set(md_sources) - set(html_sources))
    digest = hashlib.sha256()
    for path in sorted(published_files):
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode())
        digest.update(path.read_bytes())

    private_edit_keys = {"private text edit", "private contextual edit"}
    private_edit_total = sum(changes[key] for key in private_edit_keys)
    publishing_cleanup_total = sum(
        count for label, count in changes.items() if label not in private_edit_keys
    )
    report = f"""# 公开版清理说明

这份仓库由私人阅读档案生成。原始文件保留在本地，公开版只复制正式的 HTML 与 Markdown，并在发布前做身份与安全清理。

## 本次结果

- 阅读条目：{len(entries)}
- HTML：{sum(bool(entry.html_name) for entry in entries)}
- Markdown：{sum(bool(entry.md_name) for entry in entries)}
- 私人文本与生产元话语转换：{private_edit_total} 处
- 其他发布清理（远程字体、内部草稿提示、链接等）：{publishing_cleanup_total} 处
- 仅有 HTML 的条目：{len(html_only)}
- 仅有 Markdown 的条目：{len(md_only)}
- 安全扫描：通过
- 内容指纹：`sha256:{digest.hexdigest()}`

## 清理原则

1. 不修改私人原稿，只生成独立公开镜像。
2. 保留观点、案例和职业经验，去掉能反推出私人单位、具体项目和一对一称呼的身份指纹。
3. 不把所有“先生”一律替换；书中人物、历史称谓和概念名称按原意保留。
4. 阻断绝对路径、联系方式、凭据形态、私人项目名和临时文件。
5. 许可只覆盖发布者有权授权的原创解读；原书引文及第三方材料仍归各自权利人。

## 仍需读者理解的边界

清理可以降低身份暴露，却不能消除题材本身的公共可见性。这里保留了跨度很大的阅读选择，也保留了文章形成时的判断、犹疑和修订痕迹。它们是阅读记录，不是权威结论。
"""
    SANITIZATION_REPORT.write_text(report, encoding="utf-8", newline="\n")

    print(
        f"Built {len(entries)} entries: {payload['htmlCount']} HTML, "
        f"{payload['markdownCount']} Markdown; {private_edit_total} private text edits, "
        f"{publishing_cleanup_total} publishing cleanups."
    )
    return 0


def check() -> int:
    paths = list(READINGS_DIR.glob("*.html")) + list(MARKDOWN_DIR.glob("*.md"))
    if not paths or not INDEX_DATA.exists():
        print("Public archive has not been built.", file=sys.stderr)
        return 2
    findings = scan_files(paths)
    html_errors: list[str] = []
    for path in READINGS_DIR.glob("*.html"):
        html_errors.extend(validate_html(path, path.read_text(encoding="utf-8", errors="replace")))
    data = json.loads(INDEX_DATA.read_text(encoding="utf-8"))
    indexed = len(data.get("entries", []))
    actual_stems = {p.stem for p in READINGS_DIR.glob("*.html")} | {
        p.stem for p in MARKDOWN_DIR.glob("*.md")
    }
    if indexed != len(actual_stems):
        html_errors.append(f"index has {indexed} entries but archive has {len(actual_stems)}")
    if findings or html_errors:
        for error in html_errors:
            print(f"CHECK ERROR: {error}", file=sys.stderr)
        for path, label, line, excerpt in findings[:200]:
            print(f"CHECK ERROR: {path}:{line} [{label}] {excerpt}", file=sys.stderr)
        return 1
    print(f"Check passed: {indexed} entries and {len(paths)} published files.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_DEFAULT)
    parser.add_argument("--check", action="store_true", help="audit the current public archive")
    args = parser.parse_args()
    return check() if args.check else build(args.source.expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
