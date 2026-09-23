#!/usr/bin/env bash
set -euo pipefail

SERVICE=x-tweet-telegram-bot.service
PROJECT_DIR=/opt/x-tweet-telegram-bot/project
PROJECT_FILES=(
  VERSION
  LICENSE
  README.md
  requirements.txt
  bot.py
  test_bot.py
  config_cli.py
  deploy.sh
  verify_deploy.sh
  x-tweet-telegram-bot.service
  access_sync_endpoint.sh
  access_backup.sh
  deploy_backup_receiver.sh
  x-tweet-access-backup.service
  x-tweet-access-backup.timer
)

case "${SSH_ORIGINAL_COMMAND:-}" in
  "x-tweet-sync export")
    exec /usr/local/sbin/x-tweet-bot-config export-access
    ;;
  "x-tweet-sync status")
    exec systemctl is-active "$SERVICE"
    ;;
  "x-tweet-sync code-version")
    exec cat "$PROJECT_DIR/VERSION"
    ;;
  "x-tweet-sync code-export")
    for file in "${PROJECT_FILES[@]}"; do
      if [[ ! -f "$PROJECT_DIR/$file" || -L "$PROJECT_DIR/$file" ]]; then
        echo "Invalid project file: $file" >&2
        exit 1
      fi
    done
    exec tar --sort=name --owner=0 --group=0 --numeric-owner \
      -czf - -C "$PROJECT_DIR" "${PROJECT_FILES[@]}"
    ;;
  *)
    echo "Access denied" >&2
    exit 126
    ;;
esac
