# agentive-unit: lib.agentive
# design: spec.review_pipeline.severity_blocking, spec.review_pipeline.severity_blocking.reply_line, spec.review_pipeline.convergence, spec.review_pipeline.convergence.finding_identity, spec.review_pipeline.stages[RV7], spec.review_pipeline.stages[RV6] (owner D)
"""Findings: evidence filter, finding identity, RV7 verdicts, RV6 coordination, agentive-reply lines, convergence.

Finding identity (FINDINGID, user delegation 2026-10-29): a finding id is minted ONCE, at the first sighting, as
F-<sha256(path, bug_class, anchor_key)[:10]> and is stored in the comment marker and its data; it is never recomputed.
anchor_key = <sha256 of the anchor line with all whitespace removed, first 100 characters>:<n> where n counts the earlier
lines of the file with the same hash (the CodeQL primaryLocationLineHash idea); the script computes it, the reviewer never
does. bug_class is a closed enum (BUG_CLASSES); a reviewer's free text goes to `pattern` (display only).
A later report is the same finding as a posted one when, on the same path (a rename is followed with git diff -M):
M1 the reviewer named it with `same_as`, M2 the anchor_key is equal, M3 the anchor line moved by at most 3 lines
(same line hash, different n or line) [design judgment]. bug_class need not match. Matching a finding that was fixed reopens it
(no new id). A posted finding that is not matched is `fixed` only when ALL of these hold: a reviewer output existed in this
round, the head sha changed since it was posted, no new report matches it and its anchor line is gone or changed; a
reviewer may also report it fixed with evidence. Otherwise it stays open as `unconfirmed`. Every posted finding comment
carries `<!-- agentive-finding: <id> -->` and `<!-- agentive-finding-data: {json} -->` so the next round can rebuild the open
findings from the PR alone [design judgment].
"""
import hashlib
import json
import re

from . import config as cfgmod
from . import util

BUG_CLASSES = ("logic", "error-handling", "concurrency", "data-contract", "security", "test-gap", "spec-drift", "other")
ANCHOR_CHARS = 100
M3_LINES = 3
EXCERPT_CHARS = 200

SEVERITIES = ("important", "nit", "pre_existing")
REPLY_RE = re.compile(r"^agentive-reply: (\S+) (fixed|wrong|intentional|not_important)(?: (.+))?$")
SENTENCE_REQUIRED = ("wrong", "intentional", "not_important")
MARKER_RE = re.compile(r"<!--\s*agentive-finding:\s*(\S+)\s*-->")
DATA_RE = re.compile(r"<!--\s*agentive-finding-data:\s*(\{.*?\})\s*-->", re.S)
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
DISPUTED = ("wrong", "intentional", "not_important")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def line_hash(text):
    """sha256[:16] of a line with all whitespace removed, first ANCHOR_CHARS characters (CodeQL primaryLocationLineHash style)."""
    flat = re.sub(r"\s+", "", text or "")[:ANCHOR_CHARS]
    return hashlib.sha256(flat.encode("utf-8")).hexdigest()[:16]


class Anchors:
    """Anchor keys of lines of the head tree. read(path) -> file text or None (absent). Computed by the script only."""

    def __init__(self, read):
        self.read, self._cache = read, {}

    def lines(self, path):
        if path not in self._cache:
            text = self.read(path)
            self._cache[path] = None if text is None else text.splitlines()
        return self._cache[path]

    def hashes(self, path):
        ls = self.lines(path)
        return None if ls is None else [line_hash(x) for x in ls]

    def key(self, path, line):
        """(anchor_key, excerpt) of path:line, or (None, None) when the line is not in the file."""
        ls = self.lines(path)
        if ls is None or not (1 <= line <= len(ls)):
            return None, None
        hs = self.hashes(path)
        h = hs[line - 1]
        return f"{h}:{hs[:line - 1].count(h)}", ls[line - 1].strip()[:EXCERPT_CHARS]


def finding_id(path, bug_class, anchor_key):
    return util.stable_id(path, bug_class, anchor_key)


def normalize_bug_class(raw):
    """(bug_class, pattern): the closed enum value, and the reviewer's free text when it was not an enum value."""
    key = re.sub(r"[\s_]+", "-", str(raw or "").strip().lower())
    if key in BUG_CLASSES:
        return key, None
    return "other", (str(raw).strip() or None) if raw else None


def _anchor_hash(key):
    return str(key or "").split(":", 1)[0]


