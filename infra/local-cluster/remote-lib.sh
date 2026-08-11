#!/usr/bin/env bash
# Shared remote-side helpers for the two worker deploy scripts. Sourced, never
# run on its own.
#
# It exists because a deploy WRITES to the worker box, and where it writes
# decides whether someone else can wedge it. #297: /tmp/encoder-src was found
# root-owned, created by something at 02:16 that was never identified. /tmp is
# sticky, so the deploy user could neither write into it nor remove it, and that
# box was permanently undeployable until someone with sudo intervened.
#
# A fixed path under /tmp can be created by ANYTHING — a container bind mount, a
# root shell, another user's deploy, a systemd unit — so the path is the bug,
# not whatever created it, and finding the culprit would not have helped. Under
# $HOME the collision cannot happen without something having gone far more
# wrong than a deploy.
#
# The reason to share this rather than fix each script separately: the bug WAS
# the same literal path written in several places. Two copies of the fix is the
# same shape of mistake, one refactor later.

# ssh options for every remote call in both scripts. Fail fast when a box goes
# away MID-DEPLOY rather than blocking on a dead TCP connection until the OS
# gives up (~2h). ConnectTimeout covers "never answered"; ServerAlive* covers
# "answered, then vanished", which is the one that actually bit — a sleeping box
# held a whole deploy for 22 minutes with zero output before anyone noticed, and
# it would have kept going. Both paths ship ~900MB over the wire, so they sit in
# that vulnerable state for a long time.
#
# Note this is only about a box that dies mid-deploy. UNREACHABLE-vs-FAILED is
# decided by the caller (`for_each_worker` in the Makefile probes with `ssh
# true` first), so a box that never answers is skipped before either script
# runs — #239. Nothing here may change that.
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8
          -o ServerAliveInterval=10 -o ServerAliveCountMax=3)

# remote_ensure_dir <ssh_target> <dir> — mkdir -p, then prove it is writable by
# the deploying user, failing with ONE line naming the path and its owner.
#
# rsync's own version of this failure is a "Permission denied (13)" per FILE,
# which reads like a full disk or a broken mount rather than an ownership
# problem — a wall of output that says nothing about the directory that caused
# it. The check costs one ssh round trip and replaces all of that.
remote_ensure_dir() {
    ssh "${SSH_OPTS[@]}" "$1" bash -s -- "$2" <<'RSH'
set -u
d=$1
mkdir -p "$d" 2>/dev/null
[ -d "$d" ] && [ -w "$d" ] && exit 0
# Name the first component that EXISTS. When mkdir failed, the leaf is not there
# and its owner is not something anyone can act on — the culprit is the deepest
# existing ancestor, which is exactly the root-owned directory in #297.
p=$d
while [ ! -e "$p" ] && [ "$p" != "/" ]; do p=$(dirname "$p"); done
# ls -ld, not stat: -c is GNU and -f is BSD, and this fleet runs both (ubuntu
# and an Apple-Silicon Mac mini), so stat would fail on one of them.
echo "deploy: cannot write $d as $(id -un) on $(hostname)" >&2
echo "deploy: blocked by $(ls -ld "$p" | awk '{print $1, $3, $4}') $p" >&2
echo "deploy: remove or chown that path on the box, then re-run" >&2
exit 1
RSH
}

# remote_stage <ssh_target> — echo a per-user staging directory on the box,
# created and verified writable.
#
# $HOME is expanded on the FAR SIDE and resolved here into a plain absolute
# path, so nothing downstream has to survive a second expansion — an unexpanded
# '$HOME' would otherwise have to make it intact through rsync's remote path,
# scp, a heredoc and a docker argument, each with its own quoting rules.
remote_stage() {
    local target=$1 home stage
    home=$(ssh "${SSH_OPTS[@]}" "$target" 'printf %s "$HOME"') || return 1
    if [ -z "$home" ]; then
        echo "deploy: could not resolve \$HOME on $target" >&2
        return 1
    fi
    stage="$home/.cache/encoder-src"
    remote_ensure_dir "$target" "$stage" || return 1
    printf %s "$stage"
}
