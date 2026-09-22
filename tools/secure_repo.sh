#!/usr/bin/env bash
# Repository hardening for agenticcad/agenticcad. Run after the repo is PUBLIC (branch protection,
# secret scanning and rulesets are not available on private org repos without a paid plan).
# Policy: anyone can open issues; code changes only via pull requests from forks; main is protected.
set -euo pipefail
R=agenticcad/agenticcad
echo "== repo settings"
gh api -X PATCH repos/$R -F allow_forking=true -F has_issues=true -F has_projects=false -F has_wiki=false \
  -F delete_branch_on_merge=true -F allow_merge_commit=false -F allow_squash_merge=true -F allow_rebase_merge=true >/dev/null
echo "== security: secret scanning + push protection, dependabot alerts/updates, private vulnerability reporting"
gh api -X PATCH repos/$R -f 'security_and_analysis[secret_scanning][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled' >/dev/null || echo "   (secret scanning: not available yet — is the repo public?)"
gh api -X PUT repos/$R/vulnerability-alerts >/dev/null && echo "   dependabot alerts on"
gh api -X PUT repos/$R/automated-security-fixes >/dev/null && echo "   dependabot security updates on"
gh api -X PUT repos/$R/private-vulnerability-reporting >/dev/null && echo "   private vulnerability reporting on"
echo "== actions: read-only GITHUB_TOKEN, github-owned actions only, approval for first-time fork contributors"
gh api -X PUT repos/$R/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false >/dev/null
gh api -X PUT repos/$R/actions/permissions/fork-pr-contributor-approval -f approval_policy=first_time_contributors >/dev/null || true
gh api -X PUT repos/$R/actions/permissions -F enabled=true -f allowed_actions=selected >/dev/null
gh api -X PUT repos/$R/actions/permissions/selected-actions --input - <<'JSON' >/dev/null
{"github_owned_allowed": true, "verified_allowed": false, "patterns_allowed": []}
JSON
echo "== branch protection on main: PR + 1 review, conversation resolution, CI must pass, no force-push/delete, pushes restricted to the maintainer"
gh api -X PUT repos/$R/branches/main/protection --input - <<'JSON' >/dev/null
{
  "required_status_checks": {"strict": true, "contexts": ["pytest"]},
  "enforce_admins": false,
  "required_pull_request_reviews": {"dismiss_stale_reviews": true, "require_code_owner_reviews": false, "required_approving_review_count": 1},
  "restrictions": {"users": ["mikeorzel"], "teams": [], "apps": []},
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "lock_branch": false
}
JSON
echo "== verify"
gh api repos/$R --jq '{visibility, allow_forking, has_issues, has_wiki, has_projects, secret_scanning: .security_and_analysis.secret_scanning.status, push_protection: .security_and_analysis.secret_scanning_push_protection.status}'
gh api repos/$R/branches/main/protection --jq '{reviews: .required_pull_request_reviews.required_approving_review_count, checks: .required_status_checks.contexts, push_users: [.restrictions.users[].login], force_push: .allow_force_pushes.enabled, deletions: .allow_deletions.enabled}'
echo "done"
