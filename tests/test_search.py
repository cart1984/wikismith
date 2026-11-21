import hashlib
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from conftest import make_vault, write_files
from wikismith.pages import Catalog, Page
from wikismith.reader import Reader
from wikismith.search import BM25, SHARED_EMBEDDINGS_ENV, SearchIndex, chunk_page, tokenize
from wikismith.vault import Vault

SYNONYMS = {"irrigation": "watering", "hydrate": "watering", "loaf": "bread"}


class FakeEmbedder:
    """Bag of words over hashed token vectors; synonyms share a vector, so it 'understands' them."""

    name = "fake-model"
    cache_id = "fake-model"
    chunk_tokens = 400

    def __init__(self) -> None:
        self.embedded = 0

    def count_tokens(self, text: str) -> int:
        return len(tokenize(text))  # a word-level tokenizer, so tests can count by hand

    def _vec(self, text: str) -> np.ndarray:
        out = np.zeros(64, dtype=np.float32)
        for token in tokenize(text):
            token = SYNONYMS.get(token, token)
            seed = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "little")
            rng = np.random.default_rng(seed)
            out += rng.standard_normal(64).astype(np.float32)
        norm = np.linalg.norm(out)
        return out / norm if norm else out

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        self.embedded += len(texts)
        self.seen = [*getattr(self, "seen", []), *texts]
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def paths(index: SearchIndex, query: str, **kw: object) -> list[str]:
    return [h.chunk.path for h in index.search(query, **kw)]  # type: ignore[arg-type]


def test_chunks_follow_headings(vault: Vault) -> None:
    chunks = chunk_page(Catalog(vault).resolve("tomatoes"))
    assert [c.heading for c in chunks] == ["Varieties", "Watering"]
    assert chunks[0].text.startswith("San Marzano")
    assert chunks[1].embed_text.startswith("Tomatoes > Watering\n\n")


def test_long_sections_split_at_paragraphs(vault: Vault) -> None:
    long = "\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(10))
    write_files(vault.root, {"wiki/garden/long.md": f"# Long\n\n{long}\n"})
    chunks = chunk_page(Catalog(vault).resolve("long"))
    assert len(chunks) > 1
    assert all(len(c.text) <= 1600 for c in chunks)


def test_bm25_prefers_rarer_terms() -> None:
    bm = BM25([["tomato", "sauce"], ["tomato", "salad"], ["bread"]])
    scores = bm.scores(["sauce"])
    assert scores[0] > 0 and scores[1] == 0 and scores[2] == 0


def test_keyword_search(vault: Vault) -> None:
    index = SearchIndex(vault, None)
    assert paths(index, "San Marzano sauce")[0] == "wiki/garden/tomatoes.md"
    assert paths(index, "Dutch oven")[0] == "wiki/kitchen/sourdough.md"


def test_one_hit_per_page_and_area_filter(vault: Vault) -> None:
    index = SearchIndex(vault, None)
    found = paths(index, "garden tomatoes beds watering", top_k=10)
    assert len(found) == len(set(found))
    assert all(p.startswith("wiki/kitchen/") for p in paths(index, "garden tomatoes bread", area="kitchen"))


def test_unforged_captures_are_searchable(vault: Vault) -> None:
    hits = SearchIndex(vault, None).search("chili pepper seeds")
    assert hits[0].chunk.path == "raw/captures/2026-01-06_pepper-seeds.md"
    assert hits[0].chunk.kind == "capture"


def test_forged_captures_and_other_raw_files_are_not(vault: Vault) -> None:
    index = SearchIndex(vault, None)
    assert "raw/captures/2026-01-03_bread-notes.md" not in paths(index, "starter bread notes", top_k=20)
    assert index.search("hunter2 router admin password") == []


def test_log_is_not_searchable(vault: Vault) -> None:
    assert "wiki/_log.md" not in paths(SearchIndex(vault, None), "tomato thread log", top_k=20)


def test_captures_excluded_when_disabled(vault_root: Path) -> None:
    vault = make_vault(vault_root, access={"search_captures": False})
    assert "raw/captures/2026-01-06_pepper-seeds.md" not in paths(SearchIndex(vault, None), "chili pepper")


def test_archive_only_when_asked_and_readable(vault: Vault, vault_root: Path) -> None:
    index = SearchIndex(vault, None)
    assert "archive/garden/old-hose.md" not in paths(index, "old hose replaced")
    assert "archive/garden/old-hose.md" in paths(index, "old hose replaced", include_archive=True)
    wiki_only = make_vault(vault_root, access={"read": ["wiki"]}, search={"semantic": False})
    assert "archive/garden/old-hose.md" not in paths(SearchIndex(wiki_only, None), "old hose", include_archive=True)


