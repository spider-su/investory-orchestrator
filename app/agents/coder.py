from __future__ import annotations

import os
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.codex_cli import codex_environment


class CoderError(RuntimeError):
    pass


class CoderReport(BaseModel):
    status: Literal["completed", "needs_human_input"]
    summary: str
    changes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete files, commands, and observations supporting the work "
            "in the current step."
        ),
    )
    tests_run: list[str] = Field(default_factory=list, alias="testsRun")
    remaining_problems: list[str] = Field(
        default_factory=list,
        alias="remainingProblems",
    )
    needs_human_input: bool = Field(alias="needsHumanInput")


def _coder_environment() -> dict[str, str]:
    return codex_environment()


def coder_identity() -> dict[str, str]:
    return {
        "backend": "codex-cli",
        "provider": "codex-cli",
        "model": os.getenv("CODER_MODEL", ""),
    }


def _git_diff(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--", "."],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        return (
            "Unable to read git diff.\n"
            f"stderr:\n{result.stderr}"
        )

    return result.stdout


def _read_failed_patch(path: str, *, limit: int = 20_000) -> str:
    if not path:
        return "No previous failed attempt patch."

    patch_path = Path(path)

    try:
        patch = patch_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return f"Unable to read failed patch at {path}: {error}"

    if len(patch) <= limit:
        return patch or "Previous failed attempt produced an empty patch."

    return patch[:limit] + "\n... <failed patch truncated>"


def run_coder(
    *,
    workspace: Path,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    step: dict,
    completed_step_results: list[dict] | None = None,
    validation_output: str,
    review_feedback: dict,
    attempt: int,
    max_attempts: int,
    failed_patch_path: str,
    workspace_audit: dict | None = None,
) -> CoderReport:
    task_reference = (
        f"task {abs(issue_number)}"
        if issue_number < 0
        else f"GitHub issue #{issue_number}"
    )
    prompt = f"""
Implement {task_reference} in the current repository.

Title:
{issue_title}

Description:
{issue_body or "No issue body was provided."}

Current implementation step:
{step}

Results from completed implementation steps (these are part of the approved
workflow context; use them instead of asking the user to repeat findings):
{json.dumps(completed_step_results or [], indent=2, sort_keys=True)}

Orchestrator-captured initial workspace audit (captured before planning or
agent edits; authoritative for initial branch, commit, and tracked/untracked
status):
{json.dumps(workspace_audit or {}, indent=2, sort_keys=True)}

Attempt:
{attempt} of {max_attempts}

Previous validation output:
{validation_output or "No previous validation failure."}

Reviewer feedback:
{review_feedback or "None"}

Current git diff:
{_git_diff(workspace) or "No uncommitted changes."}

Previous failed attempt patch (diagnostic context only):
{_read_failed_patch(failed_patch_path)}

Do not reapply the failed patch blindly. Produce a fresh candidate from the clean step baseline.

Instructions:
- Implement exactly the current implementation step above. Do not implement
  requirements assigned to later steps, even when they are visible in the
  issue or plan.
- If the current step is inspection or inventory only, make no workspace
  changes and report concrete evidence in `evidence`.
- Treat accepted results from completed steps as available evidence and build
  on them. Do not ask the user to provide findings, files, or evidence already
  present in the issue, approved plan, completed-step results, or repository.
- A step may be completed without code changes when its requirements are
  already satisfied or prior evidence shows there is no safe, in-scope change
  to make. Record that conclusion and preserve uncertain items instead of
  requesting input or inventing work.
- Before requesting human input, inspect the available issue, plan, completed
  step results, repository, tests, and relevant documentation. Ask only when a
  material product decision is genuinely missing and no safe in-scope action
  or no-op can satisfy the step.
- Inspect AGENTS.md and repository documentation before editing.
- Implement only this issue.
- Make the smallest correct change.
- Add or update tests when required.
- Do not weaken, skip, or delete tests.
- Do not modify unrelated files.
- Do not access or print secrets.
- Leave all edits in the current workspace.
- Do not commit, push, or create a pull request.
- The orchestrator runs the complete validation suite separately.

Return a JSON object matching this contract:
{{
  "status": "completed" or "needs_human_input",
  "summary": "concise implementation summary",
  "changes": ["files or behavior changed"],
  "evidence": ["files inspected, commands run, and observations for this step"],
  "testsRun": ["commands actually run by the coder, or an empty list"],
  "remainingProblems": ["unresolved implementation problems"],
  "needsHumanInput": false
}}
Use status `needs_human_input` and set `needsHumanInput` true only when a
product decision or missing information prevents a safe implementation.
Do not claim tests were run unless you ran them.
""".strip()

    with tempfile.TemporaryDirectory(prefix="investory-coder-") as temp_dir:
        schema_path = Path(temp_dir) / "coder-report.schema.json"
        output_path = Path(temp_dir) / "coder-report.json"
        schema = CoderReport.model_json_schema(by_alias=True)
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"])
        schema_path.write_text(
            json.dumps(schema),
            encoding="utf-8",
        )
        command = [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        model = os.getenv("CODER_MODEL")
        if model:
            command.extend(["--model", model])
        command.append("-")

        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                env=_coder_environment(),
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            raise CoderError(
                f"Coder timed out after 1800 seconds.\n{output}"
            ) from error
        except OSError as error:
            raise CoderError(
                f"Could not start Codex CLI: {error}"
            ) from error

        output = result.stdout or "(Codex produced no output)"
        if result.returncode != 0:
            raise CoderError(
                f"Codex failed with exit code {result.returncode}:\n{output}"
            )
        if not output_path.is_file():
            raise CoderError("Codex did not write its structured coder report.")
        try:
            report = CoderReport.model_validate_json(
                output_path.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise CoderError(
                f"Codex returned an invalid structured coder report: {error}"
            ) from error
        if report.needs_human_input != (report.status == "needs_human_input"):
            raise CoderError(
                "Coder report status and needsHumanInput must agree."
            )
        return report
