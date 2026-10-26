#!/usr/bin/env bash
# Runs inside one GitHub Actions job: a check every 5 minutes (termin_watch.py adds
# 0-60 s of jitter) and every minute during config.json's "fast_checks" window, when
# the city frees up slots, for ~5.5 hours, committing data/ and docs/ back to the
# repo so the next job (queued by the cron) picks up where this one stopped.
set -u
git config user.name "termin-watch-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# A queued job is checked out at the commit from when it was queued, often hours
# old. Start from the newest data (bookings!) and code instead.
git fetch -q origin main && git reset -q --hard origin/main

# Only one job runs at a time (concurrency group), so its data/ and docs/ are the
# newest there are: commit them on top of whatever main is now, never merge them.
save() {
  git fetch -q origin main || { echo "fetch failed"; return; }
  git reset -q --mixed origin/main   # index = newest main; working tree keeps our data
  git add data docs
  git diff --cached --quiet || git commit -qm "$1"
  git checkout -q -- .               # pick up code pushed meanwhile; our data is committed
  git push -q origin HEAD:main || echo "push failed"
}

# Fast window as seconds since midnight, [FAST_FROM, FAST_TO); 0 0 = none.
read -r FAST_FROM FAST_TO <<< "$(python3 -I -c 'import json
w = json.load(open("config.json")).get("fast_checks")
s = lambda t: int(t[:2]) * 3600 + int(t[3:]) * 60
print(*((s(w[0]), s(w[1]) + 60) if w else (0, 0)))')"
sod() { set -- $(date '+%H %M %S'); echo $((10#$1 * 3600 + 10#$2 * 60 + 10#$3)); }
fast() { local s=${1:-$(sod)}; [ "$s" -ge "$FAST_FROM" ] && [ "$s" -lt "$FAST_TO" ]; }
# From 10 minutes before the window to its end: no handing over to the next job then.
near_fast() { local s=$(sod); [ "$s" -ge $((FAST_FROM - 600)) ] && [ "$s" -lt "$FAST_TO" ]; }

# Seconds until the next check, given how long this one took and the time now:
# 1 min apart in the window, else 5 min, but never so late that the jitter (up
# to 60 s) carries the check past the window's start.
next_wait() {
  local elapsed=$1 now=$2
  if fast "$now"; then echo $((60 - elapsed)); return; fi
  local wait=$((300 - elapsed)) to_fast=$(( (FAST_FROM - now + 86400) % 86400 ))
  [ "$FAST_TO" -gt 0 ] && [ $to_fast -lt $((wait + 60)) ] && wait=$to_fast
  echo $wait
}

# 320 min plus up to ~27 min around the fast window stays under the 355 min job timeout.
end=$((SECONDS + 320 * 60))
last_save=-9999
while [ $SECONDS -lt $end ] || near_fast; do
  t0=$SECONDS
  before=$(cat data/state.json 2>/dev/null)
  if fast; then jitter=5; else jitter=60; fi
  python3 -I termin_watch.py --jitter $jitter
  code=$?
  # Bookings are saved right away, routine checks every ~10 min (GitHub Pages
  # allows about 10 builds an hour, so not every minute in the fast window).
  if [ "$(cat data/state.json 2>/dev/null)" != "$before" ]; then
    save "state change"; last_save=$SECONDS
  elif [ $((SECONDS - last_save)) -ge 540 ]; then
    save "check"; last_save=$SECONDS
  fi
  if [ $code -eq 10 ]; then
    save "search over"
    gh workflow disable watch.yml
    exit 0
  fi
  wait=$(next_wait $((SECONDS - t0)) "$(sod)")
  [ "$wait" -gt 0 ] && sleep "$wait"
done
save "end of job"
