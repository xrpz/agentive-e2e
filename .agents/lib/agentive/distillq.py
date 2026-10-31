# agentive-unit: lib.agentive
# design: spec.maintenance.harvest.distill, spec.maintenance.distill, contracts[harvest-queue]
"""Read the harvest queue for the repo skill /distill (owner C; python3 stdlib only).

  python3 -m agentive.distillq [<queue path>]            list the entries ready for distill
  python3 -m agentive.distillq [<queue path>] --done <session_id> ...   remove handled entries

Queue: .agents/state/harvest-queue.jsonl (one JSON object per line, schema harvest-queue, appended by the
SessionEnd hook). An entry is ready when its transcript file was last written at least 30 seconds ago (the
transcript is written asynchronously); a missing transcript does not hold an entry back. Invalid lines are
reported with their line number and never removed. --done rewrites the file atomically without those entries.
"""
import json
import os
import sys
import tempfile
import time

from agentive.gitgh import git

REQUIRED = ("session_id", "transcript_path", "reason", "signals", "at")
SIGNALS = ("stop_blocks", "verify_fail_streak", "repeated_command")
MIN_AGE_S = 30


def problem(entry):
    if not isinstance(entry, dict):
        return "not an object"
    miss = [k for k in REQUIRED if k not in entry]
    if miss:
        return "missing " + ", ".join(miss)
    extra = [k for k in entry if k not in REQUIRED]
    if extra:
        return "unknown keys " + ", ".join(extra)
    sig = entry["signals"]
    if not isinstance(sig, dict) or not sig or any(k not in SIGNALS for k in sig):
        return "signals must hold stop_blocks, verify_fail_streak or repeated_command"
    for k in ("stop_blocks", "verify_fail_streak"):
        if k in sig and (not isinstance(sig[k], int) or sig[k] < 0):
            return f"signals.{k} must be an integer >= 0"
    for rc in sig.get("repeated_command", []):
        if not isinstance(rc, dict) or not rc.get("normalized") or not isinstance(rc.get("count"), int) or rc["count"] < 2:
            return "repeated_command items need normalized and count >= 2"
    return None


def read_queue(path):
    """[(line number, raw line, entry or None, problem or None)]"""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, raw in enumerate(f, 1):
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    rows.append((i, raw, None, "not JSON"))
                    continue
                rows.append((i, raw, e, problem(e)))
    except FileNotFoundError:
        pass
    return rows


def ready(entry, now=None):
    now = time.time() if now is None else now
    try:
        return now - os.stat(entry["transcript_path"]).st_mtime >= MIN_AGE_S
    except OSError:
        return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    done = []
    if "--done" in argv:
        k = argv.index("--done")
        argv, done = argv[:k], argv[k + 1:]
    root = git(os.getcwd(), "rev-parse", "--show-toplevel") or os.getcwd()
    path = argv[0] if argv else os.path.join(root, ".agents", "state", "harvest-queue.jsonl")
    rows = read_queue(path)
    if done:
        keep = [raw for _, raw, e, p in rows if p or e.get("session_id") not in done]
        removed = len(rows) - len(keep)
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(r if r.endswith("\n") else r + "\n" for r in keep)
        os.replace(tmp, path)
        print(f"QUEUE removed={removed} left={len(keep)}; NEXT=none")
        return 0
    n_ready = n_wait = n_bad = 0
    for i, _, e, p in rows:
        if p:
            n_bad += 1
            print(f"INVALID line={i} {p}")
            continue
        if not ready(e):
            n_wait += 1
            print(f"WAIT line={i} session={e['session_id']} (transcript written less than {MIN_AGE_S}s ago)")
            continue
        n_ready += 1
        print("ENTRY " + json.dumps({"line": i, "session_id": e["session_id"], "reason": e["reason"], "at": e["at"],
                                     "signals": e["signals"], "transcript_path": e["transcript_path"]}, ensure_ascii=False))
    nxt = "distill each ENTRY, then run the script again with --done <session_id> ..." if n_ready else "none"
    print(f"QUEUE path={path} ready={n_ready} waiting={n_wait} invalid={n_bad}; NEXT={nxt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
