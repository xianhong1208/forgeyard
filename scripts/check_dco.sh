#!/usr/bin/env bash
# check_dco.sh <base> <head>: fail when a non-merge commit in base..head, not authored by a bot,
# has no "Signed-off-by:" line naming its author email (Developer Certificate of Origin).
set -euo pipefail
base=$1 head=$2
bad=0
while read -r sha; do
  name=$(git show -s --format=%an "$sha")
  email=$(git show -s --format=%ae "$sha")
  case $name in *'[bot]') continue ;; esac
  if git show -s --format=%B "$sha" | grep -qiF "Signed-off-by:" &&
     git show -s --format=%B "$sha" | grep -i "^Signed-off-by:" | grep -qiF "<$email>"; then
    continue
  fi
  echo "::error::$(git show -s --format='%h %s' "$sha"): no 'Signed-off-by: ... <$email>' line (git commit -s)"
  bad=1
done < <(git rev-list --no-merges "$base..$head")
if [ "$bad" -ne 0 ]; then
  echo "Sign off each commit (git commit -s, or git rebase --signoff <base>), see CONTRIBUTING.md#licence."
  exit 1
fi
echo "Every commit is signed off."
