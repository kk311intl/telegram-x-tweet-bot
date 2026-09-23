#!/opt/x-tweet-telegram-bot/venv/bin/python
from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


ENV_PATH = Path("/etc/x-tweet-telegram-bot.env")
STATE_PATH = Path("/var/lib/x-tweet-telegram-bot/acl.json")
COOKIE_PATH = Path("/var/lib/x-tweet-telegram-bot/cookies.txt")
COOKIE_ALERT_PATH = Path("/var/lib/x-tweet-telegram-bot/cookie-alert.json")
APP_DIR = Path("/opt/x-tweet-telegram-bot")


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return values


def save_env(values: dict[str, str]) -> None:
    ENV_PATH.write_text(
        "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n",
        encoding="utf-8",
    )
    os.chmod(ENV_PATH, 0o600)


def restart() -> None:
    subprocess.run(["systemctl", "restart", "x-tweet-telegram-bot.service"], check=True)


def telegram_bot_username(token: str) -> str:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/getMe", timeout=15
    ) as response:
        bot_info = json.load(response)
    username = str((bot_info.get("result") or {}).get("username") or "")
    if not bot_info.get("ok") or not username:
        raise ValueError("Telegram did not return a bot username")
    return username


def acl_store(values: dict[str, str]):
    sys.path.insert(0, str(APP_DIR))
    from bot import ACLStore

    return ACLStore(STATE_PATH, int(values.get("OWNER_USER_ID", "0") or 0))


def validate_owner_assignment(path: Path, requested_owner: int) -> None:
    if not 1 <= requested_owner < (1 << 52):
        raise SystemExit("Telegram User ID is out of range")
    if not path.exists():
        return
    try:
        existing_owner = int(
            (json.loads(path.read_text(encoding="utf-8"))).get("owner_id", 0) or 0
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit("Could not validate the existing Owner") from error
    if existing_owner and existing_owner != requested_owner:
        raise SystemExit("Owner is already claimed; refusing to replace it")


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("Run as root")
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    values = load_env()

    if command == "set-token":
        token = getpass.getpass("Telegram Bot Token: ").strip()
        if ":" not in token or len(token) < 30:
            raise SystemExit("Token format is invalid")
        try:
            username = telegram_bot_username(token)
        except Exception:
            raise SystemExit("Token validation failed") from None
        values["BOT_TOKEN"] = token
        values["BOT_USERNAME"] = username
        save_env(values)
        restart()
    elif command == "refresh-identity":
        token = values.get("BOT_TOKEN", "")
        if not token:
            raise SystemExit("Token is not configured")
        try:
            values["BOT_USERNAME"] = telegram_bot_username(token)
        except Exception:
            raise SystemExit("Token validation failed") from None
        save_env(values)
        restart()
    elif command == "set-owner":
        if len(sys.argv) != 3 or not sys.argv[2].isdigit():
            raise SystemExit("Usage: x-tweet-bot-config set-owner <Telegram User ID>")
        requested_owner = int(sys.argv[2])
        validate_owner_assignment(STATE_PATH, requested_owner)
        values["OWNER_USER_ID"] = str(requested_owner)
        save_env(values)
        restart()
    elif command == "set-cookies":
        if len(sys.argv) != 3:
            raise SystemExit("Usage: x-tweet-bot-config set-cookies <cookies.txt>")
        source = Path(sys.argv[2]).resolve()
        first_line = source.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if first_line not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
            raise SystemExit("cookies.txt must use Netscape format")
        COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, COOKIE_PATH)
        os.chmod(COOKIE_PATH, 0o600)
        shutil.chown(COOKIE_PATH, user="x-tweet-bot", group="x-tweet-bot")
        COOKIE_ALERT_PATH.unlink(missing_ok=True)
        restart()
    elif command == "clear-cookies":
        COOKIE_PATH.unlink(missing_ok=True)
        COOKIE_ALERT_PATH.unlink(missing_ok=True)
        restart()
    elif command == "new-claim-code":
        values["BOOTSTRAP_CODE"] = secrets.token_urlsafe(24)
        save_env(values)
        print(values["BOOTSTRAP_CODE"])
        restart()
    elif command == "export-access":
        sys.path.insert(0, str(APP_DIR))
        from bot import ACLStore
        print(json.dumps(ACLStore.read_access_snapshot(STATE_PATH), ensure_ascii=False))
    elif command == "import-access":
        if subprocess.run(["systemctl", "is-active", "--quiet", "x-tweet-telegram-bot.service"]).returncode == 0:
            raise SystemExit("Stop the Bot before restoring access data")
        try:
            snapshot = json.load(sys.stdin)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"Invalid access snapshot: {error}") from error
        print(f"changed={acl_store(values).import_access(snapshot)}")
    elif command == "status":
        print(f"token={'configured' if values.get('BOT_TOKEN') else 'not configured'}")
        print(f"bot_username={values.get('BOT_USERNAME') or 'not configured'}")
        print(f"cookies={'configured' if COOKIE_PATH.exists() else 'not configured'}")
        if STATE_PATH.exists():
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                users = state.get("users") or {}
                pending = state.get("pending_applications") or {}
                quotas = [record.get("quota", 0) for record in users.values()]
                print(
                    "owner="
                    + ("configured" if int(state.get("owner_id", 0) or 0) else "not configured")
                )
                print(f"users_total={len(users)}")
                print(f"users_admin={sum(value is None for value in quotas)}")
                print(f"users_allowed={sum(isinstance(value, int) and value > 0 for value in quotas)}")
                print(f"users_initialized={sum(value == 0 for value in quotas)}")
                print(f"users_blocked={sum(value == -1 for value in quotas)}")
                print(f"pending_applications={len(pending)}")
                print(
                    "ordinary_user_access="
                    + ("enabled" if state.get("external_access_enabled", True) else "disabled")
                )
                print(
                    "ordinary_user_cookies="
                    + ("enabled" if state.get("ordinary_user_cookies_enabled", True) else "disabled")
                )
                print(
                    "auto_approve="
                    + ("enabled" if state.get("auto_approve_enabled", False) else "disabled")
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                print("state=invalid")
        else:
            print(
                "owner="
                + ("bootstrap configured" if values.get("OWNER_USER_ID") else "not configured")
            )
            print("state=not initialized")
        subprocess.run(
            ["systemctl", "is-active", "x-tweet-telegram-bot.service"], check=False
        )
    else:
        raise SystemExit(
            "Commands: status, set-token, set-owner, set-cookies, clear-cookies, "
            "new-claim-code, refresh-identity, export-access, import-access"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
