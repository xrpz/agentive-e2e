# agentive-unit: lib.agentive
# design: spec.no_human_run (revised 2026-10-27, decisions.accepted[NOHUMAN]) · owner B
"""One definition of "no human present" for every agentive script (spec.no_human_run.used_by).

A run has no human when at least one positive signal holds:
  (1) AGENTIVE_HEADLESS=1 in the environment (headless standard command, marked Routines environment)
  (2) CI or GITHUB_ACTIONS is set (GitHub Actions sets both to true)
  (3) the hook input carries permission_mode == "dontAsk" (events whose input has the field; Claude Code
      SessionStart does not)
No signal or an ambiguous one = interactive. CLAUDE_CODE_REMOTE is NOT a signal (true in every cloud session),
bypassPermissions alone does not count, TTY and entrypoint are not read.
"""
import os

__all__ = ["no_human", "signals"]

_TRUE = {"1", "true", "yes"}


def _set(env, name):
    v = env.get(name)
    return v is not None and v.strip().lower() in _TRUE


def signals(env=None, hook_input=None):
    """List of the positive signals that hold (empty list = interactive)."""
    env = os.environ if env is None else env
    out = []
    if env.get("AGENTIVE_HEADLESS", "").strip() == "1":
        out.append("AGENTIVE_HEADLESS")
    for name in ("CI", "GITHUB_ACTIONS"):
        if _set(env, name):
            out.append(name)
    if isinstance(hook_input, dict) and hook_input.get("permission_mode") == "dontAsk":
        out.append("permission_mode=dontAsk")
    return out


def no_human(env=None, hook_input=None):
    return bool(signals(env, hook_input))
