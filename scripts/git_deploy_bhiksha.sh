#!/usr/bin/env bash

set -Eeuo pipefail

###############################################################################
# CONFIG — CHANGE THESE
###############################################################################

# Where the project should live on the old Mac.
REPO_DIR="/Users/sunny/Documents/bhiksha"

# Your Git remote.
# Example SSH URL:
# REPO_URL="git@github.com:yourname/yourrepo.git"
REPO_URL="https://github.com/skishore1676/bhiksha"

# Branch that represents production.
BRANCH="main"

# Usually origin.
REMOTE="origin"

# If non-empty, the script checks that origin equals this URL.
# Set this to "" if you do not want exact remote checking.
EXPECTED_REMOTE_URL="$REPO_URL"

# Run tests after pulling/cloning/resetting.
RUN_TESTS=true

# Optional command to run before tests.
# Useful if requirements may have changed.
#
# Examples:
# INSTALL_CMD=".venv/bin/python -m pip install -r requirements.txt"
# INSTALL_CMD="python3 -m pip install -r requirements.txt"
INSTALL_CMD=""

# Test command.
# Leave empty to auto-detect:
#   .venv/bin/python -m pytest -q
# or:
#   python3 -m pytest -q
#
# Example override:
# TEST_CMD=".venv/bin/python -m pytest -q"
TEST_CMD="PYTHONPATH=src .venv/bin/python -m compileall -q src/bhiksha tests"

# Whether to run tests even when repo is already up to date.
RUN_TESTS_WHEN_ALREADY_CURRENT=false

# Optional final deploy/restart command.
#
# Examples:
# DEPLOY_CMD="pm2 restart my-app"
# DEPLOY_CMD="brew services restart my-service"
# DEPLOY_CMD="./scripts/restart.sh"
#
# Start with this empty while testing the script.
DEPLOY_CMD=""

###############################################################################
# INTERNALS
###############################################################################

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
STASHED_LOCAL_CHANGES=false

log() {
  printf "\n==> %s\n" "$*"
}

warn() {
  printf "\nWARNING: %s\n" "$*" >&2
}

die() {
  printf "\nERROR: %s\n" "$*" >&2
  exit 1
}

is_true() {
  case "${1:-}" in
    true|TRUE|yes|YES|y|Y|1) return 0 ;;
    *) return 1 ;;
  esac
}

confirm() {
  local prompt="$1"
  local default="${2:-N}"
  local suffix
  local answer

  if [ "$default" = "Y" ]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi

  while true; do
    printf "%s %s " "$prompt" "$suffix"
    read -r answer || answer=""

    if [ -z "$answer" ]; then
      answer="$default"
    fi

    case "$answer" in
      y|Y|yes|YES|Yes) return 0 ;;
      n|N|no|NO|No) return 1 ;;
      *) printf "Please answer yes or no.\n" ;;
    esac
  done
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

current_branch() {
  git symbolic-ref --quiet --short HEAD 2>/dev/null || true
}

short_sha() {
  git rev-parse --short "$1"
}

run_cmd_string() {
  local label="$1"
  local cmd="$2"

  if [ -z "$cmd" ]; then
    return 0
  fi

  log "$label"
  printf "+ %s\n" "$cmd"
  bash -lc "$cmd"
}

check_config() {
  [ -n "$REPO_DIR" ] || die "REPO_DIR is empty."
  [ -n "$REPO_URL" ] || die "REPO_URL is empty."
  [ -n "$BRANCH" ] || die "BRANCH is empty."
  [ -n "$REMOTE" ] || die "REMOTE is empty."
}

setup_lock() {
  local project_name
  project_name="$(basename "$REPO_DIR" | sed 's/[^A-Za-z0-9_.-]/_/g')"

  local lock_dir="/tmp/deploy-${project_name}.lock"

  if ! mkdir "$lock_dir" 2>/dev/null; then
    die "Another deploy appears to be running. Lock exists: $lock_dir"
  fi

  trap 'rm -rf "'"$lock_dir"'"' EXIT
}

