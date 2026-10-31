#!/bin/sh
# agentive-unit: rl.skill.chain-stage-check
# design: spec.spec_stage.chain.start_check, spec.spec_stage.chain.flow, spec.repo_layer.files[f-chain-skills], spec.no_human_run, decisions.accepted[BASEREF]
# One source installed as .agents/skills/chain-{intent,spec,plan}/scripts/stage-check.sh; tools/generate.py bakes the
# skill directory of each copy into it (SKILLPATHS), so the stage never depends on the path it was called with.
# Usage, from the repository root: sh .agents/skills/chain-<stage>/scripts/stage-check.sh <slug>
# Checks only the previous artifact on the default branch of origin. Exit 0 prints AGENTIVE_CHAIN=ok:...;
# exit 1 prints one line `AGENTIVE_CHAIN=stop:<stage>:<slug> NEXT=<action>` and the agent stops and tells the human.
set -eu

skill=chain-spec
stage=${skill#chain-}
slug=${1:-}

stop() {
  echo "AGENTIVE_CHAIN=stop:$stage:${slug:-none} NEXT=$1"
  exit 1
}

case "$stage" in intent|spec|plan) ;; *) stop "reinstall the chain skills with /agentive:update (Codex: \$agentive-update); this copy names no chain stage" ;; esac
[ -n "$slug" ] || stop "pass the slug: sh .agents/skills/$skill/scripts/stage-check.sh <slug>"
if [ "$slug" = archive ] || ! printf '%s\n' "$slug" | grep -Eq '^[a-z0-9]+(-[a-z0-9]+)*$'; then
  stop "use a kebab-case slug other than archive (for example add-rate-limit)"
fi

# The plan stage needs a human approving the plan in plan mode (spec_stage.chain.flow[plan].no_human).
if [ "$stage" = plan ]; then
  if [ "${AGENTIVE_HEADLESS:-}" = 1 ] || [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
    stop "plan approval needs a human: run /chain-plan $slug in an interactive session"
  fi
fi

git rev-parse --git-dir >/dev/null 2>&1 || stop "run inside the git repository"

# Default branch and base ref (BASEREF, E2E-06): origin/HEAD when the ref it names exists, else the remote's HEAD;
# fetched once with the explicit refspec, so a clone without a fetch refspec still gets refs/remotes/origin/<default>.
remote_head() {
  GIT_TERMINAL_PROMPT=0 git ls-remote --symref origin HEAD 2>/dev/null | sed -n 's|^ref: refs/heads/\([^[:space:]]*\)[[:space:]]*HEAD$|\1|p' | head -n 1
}
fetch_default() {
  GIT_TERMINAL_PROMPT=0 git fetch --quiet --no-tags origin "+refs/heads/$1:refs/remotes/origin/$1" 2>&1
}
default=
ref=$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)
if [ -n "$ref" ] && git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
  default=${ref#refs/remotes/origin/}
fi
[ -n "$default" ] || default=$(remote_head)
[ -n "$default" ] || stop "check that origin is reachable (git ls-remote --symref origin HEAD), then rerun sh .agents/skills/$skill/scripts/stage-check.sh $slug"

fetched=0
if err=$(fetch_default "$default"); then
  fetched=1
else
  case $err in
    *"couldn't find remote ref"*)
      new=$(remote_head)
      if [ -n "$new" ] && [ "$new" != "$default" ]; then
        echo "NOTE: origin/HEAD=$default remote HEAD=$new; using $new (refresh with git remote set-head origin --auto)"
        default=$new
        if fetch_default "$default" >/dev/null; then fetched=1; fi
      fi ;;
  esac
fi
if [ "$fetched" = 1 ]; then
  if [ "$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)" != "refs/remotes/origin/$default" ]; then
    git remote set-head origin "$default" >/dev/null 2>&1 || true
  fi
else
  echo "NOTE: git fetch of $default failed; checking the local origin/$default instead"
fi
git rev-parse --verify --quiet "refs/remotes/origin/$default" >/dev/null || stop "git fetch origin +refs/heads/$default:refs/remotes/origin/$default, then rerun sh .agents/skills/$skill/scripts/stage-check.sh $slug"

has() {
  git cat-file -e "refs/remotes/origin/$default:docs/plans/$slug/$1" 2>/dev/null
}

case "$stage" in
  intent)
    if has intent.md; then
      stop "docs/plans/$slug/intent.md is already on $default: ask the human whether this is the same change; if not, pick a new slug"
    fi ;;
  spec)
    has intent.md || stop "merge the intent PR (intent/$slug) first, then run /chain-spec $slug" ;;
  plan)
    has spec.md || stop "merge the spec PR (spec/$slug) first, then run /chain-plan $slug" ;;
esac

echo "AGENTIVE_CHAIN=ok:$stage:$slug DEFAULT=$default"
