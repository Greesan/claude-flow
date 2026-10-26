#!/usr/bin/env bash
# Rewrite a remote's URL from https://host/owner/repo(.git) to git@host:owner/repo(.git).
set -euo pipefail
remote=${1:-origin}
url=$(git config --get "remote.$remote.url")
case $url in
  https://*) ;;
  *) echo "remote $remote is not https ($url); nothing to change"; exit 0 ;;
esac
new=$(printf '%s' "$url" | sed -E 's#^https://([^/]+)/#git@\1:#')
git remote set-url "$remote" "$new"
echo "url=$new" >> "$FLOW_OUTPUT"
echo "$remote: $url -> $new"
