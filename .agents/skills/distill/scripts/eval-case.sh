#!/bin/sh
# agentive-unit: rl.skill.distill
# design: spec.review_pipeline.reviewer_eval.intake, spec.skill_scripts, contracts[reviewer-eval-case], decisions.accepted[EVALBUNDLE]
# Usage: sh .agents/skills/distill/scripts/eval-case.sh draft positive|negative --id <id> --pr <#N|base..head> --base-sha <sha> --head-sha <sha> ...
# Drafts evals/reviewer/<id>.yaml for a confirmed review escape or review noise (arguments: .agents/lib/agentive/evalcase.py).
set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "EVALCASE=stop reason=not_a_git_repo; NEXT=run inside the repository"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "EVALCASE=stop reason=python3_missing; NEXT=install python3 >= 3.13"; exit 2; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1 || { echo "EVALCASE=stop reason=python3_too_old; NEXT=install python3 >= 3.13"; exit 2; }      # E2E-46: the floor check, same NEXT as the engine
PYTHONPATH="$root/.agents/lib${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -B -m agentive.evalcase "$@"
