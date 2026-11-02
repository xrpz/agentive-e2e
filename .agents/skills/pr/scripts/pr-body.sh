#!/bin/sh
# agentive-unit: rl.skill.pr
# design: spec.work_loop.pr_skill, spec.work_loop.pr_explainer.add_later, spec.skill_scripts, contracts[pr-body-fw-review]
# Usage: sh .agents/skills/pr/scripts/pr-body.sh draft [--explain] [--tool T --model M] [--intent-skipped] [--hotfix-of N]
#        sh .agents/skills/pr/scripts/pr-body.sh check <file> [--explainer required] | publish <file> [--title T] [--label NAME]...
# Thin wrapper: the logic is .agents/lib/agentive/prbody.py (python3 stdlib); gh api only, never gh pr.
set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "AGENTIVE_PR=stop reason=not_a_git_repo; NEXT=run inside the repository"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "AGENTIVE_PR=stop reason=python3_missing; NEXT=install python3 >= 3.13"; exit 2; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1 || { echo "AGENTIVE_PR=stop reason=python3_too_old; NEXT=install python3 >= 3.13"; exit 2; }      # E2E-46: the floor check, same NEXT as the engine
PYTHONPATH="$root/.agents/lib${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -B -m agentive.prbody "$@"