print_target() {
  log "Deploy target"
  printf "Directory: %s\n" "$REPO_DIR"
  printf "Remote:    %s\n" "$REPO_URL"
  printf "Branch:    %s\n" "$BRANCH"
}

git_dir_path() {
  git rev-parse --git-dir
}

check_no_git_operation_in_progress() {
  local git_dir
  git_dir="$(git_dir_path)"

  if [ -f "$git_dir/MERGE_HEAD" ]; then
    die "A merge is in progress. Resolve it manually before deploying."
  fi

  if [ -d "$git_dir/rebase-merge" ] || [ -d "$git_dir/rebase-apply" ]; then
    die "A rebase is in progress. Resolve it manually before deploying."
  fi

  if [ -f "$git_dir/CHERRY_PICK_HEAD" ]; then
    die "A cherry-pick is in progress. Resolve it manually before deploying."
  fi

  if [ -f "$git_dir/REVERT_HEAD" ]; then
    die "A revert is in progress. Resolve it manually before deploying."
  fi
}

verify_remote() {
  local found_remote_url
  found_remote_url="$(git remote get-url "$REMOTE" 2>/dev/null || true)"

  if [ -z "$found_remote_url" ]; then
    die "Remote '$REMOTE' does not exist in this repo."
  fi

  printf "Found %s remote: %s\n" "$REMOTE" "$found_remote_url"

  if [ -n "$EXPECTED_REMOTE_URL" ] && [ "$found_remote_url" != "$EXPECTED_REMOTE_URL" ]; then
    warn "Remote URL does not match expected URL."
    printf "Expected: %s\n" "$EXPECTED_REMOTE_URL"
    printf "Found:    %s\n" "$found_remote_url"

    if ! confirm "Continue anyway?" "N"; then
      die "Aborting because remote URL does not match."
    fi
  fi
}

stash_local_changes() {
  local msg="production-local-changes-before-deploy-${TIMESTAMP}"

  log "Stashing local changes"
  if git stash push -u -m "$msg"; then
    STASHED_LOCAL_CHANGES=true
    return 0
  fi

  # Fallback for older Git versions.
  git stash save --include-untracked "$msg"
  STASHED_LOCAL_CHANGES=true
}

handle_dirty_worktree() {
  local status_output
  status_output="$(git status --porcelain)"

  if [ -z "$status_output" ]; then
    return 0
  fi

  warn "Local changes detected in production repo."
  printf "\nChanged files:\n"
  git status --short

  printf "\nChoose what to do:\n"
  printf "  1) Stash local changes and continue\n"
  printf "  2) Discard local changes and continue\n"
  printf "  3) Abort so I can inspect manually\n"

  local choice
  printf "\nSelection [3]: "
  read -r choice || choice=""
  choice="${choice:-3}"

  case "$choice" in
    1)
      stash_local_changes
      ;;
    2)
      warn "This will delete local modifications and untracked files inside:"
      printf "%s\n" "$REPO_DIR"
      printf "It will not delete ignored files, but it can delete untracked project files.\n"

      local typed
      printf "Type DISCARD to continue: "
      read -r typed || typed=""

      if [ "$typed" != "DISCARD" ]; then
        die "Discard not confirmed."
      fi

      log "Discarding local changes"
      git reset --hard
      git clean -fd
      ;;
    3)
      die "Aborting because local changes exist."
      ;;
    *)
      die "Invalid selection."
      ;;
  esac
}

fetch_remote_branch() {
  log "Fetching latest refs"
  git fetch --prune "$REMOTE"

  if ! git show-ref --verify --quiet "refs/remotes/$REMOTE/$BRANCH"; then
    die "Remote branch '$REMOTE/$BRANCH' does not exist."
  fi
}

