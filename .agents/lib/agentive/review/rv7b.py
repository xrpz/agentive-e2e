# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV7b], spec.review_pipeline.auto_merge_policy, decisions.decided (U6, revised E2E-13: agentive:rubric@2) (owner D)
"""RV7b binary risk score from deterministic inputs only (RV1 signals, RV3 classes). It never compares the score with
the threshold: RV8 does (score and decision are separate).

Thresholds inside an item's `hit` text are read from it [design judgment]: `>N` for changed_lines (default 400), `>=N`
modules for modules_touched_or_shared_path and production_modules_or_shared_path (default 2), `>=N` commits for
production_churn_90d (default 5), the `a | b` class list for change_class (default feature | fix | dependency;
'dependency' = class deps).
Inputs (RV1 signals): rubric@1 modules_touched_or_shared_path, recent_churn_30d (every changed file); rubric@2 (E2E-13)
production_modules_or_shared_path (production_modules_touched, production_shared_path_touched: tests, docs and excluded
files left out) and production_churn_90d (production_churn_commits_90d: the most changes landed on the base branch within 90
days on one changed production file; first-parent records, merge commits included).
"""
import re

from . import config as cfgmod

CLASS_WORDS = {"dependency": "deps", "dependencies": "deps"}


def _num(hit, rx, default):
    m = re.search(rx, hit or "")
    return int(m.group(1)) if m else default


def item_hit(item, signals, classes, changed_lines, shared_globs):
    inp, hit = item.get("input"), item.get("hit", "")
    if inp == "modules_touched_or_shared_path":
        n = _num(hit, r">=\s*(\d+)", 2)
        return len(signals.get("modules_touched") or []) >= n or bool(signals.get("shared_path_touched"))
    if inp == "production_modules_or_shared_path":
        n = _num(hit, r">=\s*(\d+)", 2)
        return len(signals.get("production_modules_touched") or []) >= n or bool(signals.get("production_shared_path_touched"))
    if inp == "production_churn_90d":
        return int(signals.get("production_churn_commits_90d") or 0) >= _num(hit, r">=\s*(\d+)", 5)
    if inp == "changed_lines":
        return changed_lines > _num(hit, r">\s*(\d+)", 400)
    if inp == "incident_history_90d":
        return bool(signals.get("incident_history_90d"))
    if inp == "recent_churn_30d":
        return bool(signals.get("recent_churn_30d"))
    if inp == "change_class":
        words = [w.strip().lower() for w in re.split(r"[|,]", hit) if w.strip()]
        want = {CLASS_WORDS.get(w, w) for w in words if CLASS_WORDS.get(w, w) in cfgmod.CLASSES} or {"feature", "fix", "deps"}
        return bool(set(classes) & want)
    return False


def run(tier, tier1_mode, rubric_loaded, signals, classes, changed_lines):
    if tier != 1:
        return {"score": None, "reason": "not_tier1"}
    if tier1_mode == "off":
        return {"score": None, "reason": "tier1_off"}
    if not rubric_loaded.valid or rubric_loaded.value is None:
        return {"score": None, "reason": "config_invalid"}
    eff = rubric_loaded.value
    items = []
    for it in eff["items"]:
        if not it.get("enabled", True):
            continue
        items.append({"id": it["id"], "hit": item_hit(it, signals, classes, changed_lines, eff.get("shared_globs")),
                      "weight": it["weight"]})
    return {"score": sum(i["weight"] for i in items if i["hit"]), "reason": "computed",
            "preset": eff.get("preset", cfgmod.RUBRIC_PRESET_ID), "human_if_score_gt": eff["human_if_score_gt"],
            "config_sha256": cfgmod.rubric_sha256(eff), "items": items}
