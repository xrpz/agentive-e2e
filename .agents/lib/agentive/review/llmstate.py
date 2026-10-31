# agentive-unit: lib.agentive
# design: spec.work_loop.ci_ai_review_floor.llm_failure, spec.work_loop.ci_ai_review_floor.workflow_change, decisions.accepted[AISTEPFAIL], decisions.accepted[WFCHANGE] (owner D)
"""State of the ai-review LLM step (claude-code-action or codex-action) for the finishing step and the gate.

E2E-10b (decisions.accepted[AISTEPFAIL], user decision 2026-10-30: the recommendation, without the raw-text option): when the LLM step fails, the step
`llm-state` right after it records an allowlisted code (CODES) in <work>/llm-state.json and prints one `::error` annotation.
The PR summary comment, the gate reason and the gate NEXT (`ai_step_failed:<code>`) carry only that code, a fixed sentence
and the run URL. No error text is copied anywhere (comment, annotation, step summary, state file): the text of the
execution file is read only to pick the code. The raw text is reached by a debug re-run of the job (the action shows its
full output when GitHub debug logging is on) or by running the same command locally with `claude -p`.

E2E-11 (decisions.accepted[WFCHANGE], user decision 2026-10-30): claude-code-action skips its run (exit 0, no outputs) when the
workflow file of the run is not identical to the default branch's copy (the action's workflow validation; the first install
PR and every PR that changes .github/workflows/agentive-review.yml). workflow_skip() detects it deterministically by blob
comparison of that file: the commit the run used (GITHUB_SHA, the PR merge commit, never the head) against
origin/<default branch>, plus the base commit to tell a workflow change from a base that differs from the default branch.
Hybrid: when the recorded action state says Claude ran (conclusion success | failure) or the CI produced a review, no skip
reason is given even when the blobs differ. A fork PR is named first (no secrets and no OIDC token). Claude path only.
"""
import json
import os
import subprocess

from .. import common

STATE_FILE = "llm-state.json"
WORKFLOW = ".github/workflows/agentive-review.yml"
CODES = ("insufficient_credits", "auth", "rate_limit", "timeout", "context_too_long", "max_turns", "no_result", "unclassified",
         "codex_exit")
SENTENCES = {
    "insufficient_credits": "the Claude API account of the CI credential has too little credit; add credit, or store the subscription "
                            "token CLAUDE_CODE_OAUTH_TOKEN (passed first), then re-run the ai-review job",
    "auth": "the CI credential was rejected; check the repository secret (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY), then re-run "
            "the ai-review job",
    "rate_limit": "the model API rate-limited the run or was overloaded; re-run the ai-review job later",
    "timeout": "the LLM step timed out; re-run the ai-review job",
    "context_too_long": "the review input did not fit the model context; split the PR, then push",
    "max_turns": "the LLM step reached its turn limit (--max-turns) before the review finished; re-run the ai-review job or split the PR",
    "no_result": "the LLM step ended without a result",
    "unclassified": "the LLM step failed for a reason this check does not classify",
    "codex_exit": "codex exec exited with an error; the reason is in the ai-review job log",
}
RAW_HINT = ("no error text is copied here: re-run the job with debug logging, or run the same command locally with claude -p, "
            "to read it")
# substrings of the execution file's result (lower case) -> code; first match wins. The text only picks the code.
PATTERNS = (
    ("insufficient_credits", ("credit balance", "insufficient credit", "insufficient_quota")),
    ("auth", ("invalid api key", "invalid x-api-key", "authentication", "unauthorized", "oauth token", "/login")),
    ("rate_limit", ("rate limit", "rate_limit", "429", "overloaded", "too many requests")),
    ("context_too_long", ("prompt is too long", "context length", "context window", "maximum context", "input is too long",
                          "too many tokens")),
    ("timeout", ("timed out", "timeout", "deadline exceeded")),
)


