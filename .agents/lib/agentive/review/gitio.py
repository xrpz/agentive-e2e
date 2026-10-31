# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV1], spec.review_pipeline.rules (read_from the base commit) (owner D)
"""git plumbing for the review pipeline. Every call is `git -C <repo> ...` with LC_ALL=C; nothing else.

A Diff names the two trees it compares: base (a commit sha) and head (a commit sha, or None for the index:
the precommit profile reviews `git diff --cached` on top of HEAD).
"""
import os
import re
import subprocess

ENV = dict(os.environ, LC_ALL="C", GIT_PAGER="cat", GIT_CONFIG_NOSYSTEM="1")


class GitError(RuntimeError):
    pass


def git(repo, *args, check=True, binary=False):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, env=ENV)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def ok(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, env=ENV).returncode == 0


def rev_parse(repo, rev):
    return git(repo, "rev-parse", "--verify", rev + "^{commit}").strip()


def merge_base(repo, a, b):
    out = git(repo, "merge-base", a, b, check=False).strip()
    return out or None


def is_ancestor(repo, a, b):
    return ok(repo, "merge-base", "--is-ancestor", a, b)


class Diff:
    """Changed files between base and head (head None = the index, the staged change)."""

    def __init__(self, repo, base, head=None):
        self.repo, self.base, self.head = repo, base, head

    def _range(self):
        return ["--cached", self.base] if self.head is None else [self.base, self.head]

    def name_status(self):
        """[(status letter, path, old_path or None)] with renames detected (-M)."""
        out = git(self.repo, "diff", "--no-color", "-M", "--name-status", "-z", *self._range())
        parts = out.split("\0")
        res, i = [], 0
        while i < len(parts) and parts[i]:
            st = parts[i]
            letter = st[0]
            if letter in "RC":
                res.append((letter, parts[i + 2], parts[i + 1]))
                i += 3
            else:
                res.append((letter, parts[i + 1], None))
                i += 2
        return res

    def numstat(self):
        """{path: (added, deleted)}; binary files count 0/0."""
        out = git(self.repo, "diff", "--no-color", "-M", "--numstat", "-z", *self._range())
        res, parts, i = {}, out.split("\0"), 0
        while i < len(parts) and parts[i]:
            fields = parts[i].split("\t")
            if len(fields) == 3 and fields[2] == "":        # rename: "a\td\t" then old, new
                a, d = fields[0], fields[1]
                path = parts[i + 2]
                i += 3
            else:
                a, d, path = fields[0], fields[1], fields[2]
                i += 1
            res[path] = (0 if a == "-" else int(a), 0 if d == "-" else int(d))
        return res

    def patch(self, paths=None, context=0, ignore_ws=False):
        args = ["diff", "--no-color", "-M", f"-U{context}"]
        if ignore_ws:
            args += ["-w", "--ignore-blank-lines"]
        args += self._range()
        if paths:
            args += ["--"] + list(paths)
        return git(self.repo, *args)

    def hunks(self, paths=None):
        """{path: {"added": [(line_no, text)], "removed": [(line_no, text)]}} from a -U0 patch."""
        return parse_patch(self.patch(paths, context=0))

    def rename_map(self, old_sha):
        """{old path: new path} of the files renamed between old_sha (an earlier head) and this diff's head, git diff -M.
        Empty when old_sha is unknown here (a force-push) or the head is the index."""
        if not old_sha or self.head is None or not re.fullmatch(r"[0-9a-f]{7,64}", str(old_sha)):
            return {}
        r = subprocess.run(["git", "-C", self.repo, "diff", "--no-color", "-M", "--name-status", "-z", old_sha, self.head],
                           capture_output=True, env=ENV)
        if r.returncode != 0:
            return {}
        parts, res, i = r.stdout.decode("utf-8", "replace").split("\0"), {}, 0
        while i < len(parts) and parts[i]:
            letter = parts[i][0]
            if letter in "RC":
                if letter == "R":
                    res[parts[i + 1]] = parts[i + 2]
                i += 3
            else:
                i += 2
        return res

    def head_rev(self):
        return "HEAD" if self.head is None else self.head

    def show_head(self, path):
        """Content of path in the head tree (the index for precommit); None when absent."""
        spec = f":{path}" if self.head is None else f"{self.head}:{path}"
        r = subprocess.run(["git", "-C", self.repo, "show", spec], capture_output=True, env=ENV)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    def show_base(self, path):
        return show(self.repo, self.base, path)

    def exists_head(self, path):
        spec = f":{path}" if self.head is None else f"{self.head}:{path}"
        return ok(self.repo, "cat-file", "-e", spec)


