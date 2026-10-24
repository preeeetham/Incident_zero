"""Knowledge source 1: the demo's source code.

Indexes the services' code under <demo_dir>/src so the Remediation Planner can follow an error message seen in the
telemetry to the line that raises it, and see which feature flags guard that code path. Flags are found by reading
how the code evaluates them (OpenFeature calls, constants, generated accessors, one level of wrapper functions),
not from any list written by hand.

It reads code, never flag settings: src/flagd and src/flagd-ui (the flag file and its UI) are not indexed, and only
source files are read (no JSON or YAML). It is used after the diagnosis, to choose a fix; never to diagnose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# flag infrastructure (R1), vendored code, tests and observability backends are not part of the services' code
SKIP_DIRS = {"flagd", "flagd-ui", "node_modules", "vendor", "test", "tests", "__tests__", "testdata", "build", "dist",
             "target", "bin", "obj", ".git", ".next", "telemetry-docs", "grafana", "prometheus", "opensearch",
             "otel-collector", "jaeger", "react-native-app", "postgresql", "kafka", "opamp-server", "cypress", "e2e"}
EXTS = {".go", ".java", ".kt", ".cs", ".js", ".mjs", ".ts", ".tsx", ".py", ".rb", ".rs", ".php", ".cpp", ".cc",
        ".h", ".ex", ".exs", ".erl", ".scala"}
MAX_FILE = 300_000
GENERATED = re.compile(r"_gen\.go$|\.generated\.|\.pb\.|_pb2|\.d\.ts$")

KEY = r"[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*"                    # flag keys are camelCase: paymentFailure
EVAL = re.compile(r"(?i)(?:value|flag|boolean|resolve)\w*\s*\(")  # getBooleanValue( fetch_number_value( Boolean(
WRITE = re.compile(r"(?i)\bset\w*(?:value|flag)\w*\s*\(")       # setSessionValue('currencyCode', ...) is not a flag
LITERAL = re.compile(r"""["'](""" + KEY + r""")["']""")
CONST = re.compile(r"""\b([A-Za-z_]\w*)\s*(?::[^=\n]{1,40})?=\s*["'](""" + KEY + r""")["']""")
GO_ACCESSOR_DOC = re.compile(r"""//\s*(\w+) returns the value of the "(""" + KEY + r""")" feature flag""")
GO_ACCESSOR_USE = re.compile(r"\bflags\.(\w+)\.Value\(")
FUNC = re.compile(r"(?:\bfunc\s+(?:\([^)]*\)\s*)?|\bdef\s+|\bfunction\s+|\bfun\s+|\bfn\s+|"
                  r"(?:private|public|protected|static|async|override|suspend)[\w\s<>\[\],?]*?\s)(\w+)\s*\(")
ASSIGN = re.compile(r"(?:\b(?:var|let|const|val|bool|int|double|float)\s+)?\b([A-Za-z_]\w*)\s*(?::[^=\n]{1,30})?:?=\s*")
COMMENT = re.compile(r"^\s*(//|#|\*|/\*)")


@dataclass
class FlagSite:
    flag: str
    service: str
    path: str        # relative to src/
    line: int        # 1-based
    text: str        # the line, stripped
    via: str = ""    # wrapper function the flag is read through, if any


@dataclass
class CodeHit:
    service: str
    path: str
    line: int
    text: str
    guards: list[FlagSite] = field(default_factory=list)


