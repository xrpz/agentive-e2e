# agentive-unit: lib.agentive
# design: spec.review_pipeline.config, spec.review_pipeline.rules, spec.review_pipeline.gate_protection, spec.review_pipeline.stages[RV7b].customization, contracts[review-rules], contracts[review-config], contracts[rubric], contracts[project-config], decisions.accepted[DEFAULTS] (owner D)
"""Load every input of the pipeline that is configuration.

Gate policy (the code_review.md rules block, the rubric, the review config, review.auto_merge) is read from the base
commit, never from the PR branch (spec.review_pipeline.gate_protection). Precommit reads the same files from HEAD.
`source` below is a callable (path -> text or None) bound to one tree.

Missing or invalid inputs never lower risk [design judgment]:
- rules block missing (no code_review.md, or no ```yaml agentive-review-rules``` block) -> the design defaults
  (spec.review_pipeline.stages[RV2].default_globs, confirm_scope.default_globs, RV4 defaults);
- rules block invalid -> the design defaults, and RV4 forces tier 2 (reason rules_invalid via always_human);
- review config invalid -> the defaults, and RV8 treats tier 1 as off;
- review.auto_merge unknown or unparsable -> off (nothing is merged); absent -> the framework default low_risk (DEFAULTS: tier 0 merges, tier 1 in shadow = U1=C);
- rubric override invalid -> rv7b reason config_invalid (tier 1 goes to a human; tier 0 still auto-merges when review.auto_merge allows it).
"""
import copy
import re

from .. import ayaml, common
from . import secpaths
from . import util

REVIEWERS = ["correctness-reviewer", "security-reviewer", "testing-reviewer"]
CLASSES = ["docs", "format", "refactor", "feature", "fix", "security", "deps", "lockfile", "config", "test",
           "generated", "other"]
TIER0_ELIGIBLE = ["docs", "format", "lockfile", "generated"]
U2_CATEGORIES = ["hooks", "permissions", "ci", "claude_settings", "codex_dir", "review_config"]
NEVER_REASONS = ["auth", "security", "permissions", "secrets", "db_migration", "ci_config", "agent_hooks_settings",
                 "check_config", "billing", "other"]
STAGES = ["RV1", "RV2", "RV3", "RV4", "RV7b", "RV5", "RV7", "RV6", "RV8"]
PRECOMMIT_STAGES = ["RV1", "RV2", "RV5"]

# design spec.review_pipeline.stages[RV2].default_globs + decisions.accepted[SECPATHS] (the floor: case-sensitive globs that also
# match directories, and path segment tokens). The defaults C's code_review.md#rules block also writes.
FLOOR_TOKENS = [
    ("auth", ["auth", "authn", "authz", "authentication", "authorization", "login", "oauth", "oauth2", "oidc", "saml", "sso", "jwt"]),
    ("secrets", ["secret", "secrets", "credential", "credentials", "vault", "kms"]),
    ("permissions", ["permission", "permissions", "rbac", "acl", "iam"]),
    ("security", ["security", "crypto", "cipher", "tls", "ssl", "cert", "certs"]),
    ("billing", ["billing", "payment", "payments", "invoice", "invoices"]),
    ("db_migration", ["migration", "migrations", "migrate"]),
]
DEFAULT_RULES = {
    "trust_boundary": {
        "never_ai_approve": [
            {"glob": "**/auth/**", "reason": "auth"},
            {"glob": "**/security/**", "reason": "security"},
            {"glob": "**/permissions*", "reason": "permissions"},
            {"glob": "**/*secret*", "reason": "secrets"},
            {"glob": "**/.env*", "reason": "secrets"},
            {"glob": "**/migrations/**", "reason": "db_migration"},
            {"glob": ".github/workflows/**", "reason": "ci_config"},
            {"glob": ".claude/settings*.json", "reason": "agent_hooks_settings"},
            {"glob": "**/hooks.json", "reason": "agent_hooks_settings"},
            {"glob": ".codex/**", "reason": "agent_hooks_settings"},
            {"glob": "**/billing/**", "reason": "billing"},
            {"glob": "**/payment*/**", "reason": "billing"},
            {"glob": "**/SECURITY.md", "reason": "security"},
        ] + [{"tokens": toks, "reason": reason} for reason, toks in FLOOR_TOKENS],
        # RV2.confirm_scope.default_globs + .agents/hooks/** (docs/IMPLEMENTATION.md §9.7) + guard.yaml (C's block)
        "confirm_scope_globs": {
            "hooks": ["**/hooks.json", "plugins/**/hooks/**", ".agents/hooks/**"],
            "permissions": ["**/permissions*", ".agents/guard.yaml", ".agents/custom/.agents/guard.yaml"],
            "ci": [".github/workflows/**"],
            "claude_settings": [".claude/settings*.json"],
            "codex_dir": [".codex/**"],
            "review_config": [".agents/framework.yaml", ".agents/custom/.agents/rubric.yaml"],
        },
    },
    "classify": {"path_rules": [], "whitespace_only_is_format": True},
    "tiers": {"tier0_classes": ["docs", "format"], "tier0_max_changed_lines": 200, "large_change_lines": 1000,
              "tier2_classes": ["security"], "confirm_scope_without_human_tier": 2},
}

