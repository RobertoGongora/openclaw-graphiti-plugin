#!/bin/sh
# Synthetic permission canary for an rsync-fed Claude session inbox. Runs only
# inside the disposable container of run.sh, on synthetic files, with no watcher.
#
# Roles: the uploader owns the inbox and rsyncs sessions into it; the reader (the
# worker's UID) may list and read, never write; any other UID gets nothing.
# Sources keep Claude's modes: directories 0700, files 0600.
set -u
READER=${READER_UID:-10001}
UPLOADER=${UPLOADER_UID:-10002}
OTHER=${OTHER_UID:-10003}
# A UID in the uploader's group: the owning group must not be a way in either.
GROUPMATE=${GROUPMATE_UID:-10004}
ROOT=/work
# The tested transfer: --chmod sets the ACL mask the reader's entry needs. Plain
# -a would copy 0600 and leave the mask empty; negative control N1 shows it.
RSYNC=${CANARY_RSYNC:-rsync -a --chmod=D750,F640}
# CANARY_RSYNC and CANARY_ACL exist to show that a weaker setup fails the canary.
ACL=${CANARY_ACL:-u:$READER:rx,d:u::rwx,d:u:$READER:rx,d:g::-,d:m::rx,d:o::-}

[ "$(id -u)" = 0 ] || { echo "run inside the disposable container (run.sh)" >&2; exit 2; }
[ -d "$ROOT" ] && [ -z "$(ls -A "$ROOT")" ] || { echo "$ROOT must be a new empty mount" >&2; exit 2; }

checks=0
failures=0
as() { u=$1; shift; setpriv --reuid="$u" --regid="$u" --clear-groups env HOME=/tmp sh -c "$*"; }
groupmate() { setpriv --reuid="$GROUPMATE" --regid="$GROUPMATE" --groups="$UPLOADER" env HOME=/tmp sh -c "$*"; }
ok() {
  name=$1; shift; checks=$((checks + 1))
  if "$@" >/dev/null 2>&1; then echo "PASS $name"; else echo "FAIL $name"; failures=$((failures + 1)); fi
}
denied() {
  name=$1; shift; checks=$((checks + 1))
  if "$@" >/dev/null 2>&1; then echo "FAIL $name (allowed)"; failures=$((failures + 1)); else echo "PASS $name (denied)"; fi
}
# The reader hashes the copy; root hashes the source, which the reader cannot open.
same() { [ "$(as "$READER" "sha256sum < '$1'")" = "$(sha256sum < "$2")" ]; }

# Fixture: what already exists on a host. Root creates the uploader's home and an
# uploader-owned inbox, and after this line never changes an owner or a mode.
install -d -m 0755 "$ROOT/home" "$ROOT/feeds"
install -d -o "$UPLOADER" -g "$UPLOADER" -m 0700 "$ROOT/home/uploader" "$ROOT/feeds/claude" \
  "$ROOT/feeds/plain-a" "$ROOT/feeds/no-acl" "$ROOT/feeds/legacy"
SRC=$ROOT/home/uploader/projects
INBOX=$ROOT/feeds/claude
S1=-home-dev-faro/0f6c1d2e-canary-session-one.jsonl
SUB=-home-dev-faro/0f6c1d2e-canary-session-one/subagents/agent-canary.jsonl
as "$UPLOADER" "umask 077; mkdir -p '$SRC/-home-dev-faro/0f6c1d2e-canary-session-one/subagents' \
  && printf '%s\n' '{\"type\":\"user\",\"canary\":1}' > '$SRC/$S1' \
  && printf '%s\n' '{\"type\":\"user\",\"canary\":2}' > '$SRC/$SUB'"
ok "sources are 0700 directories and 0600 files" \
  sh -c "[ -z \"\$(find '$SRC' -type d ! -perm 0700 -o -type f ! -perm 0600)\" ]"
for path in "$INBOX" "$ROOT/feeds"; do
  eval "before_$(basename "$path" | tr -c 'a-z\n' _)=\"$(stat -c %u:%g:%i "$path")\""
done

echo "== grant: the uploader sets an ACL on its own inbox only"
# Only a new, empty inbox: an ACL set later does not reach what is already there (N3, N4).
ok "the inbox is empty when its ACL is set" sh -c "[ -z \"\$(ls -A '$INBOX')\" ]"
ok "uploader sets the inbox ACL without root" as "$UPLOADER" "setfacl -m '$ACL' '$INBOX'"
echo "== create: first rsync"
ok "uploader rsyncs 0600 sources" as "$UPLOADER" "$RSYNC '$SRC/' '$INBOX/'"
ok "reader lists the inbox" as "$READER" "ls -R '$INBOX'"
ok "reader reads a session" same "$INBOX/$S1" "$SRC/$S1"
ok "reader reads a new nested subagent file" same "$INBOX/$SUB" "$SRC/$SUB"
denied "reader creates a file" as "$READER" "touch '$INBOX/reader-was-here'"
denied "reader appends to a session" as "$READER" "printf x >> '$INBOX/$S1'"
denied "reader removes a session" as "$READER" "rm -f '$INBOX/$S1'"
denied "reader renames a session" as "$READER" "mv '$INBOX/$S1' '$INBOX/$S1.moved'"
denied "reader makes a directory" as "$READER" "mkdir '$INBOX/reader-dir'"
denied "reader changes a mode" as "$READER" "chmod 0666 '$INBOX/$S1'"
denied "reader widens the ACL" as "$READER" "setfacl -m u:$OTHER:r '$INBOX/$S1'"
denied "unrelated UID lists the inbox" as "$OTHER" "ls '$INBOX'"
denied "unrelated UID reads a known path" as "$OTHER" "cat '$INBOX/$S1'"
denied "uploader's group member reads a session" groupmate "cat '$INBOX/$S1'"
denied "uploader's group member lists the inbox" groupmate "ls '$INBOX'"

