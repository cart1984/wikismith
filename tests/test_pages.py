from wikismith.pages import Catalog
from wikismith.vault import Vault


def test_catalog_titles_and_areas(vault: Vault) -> None:
    catalog = Catalog(vault)
    page = catalog.resolve("tomatoes")
    assert (page.title, page.area, page.kind) == ("Tomatoes", "garden", "wiki")
    assert catalog.areas() == ["garden", "kitchen"]
    assert catalog.get("wiki/_log.md") is not None