# spec.review_pipeline.stages[RV2].protected_check_config.paths: built in, cannot be removed (disableable: false)
PROTECTED_CHECK_CONFIG = [
    {"glob": ".agents/**", "except": [".agents/state/**"]},
    {"glob": ".claude/**"},
    {"glob": ".codex/**"},
    {"glob": "code_review.md"},
    {"glob": "evals/reviewer/**"},
]
# REVIEW.md (the managed copy of code_review.md) is a review rule file too (reviewer_eval.when) [design judgment]
PROTECTED_CHECK_CONFIG.append({"glob": "REVIEW.md"})
# SECPATHS L1: the CODEOWNERS file decides who must approve, so a change to it is a gate change (GitHub advises protecting it
# with a code owner of its own); the three places GitHub reads, in its lookup order
PROTECTED_CHECK_CONFIG += [{"glob": ".github/CODEOWNERS"}, {"glob": "CODEOWNERS"}, {"glob": "docs/CODEOWNERS"}]

DEFAULT_CONFIG = {
    "stages": {
        "RV1": {"exclude_globs": [], "require_checks": ["lint", "typecheck", "test"]},
        "RV3": {"llm_layer": True},
        "RV5": {"reviewers": list(REVIEWERS), "shadow_reviewers": [], "context": "fresh"},
        "RV8": {"check_name": "agentive-review-gate", "coverage": {"min_new_code_pct": 80, "exempt_new_lines_lt": 20}},
    },
    "profiles": {"precommit": {"stages": list(PRECOMMIT_STAGES), "reviewers": ["correctness-reviewer"]},
                 "ci": {"stages": list(STAGES)}},
    "rules_file": "code_review.md",
    "auto_merge": {},
    "criteria": {"scope": "ancestor_directory", "one_kind_per_file": True, "no_applicable_file": "normal",
                 "fallback_files": ["CLAUDE.md", "AGENTS.md"]},
}

# CITOOL (decisions.accepted, user 2026-10-29): the CI AI review tool follows the user's agent
CI_TOOLS = ("claude", "codex")
CI_TOOL_RECORD = {"claude": "claude_code", "codex": "codex"}   # names in review-report rv5.reviewers[].tool and agentive-review reviews[].tool


def ci_tool(pipeline, manifest):
    """Effective CI AI review tool: review.pipeline.ci_tool, else the agentive manifest framework.init_tool (the tool that
    ran init), else claude (a manifest written before CITOOL; the only CI tool then). The engine renders the workflow with
    this function and agentive-reviewer-eval / agentive-review use it at run time, so all three agree [design judgment]."""
    t = (pipeline or {}).get("ci_tool")
    if t in CI_TOOLS:
        return t
    t = ((manifest or {}).get("framework") or {}).get("init_tool")
    return t if t in CI_TOOLS else "claude"


# CIAUTH (decisions.accepted, user 2026-10-30): the claude CI path takes a Claude Console API key, a Claude subscription
# OAuth token (`claude setup-token`), or both. Per credential kind: the repository secret and the action input
# (code.claude.com/docs/en/github-actions, "Add an authentication secret"). codex has one kind.
CI_AUTH = ("both", "api_key", "oauth_token")
CI_AUTH_DEFAULT = "both"
CI_CREDENTIALS = {
    "claude": {"api_key": {"secret": "ANTHROPIC_API_KEY", "input": "anthropic_api_key"},
               "oauth_token": {"secret": "CLAUDE_CODE_OAUTH_TOKEN", "input": "claude_code_oauth_token"}},
    "codex": {"api_key": {"secret": "OPENAI_API_KEY", "input": "openai-api-key"}},
}


def ci_auth(pipeline, tool):
    """Effective credential choice of the CI tool: review.pipeline.ci_auth (both | api_key | oauth_token) for claude,
    default both. With both the OAuth token goes first (E2E-12, user decision 2026-10-30: option 1, OAuth token first): the workflow
    passes the API key only when the CLAUDE_CODE_OAUTH_TOKEN secret is empty (ci_input_expr), because Claude Code itself
    would pick the API key when both are set (code.claude.com/docs/en/authentication, Authentication precedence) and a
    subscription user who also stored an API key would be billed through the API. codex: always api_key."""
    if tool != "claude":
        return "api_key"
    v = (pipeline or {}).get("ci_auth")
    return v if v in CI_AUTH else CI_AUTH_DEFAULT


def ci_credentials(tool, auth):
    """[(kind, {secret, input})] the CI renders for the tool and the effective ci_auth, API key first."""
    creds = CI_CREDENTIALS.get(tool) or CI_CREDENTIALS["claude"]
    return [(k, v) for k, v in creds.items() if auth in ("both", k) or len(creds) == 1]


def ci_input_expr(tool, auth, kind):
    """The GitHub Actions expression the CI passes for the credential `kind` (the action input of the ai-review LLM step and the
    reviewer-eval env line). E2E-12 (OAuth token first): claude with ci_auth both passes the API key only when the OAuth token
    secret is empty (an unset secret is the empty string); every other case passes the secret itself."""
    creds = CI_CREDENTIALS.get(tool) or CI_CREDENTIALS["claude"]
    secret = creds[kind]["secret"]
    if tool == "claude" and auth == "both" and kind == "api_key":
        token = creds["oauth_token"]["secret"]
        return f"${{{{ secrets.{token} == '' && secrets.{secret} || '' }}}}"
    return f"${{{{ secrets.{secret} }}}}"


