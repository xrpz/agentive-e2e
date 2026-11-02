# agentive-unit: lib.agentive
# design: spec.repo_layer.files[f-reviewers].scripts, spec.review_pipeline.principles, spec.context_management.subagent_output (owner D)
"""LLM task protocol between `.agents/bin/agentive-review` (deterministic) and the agent that runs it [design judgment].

The script never calls a model. When an LLM stage has work, it writes one task input file per subagent call and
prints, last on stdout:

  AGENTIVE_REVIEW_TASK id=<task> agent=<agent> input=<input path> output=<output path> meta=<meta path>     (one line per task)
  AGENTIVE_REVIEW=tasks:<n> stage=<RVx> NEXT=<what to do>

The agent runs each task with the named project subagent (.claude/agents/<agent>.md, rendered from
.agents/agents/<agent>.md; Codex .codex/agents/<agent>.toml) in a fresh context, with the prompt
`Read <input path> and do the task it describes. Reply with the JSON object only.`, writes the subagent's reply
verbatim to the output path, and runs the same command again. Tasks of one stage are independent (run them in
parallel). The script validates every output; a missing or invalid output is handled fail-safe by the stage
(RV3 -> class other, RV4 -> tier 2, RV5/RV6 -> human_required, RV7 -> inconclusive).

Each task input carries a correlation id `session_id` (uuid4, written by this script). It is NOT a platform session id and
proves nothing about independence: finding.verification.verifier_session_id and review-report rv5.reviewers[].task_ref
only link a result to its task. Independence is enforced in CI (the review must be produced by the ai-review job itself,
review/independence.py) and attested locally (review/attest.py): locally the orchestrator writes how it ran a task
({"executed_by": "subagent" | "self"}) to the task's meta path, and a SubagentStart or SubagentStop hook observation
decides whether the record may say isolated.
"""
import json
import os
import uuid

from .. import common

AGENTS = {"rv3": "classifier", "rv4": "raise-only", "rv7": "verifier", "rv6": "coordinator"}


