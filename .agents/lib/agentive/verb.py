# agentive-unit: lib.agentive
# design: spec.verb_wrapper, contracts[verify-result], spec.init_apply.empty_dir.pending.consumers, spec.verification.E1c, decisions.accepted[VERIFYTREE], decisions.accepted[HEADLESSHINT] · owner B
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
  refs/agentive/verify-staged until the restore is checked (the backup commit uses a fixed internal identity and no signing, so
  it needs no git user.name/user.email). content_tree is then the index tree (`git write-tree`)
- `verb lint -- <file>...` runs the lint command on those files only: a trailing whole-repo target (`.`, `./...`, a
  manifest src/tests path) is replaced by them, else they are appended (agentive.lintscope.file_command, E2E-04); the
  PostToolUse lint hook lints only the edited file
- extra args after `--` are appended shell-quoted [design judgment: lets the test-rewrite hook keep arguments]
- scratch caches (E2E-32 sandbox follow-up, decisions.accepted[HEADLESSHINT]): when the command uses uv, ruff, pytest or
  npm and that tool's default cache directory cannot be written (a real write test: the Claude Code sandbox lets a command
  write only the project and temp directories), the verb points the tool at `.agents/state/cache/<tool>` (git-ignored) with
  the tool's own setting and prints one NOTE line; a variable the user set is never replaced. Dependency-store caches
  (cargo, go, gradle) are not moved: a failed verb whose log has a refused write (`Read-only file system`, `Operation not permitted`, EROFS, EPERM)
  ends with a NEXT naming the sandbox.filesystem.allowWrite entry to add (unverified on a sandboxed machine); E2E-37: only a log
  line that has the denial AND a cache or store path of a tool in the command counts, so test output that merely says
  `Operation not permitted` keeps the normal grep NEXT
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


# scratch-cache tool -> the setting that moves its cache (pytest: `-o cache_dir=` appended to PYTEST_ADDOPTS) [design judgment]
SCRATCH_CACHES = {
    "uv": "UV_CACHE_DIR",
    "ruff": "RUFF_CACHE_DIR",
    "npm": "npm_config_cache",
    "pytest": "PYTEST_ADDOPTS",
}
# dependency stores: moving them would hide what `setup` installed, so a blocked write gets a NEXT instead (paths unverified)
STORES = {"cargo": "~/.cargo", "go": "~/go/pkg/mod and ~/.cache/go-build", "gradle": "~/.gradle", "./gradlew": "~/.gradle",
          "mvn": "~/.m2", "./mvnw": "~/.m2"}
# E2E-37: a refused write counts only on a log line that names a denial AND a path of the tool's own cache or dependency store;
# the words alone (a test that chmods a file prints "Operation not permitted") do not
BLOCKED_WRITE = re.compile(r"Read-only file system|Operation not permitted|\bEROFS\b|\bEPERM\b", re.I)
STORE_PATHS = {                                 # tool -> regex of the tool's own cache or store paths [design judgment: the list]
    "uv": r"[/\\]\.cache[/\\]uv\b|[/\\]\.local[/\\]share[/\\]uv\b",
    "ruff": r"\.ruff_cache",
    "npm": r"[/\\]\.npm\b|_cacache",
    "pytest": r"\.pytest_cache",
    "cargo": r"[/\\]\.cargo\b",
    "go": r"[/\\]go[/\\]pkg[/\\]mod|go-build",
    "gradle": r"[/\\]\.gradle\b",
    "./gradlew": r"[/\\]\.gradle\b",
    "mvn": r"[/\\]\.m2\b",
    "./mvnw": r"[/\\]\.m2\b",
}


def _tools(cmd):
    words = set(re.findall(r"(?<![\w./-])(\./gradlew|\./mvnw|[a-z][a-z0-9_-]*)(?![\w-])", cmd or ""))
    if "npx" in words:
        words.add("npm")
    return words


def _default_cache(tool, root, env):
    home = os.path.expanduser("~")
    if tool == "uv":
        base = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
        return os.path.join(base, "uv")
    if tool == "npm":
        return os.path.join(home, ".npm")
    if tool == "ruff":
        return os.path.join(root, ".ruff_cache")
    if tool == "pytest":
        return os.path.join(root, ".pytest_cache")
    return None


