"""The PII scanner is a load-bearing control, so it gets real tests.

Fixtures below are synthetic strings that share the *shape* of the leaks in the
build this project replaces — a seed table of fills, a cash dictionary, a commit
subject quoting a balance — without reproducing anyone's actual figures.

This file is on the scanner's path allowlist; it necessarily contains the very
patterns the scanner looks for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from pii_scan import build_rules, scan_text

RULES = build_rules()


def rules_hit(text: str) -> set[str]:
    return {f.rule for f in scan_text(text, "fixture", RULES)}


class TestCatchesTheReferenceLeakShapes:
    def test_a_seed_transaction_table(self) -> None:
        leak = '    ("AAA", "TFSA", "Buy", 212, 45.0931),'
        assert "high-precision-float" in rules_hit(leak)
        assert "registered-account-type" in rules_hit(leak)

    def test_a_cash_balance_dictionary(self) -> None:
        assert "high-precision-float" in rules_hit('SEED_CASH = {"A": 228.0812}')

    def test_a_dollar_amount_in_a_comment(self) -> None:
        assert "currency-literal" in rules_hit("# confirmed: $6,500 contribution")

    def test_a_thousands_separated_number(self) -> None:
        assert "currency-literal" in rules_hit("loc_balance = 10,000")

    def test_a_birth_year_constant(self) -> None:
        assert "birth-year" in rules_hit("USER_BIRTH_YEAR = 2002")

    def test_a_custodian_name_in_a_comment(self) -> None:
        assert "custodian-name" in rules_hit("# matches the RBC portal")
        assert "custodian-name" in rules_hit("# NBIN cash balance")

    def test_an_email_address(self) -> None:
        assert "email-address" in rules_hit('AUTHOR = "someone@a-real-domain.ca"')

    def test_reserved_documentation_domains_are_exempt(self) -> None:
        """RFC 2606 reserves these so docs and tests can use them; an address
        there identifies nobody, and flagging it only trains people to ignore
        the scanner."""
        assert rules_hit('allowed = "person@example.com, other@example.org"') == set()
        assert rules_hit('allowed = "Person@Example.COM"') == set()
        assert rules_hit('allowed = "dev@myhost.test"') == set()

    def test_a_commit_subject_quoting_a_balance(self) -> None:
        subject = "Contributions: trued TFSA to $10k after this morning's trades"
        hits = rules_hit(subject)
        assert "currency-literal" in hits
        assert "registered-account-type" in hits

    def test_a_private_holding_mark(self) -> None:
        assert "high-precision-float" in rules_hit("manual_price=17.1840")


class TestDenylist:
    def test_a_denylisted_term_is_caught(self) -> None:
        rules = build_rules(("Ada Lovelace", "alovelace"))
        assert "denylist-term" in {f.rule for f in scan_text("author = 'Ada Lovelace'", "f", rules)}

    def test_the_denylist_is_case_insensitive(self) -> None:
        rules = build_rules(("Lovelace",))
        assert scan_text("# LOVELACE portfolio", "f", rules)

    def test_absent_denylist_still_scans(self) -> None:
        assert "currency-literal" in {f.rule for f in scan_text("cost $500", "f", build_rules(()))}


class TestDoesNotFireOnOrdinaryCode:
    @pytest.mark.parametrize(
        "line",
        [
            "def compute_positions(entries): ...",
            "TOLERANCE = 1e-9",
            "weight: float = 0.35",
            "band_pp = 5.0",
            "assert value == pytest.approx(1200.0)",
            "version = 2.0",
            "confidence: 0.35",
            "for i in range(1000):",
            "timeout_seconds = 900",
        ],
    )
    def test_clean_line(self, line: str) -> None:
        assert rules_hit(line) == set(), f"false positive on: {line}"


class TestEscapeHatch:
    def test_an_annotated_line_is_skipped(self) -> None:
        assert rules_hit("TFSA_LIMIT = 7000  # pii-ok: published CRA limit") == set()

    def test_the_escape_needs_a_reason(self) -> None:
        # Bare '# pii-ok' with no reason does not suppress: the reason is the
        # thing a reviewer reads.
        assert rules_hit("balance = $500  # pii-ok") != set()


class TestRedaction:
    def test_the_value_is_masked_in_the_report(self) -> None:
        """The report goes to a CI log, which is itself a place data can leak."""
        (finding,) = scan_text("price = 45.0931", "f", RULES)
        assert "45.0931" not in finding.excerpt
        assert "[4" in finding.excerpt


class TestPerRulePathExemptions:
    """Exemptions are per rule, not per file.

    The module implementing registered-account tax rules must be able to name
    those account types. It should still be scanned for balances, fill prices
    and custodian names — a whole-file allowlist would switch all of that off
    at once.
    """

    def test_account_types_are_allowed_in_the_jurisdiction_module(self) -> None:
        line = "# TFSA room accrues from the year you turn 18"
        assert scan_text(line, "src/desk/jurisdictions/ca.py", RULES) == []

    def test_the_same_line_is_flagged_anywhere_else(self) -> None:
        line = "# TFSA room accrues from the year you turn 18"
        assert scan_text(line, "src/desk/app/main.py", RULES) != []

    def test_other_rules_still_apply_inside_the_exempt_path(self) -> None:
        where = "src/desk/jurisdictions/ca.py"
        assert {f.rule for f in scan_text("price = 45.0931", where, RULES)} == {
            "high-precision-float"
        }
        assert {f.rule for f in scan_text("# matches the RBC portal", where, RULES)} == {
            "custodian-name"
        }
