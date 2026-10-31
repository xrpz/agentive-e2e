# agentive-unit: lib.agentive
# design: spec.enforcement.hooks.repo_root, spec.skill_scripts, docs/IMPLEMENTATION.md §4 [design judgment]
"""Small helpers every agentive script shares (python3 stdlib only, Python >= 3.9).

- repo_root(cwd): `git -C <cwd> rev-parse --show-toplevel` (never CLAUDE_PROJECT_DIR: spec.enforcement.hooks.repo_root)
- sha256_bytes / sha256_text / sha256_file: lowercase hex
- tree_sha256(dir): one hash for a directory (skills.lock, Codex bootstrap check): sha256 over the sorted
  lines "<posix relpath>\\0<file sha256>\\n" of every regular file under dir (symlinks are followed as files)
- read_json / write_json: UTF-8, write_json is atomic (temp file + rename), sorted keys off, indent 2, final newline
- utc_now(): RFC 3339 UTC timestamp with seconds ("2026-10-27T12:00:00Z")
- fail_line(...): the one-line failure format scripts print last (spec.verb_wrapper.fail_line style)
- no_command(names, sep): "<name> has no command" for one verb, "<a>, <b> have no command" for several (E2E-02)
"""
import datetime
import hashlib
import json
import os
import subprocess
import tempfile

__all__ = ["repo_root", "sha256_bytes", "sha256_text", "sha256_file", "tree_sha256",
           "read_json", "write_json", "utc_now", "fail_line", "no_command"]


def repo_root(cwd="."):
    """Top-level directory of the git work tree containing cwd, or None outside git."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_text(s):
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha256(root):
    """Deterministic hash of every regular file under root (relative POSIX paths, sorted). `__pycache__` directories are
    skipped: python writes them next to imported modules, so they would change the hash without a source change (V2-10)."""
    lines = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for name in files:
            p = os.path.join(d, name)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            lines.append(f"{rel}\0{sha256_file(p)}\n")
    lines.sort()
    return sha256_text("".join(lines))


def read_json(path, default=None):
    """Parsed JSON, or `default` when the file does not exist."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail_line(tag, next_action, **fields):
    """`<TAG> k=v ...; NEXT=<next_action>` — one line, last on stdout, for the agent to act on."""
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{tag}{(' ' + kv) if kv else ''}; NEXT={next_action}"


def no_command(names, sep=", "):
    """The pending-verb clause with subject-verb agreement: "typecheck has no command", "lint, typecheck have no command"."""
    names = list(names)
    return f"{sep.join(names)} {'has' if len(names) == 1 else 'have'} no command"