def _writable(path):
    """True when a file can be created in `path` (or in its nearest existing parent when `path` does not exist yet)."""
    probe_dir = path
    while probe_dir and not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            return False
        probe_dir = parent
    probe = os.path.join(probe_dir, f".agentive-write-test-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("")
        os.unlink(probe)
        return True
    except OSError:
        return False


def cache_env(root, cmd, env):
    """(env, notes): env with the fallback cache settings for the scratch-cache tools in `cmd` whose default cache cannot be
    written; notes = one `NOTE cache:` line per moved cache."""
    env, notes = dict(env), []
    for tool in sorted(_tools(cmd) & set(SCRATCH_CACHES)):
        var = SCRATCH_CACHES[tool]
        if tool != "pytest" and env.get(var):
            continue
        default = _default_cache(tool, root, env)
        if default is None or _writable(default):
            continue
        rel = f".agents/state/cache/{tool}"
        try:
            os.makedirs(os.path.join(root, rel), exist_ok=True)
        except OSError:
            continue
        if tool == "pytest":
            if "cache_dir" in env.get(var, ""):
                continue
            env[var] = (env.get(var, "") + f" -o cache_dir={rel}").strip()
        else:
            env[var] = os.path.join(root, rel)
        notes.append(f"NOTE cache: {tool}'s cache {default} is not writable here (sandbox or read-only home); this run uses {rel}")
    return env, notes


def _store_regex(cmd, root=None, env=None):
    """Regex for the cache or store paths of the tools named in `cmd`: the fixed paths above, the default cache of a scratch-cache
    tool, a cache directory set in the environment and the fallback directory `.agents/state/cache/`; None when no tool is known."""
    env = os.environ if env is None else env
    parts = []
    for tool in sorted(_tools(cmd) & set(STORE_PATHS)):
        parts.append(STORE_PATHS[tool])
        default = _default_cache(tool, root or os.getcwd(), env) if tool in SCRATCH_CACHES else None
        for lit in (default, env.get(SCRATCH_CACHES.get(tool, ""), "") if tool != "pytest" else ""):
            if lit and len(lit) > 1:
                parts.append(re.escape(lit.rstrip("/")))
        if tool in SCRATCH_CACHES:
            parts.append(re.escape(".agents/state/cache/" + tool))
    return re.compile("|".join(parts)) if parts else None


def blocked_write_match(cmd, data, root=None, env=None):
    """The denial phrase of the first log line that is a refused write into a cache or store path of a tool in `cmd`, or None."""
    store = _store_regex(cmd, root, env)
    if store is None:
        return None
    for line in (data or "").splitlines():
        m = BLOCKED_WRITE.search(line)
        if m and store.search(line):
            return m.group(0)
    return None


def blocked_write_next(cmd, data, root=None, env=None):
    """The NEXT for a failed verb whose log shows a write the sandbox refused in a tool's cache or store, or None (E2E-37: the
    words alone in test output do not count)."""
    phrase = blocked_write_match(cmd, data, root, env)
    if not phrase:
        return None
    stores = [STORES[t] for t in sorted(_tools(cmd) & set(STORES))]
    where = stores[0] if stores else "the directory named in the log"
    return (f"the sandbox refused a write ({phrase}): add {where} to sandbox.filesystem.allowWrite "
            "(the --settings JSON of the headless standard command, or .claude/settings.json), or run the verb outside the sandbox; "
            "the log has the path")


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
            env, notes = cache_env(root, cmd, dict(os.environ, AGENTIVE_VERB=verb))
            for n in notes:
                log.write((n + "\n").encode("utf-8"))
                if not quiet:
                    print(n)
            log.flush()
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
        nxt = blocked_write_next(cmd, data.decode("utf-8", "replace"), root, env) or f"grep -n -m20 -E {shlex.quote(grep)} {rel}"
        res["fail_line"] = common.fail_line("AGENTIVE_FAIL", nxt, verb=verb, exit=rc, log=rel)
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


BACKUP_IDENT = {"GIT_AUTHOR_NAME": "agentive", "GIT_AUTHOR_EMAIL": "agentive@localhost",
                "GIT_COMMITTER_NAME": "agentive", "GIT_COMMITTER_EMAIL": "agentive@localhost"}


def _backup_commit(root, work, parent):
    """Commit object of the work content tree for BACKUP_REF, or None. [design judgment] The commit gets a fixed internal identity
    (BACKUP_IDENT, set in the environment of this one git call) and `commit.gpgsign=false`: a user repo or a CI runner often
    has no user.name/user.email (git then refuses `commit-tree`), and a configured signing key must not be asked for an
    internal ref. The user's own git config is never read for or changed by this commit."""
    cmd = ["git", "-C", root, "-c", "commit.gpgsign=false", "commit-tree", work]
    if parent:
        cmd += ["-p", parent]
    cmd += ["-m", "agentive verify --staged: work content backup"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=dict(os.environ, **BACKUP_IDENT))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


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
    backup = _backup_commit(root, work, parent)
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
