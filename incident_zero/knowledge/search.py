"""BM25 ranking over a small corpus (runbook sections, past incidents). The corpora are tens of documents, so a
keyword ranker is exact, explainable and needs no embedding model or vector store."""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "is", "it", "its", "for", "with", "by", "as", "at",
        "be", "this", "that", "from", "are", "was", "not", "no", "but", "if", "so", "than", "then", "when"}


def tokenize(text: str) -> list[str]:
    """Lowercased words; camelCase and snake_case are split, so `resource_cpu` matches `cpu`."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str) -> list[float]:
        q = set(tokenize(query))
        out = []
        for d, ln in zip(self.docs, self.lens):
            s = 0.0
            for t in q & d.keys():
                tf = d[t]
                s += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out

    def top(self, query: str, k: int = 3, min_score: float = 0.5) -> list[tuple[int, float]]:
        ranked = sorted(enumerate(self.scores(query)), key=lambda x: -x[1])
        return [(i, s) for i, s in ranked[:k] if s >= min_score]