ensure_on_branch() {
  local branch_now
  branch_now="$(current_branch)"

  if [ "$branch_now" = "$BRANCH" ]; then
    return 0
  fi

  if [ -z "$branch_now" ]; then
    warn "Repository is in detached HEAD state."
  else
    warn "Current branch is '$branch_now', expected '$BRANCH'."
  fi

  if ! confirm "Switch to '$BRANCH'?" "N"; then
    die "Aborting because current branch is not '$BRANCH'."
  fi

  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git checkout "$BRANCH"
  else
    git checkout -b "$BRANCH" --track "$REMOTE/$BRANCH"
  fi
}

rollback_offer() {
  local old_head="$1"
  local reason="$2"
  local default_choice="${3:-Y}"

  warn "$reason"

  if [ -n "$old_head" ] && git cat-file -e "$old_head^{commit}" 2>/dev/null; then
    local old_short
    old_short="$(short_sha "$old_head")"

    if confirm "Reset repository back to previous commit $old_short?" "$default_choice"; then
      log "Rolling back repository to $old_short"
      git reset --hard "$old_head"
      die "$reason. Rolled back repository to $(short_sha HEAD)."
    fi
  fi

  die "$reason. Repository remains at $(short_sha HEAD)."
}

auto_test_command() {
  if [ -n "$TEST_CMD" ]; then
    printf "%s\n" "$TEST_CMD"
    return 0
  fi

  if [ -x ".venv/bin/python" ]; then
    printf "%s\n" ".venv/bin/python -m pytest -q"
  else
    printf "%s\n" "python3 -m pytest -q"
  fi
}

run_post_update_pipeline() {
  local old_head="$1"
  local reason="$2"

  log "Repository updated"
  printf "Reason:        %s\n" "$reason"
  printf "Current HEAD:  %s\n" "$(short_sha HEAD)"
  printf "Current branch:%s\n" " $(current_branch)"

  if [ -n "$INSTALL_CMD" ]; then
    if ! run_cmd_string "Running install/pre-test command" "$INSTALL_CMD"; then
      rollback_offer "$old_head" "Install/pre-test command failed" "Y"
    fi
  fi

  if is_true "$RUN_TESTS"; then
    local cmd
    cmd="$(auto_test_command)"

    if ! run_cmd_string "Running tests" "$cmd"; then
      rollback_offer "$old_head" "Tests failed" "Y"
    fi
  else
    log "Skipping tests because RUN_TESTS=false"
  fi

  if [ -n "$DEPLOY_CMD" ]; then
    if ! run_cmd_string "Running deploy/restart command" "$DEPLOY_CMD"; then
      rollback_offer "$old_head" "Deploy/restart command failed" "N"
    fi
  else
    log "No deploy/restart command configured; repository update only"
  fi

  if is_true "$STASHED_LOCAL_CHANGES"; then
    warn "Local changes were stashed before deploy."
    printf "Recent stashes:\n"
    git stash list | head -n 5
  fi

  log "Deploy script completed successfully"
  printf "Final commit: %s\n" "$(short_sha HEAD)"
}

clone_repo_and_run_pipeline() {
  log "Cloning repository"
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"

  cd "$REPO_DIR"

  run_post_update_pipeline "" "fresh clone of $REMOTE/$BRANCH"
}

handle_missing_or_non_git_dir() {
  if [ ! -d "$REPO_DIR" ]; then
    warn "Project directory does not exist: $REPO_DIR"

    if confirm "Clone '$BRANCH' from '$REPO_URL' into this directory?" "N"; then
      clone_repo_and_run_pipeline
      exit 0
    fi

    die "Aborting because project directory does not exist."
  fi

  if ! git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    warn "Directory exists, but it is not a Git repository:"
    printf "%s\n" "$REPO_DIR"

    if confirm "Rename this directory to a backup and clone fresh?" "N"; then
      local backup_dir="${REPO_DIR}.backup-${TIMESTAMP}"

      if [ -e "$backup_dir" ]; then
        die "Backup path already exists: $backup_dir"
      fi

      log "Renaming existing directory"
      mv "$REPO_DIR" "$backup_dir"
      printf "Backup created: %s\n" "$backup_dir"

      clone_repo_and_run_pipeline
      exit 0
    fi

    die "Aborting because directory is not a Git repository."
  fi
}

