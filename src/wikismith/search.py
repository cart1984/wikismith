"""Hybrid search: BM25 keyword scores blended with local-embedding similarity.

Everything lives in memory: a wiki has hundreds of chunks, not millions, so a brute-force dot product is instant and no
vector database is needed. Embeddings are cached on disk by content hash, so a restart only embeds what changed.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

from wikismith.logfile import unforged_captures
from wikismith.pages import Page, PageKind, load_page
from wikismith.vault import Vault

# Set for jobs: the vault's own cache folder. A job's worktree has a path of its own, so without this its searches
# would re-embed the whole vault into a new cache folder that nothing cleans up.
SHARED_EMBEDDINGS_ENV = "WIKISMITH_SHARED_EMBEDDINGS"

SEMANTIC_WEIGHT = 0.7
# Chunk size in the model's tokens, title and prefix included. Small enough for every model's limit (bge: 512),
# large enough to keep a section's facts together.
CHUNK_TOKENS = 400
# Small batches keep memory flat: fastembed's default of 256 chunks needs over 2 GB at once.
EMBED_BATCH = 16
_TOKEN = re.compile(r"\w+", re.UNICODE)

_BGE = ("Represent this sentence for searching relevant passages: ", "")
# What each model expects in front of queries and passages. Searching works without, but noticeably worse.
PROMPTS: dict[str, tuple[str, str]] = {
    "BAAI/bge-small-en-v1.5": _BGE,
    "BAAI/bge-base-en-v1.5": _BGE,
    "BAAI/bge-large-en-v1.5": _BGE,
    "nomic-ai/nomic-embed-text-v1.5": ("search_query: ", "search_document: "),
    "nomic-ai/nomic-embed-text-v1.5-Q": ("search_query: ", "search_document: "),
    "nomic-ai/nomic-embed-text-v1": ("search_query: ", "search_document: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
    "google/embeddinggemma-300m": ("task: search result | query: ", "title: none | text: "),
}


def model_limit(model: str) -> int:
    """How many tokens a model reads before it truncates, from fastembed's model list (512 if it doesn't say)."""
    from fastembed import TextEmbedding

    for info in TextEmbedding.list_supported_models():
        found = re.search(r"(\d+) input tokens", str(info.get("description", "")))
        if info.get("model") == model and found:
            return int(found.group(1))
    return 512


def estimate_tokens(text: str) -> int:
    """Without the model's tokenizer: about 3 characters per token, as measured on a real vault (citation paths
    tokenize poorly), so the estimate errs on the safe side."""
    return len(text) // 3 + 1


class EmbedderError(RuntimeError):
    pass


class Embedder(Protocol):
    name: str

    @property
    def cache_id(self) -> str: ...

    @property
    def chunk_tokens(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...

    def count_tokens(self, text: str) -> int: ...


class FastEmbedder:
    """Local ONNX embeddings via fastembed. The model loads on first use."""

    def __init__(self, model: str, models: Path) -> None:
        self.name = model
        self.query_prefix, self.passage_prefix = PROMPTS.get(model, ("", ""))
        self._cache_dir = models
        self._model = None
        self._tokenizer: Any = None
        self._lock = threading.Lock()

    @property
    def cache_id(self) -> str:
        """Names the embedding cache: the model, plus its prompts, so changing them never reuses old vectors."""
        prompts = hashlib.sha256(f"{self.query_prefix}|{self.passage_prefix}".encode()).hexdigest()[:6]
        return f"{self.name}-{prompts}"

    @property
    def chunk_tokens(self) -> int:
        """The chunk size for this model: CHUNK_TOKENS, or less if the model reads less (some read only 128)."""
        prefix = self.count_tokens(self.passage_prefix) if self.passage_prefix else 0
        return max(32, min(CHUNK_TOKENS, model_limit(self.name) - prefix - 4))  # 4: the special tokens, and slack

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding  # slow import, only when needed

                try:
                    self._model = TextEmbedding(model_name=self.name, cache_dir=str(self._cache_dir))
                except Exception as e:
                    raise EmbedderError(
                        f"Can't load the embedding model {self.name!r} ({e}). Without network access it must be in "
                        f"the cache already: run `wikismith fetch-model --model {self.name}`, or in Docker build "
                        f"the image with --build-arg WIKISMITH_MODEL={self.name}."
                    ) from e
            return self._model

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        prefixed = [self.passage_prefix + t for t in texts]
        vectors = self._load().passage_embed(prefixed, batch_size=EMBED_BATCH)
        return _normalize(np.array(list(vectors), dtype=np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        prefixed = self.query_prefix + text
        return _normalize(np.array(list(self._load().query_embed(prefixed)), dtype=np.float32))[0]

    def count_tokens(self, text: str) -> int:
        """Tokens as the model counts them (without its special tokens); an estimate if it has no tokenizer."""
        if self._tokenizer is None:
            tokenizer = getattr(getattr(self._load(), "model", None), "tokenizer", None)
            if tokenizer is None:
                self._tokenizer = False
            else:
                from tokenizers import Tokenizer

                self._tokenizer = Tokenizer.from_str(tokenizer.to_str())  # a copy: the model's own truncates
                self._tokenizer.no_truncation()
                self._tokenizer.no_padding()
        if self._tokenizer is False:
            return estimate_tokens(text)
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)


# The placeholders `Scanner.placeholder` puts into the index instead of secret values.
MASKED = re.compile(r"\[(?:secret|personal data): [^\]\n]+\]")


@dataclass(frozen=True)
class Chunk:
    path: str
    kind: PageKind
    area: str | None
    title: str
    heading: str | None
    text: str

    @property
    def masked(self) -> bool:
        """Whether a secret or personal data was masked in this chunk."""
        return MASKED.search(self.text) is not None

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.title}\n{self.heading}\n{self.text}".encode()).hexdigest()

    @property
    def embed_text(self) -> str:
        context = self.title if not self.heading else f"{self.title} > {self.heading}"
        return f"{context}\n\n{self.text}"


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


def chunk_page(page: Page, count: Callable[[str], int] = estimate_tokens, budget: int = CHUNK_TOKENS) -> list[Chunk]:
    """Split a page at its headings (levels 1-3), and long sections into pieces of at most `budget` tokens: at
    paragraphs, then at lines, then between words. `count` is the model's token counter."""
    body = page.doc.frontmatter.body
    cuts = [h for h in page.doc.headings if h.level <= 3]
    sections: list[tuple[str | None, str]] = []
    start, heading = 0, None
    for h in cuts:
        sections.append((heading, body[start : h.offset]))
        start, heading = h.offset, h.text
    sections.append((heading, body[start:]))
    chunks = []
    for heading, text in sections:
        text = re.sub(r"^#{1,6}\s+.*\n?", "", text.strip(), count=1).strip()
        if heading == page.title:
            heading = None
        context = page.title if not heading else f"{page.title} > {heading}"
        room = max(budget - count(context) - 8, budget // 4)  # the title and heading go into every piece
        for piece in _split(text, room, count):
            chunks.append(Chunk(page.path, page.kind, page.area, page.title, heading, piece))
    return chunks


def _split(text: str, budget: int, count: Callable[[str], int]) -> list[str]:
    if not text:
        return []
    if count(text) <= budget:
        return [text]
    units: list[tuple[str, str]] = []  # (text, what joins it to the one before)
    for para in text.split("\n\n"):
        if count(para) <= budget:
            units.append((para, "\n\n"))
            continue
        for i, line in enumerate(para.split("\n")):
            joiner = "\n\n" if i == 0 else "\n"
            units += [(part, joiner if j == 0 else " ") for j, part in enumerate(_split_words(line, budget, count))]
    pieces, current, used = [], "", 0
    for unit, joiner in units:
        n = count(unit) + 1
        if current and used + n > budget:
            pieces.append(current.strip())
            current, used = "", 0
        current += (joiner if current else "") + unit
        used += n
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _split_words(line: str, budget: int, count: Callable[[str], int]) -> list[str]:
    """A single line too long for one chunk (a huge table row, a pasted blob): cut between words."""
    if count(line) <= budget:
        return [line]
    words = line.split(" ")
    if len(words) == 1:
        half = len(line) // 2  # no spaces at all: cut in the middle
        return _split_words(line[:half], budget, count) + _split_words(line[half:], budget, count)
    half = len(words) // 2
    return _split_words(" ".join(words[:half]), budget, count) + _split_words(" ".join(words[half:]), budget, count)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avg = (sum(self.lengths) / len(docs)) if docs else 0.0
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.tfs), dtype=np.float32)
        for i, tf in enumerate(self.tfs):
            norm = self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avg or 1))
            out[i] = sum(self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in query if t in tf)
        return out


