# agentive-unit: lib.agentive
# design: spec.verb_wrapper, contracts[verify-result], spec.init_apply.empty_dir.pending.consumers, spec.verification.E1c, decisions.accepted[VERIFYTREE] · owner B
"""`.agents/bin/verb <verb> [-- args...]`: the one wrapper every verb runs through.

- reads the agentive manifest verbs.<verb>; a pending verb prints `PENDING verb=<v> reason=no_evidence` and exits 0
- runs `<cmd> [args]` with `sh -c`, no pipe: all output goes to .agents/state/logs/<verb>-<ts>.log (umask 077,
  latest 20 per verb kept); prints an excerpt: success = last 5 lines, failure = last 40 lines and at most 4,000
  bytes (whole lines only; no per-line cut, which breaks UTF-8)
- failure: last stdout line `AGENTIVE_FAIL verb=<v> exit=<n> log=<path>; NEXT=<one read command>`
- failures = `grep -c -E <fail_count>` on the log when the manifest sets fail_count (at least 1 on a non-zero
  exit), else 0/1 (documented limitation)
- `verb verify` runs every inner-tier verb with a command (lint, typecheck, build, test order) and then the verify
  verb's own command when it is not the wrapper itself (for example a wired secret scanner), and writes
  .agents/state/verify.json (pending[], head_sha, worktree_dirty, content_tree taken before the verbs ran, history of
  the last 10 runs). A record is fresh for a tree when its content_tree is that tree and its verb commands are the
  ones the manifest names now (fresh_for; VERIFYTREE, E2E-08): no commit sha, OS or environment is part of the key
- `verb verify --staged` verifies exactly the staged content, the pre-commit way [S191]: the paths whose work-tree
  content differs from the index (unstaged changes, untracked files) are set aside, the verbs run, and every path is
  restored, also on an error or an interrupt; a backup commit of the work content is kept in
  refs/agentive/verify-staged until the restore is checked. content_tree is then the index tree (`git write-tree`)
- `verb lint -- <file>...` runs the lint command on those files only: a trailing whole-repo target (`.`, `./...`, a
  manifest src/tests path) is replaced by them, else they are appended (agentive.lintscope.file_command, E2E-04); the
  PostToolUse lint hook lints only the edited file
- extra args after `--` are appended shell-quoted [design judgment: lets the test-rewrite hook keep arguments]
Logs are not redacted: do not paste or commit them.
"""
import datetime
import os
import re
import shlex
import subprocess
import sys
import time

from . import common, hookio, lintscope

__all__ = ["main", "run_verb", "VERB_ORDER", "planned_commands", "fresh_for"]

VERB_ORDER = ["setup", "build", "lint", "typecheck", "test", "verify", "run"]
VERIFY_ORDER = ["lint", "typecheck", "build", "test"]
SELF = ".agents/bin/verb verify"
KEEP_LOGS = 20
BACKUP_REF = "refs/agentive/verify-staged"


def _ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _prune(logdir, verb):
    logs = sorted(n for n in os.listdir(logdir) if n.startswith(verb + "-") and n.endswith(".log"))
    for n in logs[:-KEEP_LOGS]:
        try:
            os.unlink(os.path.join(logdir, n))
        except OSError:
            pass


def _excerpt(data, ok):
    lines = data.decode("utf-8", "replace").splitlines()
    if ok:
        return "\n".join(lines[-5:])
    picked, size = [], 0
    for ln in reversed(lines[-40:]):
        b = len(ln.encode("utf-8")) + 1
        if size + b > 4000:
            break
        picked.append(ln)
        size += b
    return "\n".join(reversed(picked))