def match_posted(path, line, anchor_key, same_as, posted, renames=None):
    """(finding_id, how) of the posted finding a new report is the same as, or (None, None). Same path only (a posted path
    is followed through renames(path, head_sha)); M1 same_as, M2 equal anchor_key, M3 same line hash within M3_LINES lines."""
    def here(p):
        old = p.get("path")
        return renames(old, (p.get("data") or {}).get("head_sha")) if renames and old else old
    if same_as in posted and here(posted[same_as]) == path:
        return same_as, "same_as"
    if anchor_key:
        for fid, p in posted.items():
            if here(p) == path and (p.get("data") or {}).get("anchor_key") == anchor_key:
                return fid, "anchor_key"
        best = None
        for fid, p in posted.items():
            ak, pl = (p.get("data") or {}).get("anchor_key"), p.get("line")
            if here(p) == path and ak and _anchor_hash(ak) == _anchor_hash(anchor_key) and isinstance(pl, int) \
                    and abs(pl - line) <= M3_LINES and (best is None or abs(pl - line) < best[0]):
                best = (abs(pl - line), fid)
        if best:
            return best[1], "moved_anchor"
    return None, None


def clean_reviewer_output(out, reviewer_id, shadow=False, posted=None, anchors=None, renames=None):
    """(findings, open_status, problem). Drops findings without file:line and trigger (severity_blocking.evidence).
    Identity: an existing posted finding it matches (match_posted), else a new id minted from path, bug_class, anchor_key.
    open_status: {id: {"status": still_present | fixed, "evidence": text}}; `fixed` counts only with evidence."""
    posted = posted or {}
    if not isinstance(out, dict) or not isinstance(out.get("findings"), list):
        return [], {}, f"{reviewer_id}: output is not an object with findings[]"
    res, dropped = [], 0
    for f in out["findings"]:
        if not isinstance(f, dict):
            dropped += 1
            continue
        file, line, trig = f.get("file"), f.get("line"), f.get("trigger")
        if not (isinstance(file, str) and file and isinstance(line, int) and not isinstance(line, bool) and line >= 1
                and isinstance(trig, str) and trig.strip()):
            dropped += 1
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "important"
        bug_class, pattern = normalize_bug_class(f.get("bug_class"))
        if f.get("pattern"):
            pattern = str(f["pattern"]).strip() or pattern
        ak, excerpt = anchors.key(file, line) if anchors else (None, None)
        if ak is None:
            ak = f"line:{line}"                     # the file or line is not readable: the position is the only anchor
        fid, how = match_posted(file, line, ak, f.get("same_as"), posted, renames)
        item = {"id": fid or finding_id(file, bug_class, ak), "reviewers": [reviewer_id], "severity": sev, "blocking": False,
                "file": file, "line": line, "trigger": trig.strip(), "summary": str(f.get("summary") or trig).strip(),
                "bug_class": bug_class, "anchor_key": ak}
        if how:
            item["matched_by"] = how
        if excerpt:
            item["anchor_excerpt"] = excerpt
        if pattern:
            item["pattern"] = pattern[:200]
        if f.get("evidence"):
            item["evidence"] = str(f["evidence"])
        inst = [i for i in (f.get("instances") or []) if isinstance(i, dict) and isinstance(i.get("file"), str)
                and isinstance(i.get("line"), int) and not isinstance(i.get("line"), bool) and i["line"] >= 1]
        if inst:
            item["instances"] = [{"file": i["file"], "line": i["line"]} for i in inst]
        if shadow:
            item["shadow"] = True
        res.append(item)
    status = {}
    for o in out.get("open_findings") or []:
        if not isinstance(o, dict) or o.get("id") not in posted or o.get("status") not in ("still_present", "fixed"):
            continue
        ev = str(o.get("evidence") or "").strip()
        if o["status"] == "fixed" and not ev:
            continue                                # fixed without evidence is no claim at all
        if status.get(o["id"], {}).get("status") != "still_present":
            status[o["id"]] = {"status": o["status"], "evidence": ev}
    return merge_same_ids(res), status, (f"{reviewer_id}: dropped {dropped} finding(s) without file:line and trigger" if dropped else None)


def merge_same_ids(findings):
    by, order = {}, []
    for f in findings:
        if f["id"] in by:
            g = by[f["id"]]
            g["reviewers"] = util.uniq(g["reviewers"] + f["reviewers"])
            g.setdefault("instances", [])
            for i in [{"file": f["file"], "line": f["line"]}] + f.get("instances", []):
                if i not in g["instances"] and (i["file"], i["line"]) != (g["file"], g["line"]):
                    g["instances"].append(i)
            if not g["instances"]:
                del g["instances"]
            if SEVERITIES.index(f["severity"]) < SEVERITIES.index(g["severity"]):
                g["severity"] = f["severity"]
            if not f.get("shadow"):
                g.pop("shadow", None)
        else:
            by[f["id"]] = dict(f)
            order.append(f["id"])
    return [by[i] for i in order]