class SearchIndex:
    """Chunks of every searchable page, rebuilt incrementally when files change.

    `mask` (with `secrets.mask_reads`) runs over each page before it's indexed, so neither the keyword index nor the
    embeddings (cached on disk) ever see a secret: searching for a secret's value finds nothing.
    """

    def __init__(self, vault: Vault, embedder: Embedder | None, mask: Callable[[str], str] | None = None) -> None:
        self.vault = vault
        self.embedder = embedder
        self.mask = mask
        self._lock = threading.Lock()
        self._files: dict[str, tuple[float, list[Chunk]]] = {}
        self._chunks: list[Chunk] = []
        self._bm25 = BM25([])
        self._vectors: np.ndarray | None = None
        shared = os.environ.get(SHARED_EMBEDDINGS_ENV)
        cache_id = embedder.cache_id if embedder else ""
        # A shared cache only grows: what a job's worktree lacks may still be in the vault the server reads.
        self._cache = EmbeddingCache(Path(shared) if shared else vault.cache_dir, cache_id, prune=not shared)
        self.count: Callable[[str], int] = embedder.count_tokens if embedder else estimate_tokens
        self._chunk_tokens: int | None = None
        self.status: Literal["building", "ready", "failed"] = "building"  # for /healthz

    def _sources(self) -> list[tuple[Path, PageKind]]:
        access = self.vault.config.access
        log = self.vault.log_file.resolve()
        found: list[tuple[Path, PageKind]] = []
        found.extend((p, "wiki") for p in self.vault.markdown_files(self.vault.wiki_dir) if p.resolve() != log)
        if self.vault.config.layout.archive in access.read:
            found.extend((p, "archive") for p in self.vault.markdown_files(self.vault.archive_dir))
        if access.search_captures:
            found.extend((p, "capture") for p in unforged_captures(self.vault))
        return found

    @property
    def chunk_tokens(self) -> int:
        if self._chunk_tokens is None:
            self._chunk_tokens = self.embedder.chunk_tokens if self.embedder else CHUNK_TOKENS
        return self._chunk_tokens

    def refresh(self) -> None:
        try:
            self._refresh()
        except Exception:
            self.status = "failed"
            raise
        self.status = "ready"

    def _refresh(self) -> None:
        with self._lock:
            files: dict[str, tuple[float, list[Chunk]]] = {}
            changed = False
            for path, kind in self._sources():
                rel, mtime = self.vault.rel(path), path.stat().st_mtime
                cached = self._files.get(rel)
                if cached and cached[0] == mtime:
                    files[rel] = cached
                    continue
                page = load_page(self.vault, path, kind)
                if self.mask is not None:
                    page = replace(page, text=self.mask(page.text))
                files[rel] = (mtime, chunk_page(page, self.count, self.chunk_tokens))
                changed = True
            if not changed and files.keys() == self._files.keys():
                return
            self._files = files
            self._chunks = [c for _, chunks in files.values() for c in chunks]
            self._bm25 = BM25([tokenize(c.embed_text) for c in self._chunks])
            if self.embedder is not None:
                self._vectors = self._cache.vectors(self._chunks, self.embedder)

    def search(self, query: str, top_k: int = 5, area: str | None = None, include_archive: bool = False) -> list[Hit]:
        """The best hits, one per page. Archived pages only with `include_archive` (they're outdated)."""
        self.refresh()
        if not self._chunks or not query.strip():
            return []
        keyword = _scale(self._bm25.scores(tokenize(query)))
        if self.embedder is not None and self._vectors is not None:
            semantic = _scale(self._vectors @ self.embedder.embed_query(query))
            scores = SEMANTIC_WEIGHT * semantic + (1 - SEMANTIC_WEIGHT) * keyword
        else:
            scores = keyword
        best: dict[str, Hit] = {}
        for i in np.argsort(-scores):
            chunk = self._chunks[i]
            if scores[i] <= 0 or (area and chunk.area != area) or chunk.path in best:
                continue
            if chunk.kind == "archive" and not include_archive:
                continue
            best[chunk.path] = Hit(chunk, float(scores[i]))
            if len(best) == top_k:
                break
        return list(best.values())

    @property
    def size(self) -> tuple[int, int]:
        return len(self._files), len(self._chunks)


