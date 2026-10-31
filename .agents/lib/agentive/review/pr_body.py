# agentive-unit: lib.agentive
# design: contracts[pr-body-fw-review], spec.work_loop.pr_explainer.enforcement, spec.work_loop.ci_independence_check (owner D)
"""Read and update the PR body: the ```yaml agentive-review``` block, the level-2 headings and the tier-2 ## Explainer.

The heading lists come from pr-body-agentive-review.schema.json x-agentive-body-headings (copied here because the
schema file is not installed in user repos; tests/review checks the copy against the schema).
Headings inside fenced code blocks are ignored.
"""
import re

from .. import ayaml
from . import util

ALL_TIERS = ["## Verification", "## Risk", "## How to revert"]
EXPLAINER = "## Explainer"
BLOCK_INFO = "yaml agentive-review"
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def _lines_outside_fences(body):
    """[(index, line, in_fence, fence_info)] for every line; fence_info is the info string of the open fence."""
    out, fence, info = [], None, None
    for i, ln in enumerate((body or "").splitlines()):
        m = FENCE.match(ln)
        if fence is None and m:
            fence, info = m.group(1), m.group(2).strip()
            out.append((i, ln, True, info))
            continue
        if fence is not None:
            out.append((i, ln, True, info))
            if re.match(r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", ln):
                fence, info = None, None
            continue
        out.append((i, ln, False, None))
    return out


def headings(body, level=2):
    pre = "#" * level + " "
    return [(i, ln.strip()) for i, ln, fenced, _ in _lines_outside_fences(body)
            if not fenced and ln.startswith(pre) and not ln.startswith(pre + "#")]


def block(body):
    """(agentive-review mapping or None, problem or None)."""
    blocks = util.fenced_blocks(body or "", BLOCK_INFO)
    if not blocks:
        return None, "no ```yaml agentive-review``` block"
    try:
        doc = ayaml.loads(blocks[0])
    except ayaml.YAMLError as e:
        return None, f"agentive-review block: {e}"
    if not isinstance(doc, dict) or not isinstance(doc.get("agentive-review"), dict):
        return None, "agentive-review block: top key agentive-review missing"
    return doc["agentive-review"], None


def all_tier_headings(body):
    """Problems: every ALL_TIERS heading present, in this order."""
    hs = [h for _, h in headings(body)]
    probs, last = [], -1
    for h in ALL_TIERS:
        if h not in hs:
            probs.append(f"missing heading {h}")
            continue
        idx = hs.index(h)
        if idx < last:
            probs.append(f"heading {h} out of order")
        last = max(last, idx)
    return probs


def explainer(body):
    """review-report rv8.explainer fields {present, problems} for a tier 2 PR (presence only)."""
    lines = _lines_outside_fences(body)
    h2 = [(i, ln.strip()) for i, ln, f, _ in lines if not f and ln.startswith("## ") and not ln.startswith("###")]
    present = {"section": False, "first": False, "before": False, "after": False, "before_mermaid": False, "after_mermaid": False}
    probs = []
    pos = next((k for k, (_, h) in enumerate(h2) if h == EXPLAINER), None)
    if pos is None:
        return present, ["no_section"]
    present["section"] = True
    present["first"] = pos == 0
    if not present["first"]:
        probs.append("not_first")
    start = h2[pos][0]
    end = h2[pos + 1][0] if pos + 1 < len(h2) else len(lines)
    sec = [x for x in lines if start < x[0] < end]
    h3 = [(i, ln.strip()) for i, ln, f, _ in sec if not f and ln.startswith("### ")]
    b = next((i for i, h in h3 if h == "### Before"), None)
    a = next((i for i, h in h3 if h == "### After"), None)
    present["before"], present["after"] = b is not None, a is not None
    if b is None:
        probs.append("no_before")
    if a is None:
        probs.append("no_after")
    if b is not None and a is not None and b > a:
        probs.append("before_order")

    def has_mermaid(lo, hi):
        return any(f and info == "mermaid" and FENCE.match(ln) and lo < i < hi for i, ln, f, info in sec)

    if b is not None:
        hi = a if (a is not None and a > b) else end
        present["before_mermaid"] = has_mermaid(b, hi)
        if not present["before_mermaid"]:
            probs.append("before_no_mermaid")
    if a is not None:
        hi = b if (b is not None and b > a) else end
        present["after_mermaid"] = has_mermaid(a, hi)
        if not present["after_mermaid"]:
            probs.append("after_no_mermaid")
    return present, probs


def explainer_text(body):
    """The ## Explainer section text (for detecting an explainer-only edit)."""
    lines = (body or "").splitlines()
    hs = headings(body)
    pos = next((k for k, (_, h) in enumerate(hs) if h == EXPLAINER), None)
    if pos is None:
        return ""
    start = hs[pos][0]
    end = hs[pos + 1][0] if pos + 1 < len(hs) else len(lines)
    return "\n".join(lines[start:end])


def with_pipeline(body, pipeline, review_round=None, reviews=None):
    """Body with agentive-review.pipeline (and review_round, CI reviews) written into the existing block.
    Returns None when the body has no valid block (the summary comment then carries the result) [design judgment]."""
    doc, prob = block(body)
    if doc is None:
        return None
    doc = dict(doc)
    doc["pipeline"] = pipeline
    if review_round is not None:
        doc["review_round"] = review_round
    if reviews:
        keep = [r for r in (doc.get("reviews") or []) if r.get("source") != "ci"]
        doc["reviews"] = keep + reviews
    return util.replace_fenced_block(body, BLOCK_INFO, ayaml.dumps({"agentive-review": doc}))