# U6 (decided by the user 2026-10-15, revised 2026-10-30 E2E-13): the framework rubric presets. The newest is the default and
# the one /agentive:update writes into .agents/rubric.yaml; an older preset stays pinnable: an override whose extends names it
# (.agents/custom/.agents/rubric.yaml, `extends: "agentive:rubric@1"`, alone or with changes) is merged onto that preset.
RUBRIC_PRESETS = {
    "agentive:rubric@1": {
        "preset": "agentive:rubric@1", "scoring": "binary", "human_if_score_gt": 28,
        "shared_globs": ["**/shared/**", "**/common/**"],
        "items": [
            {"id": "spread", "hit": ">=2 modules or a shared path", "input": "modules_touched_or_shared_path", "weight": 24},
            {"id": "size", "hit": ">400 changed lines (absolute LOC only)", "input": "changed_lines", "weight": 24},
            {"id": "incident_history", "hit": "revert or hotfix on touched files within 90 days", "input": "incident_history_90d", "weight": 24},
            {"id": "change_type", "hit": "feature | fix | dependency", "input": "change_class", "weight": 14},
            {"id": "recent_churn", "hit": "touched files changed within 30 days", "input": "recent_churn_30d", "weight": 14},
        ],
    },
    # E2E-13 (2026-10-30, orchestrator under the user's delegation after review; size 500 [user], churn rule confirmed by the user):
    # tests and docs are not spread; a feature or fix scores nothing by its type; the size and incident items send a PR to a
    # human on their own (30 > 28); churn counts changes landed on the base: first-parent records, merge commits included
    # (revised 2026-10-31 [user], option 1: a merged PR, a squash commit or a direct commit counts 1).
    "agentive:rubric@2": {
        "preset": "agentive:rubric@2", "scoring": "binary", "human_if_score_gt": 28,
        "shared_globs": ["**/shared/**", "**/common/**"],
        "items": [
            {"id": "spread", "hit": ">=2 production modules or a shared path among production files (tests and docs excluded)",
             "input": "production_modules_or_shared_path", "weight": 24},
            {"id": "size", "hit": ">500 changed lines (tests counted; lock, generated and vendor files excluded)", "input": "changed_lines", "weight": 30},
            {"id": "incident_history", "hit": "revert or hotfix on touched files within 90 days", "input": "incident_history_90d", "weight": 30},
            {"id": "change_type", "hit": "dependency", "input": "change_class", "weight": 14},
            {"id": "recent_churn", "hit": ">=5 changes landed on the base branch within 90 days on a changed production file (first-parent history: a merged PR, a squash commit or a direct commit counts 1)",
             "input": "production_churn_90d", "weight": 14},
        ],
    },
}
RUBRIC_PRESET_ID = "agentive:rubric@2"
# what a preset changes against the one before it (the /agentive:update report line when it moves the preset)
RUBRIC_CHANGES = {
    "agentive:rubric@2": "spread counts production modules and production shared paths only (tests and docs left out), size is "
                         ">500 changed lines with weight 30, incident_history weighs 30, change_type scores dependency changes only "
                         "(a feature or fix no longer scores by its type), recent_churn is >=5 changes landed on the base branch "
                         "within 90 days on a changed production file (first-parent history: a merged PR, a squash commit or a "
                         "direct commit counts 1); threshold 28 unchanged",
}
# design spec.review_pipeline.stages[RV7b].rubric (used when .agents/rubric.yaml is missing in the tree)
DEFAULT_RUBRIC = copy.deepcopy(RUBRIC_PRESETS[RUBRIC_PRESET_ID])
RUBRIC_INPUTS = ["modules_touched_or_shared_path", "production_modules_or_shared_path", "changed_lines", "incident_history_90d",
                 "change_class", "recent_churn_30d", "production_churn_90d"]


class Loaded:
    """A config value plus the problems found while loading it."""

    def __init__(self, value, problems=None, source=None):
        self.value, self.problems, self.source = value, problems or [], source

    @property
    def valid(self):
        return not self.problems


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------- review rules (code_review.md block)
def _is_globs(x):
    return isinstance(x, list) and x and all(isinstance(g, str) and g for g in x)


def _entry_problems(e, where, content, only_content=False):
    """Problems of one never_ai_approve / add / content_rules entry (SECPATHS grammar, see review/secpaths.py)."""
    if not isinstance(e, dict):
        return [f"{where} entry {e!r} must be a mapping"]
    bad = []
    extra = set(e) - {"glob", "token", "tokens", "path", "contains", "reason", "source", "note", "shadow"}
    if extra:
        bad.append(f"{where} entry {e!r}: unknown keys {sorted(extra)}")
    forms = [k for k in ("glob", "token", "tokens", "contains") if k in e]
    if e.get("reason") not in NEVER_REASONS:
        bad.append(f"{where} entry {e!r} needs a reason in {NEVER_REASONS}")
    if "contains" in e:
        c = e["contains"]
        if not content:
            bad.append(f"{where} entry {e!r}: path + contains rules go to add or content_rules")
        if not (isinstance(e.get("path"), str) and e["path"]):
            bad.append(f"{where} entry {e!r}: contains needs path")
        if not ((isinstance(c, str) and c) or (isinstance(c, list) and c and all(isinstance(x, str) and x for x in c))):
            bad.append(f"{where} entry {e!r}: contains is a non-empty string or list of strings")
        if forms != ["contains"]:
            bad.append(f"{where} entry {e!r}: a content rule has path and contains only")
    elif only_content:
        bad.append(f"{where} entry {e!r} needs path and contains")
    else:
        if len(forms) != 1 or "path" in e:
            bad.append(f"{where} entry {e!r} needs exactly one of glob, token, tokens (or path + contains)")
        elif forms[0] in ("glob", "token") and not (isinstance(e[forms[0]], str) and e[forms[0]]):
            bad.append(f"{where} entry {e!r}: {forms[0]} must be a non-empty string")
        elif forms[0] == "tokens" and not (isinstance(e["tokens"], list) and e["tokens"] and all(isinstance(x, str) and x for x in e["tokens"])):
            bad.append(f"{where} entry {e!r}: tokens must be a non-empty list of strings")
    if "shadow" in e and not isinstance(e["shadow"], bool):
        bad.append(f"{where} entry {e!r}: shadow must be true or false")
    return bad


