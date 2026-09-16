#!/bin/bash
# Applies a verified self-edit, and rolls it back if Jarvis doesn't come back healthy.
#
#   self_edit_guard.sh <checkpoint> <brain|app> <old-brain-pid> <port> <repo-root> <jarvis-dir>
#
# The brain starts this from the CHECKPOINT's copy of the script, in its own
# session, just before being restarted - so it survives the restart it performs,
# and an edit cannot change the code that decides whether to roll it back.
set -u
CP="$1"; MODE="$2"; OLDPID="$3"; PORT="$4"; ROOT="$5"; JDIR="$6"
GIT=(git -C "$ROOT" -c user.name=Jarvis -c user.email=jarvis@localhost)

log() { echo "$(date '+%H:%M:%S') guard: $*"; }

apply_restart() {
    if [ "$MODE" = "app" ]; then
        "$ROOT/app/install.sh"
        return
    fi
    kill -TERM "$OLDPID" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$OLDPID" 2>/dev/null || return; sleep 0.5; done
    kill -KILL "$OLDPID" 2>/dev/null   # the app respawns a fresh brain either way
}

# Healthy means a DIFFERENT process than the one we replaced, ready on two
# consecutive checks - not the old brain answering while it shuts down.
new_brain_healthy() {
    local streak=0
    for _ in $(seq 1 "$1"); do
        sleep 3
        pid=$(curl -s --max-time 2 "http://127.0.0.1:$PORT/api/status" \
            | /usr/bin/python3 -c 'import json,sys
try:
    d = json.load(sys.stdin); print(d.get("pid", "") if d.get("ready") else "")
except Exception:
    print("")' 2>/dev/null)
        if [ -n "$pid" ] && [ "$pid" != "$OLDPID" ]; then
            streak=$((streak + 1)); [ "$streak" -ge 2 ] && return 0
        else
            streak=0
        fi
    done
    return 1
}

log "applying (mode=$MODE, checkpoint=${CP:0:8}, old pid=$OLDPID)"
sleep 1
apply_restart
if new_brain_healthy 60; then
    log "healthy - change kept"
    exit 0
fi

log "no healthy Jarvis within 3 minutes - restoring the edited files to ${CP:0:8}"
while IFS= read -r path; do
    [ -z "$path" ] && continue
    if "${GIT[@]}" cat-file -e "$CP:$path" 2>/dev/null; then
        "${GIT[@]}" checkout "$CP" -- "$path"
    else
        rm -f "$ROOT/$path"
    fi
done < "$JDIR/self_edit_paths.txt"
"${GIT[@]}" add -A -- $(cat "$JDIR/self_edit_paths.txt") 2>/dev/null
"${GIT[@]}" commit -q -m "rollback: self-edit did not start" 2>/dev/null
rm -f "$JDIR/self_edit.json"
echo '{"reason": "did not start"}' > "$JDIR/self_edit_rollback.json"

# Restart the whole app. A crash-looping brain makes the supervisor give up and
# show a modal alert, which blocks the app's main thread - including its SIGTERM
# handler - so a polite quit would never land. Force it, then relaunch with
# retries: LaunchServices can ignore an open right after a kill.
pkill -9 -f 'Jarvis.app/Contents/MacOS/Jarvis'
pkill -9 -f 'brain/.venv/bin/python -m jarvis'
sleep 2
for attempt in 1 2 3 4; do
    open "$HOME/Applications/Jarvis.app" 2>/dev/null
    sleep 4
    pgrep -f 'Jarvis.app/Contents/MacOS/Jarvis' >/dev/null && break
    log "relaunch attempt $attempt did not take"
done
OLDPID=0
new_brain_healthy 60 && log "rolled back and healthy" || log "rolled back but still not healthy - see brain.log"
