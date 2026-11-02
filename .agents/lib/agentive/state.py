# agentive-unit: lib.agentive
# design: contracts[stop-state], contracts[dirty-state], contracts[verify-result], contracts[harvest-queue], spec.stop_hook · owner B
"""Local state files under .agents/state/ (gitignored through the .gitignore#state region).

stop.json (Stop hook counter), dirty.json (FileChanged slots), verify.json (verb wrapper), harvest-queue.jsonl
(SessionEnd). Every writer keeps the schema shape; readers tolerate a missing or broken file.
"""
import json
import os

from . import common

__all__ = ["path", "load_stop", "save_stop", "reset_stop", "load_dirty", "add_dirty", "load_verify", "append_harvest", "harvest_count"]


def path(root, name):
    return os.path.join(root, ".agents", "state", name)


def _read(p, default):
    try:
        v = common.read_json(p, default)
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def load_stop(root, session_id):
    s = _read(path(root, "stop.json"), {})
    if s.get("session_id") != session_id:
        # a new start: session_id changed (spec.stop_hook.reset)
        s = {"session_id": session_id or "", "consecutive_blocks": 0, "last_fail_count": None, "total_blocks": 0,
             "last_reason": None, "handoff_requested": False, "failure_window": [],
             "last_reset": "new_start" if s else None}
    s.setdefault("consecutive_blocks", 0)
    s.setdefault("last_fail_count", None)
    s.setdefault("failure_window", [])
    s.setdefault("total_blocks", 0)
    s.setdefault("handoff_requested", False)
    return s


def save_stop(root, s):
    s["failure_window"] = list(s.get("failure_window") or [])[-10:]
    common.write_json(path(root, "stop.json"), s)


def reset_stop(root, session_id, why="new_start"):
    s = {"session_id": session_id or "", "consecutive_blocks": 0, "last_fail_count": None, "total_blocks": 0,
         "last_reason": None, "handoff_requested": False, "failure_window": [], "last_reset": why}
    save_stop(root, s)
    return s


def load_dirty(root):
    d = _read(path(root, "dirty.json"), {})
    slots = d.get("slots") if isinstance(d.get("slots"), list) else []
    return {"slots": slots}


def add_dirty(root, slots, file):
    """Append slot ids for a changed file (deduplicated by slot+file; the newest time wins)."""
    d = load_dirty(root)
    now = common.utc_now()
    keep = [e for e in d["slots"] if not (e.get("file") == file and e.get("slot") in slots)]
    keep += [{"slot": s, "file": file, "at": now} for s in slots]
    d["slots"] = keep
    common.write_json(path(root, "dirty.json"), d)
    return d


def load_verify(root):
    return _read(path(root, "verify.json"), {})


def append_harvest(root, line):
    p = path(root, "harvest-queue.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def harvest_count(root):
    """Number of entries (non-empty lines) in harvest-queue.jsonl; 0 when the file is missing."""
    try:
        with open(path(root, "harvest-queue.jsonl"), encoding="utf-8", errors="replace") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0