class CodeIndex:
    def __init__(self, src: Path):
        self.src = src
        self.files: dict[str, list[str]] = {}
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if (not p.is_file() or p.suffix not in EXTS or any(part in SKIP_DIRS for part in rel.parts[:-1])
                    or "test" in p.name.lower() or p.stat().st_size > MAX_FILE):
                continue
            try:
                self.files[rel.as_posix()] = p.read_text(errors="replace").splitlines()
            except OSError:
                continue
        self.sites: list[FlagSite] = self._find_sites()

    @classmethod
    def from_demo(cls, demo_dir: str) -> Optional["CodeIndex"]:
        src = Path(demo_dir).expanduser() / "src" if demo_dir else None
        return cls(src) if src and src.is_dir() else None

    @staticmethod
    def service_of(path: str) -> str:
        return path.split("/", 1)[0]

    # ------------------------------------------------------------ flag sites
    def _find_sites(self) -> list[FlagSite]:
        accessors: dict[str, str] = {}
        for lines in self.files.values():
            for ln in lines:
                if m := GO_ACCESSOR_DOC.search(ln):
                    accessors[m[1]] = m[2]
        sites: list[FlagSite] = []
        for path, lines in self.files.items():
            if GENERATED.search(path):
                continue
            consts = {m[1]: m[2] for ln in lines for m in CONST.finditer(ln)}
            for i, ln in enumerate(lines):
                if COMMENT.match(ln):
                    continue
                keys: list[str] = []
                if EVAL.search(ln) and not WRITE.search(ln):
                    keys += LITERAL.findall(ln)
                    keys += [v for k, v in consts.items() if re.search(rf"\b{re.escape(k)}\b", ln) and not CONST.search(ln)]
                keys += [accessors[a] for a in GO_ACCESSOR_USE.findall(ln) if a in accessors]
                for k in dict.fromkeys(keys):
                    sites.append(FlagSite(k, self.service_of(path), path, i + 1, ln.strip()))
        # one level of wrappers: `func checkProductFailure(...) { return flags.X.Value(...) }` makes every call of
        # checkProductFailure a site of X
        wrapped: list[FlagSite] = []
        for s in sites:
            lines = self.files[s.path]
            for j in range(s.line - 1, max(-1, s.line - 6), -1):
                m = FUNC.search(lines[j])
                if not m:
                    continue
                name = m[1]
                if len(name) < 6:
                    break
                call = re.compile(rf"\b{re.escape(name)}\s*\(")
                for path, other in self.files.items():
                    if self.service_of(path) != s.service:
                        continue
                    for k, ln in enumerate(other):
                        if call.search(ln) and not FUNC.search(ln) and not COMMENT.match(ln):
                            wrapped.append(FlagSite(s.flag, s.service, path, k + 1, ln.strip(), via=name))
                break
        return sites + wrapped

    def flag_sites(self, service: str) -> list[FlagSite]:
        return [s for s in self.sites if s.service == service]

    # ------------------------------------------------------------ search
    def snippet(self, path: str, line: int, before: int = 4, after: int = 4) -> str:
        lines = self.files.get(path, [])
        lo, hi = max(1, line - before), min(len(lines), line + after)
        return "\n".join(f"{n}: {lines[n - 1].rstrip()}" for n in range(lo, hi + 1))

    def _guards(self, path: str, line: int) -> list[FlagSite]:
        """The flag site that decides whether `line` runs: in the same file, at most 20 lines above it, either a
        condition itself or assigning a variable used between the site and the line; the nearest such site."""
        lines = self.files[path]
        out = []
        for s in self.sites:
            if s.path != path or not (line - 20 <= s.line <= line):
                continue
            if re.search(r"\b(if|elif|while|when|unless)\b|&&", s.text) or s.line == line:
                out.append(s)
                continue
            m = ASSIGN.match(s.text)
            var = m[1] if m else None
            if var and any(re.search(rf"\b{re.escape(var)}\b", lines[k]) for k in range(s.line, line)):
                out.append(s)
        # the innermost decision wins: an earlier `if (flagA) {...}` block has closed before `if (flagB)` opens
        nearest = max((s.line for s in out), default=0)
        return [s for s in out if s.line == nearest]

    def search(self, needle: str, limit: int = 6) -> list[CodeHit]:
        n = needle.lower()
        hits = []
        for path, lines in self.files.items():
            for i, ln in enumerate(lines):
                if n in ln.lower() and not COMMENT.match(ln):
                    hits.append(CodeHit(self.service_of(path), path, i + 1, ln.strip(), self._guards(path, i + 1)))
                    if len(hits) > limit:
                        return hits  # too generic to be the line that raised this error
        return hits
