#!/usr/bin/env bash
# Bounded state persistence. Run after the GM-002 validation/snapshot gate.
set -euo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0

budget="${PERSIST_BUDGET_SECONDS:-170}"
if [[ ! "$budget" =~ ^[1-9][0-9]{0,2}$ ]] || (( budget > 170 )); then
  echo "PERSIST_BUDGET_SECONDS must be an integer from 1 to 170"
  exit 1
fi
started=$SECONDS
SAVE="$RUNNER_TEMP/state"
mkdir -p "$SAVE/ours" "$SAVE/base"
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
BASE_SHA=$(git rev-parse HEAD)
cp merge_state.py "$SAVE/merge_state.py"
cp .github/scripts/push_diagnostics.py "$SAVE/push_diagnostics.py"
STATE_FILES=(monitor_state.json bookmarks.json bot_state.json)
for file in "${STATE_FILES[@]}"; do
  if [ -f "$file" ]; then cp "$file" "$SAVE/ours/$file"; fi
  git show "$BASE_SHA:$file" > "$SAVE/base/$file" 2>/dev/null || rm -f "$SAVE/base/$file"
done

PENDING_SHA=""
PENDING_PARENT=""
REMOTE_SHA="unknown"

remaining() {
  echo "$((budget - (SECONDS - started)))"
}

network() {
  local operation=$1 attempt=$2
  shift 2
  local left cap log
  left=$(remaining)
  if (( left <= 0 )); then
    echo "Persistence budget exhausted before $operation"
    NETWORK_STATUS=124
    NETWORK_CATEGORY=transient_network
    return
  fi
  cap=20
  if (( left < cap )); then cap=$left; fi
  log="$SAVE/$operation-$attempt.log"
  if timeout --signal=TERM --kill-after=2s "${cap}s" git "$@" > "$log" 2>&1; then
    NETWORK_STATUS=0
  else
    NETWORK_STATUS=$?
  fi
  python3 "$SAVE/push_diagnostics.py" --operation "$operation" "$log" \
    "$NETWORK_STATUS" "$attempt" "$(git rev-parse HEAD)" "$REMOTE_SHA"
  NETWORK_CATEGORY=$(python3 "$SAVE/push_diagnostics.py" --category-only \
    --operation "$operation" "$log" "$NETWORK_STATUS" "$attempt" \
    "$(git rev-parse HEAD)" "$REMOTE_SHA")
  rm -f -- "$log"
}

confirmed_pending() {
  if [ -z "$PENDING_SHA" ]; then return; fi
  if git merge-base --is-ancestor "$PENDING_SHA" origin/master; then
    echo "Push confirmed on remote: commit=$PENDING_SHA"
    exit 0
  else
    local status=$?
    if (( status != 1 )); then
      echo "Cannot verify pending commit ancestry; stopping safely"
      exit 1
    fi
  fi
}

retry_or_stop() {
  local attempt=$1 category=$2 delay left
  case "$category" in
    transient_server|transient_network|non_fast_forward) ;;
    *) echo "Persistence stopped: category=$category"; exit 1 ;;
  esac
  if (( attempt >= 5 )); then
    echo "Failed to persist state after 5 attempts"
    exit 1
  fi
  left=$(remaining)
  if (( left <= 0 )); then
    echo "Persistence budget exhausted"
    exit 1
  fi
  if [ "$category" = non_fast_forward ]; then
    echo "Remote advanced; fetching and re-merging state on the next attempt"
    return
  fi
  delay=$((5 * (2 ** (attempt - 1)) + RANDOM % 4))
  if (( delay >= left )); then
    echo "Insufficient persistence budget for another retry"
    exit 1
  fi
  echo "Retry delay: seconds=$delay category=$category attempt=$attempt"
  timeout --signal=TERM --kill-after=2s "${left}s" sleep "$delay"
}

for attempt in 1 2 3 4 5; do
  network fetch "$attempt" fetch origin master
  if (( NETWORK_STATUS != 0 )); then
    retry_or_stop "$attempt" "$NETWORK_CATEGORY"
    continue
  fi
  REMOTE_SHA=$(git rev-parse origin/master)
  # A successful fetch is required before discarding or resending a pending commit.
  confirmed_pending
  if [ -z "$PENDING_SHA" ] || [ "$REMOTE_SHA" != "$PENDING_PARENT" ]; then
    git reset --hard "$REMOTE_SHA"
    for file in "${STATE_FILES[@]}"; do
      [ -f "$SAVE/ours/$file" ] || continue
      git show "$REMOTE_SHA:$file" > "$SAVE/theirs-$file" 2>/dev/null \
        || rm -f "$SAVE/theirs-$file"
      cp "$SAVE/ours/$file" "$file"
      python3 "$SAVE/merge_state.py" "$file" "$SAVE/base/$file" "$SAVE/theirs-$file"
      git add -- "$file"
    done
    if git diff --cached --quiet; then
      echo "No state changes to commit"
      exit 0
    fi
    git commit -m "Update state [$(date -u +'%Y-%m-%d %H:%M:%S UTC')]"
    PENDING_SHA=$(git rev-parse HEAD)
    PENDING_PARENT=$REMOTE_SHA
  fi
  network push "$attempt" push origin HEAD:master
  if (( NETWORK_STATUS == 0 )); then
    echo "Push succeeded on attempt $attempt"
    exit 0
  fi
  push_category=$NETWORK_CATEGORY
  case "$push_category" in
    transient_server|transient_network)
      # Even a failed client response may follow a server-accepted push.
      network confirm "$attempt" fetch origin master
      if (( NETWORK_STATUS == 0 )); then
        REMOTE_SHA=$(git rev-parse origin/master)
        confirmed_pending
      else
        echo "Remote confirmation unavailable; a fresh successful fetch is required before another push"
        case "$NETWORK_CATEGORY" in
          permission_or_rule|unknown) retry_or_stop "$attempt" "$NETWORK_CATEGORY" ;;
        esac
      fi
      ;;
  esac
  retry_or_stop "$attempt" "$push_category"
done
echo "State persistence failed"
exit 1