def show(repo, rev, path):
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True, env=ENV)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def exists(repo, rev, path):
    return ok(repo, "cat-file", "-e", f"{rev}:{path}")


def ls_tree(repo, rev):
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", rev, check=False)
    return [p for p in out.split("\0") if p]


def ls_index(repo):
    out = git(repo, "ls-files", "-z", check=False)
    return [p for p in out.split("\0") if p]


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    res, cur, old_ln, new_ln = {}, None, 0, 0
    for ln in text.splitlines():
        if ln.startswith("diff --git "):
            cur = None
            continue
        if ln.startswith("+++ "):
            p = ln[4:]
            if p == "/dev/null":
                continue
            cur = p[2:] if p.startswith("b/") else p
            res.setdefault(cur, {"added": [], "removed": []})
            continue
        if ln.startswith("--- "):
            p = ln[4:]
            if p != "/dev/null":
                old = p[2:] if p.startswith("a/") else p
                res.setdefault(old, {"added": [], "removed": []})
                cur = old                         # overwritten by +++ unless the file is deleted
            continue
        m = _HUNK.match(ln)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            continue
        if cur is None:
            continue
        if ln.startswith("+"):
            res[cur]["added"].append((new_ln, ln[1:]))
            new_ln += 1
        elif ln.startswith("-"):
            res[cur]["removed"].append((old_ln, ln[1:]))
            old_ln += 1
        elif ln.startswith(" "):
            old_ln += 1
            new_ln += 1
    return res


def commit_time(repo, rev):
    """Committer time of rev as a unix timestamp (the reference 'now' of history signals)."""
    return int(git(repo, "log", "-1", "--format=%ct", rev).strip())


def log_first_parent(repo, rev, paths, since_ts, until_ts):
    """[(sha, parents, unix time, subject, body, files)] of the commits on the first-parent history of rev that touch paths in
    (since, until] (E2E-13). One `git log --first-parent --name-only` call: merge commits stay in the list (their message
    names a merged revert or hotfix, and each record is one change landed on the base for the rubric@2 churn count),
    `parents` holds the parent shas (len > 1 for a merge), `files` are the touched paths among `paths` (`--first-parent`
    makes a merge list the files changed against its first parent, i.e. the files of the merged PR; pinned by
    tests/review/test_stages.py test_e2e_13_churn_git_merge_lists_pr_files)."""
    if not paths:
        return []
    out = git(repo, "log", "--no-color", "-z", "--first-parent", "--name-only", "--format=%x1e%H%x1f%P%x1f%ct%x1f%s%x1f%b%x1f",
              f"--since=@{since_ts}", f"--until=@{until_ts}", rev, "--", *paths, check=False)
    want = set(paths)
    res = []
    for rec in out.split("\x1e"):
        f = rec.split("\x1f")
        if len(f) < 6:
            continue
        files = [n.strip("\n") for n in f[5].split("\0")]
        res.append((f[0].strip(), f[1].split(), int(f[2]), f[3], f[4], [n for n in files if n in want]))
    return res


def log_messages(repo, rev, since_ts):
    """[(sha, unix time, full message)] reachable from rev since since_ts (kill-switch revert detection)."""
    out = git(repo, "log", "--no-color", "-z", "--format=%H%x1f%ct%x1f%B", f"--since=@{since_ts}", rev, check=False)
    res = []
    for rec in out.split("\0"):
        rec = rec.strip("\n")
        if not rec:
            continue
        f = rec.split("\x1f", 2)
        if len(f) == 3:
            res.append((f[0], int(f[1]), f[2]))
    return res


def grep_word(repo, rev, word):
    """True when `word` occurs as a whole word in any tracked file of rev (git grep -w)."""
    if rev is None:
        return ok(repo, "grep", "-q", "-w", "-F", "--cached", "-e", word)
    return ok(repo, "grep", "-q", "-w", "-F", "-e", word, rev)