def _remove_problems(e):
    """A removal names the entry (glob, token, or path + contains) and must give a reason: it weakens the boundary."""
    if not isinstance(e, dict):
        return [f"remove entry {e!r} must be a mapping"]
    extra = set(e) - {"glob", "token", "path", "contains", "reason"}
    forms = [k for k in ("glob", "token", "contains") if k in e]
    out = []
    if extra or len(forms) != 1 or (("path" in e) != ("contains" in e)):
        out.append(f"remove entry {e!r} names exactly one of glob, token, or path + contains")
    if not (isinstance(e.get("reason"), str) and e["reason"].strip()):
        out.append(f"remove entry {e!r} needs a reason (why this path is not security sensitive here)")
    return out


def validate_rules(doc):
    """Problems of an `agentive-review-rules` document (the subset of review-rules.schema.json the scripts rely on)."""
    p = []
    if not isinstance(doc, dict) or set(doc) != {"agentive-review-rules"}:
        return ["top key must be exactly agentive-review-rules"]
    r = doc["agentive-review-rules"]
    if not isinstance(r, dict):
        return ["agentive-review-rules must be a mapping"]
    extra = set(r) - {"trust_boundary", "classify", "tiers"}
    if extra:
        p.append(f"unknown keys {sorted(extra)}")
    tb = r.get("trust_boundary")
    if not isinstance(tb, dict):
        p.append("trust_boundary missing")
    else:
        nai = tb.get("never_ai_approve")
        if not isinstance(nai, list) or not nai:
            p.append("trust_boundary.never_ai_approve must be a non-empty list")
        else:
            for e in nai:
                p += _entry_problems(e, "never_ai_approve", content=False)
        if "extend_defaults" in tb and not isinstance(tb["extend_defaults"], bool):
            p.append("trust_boundary.extend_defaults must be true or false (default true)")
        for name, content in (("add", True), ("content_rules", True)):
            lst = tb.get(name)
            if lst is None:
                continue
            if not isinstance(lst, list):
                p.append(f"trust_boundary.{name} must be a list")
                continue
            for e in lst:
                p += _entry_problems(e, name, content=content, only_content=name == "content_rules")
        rm = tb.get("remove")
        if rm is not None:
            if not isinstance(rm, list):
                p.append("trust_boundary.remove must be a list")
            else:
                for e in rm:
                    p += _remove_problems(e)
        cs = tb.get("confirm_scope_globs")
        if not isinstance(cs, dict) or set(cs) != set(U2_CATEGORIES):
            p.append(f"confirm_scope_globs must hold exactly {U2_CATEGORIES}")
        else:
            for k, v in cs.items():
                if not _is_globs(v):
                    p.append(f"confirm_scope_globs.{k} needs at least one glob")
        pcc = tb.get("protected_check_config")
        if pcc is not None and not (isinstance(pcc, list) and all(isinstance(g, str) and g for g in pcc)):
            p.append("protected_check_config must be a list of globs")
    cl = r.get("classify")
    if cl is not None:
        if not isinstance(cl, dict):
            p.append("classify must be a mapping")
        else:
            for e in cl.get("path_rules") or []:
                if not isinstance(e, dict) or not isinstance(e.get("glob"), str) or e.get("class") not in CLASSES:
                    p.append(f"classify.path_rules entry {e!r} needs glob and a class in {CLASSES}")
    t = r.get("tiers")
    if not isinstance(t, dict):
        p.append("tiers missing")
    else:
        if t.get("confirm_scope_without_human_tier") != 2:
            p.append("tiers.confirm_scope_without_human_tier is the constant 2")
        t0 = t.get("tier0_classes")
        if not isinstance(t0, list) or any(c not in TIER0_ELIGIBLE for c in t0):
            p.append(f"tiers.tier0_classes may hold only {TIER0_ELIGIBLE}")
        for k in ("tier0_max_changed_lines", "large_change_lines"):
            if not isinstance(t.get(k), int) or isinstance(t.get(k), bool) or t.get(k) < 0:
                p.append(f"tiers.{k} must be a non-negative integer")
        t2 = t.get("tier2_classes")
        if t2 is not None and (not isinstance(t2, list) or any(c not in CLASSES for c in t2)):
            p.append("tiers.tier2_classes must be classes")
    return p


def floor_keys():
    return {secpaths.key(e) for e in secpaths.expand(DEFAULT_RULES["trust_boundary"]["never_ai_approve"])}