def run_verb(root, manifest, verb, extra=None, quiet=False):
    """Run one verb. Returns dict {ok, failures, duration_ms, cmd, log, rc, pending, fail_line}."""
    spec = (manifest.get("verbs") or {}).get(verb)
    if not isinstance(spec, dict) or spec.get("status") == "pending" or not spec.get("cmd"):
        if not quiet:
            print(f"PENDING verb={verb} reason=no_evidence")
        return {"pending": True}
    cmd = spec["cmd"]
    if extra and verb == "lint" and spec.get("file_args") is not False:
        cmd = lintscope.file_command(cmd, extra, manifest.get("paths"))
    elif extra:
        cmd = cmd + " " + " ".join(shlex.quote(a) for a in extra)
    logdir = os.path.join(root, ".agents", "state", "logs")
    old = os.umask(0o077)
    try:
        os.makedirs(logdir, exist_ok=True)
        rel = f".agents/state/logs/{verb}-{_ts()}.log"
        logp = os.path.join(root, rel)
        t0 = time.monotonic()
        with open(logp, "wb") as log:
            log.write(f"$ {cmd}\n".encode("utf-8"))
            log.flush()
            env = dict(os.environ, AGENTIVE_VERB=verb)
            try:
                rc = subprocess.run(["sh", "-c", cmd], cwd=root, stdout=log, stderr=subprocess.STDOUT, env=env).returncode
            except OSError as e:
                log.write(f"agentive: could not start sh: {e}\n".encode("utf-8"))
                rc = 127
        dur = int((time.monotonic() - t0) * 1000)
    finally:
        os.umask(old)
    _prune(logdir, verb)
    with open(logp, "rb") as f:
        data = f.read()
    ok = rc == 0
    failures = 0 if ok else 1
    pat = spec.get("fail_count")
    if pat:
        try:
            n = sum(1 for ln in data.decode("utf-8", "replace").splitlines() if re.search(pat, ln))
            failures = n if (n or ok) else 1
        except re.error:
            pass
    res = {"ok": ok, "failures": failures, "duration_ms": dur, "cmd": cmd, "log": rel, "rc": rc, "pending": False}
    if not quiet:
        ex = _excerpt(data, ok)
        if ex:
            print(ex)
    if not ok:
        grep = pat or r"(error|Error|ERROR|fail|FAIL|Fail)"
        res["fail_line"] = common.fail_line("AGENTIVE_FAIL", f"grep -n -m20 -E {shlex.quote(grep)} {rel}",
                                            verb=verb, exit=rc, log=rel)
    return res


def planned_commands(manifest):
    """{verb: cmd} that `verb verify` runs now: the inner verbs with a command, then the verify verb's own command."""
    verbs = (manifest or {}).get("verbs") or {}
    out = {}
    for v in VERIFY_ORDER:
        spec = verbs.get(v)
        if isinstance(spec, dict) and spec.get("status") != "pending" and spec.get("cmd") and spec.get("tier", "inner") == "inner":
            out[v] = spec["cmd"]
    vspec = verbs.get("verify")
    if isinstance(vspec, dict) and vspec.get("status") != "pending" and vspec.get("cmd") and vspec["cmd"].strip() != SELF:
        out["verify"] = vspec["cmd"]
    return out


def fresh_for(record, tree, manifest=None):
    """True when the verify record describes `tree` (VERIFYTREE): content_tree == tree and, when the manifest is known,
    the commands the record ran are the ones verify would run now. A record without content_tree (written before
    2026-10-30) is never fresh: verify runs once more."""
    if not isinstance(record, dict) or not tree or record.get("content_tree") != tree:
        return False
    if manifest is None:
        return True
    ran = {k: v.get("cmd") for k, v in (record.get("verbs") or {}).items() if isinstance(v, dict)}
    return ran == planned_commands(manifest)


