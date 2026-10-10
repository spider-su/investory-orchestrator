from __future__ import annotations

import unittest

from app.issue_validation import format_issue_contract, validate_issue_contract


VALID_ISSUE = """## Goal
Return the requested result to the user.

## Context
The current screen does not show the result.

## Product decisions
- Intended user behavior: Show the result on the task page.
- Compatibility expectations: Keep the existing API response compatible.
- Business or UX rules: Preserve the current account selection.

## Scope
### In scope
- Add the result to the task page.
### Out of scope
- Change account selection.

## Acceptance criteria
- The task page shows the result.

## Validation
- Expected automated tests: Add a focused dashboard test.
- Required manual checks: Inspect the task page.
- Existing validation that must remain passing: Run the unit suite.

## Change constraints
- Database migration allowed: no
- Breaking API change allowed: no
- Dependency changes allowed: no
- Configuration changes allowed: no
"""


class IssueValidationTests(unittest.TestCase):
    def test_accepts_complete_ready_issue(self) -> None:
        result = validate_issue_contract("Show the task result", VALID_ISSUE)

        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())

    def test_rejects_missing_acceptance_criteria_and_permissions(self) -> None:
        body = VALID_ISSUE.replace(
            "- The task page shows the result.\n", ""
        ).replace("- Dependency changes allowed: no\n", "")

        result = validate_issue_contract("Show the task result", body)

        self.assertFalse(result.valid)
        self.assertTrue(any("Acceptance Criteria" in item for item in result.errors))
        self.assertTrue(any("dependency changes allowed" in item for item in result.errors))

    def test_rejects_bug_without_reproduction_or_deterministic_test(self) -> None:
        body = VALID_ISSUE + "\n## Current behavior\nIt fails.\n\n## Expected behavior\nIt works.\n"

        result = validate_issue_contract("Fix task result", body, ["bug"])

        self.assertFalse(result.valid)
        self.assertTrue(any("Reproduction" in item for item in result.errors))

    def test_accepts_bug_with_reproduction(self) -> None:
        body = (
            VALID_ISSUE
            + "\n## Reproduction\n1. Open the task page.\n"
            + "\n## Current behavior\nThe result is hidden.\n"
            + "\n## Expected behavior\nThe result is visible.\n"
        )

        result = validate_issue_contract("Fix task result", body, ["bug"])

        self.assertTrue(result.valid, result.errors)

    def test_formats_unstructured_request_and_preserves_original_text(self) -> None:
        original = "Please add a compact VIX panel to Market Radar."
        formatted = format_issue_contract("Add VIX context", original)

        self.assertNotEqual(formatted, original)
        self.assertIn("## Goal", formatted)
        self.assertIn("## Acceptance criteria", formatted)
        self.assertIn("## Original issue description\n" + original, formatted)
        self.assertTrue(validate_issue_contract("Add VIX context", formatted).valid)

    def test_formatter_is_idempotent_and_uses_title_for_blank_description(self) -> None:
        formatted = format_issue_contract("Add VIX context", "Add a VIX panel.")
        self.assertEqual(format_issue_contract("Add VIX context", formatted), formatted)
        blank_body = format_issue_contract("Add VIX context", "")
        self.assertIn("use the issue title as the request", blank_body)
        self.assertTrue(validate_issue_contract("Add VIX context", blank_body).valid)
        self.assertEqual(format_issue_contract("", ""), "")

    def test_formats_malformed_bug_issue_without_rejecting_it(self) -> None:
        original = "The radar page crashes when opened."
        formatted = format_issue_contract("Fix radar crash", original, ["bug"])

        result = validate_issue_contract("Fix radar crash", formatted, ["bug"])
        self.assertTrue(result.valid, result.errors)
        self.assertIn("## Reproduction", formatted)


if __name__ == "__main__":
    unittest.main()