def effective_trust_boundary(block):
    """The trust boundary RV2 and the PR-body estimate use: the shipped floor merged with the block (SECPATHS; before it the block
    replaced the defaults, so an improved floor never reached a repo that had the block).

    never_ai_approve = [floor if extend_defaults (default true)] + the block's list + its `add` path/token entries, tokens
    expanded, minus the `remove` entries (matched on glob / token / path+contains), first entry per key, an enforced entry
    beating a shadow entry. content_rules = the block's content_rules + its `add` content entries, minus `remove`.
    confirm_scope_globs = floor union block when extend_defaults, else the block's lists. `removed` keeps the removals with their
    reasons for the report; `protected_check_config` is unchanged and cannot be removed."""
    dflt = DEFAULT_RULES["trust_boundary"]
    ext = block.get("extend_defaults", True)
    add = secpaths.expand(block.get("add") or [])
    rm = secpaths.removed_keys(block.get("remove") or [])
    nai = ((secpaths.expand(dflt["never_ai_approve"]) if ext else []) + secpaths.expand(block["never_ai_approve"])
           + [dict(e, source=e.get("source", "add")) for e in add if "contains" not in e])
    content = (secpaths.expand(block.get("content_rules") or [])
               + [dict(e, source=e.get("source", "add")) for e in add if "contains" in e])
    cs = {}
    for cat in U2_CATEGORIES:
        mine = list(block["confirm_scope_globs"].get(cat) or [])
        cs[cat] = util.uniq((list(dflt["confirm_scope_globs"][cat]) if ext else []) + mine)
    out = {"never_ai_approve": secpaths.dedupe([e for e in nai if secpaths.key(e) not in rm]),
           "content_rules": secpaths.dedupe([e for e in content if secpaths.key(e) not in rm]),
           "confirm_scope_globs": cs}
    if block.get("remove"):
        out["removed"] = copy.deepcopy(block["remove"])
    if block.get("protected_check_config") is not None:
        out["protected_check_config"] = list(block["protected_check_config"])
    return out


def load_rules(source, rules_file="code_review.md"):
    """Loaded(rules dict without the top key). source(path) -> text | None (the base tree)."""
    text = source(rules_file)
    if text is None:
        return Loaded(copy.deepcopy(DEFAULT_RULES), [], f"design defaults ({rules_file} absent)")
    blocks = util.fenced_blocks(text, "yaml agentive-review-rules")
    if not blocks:
        return Loaded(copy.deepcopy(DEFAULT_RULES), [], f"design defaults (no agentive-review-rules block in {rules_file})")
    try:
        doc = ayaml.loads(blocks[0])
    except ayaml.YAMLError as e:
        return Loaded(copy.deepcopy(DEFAULT_RULES), [f"{rules_file}: {e}"], rules_file)
    probs = validate_rules(doc)
    if probs:
        return Loaded(copy.deepcopy(DEFAULT_RULES), [f"{rules_file}: {x}" for x in probs], rules_file)
    r = deep_merge(DEFAULT_RULES, {})
    got = doc["agentive-review-rules"]
    r["trust_boundary"] = effective_trust_boundary(got["trust_boundary"])
    r["classify"] = deep_merge(DEFAULT_RULES["classify"], got.get("classify") or {})
    r["tiers"] = deep_merge(DEFAULT_RULES["tiers"], got["tiers"])
    return Loaded(r, [], rules_file)


# ---------------------------------------------------------------- review config (.agents/framework.yaml review.pipeline)
def validate_config(cfg):
    p = []
    if not isinstance(cfg, dict):
        return ["review.pipeline must be a mapping"]
    extra = set(cfg) - {"stages", "profiles", "rules_file", "auto_merge", "criteria", "ci_tool", "ci_auth"}
    if extra:
        p.append(f"unknown keys {sorted(extra)}")
    if "ci_tool" in cfg and cfg["ci_tool"] not in CI_TOOLS:
        p.append("ci_tool must be claude or codex (absent = the tool that ran init)")
    if "ci_auth" in cfg and cfg["ci_auth"] not in CI_AUTH:
        p.append("ci_auth must be " + " | ".join(CI_AUTH) + f" (absent = {CI_AUTH_DEFAULT}; claude only)")
    st = cfg.get("stages") or {}
    rv5 = (st.get("RV5") or {})
    rv = rv5.get("reviewers")
    if rv is not None:
        if not isinstance(rv, list) or any(x not in REVIEWERS for x in rv) or "correctness-reviewer" not in rv or "security-reviewer" not in rv:
            p.append("stages.RV5.reviewers must keep correctness-reviewer and security-reviewer and name only " + ", ".join(REVIEWERS))
    sh = rv5.get("shadow_reviewers")
    if sh is not None and (not isinstance(sh, list) or any(x not in REVIEWERS for x in sh)):
        p.append("stages.RV5.shadow_reviewers must name reviewers")
    if "context" in rv5 and rv5["context"] != "fresh":
        p.append("stages.RV5.context is the constant fresh")
    rc = (st.get("RV1") or {}).get("require_checks")
    if rc is not None and (not isinstance(rc, list) or any(x not in ("lint", "typecheck", "test", "build", "ci") for x in rc)):
        p.append("stages.RV1.require_checks must be from lint, typecheck, test, build, ci")
    ll = (st.get("RV3") or {}).get("llm_layer")
    if ll is not None and not isinstance(ll, bool):
        p.append("stages.RV3.llm_layer must be a boolean")
    if (st.get("RV8") or {}).get("check_name", "agentive-review-gate") != "agentive-review-gate":
        p.append("stages.RV8.check_name is the constant agentive-review-gate")
    am = cfg.get("auto_merge") or {}
    if "tier1" in am and am["tier1"] not in ("shadow", "on", "off"):
        p.append("auto_merge.tier1 must be shadow, on or off (deprecated and ignored since DEFAULTS: use review.auto_merge)")
    ks = am.get("kill_switch")
    if ks is not None:
        const = {"count": 2, "window_days": 7, "scope": "tier_and_source", "state_label": "agentive:auto-merge-demoted",
                 "reenable": "human_closes_state_issue"}
        for k, v in ks.items():
            if const.get(k, object()) != v:
                p.append(f"auto_merge.kill_switch.{k} is a constant ({const.get(k)!r})")
    prof = cfg.get("profiles") or {}
    if (prof.get("precommit") or {}).get("stages", PRECOMMIT_STAGES) != PRECOMMIT_STAGES:
        p.append("profiles.precommit.stages is the constant [RV1, RV2, RV5]")
    if (prof.get("ci") or {}).get("stages", STAGES) != STAGES:
        p.append("profiles.ci.stages is the constant order " + " -> ".join(STAGES))
    prv = (prof.get("precommit") or {}).get("reviewers")
    if prv is not None and (not isinstance(prv, list) or not prv or any(x not in REVIEWERS for x in prv)):
        p.append("profiles.precommit.reviewers must name at least one reviewer")
    cr = cfg.get("criteria") or {}
    const = {"scope": "ancestor_directory", "one_kind_per_file": True, "no_applicable_file": "normal"}
    for k, v in const.items():
        if k in cr and cr[k] != v:
            p.append(f"criteria.{k} is the constant {v!r}")
    ff = cr.get("fallback_files")
    if ff is not None and (not isinstance(ff, list) or not ff or any(x not in ("CLAUDE.md", "AGENTS.md") for x in ff)):
        p.append("criteria.fallback_files must be CLAUDE.md and/or AGENTS.md")
    if "rules_file" in cfg and not (isinstance(cfg["rules_file"], str) and cfg["rules_file"]):
        p.append("rules_file must be a path")
    return p


