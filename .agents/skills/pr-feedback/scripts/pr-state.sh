#!/bin/sh
# agentive-unit: rl.skill.pr-feedback
# design: spec.work_loop.pr_feedback.reads, spec.skill_scripts, spec.outer_loop.cloud
# Usage: sh .agents/skills/pr-feedback/scripts/pr-state.sh [<pr number>]   (default: the open PR of the current branch)
# Reads the PR once through gh api (never gh pr), writes .agents/state/pr-state-<n>.json, prints a summary.
set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "PR_STATE=stop reason=not_a_git_repo; NEXT=run inside the repository"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "PR_STATE=stop reason=python3_missing; NEXT=install python3 >= 3.9"; exit 2; }
command -v gh >/dev/null 2>&1 || { echo "PR_STATE=stop reason=gh_missing; NEXT=install the GitHub CLI (gh) and run gh auth login"; exit 2; }
PYTHONPATH="$root/.agents/lib${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -B -m agentive.prstate "$@"
