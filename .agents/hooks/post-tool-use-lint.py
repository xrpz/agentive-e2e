#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.enforcement.hooks.post_tool_use
"""PostToolUse lint (matcher Edit|Write; Codex: apply_patch|Edit|Write): lints only the edited file.

- the edited paths: tool_input.file_path (Claude Code); Codex apply_patch has none, so they are parsed from the
  patch text (agentive.lintscope); a patch that yields no path is skipped, Stop covers it
- a path is linted only when it exists inside the repo and its extension is one the lint verb covers
  (manifest verbs.lint.exts, else derived from the manifest's ecosystem evidence); docs and other files exit 0
- runs `.agents/bin/verb lint -- <file>...`; a lint verb with `file_args: false` cannot take file arguments:
  it is skipped here and runs once at Stop (verify)
- a pending lint verb is skipped silently. On failure exit 2 with the excerpt and the AGENTIVE_FAIL line on stderr
  (Claude Code shows PostToolUse exit-2 stderr to Claude; the tool already ran)."""
import os
import subprocess
import sys

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import hookio, lintscope  # noqa: E402


def main(argv):
    inp = hookio.read_input()
    root = hookio.root_from(inp)
    manifest = hookio.load_manifest(root)
    lint = ((manifest or {}).get("verbs") or {}).get("lint")
    if not isinstance(lint, dict) or not lint.get("cmd") or lint.get("status") == "pending":
        return 0
    if lint.get("file_args") is False:
        return 0
    paths, ok = lintscope.paths_from_hook_input(inp)
    if not ok or not paths:
        return 0
    files = lintscope.select_files(root, paths, lintscope.lint_extensions(manifest))
    if not files:
        return 0
    r = subprocess.run([os.path.join(root, ".agents", "bin", "verb"), "lint", "--"] + files, cwd=root, capture_output=True, text=True)
    if r.returncode == 0:
        return 0
    lines = (r.stdout + r.stderr).strip().splitlines()
    sys.stderr.write("\n".join(lines[-25:]) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