def load_project_config(source):
    """Loaded(review.pipeline merged over the defaults) + managed_code_review_copy, from .agents/framework.yaml."""
    text = source(".agents/framework.yaml")
    if text is None:
        return Loaded(copy.deepcopy(DEFAULT_CONFIG), [], "defaults (.agents/framework.yaml absent)")
    try:
        doc = ayaml.loads(text) or {}
    except ayaml.YAMLError as e:
        return Loaded(copy.deepcopy(DEFAULT_CONFIG), [f".agents/framework.yaml: {e}"], ".agents/framework.yaml")
    pipe = ((doc.get("review") or {}) if isinstance(doc, dict) else {}).get("pipeline") or {}
    probs = validate_config(pipe)
    if probs:
        return Loaded(copy.deepcopy(DEFAULT_CONFIG), [f".agents/framework.yaml review.pipeline: {x}" for x in probs], ".agents/framework.yaml")
    return Loaded(deep_merge(DEFAULT_CONFIG, pipe), [], ".agents/framework.yaml")


def pipeline_section(source):
    """The raw review.pipeline mapping (reviewer-change detection compares it between two trees)."""
    text = source(".agents/framework.yaml")
    if text is None:
        return None
    try:
        doc = ayaml.loads(text) or {}
    except ayaml.YAMLError:
        return {"_unparsable": text}
    return ((doc.get("review") or {}) if isinstance(doc, dict) else {}).get("pipeline")


# ---------------------------------------------------------------- rubric (preset + override)
def _validate_rubric_items(items, need_all):
    p = []
    if not isinstance(items, list):
        return ["items must be a list"]
    for it in items:
        if not isinstance(it, dict) or not re.match(r"^[a-z][a-z0-9_]*$", str(it.get("id", ""))):
            p.append(f"item {it!r} needs an id")
            continue
        extra = set(it) - {"id", "hit", "input", "weight", "enabled"}
        if extra:
            p.append(f"item {it['id']}: unknown keys {sorted(extra)}")
        if need_all and not all(k in it for k in ("hit", "input", "weight")):
            p.append(f"item {it['id']}: needs hit, input and weight")
        if "input" in it and it["input"] not in RUBRIC_INPUTS:
            p.append(f"item {it['id']}: input must be one of {RUBRIC_INPUTS}")
        if "weight" in it and (not isinstance(it["weight"], int) or isinstance(it["weight"], bool) or not 0 <= it["weight"] <= 100):
            p.append(f"item {it['id']}: weight must be an integer 0..100")
        if "enabled" in it and not isinstance(it["enabled"], bool):
            p.append(f"item {it['id']}: enabled must be a boolean")
    return p


def validate_rubric_preset(doc):
    if not isinstance(doc, dict):
        return ["rubric must be a mapping"]
    p = []
    extra = set(doc) - {"preset", "scoring", "human_if_score_gt", "shared_globs", "items"}
    if extra:
        p.append(f"unknown keys {sorted(extra)}")
    if not re.match(r"^agentive:rubric@[0-9]+$", str(doc.get("preset", ""))):
        p.append("preset must be agentive:rubric@<n>")
    if doc.get("scoring") != "binary":
        p.append("scoring must be binary")
    h = doc.get("human_if_score_gt")
    if not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 100:
        p.append("human_if_score_gt must be an integer 0..100")
    if not doc.get("items"):
        p.append("items must not be empty")
    p += _validate_rubric_items(doc.get("items") or [], True)
    return p


