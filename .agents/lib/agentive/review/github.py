# agentive-unit: lib.agentive
# design: spec.outer_loop.cloud, spec.review_pipeline.severity_blocking.reply_line.where, spec.review_pipeline.stages[RV8].rules, spec.work_loop.pr_explainer.add_later (owner D)
"""GitHub access through `gh api` (REST) only; GraphQL (`gh pr`, `gh issue`) is blocked in cloud sessions [S7].
The one exception is enabling auto-merge, which has no REST endpoint: `gh pr merge --auto` [S36] (gate job only).

Every function takes the repo slug `owner/name`. Errors raise GhError with gh's message.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import zipfile


class GhError(RuntimeError):
    pass


def _gh(args, data=None, binary=False):
    try:
        r = subprocess.run(["gh"] + args, input=data, capture_output=True)
    except FileNotFoundError:
        raise GhError("gh is not installed")
    if r.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}: {r.stderr.decode('utf-8', 'replace').strip()[:500]}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def api(path, method="GET", body=None, paginate=False):
    args = ["api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if method != "GET":
        args += ["-X", method]
    if paginate:
        args += ["--paginate", "--slurp"]
    data = None
    if body is not None:
        args += ["--input", "-"]
        data = json.dumps(body).encode("utf-8")
    out = _gh(args + [path], data=data)
    if not out.strip():
        return None
    v = json.loads(out)
    if paginate and isinstance(v, list) and all(isinstance(x, list) for x in v):
        return [y for x in v for y in x]
    return v


def slug_from_env(repo_dir=None):
    s = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GH_REPO")
    if s:
        return s
    if repo_dir:
        r = subprocess.run(["git", "-C", repo_dir, "remote", "get-url", "origin"], capture_output=True, text=True)
        url = r.stdout.strip()
        for pre in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
            if url.startswith(pre):
                return url[len(pre):].removesuffix(".git")
    return None


def pull(slug, n):
    """The raw PR object (REST `GET /repos/{owner}/{repo}/pulls/{n}`), open or closed."""
    return api(f"repos/{slug}/pulls/{n}")


def pr_context(slug, n):
    p = pull(slug, n)
    return {
        "number": p["number"], "title": p.get("title") or "", "body": p.get("body") or "",
        "author": (p.get("user") or {}).get("login"), "author_type": (p.get("user") or {}).get("type"),
        "author_association": p.get("author_association"),
        "is_fork": ((p.get("head") or {}).get("repo") or {}).get("full_name") != ((p.get("base") or {}).get("repo") or {}).get("full_name"),
        "labels": [lab.get("name") for lab in p.get("labels") or []],
        "base_sha": p["base"]["sha"], "head_sha": p["head"]["sha"], "base_ref": p["base"]["ref"],
        "state": p.get("state"), "merged": p.get("merged"),
    }


def review_comments(slug, n):
    return api(f"repos/{slug}/pulls/{n}/comments?per_page=100", paginate=True) or []


def create_review_comment(slug, n, commit, path, line, body):
    """Inline comment on the head commit; a line outside the diff falls back to a file-level comment."""
    try:
        return api(f"repos/{slug}/pulls/{n}/comments", "POST",
                   {"body": body, "commit_id": commit, "path": path, "line": line, "side": "RIGHT"})
    except GhError:
        return api(f"repos/{slug}/pulls/{n}/comments", "POST",
                   {"body": body, "commit_id": commit, "path": path, "subject_type": "file"})


def upsert_issue_comment(slug, n, marker, body):
    """Create or update in place the PR comment that carries marker."""
    for c in api(f"repos/{slug}/issues/{n}/comments?per_page=100", paginate=True) or []:
        if marker in (c.get("body") or ""):
            return api(f"repos/{slug}/issues/comments/{c['id']}", "PATCH", {"body": body})
    return api(f"repos/{slug}/issues/{n}/comments", "POST", {"body": body})


def patch_pr_body(slug, n, body):
    return api(f"repos/{slug}/pulls/{n}", "PATCH", {"body": body})


def add_labels(slug, n, labels):
    return api(f"repos/{slug}/issues/{n}/labels", "POST", {"labels": list(labels)})


def remove_label(slug, n, label):
    """Remove one label from a PR; a label that is not on it is not an error."""
    try:
        return api(f"repos/{slug}/issues/{n}/labels/{urllib.parse.quote(label, safe='')}", "DELETE")
    except GhError as e:
        if "404" in str(e) or "Label does not exist" in str(e):
            return None
        raise


def demotion_issues(slug, label):
    return [{"number": i["number"], "state": i.get("state"), "closed_at": i.get("closed_at"), "body": i.get("body") or ""}
            for i in api(f"repos/{slug}/issues?state=all&labels={label}&per_page=100", paginate=True) or []
            if "pull_request" not in i]


def create_issue(slug, title, body, labels):
    return api(f"repos/{slug}/issues", "POST", {"title": title, "body": body, "labels": list(labels)})


def merged_prs(slug, base_ref, since_iso, max_pages=5):
    """Merged PRs into base_ref updated since since_iso: [{number, author, merged_at, merge_commit_sha, body}]."""
    out = []
    for page in range(1, max_pages + 1):
        batch = api(f"repos/{slug}/pulls?state=closed&base={base_ref}&sort=updated&direction=desc&per_page=100&page={page}") or []
        for p in batch:
            if p.get("merged_at"):
                out.append({"number": p["number"], "author": (p.get("user") or {}).get("login"), "merged_at": p["merged_at"],
                            "merge_commit_sha": p.get("merge_commit_sha"), "body": p.get("body") or ""})
        if len(batch) < 100 or (batch and (batch[-1].get("updated_at") or "") < since_iso):
            break
    return out


def _merged_entry(p):
    return {"number": p["number"], "author": (p.get("user") or {}).get("login"), "merged_at": p["merged_at"],
            "merge_commit_sha": p.get("merge_commit_sha"), "body": p.get("body") or ""}


def prs_for_commit(slug, sha):
    """Merged PRs associated with a commit (REST `GET /repos/{owner}/{repo}/commits/{sha}/pulls`), in merged_prs form."""
    return [_merged_entry(p) for p in api(f"repos/{slug}/commits/{sha}/pulls?per_page=100") or [] if p.get("merged_at")]


def merged_pull(slug, n):
    """One PR in merged_prs form, or None when it is not merged (E2E-07: distinct from pull(), which returns the raw PR)."""
    p = api(f"repos/{slug}/pulls/{n}") or {}
    return _merged_entry(p) if p.get("merged_at") else None


def merge_method(slug):
    r = api(f"repos/{slug}")
    for key, m in (("allow_squash_merge", "--squash"), ("allow_merge_commit", "--merge"), ("allow_rebase_merge", "--rebase")):
        if r.get(key):
            return m
    return "--merge"


def enable_auto_merge(slug, n):
    """gh pr merge --auto [S36]; GitHub merges once every merge requirement is met [S35]. Method: the first one the
    repository allows, in the order squash, merge, rebase [design judgment]."""
    return _gh(["pr", "merge", str(n), "--auto", merge_method(slug), "--repo", slug])


def download_artifact(slug, name, dest):
    """Extract the newest unexpired artifact named `name` into dest; False when there is none."""
    res = api(f"repos/{slug}/actions/artifacts?name={name}&per_page=20") or {}
    arts = sorted([a for a in res.get("artifacts") or [] if not a.get("expired")], key=lambda a: a.get("created_at") or "", reverse=True)
    if not arts:
        return False
    data = _gh(["api", f"repos/{slug}/actions/artifacts/{arts[0]['id']}/zip"], binary=True)
    tmp = tempfile.mkdtemp(prefix="agentive-art-")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.namelist():
                if m.startswith("/") or ".." in m.split("/"):
                    continue
                z.extract(m, tmp)
        os.makedirs(dest, exist_ok=True)
        for root, _, files in os.walk(tmp):
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, tmp)
                os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
                shutil.copyfile(src, os.path.join(dest, rel))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True
