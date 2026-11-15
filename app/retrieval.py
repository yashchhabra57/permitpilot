"""BM25 keyword retrieval over ordinance chunks, written from scratch.

BM25 scores a document D for a query Q as

    score(D, Q) = sum over terms t in Q of
        idf(t) * tf(t, D) * (k1 + 1) / (tf(t, D) + k1 * (1 - b + b * |D| / avgdl))

where tf is how often t appears in D, |D| is D's length in tokens, avgdl is the average length,
and idf(t) = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5)) rewards rare terms. k1 limits how much
repeating a word helps; b controls how much long documents are penalized.

Ordinances use precise legal vocabulary ("setback", "footing", "guard"), so exact-term matching
works well and needs no embedding model, API key, or vector database.
"""

import math
import re
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

STOPWORDS = frozenset(
    """a an and are as at be by for from has have i if in is it its my of on or that the this
    to was were will with what when where which who how do does can need any all""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    """A deliberately tiny plural-only stemmer: 'decks' -> 'deck', 'properties' -> 'property'.

    It does not touch '-ing' or '-ed': in building codes 'footing' and 'foot' mean different
    things, and over-stemming would merge them.
    """
    if len(word) <= 3 or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("xes", "ches", "shes", "sses")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


@dataclass(frozen=True)
class Hit[K: Hashable]:
    key: K
    score: float


class BM25Index[K: Hashable]:
    def __init__(self, docs: Sequence[tuple[K, str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.keys: list[K] = [k for k, _ in docs]
        self.tfs: list[Counter[str]] = [Counter(tokenize(text)) for _, text in docs]
        self.lengths = [sum(tf.values()) for tf in self.tfs]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, query_terms: Sequence[str], i: int) -> float:
        tf = self.tfs[i]
        length_norm = 1 - self.b + self.b * (self.lengths[i] / self.avgdl if self.avgdl else 0)
        total = 0.0
        for t in query_terms:
            f = tf.get(t, 0)
            if f == 0:
                continue
            total += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * length_norm)
        return total

    def search(self, query: str, k: int = 5) -> list[Hit[K]]:
        terms = list(dict.fromkeys(tokenize(query)))  # dedupe, keep order
        if not terms or not self.keys:
            return []
        scored = [Hit(self.keys[i], self.score(terms, i)) for i in range(len(self.keys))]
        scored = [h for h in scored if h.score > 0]
        # Ties broken by original order so results are deterministic.
        order = {key: i for i, key in enumerate(self.keys)}
        scored.sort(key=lambda h: (-h.score, order[h.key]))
        return scored[:k]


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def best_sentences(query: str, text: str, limit: int = 2) -> list[str]:
    """Pick the sentences of a chunk that share the most query terms (an extractive answer)."""
    terms = set(tokenize(query))
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    ranked = sorted(
        enumerate(sentences),
        key=lambda pair: (-len(terms & set(tokenize(pair[1]))), pair[0]),
    )
    picked = [(i, s) for i, s in ranked[:limit] if terms & set(tokenize(s))]
    return [s for _, s in sorted(picked)]
