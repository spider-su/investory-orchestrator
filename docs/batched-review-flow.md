# Flow review: prompt to mergeable PR

## Expected flow

1. Put the requested behavior in an issue and add the configured ready label.
2. Intake formats the prompt without adding product requirements. The persisted
   queue waits for runner capacity and quota.
3. The planner creates a compact acceptance checklist and testable increments.
4. The coder implements each increment, runs formatting/unit checks, and the
   validator checks it independently. The orchestrator commits locally.
5. One independent audit checks the full request and returns all blocking
   findings together. Warnings/suggestions remain TODOs.
6. If needed, the coder repairs the whole batch; follow-up review checks the
   previous findings and changed code. Earlier working increments are retained.
7. Publish the complete branch and draft PR once. Require green CI on its head.
   Red CI supplies all failed jobs/annotations to the coder, with up to three
   bounded repair rounds. Each repaired candidate is validated and reviewed.
8. Reuse approval on the unchanged validated/reviewed commit, mark READY, and
   notify the owner. Human review/merge and post-merge CI complete the task.

## Review outcome

The former second full audit after green CI is removed for new candidates with
bound review evidence. Changed commits, missing evidence, and historical tasks
fall back to independent review. Candidate trees are captured with a temporary
Git index, leaving the real index intact. Source changes during review or between
review and finalization invalidate publication. Approval and validation must
cover the same tree; CI must pass on the corresponding published commit.

Repair context survives the CLI resume path, including CI repairs. Nonblocking
findings survive repair reviews. Related acceptance cases are requested in one
audit, although a model can still miss a genuine defect; a newly discovered
material defect may block, with concrete evidence and a proposed repair.

The setup example now selects simplified mode, matching the CLI default.
Historical checkpoints keep their saved routing. Reviewer log, report and diff
excerpts are bounded; full source and captured validation evidence remain
available for focused inspection.

## Before the next live run

Merge/deploy this version and synchronize the Mac runner to the expected build
SHA. Use one modest issue containing a basic prompt. Verify capacity/quotas allow
pickup, locally tested commits survive, the PR targets the configured branch,
one whole-plan audit occurs, and green CI reuses approval without another Codex
review job. If a repair is necessary, confirm the reviewer receives the previous
batch and only its delta. Keep CI failures blocking until green.

No additional architecture change is required for that test. Two later
optimizations deserve measured evaluation: persist per-role token usage, and
avoid repeating the final deterministic suite when the last step already ran
the same suite on identical files and configuration. Current checks are retained
until that equivalence is proven; simulated tests are not live runner acceptance.
