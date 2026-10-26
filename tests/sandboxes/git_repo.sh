#!/usr/bin/env bash
# Build a throwaway git sandbox for one git-push-recovery scenario. Local only: no network.
# usage: sandbox.sh <scenario> <dir>
#   happy     local commit, push just works
#   behind    remote has a new commit on another file -> rejected (fetch first) -> rebase -> push
#   conflict  remote changed the same line -> rebase conflicts -> abort -> needs_human
#   https     origin is https://github.com/... and the server refuses the password -> switch to SSH
#   flaky     first push fails with "Connection refused" (no regex matches), second works
#   flaky2    first push dies with GitHub's HTTP/2 "RPC failed" disconnect (no regex matches), second works
#   hook      server hook rejects the push for policy reasons (none of the recovery paths fit)
set -euo pipefail
scenario=$1 dir=$2
rm -rf "$dir" && mkdir -p "$dir" && cd "$dir"
dir=$(pwd)
git init -q --bare -b main origin.git
git clone -q origin.git work 2>/dev/null
cd work
git config user.email t@example.com && git config user.name tester
echo "line one" > app.txt && git add app.txt && git commit -qm init && git push -q origin main

other() {  # a second developer pushes $1 into $2
  git clone -q "$dir/origin.git" "$dir/other" && cd "$dir/other"
  git config user.email o@example.com && git config user.name other
  echo "$1" > "$2" && git add "$2" && git commit -qm "other: $2" && git push -q origin main && cd "$dir/work"
}

case $scenario in
  happy) ;;
  behind) other "readme" README.md ;;
  conflict) other "line one (theirs)" app.txt; echo "line one (ours)" > app.txt; git commit -qam "ours: app.txt" ;;
  https)
    # Fake GitHub: https URLs answer like a server that refuses passwords; the SSH form maps to the local bare repo.
    printf '#!/bin/sh\necho "remote: Support for password authentication was removed on August 13, 2021." >&2\necho "fatal: Authentication failed for '"'"'https://github.com/acme/app.git/'"'"'" >&2\nexit 128\n' > "$dir/https-transport.sh"
    chmod +x "$dir/https-transport.sh"
    git remote set-url origin https://github.com/acme/app.git
    git config url."ext::$dir/https-transport.sh".insteadOf https://github.com/acme/app.git
    git config url."$dir/origin.git".insteadOf git@github.com:acme/app.git
    git config protocol.ext.allow always ;;
  flaky)
    cat > "$dir/flaky-transport.sh" <<EOF
#!/usr/bin/env bash
# first call: refuse the connection; later calls: behave like the real remote
if [ ! -f "$dir/.tried" ]; then touch "$dir/.tried"
  echo "ssh: connect to host git.acme.io port 22: Connection refused" >&2
  echo "fatal: Could not read from remote repository." >&2; exit 128; fi
exec git-receive-pack "$dir/origin.git"
EOF
    chmod +x "$dir/flaky-transport.sh"
    git remote set-url origin "ext::$dir/flaky-transport.sh"
    git config protocol.ext.allow always ;;
  flaky2)
    cat > "$dir/flaky-transport.sh" <<EOF
#!/usr/bin/env bash
# first call: GitHub's HTTP/2 mid-push disconnect (no regex covers it); later calls: the real remote
if [ ! -f "$dir/.tried" ]; then touch "$dir/.tried"
  echo "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly: CANCEL (err 8)" >&2
  echo "send-pack: unexpected disconnect while reading sideband packet" >&2
  echo "fatal: the remote end hung up unexpectedly" >&2; exit 128; fi
exec git-receive-pack "$dir/origin.git"
EOF
    chmod +x "$dir/flaky-transport.sh"
    git remote set-url origin "ext::$dir/flaky-transport.sh"
    git config protocol.ext.allow always ;;
  hook)
    printf '#!/bin/sh\necho "error: commit message must reference a ticket (e.g. OPS-123)" >&2\nexit 1\n' > "$dir/origin.git/hooks/pre-receive"
    chmod +x "$dir/origin.git/hooks/pre-receive" ;;
  *) echo "unknown scenario $scenario" >&2; exit 1 ;;
esac
[ "$scenario" = conflict ] || { echo "local change $(date +%s%N)" > local.txt && git add local.txt && git commit -qm "local work"; }
echo "$dir/work"
