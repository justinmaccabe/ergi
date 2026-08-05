#!/usr/bin/env python3
"""Refuse to let portfolio data into git.

Runs in three places, all from this one file so they cannot drift:

    pre-commit    staged diff        convenience, bypassable with --no-verify
    commit-msg    the message        catches the leak a scrub commit cannot undo
    CI            tree + every log   enforcement

The failure this exists to prevent is specific. The build this project replaces
kept its owner's ledger as Python constants — exact share counts, cost bases to
six decimals, cash to the cent — and put dollar figures in about ten commit
subjects. `.gitignore` excluded the database file and made no difference,
because the data was never in the database file.

Governing assumption: **the repository is public.** Private repos become public
via one misclick, one org transfer, one collaborator added during a job hunt.
Repo visibility is not a control.

Usage:
    python tools/pii_scan.py --staged
    python tools/pii_scan.py --tree
    python tools/pii_scan.py --commit-msg .git/COMMIT_EDITMSG
    python tools/pii_scan.py --log origin/main..HEAD
    python tools/pii_scan.py --all
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Paths whose whole purpose is to hold public reference numbers or to describe
# the rules themselves.
PATH_ALLOWLIST = (
    "data/jurisdictions/",  # published contribution limits are public facts
    "tools/pii_scan.py",  # this file necessarily contains the patterns
    "tests/test_pii_scan.py",
    ".pii-denylist.example",
)

SCAN_SUFFIXES = {
    ".py",
    ".pyi",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".cfg",
    ".ini",
    ".sql",
    ".sh",
    ".j2",
    ".html",
    ".css",
    ".js",
}

INLINE_ESCAPE = re.compile(r"#\s*pii-ok:\s*(?P<reason>.+)")


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    explain: str
    # Paths where this specific rule does not apply. Deliberately per-rule
    # rather than per-file: the module implementing registered-account tax
    # rules must be able to name those account types, but it should still be
    # scanned for balances, fill prices and custodian names like anything else.
    # A whole-file allowlist would switch all of that off at once.
    exempt_paths: tuple[str, ...] = ()

    def applies_to(self, path: str) -> bool:
        return not any(path.startswith(prefix) for prefix in self.exempt_paths)


def build_rules(denylist: tuple[str, ...] = ()) -> tuple[Rule, ...]:
    rules = [
        Rule(
            "currency-literal",
            re.compile(r"\$\s?\d|(?<![\d.])\d{1,3}(?:,\d{3})+(?![\d.])"),
            "a dollar amount — balances and contributions do not belong in source or messages",
        ),
        Rule(
            # The single highest-signal rule. Legitimate code almost never
            # carries four decimal places; a fill price always does. This is
            # what catches `44.4813` and `633.634029280884`.
            "high-precision-float",
            re.compile(r"(?<![\d.])\d+\.\d{4,}(?![\d])"),
            "a high-precision number — this is the shape of a fill price or a share count",
        ),
        Rule(
            "registered-account-type",
            re.compile(r"\b(?:TFSA|FHSA|RRSP|RESP|LIRA|RRIF)\b"),
            "a registered account type outside the account-type enum",
            exempt_paths=(
                # The module whose entire job is these accounts' tax rules, and
                # its tests. Every other rule still applies to both.
                "src/desk/jurisdictions/",
                "tests/test_jurisdictions.py",
            ),
        ),
        Rule(
            "custodian-name",
            re.compile(
                r"\b(?:RBC|BMO|CIBC|Scotiabank|Questrade|Wealthsimple|"
                r"Interactive\s?Brokers|IBKR|NBIN|National\s?Bank)\b",
                re.IGNORECASE,
            ),
            "a broker or custodian name — naming where you hold assets is a disclosure",
        ),
        Rule(
            "ticker-with-quantity",
            re.compile(r"\b[A-Z]{2,5}\b[^\n]{0,24}?(?<![\d.])\d{2,}\.\d{2,}"),
            "a ticker next to a precise quantity or price — this is the shape of a seed table",
        ),
        Rule(
            "birth-year",
            re.compile(r"(?:birth|dob|born|age)\D{0,12}(?:19|20)\d{2}", re.IGNORECASE),
            "a birth year — this belongs in gitignored config, not in source",
            exempt_paths=(
                # Room accrual is defined in terms of the year the holder turns
                # 18, so these tests have to supply a birth year. The target of
                # this rule is a constant in production source, not a fixture.
                "src/desk/jurisdictions/",
                "tests/test_jurisdictions.py",
            ),
        ),
        Rule(
            # RFC 2606 reserves example.com/net/org and .test/.invalid precisely
            # so documentation can use them. An address there identifies nobody.
            "email-address",
            re.compile(
                r"\b[\w.+-]+@(?!example\.(?:com|net|org)\b|[\w-]+\.(?:test|invalid|localhost)\b)"
                r"[\w-]+\.[\w.-]+\b",
                re.IGNORECASE,
            ),
            "an email address",
        ),
    ]
    if denylist:
        rules.append(
            Rule(
                "denylist-term",
                re.compile("|".join(re.escape(t) for t in denylist), re.IGNORECASE),
                "a term from your private denylist (your name, handle, employer, or accounts)",
            )
        )
    return tuple(rules)


def load_denylist() -> tuple[str, ...]:
    """Read the private denylist.

    The list is itself sensitive — it is your name, handle and employer — so it
    is gitignored and supplied to CI from a repository secret. Its absence
    weakens the scan but never blocks it.
    """
    path = ROOT / ".pii-denylist"
    if not path.is_file():
        return ()
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return tuple(terms)


@dataclass(frozen=True)
class Finding:
    where: str
    line_no: int
    rule: str
    explain: str
    excerpt: str


def _redact(text: str, match: re.Match[str]) -> str:
    """Show enough of the hit to locate it without reprinting the value into a
    CI log, which is itself a place data can leak."""
    start, end = match.span()
    hit = match.group(0)
    masked = hit[0] + "*" * max(0, len(hit) - 2) + hit[-1] if len(hit) > 2 else "**"
    line = text[max(0, start - 30) : start] + f"[{masked}]" + text[end : end + 30]
    return line.strip()


def scan_text(text: str, where: str, rules: tuple[Rule, ...]) -> list[Finding]:
    findings: list[Finding] = []
    active = tuple(r for r in rules if r.applies_to(where))
    for i, line in enumerate(text.splitlines(), start=1):
        if INLINE_ESCAPE.search(line):
            continue
        for rule in active:
            match = rule.pattern.search(line)
            if match:
                findings.append(Finding(where, i, rule.name, rule.explain, _redact(line, match)))
    return findings


def is_allowlisted(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PATH_ALLOWLIST)


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def scan_paths(paths: list[Path], rules: tuple[Rule, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix() if path.is_absolute() else path.as_posix()
        if is_allowlisted(rel) or path.suffix not in SCAN_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(text, rel, rules))
    return findings


def scan_tree(rules: tuple[Rule, ...]) -> list[Finding]:
    """Scan tracked files, plus untracked files git would be willing to add.

    Including the untracked-but-not-ignored set matters: a file you have just
    written is not in the index yet, so a tracked-only scan would report clean
    right up until the moment you stage the thing it should have caught.
    """
    tracked = [line for line in _git("ls-files").splitlines() if line]
    untracked = [
        line for line in _git("ls-files", "--others", "--exclude-standard").splitlines() if line
    ]
    names = sorted(set(tracked) | set(untracked))
    paths = [ROOT / p for p in names] or [
        p for p in ROOT.rglob("*") if ".venv" not in p.parts and ".git" not in p.parts
    ]
    return scan_paths(paths, rules)


def scan_staged(rules: tuple[Rule, ...]) -> list[Finding]:
    staged = [line for line in _git("diff", "--cached", "--name-only").splitlines() if line]
    return scan_paths([ROOT / p for p in staged], rules)


def scan_commit_messages(revspec: str, rules: tuple[Rule, ...]) -> list[Finding]:
    """Scan commit subjects and bodies.

    This is the leak a later scrub commit cannot retract: a message is published
    the moment anyone fetches, and rewriting history does not reach forks,
    mirrors, or anyone's existing clone.
    """
    log = _git("log", "--format=%H%x00%B%x1e", revspec)
    findings: list[Finding] = []
    for record in log.split("\x1e"):
        if "\x00" not in record:
            continue
        sha, message = record.split("\x00", 1)
        findings.extend(scan_text(message, f"commit {sha[:10]}", rules))
    return findings


def report(findings: list[Finding], *, context: str) -> int:
    if not findings:
        print(f"pii-scan: {context} clean")
        return 0
    print(f"pii-scan: {len(findings)} finding(s) in {context}\n", file=sys.stderr)
    for f in findings:
        print(f"  {f.where}:{f.line_no}  [{f.rule}]", file=sys.stderr)
        print(f"      {f.explain}", file=sys.stderr)
        print(f"      {f.excerpt}\n", file=sys.stderr)
    print(
        "Portfolio data belongs in the database, not in the repository.\n"
        "If a hit is genuinely a false positive, append '# pii-ok: <reason>' to the line — "
        "these are counted and reviewed, never silent.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan for portfolio data and personal identifiers."
    )
    parser.add_argument("--staged", action="store_true", help="scan staged files")
    parser.add_argument("--tree", action="store_true", help="scan all tracked files")
    parser.add_argument("--commit-msg", metavar="FILE", help="scan a commit message file")
    parser.add_argument("--log", metavar="REVSPEC", help="scan commit messages in a range")
    parser.add_argument("--all", action="store_true", help="scan the tree and the whole log")
    args = parser.parse_args(argv)

    rules = build_rules(load_denylist())
    findings: list[Finding] = []
    contexts: list[str] = []

    if args.commit_msg:
        path = Path(args.commit_msg)
        findings += scan_text(path.read_text(encoding="utf-8"), "commit message", rules)
        contexts.append("commit message")
    if args.staged:
        findings += scan_staged(rules)
        contexts.append("staged files")
    if args.tree or args.all:
        findings += scan_tree(rules)
        contexts.append("tracked files")
    if args.log or args.all:
        findings += scan_commit_messages(args.log or "--all", rules)
        contexts.append("commit messages")

    if not contexts:
        parser.error("choose at least one of --staged, --tree, --commit-msg, --log, --all")
    return report(findings, context=" and ".join(contexts))


if __name__ == "__main__":
    raise SystemExit(main())
