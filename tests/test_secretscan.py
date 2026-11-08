from pathlib import Path

import pytest
from mcp.client import Client

from conftest import make_vault, write_files
from wikismith.config import SecretPattern
from wikismith.reader import Reader
from wikismith.secretscan import Scanner, SecretsBlocked, apply_policy
from wikismith.server import build_server

# Fake values in the shape of real ones. Built by concatenation so scanners of this repo (gitleaks in CI) don't flag the
# test file itself.
AWS = "AKIA" + "QWERTYUIOPASDFGH"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
ANTHROPIC = "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz012345"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = "-----BEGIN RSA PRIVATE" + " KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f"key {AWS} here", "aws-key"),
        (f"token={GITHUB}", "github-token"),
        (f"export KEY={ANTHROPIC}", "anthropic-key"),
        (f"Bearer {JWT}", "jwt"),
        (PEM, "private-key"),
        ("https://admin:s3cretPass@nas.local/api", "url-credentials"),
        ("password: correct-horse-battery", "password-assignment"),
        ("API_KEY = 'abcdef123456'", "password-assignment"),  # gitleaks:allow (a made-up test value)
        ("The router admin password is hunter2-not-real.", "password-sentence"),
        ("IBAN DE89 3704 0044 0532 0130 00", "iban"),
        ("card 4111 1111 1111 1111 exp 12/30", "card-number"),
        ("SSN 123-45-6789", "us-ssn"),
    ],
)
def test_detects(text: str, rule: str) -> None:
    assert [f.rule for f in Scanner().scan(text)] == [rule]


@pytest.mark.parametrize(
    "text",
    [
        "The password policy requires 12 characters.",
        "Reset the password in the app.",
        "Order 1234 5678 9012 3456 shipped.",  # fails the Luhn check
        "Phone 555-123-4567, version 1.2.3, 2026-01-06",
        "Three raised beds, 4 x 8 ft each.",
        "sk-learn is a library",
        "secret: no",  # too short to be a value
        "No registration or password is required to access it.",
        "just IP, username, and password is needed for most setups",
        'password = "***REDACTED-API-TOKEN***"',
        'password = "your_api_token"',
        '"text": "password = \\"your_api_token\\"\\n"',  # escaped, as in a JSON export
        '"entry_uuid": "123456789012345671"',  # passes Luhn, but no card prefix and length
        "api_key: ${API_KEY}",
        "https://example.com/products/x?variant=41234567891234 is a bag",
        "https://example.com/formulare/fs.php?id=1234-5678-9012-3456",
    ],
)
def test_ignores_ordinary_text(text: str) -> None:
    assert Scanner().scan(text) == []


def test_redact_keeps_labels() -> None:
    out = Scanner().redact("password: correct-horse-battery\nalso " + AWS)
    assert out == "password: ***REDACTED-PASSWORD-ASSIGNMENT***\nalso ***REDACTED-AWS-KEY***"


def test_custom_patterns() -> None:
    scanner = Scanner((SecretPattern("my-hostname", r"\bmyhome-[a-z0-9]+\.example\.org\b", "Home hostname"),))
    findings = scanner.scan("Line one\nreach it at myhome-123.example.org")
    assert [(f.rule, f.line) for f in findings] == [("my-hostname", 2)]


@pytest.mark.parametrize(
    ("policy", "stored", "warned"),
    [("allow", "pw is", False), ("warn", "hunter2-not-real", True), ("redact", "***REDACTED-", True)],
)
def test_policies(policy: str, stored: str, warned: bool) -> None:
    result = apply_policy(Scanner(), "The pw is fine. password is hunter2-not-real", policy)  # type: ignore[arg-type]
    assert stored in result.text
    assert (result.warning is not None) is warned


PARCEL = SecretPattern("parcel-id", r"\bPCL-\d{4}-\d{6}\b", "Parcel ID", pii=True)
NOTE = "Neighbor's lot is PCL-2026-000123. password is hunter2-not-real"