def test_semantic_search_finds_synonyms(vault: Vault) -> None:
    index = SearchIndex(vault, FakeEmbedder())
    assert paths(index, "irrigation")[0] == "wiki/garden/tomatoes.md"
    assert SearchIndex(vault, None).search("irrigation") == []


def test_refresh_is_incremental_and_cached(vault: Vault) -> None:
    embedder = FakeEmbedder()
    index = SearchIndex(vault, embedder)
    index.search("tomatoes")
    first = embedder.embedded
    assert first > 0
    index.search("bread")
    assert embedder.embedded == first  # nothing changed, nothing re-embedded

    page = vault.wiki_dir / "kitchen/knife-care.md"
    page.write_text(page.read_text() + "\nUse a whetstone for sharpening.\n")
    os.utime(page, (page.stat().st_atime, page.stat().st_mtime + 5))
    assert paths(index, "whetstone")[0] == "wiki/kitchen/knife-care.md"
    assert embedder.embedded == first + 1  # only the changed chunk

    again = FakeEmbedder()
    SearchIndex(vault, again).search("tomatoes")
    assert again.embedded == 0  # a fresh index (restart) uses the on-disk cache


def test_job_worktree_shares_the_vault_cache(vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    SearchIndex(vault, FakeEmbedder()).search("tomatoes")  # the server's index fills the vault's cache
    tree = tmp_path / "tree"
    shutil.copytree(vault.root, tree)
    (tree / "wiki/kitchen/sourdough.md").unlink()  # a forge merged this page away

    monkeypatch.setenv(SHARED_EMBEDDINGS_ENV, str(vault.cache_dir))
    in_job = FakeEmbedder()
    SearchIndex(Vault.open(tree), in_job).search("tomatoes")
    assert in_job.embedded == 0  # nothing re-embedded for the worktree

    monkeypatch.delenv(SHARED_EMBEDDINGS_ENV)
    server = FakeEmbedder()
    SearchIndex(vault, server).search("tomatoes")
    assert server.embedded == 0  # the job didn't drop vectors the vault still needs


def test_deleted_pages_drop_out(vault: Vault) -> None:
    index = SearchIndex(vault, FakeEmbedder())
    assert "wiki/kitchen/sourdough.md" in paths(index, "Dutch oven")
    (vault.wiki_dir / "kitchen/sourdough.md").unlink()
    assert "wiki/kitchen/sourdough.md" not in paths(index, "Dutch oven")


def test_reader_formats_hits(vault: Vault) -> None:
    out = Reader(vault).search("chili pepper seeds")
    assert out.startswith("1. **Pepper seeds** — raw/captures/2026-01-06_pepper-seeds.md [unforged capture")
    assert Reader(vault).search("zzzz nothing") == "No results for 'zzzz nothing'."
    assert "**Tomatoes > Watering** — wiki/garden/tomatoes.md" in Reader(vault).search("deep watering mulch")


@pytest.mark.skipif(not os.environ.get("WIKISMITH_TEST_MODEL"), reason="set WIKISMITH_TEST_MODEL=1 to run")
def test_real_model(vault: Vault) -> None:
    from wikismith.search import FastEmbedder
    from wikismith.vault import default_cache_dir

    real_cache = Path(os.environ.get("WIKISMITH_TEST_MODEL_CACHE", Path.home() / ".cache/wikismith"))
    embedder = FastEmbedder("BAAI/bge-small-en-v1.5", real_cache / "models")
    index = SearchIndex(vault, embedder)
    assert paths(index, "how often should I irrigate the plants")[0] == "wiki/garden/tomatoes.md"
    assert paths(index, "baking a loaf")[0] == "wiki/kitchen/sourdough.md"
    assert default_cache_dir(None).name == "cache"  # the test cache, not the user's
    # A realistic long page: no chunk, with its title, prefix and special tokens, goes over the model's 512.
    page = Page("wiki/x/long.md", "wiki", "x", "# Long\n\n" + LONG_SECTION, 0.0)
    for chunk in chunk_page(page, embedder.count_tokens):
        assert embedder.count_tokens(embedder.passage_prefix + chunk.embed_text) + 2 <= 512


SHED = {"wiki/garden/shed.md": "# Shed\n\nThe shed lock: password: correct-horse-9 (in the drawer).\n"}


def test_secrets_are_masked_before_indexing(vault_root: Path) -> None:
    from wikismith.secretscan import Scanner

    write_files(vault_root, SHED)
    vault = make_vault(vault_root, search={"semantic": True})
    keywords = SearchIndex(vault, None, Scanner().placeholder)
    assert "wiki/garden/shed.md" not in paths(keywords, "correct-horse-9")  # the value isn't in the index
    assert paths(keywords, "shed lock password")[0] == "wiki/garden/shed.md"  # what it is still is
    embedder = FakeEmbedder()
    index = SearchIndex(vault, embedder, Scanner().placeholder)
    index.refresh()
    assert embedder.seen and not any("correct-horse" in text for text in embedder.seen)  # nor in the embeddings
    out = Reader(vault, index, Scanner().redact).search("shed lock password")
    assert "[secret: password or key assignment]" in out and "[contains a masked secret]" in out


def test_without_mask_reads_nothing_is_masked(vault_root: Path) -> None:
    write_files(vault_root, SHED)
    vault = make_vault(vault_root, search={"semantic": False})
    out = Reader(vault, SearchIndex(vault, None)).search("shed lock")
    assert "correct-horse-9" in out and "masked" not in out


# One section that is far too long: a paragraph, a long bullet list with citations, and one huge line.
LONG_SECTION = (
    "Intro paragraph about the irrigation setup and why it matters. " * 20
    + "\n\n"
    + "\n".join(
        f"- Zone {i}: drip line, 2 l/h emitters (source: raw/captures/2026-0{i % 9 + 1}-01_zone-{i}.md)"
        for i in range(80)
    )
    + "\n\n"
    + "word " * 900
)


def test_long_sections_are_split_within_the_budget() -> None:
    def count(text: str) -> int:
        return len(tokenize(text))

    page = Page("wiki/x/long.md", "wiki", "x", "# Long\n\n## Irrigation\n\n" + LONG_SECTION, 0.0)
    chunks = chunk_page(page, count, budget=100)
    assert len(chunks) > 10
    for chunk in chunks:
        assert count(chunk.embed_text) <= 100, chunk.text[:80]
    text = " ".join(chunk.text for chunk in chunks)
    assert "Zone 0:" in text and "Zone 79:" in text  # nothing lost
    assert any(c.text.startswith("- Zone") for c in chunks)  # the list is cut between lines


def test_models_get_their_prompts(tmp_path: Path) -> None:
    from wikismith.search import FastEmbedder

    nomic = FastEmbedder("nomic-ai/nomic-embed-text-v1.5-Q", tmp_path)
    assert (nomic.query_prefix, nomic.passage_prefix) == ("search_query: ", "search_document: ")
    bge = FastEmbedder("BAAI/bge-small-en-v1.5", tmp_path)
    assert bge.passage_prefix == "" and bge.query_prefix.startswith("Represent this sentence")
    assert bge.cache_id.startswith("BAAI/bge-small-en-v1.5-") and bge.cache_id != nomic.cache_id

    class Model:
        def passage_embed(self, texts: list[str], batch_size: int) -> list[list[float]]:
            self.texts = texts
            return [[1.0, 0.0] for _ in texts]

    model = Model()
    nomic._model = model  # type: ignore[assignment]
    nomic.embed_passages(["Deep watering"])
    assert model.texts == ["search_document: Deep watering"]


def test_unknown_model_explains_what_to_do(tmp_path: Path) -> None:
    from wikismith.search import EmbedderError, FastEmbedder

    with pytest.raises(EmbedderError, match="--build-arg WIKISMITH_MODEL=no/such-model"):
        FastEmbedder("no/such-model", tmp_path).embed_query("x")


def test_chunks_fit_models_that_read_less(tmp_path: Path) -> None:
    from wikismith.search import FastEmbedder, model_limit

    assert model_limit("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") == 128
    assert model_limit("nomic-ai/nomic-embed-text-v1.5-Q") == 8192
    assert FastEmbedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", tmp_path).chunk_tokens == 124
    assert FastEmbedder("minishlab/potion-retrieval-32M", tmp_path).chunk_tokens == 400


def test_a_vault_with_a_single_page_finds_it(tmp_path: Path) -> None:
    """All scores equal (one chunk) must still count as a match: a new vault's first capture has to be findable."""
    root = tmp_path / "v"
    write_files(root, {"wikismith.yaml": "name: v\n", "raw/captures/2026-02-01_test.md": "# Test\n\nSaved here.\n"})
    vault = make_vault(root, search={"semantic": False})
    assert paths(SearchIndex(vault, None), "saved here") == ["raw/captures/2026-02-01_test.md"]
    assert paths(SearchIndex(vault, FakeEmbedder()), "saved here") == ["raw/captures/2026-02-01_test.md"]
    assert paths(SearchIndex(vault, None), "nothing like it") == []  # no keyword match: still nothing
