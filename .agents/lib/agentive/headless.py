# agentive-unit: lib.agentive
# design: spec.context_management.headless.bash_hint, spec.context_management.headless.standard_cmd, decisions.accepted[HEADLESSHINT] · owner B
"""Headless Bash hint (E2E-32, decisions.accepted[HEADLESSHINT]): context for a Bash call that the headless standard command
would refuse, added by the PreToolUse context-gate hook on Claude Code. It never decides: no permissionDecision, no deny, no
allow; the call is judged by Claude Code's permissions exactly as without the hook. Why: under `--permission-mode dontAsk` a
refused call only says "Permission to use Bash has been denied" and tells the model to STOP; models then generalized it to
"Bash is denied" and ended the task. PreToolUse additionalContext is placed next to the tool result, also next to the refusal
(observed with Claude Code 2.1.316; PermissionDenied and PostToolUseFailure do not fire for a dontAsk refusal).

Only with no human present (nohuman.no_human of the hook input: AGENTIVE_HEADLESS=1, CI, permission_mode dontAsk). A call
gets one hint line when one of its simple commands (guard.segments; heredoc bodies left out) has:
  exit_status   `$?`                         -> run the command alone; the tool result and AGENTIVE_FAIL ... exit=<n> show it
  redirect      `>` / `>>` / `&>` into a file (not /dev/null, not &N), which includes a heredoc written to a file -> Write/Edit
  sed_in_place  `sed -i`                     -> Edit
  cd            `cd`                         -> every Bash call starts in the project root; pass paths
  not_allowlisted a program outside the allowlist and outside READ_ONLY -> .agents/bin/verb <verb>, Read, Glob, Grep
ALLOWED is the --allowedTools list of spec.context_management.headless.standard_cmd (a test compares the two). READ_ONLY is a
small subset of the commands Claude Code runs without a rule (code.claude.com/docs/en/permissions, read-only commands) [design
judgment: the subset; the full built-in list per version is unverified]. A false hint costs one context line, never a refusal.
"""
import re

from . import guard

__all__ = ["ALLOWED", "READ_ONLY", "problems", "hint", "CODE_NEXT"]

ALLOWED = ("Read", "Edit", "Write", "Glob", "Grep", "Bash(.agents/bin/verb *)", "Bash(.agents/bin/agentive-lint *)",
           "Bash(.agents/bin/agentive-review *)", "Bash(git add *)", "Bash(git commit *)", "Bash(git push *)", "Bash(gh api *)",
           "Bash(sh .agents/skills/*)", "Bash(git switch *)", "Bash(git fetch *)")
READ_ONLY = {"cat", "head", "tail", "ls", "pwd", "echo", "printf", "wc", "grep", "rg", "sort", "uniq", "cut", "tr", "diff",
             "stat", "file", "which", "basename", "dirname", "true", "false", "test", "date", "tree", "du", "df", "nl"}
READ_ONLY_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog", "merge-base"}
CODE_NEXT = {
    "redirect": "create or change files with the Write or Edit tool, not a shell redirect or a heredoc into a file",
    "exit_status": "run the command alone, without `; echo $?`: the tool result shows the exit code, and a verb prints "
                   "`AGENTIVE_FAIL ... exit=<n>` when it fails",
    "sed_in_place": "change the file with the Edit tool instead of sed -i",
    "cd": "do not cd: every Bash call starts in the project root, so pass the path to the command",
    "not_allowlisted": "run checks with `.agents/bin/verb <verb>` (setup, build, lint, typecheck, test, verify) and read files "
                       "with Read, Glob and Grep",
}
ORDER = ("redirect", "exit_status", "sed_in_place", "cd", "not_allowlisted")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_REDIR = re.compile(r"(?<![<>&\d])(?:\d|&)?(>>?|>\|)\s*(&?)(\S*)")


def _bash_patterns():
    return [a[5:-1] for a in ALLOWED if a.startswith("Bash(") and a.endswith(")")]


def _strip_heredocs(cmd):
    """The command without heredoc bodies (their lines are data, not commands)."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        delims = [m.group(2) for m in _HEREDOC.finditer(ln)]
        i += 1
        for d in delims:
            while i < len(lines) and lines[i].strip() != d:
                i += 1
            i += 1
    return "\n".join(out)


def _unquoted(text):
    """text with quoted spans blanked, so a `>` inside quotes is not a redirect."""
    out, q = [], None
    for c in text:
        if q:
            out.append(" ")
            if c == q:
                q = None
        elif c in "'\"":
            q = c
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)


def _redirects(seg):
    for m in _REDIR.finditer(_unquoted(seg)):
        amp, target = m.group(2), m.group(3)
        if amp or target in ("", "/dev/null") or target.startswith("&"):
            continue
        yield target


def _allowed(text):
    return any(guard._prefix_match(p, text) for p in _bash_patterns())


def problems(command):
    """[(code, part)] of a Bash command, in ORDER, each code once (part = the first simple command that has it)."""
    found = {}
    text = _strip_heredocs(command or "")
    if "$?" in re.sub(r"'[^']*'", "", text):            # single quotes keep `$?` literal; double quotes expand it
        found.setdefault("exit_status", "$?")
    # fd duplications (2>&1, >&2) are harmless; guard.segments would split at their `&`, so they go first
    for seg, _op in guard.segments(re.sub(r"\d*[<>]&(?:\d+|-)", " ", text)):
        if any(True for _ in _redirects(seg)):
            found.setdefault("redirect", seg[:120])
        argv, inner = guard.normalize(seg)
        if inner is not None:
            for code, part in problems(inner):
                found.setdefault(code, part)
            continue
        if not argv:
            continue
        prog = argv[0]
        joined = " ".join(argv)
        if prog == "cd":
            found.setdefault("cd", joined[:120])
        elif prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in argv[1:]):
            found.setdefault("sed_in_place", joined[:120])
        elif not (_allowed(joined) or prog in READ_ONLY or (prog == "sed" and "-n" in argv)
                  or (prog == "git" and len(argv) > 1 and argv[1] in READ_ONLY_GIT)):
            found.setdefault("not_allowlisted", joined[:120])
    return [(c, found[c]) for c in ORDER if c in found]


def hint(command, no_human, tool="claude_code"):
    """The additionalContext line for this Bash call, or None. Never a decision (HEADLESSHINT)."""
    if tool != "claude_code" or not no_human or not isinstance(command, str) or not command.strip():
        return None
    got = problems(command)
    if not got:
        return None
    codes = ",".join(c for c, _ in got)
    parts = "; ".join(f"{c}: `{p}`" for c, p in got)
    nxt = "; ".join(CODE_NEXT[c] for c, _ in got)
    return (f"AGENTIVE_HEADLESS_BASH={codes}: this headless run (claude -p, dontAsk) runs only allowlisted commands, one simple "
            f"command per Bash call ({parts}). If this call is refused, only this one command was refused: Bash, /verify, "
            f"/precommit-review and git commit still work, so continue the task. NEXT={nxt}")
