from __future__ import annotations

import re
from dataclasses import dataclass


REQUIRED_SECTIONS = (
    "goal",
    "context",
    "product decisions",
    "scope",
    "acceptance criteria",
    "validation",
    "change constraints",
)

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_PLACEHOLDER = re.compile(
    r"\b(?:TBD|TODO|FIXME|to be decided|fill (?:this|in)|describe here)\b",
    re.IGNORECASE,
)
_YES_NO_FIELDS = (
    "database migration allowed",
    "breaking API change allowed",
    "dependency changes allowed",
    "configuration changes allowed",
)
_FORMATTED_MARKER = "<!-- investory-orchestrator-formatted-issue:v1 -->"


@dataclass(frozen=True)
class IssueValidation:
    valid: bool
    errors: tuple[str, ...]


def validate_issue_contract(
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str] = (),
) -> IssueValidation:
    """Validate the executable issue contract without invoking an agent."""
    errors: list[str] = []
    if not title.strip():
        errors.append("Issue title is empty.")

    sections = _sections(body or "")
    for name in REQUIRED_SECTIONS:
        content = sections.get(name, "").strip()
        if not content:
            errors.append(f"Add a non-empty `## {name.title()}` section.")
        elif _PLACEHOLDER.search(content):
            errors.append(f"Replace placeholder text in `## {name.title()}`.")

    acceptance = sections.get("acceptance criteria", "")
    criteria = [
        line for line in acceptance.splitlines()
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", line)
    ]
    if acceptance.strip() and not criteria:
        errors.append("Add at least one testable bullet under `## Acceptance criteria`.")

    scope = sections.get("scope", "")
    if scope:
        scope_sections = _sections(scope)
        if not scope_sections.get("in scope", "").strip():
            errors.append("Add a non-empty `### In scope` list under `## Scope`.")
        if not scope_sections.get("out of scope", "").strip():
            errors.append("Add an explicit `### Out of scope` section under `## Scope`.")

    constraints = sections.get("change constraints", "")
    for field in _YES_NO_FIELDS:
        match = re.search(
            rf"(?im)^\s*(?:[-*+]\s*)?{re.escape(field)}\s*:\s*(yes|no)\b",
            constraints,
        )
        if not match:
            errors.append(
                f"State `{field}: yes` or `{field}: no` in `## Change Constraints`."
            )

    label_set = {label.strip().casefold() for label in labels}
    if label_set.intersection({"bug", "type: bug", "type/bug"}):
        current = sections.get("current behavior", "").strip()
        expected = sections.get("expected behavior", "").strip()
        reproduction = sections.get("reproduction", "").strip()
        deterministic_test = bool(
            re.search(r"(?i)deterministic.{0,30}test|test.{0,30}reproduc", body or "")
        )
        if not current:
            errors.append("Bug issues need a non-empty `## Current behavior` section.")
        if not expected:
            errors.append("Bug issues need a non-empty `## Expected behavior` section.")
        if not reproduction and not deterministic_test:
            errors.append(
                "Bug issues need `## Reproduction` steps or a deterministic failing test."
            )

    return IssueValidation(valid=not errors, errors=tuple(errors))


def format_issue_contract(
    title: str,
    body: str,
    labels: tuple[str, ...] | list[str] = (),
) -> str:
    """Wrap an unstructured request in the canonical contract, preserving it verbatim.

    Formatting supplies conservative workflow defaults, not new product
    behavior. The original description remains available as the source of
    truth so the planner can refine the implementation scope from it.
    """
    body = body or ""
    if _FORMATTED_MARKER in body:
        return body
    original = body.strip()
    if not title.strip():
        return body or ""
    if not original:
        original = "(No description was provided; use the issue title as the request.)"
    if validate_issue_contract(title, original, labels).valid:
        return original

    def permitted(keywords: tuple[str, ...]) -> str:
        folded = original.casefold()
        for keyword in keywords:
            escaped = re.escape(keyword)
            if re.search(rf"\b(?:no|not|without|avoid)\b.{{0,50}}\b{escaped}\b", folded):
                return "no"
            if re.search(rf"\b{escaped}\b", folded):
                return "yes"
        return "no"

    bug_sections = ""
    label_set = {label.strip().casefold() for label in labels}
    if label_set.intersection({"bug", "type: bug", "type/bug"}):
        bug_sections = """## Current behavior
Confirm the current behavior from the original request, repository, and tests.

## Expected behavior
Implement the behavior requested in the original issue description.

## Reproduction
Use original reproduction details when present and add a deterministic regression test.

"""

    formatted = f"""{_FORMATTED_MARKER}
<!-- The sections below are formatting defaults; the original request is preserved verbatim. -->

## Goal
{title.strip()}

## Context
The complete original request is preserved under **Original issue description** below.

## Product decisions
- Follow existing repository behavior and conventions.
- Preserve compatibility and avoid expanding the requested scope.

## Scope
### In scope
- Implement the behavior requested in the original issue description.

### Out of scope
- Unrelated behavior changes, dependencies, and refactoring.

## Acceptance criteria
- The behavior requested in the original issue description is implemented and covered by appropriate tests.

## Validation
- Run the relevant automated tests and configured CI checks.
- Keep existing relevant regression checks passing.

## Change constraints
- Database migration allowed: {permitted(('migration', 'schema', 'database', 'persistence', 'table'))}
- Breaking API change allowed: {permitted(('breaking API', 'breaking change'))}
- Dependency changes allowed: {permitted(('dependency', 'dependencies', 'new library'))}
- Configuration changes allowed: {permitted(('configuration change', 'config change'))}

{bug_sections}## Original issue description
{original}
"""
    if validate_issue_contract(title, formatted, labels).valid:
        return formatted
    return original


def _sections(markdown: str) -> dict[str, str]:
    """Return each heading's content through the next heading at its level."""
    lines = markdown.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            name = match.group(2).strip().rstrip("#").strip().casefold()
            headings.append((index, len(match.group(1)), name))

    result: dict[str, str] = {}
    for position, (start, level, name) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        result[name] = "\n".join(lines[start + 1:end]).strip()
    return result
