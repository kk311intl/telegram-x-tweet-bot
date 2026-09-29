#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/x-tweet-telegram-bot
PROJECT_DIR=$INSTALL_DIR/project
STATE_DIR=/var/lib/x-tweet-telegram-bot
CONFIG_DIR=/etc/x-tweet-telegram-bot
SERVICE=x-tweet-telegram-bot.service
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
)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

if [[ -f "$PROJECT_DIR/VERSION" ]] && \
   [[ "$(<"$PROJECT_DIR/VERSION")" == "$(<"$SOURCE_DIR/VERSION")" ]]; then
  for file in "${PROJECT_FILES[@]}"; do
    if [[ ! -f "$PROJECT_DIR/$file" ]] || \
       ! cmp -s "$SOURCE_DIR/$file" "$PROJECT_DIR/$file"; then
      echo "Project changed without a VERSION bump: $file" >&2
      exit 2
    fi
  done
fi

missing_packages=()
for package in ca-certificates ffmpeg python3 python3-venv tzdata; do
  if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then
    missing_packages+=("$package")
  fi
done
if (( ${#missing_packages[@]} )); then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing_packages[@]}"
fi

if ! id x-tweet-bot >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin x-tweet-bot
fi

install -d -o root -g root -m 0755 "$INSTALL_DIR" "$PROJECT_DIR" "$CONFIG_DIR"
install -d -o x-tweet-bot -g x-tweet-bot -m 0700 "$STATE_DIR" "$STATE_DIR/tmp"
if [[ -f "$CONFIG_DIR/cookies.txt" && ! -f "$STATE_DIR/cookies.txt" ]]; then
  install -o x-tweet-bot -g x-tweet-bot -m 0600 \
    "$CONFIG_DIR/cookies.txt" "$STATE_DIR/cookies.txt"
  rm -f -- "$CONFIG_DIR/cookies.txt"
fi
if ! command -v uv >/dev/null 2>&1; then
  bootstrap_venv=$(mktemp -d /tmp/x-tweet-uv-bootstrap.XXXXXX)
  python3 -m venv "$bootstrap_venv"
  "$bootstrap_venv/bin/pip" install --disable-pip-version-check uv==0.12.7
  install -o root -g root -m 0755 "$bootstrap_venv/bin/uv" /usr/local/bin/uv
  rm -rf -- "$bootstrap_venv"
fi
export UV_PYTHON_INSTALL_DIR="$INSTALL_DIR/python"
uv python install 3.12
candidate_venv=$(mktemp -d "$INSTALL_DIR/.venv-candidate.XXXXXX")
chmod 0755 "$candidate_venv"
cleanup_candidate() {
  if [[ -n $candidate_venv && $candidate_venv == "$INSTALL_DIR"/.venv-candidate.* ]]; then
    rm -rf -- "$candidate_venv"
  fi
}
trap cleanup_candidate EXIT
uv venv --python 3.12 "$candidate_venv"
uv pip install --python "$candidate_venv/bin/python" \
  -r "$SOURCE_DIR/requirements.txt"

PYTHONPATH="$SOURCE_DIR" "$candidate_venv/bin/python" -m py_compile \
  "$SOURCE_DIR/bot.py" "$SOURCE_DIR/config_cli.py" "$SOURCE_DIR/test_bot.py"
(
  cd "$SOURCE_DIR"
  PYTHONPATH="$SOURCE_DIR" "$candidate_venv/bin/python" -m unittest -q test_bot.py
)

deploy_targets=(
  "$INSTALL_DIR/bot.py"
  "$INSTALL_DIR/test_bot.py"
  "$INSTALL_DIR/verify_deploy.sh"
  "$INSTALL_DIR/requirements.txt"
  /usr/local/sbin/x-tweet-bot-config
  "/etc/systemd/system/$SERVICE"
)
for file in "${PROJECT_FILES[@]}"; do
  deploy_targets+=("$PROJECT_DIR/$file")
done
rollback_dir=$(mktemp -d "$INSTALL_DIR/.deploy-rollback.XXXXXX")
previous_venv_target=""
if [[ -L "$INSTALL_DIR/venv" ]]; then
  previous_venv_target=$(readlink -f "$INSTALL_DIR/venv" || true)
fi
for target in "${deploy_targets[@]}"; do
  if [[ -e $target || -L $target ]]; then
    cp -a --parents "$target" "$rollback_dir"
  fi
done

rollback_deploy() {
  exit_code=$?
  trap - ERR
  if [[ $swapped_venv == true ]]; then
    systemctl stop "$SERVICE" || true
    if [[ -L "$INSTALL_DIR/venv" ]]; then
      rm -f -- "$INSTALL_DIR/venv"
    fi
    if [[ -e "$rollback_dir/venv" || -L "$rollback_dir/venv" ]]; then
      mv -- "$rollback_dir/venv" "$INSTALL_DIR/venv"
    fi
  fi
  for target in "${deploy_targets[@]}"; do
    backup="$rollback_dir$target"
    if [[ -e $backup || -L $backup ]]; then
      cp -a "$backup" "$target"
    else
      rm -f -- "$target"
    fi
  done
  systemctl daemon-reload || true
  systemctl restart "$SERVICE" || true
  if [[ $rollback_dir == "$INSTALL_DIR"/.deploy-rollback.* ]]; then
    rm -rf -- "$rollback_dir"
  fi
  exit "$exit_code"
}
swapped_venv=false
trap rollback_deploy ERR

if systemctl cat "$SERVICE" >/dev/null 2>&1; then
  systemctl stop "$SERVICE"
fi
swapped_venv=true
if [[ -e "$INSTALL_DIR/venv" || -L "$INSTALL_DIR/venv" ]]; then
  mv -- "$INSTALL_DIR/venv" "$rollback_dir/venv"
fi
ln -s "$candidate_venv" "$INSTALL_DIR/venv"

install -o root -g root -m 0755 "$SOURCE_DIR/bot.py" "$INSTALL_DIR/bot.py"
install -o root -g root -m 0644 "$SOURCE_DIR/test_bot.py" "$INSTALL_DIR/test_bot.py"
install -o root -g root -m 0755 "$SOURCE_DIR/verify_deploy.sh" "$INSTALL_DIR/verify_deploy.sh"
install -o root -g root -m 0755 "$SOURCE_DIR/config_cli.py" /usr/local/sbin/x-tweet-bot-config
install -o root -g root -m 0644 "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

for file in "${PROJECT_FILES[@]}"; do
  mode=0644
  case "$file" in
    *.py|*.sh) mode=0755 ;;
  esac
  install -o root -g root -m "$mode" "$SOURCE_DIR/$file" "$PROJECT_DIR/$file"
done

if [[ ! -f /etc/x-tweet-telegram-bot.env ]]; then
  cat >/etc/x-tweet-telegram-bot.env <<EOF
BOOTSTRAP_CODE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
MAX_MEDIA_BYTES=47000000
MAX_QUEUE=12
MAX_TOTAL_BYTES=167772160
WORKER_COUNT=2
INLINE_WORKER_COUNT=2
INLINE_MAX_PENDING=4
INLINE_CACHE_SECONDS=60
OWNER_USER_ID=0
OWNER_LANGUAGE=zh
BOT_TIMEZONE=UTC
DAILY_RESET_HOUR=0
DAILY_REPORT_HOUR=22
DEFAULT_DAILY_LIMIT=50
EOF
  chmod 0600 /etc/x-tweet-telegram-bot.env
fi

install -o root -g root -m 0644 "$SOURCE_DIR/$SERVICE" "/etc/systemd/system/$SERVICE"
python3 -m py_compile "$INSTALL_DIR/bot.py" /usr/local/sbin/x-tweet-bot-config
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 3
systemctl is-active --quiet "$SERVICE"
[[ $(systemctl show "$SERVICE" -p MainPID --value) =~ ^[1-9][0-9]*$ ]]
trap - ERR
if [[ $rollback_dir == "$INSTALL_DIR"/.deploy-rollback.* ]]; then
  rm -rf -- "$rollback_dir"
fi
if [[ $previous_venv_target == "$INSTALL_DIR"/.venv-candidate.* && \
      $previous_venv_target != "$candidate_venv" ]]; then
  rm -rf -- "$previous_venv_target"
fi
candidate_venv=""

echo "service=$(systemctl is-active "$SERVICE")"
/usr/local/sbin/x-tweet-bot-config status
echo "Configure later with: x-tweet-bot-config set-token"
echo "Set owner directly with: x-tweet-bot-config set-owner TELEGRAM_USER_ID"