def validate_rubric_override(doc):
    if not isinstance(doc, dict):
        return ["override must be a mapping"]
    p = []
    extra = set(doc) - {"extends", "human_if_score_gt", "shared_globs", "items"}
    if extra:
        p.append(f"unknown keys {sorted(extra)}")
    if not re.match(r"^agentive:rubric@[0-9]+$", str(doc.get("extends", ""))):
        p.append("extends must be agentive:rubric@<n>")
    if "human_if_score_gt" in doc:
        h = doc["human_if_score_gt"]
        if not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 100:
            p.append("human_if_score_gt must be an integer 0..100")
    p += _validate_rubric_items(doc.get("items") or [], False)
    return p


def effective_rubric(source):
    """Loaded(effective rubric) from .agents/rubric.yaml and .agents/custom/.agents/rubric.yaml (merged by item id)."""
    probs = []
    text = source(".agents/rubric.yaml")
    if text is None:
        preset = copy.deepcopy(DEFAULT_RUBRIC)
    else:
        try:
            preset = ayaml.loads(text)
        except ayaml.YAMLError as e:
            return Loaded(None, [f".agents/rubric.yaml: {e}"])
        pp = validate_rubric_preset(preset)
        if pp:
            return Loaded(None, [f".agents/rubric.yaml: {x}" for x in pp])
    otext = source(".agents/custom/.agents/rubric.yaml")
    eff = copy.deepcopy(preset)
    eff.setdefault("shared_globs", ["**/shared/**", "**/common/**"])
    if otext is not None:
        try:
            over = ayaml.loads(otext)
        except ayaml.YAMLError as e:
            return Loaded(None, [f".agents/custom/.agents/rubric.yaml: {e}"])
        op = validate_rubric_override(over)
        if not op and over.get("extends") != preset.get("preset"):
            if over.get("extends") in RUBRIC_PRESETS:
                # a pinned framework preset (E2E-13: @1 stays pinnable): the override is merged onto that preset
                eff = copy.deepcopy(RUBRIC_PRESETS[over["extends"]])
            else:
                op.append(f"extends {over.get('extends')} is neither the preset {preset.get('preset')} nor a framework preset "
                          f"({', '.join(sorted(RUBRIC_PRESETS))})")
        elif not op and len(over) < 2:
            op.append("an override holds extends and at least one change (or extends names another framework preset to pin it)")
        if op:
            return Loaded(None, [f".agents/custom/.agents/rubric.yaml: {x}" for x in op])
        if "human_if_score_gt" in over:
            eff["human_if_score_gt"] = over["human_if_score_gt"]
        if "shared_globs" in over:
            eff["shared_globs"] = list(over["shared_globs"])
        by_id = {it["id"]: it for it in eff["items"]}
        for it in over.get("items") or []:
            if it["id"] in by_id:
                by_id[it["id"]].update(copy.deepcopy(it))
            else:
                if not all(k in it for k in ("hit", "input", "weight")):
                    probs.append(f".agents/custom/.agents/rubric.yaml: new item {it['id']} needs hit, input and weight")
                    continue
                eff["items"].append(copy.deepcopy(it))
                by_id[it["id"]] = eff["items"][-1]
    if probs:
        return Loaded(None, probs)
    for it in eff["items"]:
        it.setdefault("enabled", True)
    return Loaded(eff, [])


def rubric_sha256(eff):
    return util.canonical_sha256(eff)


# ---------------------------------------------------------------- review.auto_merge (DEFAULTS), manifest
# decisions.accepted[DEFAULTS] (user 2026-10-29): defaults live in framework code; .agents/framework.yaml holds overrides only.
AUTO_MERGE_VALUES = ("off", "dry_run", "low_risk", "low_and_medium_risk")
AUTO_MERGE_DEFAULT = "low_risk"        # = the original U1=C behaviour: tier 0 merges, tier 1 runs in shadow (U1.d)
AUTO_MERGE_LABEL = "agentive:shadow-auto-merge"
AUTO_MERGE_MEANING = {
    "off": "nothing is merged automatically; a human merges every PR",
    "dry_run": "nothing is merged automatically; a PR (tier 0 included) that would have been merged gets the label " + AUTO_MERGE_LABEL,
    "low_risk": "tier 0 PRs (mechanically verified changes) are merged automatically; tier 1 runs in shadow mode: a tier 1 PR that would "
                "qualify gets the label " + AUTO_MERGE_LABEL + " and a human merges it",
    "low_and_medium_risk": "tier 0 and tier 1 PRs are merged automatically (tier 1 only with a rubric score at or below the threshold)",
}
AUTO_MERGE_SHORT = {"off": "nothing is merged automatically", "dry_run": "nothing is merged automatically, would-be merges are labelled",
                    "low_risk": "tier 0 on, tier 1 shadow", "low_and_medium_risk": "tier 0 on, tier 1 on"}
AUTO_MERGE_RULES = "tier 2 PRs are always merged by a human; GitHub 'Allow auto-merge' and the required check agentive-review-gate must be on for a merge to happen"


def load_auto_merge(source):
    """Loaded(value) for review.auto_merge in .agents/framework.yaml, source = (path -> text | None) of the base tree.
    Absent file or key -> the framework default low_risk; unparsable file or an unknown value -> off (fail-closed: nothing is
    merged) with a problem. Loaded.source says where the value comes from."""
    text = source(".agents/framework.yaml")
    if text is None:
        return Loaded(AUTO_MERGE_DEFAULT, [], "framework default")
    try:
        doc = ayaml.loads(text) or {}
    except ayaml.YAMLError as e:
        return Loaded("off", [f".agents/framework.yaml: {e}; review.auto_merge treated as off"], ".agents/framework.yaml")
    rv = doc.get("review") if isinstance(doc, dict) else None
    if not isinstance(rv, dict) or "auto_merge" not in rv:
        return Loaded(AUTO_MERGE_DEFAULT, [], "framework default")
    v = rv["auto_merge"]
    if v not in AUTO_MERGE_VALUES:
        return Loaded("off", [f".agents/framework.yaml review.auto_merge {v!r} is not one of {', '.join(AUTO_MERGE_VALUES)}; treated as off"],
                      ".agents/framework.yaml")
    return Loaded(v, [], ".agents/framework.yaml")


