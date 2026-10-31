# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV8].rules (kill switch), spec.review_pipeline.auto_merge_policy.conditions[U1.c], spec.review_pipeline.stages[RV8].demotion_state (owner D)
"""U1.c kill switch, a pure function over PR and git history (constants: 2 within 7 days, per tier and source).

revert   a default-branch commit whose message contains 'This reverts <full sha>' (also 'This reverts commit <sha>')
         of the merge commit of an auto-merged PR [S37]
hotfix   a merged PR whose agentive-review.hotfix_of names the PR
source   the PR author login [design judgment]
auto-merged PR = its body block records pipeline.gate.auto_merge true and pipeline.tier.value = the tier.
Only events after the latest close time of a demotion issue for the same scope count [design judgment: K6].
The demotion issue carries `<!-- agentive-demotion: tier=<t> source=<login> -->` [design judgment: scope marker].
"""
import datetime
import re

from . import pr_body
from . import util

COUNT = 2
WINDOW_DAYS = 7
LABEL = "agentive:auto-merge-demoted"
SCOPE_RE = re.compile(r"<!--\s*agentive-demotion:\s*tier=(\d)\s+source=(\S+?)\s*-->")
REVERT_RE = re.compile(r"This reverts (?:commit )?([0-9a-f]{40})")


def scope_marker(tier, source):
    return f"<!-- agentive-demotion: tier={tier} source={source} -->"


def _scope(issue):
    m = SCOPE_RE.search(issue.get("body") or "")
    return (int(m.group(1)), m.group(2)) if m else None


def auto_merged_tier(body):
    blk, _ = pr_body.block(body)
    pipe = (blk or {}).get("pipeline") or {}
    if (pipe.get("gate") or {}).get("auto_merge") is True:
        return (pipe.get("tier") or {}).get("value")
    return None


def evaluate(tier, source, now, merged_prs, revert_commits, demotion_issues):
    """{count, prs, demoted, open_issue: bool, since}.

    merged_prs: [{number, author, merged_at, merge_commit_sha, body}] (recently merged PRs of the repo)
    revert_commits: [(sha, unix_time, message)] on the default branch within the window
    demotion_issues: [{number, state, closed_at, body}] labeled agentive:auto-merge-demoted"""
    scope = (tier, source)
    open_now = any(i.get("state") == "open" and _scope(i) == scope for i in demotion_issues)
    closed = [util.parse_time(i["closed_at"]) for i in demotion_issues
              if i.get("state") == "closed" and i.get("closed_at") and _scope(i) == scope]
    since = now - datetime.timedelta(days=WINDOW_DAYS)
    if closed:
        since = max(since, max(closed))
    candidates = {}
    for p in merged_prs:
        if p.get("author") != source or auto_merged_tier(p.get("body")) != tier:
            continue
        if p.get("merge_commit_sha"):
            candidates[p["merge_commit_sha"]] = p["number"]
    hit = set()
    for sha, ts, msg in revert_commits:
        t = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        if t <= since:
            continue
        for full in REVERT_RE.findall(msg or ""):
            if full in candidates:
                hit.add(candidates[full])
    nums = set(candidates.values())
    for p in merged_prs:
        blk, _ = pr_body.block(p.get("body"))
        hf = (blk or {}).get("hotfix_of")
        if isinstance(hf, int) and hf in nums and p.get("merged_at") and util.parse_time(p["merged_at"]) > since:
            hit.add(hf)
    count = len(hit)
    return {"count": count, "prs": sorted(hit), "demoted": open_now or count >= COUNT,
            "open_issue": (not open_now) and count >= COUNT, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ")}


def issue_text(tier, source, count, prs):
    title = f"agentive: auto-merge demoted for tier {tier}, source {source}"
    body = (f"{scope_marker(tier, source)}\n"
            f"The kill switch (U1.c) counted {count} auto-merged PRs of tier {tier} by {source} that were reverted or "
            f"hotfixed within {WINDOW_DAYS} days: {', '.join('#' + str(n) for n in sorted(prs)) or '-'}.\n\n"
            f"Auto-merge is stopped for this tier and source only. Close this issue to re-enable it; only reverts and "
            f"hotfixes after the close time count again.")
    return title, body
