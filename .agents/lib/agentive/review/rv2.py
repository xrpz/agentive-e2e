# agentive-unit: lib.agentive
# design: spec.review_pipeline.stages[RV2], spec.review_pipeline.stages[RV2].confirm_scope, spec.work_loop.precommit_review.final_confirmation.non_interactive, spec.spec_stage.chain.must_human, decisions.accepted[LOCKFILEDEPS], decisions.accepted[DEPHUMAN] (owner D)
"""RV2 trust boundary (llm: false).

hits             never_ai_approve globs of the rules block (base commit) + the built-in protected_check_config
                 (reason check_config, cannot be turned off) + the project's extra protected paths
confirm_scope    one glob list per U2 category; a hit without a human confirmation -> u2.action pr_only_tier2
always_human     the design's always_human items that a script can see (returned as (code, detail) pairs; RV4 turns
                 each into tier 2; codes are review-report gate_reason values where one fits)

Four layers (decision SECPATHS, review/secpaths.py): L1 the organization's own declarations (CODEOWNERS lines with a security
owner, snapshotted by init/update into the block with `source: codeowners:<line>`, and the project's `add` entries), L2 the
shipped floor (case-sensitive globs and path segment tokens), L3 content rules `{path, contains}` evaluated on the diff
(stack presets are written by the engine with `source: preset:<id>`), L4 the RV4 raise-only check. Every hit records
{path, glob, reason, layer, source?}. A `shadow: true` rule never forces a tier: it is listed in rv2.shadow_hits.

Not detectable by a script and left to the RV4 raise-only check (content-based backstop, B12): data deletion and mass
external sends without a path signal. The pipeline never reads CODEOWNERS at run time (meaning differences, the 3 MB limit and
parser upkeep); it reads the snapshot in the block, which is read from the base commit [design judgment].
"""
import posixpath
import re

from . import config
from . import rv1 as rv1mod
from . import secpaths
from . import util

TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MULTI_PURPOSE = {"package.json", "pyproject.toml", "setup.cfg", "tox.ini", "Cargo.toml", "go.mod", "pom.xml",
                 "build.gradle", "build.gradle.kts", "Makefile", "composer.json", "Gemfile"}
REGION = re.compile(r"<!--\s*AGENTIVE (START|END) ([^\s>]+)\s*-->")
CHAIN_STAGE_ARTIFACT = re.compile(r"^docs/plans/(?!archive/)[^/]+/(intent|spec)\.md$")


def _protected(path, builtin, extra):
    for e in builtin:
        if secpaths.glob_dir_match(e["glob"], path) and not util.first_match(e.get("except", []), path):
            return e["glob"]
    return secpaths.first_dir_match(extra, path)


def _hit(e, path, floor):
    h = {"path": path, "glob": secpaths.label(e), "reason": e["reason"], "layer": secpaths.layer_of(e, floor)}
    if e.get("source"):
        h["source"] = e["source"]
    if "contains" in e:
        h["contains"] = secpaths.contains_list(e)[0] if isinstance(e["contains"], str) else ", ".join(secpaths.contains_list(e))
    return h


def security_checks(manifest, checks):
    """RV1 signal consumed here: the security tools the engine found configured in the repo (agentive manifest
    security_checks: gitleaks, semgrep, codeql, trufflehog) against the check results of this run. [{tool, check, status}],
    status pass | fail | not_run. A tool's own rules are never copied; its verdict is the signal [design judgment: a check matches a tool
    when its name holds the tool id, case-insensitive]."""
    out = []
    for t in (manifest.get("security_checks") or []):
        tool = t.get("tool") if isinstance(t, dict) else None
        if not tool:
            continue
        got = [c for c in checks or [] if tool.lower() in str(c.get("name", "")).lower()]
        if not got:
            out.append({"tool": tool, "status": "not_run"})
            continue
        bad = [c for c in got if c.get("status") == "fail"]
        pick = bad[0] if bad else got[0]
        out.append({"tool": tool, "check": pick["name"], "status": "fail" if bad else ("pass" if pick.get("status") == "pass" else "not_run")})
    return out


def _region_spans(text):
    """[(start_line, end_line)] 1-based, of AGENTIVE START/END regions."""
    spans, start = [], None
    for i, ln in enumerate((text or "").splitlines(), 1):
        m = REGION.search(ln)
        if not m:
            continue
        if m.group(1) == "START":
            start = i
        elif start is not None:
            spans.append((start, i))
            start = None
    return spans


