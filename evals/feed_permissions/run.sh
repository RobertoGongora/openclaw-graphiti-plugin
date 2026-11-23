#!/bin/sh
# Build and run the feed permission canary in a disposable container: no network,
# a fresh anonymous volume (or tmpfs with FS=tmpfs), removed on exit. It never
# mounts a host path, so it cannot reach a real inbox or a running watcher.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
image=graph-memory-feed-canary:local
docker build -q -t "$image" "$here" >/dev/null
case "${FS:-volume}" in
  volume) storage="--mount type=volume,dst=/work" ;;
  tmpfs) storage="--tmpfs /work" ;;
  *) echo "FS must be volume or tmpfs" >&2; exit 2 ;;
esac
# shellcheck disable=SC2086
exec docker run --rm --network none $storage -e CANARY_RSYNC -e CANARY_ACL "$image"
