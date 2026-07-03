#!/usr/bin/env bash
set -euo pipefail

# Push the current commit to the GitHub branch connected to Railway production.
# Railway auto-deploys this branch after a successful push.
REPO_URL="${RAILWAY_GITHUB_REPO:-https://github.com/matvey21354765/-.git}"
BRANCH="${RAILWAY_DEPLOY_BRANCH:-claude/amazing-allen-p3ksdq}"

echo "Deploying $(git rev-parse --short=12 HEAD) to $BRANCH ..."

if [[ -n "${GITHUB_TOKEN:-}" && "$REPO_URL" == https://github.com/* ]]; then
  git -c "http.https://github.com/.extraheader=AUTHORIZATION: bearer ${GITHUB_TOKEN}" \
    push "$REPO_URL" "HEAD:$BRANCH"
else
  git push "$REPO_URL" "HEAD:$BRANCH"
fi

echo "Pushed. Railway should start an automatic deployment for $BRANCH."
