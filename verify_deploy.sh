#!/usr/bin/env bash
set -u

APP_DIR=/opt/x-tweet-telegram-bot
SERVICE=x-tweet-telegram-bot.service
TEST_URL=${TEST_URL:-}
BOT_TIMEZONE=$(sed -n 's/^BOT_TIMEZONE=//p' /etc/x-tweet-telegram-bot.env | tail -n 1)
BOT_TIMEZONE=${BOT_TIMEZONE:-UTC}

if [[ -n $TEST_URL ]]; then
echo "TEXT_TEST"
runuser -u x-tweet-bot -- env PYTHONPATH="$APP_DIR" \
  "$APP_DIR/venv/bin/python" - "$TEST_URL" <<'PY'
import sys
from bot import fetch_tweet_text, media_caption

text, author, author_url = fetch_tweet_text(sys.argv[1])
print(f"author={author}")
print(f"author_url={author_url}")
print(f"text_chars={len(text)}")
if not text:
    raise SystemExit("oEmbed returned no tweet text")
if not author_url.startswith("https://x.com/"):
    raise SystemExit("oEmbed returned no trusted author profile URL")
caption = media_caption(author, author_url, text, sys.argv[1])
expected_link = f'<a href="{author_url}">{author}</a>:'
if not caption.startswith(expected_link):
    raise SystemExit("author profile URL is not linked from the author name")
PY
text_rc=$?

echo "MEDIA_TEST"
timeout 45s runuser -u x-tweet-bot -- \
  "$APP_DIR/venv/bin/gallery-dl" --simulate "$TEST_URL"
media_rc=$?
else
  echo "NETWORK_TEST_SKIPPED Set TEST_URL to a public single-post URL to run it."
  text_rc=0
  media_rc=0
fi
echo "media_exit=$media_rc"

echo "STATE_JSON"
runuser -u x-tweet-bot -- env PYTHONPATH="$APP_DIR" STATE_DIR=/var/lib/x-tweet-telegram-bot BOT_TIMEZONE="$BOT_TIMEZONE" \
  "$APP_DIR/venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path
from bot import bot_date

MAX_TELEGRAM_USER_ID = (1 << 52) - 1
TODAY = bot_date()


def valid_user_id(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_TELEGRAM_USER_ID
    )


def validate_acl(payload, label):
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} must contain an object")
    owner_id = payload.get("owner_id", 0)
    if owner_id and not valid_user_id(owner_id):
        raise SystemExit(f"{label} contains an invalid owner ID")
    users = payload.get("users")
    pending = payload.get("pending_applications")
    if not isinstance(users, dict) or not isinstance(pending, dict):
        raise SystemExit(f"{label} users and pending applications must be objects")
    for setting in (
        "external_access_enabled",
        "ordinary_user_cookies_enabled",
        "auto_approve_enabled",
    ):
        if setting in payload and not isinstance(payload[setting], bool):
            raise SystemExit(f"{label} contains an invalid {setting}")

    used_today = 0
    active_today = 0
    profiles_refreshed_today = 0
    for key, record in users.items():
        try:
            user_id = int(key)
        except (TypeError, ValueError):
            raise SystemExit(f"{label} contains a non-numeric user key") from None
        if not valid_user_id(user_id) or str(user_id) != key:
            raise SystemExit(f"{label} contains an invalid user key")
        if not isinstance(record, dict):
            raise SystemExit(f"{label} contains a non-object user record")
        if "user_id" in record and record["user_id"] != user_id:
            raise SystemExit(f"{label} contains a mismatched user ID")
        quota = record.get("quota", 0)
        if quota is not None and (
            not isinstance(quota, int)
            or isinstance(quota, bool)
            or not -1 <= quota <= 10000
        ):
            raise SystemExit(f"{label} contains an invalid quota")
        updated_at = record.get("quota_updated_at", 0)
        if (
            not isinstance(updated_at, int)
            or isinstance(updated_at, bool)
            or updated_at <= 0
        ):
            raise SystemExit(f"{label} contains an invalid quota timestamp")
        usage_count = record.get("usage_count", 0)
        if (
            not isinstance(usage_count, int)
            or isinstance(usage_count, bool)
            or usage_count < 0
        ):
            raise SystemExit(f"{label} contains an invalid usage count")
        usage_date = record.get("usage_date", "")
        profile_date = record.get("profile_checked_date", "")
        if not isinstance(usage_date, str) or not isinstance(profile_date, str):
            raise SystemExit(f"{label} contains an invalid date field")
        for field in ("username", "first_name", "last_name"):
            if field in record and not isinstance(record[field], str):
                raise SystemExit(f"{label} contains an invalid profile field")
        if usage_date == TODAY:
            active_today += int(usage_count > 0)
            used_today += usage_count
        if profile_date == TODAY:
            profiles_refreshed_today += 1

    for key, application in pending.items():
        try:
            pending_user_id = int(key)
        except (TypeError, ValueError):
            raise SystemExit(f"{label} contains an invalid pending user key") from None
        if not valid_user_id(pending_user_id) or str(pending_user_id) != key:
            raise SystemExit(f"{label} contains an invalid pending user key")
        if not isinstance(application, dict):
            raise SystemExit(f"{label} contains a non-object pending request")
        requested_at = application.get("requested_at", 0)
        if (
            not isinstance(requested_at, (int, float))
            or isinstance(requested_at, bool)
            or requested_at <= 0
        ):
            raise SystemExit(f"{label} contains an invalid request timestamp")

    return len(users), len(pending), active_today, used_today, profiles_refreshed_today


state_dir = Path(os.environ["STATE_DIR"])
acl_path = state_dir / "acl.json"
acl = json.loads(acl_path.read_text(encoding="utf-8"))
summary = validate_acl(acl, "acl.json")

backup_path = state_dir / "acl.json.bak"
if backup_path.exists():
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    validate_acl(backup, "acl.json.bak")

offset_path = state_dir / "update-offset.json"
if offset_path.exists():
    offset = json.loads(offset_path.read_text(encoding="utf-8")).get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise SystemExit("update-offset.json contains an invalid offset")

print(f"acl_json=ok backup_json={'ok' if backup_path.exists() else 'not_created'} "
      f"offset_json={'ok' if offset_path.exists() else 'not_created'}")
print(
    "acl_users=%s pending=%s active_today=%s used_today=%s "
    "profiles_refreshed_today=%s" % summary
)
PY
state_rc=$?

echo "PROCESS_NETWORK"
pid="$(systemctl show "$SERVICE" -p MainPID --value)"
echo "pid=$pid"
if ss -lntup | grep -Fq "pid=$pid,"; then
  echo "unexpected_listener=yes"
  ss -lntup | grep -F "pid=$pid,"
  listener_rc=1
else
  echo "unexpected_listener=no"
  listener_rc=0
fi

echo "CONFIG_STATUS"
x-tweet-bot-config status

if (( text_rc != 0 || media_rc != 0 || state_rc != 0 || listener_rc != 0 )); then
  exit 1
fi