def _in_spans(lines, spans):
    return any(a <= ln <= b for ln, _ in lines for a, b in spans)


def _section_spans(text, section):
    """Line spans of a TOML/INI section `[section]` (and its sub-tables `[section.x]`)."""
    spans, start = [], None
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*$", ln)
        if not m:
            continue
        name = m.group(1).strip().strip('"')
        if start is not None:
            spans.append((start, i - 1))
            start = None
        if name == section or name.startswith(section + "."):
            start = i
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def evidence_config_files(manifest):
    """[(path, section or None)] of the dedicated lint, typecheck and format config files named in verbs.*.evidence.files.
    A multi-purpose file (package.json, pyproject.toml, ...) counts only as `path#section` [design judgment]. A lockfile or a
    dependency file (requirements*.txt, Pipfile, setup.py, ...) is dependency data, not check config: it is never protected
    here (E2E-21, LOCKFILEDEPS); a change of its dependency entries goes to a human through dependency_change (DEPHUMAN)."""
    out = []
    for name, v in (manifest.get("verbs") or {}).items():
        if name not in ("lint", "typecheck", "format") or not isinstance(v, dict):
            continue
        for f in ((v.get("evidence") or {}).get("files") or []):
            if not isinstance(f, str) or not f:
                continue
            path, _, section = f.partition("#")
            if section:
                out.append((path, section))
            elif posixpath.basename(path) not in MULTI_PURPOSE and not rv1mod.is_lockfile(path) and not rv1mod.is_package_file(path):
                out.append((path, None))
    return util.uniq(out)


def run(diff, statuses, hunks, rules, manifest, signals, pr=None, checks=None, rules_problems=None):
    """pr: {author_association, is_fork, labels: [..]} (CI) or None (precommit)."""
    tb = rules["trust_boundary"]
    hits, shadow_hits, cs_hits, always = [], [], [], []
    extra_protected = list(tb.get("protected_check_config") or [])
    ev_files = evidence_config_files(manifest)
    changed = [p for _, p, _ in statuses]
    floor = config.floor_keys()
    nai = secpaths.expand(tb["never_ai_approve"])
    content_rules = secpaths.expand(tb.get("content_rules") or [])
    for st, p, old in statuses:
        names = [p] + ([old] if old else [])
        for n in names:
            for e in nai:
                if secpaths.path_hit(e, n):
                    if "token" in e and any(h["path"] == p and h["reason"] == e["reason"] for h in hits):
                        continue          # a glob already reported this path for the same reason
                    (shadow_hits if e.get("shadow") else hits).append(_hit(e, p, floor))
            g = _protected(n, config.PROTECTED_CHECK_CONFIG, extra_protected)
            if g:
                hits.append({"path": p, "glob": g, "reason": "check_config", "layer": "protected"})
        for rule in content_rules:
            for n in names:
                if secpaths.content_hit(rule, n, hunks.get(p)):
                    (shadow_hits if rule.get("shadow") else hits).append(_hit(rule, p, floor))
                    break
        base_name = posixpath.basename(p)
        if base_name in ("AGENTS.md", "CLAUDE.md"):
            h = hunks.get(p) or {}
            if _in_spans(h.get("added", []), _region_spans(diff.show_head(p))) or \
                    _in_spans(h.get("removed", []), _region_spans(diff.show_base(old or p))):
                hits.append({"path": p, "glob": f"{base_name} managed region", "reason": "check_config", "layer": "protected"})
        for path, section in ev_files:
            if p != path and old != path:
                continue
            if section is None:
                hits.append({"path": p, "glob": path, "reason": "check_config", "layer": "protected"})
            else:
                h = hunks.get(p) or {}
                if _in_spans(h.get("added", []), _section_spans(diff.show_head(p), section)) or \
                        _in_spans(h.get("removed", []), _section_spans(diff.show_base(old or p), section)):
                    hits.append({"path": p, "glob": f"{path}#{section}", "reason": "check_config", "layer": "protected"})
        for cat, globs in tb["confirm_scope_globs"].items():
            for n in names:
                g = secpaths.first_dir_match(globs, n)
                if g:
                    cs_hits.append({"path": p, "category": cat, "glob": g})
                    break
    hits = _dedupe(hits, ("path", "glob", "reason"))
    shadow_hits = _dedupe(shadow_hits, ("path", "glob", "reason"))
    cs_hits = _dedupe(cs_hits, ("path", "category"))
    sec_checks = security_checks(manifest, checks)
    for c in sec_checks:
        if c["status"] == "fail":
            always.append(("always_human", f"security tool check failed: {c['check']}"))
    # always_human items a script can see
    env = list(signals.get("env_vars_changed") or [])
    if env:                                   # E2E-35: one item for all names (the full list stays in rv1.signals)
        always.append(("always_human", env_vars_text(env)))
    # DEPHUMAN (E2E-20, user 2026-10-31): a change of the dependency entries of a package file or of a lockfile's content is
    # tier 2 in every profile, agent-authored or not (bot PRs are human already, BOTPR). Dependency review is information for
    # the human: pass or no result never decides the tier (critical_cve_unchecked is retired); a reported critical CVE keeps
    # its own always_human line [design judgment: it can only raise, and it covers manifests RV1 does not read].
    dep = {c["name"]: c["status"] for c in (checks or [])}.get("dependency-review")
    if dep == "fail":
        always.append(("always_human", "dependency-review failed (critical CVE in a dependency)"))
    dc = signals.get("dependency_changes")
    if dc is None:
        dc = rv1mod.dependency_changes(diff, statuses)
    if rv1mod.is_dependency_change(dc):
        always.append(("dependency_change", rv1mod.dependency_change_text(dc, dep if pr is not None else "no_ci")))
    if any(h["reason"] == "check_config" for h in hits):
        always.append(("always_human", "gate or agent config change (protected_check_config)"))
    if len(changed) > 100:
        always.append(("always_human", f"more than 100 changed files ({len(changed)})"))
    if pr is not None:
        if pr.get("is_fork") or pr.get("author_association") not in TRUSTED_ASSOCIATIONS:
            always.append(("always_human", f"untrusted or fork author ({pr.get('author_association')}, fork={bool(pr.get('is_fork'))})"))
        for lab in pr.get("labels") or []:
            if str(lab).startswith("agentive-janitor:"):
                always.append(("always_human", f"label {lab}"))
    tags = signals.get("agentive_check_tags") or {}
    if tags.get("added") or tags.get("removed"):
        always.append(("always_human", "agentive-check: tag added or removed"))
    for st, p, _ in statuses:
        if st in "AMR" and CHAIN_STAGE_ARTIFACT.match(p):
            always.append(("chain_approval", f"chain artifact {p} added or modified"))
    for x in rules_problems or []:
        always.append(("always_human", f"review rules invalid, design defaults used: {x}"))
    rv2 = {"hits": hits, "confirm_scope_hits": cs_hits}
    if shadow_hits:
        rv2["shadow_hits"] = shadow_hits
    if sec_checks:
        rv2["security_checks"] = sec_checks
    if tb.get("removed"):
        rv2["removed"] = [{k: e[k] for k in ("glob", "token", "path", "contains", "reason") if k in e} for e in tb["removed"]]
    return rv2, util.uniq(always)