class EmbeddingCache:
    """Embeddings keyed by chunk content hash, persisted as one `.npz` per model."""

    def __init__(self, cache_dir: Path, model: str, prune: bool = True) -> None:
        slug = re.sub(r"[^A-Za-z0-9.-]+", "_", model)
        self.path = cache_dir / f"embeddings-{slug}.npz"
        self.prune = prune  # drop vectors of chunks that no longer exist
        self._vectors: dict[str, np.ndarray] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with np.load(self.path) as data:
                self._vectors = {k: data[k] for k in data.files}
        except (OSError, ValueError, zipfile.BadZipFile):  # a damaged cache only costs re-embedding
            self._vectors = {}

    def vectors(self, chunks: list[Chunk], embedder: Embedder) -> np.ndarray:
        self._load()
        keys = [c.key for c in chunks]
        missing = [i for i, k in enumerate(keys) if k not in self._vectors]
        if missing:
            new = embedder.embed_passages([chunks[i].embed_text for i in missing])
            for i, vec in zip(missing, new, strict=True):
                self._vectors[keys[i]] = vec
        wanted = set(keys)
        stale = [k for k in self._vectors if k not in wanted] if self.prune else []
        for k in stale:
            del self._vectors[k]
        if missing or stale:
            self._save()
        if not keys:
            return np.zeros((0, 0), dtype=np.float32)
        return np.stack([self._vectors[k] for k in keys])

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp.npz")  # the server and a job may save at once
            np.savez(tmp, **self._vectors)  # pyright: ignore[reportArgumentType]
            tmp.replace(self.path)
        except OSError:
            pass  # a read-only cache only costs re-embedding on the next start


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def _scale(scores: np.ndarray) -> np.ndarray:
    """Min-max scale to 0..1 so keyword and semantic scores can be blended."""
    if scores.size == 0:
        return scores
    lo, hi = float(scores.min()), float(scores.max())
    if hi <= lo:  # all equal, e.g. a vault with a single page: a match is a full match, not none
        return np.ones_like(scores) if hi > 0 else np.zeros_like(scores)
    return (scores - lo) / (hi - lo)
