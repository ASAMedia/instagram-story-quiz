#!/usr/bin/env bash
# One story run: first catch up a due or overdue reveal, then post today's
# question if it is time. Each step is a no-op when there is nothing to do, so
# this is safe to run as often as you like.
set -euo pipefail

step() {
  python story_quiz.py make "$1"
  git add docs state locations.json
  git commit -m "Prepare $1 $(date -u +%F)" || echo "nothing to commit"
  git pull --rebase
  git push
  python story_quiz.py publish "$1"
  git add state
  git commit -m "Published $1 $(date -u +%F)" || echo "nothing to commit"
  git pull --rebase
  git push
}

step reveal
step question