def _verify(root, manifest, tree=None):
    verbs = manifest.get("verbs") or {}
    content = tree or hookio.content_tree(root)   # taken before the verbs run; the staged run passes the index tree
    results, pending, fails = {}, [], []
    for v in VERIFY_ORDER:
        spec = verbs.get(v)
        if not isinstance(spec, dict):
            continue
        if spec.get("status") == "pending":
            pending.append(v)
            continue
        if spec.get("tier", "inner") != "inner":
            continue
        print(f"== {v}: {spec['cmd']}")
        r = run_verb(root, manifest, v)
        results[v] = {k: r[k] for k in ("ok", "failures", "duration_ms", "cmd", "log")}
        if not r["ok"]:
            fails.append(r["fail_line"])
    vspec = verbs.get("verify")
    if isinstance(vspec, dict) and vspec.get("status") == "pending":
        pending.append("verify")
    elif isinstance(vspec, dict) and vspec.get("cmd") and vspec["cmd"].strip() != SELF:
        print(f"== verify: {vspec['cmd']}")
        r = run_verb(root, manifest, "verify")
        results["verify"] = {k: r[k] for k in ("ok", "failures", "duration_ms", "cmd", "log")}
        if not r["ok"]:
            fails.append(r["fail_line"])
    for v in ("setup", "run"):
        if isinstance(verbs.get(v), dict) and verbs[v].get("status") == "pending":
            pending.append(v)
    total = sum(r["failures"] for r in results.values())
    prev = common.read_json(os.path.join(root, ".agents", "state", "verify.json"), {}) or {}
    run_id = "verify-" + _ts()
    history = (list(prev.get("history") or []) + [{"run_id": run_id, "total_failures": total}])[-10:]
    head = hookio.head_sha(root) or "0" * 40
    porcelain = hookio.git(root, "status", "--porcelain") or ""
    doc = {"run_id": run_id, "ts": common.utc_now(), "verbs": results, "total_failures": total,
           "history": history, "pending": sorted(set(pending), key=VERB_ORDER.index), "head_sha": head,
           "worktree_dirty": bool(porcelain.strip())}
    if content:
        doc["content_tree"] = content
    common.write_json(os.path.join(root, ".agents", "state", "verify.json"), doc)
    for line in fails:
        print(line)
    if not results:
        print("PENDING verify: no verb has a command yet, pending (no evidence); NEXT=scaffold the project, then run /agentive:update")
    return 1 if fails else 0


def _paths_input(paths):
    return b"".join(os.fsencode(p) + b"\0" for p in paths)