# ---------------------------------------------------------------- E2E-10b: the failure code
def classify_claude(path):
    """The code for a failed claude-code-action run from its execution file (a JSON list of SDK messages; the last element of
    type result carries subtype, is_error and result). Returns a code from CODES; nothing else leaves this function."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return "no_result"
    items = data if isinstance(data, list) else []
    res = next((m for m in reversed(items) if isinstance(m, dict) and m.get("type") == "result"), None)
    if res is None:
        return "no_result"
    if str(res.get("subtype") or "") == "error_max_turns":
        return "max_turns"
    text = " ".join(str(x) for x in (res.get("result"), res.get("errors"), res.get("error")) if x).lower()
    for code, words in PATTERNS:
        if any(w in text for w in words):
            return code
    return "unclassified"


def _started(conclusion, execution_file):
    """True when Claude itself ran (the action set its documented conclusion output, or wrote an execution file with messages),
    False when the action ended before Claude ran, None when unknown."""
    if conclusion in ("success", "failure"):
        return True
    if execution_file:
        try:
            with open(execution_file, encoding="utf-8") as f:
                return bool(json.load(f))
        except (OSError, ValueError):
            return False
    return False


def record(tool, outcome, conclusion, execution_file):
    """The llm-state.json record: tool, outcome (steps.llm.outcome), conclusion (steps.llm.outputs.conclusion), started, failed,
    code. Never an error text."""
    outcome = outcome or "unknown"
    st = {"tool": tool, "outcome": outcome, "conclusion": conclusion or "", "failed": outcome in ("failure", "cancelled")}
    if tool == "claude":
        st["started"] = _started(conclusion, execution_file)
        if st["failed"]:
            st["code"] = classify_claude(execution_file) if execution_file else "no_result"
    else:
        st["started"] = None                     # codex-action has no output that tells it (final-message only on success)
        if st["failed"]:
            st["code"] = "codex_exit"
    return st


def write(work, st):
    common.write_json(os.path.join(work, STATE_FILE), st)


def read(work):
    """The recorded state, or None. The file comes from the ai-review job (it runs the PR head's scripts), so only known
    values are kept."""
    st = common.read_json(os.path.join(work, STATE_FILE)) if work else None
    if not isinstance(st, dict):
        return None
    out = {"tool": st.get("tool") if st.get("tool") in ("claude", "codex") else None,
           "outcome": str(st.get("outcome") or "unknown")[:20], "failed": st.get("failed") is True,
           "started": st.get("started") if st.get("started") in (True, False) else None}
    if out["failed"]:
        out["code"] = st.get("code") if st.get("code") in CODES else "unclassified"
    return out


def run_url():
    """The URL of this workflow run (GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_RUN_ID), or None outside Actions."""
    if not (os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY")):
        return None
    return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def failure_text(st, url=None):
    """One sentence for the comment, the gate reason and the annotation: code, fixed sentence, hint, run URL."""
    code = st["code"]
    hint = "" if code == "codex_exit" else f"; {RAW_HINT}"
    return f"AI review step failed (ai_step_failed:{code}): {SENTENCES[code]}{hint}" + (f" (run {url})" if url else "")


def annotation(st, url=None):
    """The single ::error workflow command for a failed LLM step (fixed text, no newline or % to escape)."""
    return f"::error title=agentive ai-review::{failure_text(st, url)}"


# ---------------------------------------------------------------- E2E-11: the workflow-validation skip
FORK = ("fork PR: GitHub gives a workflow run from a fork no repository secrets and no OIDC token, so the AI review cannot run "
        "(the gate sends the PR to a human)")
WORKFLOW_CHANGE = ("workflow-change PR: AI review skipped by the action; human review. The Claude Code action runs only when the "
                   f"workflow file of the run is identical to the default branch's copy (its workflow validation), and this PR "
                   f"{{what}} {WORKFLOW}; a human reviews and merges it, and later PRs get the AI review once it is merged")
NEXT = {
    "fork": "a human reviews and merges the fork PR (no AI review can run on it)",
    "workflow_change": "workflow-change PR: AI review skipped by the action; human review (merge it, then later PRs get the AI review)",
    "base_differs": "bring the default branch workflow into the base: merge {default}'s " + WORKFLOW + " into {base}; until then PRs into "
                    "{base} get no AI review and a human reviews them",
    "stale_merge": "push a new commit or close and reopen the PR: GitHub then builds a new merge commit with the default branch's "
                   + WORKFLOW + " (running the old run again keeps its merge commit)",
}


def _git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def blob(repo, rev, path=WORKFLOW):
    """The blob id of path at rev, or None when the commit or the file is missing."""
    if not rev:
        return None
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}:{path}")


def default_ref(repo, default_branch):
    """refs/remotes/origin/<default>, fetched once (depth 1) when the checkout lacks it; None when it cannot be read."""
    ref = f"refs/remotes/origin/{default_branch}"
    if _git(repo, "rev-parse", "--verify", "--quiet", ref) is None:
        _git(repo, "fetch", "--no-tags", "--quiet", "--depth=1", "origin", f"+refs/heads/{default_branch}:{ref}")
    return ref if _git(repo, "rev-parse", "--verify", "--quiet", ref) is not None else None


def workflow_skip(repo, tool, state, have_ci_review, default_branch, base_ref, base_sha, run_rev, head_repo=None, repository=None):
    """{kind, exempt, detail, next} when the AI review of this run did not happen because of the action's workflow validation
    (kinds workflow_change, base_differs, stale_merge; exempt True: agentive-independence does not require the CI review and
    the gate ends human_required) or because the PR comes from a fork (kind fork, exempt False). None otherwise, and None
    whenever a fact cannot be read (fail-safe: the earlier behaviour applies)."""
    if tool != "claude" or have_ci_review or (state or {}).get("started") is True:
        return None
    if head_repo and repository and head_repo != repository:
        return {"kind": "fork", "exempt": False, "detail": FORK, "next": NEXT["fork"]}
    if not default_branch:
        return None
    run_blob = blob(repo, run_rev or "HEAD")
    if run_blob is None:
        return None
    ref = default_ref(repo, default_branch)
    if ref is None:
        return None
    default_blob = blob(repo, ref)
    if default_blob == run_blob:
        return None
    base_blob = blob(repo, base_sha) if base_sha else None
    if run_blob != base_blob:
        what = "adds (first install: the default branch has no copy yet)" if default_blob is None else "changes"
        return {"kind": "workflow_change", "exempt": True, "detail": WORKFLOW_CHANGE.format(what=what), "next": NEXT["workflow_change"]}
    if base_ref and base_ref != default_branch:
        nxt = NEXT["base_differs"].format(default=default_branch, base=base_ref)
        return {"kind": "base_differs", "exempt": True, "next": nxt,
                "detail": f"AI review skipped by the action: the base branch {base_ref} has a {WORKFLOW} that differs from the default "
                          f"branch {default_branch}, and the action runs only with the default branch's copy; human review"}
    return {"kind": "stale_merge", "exempt": True, "next": NEXT["stale_merge"],
            "detail": f"AI review skipped by the action: this run's merge commit has an older {WORKFLOW} than the default branch "
                      f"{default_branch} (the default branch changed it after this run was created); human review"}


def skip_from_env(repo, tool, state, have_ci_review, pr):
    """workflow_skip with the facts of a pull_request run: AGENTIVE_DEFAULT_BRANCH, AGENTIVE_HEAD_REPO, GITHUB_REPOSITORY and
    GITHUB_SHA (the merge commit the run used) from the environment, base ref and sha from the PR."""
    return workflow_skip(repo, tool, state, have_ci_review, os.environ.get("AGENTIVE_DEFAULT_BRANCH"), (pr or {}).get("base_ref"),
                         (pr or {}).get("base_sha"), os.environ.get("GITHUB_SHA") or "HEAD",
                         os.environ.get("AGENTIVE_HEAD_REPO"), os.environ.get("GITHUB_REPOSITORY"))
