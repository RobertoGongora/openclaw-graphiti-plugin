#!/bin/bash
# Usage: measure.sh <container> <namespace> <concurrent> <rounds>
# Holds N concurrent docker exec stdio sessions open, sampling cgroup memory and
# process count; then churns rounds of short-lived clients and checks it returns flat.
C=$1; NS=$2; N=${3:-30}; R=${4:-5}
OUT=$(mktemp -d)
cg(){ docker exec $C cat /sys/fs/cgroup/memory.current; }
procs(){ docker exec $C sh -c 'ls /proc | grep -c "^[0-9]"'; }
init='{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}'
list='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
b=$(cg); bp=$(procs)
echo "baseline: cgroup_MiB=$((b/1048576)) procs=$bp"
for i in $(seq 1 $N); do ( (echo "$init"; echo "$list"; sleep 120) | docker exec -i $C graph-memory --namespace $NS serve >$OUT/out.$i ) & done
sleep 25
a=$(cg); ap=$(procs)
ok=$(grep -l '"tools"' $OUT/out.* 2>/dev/null | wc -l | tr -d ' ')
echo "$N held sessions: cgroup_MiB=$((a/1048576)) procs=$ap per_session_KiB=$(( (a-b)/N/1024 )) answered=$ok/$N"
pkill -f 'sleep 120' 2>/dev/null; sleep 8
echo "after release: cgroup_MiB=$(( $(cg)/1048576 )) procs=$(procs)"
rm -rf "$OUT"
for r in $(seq 1 $R); do
  for i in $(seq 1 $N); do (printf '%s\n%s\n' "$init" "$list" | docker exec -i $C graph-memory --namespace $NS serve >/dev/null) & done
  wait
  echo "churn round $r ($N clients): cgroup_MiB=$(( $(cg)/1048576 )) procs=$(procs)"
done