def _git_in(root, args, data):
    try:
        r = subprocess.run(["git", "-C", root] + args, input=data, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.decode("utf-8", "replace").strip()


def _set_paths(root, tree, paths):
    """Make the work-tree content of `paths` equal `tree` without touching the index: `git restore --source=<tree>
    --worktree` for the paths the tree has, removal for the others. Returns (ok, message)."""
    listed = hookio._git_bytes(root, "ls-tree", "-r", "-z", "--name-only", tree)
    if listed is None:
        return False, f"git ls-tree {tree} failed"
    have = {os.fsdecode(x) for x in listed.split(b"\0") if x}
    present = [p for p in paths if p in have]
    if present:
        ok, err = _git_in(root, ["--literal-pathspecs", "restore", f"--source={tree}", "--worktree",
                                 "--pathspec-from-file=-", "--pathspec-file-nul"], _paths_input(present))
        if not ok:
            return False, err or "git restore failed"
    for p in paths:
        if p in have:
            continue
        full = os.path.join(root, p)
        try:
            if os.path.islink(full) or os.path.isfile(full):
                os.unlink(full)
        except OSError as e:
            return False, f"could not set aside {p}: {e}"
    return True, ""


def _verify_staged(root, manifest):
    """`verb verify --staged`: verify exactly the staged content (VERIFYTREE, the pre-commit way [S191])."""
    def fail(nxt, code=2):
        print(common.fail_line("AGENTIVE_FAIL", nxt, verb="verify", exit=code, log="none"))
        return code
    if hookio.git(root, "rev-parse", "--git-dir") is None:
        return fail("run inside a git repository; outside git use .agents/bin/verb verify")
    index = hookio.index_tree(root)
    if not index:
        return fail("resolve the unmerged paths (git status), stage the result, then rerun .agents/bin/verb verify --staged")
    work = hookio.content_tree(root)
    if not work:
        return fail("git could not read the work tree (git status), then rerun .agents/bin/verb verify --staged")
    out = hookio._git_bytes(root, "-c", "core.quotepath=off", "diff", "--name-only", "-z", "--no-renames", index, work)
    if out is None:
        return fail(f"git diff {index} {work} failed; run .agents/bin/verb verify after staging everything")
    paths = sorted({os.fsdecode(x) for x in out.split(b"\0") if x})
    if not paths:
        return _verify(root, manifest, tree=index)
    parent = hookio.git(root, "rev-parse", "--verify", "-q", "HEAD")
    backup = hookio.git(root, "commit-tree", work, *(["-p", parent] if parent else []), "-m", "agentive verify --staged: work content backup")
    if not backup or hookio.git(root, "update-ref", "-m", "agentive verify --staged", BACKUP_REF, backup) is None:
        return fail("git could not record the backup commit; stage everything and run .agents/bin/verb verify")
    recover = f"git restore --source={BACKUP_REF} --worktree -- <path> (or every path: git diff --name-only {index} {BACKUP_REF})"
    print(f"NOTE verify --staged: {len(paths)} path(s) not staged (unstaged changes, untracked files) are set aside and restored "
          f"after the verbs; backup {BACKUP_REF} ({backup[:12]}); if this run is killed, restore with {recover}")
    import signal
    old_term = signal.getsignal(signal.SIGTERM)

    def _term(signum, frame):
        raise KeyboardInterrupt("SIGTERM")
    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError):         # not the main thread
        old_term = None
    rc = 2
    try:
        ok, msg = _set_paths(root, index, paths)
        if not ok:
            print(f"NOTE verify --staged: could not set the paths aside ({msg}); restoring")
        else:
            rc = _verify(root, manifest, tree=index)
    finally:
        ok, msg = _set_paths(root, work, paths)
        now = hookio.content_tree(root) if ok else None
        left = None
        if now:
            d = hookio._git_bytes(root, "diff", "--name-only", "-z", "--no-renames", work, now, "--", *[":(literal)" + p for p in paths])
            left = None if d is None else [x for x in d.split(b"\0") if x]
        if ok and left == []:
            hookio.git(root, "update-ref", "-d", BACKUP_REF)
        else:
            print(f"NOTE verify --staged: the restore was not complete ({msg or 'the work tree differs from the backup'}); "
                  f"the backup {BACKUP_REF} is kept")
            print(common.fail_line("AGENTIVE_FAIL", f"restore the set-aside paths: {recover}; then delete the backup with "
                                   f"git update-ref -d {BACKUP_REF}", verb="verify", exit=2, log="none"))
            rc = 2
        if old_term is not None:
            try:
                signal.signal(signal.SIGTERM, old_term)
            except (ValueError, OSError):
                pass
    return rc


def main(argv):
    root = common.repo_root(os.getcwd()) or os.getcwd()
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: .agents/bin/verb <" + "|".join(VERB_ORDER) + "> [-- args...]   |   .agents/bin/verb verify --staged")
        return 2
    verb, rest = argv[0], argv[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if verb not in VERB_ORDER:
        print(common.fail_line("AGENTIVE_FAIL", "use one of " + ",".join(VERB_ORDER), verb=verb, exit=2, log="none"))
        return 2
    manifest = hookio.load_manifest(root)
    if manifest is None:
        print(common.fail_line("AGENTIVE_FAIL", "ask the user to run /agentive:init (Codex: $agentive-init)",
                               verb=verb, exit=2, log="none"))
        return 2
    if verb == "verify" and not rest:
        return _verify(root, manifest)
    if verb == "verify" and argv[1:] == ["--staged"]:
        return _verify_staged(root, manifest)
    r = run_verb(root, manifest, verb, rest)
    if r.get("pending"):
        return 0
    if not r["ok"]:
        print(r["fail_line"])
        return r["rc"] if 0 < r["rc"] < 256 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
