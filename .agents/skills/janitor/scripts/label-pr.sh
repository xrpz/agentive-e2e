#!/bin/sh
# agentive-unit: rl.skill.janitor
# design: spec.maintenance.janitor.merge, spec.review_pipeline.convergence.label_trigger, spec.skill_scripts, spec.outer_loop.cloud
# Usage: sh .agents/skills/janitor/scripts/label-pr.sh <kind> <pr number>
# E2E-40: /pr publish --label agentive-janitor:<kind> does this as part of opening the PR (same POST, GET, one repeat); this script is the
# standalone repair that the AGENTIVE_FAIL tool=pr-label NEXT names.
# Adds the label agentive-janitor:<kind> to the PR and checks that the PR carries it: POST repos/<owner>/<repo>/issues/<n>/labels,
# then GET the labels; when the label is missing the POST is repeated once. Exit 0 with `LABELED agentive-janitor:<kind> pr=<n>`;
# exit 2 with an AGENTIVE_FAIL line otherwise. gh api only (works in Routines runs, where the pull-request subcommands are blocked).
# The token must not be the workflow's GITHUB_TOKEN: an event made with it starts no workflow run, so the gate would not re-run
# on the labeled event. A Routines run uses the user's own token and `claude -p` in CI uses a GitHub App token.
set -eu
fail() { echo "AGENTIVE_FAIL tool=janitor-label reason=$1; NEXT=$2"; exit 2; }
kind=${1:-}; pr=${2:-}
if [ -z "$kind" ] || ! printf '%s\n' "$kind" | grep -Eq '^[a-z0-9][a-z0-9-]*$'; then
  fail bad_kind "pass a kind id from .agents/manifest.yaml janitor.kinds"
fi
case "$pr" in ''|*[!0-9]*) fail bad_pr "pass the PR number /pr printed: sh .agents/skills/janitor/scripts/label-pr.sh $kind <pr number>" ;; esac
command -v gh >/dev/null 2>&1 || fail gh_missing "install the GitHub CLI (gh) and authenticate"
label="agentive-janitor:$kind"
# owner/repo from the origin URL (also a proxied clone URL); gh's {owner}/{repo} placeholders otherwise
repo=$(git remote get-url origin 2>/dev/null | sed -E 's#(\.git)?/*$##; s#^.*[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$#\1#' || true)
case "$repo" in */*) ;; *) repo='{owner}/{repo}' ;; esac
has_label() {
  names=$(gh api -X GET "repos/$repo/issues/$pr/labels" --jq '.[].name' 2>/dev/null) || return 1
  printf '%s\n' "$names" | grep -Fxq "$label"
}
add_label() { gh api -X POST "repos/$repo/issues/$pr/labels" -f "labels[]=$label" >/dev/null 2>&1; }
add_label || true
if has_label; then echo "LABELED $label pr=$pr"; exit 0; fi
add_label || true
if has_label; then echo "LABELED $label pr=$pr"; exit 0; fi
fail label_missing "check gh auth status (the token needs write access to issues; not GITHUB_TOKEN) and that PR $pr exists, then rerun; until $label is on the PR it is not tier 2: say so in the report"
