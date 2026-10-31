# agentive-unit: lib.agentive
# design: spec.enforcement.hooks (repo_root), spec.init_apply.session_start, spec.stop_hook, spec.verification.E1c, decisions.accepted[VERIFYTREE] · owner B
"""Shared plumbing for the installed hook scripts (.agents/hooks/*.py) and .agents/bin/verb.

- read_input(): hook input JSON from stdin ({} when empty or not JSON)
- root_from(inp): repo root from the hook input cwd (`git -C <cwd> rev-parse --show-toplevel`), never from
  CLAUDE_PROJECT_DIR (it keeps pointing at the project root inside a worktree); falls back to cwd
- load_manifest(root): the agentive manifest (.agents/manifest.yaml) as a dict, or None
- emit(obj): print one JSON object on stdout (the only stdout of a JSON-speaking hook)
- tool_arg(argv): "codex" when the script got --tool codex (Codex hooks.json passes it), else "claude"
- state_dir(root): .agents/state (created on demand)
- content_tree(root): the git tree id of the work content (tracked files as they are on disk, staged or not, plus
  untracked files that are not ignored; .agents/state excluded): a copy of the index that keeps its mtime, `git add -A`
  of the changed paths into the copy, `git write-tree` (VERIFYTREE, E2E-08). No commit sha is part of it: the same content has the same id
  before and after a commit. None outside git, with an unmerged index or on any git failure (callers then do not reuse)
- index_tree(root): `git write-tree` of the real index (the content a commit would record), None on failure
"""
import hashlib
import json
import os
import subprocess
import sys

from . import ayaml, common

__all__ = ["read_input", "root_from", "load_manifest", "emit", "tool_arg", "state_dir", "git", "head_sha",
           "current_branch", "default_branch", "content_tree", "index_tree"]


def read_input(stream=None):
    stream = sys.stdin if stream is None else stream
    try:
        data = stream.read()
    except (OSError, ValueError):
        return {}
    if not data.strip():
        return {}
    try:
        obj = json.loads(data)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def root_from(inp):
    cwd = inp.get("cwd") if isinstance(inp, dict) else None
    cwd = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    return common.repo_root(cwd) or os.path.abspath(cwd)


def load_manifest(root):
    path = os.path.join(root, ".agents", "manifest.yaml")
    if not os.path.isfile(path):
        return None
    try:
        doc = ayaml.load(path)
    except Exception:  # noqa: BLE001 - a broken manifest must not crash a hook; callers treat None as absent
        return None
    return doc if isinstance(doc, dict) else None


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tool_arg(argv):
    for i, a in enumerate(argv):
        if a == "--tool" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--tool="):
            return a.split("=", 1)[1]
    return "claude"


def state_dir(root):
    d = os.path.join(root, ".agents", "state")
    os.makedirs(d, exist_ok=True)
    return d


def git(root, *args):
    """stdout of a git command in root (stripped), or None on any failure."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def head_sha(root):
    return git(root, "rev-parse", "HEAD")


def current_branch(root):
    """Branch name, or None on a detached HEAD / outside git."""
    b = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return b or None


def default_branch(root):
    """Default branch from the real remote (origin/HEAD); None when unknown [design judgment: callers then use main/master]."""
    ref = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    return None


def _git_bytes(root, *args, env=None):
    try:
        r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


STATE_EXCLUDE = ":(exclude).agents/state"


def index_tree(root):
    """`git write-tree` of the real index: the tree a commit made now would record. None on failure (unmerged paths)."""
    out = _git_bytes(root, "write-tree")
    return out.decode().strip() if out else None


def content_tree(root):
    """Tree id of the work content (spec.stop_hook.fingerprint, verification.E1c; VERIFYTREE): tracked files as on disk
    (staged and unstaged), untracked files that are not ignored, `.agents/state/**` excluded. Built in a scratch index
    inside the git dir: a copy of the real index that keeps its mtime (shutil.copy2: a copy with a new mtime weakens
    git's racy-clean check and can miss a same-size edit [S190]), or `git read-tree` of HEAD (empty when unborn) when
    there is no index yet; then `git add -A` of the paths `git ls-files --others --exclude-standard --modified
    --deleted` names and `git write-tree` [S189]. None when git cannot answer."""
    import shutil
    import tempfile
    gd = _git_bytes(root, "rev-parse", "--absolute-git-dir")
    idx = _git_bytes(root, "rev-parse", "--git-path", "index")      # honours GIT_INDEX_FILE; relative to root
    if gd is None or idx is None:
        return None
    idx = os.path.join(root, os.fsdecode(idx.strip()))
    fd, tmp = tempfile.mkstemp(prefix="agentive-content-", suffix=".index", dir=os.fsdecode(gd.strip()))
    os.close(fd)
    env = dict(os.environ, GIT_INDEX_FILE=tmp)
    try:
        if os.path.isfile(idx):
            shutil.copy2(idx, tmp)
        else:
            os.unlink(tmp)
            head = _git_bytes(root, "rev-parse", "--verify", "-q", "HEAD")
            if _git_bytes(root, *(["read-tree", "HEAD"] if head else ["read-tree", "--empty"]), env=env) is None:
                return None
        # `git add -A -- . ':(exclude).agents/state'` fails when .agents/state is ignored (git names the ignored
        # pathspec); list the changed paths with ls-files, which takes the exclude, and add exactly those
        listed = _git_bytes(root, "ls-files", "-z", "--others", "--exclude-standard", "--modified", "--deleted",
                            "--", ".", STATE_EXCLUDE, env=env)
        if listed is None:
            return None
        if listed.strip(b"\0"):
            try:
                r = subprocess.run(["git", "-C", root, "--literal-pathspecs", "add", "-A", "--pathspec-from-file=-",
                                    "--pathspec-file-nul"], input=listed, capture_output=True, timeout=300, env=env)
            except (OSError, subprocess.SubprocessError):
                return None
            if r.returncode != 0:
                return None
        out = _git_bytes(root, "write-tree", env=env)
        return out.decode().strip() if out else None
    except OSError:
        return None
    finally:
        for f in (tmp, tmp + ".lock"):
            try:
                os.unlink(f)
            except OSError:
                pass
