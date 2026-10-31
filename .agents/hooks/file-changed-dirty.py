#!/usr/bin/env python3
# agentive-unit: rl.hooks
# design: spec.init_apply.reprove_on_change, spec.init_apply.stack_change.watch, contracts[dirty-state]
"""FileChanged (Claude Code only; matcher = literal evidence file names rendered by the engine).

Appends the slot ids whose evidence names the changed file to .agents/state/dirty.json (FileChanged cannot
block). While any verb is pending every pending verb and paths.* are marked, so the first scaffold triggers a
re-derive. The next Stop hook reports them; /agentive:reprove re-derives and re-proves them.
"""
import os
import sys

sys.dont_write_bytecode = True  # no __pycache__ in the user's repo (V1-08)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from agentive import hookio, state  # noqa: E402


def main(argv):
    inp = hookio.read_input()
    root = hookio.root_from(inp)
    manifest = hookio.load_manifest(root)
    if manifest is None:
        return 0
    fp = inp.get("file_path") or inp.get("path") or (inp.get("tool_input") or {}).get("file_path") or ""
    if not fp:
        return 0
    ap = fp if os.path.isabs(fp) else os.path.join(root, fp)
    rel = os.path.relpath(os.path.realpath(ap), os.path.realpath(root)).replace(os.sep, "/")
    base = os.path.basename(rel)
    slots = []
    pending = []
    for name, spec in (manifest.get("verbs") or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("status") == "pending":
            pending.append(f"verbs.{name}")
            continue
        files = (spec.get("evidence") or {}).get("files") or []
        if rel in files or base in files or base in {os.path.basename(f) for f in files}:
            slots.append(f"verbs.{name}")
    if pending:
        slots += pending + ["paths.src", "paths.tests", "paths.generated", "paths.vendor"]
    if slots:
        state.add_dirty(root, sorted(set(slots)), rel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
