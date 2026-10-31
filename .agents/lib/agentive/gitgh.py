# agentive-unit: lib.agentive
# design: spec.spec_stage.chain.start_check.default_branch, spec.outer_loop.cloud, spec.work_loop.pr_feedback.reads, spec.no_human_run, spec.work_loop.pr_skill.base_ref, decisions.accepted[BASEREF]
"""git and `gh api` helpers for the work-loop skill scripts (owner C; python3 stdlib only).

- run(args, cwd): subprocess with captured text output; never raises on a non-zero exit
- default_branch(root): origin/HEAD via `git symbolic-ref` when the ref it points to exists, else `git ls-remote
  --symref origin HEAD`, else None (A9_fallback; no gh api step: BASEREF, E2E-06)
- base_ref(root, fetch=True): the default branch name and its remote-tracking ref, fetched once with the explicit
  refspec `+refs/heads/<d>:refs/remotes/origin/<d>` so a clone without a fetch refspec (an empty repository cloned
  with --depth or --single-branch) still gets it; NOTE lines and the stop line when the ref cannot be had (BASEREF)
- every network git call runs with GIT_TERMINAL_PROMPT=0, so a missing credential fails instead of waiting
- origin_repo(root): "owner/repo" parsed from the origin URL (no network), else None
- gh(args) / gh_json(endpoint, ...) / gh_pages(endpoint): `gh api` only (works in cloud sessions and Routines
  runs where `gh pr` is blocked); gh_pages follows --paginate and merges the concatenated JSON arrays
- no_human(): the run has no human present (AGENTIVE_HEADLESS=1, CI or GITHUB_ACTIONS); hook-only signals
  (permission_mode dontAsk) are not visible to scripts, so their absence counts as interactive
"""
import json
import os
import re
import subprocess

__all__ = ["run", "git", "default_branch", "remote_head", "base_ref", "origin_repo", "gh", "gh_json", "gh_pages", "no_human",
           "GhError"]


class GhError(RuntimeError):
    pass


def run(args, cwd=None, input_text=None, env=None):
    try:
        p = subprocess.run(args, cwd=cwd, input=input_text, capture_output=True, text=True, env=env)
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def git(root, *args):
    """stdout of a git command (stripped), or None on failure."""
    rc, out, _ = run(["git", "-C", root] + list(args))
    return out.strip() if rc == 0 else None


def _net_env():
    return dict(os.environ, GIT_TERMINAL_PROMPT="0")


def remote_head(root):
    """The branch origin's HEAD names now (`git ls-remote --symref origin HEAD`), or None (offline, no access, unborn)."""
    rc, out, _ = run(["git", "-C", root, "ls-remote", "--symref", "origin", "HEAD"], env=_net_env())
    if rc == 0:
        m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", out, re.M)
        if m:
            return m.group(1)
    return None


def default_branch(root):
    """origin/HEAD when the ref it points to exists (a dangling origin/HEAD, left by a rename and `fetch --prune`, is
    skipped), else the remote's HEAD; None when both fail."""
    ref = git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ref and ref.startswith("refs/remotes/origin/") and git(root, "rev-parse", "--verify", "--quiet", ref):
        return ref[len("refs/remotes/origin/"):]
    return remote_head(root)


def _fetch(root, name):
    rc, out, err = run(["git", "-C", root, "fetch", "--quiet", "--no-tags", "origin",
                        f"+refs/heads/{name}:refs/remotes/origin/{name}"], env=_net_env())
    return rc == 0, (err or out).strip()


def base_ref(root, fetch=True):
    """{name, ref, notes[], stop} for the default branch (BASEREF, E2E-06). stop is None or the full
    `AGENTIVE_PR=stop reason=...; NEXT=...` line; notes are `NOTE ...` lines to print before it (stdout).
    1. name = default_branch(root)                      none -> stop no_default_branch
    2. fetch once: git fetch --quiet --no-tags origin +refs/heads/<d>:refs/remotes/origin/<d> (GIT_TERMINAL_PROMPT=0)
       - the remote has no refs/heads/<d> (a stale origin/HEAD after a rename): name again with ls-remote, fetch once more
       - success: git remote set-head origin <d> when origin/HEAD is missing or names another branch
       - failure with a local ref: NOTE, continue on the local ref (may be stale)
    3. no refs/remotes/origin/<d> -> stop base_ref_missing (refspec=none when remote.origin.fetch is empty, with a NOTE)"""
    draft = "rerun sh .agents/skills/pr/scripts/pr-body.sh draft"
    notes = []
    name = default_branch(root)
    if not name:
        return {"name": None, "ref": None, "notes": notes,
                "stop": f"AGENTIVE_PR=stop reason=no_default_branch; NEXT=check that origin is reachable (git ls-remote --symref origin HEAD), then {draft}"}
    fetched = None
    if fetch:
        fetched, err = _fetch(root, name)
        if not fetched and "couldn't find remote ref" in err:
            new = remote_head(root)
            if new and new != name:
                notes.append(f"NOTE origin/HEAD={name} remote HEAD={new}; using {new} (refresh with git remote set-head origin --auto)")
                name = new
                fetched, err = _fetch(root, name)
        if fetched:
            if git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD") != f"refs/remotes/origin/{name}":
                git(root, "remote", "set-head", "origin", name)
    ref = f"refs/remotes/origin/{name}"
    if not git(root, "rev-parse", "--verify", "--quiet", ref):
        refspec = (git(root, "config", "--get-all", "remote.origin.fetch") or "").replace("\n", ",") or "none"
        if refspec == "none":
            notes.append("NOTE remote.origin.fetch is empty (a --single-branch or --depth clone, for example of an empty repository); "
                         "to track every branch: git remote set-branches origin '*' && git fetch origin")
        state = "failed" if fetched is False else ("skipped" if fetched is None else "ok")
        return {"name": name, "ref": ref, "notes": notes,
                "stop": f"AGENTIVE_PR=stop reason=base_ref_missing ref={ref} fetch={state} refspec={refspec}; "
                        f"NEXT=git fetch origin +refs/heads/{name}:{ref}, then {draft}"}
    if fetched is False:
        notes.append(f"NOTE git fetch of {name} failed; using the local origin/{name} (may be stale)")
    return {"name": name, "ref": ref, "notes": notes, "stop": None}


_REMOTE = re.compile(r"(?:[:/])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def origin_repo(root):
    url = git(root, "remote", "get-url", "origin")
    if not url:
        return None
    m = _REMOTE.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def gh(args, input_text=None, cwd=None):
    """stdout of `gh api <args>`; raises GhError with stderr on failure."""
    rc, out, err = run(["gh", "api"] + list(args), cwd=cwd, input_text=input_text)
    if rc != 0:
        raise GhError((err or out).strip().splitlines()[-1] if (err or out).strip() else f"gh api exit {rc}")
    return out


def gh_json(endpoint, *extra, cwd=None):
    out = gh([endpoint] + list(extra), cwd=cwd)
    return json.loads(out) if out.strip() else None


def gh_pages(endpoint, *extra, cwd=None):
    """All items of a paginated REST list (gh prints one JSON array per page)."""
    out = gh([endpoint, "--paginate"] + list(extra), cwd=cwd)
    items, dec, i = [], json.JSONDecoder(), 0
    while True:
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            break
        obj, i = dec.raw_decode(out, i)
        items.extend(obj if isinstance(obj, list) else [obj])
    return items


def no_human(env=None):
    env = os.environ if env is None else env
    return env.get("AGENTIVE_HEADLESS") == "1" or bool(env.get("CI")) or bool(env.get("GITHUB_ACTIONS"))