def env_vars_text(names, show=5):
    """E2E-35 (decision e2e_32_36): one always_human detail for every env var name, `env vars added or removed (N): A, B, C, D, E
    (+k more)`; the rule (env var added or removed -> a human) is unchanged."""
    names = list(names)
    more = f" (+{len(names) - show} more)" if len(names) > show else ""
    return f"env vars added or removed ({len(names)}): {', '.join(names[:show])}{more}"


def _dedupe(items, keys):
    seen, out = set(), []
    for it in items:
        k = tuple(it[x] for x in keys)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def u2_record(cs_hits, profile, body_u2=None, body_present=False, run=None, human_confirmation=None):
    """review-report u2 {run, human_confirmation, action, source}.

    precommit: run from the no-human signals (run argument), human_confirmation as recorded by the commit procedure
    (None = not asked yet -> not_obtained while a hit exists). ci: the PR body agentive-review.u2 block; a missing
    block with a hit means not_obtained (RV2.confirm_scope.human_confirmation)."""
    if profile == "precommit":
        source = "local"
        run = run or "interactive"
        hc = human_confirmation
    else:
        if body_u2 is None:
            source, run, hc = "pr_body_missing", "unknown", None
        else:
            source = "pr_body"
            run = body_u2.get("run") if body_u2.get("run") in ("interactive", "non_interactive", "unknown") else "unknown"
            hc = body_u2.get("human_confirmation")
    if not cs_hits:
        hc = "not_required"
    elif hc != "obtained" or run != "interactive":
        hc = "not_obtained"
    action = "pr_only_tier2" if hc == "not_obtained" else "commit"
    return {"run": run, "human_confirmation": hc, "action": action, "source": source}
