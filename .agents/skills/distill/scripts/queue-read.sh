#!/bin/sh
# agentive-unit: rl.skill.distill
# design: spec.maintenance.harvest.distill, spec.skill_scripts, contracts[harvest-queue]
# Usage: sh .agents/skills/distill/scripts/queue-read.sh [<queue path>] [--done <session_id> ...]
# Lists harvest-queue entries ready for distill (transcript >= 30 s old); --done removes handled entries.
set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "QUEUE=stop reason=not_a_git_repo; NEXT=run inside the repository"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "QUEUE=stop reason=python3_missing; NEXT=install python3 >= 3.9"; exit 2; }
PYTHONPATH="$root/.agents/lib${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -B -m agentive.distillq "$@"