compare_and_update() {
  local local_sha
  local remote_sha
  local base_sha

  local_sha="$(git rev-parse HEAD)"
  remote_sha="$(git rev-parse "$REMOTE/$BRANCH")"
  base_sha="$(git merge-base HEAD "$REMOTE/$BRANCH")"

  log "Repository status"
  printf "Local HEAD:   %s\n" "$(short_sha "$local_sha")"
  printf "Remote HEAD:  %s\n" "$(short_sha "$remote_sha")"

  if [ "$local_sha" = "$remote_sha" ]; then
    log "Already up to date with $REMOTE/$BRANCH"

    if is_true "$RUN_TESTS_WHEN_ALREADY_CURRENT"; then
      run_post_update_pipeline "$local_sha" "already current; running configured checks"
    fi

    exit 0
  fi

  if [ "$local_sha" = "$base_sha" ]; then
    local behind_count
    behind_count="$(git rev-list --count "HEAD..$REMOTE/$BRANCH")"

    printf "\nLocal '%s' is behind '%s/%s' by %s commit(s).\n" \
      "$BRANCH" "$REMOTE" "$BRANCH" "$behind_count"

    if ! confirm "Fast-forward to latest $REMOTE/$BRANCH?" "N"; then
      die "Aborting without pulling."
    fi

    local old_head
    old_head="$local_sha"

    log "Fast-forwarding to $REMOTE/$BRANCH"
    git merge --ff-only "$REMOTE/$BRANCH"

    run_post_update_pipeline "$old_head" "fast-forward from $REMOTE/$BRANCH"
    exit 0
  fi

  if [ "$remote_sha" = "$base_sha" ]; then
    local ahead_count
    ahead_count="$(git rev-list --count "$REMOTE/$BRANCH..HEAD")"

    warn "Local '$BRANCH' is ahead of '$REMOTE/$BRANCH' by $ahead_count commit(s)."
    printf "This production machine has commits that are not on the remote.\n"

    if confirm "Create backup branch and reset hard to $REMOTE/$BRANCH?" "N"; then
      local backup_branch="backup/production-ahead-${TIMESTAMP}"
      local old_head="$local_sha"

      log "Creating backup branch: $backup_branch"
      git branch "$backup_branch" HEAD

      log "Resetting to $REMOTE/$BRANCH"
      git reset --hard "$REMOTE/$BRANCH"

      run_post_update_pipeline "$old_head" "reset local-ahead branch to $REMOTE/$BRANCH"
      exit 0
    fi

    die "Aborting because local branch is ahead of remote."
  fi

  local ahead_count
  local behind_count
  ahead_count="$(git rev-list --count "$REMOTE/$BRANCH..HEAD")"
  behind_count="$(git rev-list --count "HEAD..$REMOTE/$BRANCH")"

  warn "Local '$BRANCH' and '$REMOTE/$BRANCH' have diverged."
  printf "Local-only commits:  %s\n" "$ahead_count"
  printf "Remote-only commits: %s\n" "$behind_count"

  if confirm "Create backup branch and reset hard to $REMOTE/$BRANCH?" "N"; then
    local backup_branch="backup/production-diverged-${TIMESTAMP}"
    local old_head="$local_sha"

    log "Creating backup branch: $backup_branch"
    git branch "$backup_branch" HEAD

    log "Resetting to $REMOTE/$BRANCH"
    git reset --hard "$REMOTE/$BRANCH"

    run_post_update_pipeline "$old_head" "reset diverged branch to $REMOTE/$BRANCH"
    exit 0
  fi

  die "Aborting because local and remote branches diverged."
}

main() {
  require_cmd git
  require_cmd bash
  check_config
  setup_lock
  print_target

  handle_missing_or_non_git_dir

  cd "$REPO_DIR"

  check_no_git_operation_in_progress
  verify_remote
  handle_dirty_worktree
  fetch_remote_branch
  ensure_on_branch

  # Re-check after branch switch.
  check_no_git_operation_in_progress
  handle_dirty_worktree

  compare_and_update
}

main "$@"
