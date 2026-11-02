#!/bin/sh
# agentive-unit: rl.skill.janitor
# design: spec.maintenance.janitor.gate, spec.maintenance.janitor.pr_limits, spec.outer_loop.cloud
# Usage: sh .agents/skills/janitor/scripts/gate.sh <kind>
# One open PR per kind: exit 0 with `GO <kind>` when no open PR carries the label agentive-janitor:<kind>;
# exit 1 with the final message `SKIP <kind>: #N open` when one does; exit 2 when gh api fails.
# gh api only (works in Routines runs, where gh pr is blocked).
set -eu
kind=${1:-}
if [ -z "$kind" ] || ! printf '%s\n' "$kind" | grep -Eq '^[a-z0-9][a-z0-9-]*$'; then
  echo "AGENTIVE_FAIL tool=janitor-gate reason=bad_kind; NEXT=pass a kind id from .agents/manifest.yaml janitor.kinds"
  exit 2
fi
command -v gh >/dev/null 2>&1 || { echo "AGENTIVE_FAIL tool=janitor-gate reason=gh_missing; NEXT=install the GitHub CLI (gh) and authenticate"; exit 2; }
# owner/repo from the origin URL (also a proxied clone URL); gh's {owner}/{repo} placeholders otherwise
repo=$(git remote get-url origin 2>/dev/null | sed -E 's#(\.git)?/*$##; s#^.*[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$#\1#' || true)
case "$repo" in */*) ;; *) repo='{owner}/{repo}' ;; esac
if ! open=$(gh api -X GET "repos/$repo/issues" -f state=open -f "labels=agentive-janitor:$kind" -f per_page=100 \
    --jq '[.[] | select(.pull_request != null) | .number] | first // empty' 2>/dev/null); then
  echo "AGENTIVE_FAIL tool=janitor-gate reason=gh_api_failed; NEXT=check gh auth status and the origin remote, then rerun"
  exit 2
fi
if [ -n "$open" ]; then
  echo "SKIP $kind: #$open open"
  exit 1
fi
echo "GO $kind"
