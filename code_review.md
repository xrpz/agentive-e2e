<!-- AGENTIVE START rules -->
<!-- Managed by agentive; edits inside this region are reported by /agentive:update and replaced only with --force or when you accept them. Keep text you want preserved outside the markers. -->
## agentive review rules (generated)

The block below is machine data for the review pipeline (what a reviewer checks is the `## Code Review Rules` section of AGENTS.md): the trust boundary (paths a human must review), the confirm scope (changes whose commit needs a human's yes), the classification of changes and the review tiers. Scripts read it from the base commit, never from the PR branch. To change it, put a fenced `yaml agentive-review-rules` block with only your changes in `.agents/custom/code_review.md`; it is merged into this block. Security-sensitive paths: `trust_boundary.add` adds a path glob `{glob, reason}`, a path token `{token, reason}` or a content rule `{path, contains, reason}`; `trust_boundary.remove` drops one with `{glob, reason}` and the reason is required; `extend_defaults: false` ignores the shipped floor. Matching is case-sensitive; a glob also covers the directory it names; a token matches one word of a path segment (`auth` matches `auth_service.py`, never `author.py`). Lines with `source: codeowners:<n>` or `source: preset:<id>` are written by /agentive:init and /agentive:update from your CODEOWNERS and your stack; do not edit them here, change CODEOWNERS or use `remove`.

```yaml agentive-review-rules
agentive-review-rules:
  trust_boundary:
    extend_defaults: true
    never_ai_approve:
      - {glob: "**/auth/**", reason: auth}
      - {glob: "**/security/**", reason: security}
      - {glob: "**/permissions*", reason: permissions}
      - {glob: "**/*secret*", reason: secrets}
      - {glob: "**/.env*", reason: secrets}
      - {glob: "**/migrations/**", reason: db_migration}
      - {glob: ".github/workflows/**", reason: ci_config}
      - {glob: ".claude/settings*.json", reason: agent_hooks_settings}
      - {glob: "**/hooks.json", reason: agent_hooks_settings}
      - {glob: ".codex/**", reason: agent_hooks_settings}
      - {glob: "**/billing/**", reason: billing}
      - {glob: "**/payment*/**", reason: billing}
      - {glob: "**/SECURITY.md", reason: security}
      - {tokens: [auth, authn, authz, authentication, authorization, login, oauth, oauth2, oidc, saml, sso, jwt], reason: auth}
      - {tokens: [secret, secrets, credential, credentials, vault, kms], reason: secrets}
      - {tokens: [permission, permissions, rbac, acl, iam], reason: permissions}
      - {tokens: [security, crypto, cipher, tls, ssl, cert, certs], reason: security}
      - {tokens: [billing, payment, payments, invoice, invoices], reason: billing}
      - {tokens: [migration, migrations, migrate], reason: db_migration}
    confirm_scope_globs:
      hooks: ["**/hooks.json", "plugins/**/hooks/**", ".agents/hooks/**"]
      permissions: ["**/permissions*", ".agents/guard.yaml", ".agents/custom/.agents/guard.yaml"]
      ci: [".github/workflows/**"]
      claude_settings: [".claude/settings*.json"]
      codex_dir: [".codex/**"]
      review_config: [".agents/framework.yaml", ".agents/custom/.agents/rubric.yaml"]
  classify:
    whitespace_only_is_format: true
  tiers:
    tier0_classes: [docs, format]
    tier0_max_changed_lines: 200
    large_change_lines: 1000
    confirm_scope_without_human_tier: 2
    tier2_classes: [security]
```
<!-- AGENTIVE END rules -->