def apply_verdicts(findings, out, session_id):
    """RV7: set verification on every finding; a missing or invalid verdict is inconclusive."""
    got = {}
    if isinstance(out, dict):
        for v in out.get("verdicts") or []:
            if isinstance(v, dict) and v.get("verdict") in ("confirmed", "refuted", "inconclusive"):
                got[v.get("id")] = v
    for f in findings:
        v = got.get(f["id"])
        ver = {"verdict": v["verdict"] if v else "inconclusive", "verifier_session_id": session_id}
        if v and v.get("method") in ("execution", "adversarial_fresh_context"):
            ver["method"] = v["method"]
        else:
            ver["method"] = "adversarial_fresh_context"
        note = (v or {}).get("note") or ("" if v else "no verdict in the verifier output")
        if note:
            ver["note"] = str(note)
        f["verification"] = ver
    return findings


def coordinate(findings, out):
    """RV6: (findings, problem). The coordinator may merge (merged_from), regroup by bug class and set severity;
    every input id must end up in exactly one output finding, else the input is kept unchanged."""
    if not isinstance(out, dict) or not isinstance(out.get("findings"), list):
        return findings, "coordinator: output is not an object with findings[]"
    by = {f["id"]: f for f in findings}
    used, res = set(), []
    for o in out["findings"]:
        if not isinstance(o, dict) or o.get("id") not in by:
            return findings, f"coordinator: unknown finding {o!r}"
        ids = [o["id"]] + [x for x in (o.get("merged_from") or []) if x != o["id"]]
        if any(x not in by or x in used for x in ids):
            return findings, f"coordinator: finding ids {ids} unknown or used twice"
        used.update(ids)
        base = dict(by[o["id"]])
        if o.get("severity") in SEVERITIES:
            base["severity"] = o["severity"]
        if o.get("bug_class"):
            base["bug_class"], pat = normalize_bug_class(o["bug_class"])
            if pat:
                base["pattern"] = pat[:200]
        others = [by[x] for x in ids[1:]]
        if others:
            base["merged_from"] = ids[1:]
            base["reviewers"] = util.uniq(base["reviewers"] + [r for x in others for r in x["reviewers"]])
            inst = list(base.get("instances") or [])
            for x in others:
                for i in [{"file": x["file"], "line": x["line"]}] + list(x.get("instances") or []):
                    if i not in inst and (i["file"], i["line"]) != (base["file"], base["line"]):
                        inst.append(i)
            if inst:
                base["instances"] = inst
            rank = {"confirmed": 0, "inconclusive": 1, "refuted": 2}
            best = min([base] + others, key=lambda x: rank[(x.get("verification") or {}).get("verdict", "inconclusive")])
            if best.get("verification"):
                base["verification"] = dict(best["verification"])
            if any(not x.get("shadow") for x in [base] + others):
                base.pop("shadow", None)
        res.append(base)
    if used != set(by):
        return findings, f"coordinator: findings {sorted(set(by) - used)} missing from the output"
    return res, None


def set_blocking(findings, profile):
    for f in findings:
        if profile == "precommit":
            f["blocking"] = f["severity"] == "important" and not f.get("shadow")
        else:
            f["blocking"] = (f["severity"] == "important" and not f.get("shadow")
                             and (f.get("verification") or {}).get("verdict") == "confirmed"
                             and (f.get("response") or {}).get("source") != "next_round")
    return findings


# ---------------------------------------------------------------- posted findings and reply lines
def parse_reply(body):
    """(finding_id, reason, sentence) of the first valid agentive-reply line in a comment body, or None."""
    for ln in (body or "").splitlines():
        m = REPLY_RE.match(ln.strip())
        if not m:
            continue
        fid, reason, sentence = m.group(1), m.group(2), (m.group(3) or "").strip()
        if reason in SENTENCE_REQUIRED and not sentence:
            continue
        return fid, reason, sentence
    return None