echo "== update: the producer appends, rsync again"
as "$UPLOADER" "printf '%s\n' '{\"type\":\"assistant\",\"canary\":3}' >> '$SRC/$S1'"
ok "uploader rsyncs an appended session" as "$UPLOADER" "$RSYNC '$SRC/' '$INBOX/'"
ok "reader reads the appended session" same "$INBOX/$S1" "$SRC/$S1"

echo "== swap and rename: new inodes at the source, rsync again"
as "$UPLOADER" "umask 077; cp '$SRC/$SUB' '$SRC/$SUB.tmp' && printf '%s\n' '{\"canary\":4}' >> '$SRC/$SUB.tmp' \
  && mv '$SRC/$SUB.tmp' '$SRC/$SUB'"
S2=-home-dev-faro/7a9e4b10-canary-session-two.jsonl
as "$UPLOADER" "mv '$SRC/$S1' '$SRC/$S2'"
S3=-home-dev-atlas/3c2d5e6f-canary-session-three.jsonl
as "$UPLOADER" "umask 077; mkdir -m 0700 '$SRC/-home-dev-atlas' && printf '%s\n' '{\"canary\":5}' > '$SRC/$S3'"
ok "uploader rsyncs swapped, renamed and new files" as "$UPLOADER" "$RSYNC '$SRC/' '$INBOX/'"
ok "reader reads the swapped file" same "$INBOX/$SUB" "$SRC/$SUB"
ok "reader reads the renamed session" same "$INBOX/$S2" "$SRC/$S2"
ok "reader reads a session in a new 0700 project directory" same "$INBOX/$S3" "$SRC/$S3"
ok "reader still reads the old name, which rsync without --delete keeps" as "$READER" "cat '$INBOX/$S1'"
denied "reader writes into the new directory" as "$READER" "touch '$INBOX/-home-dev-atlas/x'"
denied "unrelated UID reads the new session" as "$OTHER" "cat '$INBOX/$S3'"

echo "== the producer keeps full control"
ok "uploader creates and removes in its inbox" as "$UPLOADER" "touch '$INBOX/.probe' && rm '$INBOX/.probe'"
ok "uploader rsyncs again after the reader read" as "$UPLOADER" "$RSYNC '$SRC/' '$INBOX/'"

echo "== scope: no ownership change, no ACL outside the inbox"
ok "every inbox entry still belongs to the uploader" \
  sh -c "[ -z \"\$(find '$INBOX' ! -uid $UPLOADER -o ! -gid $UPLOADER)\" ]"
ok "inbox and its parent keep owner, group and inode" sh -c \
  "[ '$(stat -c %u:%g:%i "$INBOX")' = \"$before_claude\" ] && [ '$(stat -c %u:%g:%i "$ROOT/feeds")' = \"$before_feeds\" ]"
ok "extended ACLs exist only inside the inbox" sh -c \
  "[ -z \"\$(getfacl -R -s -p '$ROOT' 2>/dev/null | sed -n 's/^# file: //p' | grep -v '^$INBOX\\(/\\|\$\\)')\" ]"
ok "the owning group and others have no effective access" sh -c \
  "[ -z \"\$(find '$INBOX' -exec getfacl -p {} + 2>/dev/null | grep -E '^(group|other)::' | grep -v -- '---\$')\" ]"
ok "the reader is never granted write" sh -c \
  "! find '$INBOX' -exec getfacl -p {} + 2>/dev/null | grep -E '^(default:)?user:$READER:.w'"

echo "== negative controls: the checks above can fail"
as "$UPLOADER" "setfacl -m '$ACL' '$ROOT/feeds/plain-a' && rsync -a '$SRC/' '$ROOT/feeds/plain-a/'"
denied "N1 plain rsync -a of 0600 files leaves the reader out" as "$READER" "cat '$ROOT/feeds/plain-a/$S2'"
as "$UPLOADER" "$RSYNC '$SRC/' '$ROOT/feeds/no-acl/'"
denied "N2 without the inbox ACL the reader is out" as "$READER" "cat '$ROOT/feeds/no-acl/$S2'"
# A prepopulated inbox: sessions arrived before the ACL, then the same pattern is applied.
LEGACY=$ROOT/feeds/legacy
as "$UPLOADER" "rsync -a '$SRC/' '$LEGACY/' && setfacl -m '$ACL' '$LEGACY'"
S4=-home-dev-faro/9b8c7d6e-canary-session-four.jsonl
S5=-home-dev-orion/5e4f3a2b-canary-session-five.jsonl
as "$UPLOADER" "umask 077; printf '%s\n' '{\"canary\":6}' > '$SRC/$S4' && mkdir -m 0700 '$SRC/-home-dev-orion' \
  && printf '%s\n' '{\"canary\":7}' > '$SRC/$S5' && $RSYNC '$SRC/' '$LEGACY/'"
denied "N3 a new session in a project directory from before the ACL leaves the reader out" \
  as "$READER" "cat '$LEGACY/$S4'"
ok "N3 while a project directory created after the ACL is readable" as "$READER" "cat '$LEGACY/$S5'"
ok "N4 rsync --chmod gives the owning group read on a legacy inode (why only a new inbox)" \
  sh -c "getfacl -cp '$LEGACY/$S2' | grep -qx 'group::r--'"

if [ "$failures" -eq 0 ]; then
  echo "CANARY PASS $checks checks reader=$READER uploader=$UPLOADER other=$OTHER"
else
  echo "CANARY FAIL $failures of $checks checks"
  exit 1
fi
