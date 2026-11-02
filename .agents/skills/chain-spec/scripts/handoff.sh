#!/bin/sh
# agentive-unit: rl.skill.chain-spec
# design: spec.handoff.writers, spec.spec_stage.insufficient_spec, spec.skill_scripts, spec.context_management.headless.standard_cmd
# Usage: sh .agents/skills/chain-spec/scripts/handoff.sh "<the question>" "<what the human must decide>"
# Writes the open_question handoff of a headless run that must stop (.agents/state/handoff/<branch>.md) and prints
# HANDOFF=<path>. A skill script, so the headless allowlist `Bash(sh .agents/skills/*)` covers it (V1-03).
set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "AGENTIVE_HANDOFF=stop reason=not_a_git_repo; NEXT=run inside the repository"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "AGENTIVE_HANDOFF=stop reason=python3_missing; NEXT=install python3 >= 3.13"; exit 2; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1 || { echo "AGENTIVE_HANDOFF=stop reason=python3_too_old; NEXT=install python3 >= 3.13"; exit 2; }      # E2E-46: the floor check, same NEXT as the engine
[ "$#" -eq 2 ] || { echo "AGENTIVE_HANDOFF=stop reason=usage; NEXT=sh .agents/skills/chain-spec/scripts/handoff.sh \"<the question>\" \"<what the human must decide>\""; exit 2; }
cd "$root"
PYTHONPATH="$root/.agents/lib${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -B -m agentive.handoff write --reason open_question --failure-reason "$1" --next-step "$2"
