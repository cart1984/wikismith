from wikismith.logfile import logged_paths, unforged_captures, unforged_raw_files
from wikismith.vault import Vault


def test_logfile(vault: Vault) -> None:
    assert "raw/notes/2026-01-01_tomato-thread.md" in logged_paths(vault)
    with vault.log_file.open("a") as log:
        log.write("- raw/captures/2026-01-07 dentist visit.md — 2026-01-08 — health\n")
        log.write("- `raw/notes/quoted name.md`\n")
    assert {"raw/captures/2026-01-07 dentist visit.md", "raw/notes/quoted name.md"} <= logged_paths(vault)
    assert [vault.rel(p) for p in unforged_raw_files(vault)] == [
        "raw/captures/2026-01-06_pepper-seeds.md",
        "raw/notes/2026-01-05_router-setup.md",
    ]
    assert [vault.rel(p) for p in unforged_captures(vault)] == ["raw/captures/2026-01-06_pepper-seeds.md"]
