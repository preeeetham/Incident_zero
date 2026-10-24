"""Knowledge source 2: runbooks, organised by symptom (never by injected fault), one `##` section per situation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .search import BM25

RUNBOOK_DIR = Path(__file__).with_name("runbooks")


@dataclass
class Section:
    file: str
    title: str
    text: str

    @property
    def ref(self) -> str:
        return f"{self.file}#{self.title}"


def load_sections(directory: Path = RUNBOOK_DIR) -> list[Section]:
    out: list[Section] = []
    for p in sorted(directory.glob("*.md")):
        title, body = None, []
        for line in p.read_text().splitlines() + ["## "]:
            if line.startswith("## "):
                if title and any(b.strip() for b in body):
                    out.append(Section(p.name, title, "\n".join(body).strip()))
                title, body = line[3:].strip(), []
            elif title is not None:
                body.append(line)
    return out


class Runbooks:
    def __init__(self, directory: Path = RUNBOOK_DIR):
        self.sections = load_sections(directory)
        self.bm25 = BM25([f"{s.title} {s.title} {s.text}" for s in self.sections])

    def search(self, query: str, k: int = 2) -> list[Section]:
        return [self.sections[i] for i, _ in self.bm25.top(query, k)]