def posted_findings(comments):
    """{finding_id: {comment_id, path, line, data, replies: [(comment_id, reason, sentence, author)]}} from the
    REST pulls/<n>/comments list (id, in_reply_to_id, body, path, line, author_association, user.login)."""
    roots, out = {}, {}
    for c in comments or []:
        m = MARKER_RE.search(c.get("body") or "")
        if m and not c.get("in_reply_to_id"):
            data = {}
            dm = DATA_RE.search(c.get("body") or "")
            if dm:
                try:
                    data = json.loads(dm.group(1))
                except ValueError:
                    data = {}
            roots[c["id"]] = m.group(1)
            out[m.group(1)] = {"comment_id": c["id"], "path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                               "data": data, "replies": []}
    for c in sorted(comments or [], key=lambda x: (x.get("created_at") or "", x.get("id") or 0)):
        root = c.get("in_reply_to_id")
        if not root or root not in roots:
            continue
        if c.get("author_association") not in TRUSTED:
            continue
        r = parse_reply(c.get("body"))
        if r is None or r[0] != roots[root]:
            continue                      # a reply line whose id does not match its thread's marker is ignored
        out[roots[root]]["replies"].append((c["id"], r[1], r[2], (c.get("user") or {}).get("login")))
    return out


def attach_responses(findings, posted):
    """finding.response from the latest valid reply line in its thread (source reply_line)."""
    for f in findings:
        p = posted.get(f["id"])
        if not p or not p["replies"]:
            continue
        cid, reason, _, _ = p["replies"][-1]
        kind = "open" if reason == "fixed" else "replied_resolved"
        f["response"] = {"kind": kind, "reason": reason, "source": "reply_line", "comment_id": int(cid)}
        if reason == "fixed" and f.get("matched_by"):
            f["reopened"] = True                    # the author said fixed, a reviewer reports it again: same id, open again
    return findings


def carried_findings(posted, current_ids, open_status, head_sha, anchors=None, renames=None, reviewers_ran=True):
    """Findings posted in an earlier round that no report of this round matches (FINDINGID).
    `fixed` (source next_round) only when ALL hold: a reviewer output existed in this round (reviewers_ran), the head sha
    changed since the finding was posted, and the anchor line is gone or changed in the head tree; or when a reviewer
    explicitly reported it fixed with evidence. A reviewer that still sees it keeps it open. Otherwise the finding stays
    open with its earlier severity and verdict and is marked `unconfirmed` (not mentioned is NOT fixed)."""
    out = []
    for fid, p in posted.items():
        if fid in current_ids:
            continue
        d = p.get("data") or {}
        if not p.get("path") or not p.get("line"):
            continue
        path = renames(p["path"], d.get("head_sha")) if renames else p["path"]
        f = {"id": fid, "reviewers": [r for r in (d.get("reviewers") or []) if r in cfgmod.REVIEWERS] or ["correctness-reviewer"],
             "severity": d.get("severity") if d.get("severity") in SEVERITIES else "important", "blocking": False,
             "file": path, "line": int(p["line"]), "trigger": d.get("trigger") or "posted in an earlier round",
             "summary": d.get("summary") or d.get("trigger") or "posted in an earlier round",
             "verification": {"verdict": d.get("verdict") if d.get("verdict") in ("confirmed", "refuted", "inconclusive") else "inconclusive",
                              "verifier_session_id": d.get("verifier_session_id") or "earlier-round"}}
        if d.get("bug_class") in BUG_CLASSES:
            f["bug_class"] = d["bug_class"]
        if d.get("pattern"):
            f["pattern"] = d["pattern"]
        if d.get("anchor_key"):
            f["anchor_key"] = d["anchor_key"]
        if isinstance(d.get("round"), int) and d["round"] >= 1:
            f["first_posted_round"] = d["round"]
        st = open_status.get(fid) or {}
        if st.get("status") == "fixed":
            f["response"] = {"kind": "fixed", "reason": "fixed", "source": "next_round"}
            f["fixed_evidence"] = st.get("evidence", "")
        elif st.get("status") != "still_present":
            if _fixed_by_all(d, path, head_sha, anchors, reviewers_ran):
                f["response"] = {"kind": "fixed", "reason": "fixed", "source": "next_round"}
            else:
                f["unconfirmed"] = True
        out.append(f)
    return out


def _fixed_by_all(d, path, head_sha, anchors, reviewers_ran):
    """The four conditions for fixed without a reviewer's explicit claim (the 'no new report matches' one is the caller's)."""
    if not reviewers_ran or anchors is None:
        return False
    if d.get("head_sha") == head_sha:                  # no new commit since it was posted: nothing can have fixed it
        return False
    ak = d.get("anchor_key")
    if not ak:                                          # a marker without an anchor (before FINDINGID): cannot be confirmed
        return False
    hs = anchors.hashes(path)
    return hs is None or _anchor_hash(ak) not in hs


def finding_data(f, round_no, head_sha=None):
    d = {"severity": f["severity"], "trigger": f["trigger"], "summary": f["summary"][:300],
         "reviewers": f["reviewers"], "round": f.get("first_posted_round", round_no)}
    if f.get("bug_class"):
        d["bug_class"] = f["bug_class"]
    for k in ("pattern", "anchor_key", "anchor_excerpt"):
        if f.get(k):
            d[k] = f[k]
    if head_sha:
        d["head_sha"] = head_sha
    v = f.get("verification") or {}
    if v:
        d["verdict"] = v.get("verdict")
        d["verifier_session_id"] = v.get("verifier_session_id")
    return d


def to_post(findings, posted_ids):
    """Inline comments to create: important, not refuted, not shadow-hidden, not posted before (convergence)."""
    res = []
    for f in findings:
        if f["id"] in posted_ids or f["severity"] != "important":
            continue
        if (f.get("verification") or {}).get("verdict") == "refuted":
            continue
        if (f.get("response") or {}).get("source") == "next_round":
            continue
        res.append(f)
    return res
