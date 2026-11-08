"""Secret scanning, redaction and the per-vault secret policy.

Built-in patterns aim for precision: a scanner that cries wolf gets switched off. Each vault adds its own patterns
(hostnames, ID formats) in `wikismith.yaml`. A pattern with a named group `secret` redacts only that group, so
`password: hunter2` keeps its label. Patterns marked `pii` find personal data and follow `secrets.pii` instead of
`secrets.policy`; under `allow` they're not scanned for at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from wikismith.config import Policy, SecretPattern, Secrets


@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern[str]
    description: str
    validate: Callable[[str], bool] | None = None
    pii: bool = False


@dataclass(frozen=True)
class Finding:
    rule: str
    description: str
    start: int
    end: int
    line: int
    pii: bool = False


class SecretsBlocked(ValueError):
    def __init__(self, findings: list[Finding]) -> None:
        kinds = ", ".join(sorted({f.description for f in findings}))
        lines = ", ".join(str(f.line) for f in sorted(findings, key=lambda f: f.line))
        what = {(True, False): "secrets", (False, True): "personal data"}.get(
            (any(not f.pii for f in findings), any(f.pii for f in findings)), "secrets and personal data"
        )
        super().__init__(f"Refused: the text contains {what} ({kinds}) on line(s) {lines}. Remove them.")
        self.findings = findings


_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|6011|65)")


def _card(number: str) -> bool:
    """A real card prefix and length, and a valid Luhn checksum. Random IDs rarely pass all three."""
    plain = re.sub(r"\D", "", number)
    if not _CARD_PREFIX.match(plain) or len(plain) not in (15, 16, 19):
        return False
    digits = [int(d) for d in plain][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


_VALUE = r"(?P<secret>[^\s\\\"'`,;]{6,})"
_PLACEHOLDER = re.compile(
    r"(?i)REDACTED|^\[(?:secret|personal data):|^(?:your|my|example|changeme|xxx|<|\$\{|\$[A-Z_])"
)


def _password_like(value: str) -> bool:
    """A real password has a digit or symbol; "required" or `your_password_here` don't count."""
    return not _PLACEHOLDER.search(value) and any(not c.isalpha() for c in value)


BUILTIN_RULES: tuple[Rule, ...] = (
    Rule(
        "private-key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "private key",
    ),
    Rule("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key"),
    Rule(
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
        "GitHub token",
    ),
    Rule("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    Rule("openai-key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{32,}"), "OpenAI API key"),
    Rule("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "Slack token"),
    Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    Rule("stripe-key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{24,}"), "Stripe key"),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    Rule(
        "url-credentials",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:(?P<secret>[^\s/@]+)@"),
        "password in a URL",
    ),
    Rule(
        "password-assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|passphrase|passcode|secret|api[_ -]?key"
            r"|access[_ -]?token|auth[_ -]?token|client[_ -]?secret)\b\s*[:=]\s*\\?[\"'`]?" + _VALUE
        ),
        "password or key assignment",
        _password_like,
    ),
    Rule(
        "password-sentence",
        re.compile(r"(?i)\b(?:password|passcode|passphrase|pin)\s+(?:is|was)\s*:?\s+\\?[\"'`]?" + _VALUE),
        "password in a sentence",
        _password_like,
    ),
    Rule("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), "IBAN"),
    Rule("card-number", re.compile(r"(?<![=/.\w-])(?:\d[ -]?){12,18}\d\b"), "payment card number", _card),
    Rule(
        "us-ssn",
        re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
        "US Social Security number",
    ),
)


class Scanner:
    def __init__(self, extra: tuple[SecretPattern, ...] = (), pii: Policy = "redact") -> None:
        self.pii = pii
        custom = tuple(
            Rule(p.id, re.compile(p.regex), p.description or p.id, pii=p.pii)
            for p in extra
            if not (p.pii and pii == "allow")
        )
        self.rules = BUILTIN_RULES + custom

    @classmethod
    def for_config(cls, secrets: Secrets) -> Scanner:
        return cls(secrets.patterns, secrets.pii)

    def scan(self, text: str) -> list[Finding]:
        found: list[Finding] = []
        for rule in self.rules:
            for m in rule.regex.finditer(text):
                group = "secret" if "secret" in rule.regex.groupindex else 0
                if rule.validate and not rule.validate(m.group(group)):
                    continue
                start, end = m.span(group)
                line = text.count("\n", 0, start) + 1
                found.append(Finding(rule.id, rule.description, start, end, line, rule.pii))
        return _drop_overlaps(found)

    def placeholder(self, text: str) -> str:
        """Mask for the search index: a readable placeholder (`[secret: GitHub token]`), which embeddings understand
        and which doesn't make unrelated chunks look alike the way identical `***REDACTED***` strings would."""
        for f in sorted(self.scan(text), key=lambda f: f.start, reverse=True):
            label = "personal data" if f.pii else "secret"
            text = text[: f.start] + f"[{label}: {f.description}]" + text[f.end :]
        return text

    def redact(self, text: str, findings: list[Finding] | None = None) -> str:
        """Mask the findings (by default all of them) with a label naming the rule."""
        findings = self.scan(text) if findings is None else findings
        for f in sorted(findings, key=lambda f: f.start, reverse=True):
            text = text[: f.start] + f"***REDACTED-{f.rule.upper()}***" + text[f.end :]
        return text


@dataclass(frozen=True)
class PolicyResult:
    text: str
    findings: tuple[Finding, ...]
    warning: str | None


def apply_policy(scanner: Scanner, text: str, policy: Policy) -> PolicyResult:
    """What to store for a new piece of content: secrets under the vault's secret policy, personal data under its PII
    policy (the scanner's)."""
    findings = scanner.scan(text)
    secrets = [] if policy == "allow" else [f for f in findings if not f.pii]
    personal = [f for f in findings if f.pii]
    blocked = (secrets if policy == "block" else []) + (personal if scanner.pii == "block" else [])
    if blocked:
        raise SecretsBlocked(blocked)
    redact = (secrets if policy == "redact" else []) + (personal if scanner.pii == "redact" else [])
    warn = (secrets if policy == "warn" else []) + (personal if scanner.pii == "warn" else [])
    notes = []
    if redact:
        notes.append(f"Redacted before saving: {_kinds(redact)}.")
    if warn:
        notes.append(f"Saved, but it contains what looks like secrets or personal data: {_kinds(warn)}.")
    return PolicyResult(scanner.redact(text, redact), tuple(findings), " ".join(notes) or None)


def _kinds(findings: list[Finding]) -> str:
    return ", ".join(sorted({f.description for f in findings}))


def _drop_overlaps(findings: list[Finding]) -> list[Finding]:
    """Keep the earliest, then longest, match where matches overlap."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda f: (f.start, -(f.end - f.start))):
        if kept and f.start < kept[-1].end:
            continue
        kept.append(f)
    return kept
