#!/usr/bin/env bash
set -euo pipefail

# Push the current commit to the GitHub branch connected to Railway production.
# Railway auto-deploys this branch after a successful push.
REPO_URL="${RAILWAY_GITHUB_REPO:-https://github.com/matvey21354765/-.git}"
BRANCH="${RAILWAY_DEPLOY_BRANCH:-claude/amazing-allen-p3ksdq}"
REMOTE="${RAILWAY_DEPLOY_REMOTE:-origin}"

if [[ -n "${GITHUB_TOKEN:-}" && "$REPO_URL" == https://github.com/* ]]; then
  REPO_URL="https://${GITHUB_TOKEN}@${REPO_URL#https://}"
fi

if git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote set-url "$REMOTE" "$REPO_URL"
else
  git remote add "$REMOTE" "$REPO_URL"
fi

echo "Deploying $(git rev-parse --short=12 HEAD) to $REMOTE/$BRANCH ..."
git push "$REMOTE" "HEAD:$BRANCH"
echo "Pushed. Railway should start an automatic deployment for $BRANCH."