@pytest.mark.parametrize(
    ("pii", "parcel_stored", "warning"),
    [
        ("allow", True, "Redacted before saving: password in a sentence."),
        (
            "warn",
            True,
            "Redacted before saving: password in a sentence. Saved, but it contains what looks like "
            "secrets or personal data: Parcel ID.",
        ),
        ("redact", False, "Redacted before saving: Parcel ID, password in a sentence."),
    ],
)
def test_pii_follows_its_own_policy(pii: str, parcel_stored: bool, warning: str) -> None:
    scanner = Scanner((PARCEL,), pii)  # type: ignore[arg-type]
    result = apply_policy(scanner, NOTE, "redact")
    assert ("PCL-2026-000123" in result.text) is parcel_stored
    assert "hunter2" not in result.text
    assert result.warning == warning


def test_pii_allow_isnt_scanned_or_masked() -> None:
    scanner = Scanner((PARCEL,), "allow")
    assert [f.rule for f in scanner.scan(NOTE)] == ["password-sentence"]
    assert "PCL-2026-000123" in scanner.redact(NOTE)
    assert "PCL-2026-000123" not in Scanner((PARCEL,), "warn").redact(NOTE)  # masked in reads unless allowed


def test_each_block_policy_blocks_only_its_own_kind() -> None:
    result = apply_policy(Scanner((PARCEL,)), "Lot PCL-2026-000123.", "block")  # secrets: block, PII: redact
    assert result.text == "Lot ***REDACTED-PARCEL-ID***."
    strict = Scanner((PARCEL,), "block")
    with pytest.raises(SecretsBlocked, match=r"contains personal data \(Parcel ID\) on line\(s\) 1\."):
        apply_policy(strict, "Lot PCL-2026-000123.", "redact")
    assert "***REDACTED-" in apply_policy(strict, "password is hunter2-not-real", "redact").text
    with pytest.raises(SecretsBlocked, match="contains secrets and personal data"):
        apply_policy(strict, NOTE, "block")


def test_block_policy() -> None:
    with pytest.raises(SecretsBlocked, match=r"password in a sentence\) on line\(s\) 2"):
        apply_policy(Scanner(), "fine\npassword is hunter2-not-real", "block")
    assert apply_policy(Scanner(), "nothing here", "block").warning is None


def test_reads_are_masked(vault_root: Path) -> None:
    write_files(
        vault_root,
        {"wiki/garden/shed.md": "# Shed\n\nLock code: shed-lock. password: correct-horse-battery\n"},
    )
    vault = make_vault(vault_root)
    reader = Reader(vault, mask=Scanner().redact)
    assert "correct-horse" not in reader.read_page("shed")
    assert "correct-horse" not in reader.search("shed lock password")


@pytest.mark.anyio
@pytest.mark.parametrize(("policy", "has_reveal"), [("allow", True), ("redact", False), ("block", False)])
async def test_reveal_only_with_allow(vault_root: Path, policy: str, has_reveal: bool) -> None:
    write_files(vault_root, {"wiki/garden/shed.md": "# Shed\n\npassword: correct-horse-battery\n"})
    vault = make_vault(vault_root, secrets={"policy": policy}, search={"semantic": False})
    async with Client(build_server(vault, warm=False), mode="legacy") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        masked = await client.call_tool("read_page", {"page": "shed"})
        assert "correct-horse" not in masked.content[0].text  # type: ignore[union-attr]
        assert ("reveal" in names) is has_reveal
        if has_reveal:
            shown = await client.call_tool("reveal", {"page": "shed"})
            assert "correct-horse-battery" in shown.content[0].text  # type: ignore[union-attr]


def test_placeholders_for_the_index() -> None:
    scanner = Scanner((PARCEL,))
    assert scanner.placeholder(NOTE) == (
        "Neighbor's lot is [personal data: Parcel ID]. password is [secret: password in a sentence]"
    )


def test_masking_twice_changes_nothing() -> None:
    scanner = Scanner()
    once = scanner.placeholder("password: hunter2-not-real")
    assert scanner.placeholder(once) == once and scanner.redact(once) == once
    redacted = scanner.redact("password: hunter2-not-real")
    assert scanner.redact(redacted) == redacted
