"""项目代码索引：把堆栈、日志里的文件名/函数名/关键词映射回源码。"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import CodeRef, ErrorGroup, Frame
from .utils import normalize_slashes, read_text_safe, short_type_name

CODE_EXTS = {
    ".py",
    ".java",
    ".kt",
    ".kts",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".cs",
    ".rb",
    ".php",
    ".rs",
    ".scala",
    ".swift",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".m",
    ".vue",
    ".svelte",
}

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
    ".scala": "scala",
    ".swift": "swift",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".m": "objective-c",
    ".vue": "vue",
    ".svelte": "svelte",
}

DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "bower_components",
    "dist",
    "build",
    "target",
    "bin",
    "obj",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "coverage",
    "htmlcov",
    "logs",
    "log",
}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_FILE_HINT_RE = re.compile(r'File "([^"]+)"(?:,\s*line\s+(\d+))?')
_PATH_TOKEN_RE = re.compile(
    r"(?:(?:[A-Za-z]:)?[\\/])?(?:[\w.@$+-]+[\\/])+[\w.@$+-]+\.[A-Za-z0-9]{1,8}(?::\d+(?::\d+)?)?"
)
_QUOTED_RE = re.compile(r"['\"`]([A-Za-z_][\w./\\:-]{2,80})['\"`]")

_STOPWORDS = {
    "the", "and", "for", "not", "you", "all", "can", "has", "had", "how", "its",
    "may", "new", "now", "one", "our", "out", "see", "two", "use", "via", "who",
    "why", "yes", "yet", "was", "were", "are", "is", "an", "this", "that", "with",
    "from", "have", "will", "your", "when", "then", "than", "there", "their",
    "about", "into", "over", "under", "after", "before", "because", "could",
    "should", "would", "which", "while", "where", "what", "more", "most", "some",
    "such", "only", "also", "been", "being", "error", "errors", "exception",
    "failed", "failure", "false", "true", "none", "null", "test", "line", "file",
    "http", "https", "java", "lang", "com", "org", "net", "www", "api", "log",
    "logs", "message", "msg", "trace", "stack", "info", "warn", "debug", "code",
    "status", "request", "response", "server", "client", "service", "user",
}

_SYMBOL_PATTERNS = [
    re.compile(
        r"^\s*(?:public|private|protected|internal|static|final|abstract|sealed|partial|"
        r"async|export|default|external|inline|virtual|override|synchronized|native|open|"
        r"suspend|\s)*\b(?:def|function|func|fun|fn|sub|method)\s+(?P<name>[A-Za-z_]\w*)",
        re.M,
    ),
    re.compile(
        r"^\s*(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
        r"default|virtual|override|async|sealed|\s)+[\w<>\[\],.?@]+\s+(?P<name>[A-Za-z_]\w*)\s*\(",
        re.M,
    ),
    re.compile(r"\bclass\s+(?P<name>[A-Za-z_]\w*)"),
    re.compile(r"\b(?:interface|struct|enum|trait|record|object)\s+(?P<name>[A-Za-z_]\w*)"),
]


def extract_file_candidates(text: str, limit: int = 12) -> list[tuple[str, int | None]]:
    """从日志文字里抠出「文件路径 + 行号」候选。"""
    found: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    def add(path: str, line: int | None) -> None:
        path = path.strip().strip("\"'`()[]{}<>,;")
        if not path or len(path) > 240:
            return
        key = path.lower()
        if key in seen:
            return
        seen.add(key)
        found.append((path, line))

    for match in _FILE_HINT_RE.finditer(text):
        line = int(match.group(2)) if match.group(2) and match.group(2).isdigit() else None
        add(match.group(1), line)

    for match in _PATH_TOKEN_RE.finditer(text):
        token = match.group(0)
        line: int | None = None
        line_match = re.search(r":(\d+)(?::\d+)?$", token)
        if line_match:
            line = int(line_match.group(1))
            token = token[: line_match.start()]
        extension = os.path.splitext(token)[1].lower()
        if extension in CODE_EXTS:
            add(token, line)
        if len(found) >= limit:
            break

    return found[:limit]


@dataclass
class CodeFile:
    path: str
    abs_path: str
    language: str = ""
    lines: list[str] = field(default_factory=list)
    line_count: int = 0
    symbols: list[tuple[str, int, str]] = field(default_factory=list)
    identifiers: set[str] = field(default_factory=set)
    lowercase: str = ""

    def line(self, number: int) -> str:
        if 1 <= number <= self.line_count:
            return self.lines[number - 1]
        return ""


class ProjectIndex:
    """轻量代码索引。只读、无第三方依赖，适合几千个文件的项目。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        excludes: Iterable[str] | None = None,
        max_file_kb: int = 512,
        max_files: int = 8000,
    ) -> None:
        self.root = str(Path(root).resolve())
        self.excludes = {item.strip().lower() for item in (excludes or DEFAULT_EXCLUDES) if item.strip()}
        self.max_file_bytes = max(16, int(max_file_kb)) * 1024
        self.max_files = max(1, int(max_files))
        self.files: dict[str, CodeFile] = {}
        self.warnings: list[str] = []
        self._lower_index: dict[str, str] = {}
        self._by_basename: dict[str, list[str]] = {}

    # ---------- 构建 ----------
    def build(self) -> "ProjectIndex":
        root = Path(self.root)
        if not root.is_dir():
            self.warnings.append(f"项目目录不存在：{self.root}")
            return self
        loaded = 0
        skipped_big = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.lower() not in self.excludes and name != "__pycache__"
            )
            for filename in sorted(filenames):
                extension = os.path.splitext(filename)[1].lower()
                if extension not in CODE_EXTS:
                    continue
                absolute = Path(dirpath) / filename
                try:
                    size = absolute.stat().st_size
                except OSError:
                    continue
                if size > self.max_file_bytes:
                    skipped_big += 1
                    continue
                if loaded >= self.max_files:
                    truncated = True
                    break
                try:
                    text = read_text_safe(absolute)
                except OSError:
                    continue
                relative = normalize_slashes(os.path.relpath(absolute, root))
                code_file = CodeFile(
                    path=relative,
                    abs_path=str(absolute),
                    language=LANGUAGE_BY_EXT.get(extension, extension.lstrip(".")),
                )
                code_file.lines = text.splitlines()
                code_file.line_count = len(code_file.lines)
                code_file.lowercase = text.lower()
                code_file.identifiers = {word.lower() for word in _WORD_RE.findall(text)}
                code_file.symbols = self._extract_symbols(text, code_file)
                self.files[relative] = code_file
                self._lower_index[relative.lower()] = relative
                self._by_basename.setdefault(filename.lower(), []).append(relative)
                loaded += 1
            if truncated:
                break

        if skipped_big:
            self.warnings.append(f"跳过了 {skipped_big} 个超过 {self.max_file_bytes // 1024}KB 的源码文件")
        if truncated:
            self.warnings.append(f"源码文件超过上限 {self.max_files} 个，索引已截断")
        return self

    def _extract_symbols(self, text: str, code_file: CodeFile) -> list[tuple[str, int, str]]:
        symbols: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        if code_file.language == "python":
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    name: str | None = None
                    kind = ""
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        name, kind = node.name, "function"
                    elif isinstance(node, ast.ClassDef):
                        name, kind = node.name, "class"
                    elif isinstance(node, ast.Assign):
                        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                        for item in names:
                            if (item, node.lineno) not in seen:
                                seen.add((item, node.lineno))
                                symbols.append((item, node.lineno, "variable"))
                        continue
                    if name and (name, node.lineno) not in seen:
                        seen.add((name, node.lineno))
                        symbols.append((name, node.lineno, kind))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            code_file.identifiers.add(alias.name.split(".")[0].lower())
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            code_file.identifiers.add(node.module.split(".")[0].lower())
                        for alias in node.names:
                            if alias.name != "*":
                                code_file.identifiers.add(alias.name.lower())
                return symbols

        for pattern in _SYMBOL_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group("name")
                line_number = text.count("\n", 0, match.start("name")) + 1
                if (name, line_number) in seen:
                    continue
                seen.add((name, line_number))
                symbols.append((name, line_number, "symbol"))
        return symbols

    # ---------- 查询 ----------
    def resolve_path(self, raw: str | None) -> str | None:
        if not raw:
            return None
        path = normalize_slashes(str(raw)).strip().strip("\"'")
        path = re.sub(r"^(?:webpack://|webpack:|file:///?)+", "", path, flags=re.I)
        path = path.split("?", 1)[0]
        root = normalize_slashes(self.root).rstrip("/")
        if re.match(r"^[A-Za-z]:/", path) and path.lower().startswith(root.lower() + "/"):
            path = path[len(root) + 1 :]
        path = path.lstrip("./")
        if not path:
            return None
        if path in self.files:
            return path
        lower = path.lower()
        if lower in self._lower_index:
            return self._lower_index[lower]

        segments = path.split("/")
        for index in range(1, len(segments)):
            suffix = "/".join(segments[index:]).lower()
            if suffix in self._lower_index:
                return self._lower_index[suffix]

        basename = segments[-1].lower()
        candidates = self._by_basename.get(basename) or []
        if candidates:
            return min(candidates, key=lambda item: (item.count("/"), len(item), item))
        return None

    def _find_symbol_line(self, code_file: CodeFile, function: str | None) -> int | None:
        if not function:
            return None
        name = function.strip().split(".")[-1]
        name = re.sub(r"<[^>]*>", "", name)
        if not name:
            return None
        for symbol, line, _kind in code_file.symbols:
            if symbol == name:
                return line
        pattern = re.compile(rf"\b(?:def|function|func|fun|fn|class|interface|struct)\s+{re.escape(name)}\b")
        for index, line in enumerate(code_file.lines, 1):
            if pattern.search(line):
                return index
        call_pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
        for index, line in enumerate(code_file.lines, 1):
            if call_pattern.search(line):
                return index
        return None

    def snippet(self, code_file: CodeFile, line_number: int | None, context: int = 2) -> list[str]:
        if code_file.line_count <= 0:
            return []
        line_number = max(1, min(int(line_number or 1), code_file.line_count))
        start = max(1, line_number - context)
        end = min(code_file.line_count, line_number + context)
        rendered: list[str] = []
        for number in range(start, end + 1):
            marker = ">" if number == line_number else " "
            rendered.append(f"{marker}{number:>5} | {code_file.lines[number - 1].rstrip()[:300]}")
        return rendered

    def search_frame(self, frame: Frame, limit: int = 4) -> list[CodeRef]:
        resolved = self.resolve_path(frame.file)
        if resolved:
            code_file = self.files[resolved]
            line_number = frame.line
            if not line_number and frame.function:
                line_number = self._find_symbol_line(code_file, frame.function)
            line_number = line_number or 1
            refs = [
                CodeRef(
                    path=resolved,
                    line=line_number,
                    kind="stack",
                    symbol=frame.function,
                    snippet=self.snippet(code_file, line_number),
                    score=9.0,
                    note="堆栈位置",
                )
            ]
            definition_line = self._find_symbol_line(code_file, frame.function) if frame.function else None
            if definition_line and definition_line != line_number:
                refs.append(
                    CodeRef(
                        path=resolved,
                        line=definition_line,
                        kind="symbol",
                        symbol=frame.function,
                        snippet=self.snippet(code_file, definition_line),
                        score=7.0,
                        note="函数定义",
                    )
                )
            return refs[:limit]
        if frame.function:
            return self.search_symbol(frame.function, limit=limit)
        return []

    def search_symbol(self, name: str, limit: int = 5) -> list[CodeRef]:
        simple = re.sub(r"<[^>]*>", "", str(name).strip().split(".")[-1])
        if not simple or simple in {"<module>", "<lambda>", "<init>", "main"}:
            return []
        best: list[CodeRef] = []
        for code_file in self.files.values():
            if simple.lower() not in code_file.identifiers:
                continue
            line_number = self._find_symbol_line(code_file, simple)
            if line_number is None:
                continue
            best.append(
                CodeRef(
                    path=code_file.path,
                    line=line_number,
                    kind="symbol",
                    symbol=name,
                    snippet=self.snippet(code_file, line_number),
                    score=6.0,
                    note="按函数名匹配",
                )
            )
        best.sort(key=lambda ref: (-ref.score, ref.path))
        return best[:limit]

    def search_text(
        self,
        queries: Iterable[str] = (),
        terms: Iterable[str] = (),
        limit: int = 5,
    ) -> list[CodeRef]:
        query_list = [item for item in dict.fromkeys(queries) if item and len(item) >= 3][:6]
        term_list = [item for item in dict.fromkeys(terms) if item and len(item) >= 3][:16]
        if not query_list and not term_list:
            return []

        scored: list[tuple[float, CodeFile, int, list[str]]] = []
        for code_file in self.files.values():
            text = code_file.lowercase
            score = 0.0
            hits: list[str] = []
            for query in query_list:
                lowered = query.lower()
                if lowered in text:
                    score += 3.0
                    hits.append(query)
            for term in term_list:
                lowered = term.lower()
                if lowered in code_file.identifiers:
                    score += 2.0
                    hits.append(term)
                elif lowered in text:
                    score += 1.0
                    hits.append(term)
            if score <= 0:
                continue
            first_line = 1
            needles = [hit.lower() for hit in hits]
            for index, line in enumerate(code_file.lines, 1):
                lowered_line = line.lower()
                if any(needle in lowered_line for needle in needles):
                    first_line = index
                    break
            scored.append((score, code_file, first_line, hits))

        scored.sort(key=lambda item: (-item[0], len(item[1].path), item[1].path))
        refs: list[CodeRef] = []
        for score, code_file, line_number, hits in scored[:limit]:
            refs.append(
                CodeRef(
                    path=code_file.path,
                    line=line_number,
                    kind="search",
                    snippet=self.snippet(code_file, line_number),
                    score=score,
                    note="关键词：" + "、".join(dict.fromkeys(hits))[:80],
                )
            )
        return refs

    def references_for_group(self, group: ErrorGroup, limit: int = 6) -> list[CodeRef]:
        refs: list[CodeRef] = []
        for frame in group.stack[:8]:
            refs.extend(self.search_frame(frame, limit=3))

        sample_text = "\n".join(
            [group.title, group.sample_message or ""]
            + [event.raw[:1500] for event in group.events[:3]]
        )
        for candidate, line_number in extract_file_candidates(sample_text)[:6]:
            resolved = self.resolve_path(candidate)
            if not resolved:
                continue
            code_file = self.files[resolved]
            refs.append(
                CodeRef(
                    path=resolved,
                    line=line_number or 0,
                    kind="message",
                    snippet=self.snippet(code_file, line_number or 1)
                    if line_number
                    else code_file.lines[:3],
                    score=6.0,
                    note="日志中提到的文件",
                )
            )

        queries: list[str] = []
        terms: list[str] = []
        if group.exception_type:
            queries.append(group.exception_type)
            simple_type = short_type_name(group.exception_type)
            if len(simple_type) >= 6:
                queries.append(simple_type)
            terms.append(simple_type)
        queries.extend(_QUOTED_RE.findall(sample_text))
        for word in _WORD_RE.findall(sample_text):
            lowered = word.lower()
            if lowered not in _STOPWORDS and lowered not in {term.lower() for term in terms}:
                terms.append(word)
        search_refs: list[CodeRef] = []
        if not refs and group.exception_type:
            search_refs = [
                ref
                for ref in self.search_text(queries=queries, terms=terms[:16], limit=6)
                if ref.score >= 5.0
            ]
        refs.extend(search_refs[:3])

        deduped: dict[tuple[str, int], CodeRef] = {}
        for ref in refs:
            key = (ref.path, ref.line)
            if key not in deduped or ref.score > deduped[key].score:
                deduped[key] = ref
        ordered = sorted(deduped.values(), key=lambda ref: (-ref.score, ref.path, ref.line))
        return ordered[:limit]
