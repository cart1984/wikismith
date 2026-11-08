from pathlib import Path

import pytest

from wikismith.config import ConfigError, load_config, parse_config


def test_minimal_config_gets_defaults() -> None:
    config = parse_config({"name": "personal"})
    assert config.layout.wiki == "wiki"
    assert config.access.read == ("wiki", "archive")
    assert config.secrets.policy == "redact" and config.secrets.pii == "redact"
    assert config.secrets.mask_reads is True
    assert (config.git.sync, config.git.commit_captures) == (True, True)
    assert config.jobs.agent[:2] == ("claude", "-p")


def test_full_config() -> None:
    config = parse_config(
        {
            "name": "work",
            "access": {"read": ["wiki", "archive"], "search_captures": False},
            "secrets": {
                "policy": "block",
                "patterns": [{"id": "ticket", "regex": r"TCK-\d{6}", "description": "Ticket ids"}],
            },
            "git": {"sync": False, "commit_captures": False},
            "review": {"auto_merge": "none", "flag": ["wiki/*/my-*.md"]},
        }
    )
    assert config.access.read == ("wiki", "archive")
    assert config.secrets.policy == "block"
    assert config.secrets.patterns[0].id == "ticket"
    assert config.review.flag == ("wiki/*/my-*.md",)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "`name` is required"),
        ({"name": "x", "secrets": {"polcy": "allow"}}, "unknown key(s): polcy"),
        ({"name": "x", "secrets": {"policy": "sometimes"}}, "must be one of"),
        ({"name": "x", "secrets": {"pii": "never"}}, "must be one of: allow, warn, redact, block"),
        ({"name": "x", "git": {"sync": "yes"}}, "wrong type"),
        ({"name": "x", "timezone": "Mars/Olympus"}, "unknown time zone"),
        ({"name": "x", "files": {"images": "block"}}, "must be one of: shrink, ask"),
        ({"name": "x", "files": {"max_kb": 0}}, "files.max_kb must be at least 1"),
        ({"name": "x", "access": {"read": ["raw"]}}, "is not allowed"),
        ({"name": "x", "layout": {"wiki": "../elsewhere"}}, "relative path inside the vault"),
        ({"name": "x", "layout": {"captures": "inbox"}}, "inside layout.raw"),
        ({"name": "x", "secrets": {"patterns": [{"id": "bad", "regex": "("}]}}, "invalid regex"),
        ({"name": "x", "jobs": {"timeout_minutes": 0}}, "must be positive"),
        ({"name": "x", "extra": 1}, "unknown key(s): extra"),
    ],
)
def test_invalid_config(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_config(data)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="wikismith init"):
        load_config(tmp_path)


def test_load_config_reports_yaml_errors_on_one_line(tmp_path: Path) -> None:
    (tmp_path / "wikismith.yaml").write_text("name: x\ndescription: Notes: home and garden\n")
    with pytest.raises(ConfigError, match=r"invalid YAML on line 2: mapping values are not allowed here$"):
        load_config(tmp_path)


def test_load_config_from_file(vault_root: Path) -> None:
    assert load_config(vault_root).name == "test-vault"
