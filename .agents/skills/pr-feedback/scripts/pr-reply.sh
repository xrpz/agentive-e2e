#!/bin/sh
# agentive-unit: rl.skill.pr-feedback
# design: spec.work_loop.pr_feedback.reply, spec.review_pipeline.severity_blocking.reply_line, spec.skill_scripts, spec.outer_loop.cloud
# Usage: sh .agents/skills/pr-feedback/scripts/pr-reply.sh <pr number> <comment id> <body file>
# Posts the text of <body file> as one reply in the review thread of <comment id> through REST only
# (gh api POST repos/<owner>/<repo>/pulls/<n>/comments/<id>/replies, body read from the file with -F body=@file), so the
# agent never builds a shell-quoted body. Write the file with the Write tool (for example .agents/state/pr-reply-<id>.md).
# A line that starts with `agentive-reply:` must be `agentive-reply: <finding id> <fixed|wrong|intentional|not_important>
# [<one sentence>]`, with the sentence required unless the reason is fixed (the format RV7 and pr-state.sh read).
# One call posts one reply and is never retried here (a second POST would double the reply).
set -eu
fail() { echo "AGENTIVE_FAIL tool=pr-reply reason=$1; NEXT=$2"; exit 2; }
pr=${1:-}; cid=${2:-}; file=${3:-}
case "$pr" in ''|*[!0-9]*) fail bad_pr "pass the PR number: sh .agents/skills/pr-feedback/scripts/pr-reply.sh <pr number> <comment id> <body file>" ;; esac
case "$cid" in ''|*[!0-9]*) fail bad_comment_id "pass the numeric id of the thread's first comment (thread= of a FINDING line, the id after THREAD in pr-state.sh output)" ;; esac
[ -n "$file" ] && [ -f "$file" ] && [ -s "$file" ] || fail body_file_missing "write the reply text to a file with the Write tool (for example .agents/state/pr-reply-$cid.md), then rerun with that path"
command -v gh >/dev/null 2>&1 || fail gh_missing "install the GitHub CLI (gh) and authenticate"
# every agentive-reply line must have the exact format; the sentence is required unless the reason is fixed
bad=$(grep -E '^agentive-reply:' "$file" | grep -Ev '^agentive-reply: [^[:space:]]+ (fixed|wrong|intentional|not_important)( .+)?$' | head -n 1 || true)
if [ -n "$bad" ]; then
  fail bad_reply_line "fix the line to 'agentive-reply: <finding id> <fixed|wrong|intentional|not_important> <one sentence>' in $file, then rerun"
fi
nosentence=$(grep -E '^agentive-reply: [^[:space:]]+ (wrong|intentional|not_important)[[:space:]]*$' "$file" | head -n 1 || true)
if [ -n "$nosentence" ]; then
  fail reply_sentence_missing "add one sentence after the reason (required unless it is fixed) in $file, then rerun"
fi
# owner/repo from the origin URL (also a proxied clone URL); gh's {owner}/{repo} placeholders otherwise
repo=$(git remote get-url origin 2>/dev/null | sed -E 's#(\.git)?/*$##; s#^.*[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$#\1#' || true)
case "$repo" in */*) ;; *) repo='{owner}/{repo}' ;; esac
if ! url=$(gh api -X POST "repos/$repo/pulls/$pr/comments/$cid/replies" -F "body=@$file" --jq '.html_url' 2>/dev/null); then
  fail gh_api_failed "check gh auth status, that comment $cid belongs to PR $pr, and that the reply is not already posted (pr-state.sh lists the thread), then rerun once"
fi
echo "REPLY_POSTED pr=$pr comment=$cid url=${url:-unknown}; NEXT=post the next reply, resolve the fixed threads, then refresh ## Verification once with /pr as the last step"