class Tasks:
    def __init__(self, work_dir):
        self.dir = os.path.join(work_dir, "tasks")
        self.pending = []

    def _paths(self, tid):
        return os.path.join(self.dir, tid + ".input.json"), os.path.join(self.dir, tid + ".output.json")

    def output(self, tid):
        """(parsed output or None, session_id or None, problem or None) of a task that has an input file."""
        ip, op = self._paths(tid)
        inp = common.read_json(ip)
        if inp is None:
            return None, None, None
        sid = inp.get("session_id")
        if not os.path.exists(op):
            return None, sid, "no output"
        try:
            with open(op, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as e:
            return None, sid, str(e)
        out = _parse_json_reply(text)
        if out is None:
            return None, sid, "output is not a JSON object"
        return out, sid, None

    def meta_path(self, tid):
        return os.path.join(self.dir, tid + ".meta.json")

    def input_path(self, tid):
        return self._paths(tid)[0]

    def output_path(self, tid):
        return self._paths(tid)[1]

    def has_input(self, tid):
        return os.path.exists(self._paths(tid)[0])

    def has_output(self, tid):
        return os.path.exists(self._paths(tid)[1])

    def request(self, tid, agent, payload):
        """Write the task input (once; a rerun keeps the session id) and queue the task."""
        ip, op = self._paths(tid)
        if not os.path.exists(ip):
            data = dict(payload)
            data.update({"task": tid, "agent": agent, "session_id": str(uuid.uuid4()), "output_path": op,
                         "reply": "Reply with exactly one JSON object as described in output_format; no prose, no code fence."})
            common.write_json(ip, data)
        self.pending.append((tid, agent, ip, op))

    def lines(self, stage, rerun_cmd):
        ci = bool(os.environ.get("GITHUB_ACTIONS"))
        out = [f"AGENTIVE_REVIEW_TASK id={t} agent={a} input={i} output={o}" + ("" if ci else f" meta={self.meta_path(t)}")
               for t, a, i, o in self.pending]
        meta = "" if ci else (" write {\"executed_by\": \"subagent\"} (or \"self\" when you did the task yourself) to its meta path,")
        out.append(f"AGENTIVE_REVIEW=tasks:{len(self.pending)} stage={stage} NEXT=run each AGENTIVE_REVIEW_TASK with its "
                   f"agent as a subagent in a fresh context (prompt: Read <input> and do the task it describes. Reply with the "
                   f"JSON object only.), write each reply verbatim to its output path,{meta} then run: {rerun_cmd}")
        return out


def _parse_json_reply(text):
    """The JSON object in a subagent reply (tolerates a ```json fence around it)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    try:
        v = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            v = json.loads(t[start:end + 1])
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


OUTPUT_FORMATS = {
    "classifier": {"classes": "non-empty list from: refactor, feature, fix, security, deps, config, test, other "
                              "(never docs, format, lockfile or generated: only rules may give those)",
                   "why": "one sentence"},
    "raise-only": {"human_needed": "true or false",
                   "criteria": "list of a | b | c that apply (non-empty when human_needed is true)",
                   "paths": "optional list of the changed repo-relative paths that made you answer true (a human may turn them into a rule)",
                   "why": "one sentence naming the code and the criterion"},
    "reviewer": {"findings": [{"file": "repo-relative path", "line": "integer line number in the file at head (the +c side of the @@ hunk header), "
                                       "1..changed_files[file].lines; never a line number of the diff file",
                               "trigger": "the condition that makes it a bug (required)",
                               "summary": "one sentence", "severity": "important | nit | pre_existing",
                               "bug_class": "logic | error-handling | concurrency | data-contract | security | test-gap | spec-drift | other",
                               "pattern": "free-text name of the bug pattern (display only, optional)",
                               "evidence": "how you know (code ref, command)",
                               "instances": [{"file": "path", "line": "integer"}],
                               "same_as": "the id of an open finding this is the same issue as (optional; never compute an id)"}],
                 "open_findings": [{"id": "open finding id from the input", "status": "still_present | fixed",
                                    "evidence": "required when status is fixed"}]},
    "verifier": {"verdicts": [{"id": "finding id", "verdict": "confirmed | refuted | inconclusive",
                               "method": "execution | adversarial_fresh_context",
                               "note": "the command you ran and its result, or why the claim holds or fails"}]},
    "coordinator": {"findings": [{"id": "finding id kept", "merged_from": ["ids merged into it"],
                                  "severity": "important | nit | pre_existing",
                                  "bug_class": "logic | error-handling | concurrency | data-contract | security | test-gap | spec-drift | other"}]},
}

# The rules every reviewer follows for a finding, written once. The reviewer task input carries them as `finding_rules`, so the
# three reviewer prompts (.agents/agents/*-reviewer.md) keep only what is specific to their role.
FINDING_RULES = [
    "`file` and `line` in the head tree and `trigger` (the input or state that makes it fail) are required; a finding without "
    "both is dropped before verification.",
    "`line` is the line number in the file at head: count from the `+c` of the hunk header `@@ -a,b +c,d @@`, or open the file; "
    "it lies inside `changed_files[<file>].lines`. Never use a line number of `diff_file` itself: a patch read with a "
    "line-numbered viewer shows its own numbers, which point past the end of the file.",
    "One finding per bug class: list the other places with the same pattern in `instances`.",
    "`severity` is important (a real defect a human would want fixed before merge), nit or pre_existing (code the PR did not introduce).",
    "`bug_class` is one of the values in `output_format`; a more specific name goes to `pattern` (free text, display only).",
    "Re-reporting an open finding: set `same_as` to its id (the script checks that the path is the same file); never compute or "
    "invent an id or a hash. For each entry of `open_findings` say `still_present` or `fixed` in `open_findings` of your reply; `fixed` "
    "needs an `evidence` sentence (the changed line or the command you ran), otherwise leave the entry out: a finding you do "
    "not mention is NOT treated as fixed.",
]
