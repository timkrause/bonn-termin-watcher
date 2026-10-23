#!/usr/bin/env bash
# Runs inside one GitHub Actions job: a check every 5 minutes (termin_watch.py adds
# 0-60 s of jitter) for ~5.5 hours, committing data/ and docs/ back to the repo so
# the next job (queued by the cron) picks up where this one stopped.
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

end=$((SECONDS + 330 * 60))
i=0
while [ $SECONDS -lt $end ]; do
  t0=$SECONDS
  before=$(cat data/state.json 2>/dev/null)
  python3 -I termin_watch.py --jitter 60
  code=$?
  # Bookings are saved right away; routine checks every ~10 min (GitHub Pages
  # allows about 10 builds an hour).
  if [ "$(cat data/state.json 2>/dev/null)" != "$before" ]; then
    save "state change"
  elif [ $((i % 2)) -eq 0 ]; then
    save "check"
  fi
  if [ $code -eq 10 ]; then
    save "search over"
    gh workflow disable watch.yml
    exit 0
  fi
  i=$((i + 1))
  left=$((300 - (SECONDS - t0)))
  [ $left -gt 0 ] && sleep $left
done
save "end of job"