def migrate_decisions(decisions_doc, pipeline_doc=None):
    """V1-18 migration of a v0.1.0 .agents/decisions.yaml (parsed) and review.pipeline.auto_merge.tier1.
    -> {"auto_merge": value or None, "notes": [text]}. None = nothing to write (the value is, or maps to, the framework default).
    What v0.1.0 init wrote (U1=C with tier 1 in shadow, U2=B, U6=agentive:rubric@1) is the framework default, not a record of
    the user's choice, so it is dropped; a user's own change is kept: U1 A -> off, U1 C with tier1 on -> low_and_medium_risk.
    U1 B (tier 0 only) and U1 C with tier1 shadow or off equal the default low_risk (tier 1 only gains the shadow label). U2 is a framework constant and U6 is the preset named in .agents/rubric.yaml; a different value there
    cannot be kept and is reported."""
    notes = []
    doc = decisions_doc if isinstance(decisions_doc, dict) else {}
    u1 = (doc.get("U1") or {}).get("value") if isinstance(doc.get("U1"), dict) else None
    am = (pipeline_doc or {}).get("auto_merge") if isinstance(pipeline_doc, dict) else None
    tier1 = am.get("tier1") if isinstance(am, dict) else None
    value = None
    if u1 == "A":
        value = "off"
    elif u1 == "B":
        notes.append("U1=B (tier 0 auto-merge only) equals the default review.auto_merge low_risk: dropped (tier 1 PRs now also get the shadow label)")
    elif u1 == "C" and tier1 == "on":
        value = "low_and_medium_risk"
    elif u1 == "C":
        notes.append("U1=C (tier 0 auto-merge, tier 1 in shadow) was the v0.1.0 default and is still the default (review.auto_merge: low_risk): dropped")
    elif u1 is not None:
        notes.append(f"U1 value {u1!r} is unknown: dropped")
    u2 = doc.get("U2")
    if isinstance(u2, dict) and (u2.get("value") != "B" or u2.get("non_interactive") != "pr_only_tier2"
                                 or set(u2.get("confirm_scope") or []) != set(U2_CATEGORIES)):
        notes.append("U2 is a framework constant now (confirm_scope hooks, permissions, ci, claude_settings, codex_dir, review_config; "
                     "non-interactive changes open a tier 2 PR): the U2 override in decisions.yaml is ignored")
    u6 = doc.get("U6")
    if isinstance(u6, dict) and u6.get("value") not in (None, *RUBRIC_PRESETS):
        notes.append(f"U6 {u6.get('value')!r}: the rubric preset is the name in .agents/rubric.yaml; decisions.yaml is not read")
    if tier1 is not None:
        notes.append("review.pipeline.auto_merge.tier1 is ignored now (replaced by review.auto_merge): delete the key")
    return {"auto_merge": value, "notes": notes}


def load_manifest(source):
    text = source(".agents/manifest.yaml")
    if text is None:
        return Loaded({}, [".agents/manifest.yaml absent"])
    try:
        doc = ayaml.loads(text) or {}
    except ayaml.YAMLError as e:
        return Loaded({}, [f".agents/manifest.yaml: {e}"])
    return Loaded(doc if isinstance(doc, dict) else {}, [])


def manifest_paths(manifest, key):
    return [e.get("path") for e in ((manifest.get("paths") or {}).get(key) or []) if isinstance(e, dict) and e.get("path")]


def path_glob(p):
    """A manifest path entry ('dist', 'dist/', 'vendor/**', '*.gen.go') as a glob."""
    p = p.strip()
    while p.startswith("./"):
        p = p[2:]
    if any(c in p for c in "*?["):
        return p
    return p.rstrip("/") + "/**" if p else p


def pending_verbs(manifest):
    out = []
    for name, v in (manifest.get("verbs") or {}).items():
        if isinstance(v, dict) and v.get("status") == "pending":
            out.append(name)
    return sorted(out)


def pending_required(manifest, conf):
    """The pending verbs that are also required checks (review.pipeline.stages.RV1.require_checks). While there are any, RV8 sends
    every PR to a human (reason pending_verbs): auto-merge is unavailable (V2-23, PENDINGNOTICE)."""
    req = (((conf or {}).get("stages") or {}).get("RV1") or {}).get("require_checks") or []
    return [v for v in pending_verbs(manifest) if v in req]


def unavailable_text(verbs):
    """The one sentence that says why auto-merge cannot happen and the first way out (PENDINGNOTICE). The second way out
    (a project without that verb removes it from require_checks) is printed next to it by the engine report and by
    `agentive-lint settings --effective`."""
    return f"{common.no_command(verbs)} -> give the command or run /agentive:update"


REQUIRE_CHECKS_EXIT = ("if this project has no such check, remove it from review.pipeline.stages.RV1.require_checks "
                       "in .agents/framework.yaml (the gate then stops waiting for it)")
