#!/usr/bin/env python3
from __future__ import annotations

import html
import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from PIL import Image, ImageOps
from urllib3.exceptions import HTTPError as StreamHTTPError


APP_NAME = "x-tweet-telegram-bot"
APP_VERSION = "3.3.6"
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/x-tweet-telegram-bot"))
ACL_PATH = STATE_DIR / "acl.json"
UPDATE_OFFSET_PATH = STATE_DIR / "update-offset.json"
TMP_DIR = STATE_DIR / "tmp"
COOKIES_PATH = STATE_DIR / "cookies.txt"
COOKIE_ALERT_PATH = STATE_DIR / "cookie-alert.json"
def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
BOT_MENTION = f"@{BOT_USERNAME}" if BOT_USERNAME else "@YourBotUsername"
ENV_OWNER_ID = env_int("OWNER_USER_ID", 0, 0, (1 << 52) - 1)
BOOTSTRAP_CODE = os.environ.get("BOOTSTRAP_CODE", "").strip()
OWNER_CONTACT_URL = os.environ.get("OWNER_CONTACT_URL", "").strip()
OWNER_CONTACT_LABEL = os.environ.get("OWNER_CONTACT_LABEL", "").strip()
CURRENT_LANGUAGE: ContextVar[str] = ContextVar("current_language", default="zh")


def ui_language() -> str:
    return CURRENT_LANGUAGE.get()


@contextmanager
def language_scope(language: str):
    token = CURRENT_LANGUAGE.set(language if language in PUBLIC_TEXT else "zh")
    try:
        yield
    finally:
        CURRENT_LANGUAGE.reset(token)


BOT_TIMEZONE_NAME = os.environ.get("BOT_TIMEZONE", "UTC").strip()
try:
    BOT_TIMEZONE = timezone.utc if BOT_TIMEZONE_NAME == "UTC" else ZoneInfo(BOT_TIMEZONE_NAME)
except (ZoneInfoNotFoundError, ValueError) as error:
    raise ValueError("BOT_TIMEZONE must be a valid IANA time zone") from error
MAX_QUEUE = env_int("MAX_QUEUE", 12, 1, 100)
WORKER_COUNT = env_int("WORKER_COUNT", 2, 1, 8)
INLINE_WORKER_COUNT = env_int("INLINE_WORKER_COUNT", 2, 1, 4)
INLINE_MAX_PENDING = max(
    INLINE_WORKER_COUNT,
    env_int("INLINE_MAX_PENDING", 4, 1, 32),
)
MAX_MEDIA_BYTES = env_int("MAX_MEDIA_BYTES", 48 * 1024 * 1024, 1024 * 1024, 50_000_000)
MAX_VIDEO_BYTES = env_int("MAX_VIDEO_BYTES", 50_000_000, 1024 * 1024, 50_000_000)
MAX_TOTAL_BYTES = env_int(
    "MAX_TOTAL_BYTES", 160 * 1024 * 1024, 1024 * 1024, 500 * 1024 * 1024
)
MAX_COOKIE_BYTES = 1024 * 1024
MIN_FREE_DISK_BYTES = env_int("MIN_FREE_DISK_BYTES", 1024 ** 3, 64 * 1024 ** 2, 20 * 1024 ** 3)
TEMP_SOFT_LIMIT_BYTES = env_int("TEMP_SOFT_LIMIT_BYTES", 1024 ** 3, 64 * 1024 ** 2, 100 * 1024 ** 3)
TEMP_RETENTION_HOURS = env_int("TEMP_RETENTION_HOURS", 24, 1, 720)
COOKIE_ALERT_INTERVAL = 24 * 60 * 60
MAX_DAILY_LIMIT = 100_000
DEFAULT_DAILY_LIMIT = env_int("DEFAULT_DAILY_LIMIT", 50, 1, MAX_DAILY_LIMIT)
MANAGEMENT_PAGE_SIZE = 20
MIN_TELEGRAM_USER_ID_SHORTCUT = 100_000
MAX_TELEGRAM_USER_ID = (1 << 52) - 1
USER_LIST_NAME_WIDTH = 10
INLINE_USAGE_DEDUP_SECONDS = 5 * 60
INLINE_RESULT_LIMIT = 10
INLINE_CACHE_SECONDS = env_int("INLINE_CACHE_SECONDS", 60, 0, 3600)
TELEGRAM_RETRY_AFTER_MAX_SECONDS = 30
DAILY_REPORT_HOUR = env_int("DAILY_REPORT_HOUR", 22, 0, 23)
DAILY_RESET_HOUR = env_int("DAILY_RESET_HOUR", 0, 0, 23)

HTTP_LOCAL = threading.local()
MEDIA_DISK_LOCK = threading.Lock()
ACTIVE_MEDIA_DIRS: set[Path] = set()


def bot_date(timestamp: float | None = None) -> str:
    current = time.time() if timestamp is None else timestamp
    local = datetime.fromtimestamp(current, BOT_TIMEZONE)
    if local.hour < DAILY_RESET_HOUR:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def http_session() -> requests.Session:
    """Reuse DNS, TCP and TLS state within each bounded worker thread."""
    if not hasattr(HTTP_LOCAL, "session"):
        session = requests.Session()
        session.headers["User-Agent"] = f"{APP_NAME}/{APP_VERSION}"
        HTTP_LOCAL.session = session
    return HTTP_LOCAL.session


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        temporary.replace(path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def load_update_offset(path: Path = UPDATE_OFFSET_PATH) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("update offset state must be an object")
        offset = payload.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("update offset must be a non-negative integer")
        return offset
    except FileNotFoundError:
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        LOG.exception("Could not load Telegram update offset; starting from zero")
        return 0


def save_update_offset(offset: int, path: Path = UPDATE_OFFSET_PATH) -> None:
    atomic_write_text(path, json.dumps({"offset": max(0, int(offset))}) + "\n")


def media_disk_available() -> bool:
    try:
        if TMP_DIR.is_symlink():
            return False
        filesystem = TMP_DIR
        while not filesystem.exists():
            if filesystem.parent == filesystem:
                return False
            filesystem = filesystem.parent
        if shutil.disk_usage(filesystem).free < MIN_FREE_DISK_BYTES:
            return False
        total = 0
        for path in TMP_DIR.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass  # A concurrent job removed or renamed its temporary file.
        return total < TEMP_SOFT_LIMIT_BYTES
    except OSError:
        LOG.warning("Could not check media disk space")
        return False


def cleanup_stale_media() -> None:
    # Never trim ACL recovery files or credentials to meet a space target.
    cutoff = time.time() - TEMP_RETENTION_HOURS * 3600
    if TMP_DIR.is_symlink():
        LOG.warning("Skipping media cleanup on a symlinked temporary root")
        return
    with MEDIA_DISK_LOCK:
        for directory in TMP_DIR.glob("tweet-*"):
            try:
                if directory in ACTIVE_MEDIA_DIRS or directory.is_symlink() or not directory.is_dir():
                    continue
                if directory.stat().st_mtime >= cutoff or any(path.lstat().st_mtime >= cutoff for path in directory.rglob("*")):
                    continue
                if directory.resolve().parent == TMP_DIR.resolve():
                    shutil.rmtree(directory)
            except OSError:
                LOG.warning("Could not clean an expired media directory")
    if len(list(STATE_DIR.glob("acl.json.corrupt-*"))) > 3:
        LOG.warning("More than three ACL recovery files retained; review manually")


@contextmanager
def media_temporary_directory():
    with MEDIA_DISK_LOCK:
        temporary = tempfile.TemporaryDirectory(prefix="tweet-", dir=TMP_DIR)
        directory = Path(temporary.name)
        ACTIVE_MEDIA_DIRS.add(directory)
    try:
        yield directory
    finally:
        with MEDIA_DISK_LOCK:
            try:
                temporary.cleanup()
            finally:
                ACTIVE_MEDIA_DIRS.discard(directory)

OWNER_BUTTONS = {
    "👤 用戶管理": "/usermenu",
    "🍪 Cookies 管理": "/cookiemenu",
    "📊 系統狀態": "/status",
    "ℹ️ 使用說明": "/help",
    "👥 用戶列表": "/users",
    "📝 申請審批": "/requests",
    "🔐 用戶權限修改": "/finduser",
    "🍪 匯入 Cookies": "/cookies",
    "📖 Cookies 說明": "/cookiehelp",
    "🗑 清除 Cookies": "/clearcookies",
    "↩️ 返回主選單": "/menu",
}

PUBLIC_TEXT = {
    "zh-cn": {
        "start_allowed": "请发送有效的 X/Twitter 单篇推文链接。",
        "help_allowed": (
            "使用说明\n\n"
            "发送单篇 X/Twitter 推文链接，即可获取文字、图片和视频。\n"
            f"在其他聊天输入 {BOT_MENTION} 加上推文链接，可选择并分享媒体。"
        ),
        "language_menu": "🌐 語言/Language",
        "help_menu": "ℹ️ 使用说明",
        "choose_language": "请选择语言。",
        "back": "↩️ 返回",
        "start_owner": "管理菜单已加载，也可以直接发送 X/Twitter 推文链接。",
        "access": "此账号尚未获取使用权限。",
        "apply": "申请使用权限",
        "apply_created": "申请已提交。",
        "apply_pending": "申请待审批。",
        "apply_allowed": "已获取使用权限。",
        "apply_auto_approved": "申请已通过，可以开始使用。",
        "service_paused": "目前暂停使用，请稍后再试。",
        "invalid_url": "请发送有效的 X/Twitter 单篇推文链接。",
        "queue_full": "目前较忙，请稍后再试。",
        "quota": "今日使用已达到预设上限，请稍后再试或联系管理员。",
        "approved": "申请已通过，可以开始使用。",
        "failed": "处理失败，请稍后重试。",
        "post_unavailable": "无法获取这条推文，请确认推文可公开浏览或稍后再试。",
        "url_only": "请发送有效的 X/Twitter 单篇推文链接。",
        "inline_apply": "开启机器人申请使用权限",
        "video_oversized": "{count} 个视频超过 Telegram 的 50 MB 上限，已跳过。",
        "images_skipped": "部分图片超过大小限制，已跳过。",
        "preview_failed": "媒体预览发送失败，请稍后重试。",
        "originals_failed": "部分原始文件发送失败，请稍后重试。",
        "language_set": "语言已切换为简体中文。",
        "unsupported_language": "不支持此语言。",
    },
    "zh": {
        "start_allowed": "請傳送有效的 X/Twitter 單篇貼文網址。",
        "help_allowed": (
            "使用說明\n\n"
            "傳送單篇 X/Twitter 貼文網址，即可取得文字、圖片和影片。\n"
            f"在其他聊天輸入 {BOT_MENTION} 加上貼文網址，可選擇並分享媒體。"
        ),
        "language_menu": "🌐 語言/Language",
        "help_menu": "ℹ️ 使用說明",
        "choose_language": "請選擇語言。",
        "back": "↩️ 返回",
        "start_owner": "管理選單已載入，也可以直接傳送 X/Twitter 貼文連結。",
        "access": "此帳號尚未取得使用權限。",
        "apply": "申請使用權限",
        "apply_created": "申請已送出。",
        "apply_pending": "申請待審批。",
        "apply_allowed": "已取得使用權限。",
        "apply_auto_approved": "申請已通過，可以開始使用。",
        "service_paused": "目前暫停使用，請稍後再試。",
        "invalid_url": "請傳送有效的 X/Twitter 單篇貼文網址。",
        "queue_full": "目前較忙，請稍後再試。",
        "quota": "今日使用已達到預設上限，請稍後再試或聯繫管理員。",
        "approved": "申請已通過，可以開始使用。",
        "failed": "處理失敗，請稍後重試。",
        "post_unavailable": "無法取得這則貼文，請確認貼文可公開瀏覽或稍後再試。",
        "url_only": "請傳送有效的 X/Twitter 單篇貼文網址。",
        "inline_apply": "開啟機器人申請使用權限",
        "video_oversized": "{count} 個影片超過 Telegram 的 50 MB 上限，已略過。",
        "images_skipped": "部分圖片超過大小限制，已略過。",
        "preview_failed": "媒體預覽傳送失敗，請稍後重試。",
        "originals_failed": "部分原始檔案傳送失敗，請稍後重試。",
        "language_set": "語言已切換為繁體中文。",
        "unsupported_language": "不支援此語言。",
    },
    "en": {
        "start_allowed": "Send a valid single-post X/Twitter URL.",
        "help_allowed": (
            "How to use\n\n"
            "Send a single X/Twitter post URL to get its text, images and videos.\n"
            f"In another chat, type {BOT_MENTION} followed by the post URL to select and share media."
        ),
        "language_menu": "🌐 語言/Language",
        "help_menu": "ℹ️ How to use",
        "choose_language": "Choose a language.",
        "back": "↩️ Back",
        "start_owner": "The management menu is ready. You can also send an X/Twitter post URL directly.",
        "access": "This account does not have access yet.",
        "apply": "Request access",
        "apply_created": "Your request was submitted.",
        "apply_pending": "Your request is still pending.",
        "apply_allowed": "You already have access.",
        "apply_auto_approved": "Access approved. You can start using the bot.",
        "service_paused": "Service temporarily paused. Try again later.",
        "invalid_url": "Send a valid single-post X/Twitter URL.",
        "queue_full": "The bot is busy. Try again later.",
        "quota": "You have reached the preset usage limit for today. Try again later or contact an administrator.",
        "approved": "Access approved. You can start using the bot.",
        "failed": "Processing failed. Try again later.",
        "post_unavailable": "Couldn't get this post. Check that it is public or try again later.",
        "url_only": "Send a valid single-post X/Twitter URL.",
        "inline_apply": "Open the Bot to request access",
        "video_oversized": "{count} video(s) exceeded Telegram's 50 MB limit and were skipped.",
        "images_skipped": "Some images were too large and were skipped.",
        "preview_failed": "Couldn't send the media preview. Try again later.",
        "originals_failed": "Couldn't send some original files. Try again later.",
        "language_set": "Language changed to English.",
        "unsupported_language": "Unsupported language.",
    },
    "ja": {
        "start_allowed": "有効な X/Twitter の単一投稿URLを送信してください。",
        "help_allowed": (
            "使い方\n\n"
            "X/Twitter の単一投稿URLを送ると、本文・画像・動画を取得できます。\n"
            f"他のチャットで {BOT_MENTION} に続けて投稿URLを入力すると、メディアを選んで共有できます。"
        ),
        "language_menu": "🌐 語言/Language",
        "help_menu": "ℹ️ 使い方",
        "choose_language": "言語を選択してください。",
        "back": "↩️ 戻る",
        "start_owner": "管理メニューを表示しました。X/Twitter の投稿URLを直接送信することもできます。",
        "access": "このアカウントはまだ許可されていません。",
        "apply": "利用を申請",
        "apply_created": "申請を送信しました。",
        "apply_pending": "申請は審査待ちです。",
        "apply_allowed": "すでに利用が許可されています。",
        "apply_auto_approved": "申請が承認されました。利用を開始できます。",
        "service_paused": "利用を一時停止しています。しばらくしてから再試行してください。",
        "invalid_url": "有効な X/Twitter の単一投稿URLを送信してください。",
        "queue_full": "ただいま混み合っています。しばらくしてから再試行してください。",
        "quota": "本日の利用回数が設定された上限に達しました。しばらくしてから再試行するか、管理者にお問い合わせください。",
        "approved": "申請が承認されました。利用を開始できます。",
        "failed": "処理に失敗しました。しばらくしてから再試行してください。",
        "post_unavailable": "投稿を取得できません。公開されているか確認するか、しばらくしてから再試行してください。",
        "url_only": "有効な X/Twitter の単一投稿URLを送信してください。",
        "inline_apply": "Botを開いて利用を申請",
        "video_oversized": "{count} 件の動画が Telegram の 50 MB 上限を超えたため、スキップしました。",
        "images_skipped": "一部の画像はサイズが大きすぎるため、スキップしました。",
        "preview_failed": "メディアのプレビューを送信できませんでした。しばらくしてから再試行してください。",
        "originals_failed": "一部の元ファイルを送信できませんでした。しばらくしてから再試行してください。",
        "language_set": "表示言語を日本語に変更しました。",
        "unsupported_language": "この言語は対応していません。",
    },
}


def public_text(language: str, key: str, **values: Any) -> str:
    selected = language if language in PUBLIC_TEXT else "zh"
    return PUBLIC_TEXT[selected][key].format(**values)


def public_help_text(language: str) -> str:
    language = language if language in PUBLIC_TEXT else "zh"
    parsed = urlparse(OWNER_CONTACT_URL)
    help_text = public_text(language, "help_allowed")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return help_text
    label = OWNER_CONTACT_LABEL or {"zh": "聯絡管理員", "ja": "管理者に連絡", "en": "Contact the owner", "zh-cn": ("联络管理员")}[language]
    return help_text + "\n\n" + f'<a href="{html.escape(OWNER_CONTACT_URL, quote=True)}">{html.escape(label)}</a>'


# Every account keeps its own language; request-local context isolates workers.
ADMIN_TEXT = {
    "menu": ("管理選單", "Management menu", "管理メニュー", "管理菜单"),
    "menu_ready": ("管理選單已載入。", "Management menu loaded.", "管理メニューを表示しました。", "管理菜单已加载。"),
    "users": ("用戶管理", "User management", "ユーザー管理", "用户管理"),
    "user_list": ("用戶列表", "Users", "ユーザー一覧", "用户列表"),
    "requests": ("申請審批", "Access requests", "利用申請", "申请审批"),
    "permissions": ("用戶權限修改", "Change access", "権限を変更", "用户权限修改"),
    "cookies": ("Cookies 管理", "Cookies management", "Cookies 管理", "Cookies 管理"),
    "cookie_import": ("匯入 Cookies", "Import Cookies", "Cookies を取り込む", "导入 Cookies"),
    "cookie_help": ("Cookies 說明", "Cookies guide", "Cookies の説明", "Cookies 说明"),
    "cookie_clear": ("清除 Cookies", "Clear Cookies", "Cookies を削除", "清除 Cookies"),
    "status": ("系統狀態", "System status", "システム状態", "系统状态"),
    "status_runtime": (
        "服務：{service}\n運行時間：{uptime}\n處理佇列：{queue_size}/{queue_limit}（{queue_percent}%）\n\n",
        "Service: {service}\nUptime: {uptime}\nQueue: {queue_size}/{queue_limit} ({queue_percent}%)\n\n",
        "サービス：{service}\n稼働時間：{uptime}\n待機列：{queue_size}/{queue_limit}（{queue_percent}%）\n\n",
        "服务：{service}\n运行时间：{uptime}\n处理队列：{queue_size}/{queue_limit}（{queue_percent}%）\n\n",
    ),
    "status_users": (
        "用戶\n總記錄：{total}\n普通：{ordinary}\n管理員：{administrators}\n初始化：{initialized}\n待審批：{pending}\n封鎖：{banned}\n今日活躍：{active_today}\n今日用量：{interactions}\n已達額度：{exhausted}\n",
        "Users\nRecords: {total}\nRegular: {ordinary}\nAdministrators: {administrators}\nInitialized: {initialized}\nPending: {pending}\nBlocked: {banned}\nActive today: {active_today}\nUsage today: {interactions}\nAt quota: {exhausted}\n",
        "ユーザー\n記録：{total}\n一般：{ordinary}\n管理者：{administrators}\n初期化：{initialized}\n審査待ち：{pending}\nブロック：{banned}\n本日の利用者：{active_today}\n本日の使用量：{interactions}\n上限到達：{exhausted}\n",
        "用户\n总记录：{total}\n普通：{ordinary}\n管理员：{administrators}\n初始化：{initialized}\n待审批：{pending}\n封禁：{banned}\n今日活跃：{active_today}\n今日用量：{interactions}\n已达额度：{exhausted}\n",
    ),
    "running": ("正常", "Running", "稼働中", "正常"),
    "worker_stopped": ("工作執行緒未運行", "Worker stopped", "ワーカー停止", "工作线程未运行"),
    "help": ("使用說明", "Help", "使い方", "使用说明"),
    "advanced": ("高級選項", "Advanced settings", "詳細設定", "高级选项"),
    "implementation": ("實現方式", "Implementation details", "実装方法", "实现方式"),
    "access_switch": ("使用開關", "User access", "ユーザー利用", "使用开关"),
    "auto_approve": ("自動通過", "Auto-approve", "自動承認", "自动通过"),
    "cookie_switch": ("Cookies 使用", "Use Cookies", "Cookies の使用", "Cookies 使用"),
    "on": ("開啟", "On", "オン", "开启"),
    "off": ("關閉", "Off", "オフ", "关闭"),
    "open": ("開放", "Open", "許可", "开放"),
    "paused": ("暫停", "Paused", "停止", "暂停"),
    "back": ("返回", "Back", "戻る", "返回"),
    "refresh": ("重新整理", "Refresh", "更新", "重新整理"),
    "previous": ("上一頁", "Previous", "前へ", "上一页"),
    "next": ("下一頁", "Next", "次へ", "下一页"),
    "approve": ("通過", "Approve", "承認", "通过"),
    "deny": ("拒絕", "Reject", "拒否", "拒绝"),
    "approve_page": ("通過本頁全部", "Approve this page", "このページをすべて承認", "通过本页全部"),
    "owner": ("所有者", "Owner", "所有者", "所有者"),
    "admin": ("管理員", "Administrator", "管理者", "管理员"),
    "ordinary": ("普通", "Regular", "一般", "普通"),
    "blocked": ("封鎖", "Blocked", "ブロック", "封禁"),
    "pending": ("待審批", "Pending", "審査待ち", "待审批"),
    "initialized": ("初始化", "Initialized", "初期化", "初始化"),
    "unlimited": ("不限", "Unlimited", "無制限", "不限"),
    "not_named": ("（未提供名稱）", "(No name)", "（名前なし）", "（未提供名称）"),
    "confirm_create": ("確認建立", "Create user", "ユーザーを作成", "确认建立"),
    "confirm_change": ("確認修改", "Confirm change", "変更を確定", "确认修改"),
    "cancel": ("取消", "Cancel", "キャンセル", "取消"),
    "owner_account": ("所有者帳號", "Owner account", "所有者アカウント", "所有者账号"),
    "admin_read_only": ("管理員帳號（唯讀）", "Administrator (read-only)", "管理者（閲覧のみ）", "管理员账号（唯读）"),
    "default_quota": ("預設額度 ({limit})", "Default limit ({limit})", "標準上限 ({limit})", "预设额度 ({limit})"),
    "default_limit": ("預設額度", "Default limit", "標準上限", "预设额度"),
    "default_limit_prompt": (
        "目前預設額度：{limit}。\n請輸入新的每日額度（1–100000）。",
        "Current default limit: {limit}.\nEnter a new daily limit (1–100000).",
        "現在の標準上限：{limit}。\n新しい1日の上限（1～100000）を入力してください。",
        "目前预设额度：{limit}。\n请输入新的每日额度（1–100000）。",
    ),
    "default_limit_confirm": (
        "確認將預設額度改為 {limit}？\n只更新沿用原預設額度的正常普通用戶；其他額度與權限不變。新用戶使用新值，今日用量不變。",
        "Set the default limit to {limit}?\nOnly approved regular users matching the previous default are updated; other quotas and access stay unchanged. New users use the new value; today's usage stays unchanged.",
        "標準上限を {limit} に変更しますか？\n変更前の標準上限と同じ上限の承認済み一般ユーザーのみ更新します。他の上限・権限と本日の使用量は変えず、新規ユーザーには新しい値を使います。",
        "确认将预设额度改为 {limit}？\n只更新沿用原预设额度的正常普通用户；其他额度与权限不变。新用户使用新值，今日用量不变。",
    ),
    "default_limit_changed": (
        "預設額度已設為 {limit}，已更新 {count} 位普通用戶。",
        "Default limit set to {limit}; updated {count} regular users.",
        "標準上限を {limit} に設定し、一般ユーザー {count} 人を更新しました。",
        "预设额度已设为 {limit}，已更新 {count} 位普通用户。",
    ),
    "default_limit_range": ("預設額度必須是 1 至 100000 的整數。", "The default limit must be an integer from 1 to 100000.", "標準上限は 1～100000 の整数にしてください。", "预设额度必须是 1 至 100000 的整数。"),
    "admin_unlimited": ("不限・管理員", "Unlimited · Administrator", "無制限・管理者", "不限・管理员"),
    "no_requests": ("目前沒有待審批申請。", "No pending requests.", "審査待ちの申請はありません。", "目前没有待审批申请。"),
    "find_user": ("請輸入要修改權限的 Telegram User ID。", "Enter the Telegram User ID to manage.", "権限を変更する Telegram User ID を入力してください。", "请输入要修改权限的 Telegram User ID。"),
    "invalid_menu": ("無效選單。", "Invalid menu.", "無効なメニューです。", "无效菜单。"),
    "invalid_action": ("無效操作。", "Invalid action.", "無効な操作です。", "无效操作。"),
    "private_only": ("管理功能只允許在 Bot 私聊使用。", "Management is available only in a private chat with the bot.", "管理機能は Bot との個別チャットでのみ利用できます。", "管理功能只允许在 Bot 私聊使用。"),
    "owner_only_cookies": ("Cookies 只允許所有者管理。", "Only the owner can manage Cookies.", "Cookies を管理できるのは所有者のみです。", "Cookies 只允许所有者管理。"),
    "admin_only": ("只有管理員可以執行此操作。", "Only administrators can do this.", "この操作は管理者のみ実行できます。", "只有管理员可以执行此操作。"),
    "menu_invalid_page": ("頁碼無效。", "Invalid page number.", "ページ番号が無効です。", "页码无效。"),
    "requests_changed": ("申請列表已變更，請確認後再操作。", "Requests changed. Review the updated list first.", "申請一覧が変わりました。確認してから操作してください。", "申请列表已变更，请确认后再操作。"),
    "cancelled": ("已取消。", "Cancelled.", "キャンセルしました。", "已取消。"),
    "create_cancelled": ("已取消建立用戶。", "User creation cancelled.", "ユーザー作成を中止しました。", "已取消建立用户。"),
    "change_cancelled": ("已取消修改權限。", "Access change cancelled.", "権限の変更を中止しました。", "已取消修改权限。"),
    "quota_missing": ("缺少額度。", "Missing limit.", "上限値がありません。", "缺少额度。"),
    "default_quota_missing": ("缺少預設額度。", "Missing default limit.", "初期上限値がありません。", "缺少预设额度。"),
    "quota_invalid": ("額度格式無效。", "Invalid limit.", "上限値が無効です。", "额度格式无效。"),
    "default_quota_invalid": ("預設額度無效。", "Invalid default limit.", "初期上限値が無効です。", "预设额度无效。"),
    "permission_missing": ("缺少權限值。", "Missing access value.", "権限値がありません。", "缺少权限值。"),
    "permission_invalid": ("權限值無效。", "Invalid access value.", "権限値が無効です。", "权限值无效。"),
    "quota_range": ("額度必須是 -1、0 或 1 至 100000。", "The limit must be -1, 0, or 1–100000.", "上限値は -1、0、または 1～100000 にしてください。", "额度必须是 -1、0 或 1 至 100000。"),
    "user_created": ("用戶已建立。", "User created.", "ユーザーを作成しました。", "用户已建立。"),
    "user_exists": ("該 User ID 已存在，未覆寫現有資料。", "That User ID already exists; no data was overwritten.", "その User ID は既に存在します。データは上書きしていません。", "该 User ID 已存在，未覆写现有数据。"),
    "user_missing": ("用戶已不存在，未進行修改。", "User no longer exists; nothing changed.", "ユーザーが存在しません。変更はしていません。", "用户已不存在，未进行修改。"),
    "permission_changed": ("權限已修改。", "Access changed.", "権限を変更しました。", "权限已修改。"),
    "id_invalid": ("User ID 範圍無效。", "User ID is out of range.", "User ID が有効範囲外です。", "User ID 范围无效。"),
    "access_owner_only": ("使用開關只允許所有者切換。", "Only the owner can change user access.", "ユーザー利用の切り替えは所有者のみ可能です。", "使用开关只允许所有者切换。"),
    "auto_owner_only": ("自動通過只允許所有者切換。", "Only the owner can change auto-approval.", "自動承認の切り替えは所有者のみ可能です。", "自动通过只允许所有者切换。"),
    "cookie_owner_only": ("Cookies 開關只允許所有者切換。", "Only the owner can change the Cookies setting.", "Cookies 設定の切り替えは所有者のみ可能です。", "Cookies 开关只允许所有者切换。"),
    "cookie_cleared": ("X/Twitter Cookies 已清除。", "X/Twitter Cookies cleared.", "X/Twitter の Cookies を削除しました。", "X/Twitter Cookies 已清除。"),
    "approve_missing": ("申請不存在或用戶已被封鎖。", "Request not found or user blocked.", "申請がないか、ユーザーがブロックされています。", "申请不存在或用户已被封禁。"),
    "choose_access": ("選擇用戶權限。", "Choose access.", "権限を選択してください。", "选择用户权限。"),
    "owner_cannot_ban": ("不能封鎖所有者。", "The owner cannot be blocked.", "所有者はブロックできません。", "不能封禁所有者。"),
    "owner_cannot_change": ("不能修改所有者。", "The owner cannot be changed.", "所有者は変更できません。", "不能修改所有者。"),
    "admin_cannot_self": ("管理員不能修改自己的權限。", "Administrators cannot change their own access.", "管理者は自分の権限を変更できません。", "管理员不能修改自己的权限。"),
    "admin_cannot_admin": ("管理員不能修改其他管理員。", "Administrators cannot change other administrators.", "管理者は他の管理者を変更できません。", "管理员不能修改其他管理员。"),
    "owner_only_admin": ("只有所有者可以新增管理員。", "Only the owner can add administrators.", "管理者の追加は所有者のみ可能です。", "只有所有者可以新增管理员。"),
    "advanced_owner_only": ("高級選項只允許所有者使用。", "Only the owner can use Advanced settings.", "詳細設定を使用できるのは所有者のみです。", "高级选项只允许所有者使用。"),
}


def admin_text(key: str) -> str:
    return ADMIN_TEXT[key][{"zh": 0, "en": 1, "ja": 2, "zh-cn": 3}[ui_language()]]

URL_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/(\d+)(?:[^\s]*)?",
    re.IGNORECASE,
)


def is_telegram_user_id_shortcut(value: int) -> bool:
    """Use a non-overlapping heuristic for owner numeric shortcuts."""
    return MIN_TELEGRAM_USER_ID_SHORTCUT <= value <= MAX_TELEGRAM_USER_ID


def is_telegram_user_id(value: int) -> bool:
    return 1 <= value <= MAX_TELEGRAM_USER_ID

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger(APP_NAME)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.in_paragraph = False

    def handle_starttag(
        self, tag: str, _attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "p":
            self.in_paragraph = True
        elif tag == "br" and self.in_paragraph:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self.in_paragraph = False

    def handle_data(self, data: str) -> None:
        if not self.in_paragraph:
            return
        value = data.strip()
        if value:
            self.parts.append(value)


class ACLStore:
    def __init__(self, path: Path, env_owner_id: int = 0) -> None:
        self.path = path
        self.backup_path = path.with_name(path.name + ".bak")
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "owner_id": 0,
            "users": {},
            "pending_applications": {},
            "last_daily_report_date": "",
            "external_access_enabled": True,
            "ordinary_user_cookies_enabled": True,
            "auto_approve_enabled": False,
            "pending_jobs": {},
        }
        self._load()
        if env_owner_id and not self.data.get("owner_id"):
            self.data["owner_id"] = env_owner_id
            record = self.data["users"].setdefault(
                str(env_owner_id), {"user_id": env_owner_id}
            )
            record["quota"] = None
            record["quota_updated_at"] = self._quota_timestamp()
            self._save()

    def _apply_state(self, raw: dict[str, Any], migration_timestamp: int) -> None:
        if not isinstance(raw, dict):
            raise ValueError("ACL state must be an object")
        users = raw.get("users", {})
        pending = raw.get("pending_applications", {})
        if not isinstance(users, dict) or not isinstance(pending, dict):
            raise ValueError("ACL users and pending applications must be objects")
        for setting in ("external_access_enabled", "ordinary_user_cookies_enabled",
                        "auto_approve_enabled", "debug_mode"):
            if setting in raw and type(raw[setting]) is not bool:
                raise ValueError(f"invalid ACL boolean: {setting}")
        data: dict[str, Any] = {
            "owner_id": int(raw.get("owner_id", 0) or 0),
            "users": users,
            "pending_applications": pending,
            "last_daily_report_date": str(raw.get("last_daily_report_date") or ""),
            "external_access_enabled": bool(raw.get("external_access_enabled", True)),
            "ordinary_user_cookies_enabled": bool(
                raw.get("ordinary_user_cookies_enabled", True)
            ),
            "auto_approve_enabled": bool(raw.get("auto_approve_enabled", False)),
            "pending_jobs": raw.get("pending_jobs", {}),
        }
        if "default_daily_limit" in raw:
            limit = raw["default_daily_limit"]
            if type(limit) is not int or not 1 <= limit <= MAX_DAILY_LIMIT:
                raise ValueError("invalid default daily limit")
            data["default_daily_limit"] = limit
            updated_at = raw.get("default_daily_limit_updated_at", migration_timestamp)
            if type(updated_at) is not int or updated_at < 0:
                raise ValueError("invalid default daily limit timestamp")
            data["default_daily_limit_updated_at"] = updated_at
        elif "default_daily_limit_updated_at" in raw:
            raise ValueError("default daily limit timestamp without a limit")
        legacy_allowed = {int(value) for value in raw.get("allowed_user_ids", [])}
        legacy_banned = {int(value) for value in raw.get("banned_user_ids", [])}
        known_ids = (
            legacy_allowed
            | legacy_banned
            | {int(value) for value in pending}
            | {int(value) for value in users}
        )
        if data["owner_id"]:
            known_ids.add(data["owner_id"])
        for user_id in known_ids:
            if not is_telegram_user_id(user_id):
                raise ValueError("ACL contains an invalid Telegram user ID")
            record = users.setdefault(str(user_id), {"user_id": user_id})
            if not isinstance(record, dict):
                raise ValueError("ACL user record must be an object")
            if "debug_mode" in record and type(record["debug_mode"]) is not bool:
                raise ValueError("invalid ACL user debug mode")
            record.pop("management_mode", None)
            record["user_id"] = user_id
            if user_id == data["owner_id"]:
                record["quota"] = None
            elif "quota" not in record:
                if user_id in legacy_banned:
                    record["quota"] = -1
                elif user_id in legacy_allowed:
                    legacy_limit = int(
                        record.get("daily_limit", DEFAULT_DAILY_LIMIT) or 0
                    )
                    record["quota"] = None if legacy_limit == 0 else legacy_limit
                else:
                    record["quota"] = 0
            record.setdefault("quota_updated_at", migration_timestamp)
            record.pop("daily_limit", None)
            if user_id == data["owner_id"]:
                record.setdefault("debug_mode", bool(raw.get("debug_mode", False)))
        jobs = data["pending_jobs"]
        if not isinstance(jobs, dict):
            raise ValueError("invalid pending jobs")
        for key, job in jobs.items():
            if (not isinstance(job, list) or len(job) != 4
                    or any(type(value) is not int for value in job[:3])
                    or not is_telegram_user_id(job[2])
                    or key != f"{job[0]}:{job[1]}"
                    or not isinstance(job[3], str)
                    or normalize_status_url(job[3]) != job[3]):
                raise ValueError("invalid pending job")
        self.data = data

    @classmethod
    def read_access_snapshot(cls, path: Path, default_daily_limit: int | None = None) -> dict[str, Any]:
        # Read exactly one atomic file generation; never migrate or write state.
        store = cls.__new__(cls)
        with path.open(encoding="utf-8") as source:
            store._apply_state(json.load(source), os.fstat(source.fileno()).st_mtime_ns)
        return store.export_access(default_daily_limit)

    def finish_job(self, chat_id: int, message_id: int) -> None:
        with self.lock:
            self.data["pending_jobs"].pop(f"{chat_id}:{message_id}", None)
            self._save()

    def _load(self) -> None:
        if not self.path.exists() and not self.backup_path.exists():
            return
        main_error: Exception | None = None
        for candidate in (self.path, self.backup_path):
            if not candidate.exists():
                continue
            try:
                migration_timestamp = candidate.stat().st_mtime_ns
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                self._apply_state(raw, migration_timestamp)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                if candidate == self.path:
                    main_error = error
                    LOG.exception("Could not load primary ACL state; trying backup")
                else:
                    LOG.exception("Could not load ACL backup")
                continue
            if candidate == self.backup_path:
                if self.path.exists():
                    corrupt = self.path.with_name(
                        f"{self.path.name}.corrupt-{time.time_ns()}"
                    )
                    self.path.replace(corrupt)
                LOG.error("Recovered ACL state from backup")
            self._save()
            return
        raise RuntimeError("ACL state and backup are invalid; refusing to overwrite them") from main_error

    def _save(self) -> None:
        if self.path.exists():
            try:
                current = self.path.read_text(encoding="utf-8")
                previous = json.loads(current)
                if not isinstance(previous, dict):
                    raise ValueError("ACL state must be an object")
                if any(not isinstance(previous.get(key, {}), dict)
                       for key in ("users", "pending_applications")):
                    raise ValueError("ACL users and pending applications must be objects")
                atomic_write_text(self.backup_path, current)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                LOG.exception("Refusing to replace the ACL backup with invalid state")
        atomic_write_text(
            self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        )
        if getattr(os, "geteuid", lambda: -1)() == 0:
            shutil.chown(self.path, user="x-tweet-bot", group="x-tweet-bot")
            if self.backup_path.exists():
                shutil.chown(self.backup_path, user="x-tweet-bot", group="x-tweet-bot")

    @staticmethod
    def _quota_timestamp() -> int:
        return time.time_ns()

    def export_access(self, default_daily_limit: int | None = None) -> dict[str, Any]:
        limit = self.data.get("default_daily_limit", self.default_daily_limit if default_daily_limit is None else default_daily_limit)
        if type(limit) is not int or not 1 <= limit <= MAX_DAILY_LIMIT:
            raise ValueError("invalid default daily limit")
        users = []
        for key, record in self.data["users"].items():
            user_id = int(key)
            if not is_telegram_user_id(user_id):
                continue
            quota = self.quota(user_id)
            users.append(
                {
                    "user_id": user_id,
                    "quota": quota,
                    "updated_at": int(record.get("quota_updated_at", 0) or 0),
                }
            )
        return {
            "version": 1,
            "users": sorted(users, key=lambda item: item["user_id"]),
            "default_daily_limit": limit,
            "default_daily_limit_updated_at": self.data.get("default_daily_limit_updated_at", 0),
        }

    def import_access(self, snapshot: dict[str, Any]) -> int:
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            raise ValueError("unsupported access snapshot")
        users = snapshot.get("users")
        if not isinstance(users, list):
            raise ValueError("access snapshot users must be a list")
        has_default = "default_daily_limit" in snapshot
        default_limit = snapshot.get("default_daily_limit")
        default_updated_at = snapshot.get("default_daily_limit_updated_at", 0)
        if has_default:
            if type(default_limit) is not int or not 1 <= default_limit <= MAX_DAILY_LIMIT:
                raise ValueError("invalid default daily limit")
            if type(default_updated_at) is not int or default_updated_at < 0:
                raise ValueError("invalid default daily limit timestamp")
        elif "default_daily_limit_updated_at" in snapshot:
            raise ValueError("default daily limit timestamp without a limit")
        validated = []
        for item in users:
            if not isinstance(item, dict):
                raise ValueError("invalid access snapshot record")
            user_id = int(item.get("user_id", 0) or 0)
            if not is_telegram_user_id(user_id):
                raise ValueError("invalid Telegram user ID")
            quota = item.get("quota")
            if quota is not None:
                quota = int(quota)
                if quota < -1 or quota > MAX_DAILY_LIMIT:
                    raise ValueError("quota out of range")
            updated_at = int(item.get("updated_at", 0) or 0)
            if updated_at <= 0:
                raise ValueError("invalid quota update timestamp")
            validated.append((user_id, quota, updated_at))

        changed = 0
        dirty = False
        with self.lock:
            if has_default and (
                "default_daily_limit" not in self.data
                or default_updated_at > self.data.get("default_daily_limit_updated_at", 0)
            ):
                self.data["default_daily_limit"] = default_limit
                self.data["default_daily_limit_updated_at"] = default_updated_at
                dirty = True
            for user_id, quota, updated_at in validated:
                key = str(user_id)
                record = self.data["users"].get(key)
                local_updated_at = int(
                    (record or {}).get("quota_updated_at", 0) or 0
                )
                if record is not None and updated_at <= local_updated_at:
                    continue
                if record is not None and self.quota(user_id) == quota:
                    record["quota_updated_at"] = updated_at
                    dirty = True
                    continue
                record = self.data["users"].setdefault(key, {"user_id": user_id})
                record["user_id"] = user_id
                record["quota"] = quota
                record["quota_updated_at"] = updated_at
                record.pop("daily_limit", None)
                if quota is None or (isinstance(quota, int) and quota != 0):
                    self.data["pending_applications"].pop(key, None)
                changed += 1
                dirty = True
            if dirty:
                self._save()
        return changed

    @property
    def owner_id(self) -> int:
        return int(self.data.get("owner_id", 0) or 0)

    @property
    def external_access_enabled(self) -> bool:
        return bool(self.data.get("external_access_enabled", True))

    @property
    def ordinary_user_cookies_enabled(self) -> bool:
        return bool(self.data.get("ordinary_user_cookies_enabled", True))

    @property
    def auto_approve_enabled(self) -> bool:
        return bool(self.data.get("auto_approve_enabled", False))

    @property
    def default_daily_limit(self) -> int:
        return self.data.get("default_daily_limit", DEFAULT_DAILY_LIMIT)

    def set_default_daily_limit(self, actor_id: int, limit: int) -> int:
        self._require_owner(actor_id)
        if type(limit) is not int or not 1 <= limit <= MAX_DAILY_LIMIT:
            raise ValueError("default daily limit out of range")
        with self.lock:
            previous_limit = self.default_daily_limit
            self.data["default_daily_limit"] = limit
            changed = 0
            timestamp = self._quota_timestamp()
            if previous_limit != limit or "default_daily_limit_updated_at" not in self.data:
                self.data["default_daily_limit_updated_at"] = timestamp
            for key, record in self.data["users"].items():
                quota = self.quota(int(key))
                if (quota != previous_limit or quota == limit
                        or key in self.data["pending_applications"]):
                    continue
                record["quota"] = limit
                record["quota_updated_at"] = timestamp
                changed += 1
            self._save()
            return changed

    def debug_mode(self, user_id: int) -> bool:
        record = self.data["users"].get(str(user_id)) or {}
        return user_id == self.owner_id and bool(record.get("debug_mode", False))

    def toggle_debug_mode(self, user_id: int) -> bool:
        if user_id != self.owner_id:
            raise ValueError("only the owner can change implementation details")
        with self.lock:
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id, "quota": None}
            )
            enabled = not bool(record.get("debug_mode", False))
            record["debug_mode"] = enabled
            self._save()
            return enabled

    def _require_owner(self, actor_id: int) -> None:
        if actor_id != self.owner_id:
            raise ValueError("only the owner can change global settings")

    def toggle_external_access(self, actor_id: int) -> bool:
        self._require_owner(actor_id)
        with self.lock:
            enabled = not bool(self.data.get("external_access_enabled", True))
            self.data["external_access_enabled"] = enabled
            self._save()
            return enabled

    def toggle_ordinary_user_cookies(self, actor_id: int) -> bool:
        self._require_owner(actor_id)
        with self.lock:
            enabled = not bool(
                self.data.get("ordinary_user_cookies_enabled", True)
            )
            self.data["ordinary_user_cookies_enabled"] = enabled
            self._save()
            return enabled

    def toggle_auto_approve(self, actor_id: int) -> bool:
        self._require_owner(actor_id)
        with self.lock:
            enabled = not bool(self.data.get("auto_approve_enabled", False))
            self.data["auto_approve_enabled"] = enabled
            self._save()
            return enabled

    def claim(self, user_id: int, code: str) -> bool:
        with self.lock:
            if self.owner_id or not BOOTSTRAP_CODE or code != BOOTSTRAP_CODE:
                return False
            self.data["owner_id"] = user_id
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id}
            )
            record["quota"] = None
            record["quota_updated_at"] = self._quota_timestamp()
            self._save()
            return True

    def quota(self, user_id: int) -> int | None:
        if user_id == self.owner_id:
            return None
        record = self.data["users"].get(str(user_id)) or {}
        value = record.get("quota", 0)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.owner_id or self.quota(user_id) is None

    def is_allowed(self, user_id: int) -> bool:
        quota = self.quota(user_id)
        return user_id == self.owner_id or quota is None or quota > 0

    def is_banned(self, user_id: int) -> bool:
        return user_id != self.owner_id and self.quota(user_id) == -1

    def language(self, user_id: int) -> str:
        record = self.data["users"].get(str(user_id)) or {}
        language = str(record.get("language") or "zh")
        return language if language in PUBLIC_TEXT else "zh"

    def set_language(self, user_id: int, language: str) -> None:
        if language not in PUBLIC_TEXT:
            raise ValueError("unsupported language")
        with self.lock:
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id}
            )
            record["language"] = language
            self._save()

    def observe(self, sender: dict[str, Any], now: float | None = None) -> bool:
        user_id = int(sender.get("id", 0) or 0)
        if not user_id:
            return False
        current = time.time() if now is None else now
        profile_date = bot_date(current)
        with self.lock:
            key = str(user_id)
            record = self.data["users"].setdefault(key, {})
            if record.get("profile_checked_date") == profile_date:
                return False
            profile = {
                "user_id": user_id,
                "username": str(sender.get("username") or ""),
                "first_name": str(sender.get("first_name") or ""),
                "last_name": str(sender.get("last_name") or ""),
            }
            changed = any(record.get(field) != value for field, value in profile.items())
            record.update(profile)
            record["profile_checked_date"] = profile_date
            record["last_seen"] = int(current)
            record.setdefault("quota", None if user_id == self.owner_id else 0)
            record.setdefault("quota_updated_at", self._quota_timestamp())
            record.setdefault("usage_date", "")
            record.setdefault("usage_count", 0)
            self._save()
            return changed

    def add(self, user_id: int) -> None:
        self.set_quota(user_id, self.default_daily_limit)

    def remove(self, user_id: int) -> None:
        self.set_quota(user_id, 0)

    def has_user(self, user_id: int) -> bool:
        with self.lock:
            return str(user_id) in self.data["users"]

    def ensure_managed_user(
        self, user_id: int, initial_quota: Any = ...
    ) -> bool:
        if user_id <= 0:
            raise ValueError("user id must be positive")
        with self.lock:
            key = str(user_id)
            if key in self.data["users"]:
                return False
            if initial_quota is Ellipsis:
                initial_quota = self.default_daily_limit
            self.data["users"][key] = {
                "user_id": user_id,
                "quota": initial_quota,
                "quota_updated_at": self._quota_timestamp(),
                "usage_date": "",
                "usage_count": 0,
            }
            self.data["pending_applications"].pop(key, None)
            self._save()
            return True

    def request_access(self, user_id: int, now: float | None = None) -> str:
        current = time.time() if now is None else now
        with self.lock:
            if self.is_banned(user_id):
                return "banned"
            if self.is_allowed(user_id):
                return "allowed"
            key = str(user_id)
            if self.auto_approve_enabled:
                record = self.data["users"].setdefault(key, {"user_id": user_id})
                record["quota"] = self.default_daily_limit
                record["quota_updated_at"] = self._quota_timestamp()
                self.data["pending_applications"].pop(key, None)
                self._save()
                return "auto_approved"
            if key in self.data["pending_applications"]:
                return "pending"
            record = self.data["users"].setdefault(key, {"user_id": user_id})
            record.setdefault("quota", 0)
            record.setdefault("quota_updated_at", self._quota_timestamp())
            self.data["pending_applications"][key] = {"requested_at": current}
            self._save()
            return "created"

    def approve(self, user_id: int, requested_at: float | None = None) -> bool:
        with self.lock:
            if self.is_banned(user_id):
                return False
            key = str(user_id)
            if key not in self.data["pending_applications"] or self.is_admin(user_id):
                return False
            if (requested_at is not None and requested_at !=
                    self.data["pending_applications"][key].get("requested_at")):
                return False
            self.data["pending_applications"].pop(key, None)
            record = self.data["users"].setdefault(key, {"user_id": user_id})
            record["quota"] = self.default_daily_limit
            record["quota_updated_at"] = self._quota_timestamp()
            self._save()
            return True

    def deny(self, user_id: int, requested_at: float | None = None) -> bool:
        with self.lock:
            application = self.data["pending_applications"].get(str(user_id))
            if (requested_at is not None and (not application or
                    requested_at != application.get("requested_at"))):
                return False
            removed = self.data["pending_applications"].pop(str(user_id), None)
            self._save()
            return removed is not None

    def ban(self, user_id: int) -> None:
        if user_id == self.owner_id:
            raise ValueError("owner cannot be banned")
        self.set_quota(user_id, -1)

    def unban(self, user_id: int) -> None:
        self.set_quota(user_id, 0)

    def pending(self) -> list[dict[str, Any]]:
        results = []
        for key, application in self.data["pending_applications"].items():
            record = dict(self.data["users"].get(key) or {})
            record["user_id"] = int(key)
            record["requested_at"] = float(application.get("requested_at", 0) or 0)
            results.append(record)
        return sorted(results, key=lambda item: item["requested_at"])

    def daily_report_due(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        local_now = datetime.fromtimestamp(current, BOT_TIMEZONE)
        return (
            local_now.hour >= DAILY_REPORT_HOUR
            and self.data.get("last_daily_report_date")
            != local_now.strftime("%Y-%m-%d")
        )

    def mark_daily_report(self, now: float | None = None) -> None:
        with self.lock:
            current = time.time() if now is None else now
            self.data["last_daily_report_date"] = datetime.fromtimestamp(
                current, BOT_TIMEZONE
            ).strftime("%Y-%m-%d")
            self._save()

    def usage_summary(self, usage_date: str) -> tuple[int, int]:
        active = total = 0
        with self.lock:
            for record in self.data["users"].values():
                count = 0
                if record.get("usage_date") == usage_date:
                    count = int(record.get("usage_count", 0) or 0)
                active += int(count > 0)
                total += count
        return active, total

    def set_quota(self, user_id: int, quota: int | None) -> None:
        if user_id == self.owner_id:
            raise ValueError("owner quota cannot be changed")
        if quota is not None and (quota < -1 or quota > MAX_DAILY_LIMIT):
            raise ValueError("quota out of range")
        with self.lock:
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id}
            )
            record["quota"] = quota
            record["quota_updated_at"] = self._quota_timestamp()
            record.pop("daily_limit", None)
            self.data["pending_applications"].pop(str(user_id), None)
            self._save()

    def consume(self, user_id: int, now: float | None = None,
                job: tuple[int, int, int, str] | None = None) -> tuple[bool, int, int]:
        current = time.time() if now is None else now
        usage_date = bot_date(current)
        with self.lock:
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id}
            )
            quota = self.quota(user_id)
            limit = 0 if quota is None else max(0, quota)
            if quota is not None and quota <= 0:
                return False, 0, limit
            if job is not None and f"{job[0]}:{job[1]}" in self.data["pending_jobs"]:
                return False, int(record.get("usage_count", 0)), limit
            if record.get("usage_date") != usage_date:
                record["usage_date"] = usage_date
                record["usage_count"] = 0
            used = int(record.get("usage_count", 0) or 0)
            if quota is not None and quota > 0 and used >= quota:
                return False, used, limit
            used += 1
            record["usage_count"] = used
            if job is not None:
                self.data["pending_jobs"][f"{job[0]}:{job[1]}"] = list(job)
            self._save()
            return True, used, limit

    def records(self) -> list[dict[str, Any]]:
        pending = set(self.data["pending_applications"])
        results = []
        today = bot_date()
        for key, value in self.data["users"].items():
            record = dict(value)
            user_id = int(key)
            quota = self.quota(user_id)
            record["user_id"] = user_id
            record["quota"] = quota
            record["allowed"] = self.is_allowed(user_id)
            record["admin"] = self.is_admin(user_id)
            record["pending"] = key in pending
            record["banned"] = self.is_banned(user_id)
            record.setdefault("usage_count", 0)
            if record.get("usage_date") != today:
                record["usage_count"] = 0
            results.append(record)
        def access_order(item: dict[str, Any]) -> tuple[int, int, int, int]:
            user_id = int(item["user_id"])
            quota = item.get("quota")
            if user_id == self.owner_id:
                return 0, 0, 0, user_id
            if quota is None:
                return 1, 0, 0, user_id
            numeric_quota = int(quota or 0)
            if numeric_quota >= 1:
                return 2, -int(item["usage_count"]), -numeric_quota, user_id
            if numeric_quota == 0:
                return 3, 0, 0, user_id
            return 4, 0, 0, user_id

        return sorted(results, key=access_order)


def normalize_status_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    parsed = urlparse(match.group(0))
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    path_match = re.search(r"/([A-Za-z0-9_]+)/status/(\d+)", parsed.path)
    if not path_match:
        return None
    return f"https://x.com/{path_match.group(1)}/status/{path_match.group(2)}"


def owner_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": f'👤 {admin_text("users")}', "callback_data": "nav:users"},
                {
                    "text": public_text(ui_language(), "language_menu"),
                    "callback_data": "public:language",
                },
            ],
            [
                {"text": f'📊 {admin_text("status")}', "callback_data": "nav:status"},
                {"text": f'ℹ️ {admin_text("help")}', "callback_data": "nav:help"},
            ],
        ],
    }


def user_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": f'👥 {admin_text("user_list")}', "callback_data": "nav:userlist"},
                {"text": f'📝 {admin_text("requests")}', "callback_data": "nav:requests"},
            ],
            [{"text": f'🔐 {admin_text("permissions")}', "callback_data": "nav:finduser"}],
            [{"text": f'↩️ {admin_text("back")}{": " if ui_language() == "en" else "："}{admin_text("menu")}', "callback_data": "nav:main"}],
        ],
    }


def cookie_menu_keyboard(
    ordinary_user_cookies_enabled: bool = True,
) -> dict[str, Any]:
    cookie_state = admin_text("on" if ordinary_user_cookies_enabled else "off")
    return {
        "inline_keyboard": [
            [
                {"text": f'🍪 {admin_text("cookie_import")}', "callback_data": "nav:cookieupload"},
                {"text": f'📖 {admin_text("cookie_help")}', "callback_data": "nav:cookiehelp"},
            ],
            [{
                "text": f'🍪 {admin_text("cookie_switch")}{"：" if ui_language() != "en" else ": "}{cookie_state}',
                "callback_data": "ordinarycookiestoggle:0",
            }],
            [{"text": f'🗑 {admin_text("cookie_clear")}', "callback_data": "nav:clearcookies"}],
            [{"text": f'↩️ {admin_text("back")}{": " if ui_language() == "en" else "："}{admin_text("advanced")}', "callback_data": "nav:advanced"}],
        ],
    }


def remove_keyboard() -> dict[str, bool]:
    return {"remove_keyboard": True}


def user_name(record: dict[str, Any]) -> str:
    def clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    name = " ".join(
        value for value in (
            clean(record.get("first_name")),
            clean(record.get("last_name")),
        ) if value
    )
    return name or admin_text("not_named")


def user_label(record: dict[str, Any]) -> str:
    name = user_name(record)
    username = re.sub(r"\s+", " ", str(record.get("username") or "")).strip()
    if username:
        return f"{name} (@{username})"
    return name


def telegram_username(record: dict[str, Any]) -> str:
    username = str(record.get("username") or "").strip().lstrip("@")
    return username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else ""


def language_row(selected: str) -> list[dict[str, str]]:
    return [
        {
            "text": ("✓ " if selected == language else "") + label,
            "callback_data": f"lang:{language}",
        }
        for language, label in (("zh-cn", "简体中文"), ("zh", "繁體中文"), ("en", "English"), ("ja", "日本語"))
    ]


def start_keyboard(
    language: str,
    is_admin: bool,
    is_allowed: bool,
) -> dict[str, Any]:
    rows = []
    if is_admin:
        rows.extend(owner_keyboard()["inline_keyboard"])
    elif not is_allowed:
        rows.append([{
            "text": public_text(language, "apply"),
            "callback_data": "apply",
        }])
    if not is_admin:
        rows.append([
            {"text": public_text(language, "language_menu"), "callback_data": "public:language"},
            {"text": public_text(language, "help_menu"), "callback_data": "public:help"},
        ])
    return {"inline_keyboard": rows}


def application_keyboard(language: str = "zh") -> dict[str, Any]:
    return start_keyboard(language, False, False)


def pagination_row(prefix: str, page: int, total: int) -> list[dict[str, str]]:
    pages = max(1, (total + MANAGEMENT_PAGE_SIZE - 1) // MANAGEMENT_PAGE_SIZE)
    row = []
    if page > 0:
        row.append({"text": f'⬅️ {admin_text("previous")}', "callback_data": f"{prefix}:{page - 1}"})
    row.append({"text": f"{page + 1}/{pages}", "callback_data": "noop:0"})
    if page + 1 < pages:
        row.append({"text": f'{admin_text("next")} ➡️', "callback_data": f"{prefix}:{page + 1}"})
    return row


def pending_page_fingerprint(records: list[dict[str, Any]], page: int) -> str:
    start = page * MANAGEMENT_PAGE_SIZE
    applications = [(record["user_id"], record.get("requested_at", 0))
                    for record in records[start:start + MANAGEMENT_PAGE_SIZE]]
    return hashlib.sha256(json.dumps(applications).encode("ascii")).hexdigest()[:16]


def pending_keyboard(
    records: list[dict[str, Any]], page: int = 0
) -> dict[str, Any]:
    rows = []
    start = page * MANAGEMENT_PAGE_SIZE
    for record in records[start : start + MANAGEMENT_PAGE_SIZE]:
        user_id = int(record["user_id"])
        label = user_label(record)[:24]
        requested_at = record["requested_at"]
        rows.append([
            {"text": f'{admin_text("approve")} {label}', "callback_data": f"approve:{user_id}:{page}:{requested_at}"},
            {"text": admin_text("deny"), "callback_data": f"deny:{user_id}:{page}:{requested_at}"},
        ])
    rows.append([{
        "text": admin_text("approve_page"),
        "callback_data": f"approvepage:{page}:{pending_page_fingerprint(records, page)}",
    }])
    rows.append(pagination_row("requestspage", page, len(records)))
    rows.append([{"text": f'↩️ {admin_text("users")}', "callback_data": "nav:users"}])
    return {"inline_keyboard": rows}


def users_page_keyboard(page: int, total: int) -> dict[str, Any]:
    return {"inline_keyboard": [
        pagination_row("userspage", page, total),
        [{"text": f'↩️ {admin_text("users")}', "callback_data": "nav:users"}],
    ]}


def searched_user_keyboard(
    record: dict[str, Any],
    owner_id: int,
    can_modify: bool = True,
    allow_admin: bool = False,
    default_daily_limit: int = DEFAULT_DAILY_LIMIT,
) -> dict[str, Any]:
    user_id = int(record["user_id"])
    if user_id == owner_id:
        return {"inline_keyboard": [
            [{"text": admin_text("owner_account"), "callback_data": "noop:0"}],
            [{"text": f'↩️ {admin_text("users")}', "callback_data": "nav:users"}],
        ]}
    if can_modify:
        return quota_choices_keyboard(user_id, allow_admin=allow_admin, default_daily_limit=default_daily_limit)
    return {"inline_keyboard": [
        [{"text": admin_text("admin_read_only"), "callback_data": "noop:0"}],
        [{"text": f'↩️ {admin_text("users")}', "callback_data": "nav:users"}],
    ]}


def confirm_new_user_keyboard(user_id: int, quota: int) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{
            "text": admin_text("confirm_create"),
            "callback_data": f"createuser:{user_id}:{quota}",
        }],
        [{"text": admin_text("cancel"), "callback_data": f"cancelcreate:{user_id}"}],
    ]}


def confirm_quota_change_keyboard(user_id: int, quota: int) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{
            "text": admin_text("confirm_change"),
            "callback_data": f"confirmquota:{user_id}:{quota}",
        }],
        [{"text": admin_text("cancel"), "callback_data": f"cancelquota:{user_id}"}],
    ]}


def default_limit_keyboard(limit: int | None = None) -> dict[str, Any]:
    rows = []
    if limit is not None:
        rows.append([{"text": admin_text("confirm_change"), "callback_data": f"defaultquota:{limit}"}])
    rows.append([{"text": admin_text("cancel"), "callback_data": "nav:advanced"}])
    return {"inline_keyboard": rows}


def status_keyboard(is_owner: bool = True) -> dict[str, Any]:
    rows = [[{"text": f'🔄 {admin_text("refresh")}', "callback_data": "statusrefresh:0"}]]
    if is_owner:
        rows.append([{
            "text": f'⚙️ {admin_text("advanced")}',
            "callback_data": "nav:advanced",
        }])
    rows.append([{"text": f'↩️ {admin_text("back")}{": " if ui_language() == "en" else "："}{admin_text("menu")}', "callback_data": "nav:main"}])
    return {"inline_keyboard": rows}


def advanced_status_keyboard(
    debug_mode: bool,
    external_access_enabled: bool = True,
    auto_approve_enabled: bool = False,
    can_configure: bool = True,
    default_daily_limit: int = DEFAULT_DAILY_LIMIT,
) -> dict[str, Any]:
    debug_state = admin_text("on" if debug_mode else "off")
    external_state = admin_text("open" if external_access_enabled else "paused")
    auto_approve_state = admin_text("on" if auto_approve_enabled else "off")
    owner_rows = [
        [{
            "text": f'🍪 {admin_text("cookies")}',
            "callback_data": "nav:cookies",
        }],
        [{
            "text": f'🌐 {admin_text("access_switch")}{"：" if ui_language() != "en" else ": "}{external_state}',
            "callback_data": "externaltoggle:0",
        }],
        [{
            "text": f'✅ {admin_text("auto_approve")}{"：" if ui_language() != "en" else ": "}{auto_approve_state}',
            "callback_data": "autoapprovetoggle:0",
        }],
        [{
            "text": f'🎯 {admin_text("default_limit")}{"：" if ui_language() != "en" else ": "}{default_daily_limit}',
            "callback_data": "nav:defaultquota",
        }],
    ] if can_configure else []
    if can_configure:
        owner_rows.append([{
            "text": f'🐞 {admin_text("implementation")}{"：" if ui_language() != "en" else ": "}{debug_state}',
            "callback_data": "debugtoggle:0",
        }])
    return {"inline_keyboard": [
        *owner_rows,
        [{"text": f'↩️ {admin_text("back")}{": " if ui_language() == "en" else "："}{admin_text("status")}', "callback_data": "nav:status"}],
    ]}


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if ui_language() == "en":
        return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"
    if ui_language() == "ja":
        return f"{days}日 {hours}時間" if days else f"{hours}時間 {minutes}分" if hours else f"{minutes}分"
    if days:
        return {"zh": (f"{days} 天 {hours} 小時"), "zh-cn": (f"{days} 天 {hours} 小时")}[ui_language()]
    if hours:
        return {"zh": (f"{hours} 小時 {minutes} 分"), "zh-cn": (f"{hours} 小时 {minutes} 分")}[ui_language()]
    return {"zh": (f"{minutes} 分"), "zh-cn": (f"{minutes} 分")}[ui_language()]


def quota_choices_keyboard(
    user_id: int, allow_admin: bool = True, default_daily_limit: int = DEFAULT_DAILY_LIMIT
) -> dict[str, Any]:
    rows = [
            [
                {"text": admin_text("default_quota").format(limit=default_daily_limit), "callback_data": f"quota:{user_id}:{default_daily_limit}"},
                {"text": f'{admin_text("blocked")} (-1)', "callback_data": f"quota:{user_id}:blocked"},
                {"text": f'{admin_text("initialized")} (0)', "callback_data": f"quota:{user_id}:0"},
            ],
        ]
    if allow_admin:
        rows.append([
            {"text": admin_text("admin_unlimited"), "callback_data": f"quota:{user_id}:unlimited"},
        ])
    rows.append([{"text": f'↩️ {admin_text("users")}', "callback_data": "nav:users"}])
    return {"inline_keyboard": rows}


def access_request_text(user_id: int, language: str = "zh") -> str:
    return (
        f"Telegram User ID: {user_id}\n"
        f"{public_text(language, 'access')}"
    )


def cookie_upload_text() -> str:
    return {
        "en": "Upload a Netscape-format cookies.txt file (maximum 1 MB). Select Cookies guide if needed. The file passes through Telegram; use /cancel to cancel.",
        "ja": "Netscape 形式の cookies.txt をアップロードしてください（上限 1 MB）。必要なら Cookies の説明を開いてください。ファイルは Telegram を経由します。/cancel で中止できます。",
        "zh-cn": ("请上传 Netscape 格式的 cookies.txt，文件上限 1 MB。\n"
        "不清楚如何获取时，点选「Cookies 说明」。\n"
        "文件会经 Telegram 发送；输入 /cancel 可取消。"),
    }.get(ui_language()) or (
        "請上傳 Netscape 格式的 cookies.txt，檔案上限 1 MB。\n"
        "不清楚如何取得時，點選「Cookies 說明」。\n"
        "文件會經 Telegram 傳送；輸入 /cancel 可取消。"
    )


def cookie_help_text() -> str:
    return {
        "en": "X/Twitter Cookies\n1. Sign in to x.com in a browser, ideally with a dedicated bot account.\n2. Use a trusted tool to export Netscape cookies.txt for x.com/twitter.com only.\n3. Select Import Cookies and upload the file as a document.\n4. After success, delete the original document from Telegram.\n\nCookies are login credentials. Do not export other sites or forward them. Import again if the X session expires.",
        "ja": "X/Twitter の Cookies\n1. ブラウザーで x.com にログインします。Bot 専用アカウントを推奨します。\n2. 信頼できるツールで x.com/twitter.com のみの Netscape 形式 cookies.txt を書き出します。\n3. Cookies を取り込むを選び、ファイルを文書として送信します。\n4. 成功後、Telegram の元ファイルを削除します。\n\nCookies はログイン資格情報です。他のサイトの情報を含めたり、第三者に転送したりしないでください。セッション失効時は再度取り込んでください。",
        "zh-cn": ("获取 X/Twitter Cookies：\n"
        "1. 在浏览器登入 x.com。建议使用专门给 Bot 的独立账号。\n"
        "2. 使用可信任、可导出 Netscape cookies.txt 的浏览器工具，只导出 "
        "x.com／twitter.com 目前网站的 Cookies。\n"
        "3. 点「导入 Cookies」，再把 cookies.txt 当作文件上传。\n"
        "4. Bot 显示成功后，删除 Telegram 对话中的原始文件。\n\n"
        "Cookies 等同登入凭证。不要导出其他网站、不要转传给他人；"
        "若 X 账号登出或工作阶段失效，需重新导入。"),
    }.get(ui_language()) or (
        "取得 X/Twitter Cookies：\n"
        "1. 在瀏覽器登入 x.com。建議使用專門給 Bot 的獨立帳號。\n"
        "2. 使用可信任、可匯出 Netscape cookies.txt 的瀏覽器工具，只匯出 "
        "x.com／twitter.com 目前網站的 Cookies。\n"
        "3. 點「匯入 Cookies」，再把 cookies.txt 當作文件上傳。\n"
        "4. Bot 顯示成功後，刪除 Telegram 對話中的原始文件。\n\n"
        "Cookies 等同登入憑證。不要匯出其他網站、不要轉傳給他人；"
        "若 X 帳號登出或工作階段失效，需重新匯入。"
    )


def owner_help_text(is_owner: bool = True) -> str:
    if ui_language() == "en":
        role = (
            "Only the owner can grant administrator access and use Advanced settings."
            if is_owner else
            "Administrators can manage regular users only, not the owner, themselves, or other administrators."
        )
        return (
            "Owner guide" if is_owner else "Administrator guide"
        ) + f"\n\nIn a private chat, send a User ID to search, or ‘User ID quota’ to change access; confirm when prompted.\n-1: blocked · 0: initialized · positive: daily limit\n\n{role}"
    if ui_language() == "ja":
        role = (
            "管理者権限の付与と詳細設定は所有者のみ利用できます。"
            if is_owner else
            "管理者が変更できるのは一般ユーザーのみです。所有者、自分、他の管理者は変更できません。"
        )
        return (
            "所有者向けガイド" if is_owner else "管理者向けガイド"
        ) + f"\n\n個別チャットで User ID を送ると検索、「User ID 上限値」で権限を変更できます。確認画面で確定してください。\n-1：ブロック · 0：初期化 · 正の数：1日の上限\n\n{role}"
    role = (
        {"zh": ("僅所有者可授予管理員權限及使用高級選項。"
        if is_owner else
        "管理員只能管理普通用戶，不能修改所有者、自己或其他管理員。"), "zh-cn": ("仅所有者可授予管理员权限及使用高级选项。"
        if is_owner else
        "管理员只能管理普通用户，不能修改所有者、自己或其他管理员。")}[ui_language()]
    )
    return {
        "zh": ((
            "所有者管理說明" if is_owner else "管理員使用說明"
        ) + f"\n\n在 Bot 私聊傳送 User ID 查找用戶，或傳送「User ID 額度」修改權限，依提示確認。\n-1：封鎖 · 0：初始化 · 正數：每日上限\n\n{role}"),
        "zh-cn": ((
            "所有者管理说明" if is_owner else "管理员使用说明"
        ) + f"\n\n在 Bot 私聊发送 User ID 查找用户，或发送「User ID 额度」修改权限，依提示确认。\n-1：封禁 · 0：初始化 · 正数：每日上限\n\n{role}"),
    }[ui_language()]


def validate_cookie_file(content: bytes) -> str:
    if not content or len(content) > MAX_COOKIE_BYTES:
        raise ValueError("Cookies 檔案必須小於 1 MB。")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Cookies 檔案必須是 UTF-8 文字格式。") from error
    lines = [line.rstrip("\r") for line in text.splitlines()]
    if not lines or lines[0] not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
        raise ValueError("只接受 Netscape 格式的 cookies.txt。")
    valid_x_cookie = False
    for line in lines[1:]:
        candidate = line.removeprefix("#HttpOnly_")
        if not candidate or candidate.startswith("#"):
            continue
        fields = candidate.split("\t")
        if len(fields) != 7:
            continue
        domain = fields[0].lstrip(".").lower()
        if domain in {"x.com", "twitter.com"} or domain.endswith((".x.com", ".twitter.com")):
            valid_x_cookie = True
    if not valid_x_cookie:
        raise ValueError("檔案中找不到 X/Twitter 的有效 Cookie 記錄。")
    return "\n".join(lines) + "\n"


def save_cookie_file(text: str) -> None:
    atomic_write_text(COOKIES_PATH, text)
    COOKIE_ALERT_PATH.unlink(missing_ok=True)


def cookies_look_invalid(output: str) -> bool:
    lowered = output.lower()
    indicators = (
        "401 unauthorized",
        "403 forbidden",
        "authentication required",
        "login required",
        "loginrequired",
        "not logged in",
        "please log in",
        "please sign in",
        "sign in to confirm",
        "cookies are no longer valid",
        "cookies have expired",
        "cookie has expired",
        "only available to registered users",
    )
    return any(indicator in lowered for indicator in indicators)


def cookie_alert_due(path: Path = COOKIE_ALERT_PATH, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        last_sent = float(payload.get("last_sent", 0) or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        last_sent = 0
    return current - last_sent >= COOKIE_ALERT_INTERVAL


def record_cookie_alert(path: Path = COOKIE_ALERT_PATH, now: float | None = None) -> None:
    current = time.time() if now is None else now
    atomic_write_text(path,
        json.dumps({"last_sent": current}, separators=(",", ":")) + "\n",
    )


def safe_telegram_description(value: Any) -> str:
    description = " ".join(str(value or "").split())
    if BOT_TOKEN:
        description = description.replace(BOT_TOKEN, "[REDACTED]")
    description = re.sub(
        r"https://api\.telegram\.org/(?:file/)?bot[^/\s]+",
        "https://api.telegram.org/bot[REDACTED]",
        description,
        flags=re.IGNORECASE,
    )
    return description[:240]


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, status_code: int, description: str = "") -> None:
        self.method = method
        self.status_code = status_code
        self.description = safe_telegram_description(description)
        detail = f": {self.description}" if self.description else ""
        super().__init__(f"Telegram {method} returned HTTP {status_code}{detail}")


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.local = threading.local()

    def session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers["User-Agent"] = f"{APP_NAME}/{APP_VERSION}"
        return self.local.session

    def call(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: tuple[int, int] = (10, 70),
    ) -> Any:
        for attempt in range(2):
            try:
                response = self.session().post(
                    f"{self.base}/{method}",
                    data=data or {},
                    files=files,
                    timeout=timeout,
                )
            except requests.RequestException as error:
                # requests includes the full URL (and therefore the bot token) in
                # its exception text. Never allow that URL into journald.
                raise RuntimeError(
                    f"Telegram {method} request failed: {type(error).__name__}"
                ) from None
            try:
                payload = response.json()
            except ValueError as error:
                if response.status_code >= 400:
                    raise TelegramAPIError(method, response.status_code) from None
                raise RuntimeError(
                    f"Telegram {method} returned invalid JSON"
                ) from error
            if response.status_code == 429 and attempt == 0 and not files:
                retry_after = (payload.get("parameters") or {}).get("retry_after", 0)
                try:
                    retry_after = int(retry_after)
                except (TypeError, ValueError):
                    retry_after = 0
                if 0 < retry_after <= TELEGRAM_RETRY_AFTER_MAX_SECONDS:
                    LOG.warning(
                        "Telegram %s rate limited; retrying after %s seconds",
                        method,
                        retry_after,
                    )
                    time.sleep(retry_after)
                    continue
            if response.status_code >= 400:
                raise TelegramAPIError(
                    method,
                    response.status_code,
                    str(payload.get("description") or ""),
                )
            if not payload.get("ok"):
                raise TelegramAPIError(
                    method,
                    int(payload.get("error_code", response.status_code) or 0),
                    str(payload.get("description") or ""),
                )
            return payload.get("result")
        raise RuntimeError(f"Telegram {method} retry limit reached")

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> Any:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text if parse_mode else text[:4096],
            "disable_web_page_preview": "true",
        }
        if reply_to:
            data["reply_parameters"] = json.dumps({
                "message_id": reply_to, "allow_sending_without_reply": True,
            })
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if parse_mode:
            data["parse_mode"] = parse_mode
        return self.call("sendMessage", data)

    def remove_reply_keyboard(self, chat_id: int) -> None:
        result = self.send_message(
            chat_id, admin_text("menu_ready"), reply_markup=remove_keyboard()
        )
        message_id = int((result or {}).get("message_id", 0) or 0)
        if message_id:
            try:
                self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            except RuntimeError:
                LOG.warning("Could not delete reply-keyboard migration message")

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text if parse_mode else text[:4096],
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            self.call("editMessageText", data)
        except TelegramAPIError as error:
            if (
                error.status_code == 400
                and "message is not modified" in error.description.lower()
            ):
                return False
            raise
        return True

    def configure_commands(self) -> None:
        command_sets = {
            "": [
                {"command": "start", "description": "啟動並顯示操作選單"},
                {"command": "id", "description": "顯示我的 User ID"},
            ],
            "zh": [
                {"command": "start", "description": "启动并显示操作菜单"},
                {"command": "id", "description": "显示我的 User ID"},
            ],
            "en": [
                {"command": "start", "description": "Start and show the options"},
                {"command": "id", "description": "Show my User ID"},
            ],
            "ja": [
                {"command": "start", "description": "起動してメニューを表示"},
                {"command": "id", "description": "自分の User ID を表示"},
            ],
        }
        for language_code, commands in command_sets.items():
            payload = {
                "commands": json.dumps(commands, ensure_ascii=False),
                "scope": json.dumps({"type": "all_private_chats"}),
            }
            if language_code:
                payload["language_code"] = language_code
            self.call("setMyCommands", payload)
        self.call(
            "setChatMenuButton",
            {"menu_button": json.dumps({"type": "commands"})},
        )

    def configure_profile(self) -> None:
        profiles = {
            "": (
                "傳送 X/Twitter 貼文連結，取得文字、圖片與影片。",
                "傳送單篇 X/Twitter 貼文網址，取得文字、圖片和影片。"
                f"取得使用權限後，也可在其他聊天輸入 {BOT_MENTION} 加上網址分享媒體。",
            ),
            "zh": (
                "发送 X/Twitter 推文链接，获取文字、图片与视频。",
                "发送单篇 X/Twitter 推文链接，获取文字、图片和视频。"
                f"获取使用权限后，也可在其他聊天输入 {BOT_MENTION} 加上链接分享媒体。",
            ),
            "en": (
                "Send an X/Twitter post URL to retrieve its text, images and videos.",
                "Send a single X/Twitter post URL to get its text, images and videos. "
                f"Once approved, type {BOT_MENTION} followed by the post URL in another chat to share media.",
            ),
            "ja": (
                "X/Twitterの投稿URLから本文・画像・動画を取得します。",
                "X/Twitterの単一投稿URLを送ると、本文・画像・動画を取得できます。"
                f"承認後は他のチャットで {BOT_MENTION} に続けて投稿URLを入力し、メディアを共有できます。",
            ),
        }
        for language_code, (short_description, description) in profiles.items():
            short_data = {"short_description": short_description}
            description_data = {"description": description}
            if language_code:
                short_data["language_code"] = language_code
                description_data["language_code"] = language_code
            self.call("setMyShortDescription", short_data)
            self.call("setMyDescription", description_data)

    def download_file(self, file_id: str, maximum_bytes: int) -> bytes:
        result = self.call("getFile", {"file_id": file_id})
        file_path = str((result or {}).get("file_path", ""))
        if not file_path:
            raise RuntimeError("Telegram did not return a file path")
        try:
            response = self.session().get(
                f"{self.file_base}/{file_path}", stream=True, timeout=(10, 60)
            )
        except requests.RequestException as error:
            raise RuntimeError(
                f"Telegram file download failed: {type(error).__name__}"
            ) from None
        if response.status_code >= 400:
            raise RuntimeError(f"Telegram file download returned HTTP {response.status_code}")
        content = bytearray()
        for chunk in response.iter_content(64 * 1024):
            content.extend(chunk)
            if len(content) > maximum_bytes:
                raise ValueError("file is too large")
        return bytes(content)

    def send_action(self, chat_id: int, action: str) -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except RuntimeError as error:
            # Chat actions are cosmetic. A transient Telegram failure must not
            # abort media retrieval or consume the user's request without output.
            LOG.warning("Could not send Telegram chat action: %s", error)

    def answer_callback(self, callback_id: str, text: str, alert: bool = False) -> None:
        data = {
            "callback_query_id": callback_id,
            "show_alert": "true" if alert else "false",
        }
        if text:
            data["text"] = text[:200]
        self.call(
            "answerCallbackQuery",
            data,
        )

    def answer_inline_query(
        self,
        inline_query_id: str,
        results: list[dict[str, Any]],
        button: dict[str, str] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "inline_query_id": inline_query_id,
            "results": json.dumps(results[:INLINE_RESULT_LIMIT], ensure_ascii=False),
            "cache_time": str(INLINE_CACHE_SECONDS),
            "is_personal": "true",
        }
        if button:
            data["button"] = json.dumps(button, ensure_ascii=False)
        self.call("answerInlineQuery", data, timeout=(10, 30))

    def send_preview(
        self,
        chat_id: int,
        path: Path,
        caption: str = "",
        silent: bool = False,
        parse_mode: str | None = None,
    ) -> Any:
        suffix = path.suffix.lower()
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption if parse_mode else caption[:1024],
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            method, field = "sendPhoto", "photo"
        elif suffix in {".mp4", ".mov", ".m4v"}:
            method, field = "sendVideo", "video"
            dimensions = video_dimensions(path)
            if dimensions:
                data["width"], data["height"] = dimensions
            data["supports_streaming"] = "true"
        else:
            return None
        if silent:
            data["disable_notification"] = "true"
        with path.open("rb") as handle:
            return self.call(
                method,
                data,
                {field: (path.name, handle)},
                timeout=(15, 180),
            )

    def send_previews(
        self,
        chat_id: int,
        paths: list[Path],
        caption: str,
        parse_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        supported = [
            path for path in paths
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
        ]
        if not supported:
            return []
        if len(supported) == 1:
            result = self.send_preview(
                chat_id, supported[0], caption, parse_mode=parse_mode
            )
            return [result] if isinstance(result, dict) else []

        media: list[dict[str, Any]] = []
        with ExitStack() as stack:
            files: dict[str, Any] = {}
            for index, path in enumerate(supported[:10]):
                name = f"media{index}"
                suffix = path.suffix.lower()
                item: dict[str, Any] = {
                    "type": "photo" if suffix in {".jpg", ".jpeg", ".png", ".webp"} else "video",
                    "media": f"attach://{name}",
                }
                if index == 0:
                    item["caption"] = caption if parse_mode else caption[:1024]
                    if parse_mode:
                        item["parse_mode"] = parse_mode
                if item["type"] == "video":
                    dimensions = video_dimensions(path)
                    if dimensions:
                        item["width"], item["height"] = dimensions
                    item["supports_streaming"] = True
                handle = stack.enter_context(path.open("rb"))
                files[name] = (path.name, handle)
                media.append(item)
            result = self.call(
                "sendMediaGroup",
                {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
                files,
                timeout=(15, 240),
            )
            return result if isinstance(result, list) else []

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        with path.open("rb") as handle:
            self.call(
                "sendDocument",
                {"chat_id": chat_id, "caption": caption[:1024]},
                {"document": (path.name, handle)},
                timeout=(15, 180),
            )

    def send_documents(self, chat_id: int, paths: list[Path]) -> None:
        if not paths:
            return
        if len(paths) == 1:
            self.send_document(chat_id, paths[0])
            return
        with ExitStack() as stack:
            files: dict[str, Any] = {}
            media = []
            for index, path in enumerate(paths[:10]):
                name = f"document{index}"
                item: dict[str, Any] = {
                    "type": "document",
                    "media": f"attach://{name}",
                }
                handle = stack.enter_context(path.open("rb"))
                files[name] = (path.name, handle)
                media.append(item)
            self.call(
                "sendMediaGroup",
                {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
                files,
                timeout=(15, 240),
            )


def run_command(
    command: list[str], timeout: int, directory: Path
) -> subprocess.CompletedProcess[str]:
    LOG.info("Running extractor: %s", command[0])
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix=".extract-", suffix=".txt"
    ) as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            env={**os.environ, "HOME": str(STATE_DIR), "XDG_CACHE_HOME": str(directory / ".cache")},
        )
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                try:
                    process.wait(timeout=max(0, min(0.2, remaining)))
                except subprocess.TimeoutExpired:
                    pass
                try:
                    total = sum(
                        path.stat().st_size
                        for path in directory.rglob("*") if path.is_file()
                    )
                except FileNotFoundError:
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout) from None
                    continue  # A downloader renamed a partial file while we counted.
                if total > MAX_TOTAL_BYTES or shutil.disk_usage(directory).free < MIN_FREE_DISK_BYTES:
                    raise ValueError("Extractor exceeded the media directory limit")
                if process.poll() is not None:
                    break
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 65536))
        return subprocess.CompletedProcess(
            command, process.returncode, output.read().decode("utf-8", "replace")
        )


def normalize_author_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https":
        return ""
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]):
        return ""
    return f"https://x.com/{parts[0]}"


def author_profile_url(username: Any) -> str:
    candidate = str(username or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", candidate):
        return ""
    return f"https://x.com/{candidate}"


def truncate_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return "." * limit
    return value[: limit - 3] + "..."


def author_text_html(author: str, author_url: str, text: str, limit: int) -> str:
    label = author or author_url
    if not label:
        return html.escape(truncate_text(text, limit))

    label = truncate_text(label, max(0, limit - 1))
    if not label:
        return ""
    if author_url:
        rendered = (
            f'<a href="{html.escape(author_url, quote=True)}">'
            f"{html.escape(label)}</a>:"
        )
    else:
        rendered = html.escape(label) + ":"

    remaining = limit - len(label) - 1
    if text and remaining > 1:
        rendered += "\n" + html.escape(truncate_text(text, remaining - 1))
    return rendered


def tweet_html(
    author: str,
    author_url: str,
    text: str,
    url: str,
    limit: int,
    note: str = "",
) -> str:
    suffix_text = f"\n\n{url}"
    if note:
        suffix_text += f"\n\n{note}"
    heading_limit = max(0, limit - len(suffix_text))
    heading = author_text_html(author, author_url, text, heading_limit)
    if not heading:
        return html.escape(truncate_text(url + (f"\n\n{note}" if note else ""), limit))
    return heading + html.escape(suffix_text)


def fetch_tweet_text(url: str) -> tuple[str, str, str]:
    try:
        response = http_session().get(
            "https://publish.twitter.com/oembed?" + urlencode(
                {"url": url, "omit_script": "true", "dnt": "true"}
            ),
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        parser = TextExtractor()
        parser.feed(payload.get("html", ""))
        text = " ".join(parser.parts).replace(" \n ", "\n")
        text = re.sub(r"(?:https?://)?pic\.twitter\.com/\S+", "", text).strip()
        return (
            html.unescape(text).strip(),
            str(payload.get("author_name", "")).strip(),
            normalize_author_url(str(payload.get("author_url", ""))),
        )
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status in {403, 404}:
            LOG.info("oEmbed unavailable (HTTP %s)", status)
        else:
            LOG.exception("oEmbed text extraction failed")
        return "", "", ""
    except (requests.RequestException, ValueError, TypeError):
        LOG.exception("oEmbed text extraction failed")
        return "", "", ""


def fetch_fxtwitter(url: str) -> dict[str, Any] | None:
    match = URL_RE.search(url)
    if not match:
        return None
    try:
        response = http_session().get(
            f"https://api.fxtwitter.com/i/status/{match.group(1)}",
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        tweet = payload.get("tweet") or payload.get("status")
        return tweet if isinstance(tweet, dict) else None
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status in {403, 404}:
            LOG.info("FxTwitter unavailable (HTTP %s)", status)
        else:
            LOG.exception("FxTwitter fallback failed")
        return None
    except (requests.RequestException, ValueError, TypeError):
        LOG.exception("FxTwitter fallback failed")
        return None


def fxtwitter_tweet_url(tweet: dict[str, Any]) -> str | None:
    candidate = normalize_status_url(str(tweet.get("url") or ""))
    if candidate:
        return candidate
    tweet_id = str(tweet.get("id") or "").strip()
    if not tweet_id.isdigit():
        return None
    author = tweet.get("author") or {}
    username = ""
    if isinstance(author, dict):
        username = str(
            author.get("screen_name") or author.get("username") or ""
        ).strip().lstrip("@")
    return f"https://x.com/{username or 'i'}/status/{tweet_id}"


def fxtwitter_text_author(tweet: dict[str, Any]) -> tuple[str, str, str]:
    author = tweet.get("author") or {}
    author_name = ""
    author_url = ""
    if isinstance(author, dict):
        author_name = str(
            author.get("name") or author.get("screen_name") or author.get("username") or ""
        ).strip()
        author_url = author_profile_url(
            author.get("screen_name") or author.get("username") or ""
        )
    return str(tweet.get("text") or "").strip(), author_name, author_url


def sorted_mp4_formats(item: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (entry for entry in item.get("formats") or []
         if isinstance(entry, dict) and entry.get("container") == "mp4" and entry.get("url")),
        key=lambda entry: (
            int(entry.get("width") or 0) * int(entry.get("height") or 0),
            int(entry.get("bitrate") or 0),
        ),
        reverse=True,
    )


def fxtwitter_media(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    media = tweet.get("media")
    if not isinstance(media, dict):
        return results
    for item in media.get("all") or []:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        if candidate.get("type") in {"video", "gif"}:
            formats = sorted_mp4_formats(candidate)
            if formats:
                candidate["url"] = formats[0]["url"]
        media_url = str(candidate.get("url") or "")
        if media_url and media_url not in seen:
            seen.add(media_url)
            results.append(candidate)
    return results


def trusted_twimg_url(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url")
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "twimg.com" or host.endswith(".twimg.com")
    ):
        return None
    return candidate


def trusted_twimg_response(
    url: str, timeout: tuple[int, int], maximum_redirects: int = 3
) -> requests.Response:
    current = url
    for _ in range(maximum_redirects + 1):
        if not trusted_twimg_url(current):
            raise ValueError("untrusted media URL")
        response = http_session().get(
            current,
            stream=True,
            allow_redirects=False,
            timeout=timeout,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location", "")
            response.close()
            next_url = urljoin(current, location)
            if not trusted_twimg_url(next_url):
                raise ValueError("media redirect left trusted twimg.com hosts")
            current = next_url
            continue
        try:
            response.raise_for_status()
            if response.status_code != 200 or not trusted_twimg_url(response.url):
                raise ValueError("media response did not use a trusted twimg.com URL")
        except Exception:
            response.close()
            raise
        return response
    raise ValueError("too many media redirects")


def inline_thumbnail_url(item: dict[str, Any]) -> str | None:
    for key in (
        "thumbnail_url",
        "thumb_url",
        "poster",
        "preview_image_url",
        "thumbnail",
    ):
        candidate = trusted_twimg_url(item.get(key))
        if candidate:
            return candidate
    return None


def inline_photo_url(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["name"] = "large"
    return urlunparse(parsed._replace(query=urlencode(query)))


def remote_media_size(url: str) -> int | None:
    try:
        with trusted_twimg_response(url, timeout=(10, 30)) as response:
            length = response.headers.get("Content-Length")
            return int(length) if length and length.isdigit() else None
    except (requests.RequestException, TypeError, ValueError):
        LOG.exception("Could not determine inline media size")
        return None


def inline_result_id(media_url: str, index: int) -> str:
    digest = hashlib.sha256(media_url.encode("utf-8")).hexdigest()[:24]
    return f"tweet-{index}-{digest}"


def inline_caption(
    author: str, author_url: str, text: str, url: str, debug: bool = False
) -> str:
    return tweet_html(
        author,
        author_url,
        text,
        url,
        1024,
        {"zh": "取得方式：FxTwitter Inline", "en": "Method: FxTwitter Inline", "ja": "取得方法：FxTwitter Inline", "zh-cn": ("获取方式：FxTwitter Inline")}[ui_language()] if debug else "",
    )


def build_inline_results(url: str, debug: bool = False) -> list[dict[str, Any]]:
    root_tweet = fetch_fxtwitter(url)
    if not root_tweet:
        return []
    tweet = root_tweet
    effective_url = fxtwitter_tweet_url(tweet) or url
    text, author, author_url = fxtwitter_text_author(tweet)
    if not text:
        text, author, author_url = fetch_tweet_text(effective_url)
    caption = inline_caption(author, author_url, text, effective_url, debug)
    results: list[dict[str, Any]] = []
    for index, item in enumerate(fxtwitter_media(tweet), start=1):
        media_url = trusted_twimg_url(item.get("url"))
        if not media_url:
            continue
        media_type = str(item.get("type") or "").lower()
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if media_type == "photo":
            preview_url = inline_photo_url(media_url)
            result: dict[str, Any] = {
                "type": "photo",
                "id": inline_result_id(media_url, index),
                "photo_url": preview_url,
                "thumbnail_url": preview_url,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if width > 0 and height > 0:
                result["photo_width"] = width
                result["photo_height"] = height
            results.append(result)
            continue
        if media_type not in {"video", "gif"}:
            continue
        thumbnail_url = inline_thumbnail_url(item)
        media_size = remote_media_size(media_url)
        if not thumbnail_url or media_size is None or media_size > MAX_VIDEO_BYTES:
            continue
        result = {
            "type": "mpeg4_gif" if media_type == "gif" else "video",
            "id": inline_result_id(media_url, index),
            "thumbnail_url": thumbnail_url,
            "title": author or "X/Twitter media",
            "caption": caption,
            "parse_mode": "HTML",
        }
        if media_type == "gif":
            result["mpeg4_url"] = media_url
            if width > 0 and height > 0:
                result["mpeg4_width"] = width
                result["mpeg4_height"] = height
        else:
            result["video_url"] = media_url
            result["mime_type"] = "video/mp4"
            if width > 0 and height > 0:
                result["video_width"] = width
                result["video_height"] = height
            duration = int(item.get("duration") or 0)
            if duration > 0:
                result["video_duration"] = duration
        results.append(result)
        if len(results) >= INLINE_RESULT_LIMIT:
            break
    if results:
        return results
    message = tweet_html(author, author_url, text, effective_url, 4096)
    return [{
        "type": "article",
        "id": inline_result_id(effective_url, 0),
        "title": author or "X/Twitter post",
        "description": text[:120],
        "input_message_content": {
            "message_text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    }]


def media_caption(
    author: str,
    author_url: str,
    text: str,
    url: str,
    debug: bool = False,
    method: str = "",
) -> str:
    if ui_language() in {"en", "ja"}:
        method = {
            "FxTwitter 直連": "FxTwitter direct",
            "FxTwitter 備援": "FxTwitter fallback",
            "gallery-dl（匿名）": "gallery-dl (anonymous)",
            "gallery-dl（Cookies）": "gallery-dl (Cookies)",
            "yt-dlp（匿名）": "yt-dlp (anonymous)",
            "yt-dlp（Cookies）": "yt-dlp (Cookies)",
        }.get(method, method)
    elif ui_language() == "zh-cn":
        method = {"FxTwitter 直連": "FxTwitter 直连", "FxTwitter 備援": "FxTwitter 备用"}.get(method, method)
    return tweet_html(
        author,
        author_url,
        text,
        url,
        1024,
        {
            "zh": f"取得方式：{method or '未取得媒體'}",
            "en": f"Method: {method or 'No media'}",
            "ja": f"取得方法：{method or 'メディアなし'}",
            "zh-cn": (f"获取方式：{method or '未获取媒体'}"),
        }[ui_language()] if debug else "",
    )


def download_fxtwitter_media(
    tweet: dict[str, Any], directory: Path
) -> tuple[list[Path], str, int]:
    downloaded: list[Path] = []
    total = 0
    oversized_videos = 0
    deadline = time.monotonic() + 240
    for index, item in enumerate(fxtwitter_media(tweet), start=1):
        media_type = str(item.get("type") or "")
        item_limit = MAX_VIDEO_BYTES if media_type in {"video", "gif"} else MAX_MEDIA_BYTES
        candidates = [item]
        if media_type in {"video", "gif"}:
            candidates = sorted_mp4_formats(item) or candidates

        item_downloaded = False
        item_oversized = False
        seen_urls: set[str] = set()
        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("Media download exceeded its time limit")
            if shutil.disk_usage(directory).free < MIN_FREE_DISK_BYTES:
                raise OSError("Not enough free space for media download")
            media_url = trusted_twimg_url(candidate.get("url"))
            if not media_url or media_url in seen_urls:
                continue
            seen_urls.add(media_url)
            parsed = urlparse(media_url)
            suffix = Path(parsed.path).suffix.lower()
            if media_type == "photo" and suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                suffix = ".jpg"
            elif media_type in {"video", "gif"} and suffix not in {".mp4", ".mov", ".m4v"}:
                suffix = ".mp4"
            target = directory / f"fxtwitter_{index}{suffix or '.bin'}"
            try:
                with trusted_twimg_response(media_url, timeout=(min(10, remaining), min(30, remaining))) as response:
                    # read1 returns available data without waiting to fill a chunk,
                    # so a slow trickle cannot bypass the overall deadline.
                    read = getattr(response.raw, "read1", None)
                    if not callable(read) or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("Media response does not support bounded streaming")
                    content_length = response.headers.get("Content-Length", "")
                    if content_length.isdigit() and int(content_length) > item_limit:
                        item_oversized = True
                        continue
                    size = 0
                    with target.open("wb") as handle:
                        for chunk in iter(lambda: read(128 * 1024, decode_content=False), b""):
                            if time.monotonic() >= deadline:
                                raise requests.Timeout("Media download exceeded its time limit")
                            if not chunk:
                                continue
                            if shutil.disk_usage(directory).free < MIN_FREE_DISK_BYTES + len(chunk):
                                raise OSError("Not enough free space for media download")
                            size += len(chunk)
                            if size > item_limit:
                                item_oversized = True
                                raise ValueError("FxTwitter media exceeds item limit")
                            if total + size > MAX_TOTAL_BYTES:
                                raise ValueError("FxTwitter media exceeds configured limit")
                            handle.write(chunk)
                    total += size
                    downloaded.append(target)
                    item_downloaded = True
                    break
            except (OSError, ValueError, requests.RequestException, StreamHTTPError):
                target.unlink(missing_ok=True)
                LOG.warning("FxTwitter media candidate failed", exc_info=True)
        if not item_downloaded and item_oversized and media_type in {"video", "gif"}:
            oversized_videos += 1
    return (
        downloaded,
        "FxTwitter direct download returned no downloadable media",
        oversized_videos,
    )


def media_files(directory: Path) -> list[Path]:
    ignored = {".json", ".part", ".ytdl", ".txt"}
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() not in ignored
        and ".cache" not in path.relative_to(directory).parts
    )


def telegram_photo_dimensions_valid(width: int, height: int) -> bool:
    if width <= 0 or height <= 0 or width + height > 10_000:
        return False
    return max(width / height, height / width) <= 20


def pad_extreme_photo_ratio(image: Image.Image) -> Image.Image:
    width, height = image.size
    if telegram_photo_dimensions_valid(width, height):
        return image
    target_width = max(width, math.ceil(height / 20))
    target_height = max(height, math.ceil(width / 20))
    canvas = Image.new("RGB", (target_width, target_height), "white")
    canvas.paste(image, ((target_width - width) // 2, (target_height - height) // 2))
    return canvas


def prepare_image(path: Path) -> Path:
    try:
        with Image.open(path) as image:
            orientation = int(image.getexif().get(274, 1) or 1)
            if (
                orientation == 1
                and image.mode in {"RGB", "L"}
                and image.width <= 4096
                and image.height <= 4096
                and path.stat().st_size <= 9_000_000
                and telegram_photo_dimensions_valid(image.width, image.height)
            ):
                return path
            image = ImageOps.exif_transpose(image)
            image.thumbnail((4096, 4096))
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image = pad_extreme_photo_ratio(image)
            output = path.with_suffix(".telegram.jpg")
            quality = 90
            while quality >= 55:
                image.save(output, "JPEG", quality=quality, optimize=True)
                if output.stat().st_size <= 9_000_000:
                    return output
                quality -= 10
    except (OSError, ValueError):
        LOG.exception("Image conversion failed: %s", path)
    return path


def parse_video_dimensions(payload: dict[str, Any]) -> tuple[int, int] | None:
    streams = payload.get("streams") or []
    if not streams or not isinstance(streams[0], dict):
        return None
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        return None
    ratio = str(stream.get("sample_aspect_ratio") or "1:1")
    try:
        numerator, denominator = (int(value) for value in ratio.split(":", 1))
        if numerator > 0 and denominator > 0:
            width = max(1, round(width * numerator / denominator))
    except (ValueError, ZeroDivisionError):
        pass
    rotation = int((stream.get("tags") or {}).get("rotate") or 0)
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and side_data.get("rotation") is not None:
            rotation = int(side_data["rotation"])
            break
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def video_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
                "-of", "json", str(path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode == 0:
            return parse_video_dimensions(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        LOG.exception("Video dimension probe failed: %s", path)
    return None


def download_media(
    url: str, directory: Path, allow_cookies: bool = True
) -> tuple[list[Path], str, str, bool]:
    command = [
        "/opt/x-tweet-telegram-bot/venv/bin/gallery-dl",
        "--ignore-config",
        "--range", "1-10",
        "--filesize-max", str(max(MAX_MEDIA_BYTES, MAX_VIDEO_BYTES)),
        "--directory",
        str(directory),
        "--filename",
        "{tweet_id}_{num}.{extension}",
        "-o",
        "extractor.twitter.videos=ytdl",
        "-o",
        "extractor.twitter.cards=true",
        "-o",
        "extractor.twitter.quoted=false",
        "-o",
        "extractor.twitter.replies=false",
        "-o",
        "extractor.twitter.retweets=false",
        "-o",
        "extractor.twitter.ratelimit=abort:15",
        "-o",
        "downloader.ytdl.format=best[filesize<45M]/best[filesize_approx<45M]/worst",
    ]
    result = run_command([*command, url], timeout=240, directory=directory)
    files = media_files(directory)
    method = "gallery-dl（匿名）" if files else ""
    cookie_invalid = False

    if not files and allow_cookies and COOKIES_PATH.exists():
        result = run_command(
            [*command, "--cookies", str(COOKIES_PATH), url],
            timeout=240, directory=directory,
        )
        cookie_invalid = cookies_look_invalid(result.stdout)
        files = media_files(directory)
        if files:
            method = "gallery-dl（Cookies）"

    if not files:
        fallback = [
            "/opt/x-tweet-telegram-bot/venv/bin/yt-dlp",
            "--no-config",
            "--no-playlist",
            "--max-filesize", str(MAX_VIDEO_BYTES),
            "--restrict-filenames",
            "--merge-output-format",
            "mp4",
            "--format",
            "best[filesize<45M]/best[filesize_approx<45M]/worst",
            "--output",
            str(directory / "%(id)s.%(ext)s"),
        ]
        result = run_command([*fallback, url], timeout=240, directory=directory)
        files = media_files(directory)
        if files:
            method = "yt-dlp（匿名）"

        if not files and allow_cookies and COOKIES_PATH.exists():
            result = run_command(
                [*fallback, "--cookies", str(COOKIES_PATH), url],
                timeout=240, directory=directory,
            )
            cookie_invalid = cookie_invalid or cookies_look_invalid(result.stdout)
            files = media_files(directory)
            if files:
                method = "yt-dlp（Cookies）"

    log_tail = "\n".join(result.stdout.strip().splitlines()[-8:])
    return files, log_tail, method, cookie_invalid


def trim_files(files: list[Path]) -> tuple[list[Path], list[str]]:
    accepted: list[Path] = []
    rejected: list[str] = []
    total = 0
    for path in files[:10]:
        size = path.stat().st_size
        is_video = path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}
        item_limit = MAX_VIDEO_BYTES if is_video else MAX_MEDIA_BYTES
        if size > item_limit or total + size > MAX_TOTAL_BYTES:
            rejected.append(path.name)
            continue
        accepted.append(path)
        total += size
    return accepted, rejected


class Bot:
    def __init__(self, api: TelegramAPI, acl: ACLStore) -> None:
        self.api = api
        self.acl = acl
        self.jobs: queue.Queue[tuple[int, int, int, str]] = queue.Queue()
        for job in self.acl.data["pending_jobs"].values():
            self.jobs.put_nowait(tuple(job))
        self.stop_event = threading.Event()
        self.workers = [
            threading.Thread(
                target=self._worker,
                daemon=True,
                name=f"media-worker-{index + 1}",
            )
            for index in range(WORKER_COUNT)
        ]
        # Keep the former attribute for lightweight external status checks.
        self.worker = self.workers[0]
        self.inline_executor = ThreadPoolExecutor(
            max_workers=INLINE_WORKER_COUNT,
            thread_name_prefix="inline-worker",
        )
        self.inline_slots = threading.BoundedSemaphore(INLINE_MAX_PENDING)
        self.inline_futures_lock = threading.Lock()
        self.inline_futures: set[Future[Any]] = set()
        self.inline_cache_lock = threading.Lock()
        self.inline_cache: dict[
            tuple[str, bool, str], tuple[float, list[dict[str, Any]]]
        ] = {}
        self.pending_cookie_uploads: set[int] = set()
        self.cookie_alert_lock = threading.Lock()
        self.pending_user_searches: set[int] = set()
        self.pending_default_quotas: dict[int, int | None] = {}
        self.inline_usage_lock = threading.Lock()
        self.inline_usage: dict[tuple[int, str], float] = {}
        self.started_at = time.time()
        self.last_disk_cleanup = 0.0

    def clear_pending_input(self, user_id: int) -> None:
        self.pending_cookie_uploads.discard(user_id)
        self.pending_user_searches.discard(user_id)
        self.pending_default_quotas.pop(user_id, None)

    def target_management_error(self, actor_id: int, target_id: int) -> str | None:
        if not is_telegram_user_id(target_id):
            return {"zh": "User ID 不在 Telegram 的有效範圍內。", "en": "User ID is outside Telegram's valid range.", "ja": "User ID が Telegram の有効範囲外です。", "zh-cn": ("User ID 不在 Telegram 的有效范围内。")}[ui_language()]
        if target_id == self.acl.owner_id:
            return admin_text("owner_cannot_change")
        if actor_id != self.acl.owner_id:
            if target_id == actor_id:
                return admin_text("admin_cannot_self")
            if self.acl.is_admin(target_id):
                return admin_text("admin_cannot_admin")
        return None

    @staticmethod
    def is_private_management_chat(user_id: int, chat_id: int) -> bool:
        return user_id == chat_id

    def start(self) -> None:
        cleanup_stale_media()
        self.last_disk_cleanup = time.monotonic()
        try:
            self.api.configure_commands()
        except (requests.RequestException, RuntimeError, ValueError):
            LOG.exception("Could not configure Telegram command menu")
        try:
            self.api.configure_profile()
        except (requests.RequestException, RuntimeError, ValueError):
            LOG.exception("Could not configure Telegram profile")
        for worker in self.workers:
            worker.start()
        offset = load_update_offset()
        while not self.stop_event.is_set():
            try:
                updates = self.api.call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 20,
                        "allowed_updates": json.dumps(
                            ["message", "edited_message", "callback_query", "inline_query"]
                        ),
                    },
                    timeout=(10, 28),
                )
                for update in updates:
                    if self.stop_event.is_set():
                        break
                    next_offset = max(offset, int(update["update_id"]) + 1)
                    try:
                        self.handle_update(update)
                    except OSError:
                        # Do not acknowledge an update whose durable state failed.
                        raise
                    except Exception:
                        LOG.exception(
                            "Telegram update handling failed: update_id=%s",
                            update.get("update_id"),
                        )
                    offset = next_offset
                    save_update_offset(offset)
                self.maybe_send_daily_report()
                if time.monotonic() - self.last_disk_cleanup >= 3600:
                    cleanup_stale_media()
                    self.last_disk_cleanup = time.monotonic()
            except (requests.RequestException, RuntimeError, ValueError):
                LOG.exception("Polling failed")
                time.sleep(5)

    def stop(self) -> None:
        self.stop_event.set()

    def can_process(self, user_id: int) -> bool:
        return self.acl.is_allowed(user_id) and (
            self.acl.external_access_enabled
            or self.acl.is_admin(user_id)
        )

    def handle_update(self, update: dict[str, Any]) -> None:
        event = update.get("inline_query") or update.get("callback_query") or update.get("message") or update.get("edited_message") or {}
        user_id = int((event.get("from") or {}).get("id", 0) or 0)
        with language_scope(self.acl.language(user_id)):
            self._handle_update(update)

    def _handle_update(self, update: dict[str, Any]) -> None:
        inline_query = update.get("inline_query")
        if isinstance(inline_query, dict):
            self.handle_inline_query(inline_query)
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self.handle_callback(callback)
            return
        message = update.get("message") or update.get("edited_message") or {}
        sender = message.get("from") or {}
        user_id = int(sender.get("id", 0) or 0)
        chat_id = int((message.get("chat") or {}).get("id", 0) or 0)
        message_id = int(message.get("message_id", 0) or 0)
        if not user_id or not chat_id:
            return
        self.acl.observe(sender)
        language = self.acl.language(user_id)
        if self.acl.is_banned(user_id):
            return

        is_owner = user_id == self.acl.owner_id
        is_admin = self.acl.is_admin(user_id)
        is_private_chat = self.is_private_management_chat(user_id, chat_id)

        document = message.get("document")
        if isinstance(document, dict):
            if is_admin:
                self.handle_document(chat_id, message_id, user_id, document)
            else:
                self.api.send_message(
                    chat_id,
                    public_text(language, "url_only"),
                    message_id,
                    start_keyboard(
                        language,
                        is_admin,
                        self.acl.is_allowed(user_id),
                    ),
                )
            return

        text = str(message.get("text", "")).strip()
        if not text:
            return

        if is_admin and text in OWNER_BUTTONS:
            text = OWNER_BUTTONS[text]

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()

        if is_owner and is_admin and user_id in self.pending_default_quotas:
            if command.startswith("/"):
                self.pending_default_quotas.pop(user_id, None)
            else:
                if not is_private_chat:
                    self.api.send_message(chat_id, admin_text("private_only"), message_id)
                    return
                if not re.fullmatch(r"[0-9]{1,6}", text) or not 1 <= int(text) <= MAX_DAILY_LIMIT:
                    self.api.send_message(
                        chat_id, admin_text("default_limit_range"), message_id, default_limit_keyboard()
                    )
                    return
                limit = int(text)
                self.pending_default_quotas[user_id] = limit
                self.api.send_message(
                    chat_id,
                    admin_text("default_limit_confirm").format(limit=limit),
                    message_id,
                    default_limit_keyboard(limit),
                )
                return

        if (
            is_admin
            and user_id in self.pending_user_searches
            and not command.startswith("/")
        ):
            self.pending_user_searches.discard(user_id)
            if not is_private_chat:
                self.api.send_message(
                    chat_id, admin_text("private_only"), message_id
                )
                return
            try:
                target = int(text)
                if not is_telegram_user_id(target):
                    raise ValueError
            except ValueError:
                self.api.send_message(
                    chat_id,
                    {"zh": "User ID 必須是 1 至 2^52-1 的正整數。請重新點選「用戶權限修改」。", "en": "User ID must be between 1 and 2^52-1. Select Change access again.", "ja": "User ID は 1～2^52-1 の整数にしてください。権限を変更をもう一度選んでください。", "zh-cn": ("User ID 必须是 1 至 2^52-1 的正整数。请重新点选「用户权限修改」。")}[ui_language()],
                    message_id,
                    user_menu_keyboard(),
                )
                return
            if is_owner and not self.acl.has_user(target):
                self.api.send_message(
                    chat_id,
                    {"zh": f"資料庫中沒有 User ID {target}。\n確認以每日額度 {self.acl.default_daily_limit} 建立此用戶？", "en": f"User ID {target} is not in the database.\nCreate with a daily limit of {self.acl.default_daily_limit}?", "ja": f"User ID {target} はデータベースにありません。\n1日の上限 {self.acl.default_daily_limit} で作成しますか？", "zh-cn": (f"数据库中没有 User ID {target}。\n确认以每日额度 {self.acl.default_daily_limit} 建立此用户？")}[ui_language()],
                    message_id,
                    confirm_new_user_keyboard(target, self.acl.default_daily_limit),
                )
                return
            self.send_user_search_result(chat_id, message_id, target, user_id)
            return

        if is_admin:
            numeric_parts = text.split()
            if (
                len(numeric_parts) in {1, 2}
                and numeric_parts[0].isdigit()
                and is_telegram_user_id_shortcut(int(numeric_parts[0]))
                and (
                    len(numeric_parts) == 1
                    or re.fullmatch(r"-?\d+", numeric_parts[1])
                )
            ):
                if not is_private_chat:
                    self.api.send_message(
                        chat_id, admin_text("private_only"), message_id
                    )
                    return
                target = int(numeric_parts[0])
                quota = self.acl.default_daily_limit
                if len(numeric_parts) == 2:
                    try:
                        quota = int(numeric_parts[1])
                        if quota < -1 or quota > MAX_DAILY_LIMIT:
                            raise ValueError
                    except ValueError:
                        self.api.send_message(
                            chat_id,
                            admin_text("quota_range"),
                            message_id,
                        )
                        return
                if not self.acl.has_user(target):
                    self.api.send_message(
                        chat_id,
                        {"zh": f"資料庫中沒有 User ID {target}。\n確認以每日額度 {quota} 建立此用戶？", "en": f"User ID {target} is not in the database.\nCreate with a daily limit of {quota}?", "ja": f"User ID {target} はデータベースにありません。\n1日の上限 {quota} で作成しますか？", "zh-cn": (f"数据库中没有 User ID {target}。\n确认以每日额度 {quota} 建立此用户？")}[ui_language()],
                        message_id,
                        confirm_new_user_keyboard(target, quota),
                    )
                    return
                if len(numeric_parts) == 2:
                    error = self.target_management_error(user_id, target)
                    if error:
                        self.api.send_message(chat_id, error, message_id)
                        return
                    current_quota = self.acl.quota(target)
                    current_label = (
                        admin_text("admin_unlimited") if current_quota is None else str(current_quota)
                    )
                    self.api.send_message(
                        chat_id,
                        {"zh": f"確認修改 User ID {target} 的權限？\n目前：{current_label}\n新值：{quota}", "en": f"Change access for User ID {target}?\nCurrent: {current_label}\nNew: {quota}", "ja": f"User ID {target} の権限を変更しますか？\n現在：{current_label}\n新しい値：{quota}", "zh-cn": (f"确认修改 User ID {target} 的权限？\n目前：{current_label}\n新值：{quota}")}[ui_language()],
                        message_id,
                        confirm_quota_change_keyboard(target, quota),
                    )
                    return
                self.send_user_search_result(chat_id, message_id, target, user_id)
                return

        if command == "/start":
            self.clear_pending_input(user_id)
            is_allowed = self.acl.is_allowed(user_id)
            text_key = "start_owner" if is_admin else "start_allowed"
            text = (
                public_text(language, text_key)
                if is_allowed
                else access_request_text(user_id, language)
            )
            self.api.send_message(
                chat_id,
                text,
                message_id,
                start_keyboard(
                    language,
                    is_admin,
                    is_allowed,
                ),
            )
            return

        if command == "/id":
            identity = f"Telegram User ID: {user_id}"
            if chat_id != user_id:
                identity += f"\nTelegram Chat ID: {chat_id}"
            self.api.send_message(chat_id, identity, message_id)
            return

        if command == "/claim":
            if not is_private_chat:
                self.api.send_message(
                    chat_id, {"zh": "所有者認領只允許在 Bot 私聊完成。", "en": "Owner claim is available only in a private chat with the bot.", "ja": "所有者の登録は Bot との個別チャットでのみ可能です。", "zh-cn": ("所有者认领只允许在 Bot 私聊完成。")}[ui_language()], message_id
                )
                return
            if self.acl.claim(user_id, argument):
                self.api.configure_commands()
                self.api.remove_reply_keyboard(chat_id)
                self.api.send_message(
                    chat_id, {"zh": "所有者設定完成，管理選單已載入。", "en": "Owner configured. Management menu loaded.", "ja": "所有者を設定し、管理メニューを表示しました。", "zh-cn": ("所有者设置完成，管理菜单已加载。")}[ui_language()], message_id, owner_keyboard()
                )
            else:
                self.api.send_message(chat_id, {"zh": "認領失敗或所有者已存在。", "en": "Claim failed or an owner already exists.", "ja": "登録に失敗したか、所有者が既に存在します。", "zh-cn": ("认领失败或所有者已存在。")}[ui_language()], message_id)
            return

        if command in {
            "/allow",
            "/deny",
            "/ban",
            "/unban",
            "/users",
            "/requests",
            "/finduser",
            "/status",
            "/limit",
            "/help",
            "/menu",
            "/usermenu",
            "/cookiemenu",
            "/cookies",
            "/cookiehelp",
            "/clearcookies",
            "/cancel",
        }:
            if not is_admin:
                if command == "/help":
                    self.api.send_message(
                        chat_id,
                        public_help_text(language),
                        message_id,
                        {"inline_keyboard": [[{
                            "text": public_text(language, "back"),
                            "callback_data": "public:main",
                        }]]},
                        parse_mode="HTML",
                    )
                    return
                keyboard = start_keyboard(
                    language,
                    is_admin,
                    self.acl.is_allowed(user_id),
                )
                self.api.send_message(
                    chat_id,
                    (
                        public_text(language, "start_allowed")
                        if self.acl.is_allowed(user_id)
                        else access_request_text(user_id, language)
                    ),
                    message_id,
                    keyboard,
                )
                return
            if not is_private_chat:
                self.api.send_message(
                    chat_id, admin_text("private_only"), message_id
                )
                return
            if command in {
                "/cookiemenu",
                "/cookies",
                "/cookiehelp",
                "/clearcookies",
            } and not is_owner:
                self.api.send_message(
                    chat_id,
                    admin_text("owner_only_cookies"),
                    message_id,
                    owner_keyboard(),
                )
                return
            self.clear_pending_input(user_id)
            self.handle_owner_command(
                chat_id, message_id, user_id, command, argument
            )
            return

        if not self.acl.is_allowed(user_id):
            self.api.send_message(
                chat_id,
                access_request_text(user_id, language),
                message_id,
                application_keyboard(language),
            )
            return

        url = normalize_status_url(text)
        if not url:
            self.api.send_message(chat_id, public_text(language, "invalid_url"), message_id)
            return
        if not is_admin and not self.acl.external_access_enabled:
            self.api.send_message(
                chat_id, public_text(language, "service_paused"), message_id
            )
            return
        if self.jobs.qsize() >= MAX_QUEUE:
            self.api.send_message(chat_id, public_text(language, "queue_full"), message_id)
            return
        job = (chat_id, message_id, user_id, url)
        if f"{chat_id}:{message_id}" in self.acl.data["pending_jobs"]:
            return
        if not media_disk_available():
            self.api.send_message(chat_id, public_text(language, "queue_full"), message_id)
            return
        quota_ok, used, limit = self.acl.consume(user_id, job=job)
        if not quota_ok:
            self.api.send_message(
                chat_id,
                public_text(language, "quota", used=used, limit=limit),
                message_id,
            )
            return
        try:
            self.jobs.put_nowait((chat_id, message_id, user_id, url))
        except queue.Full:
            self.api.send_message(chat_id, public_text(language, "queue_full"), message_id)

    def consume_inline_once(self, user_id: int, url: str) -> bool:
        now = time.time()
        key = (user_id, url)
        with self.inline_usage_lock:
            self.inline_usage = {
                item: timestamp
                for item, timestamp in self.inline_usage.items()
                if now - timestamp < INLINE_USAGE_DEDUP_SECONDS
            }
            if key in self.inline_usage:
                return True
            allowed, _used, _limit = self.acl.consume(user_id, now=now)
            if allowed:
                self.inline_usage[key] = now
            return allowed

    def handle_inline_query(self, inline_query: dict[str, Any]) -> None:
        query_id = str(inline_query.get("id") or "")
        sender = inline_query.get("from") or {}
        user_id = int(sender.get("id", 0) or 0)
        if not query_id or not user_id:
            return
        self.acl.observe(sender)
        if self.acl.is_banned(user_id):
            self.api.answer_inline_query(query_id, [])
            return
        if not self.acl.is_allowed(user_id):
            self.api.answer_inline_query(
                query_id,
                [],
                {
                    "text": public_text(self.acl.language(user_id), "inline_apply"),
                    "start_parameter": "inline",
                },
            )
            return
        privileged_mode = self.acl.is_admin(user_id)
        if not privileged_mode and not self.acl.external_access_enabled:
            self.api.answer_inline_query(query_id, [])
            return
        url = normalize_status_url(str(inline_query.get("query") or ""))
        if not url:
            self.api.answer_inline_query(query_id, [])
            return
        if self.stop_event.is_set() or not self.inline_slots.acquire(blocking=False):
            self.api.answer_inline_query(query_id, [])
            return
        try:
            allowed = self.consume_inline_once(user_id, url)
        except Exception:
            self.inline_slots.release()
            raise
        if not allowed:
            self.inline_slots.release()
            self.api.answer_inline_query(query_id, [])
            return
        debug = privileged_mode and self.acl.debug_mode(user_id)
        try:
            future = self.inline_executor.submit(
                self._process_inline_query, query_id, url, debug, user_id
            )
        except Exception:
            self.inline_slots.release()
            raise
        with self.inline_futures_lock:
            self.inline_futures.add(future)
        future.add_done_callback(self._inline_done)

    def _inline_done(self, future: Future[Any]) -> None:
        with self.inline_futures_lock:
            self.inline_futures.discard(future)
        self.inline_slots.release()

    def wait_for_inline_idle(self, timeout: float = 5) -> None:
        with self.inline_futures_lock:
            futures = set(self.inline_futures)
        if futures:
            wait(futures, timeout=timeout)

    def _process_inline_query(self, query_id: str, url: str, debug: bool,
                              user_id: int | None = None) -> None:
        language = self.acl.language(user_id) if user_id is not None else ui_language()
        with language_scope(language):
            self._build_inline_query_response(query_id, url, debug, user_id)

    def _build_inline_query_response(self, query_id: str, url: str, debug: bool,
                                     user_id: int | None = None) -> None:
        try:
            if user_id is not None and not self.can_process(user_id):
                self.api.answer_inline_query(query_id, [])
                return
            now = time.monotonic()
            key = (url, debug, ui_language())
            with self.inline_cache_lock:
                self.inline_cache = {
                    cache_key: value
                    for cache_key, value in self.inline_cache.items()
                    if value[0] > now
                }
                cached = self.inline_cache.get(key)
            if cached:
                results = cached[1]
            else:
                results = build_inline_results(url, debug=debug)
                if INLINE_CACHE_SECONDS:
                    with self.inline_cache_lock:
                        self.inline_cache[key] = (
                            now + INLINE_CACHE_SECONDS,
                            results,
                        )
            if user_id is not None and not self.can_process(user_id):
                results = []
            self.api.answer_inline_query(query_id, results)
        except (OSError, requests.RequestException, RuntimeError, TypeError, ValueError):
            LOG.exception("Inline query processing failed")
            try:
                self.api.answer_inline_query(query_id, [])
            except (requests.RequestException, RuntimeError):
                LOG.warning("Could not answer failed inline query")

    def maybe_send_daily_report(self, force: bool = False) -> None:
        with language_scope(self.acl.language(self.acl.owner_id)):
            self._send_daily_report(force)

    def _send_daily_report(self, force: bool = False) -> None:
        owner_id = self.acl.owner_id
        records = self.acl.pending()
        if not owner_id:
            return
        if not force and not self.acl.daily_report_due():
            return
        now = time.time()
        report_date = bot_date(now)
        active, total = self.acl.usage_summary(report_date)
        text = {
            "zh": f"每日使用簡報（{report_date}）\n\n活躍用戶：{active}\n用量：{total}\n待審批：{len(records)}",
            "en": f"Daily usage report ({report_date})\n\nActive users: {active}\nUsage: {total}\nPending approvals: {len(records)}",
            "ja": f"日次利用レポート（{report_date}）\n\n利用ユーザー：{active}\n使用量：{total}\n審査待ち：{len(records)}",
            "zh-cn": (f"每日使用简报（{report_date}）\n\n活跃用户：{active}\n用量：{total}\n待审批：{len(records)}"),
        }[ui_language()]
        keyboard = pending_keyboard(records, 0) if records else None
        self.api.send_message(
            owner_id,
            text,
            reply_markup=keyboard,
        )
        self.acl.mark_daily_report(now)

    def users_page(
        self, records: list[dict[str, Any]], page: int
    ) -> tuple[str, dict[str, Any], int]:
        pages = max(1, (len(records) + MANAGEMENT_PAGE_SIZE - 1) // MANAGEMENT_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * MANAGEMENT_PAGE_SIZE
        title = {
            "zh": f"用戶列表（第 {page + 1}/{pages} 頁，共 {len(records)} 位）",
            "en": f"Users (page {page + 1}/{pages}, {len(records)} total)",
            "ja": f"ユーザー一覧（{page + 1}/{pages} ページ、計 {len(records)} 人）",
            "zh-cn": (f"用户列表（第 {page + 1}/{pages} 页，共 {len(records)} 位）"),
        }[ui_language()]
        lines = [
            title,
            {"zh": "ID｜狀態｜額度｜今日用量｜名稱", "en": "ID | Status | Limit | Usage today | Name", "ja": "ID｜状態｜上限｜本日の使用量｜名前", "zh-cn": ("ID｜状态｜额度｜今日用量｜名称")}[ui_language()],
        ]
        for record in records[start : start + MANAGEMENT_PAGE_SIZE]:
            user_id = int(record["user_id"])
            quota = record.get("quota")
            if user_id == self.acl.owner_id:
                status, quota_text = admin_text("owner"), admin_text("unlimited")
            elif quota is None:
                status, quota_text = admin_text("admin"), admin_text("unlimited")
            elif quota == -1:
                status, quota_text = admin_text("blocked"), "-1"
            elif record.get("pending"):
                status, quota_text = admin_text("pending"), "0"
            elif quota == 0:
                status, quota_text = admin_text("initialized"), "0"
            else:
                status, quota_text = admin_text("ordinary"), str(quota)
            name = user_name(record)
            if name != admin_text("not_named"):
                width = 0
                for index, character in enumerate(name):
                    width += 1 if character.isascii() and (
                        character.isalnum() or character == " "
                    ) else 2
                    if width > USER_LIST_NAME_WIDTH:
                        name = name[:index] + "…"
                        break
            name = html.escape(name)
            username = telegram_username(record)
            name_text = (
                f'<a href="https://t.me/{username}">{name}</a>'
                if username else name
            )
            usage = int(record.get("usage_count", 0) or 0)
            lines.append(
                f"<code>{user_id}</code>｜{status}｜{quota_text}｜{usage}｜{name_text}"
            )
        return "\n".join(lines), users_page_keyboard(page, len(records)), page

    def pending_page(
        self, records: list[dict[str, Any]], page: int
    ) -> tuple[str, dict[str, Any], int]:
        if not records:
            return admin_text("no_requests"), user_menu_keyboard(), 0
        pages = max(1, (len(records) + MANAGEMENT_PAGE_SIZE - 1) // MANAGEMENT_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * MANAGEMENT_PAGE_SIZE
        lines = [{
            "zh": f"待審批使用申請（第 {page + 1}/{pages} 頁，共 {len(records)} 筆）：",
            "en": f"Pending requests (page {page + 1}/{pages}, {len(records)} total):",
            "ja": f"審査待ちの申請（{page + 1}/{pages} ページ、計 {len(records)} 件）：",
            "zh-cn": (f"待审批使用申请（第 {page + 1}/{pages} 页，共 {len(records)} 笔）："),
        }[ui_language()]]
        for record in records[start : start + MANAGEMENT_PAGE_SIZE]:
            lines.append(f"• {user_label(record)}｜ID {record['user_id']}")
        return "\n".join(lines), pending_keyboard(records, page), page

    def edit_pending_page(
        self, chat_id: int, message_id: int, records: list[dict[str, Any]], page: int,
        empty_keyboard: dict[str, Any] | None = None,
    ) -> None:
        if records:
            text, keyboard, _ = self.pending_page(records, page)
            self.api.edit_message(chat_id, message_id, text, keyboard)
        elif empty_keyboard is not None:
            self.api.edit_message(chat_id, message_id, admin_text("no_requests"), empty_keyboard)
        else:
            self.api.edit_message(chat_id, message_id, admin_text("no_requests"))

    def user_search_result(
        self, target: int, actor_id: int | None = None
    ) -> tuple[str, dict[str, Any]] | None:
        record = next(
            (item for item in self.acl.records() if int(item["user_id"]) == target),
            None,
        )
        if record is None:
            return None
        quota = record.get("quota")
        if target == self.acl.owner_id:
            status, quota_text = admin_text("owner"), admin_text("unlimited")
        elif quota is None:
            status, quota_text = admin_text("admin"), admin_text("unlimited")
        elif quota == -1:
            status, quota_text = admin_text("blocked"), "-1"
        elif record.get("pending"):
            status, quota_text = admin_text("pending"), "0"
        elif quota == 0:
            status, quota_text = admin_text("initialized"), "0"
        else:
            status, quota_text = {"zh": "普通用戶", "zh-cn": "普通用户"}.get(ui_language(), admin_text("ordinary")), str(quota)
        can_modify = (
            actor_id is None
            or self.target_management_error(actor_id, target) is None
        )
        used = int(record.get("usage_count", 0) or 0)
        summary = {
            "zh": f"{user_label(record)}\nUser ID：{target}\n狀態：{status}\n每日額度：{quota_text}\n今日用量：{used} 次",
            "en": f"{user_label(record)}\nUser ID: {target}\nStatus: {status}\nDaily limit: {quota_text}\nUsage today: {used}",
            "ja": f"{user_label(record)}\nUser ID：{target}\n状態：{status}\n1日の上限：{quota_text}\n本日の使用量：{used} 回",
            "zh-cn": (f"{user_label(record)}\nUser ID：{target}\n状态：{status}\n每日额度：{quota_text}\n今日用量：{used} 次"),
        }[ui_language()]
        return (
            summary,
            searched_user_keyboard(
                record,
                self.acl.owner_id,
                can_modify,
                allow_admin=actor_id == self.acl.owner_id,
                default_daily_limit=self.acl.default_daily_limit,
            ),
        )

    def send_user_search_result(
        self, chat_id: int, message_id: int | None, target: int, actor_id: int
    ) -> None:
        result = self.user_search_result(target, actor_id)
        if result is None:
            self.api.send_message(
                chat_id,
                {
                    "zh": f"找不到 User ID {target}。此用戶可能尚未與 Bot 互動。",
                    "en": f"User ID {target} was not found. They may not have interacted with the bot yet.",
                    "ja": f"User ID {target} が見つかりません。まだ Bot を利用していない可能性があります。",
                    "zh-cn": (f"找不到 User ID {target}。此用户可能尚未与 Bot 互动。"),
                }[ui_language()],
                message_id,
                user_menu_keyboard(),
            )
            return
        text, keyboard = result
        self.api.send_message(chat_id, text, message_id, keyboard)

    def system_status_text(self, viewer_id: int) -> str:
        with language_scope(self.acl.language(viewer_id)):
            return self._system_status_text(viewer_id)

    def _system_status_text(self, viewer_id: int) -> str:
        records = self.acl.records()
        today = bot_date()
        ordinary = sum(
            1 for item in records
            if isinstance(item.get("quota"), int) and item.get("quota", 0) >= 1
        )
        administrators = sum(
            1 for item in records
            if item.get("quota") is None
            and int(item["user_id"]) != self.acl.owner_id
        )
        initialized = sum(
            1 for item in records
            if item.get("quota") == 0 and not item.get("pending")
        )
        pending = sum(1 for item in records if item.get("pending"))
        banned = sum(1 for item in records if item.get("quota") == -1)
        active_today = sum(
            1
            for item in records
            if item.get("usage_date") == today
            and int(item.get("usage_count", 0) or 0) > 0
        )
        interactions = sum(
            int(item.get("usage_count", 0) or 0)
            for item in records
            if item.get("usage_date") == today
        )
        exhausted = sum(
            1
            for item in records
            if isinstance(item.get("quota"), int)
            and item.get("quota", 0) >= 1
            and int(item.get("usage_count", 0) or 0)
            >= int(item.get("quota", 0))
        )
        queue_size = self.jobs.qsize()
        queue_percent = round(queue_size * 100 / MAX_QUEUE) if MAX_QUEUE else 0
        text = admin_text("status") + "\n\n"
        text += admin_text("status_runtime").format(
            service=admin_text("running" if any(worker.is_alive() for worker in self.workers) else "worker_stopped"),
            uptime=format_duration(time.time() - self.started_at),
            queue_size=queue_size, queue_limit=MAX_QUEUE, queue_percent=queue_percent,
        )
        text += admin_text("status_users").format(
            total=len(records), ordinary=ordinary, administrators=administrators,
            initialized=initialized, pending=pending, banned=banned,
            active_today=active_today, interactions=interactions, exhausted=exhausted,
        )
        return text.rstrip()

    def handle_callback(self, callback: dict[str, Any]) -> None:
        user_id = int((callback.get("from") or {}).get("id", 0) or 0)
        with language_scope(self.acl.language(user_id)):
            self._handle_callback(callback)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from") or {}
        user_id = int(sender.get("id", 0) or 0)
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", user_id) or user_id)
        message_id = int(message.get("message_id", 0) or 0)
        if not user_id or not callback_id:
            return
        self.acl.observe(sender)
        language = self.acl.language(user_id)
        if self.acl.is_banned(user_id):
            return
        is_owner = user_id == self.acl.owner_id
        is_admin = self.acl.is_admin(user_id)

        if data.startswith("lang:"):
            if is_admin and not self.is_private_management_chat(user_id, chat_id):
                self.api.answer_callback(callback_id, admin_text("private_only"), alert=True)
                return
            selected = data.split(":", 1)[1]
            try:
                self.acl.set_language(user_id, selected)
            except ValueError:
                self.api.answer_callback(callback_id, public_text(language, "unsupported_language"), alert=True)
                return
            self.clear_pending_input(user_id)
            is_allowed = self.acl.is_allowed(user_id)
            text_key = "start_owner" if is_admin else "start_allowed"
            text = (
                public_text(selected, text_key)
                if is_allowed
                else access_request_text(user_id, selected)
            )
            with language_scope(selected):
                keyboard = start_keyboard(selected, is_admin, is_allowed)
            self.api.edit_message(
                chat_id,
                message_id,
                text,
                keyboard,
            )
            self.api.answer_callback(
                callback_id, public_text(selected, "language_set")
            )
            return

        if data.startswith("public:"):
            destination = data.split(":", 1)[1]
            if is_admin and not self.is_private_management_chat(user_id, chat_id):
                self.api.answer_callback(callback_id, admin_text("private_only"), alert=True)
                return
            self.clear_pending_input(user_id)
            back_keyboard = {"inline_keyboard": [[{
                "text": public_text(language, "back"),
                "callback_data": "public:main",
            }]]}
            if destination == "language":
                text = public_text(language, "choose_language")
                keyboard = {
                    "inline_keyboard": [language_row(language)]
                    + back_keyboard["inline_keyboard"]
                }
                self.api.edit_message(chat_id, message_id, text, keyboard)
            elif destination == "help":
                self.api.edit_message(
                    chat_id,
                    message_id,
                    public_help_text(language),
                    back_keyboard,
                    parse_mode="HTML",
                )
            elif destination == "main":
                is_allowed = self.acl.is_allowed(user_id)
                text = (
                    public_text(
                        language,
                        "start_owner"
                        if is_admin
                        else "start_allowed",
                    )
                    if is_allowed
                    else access_request_text(user_id, language)
                )
                self.api.edit_message(
                    chat_id,
                    message_id,
                    text,
                    start_keyboard(
                        language,
                        is_admin,
                        is_allowed,
                    ),
                )
            else:
                self.api.answer_callback(callback_id, admin_text("invalid_menu") if is_admin else public_text(language, "failed"), alert=True)
                return
            self.api.answer_callback(callback_id, "")
            return

        if data == "apply":
            result = self.acl.request_access(user_id)
            responses = {
                "created": public_text(language, "apply_created"),
                "pending": public_text(language, "apply_pending"),
                "allowed": public_text(language, "apply_allowed"),
                "auto_approved": public_text(language, "apply_auto_approved"),
            }
            if result == "auto_approved" and message_id:
                self.api.edit_message(
                    chat_id,
                    message_id,
                    public_text(language, "start_allowed"),
                    start_keyboard(language, False, True),
                )
            self.api.answer_callback(callback_id, responses[result], alert=True)
            return

        if not is_admin:
            self.api.answer_callback(callback_id, admin_text("admin_only"), alert=True)
            return
        if not self.is_private_management_chat(user_id, chat_id):
            self.api.answer_callback(
                callback_id, admin_text("private_only"), alert=True
            )
            return

        if data.startswith("nav:"):
            destination = data.split(":", 1)[1]
            self.clear_pending_input(user_id)
            if destination == "main":
                text, keyboard = admin_text("menu"), owner_keyboard()
            elif destination == "users":
                text, keyboard = admin_text("users"), user_menu_keyboard()
            elif destination == "cookies":
                if not is_owner:
                    self.api.answer_callback(
                        callback_id, admin_text("owner_only_cookies"), alert=True
                    )
                    return
                text, keyboard = admin_text("cookies"), cookie_menu_keyboard(
                    self.acl.ordinary_user_cookies_enabled
                )
            elif destination == "status":
                text = self.system_status_text(user_id)
                keyboard = status_keyboard(is_owner)
            elif destination == "advanced":
                if not is_owner:
                    self.api.answer_callback(
                        callback_id, admin_text("advanced_owner_only"), alert=True
                    )
                    return
                text = admin_text("advanced")
                keyboard = advanced_status_keyboard(
                    self.acl.debug_mode(user_id),
                    self.acl.external_access_enabled,
                    self.acl.auto_approve_enabled,
                    is_owner,
                    self.acl.default_daily_limit,
                )
            elif destination == "defaultquota":
                if not is_owner:
                    self.api.answer_callback(callback_id, admin_text("advanced_owner_only"), alert=True)
                    return
                self.pending_cookie_uploads.discard(user_id)
                self.pending_default_quotas[user_id] = None
                text = admin_text("default_limit_prompt").format(limit=self.acl.default_daily_limit)
                keyboard = default_limit_keyboard()
            elif destination == "help":
                text, keyboard = owner_help_text(is_owner), owner_keyboard()
            elif destination == "userlist":
                text, keyboard, _ = self.users_page(self.acl.records(), 0)
            elif destination == "requests":
                text, keyboard, _ = self.pending_page(self.acl.pending(), 0)
            elif destination == "finduser":
                self.pending_user_searches.add(user_id)
                text, keyboard = admin_text("find_user"), user_menu_keyboard()
            elif destination == "cookieupload":
                if not is_owner:
                    self.api.answer_callback(
                        callback_id, admin_text("owner_only_cookies"), alert=True
                    )
                    return
                self.pending_cookie_uploads.add(user_id)
                text, keyboard = cookie_upload_text(), cookie_menu_keyboard(
                    self.acl.ordinary_user_cookies_enabled
                )
            elif destination == "cookiehelp":
                if not is_owner:
                    self.api.answer_callback(
                        callback_id, admin_text("owner_only_cookies"), alert=True
                    )
                    return
                text, keyboard = cookie_help_text(), cookie_menu_keyboard(
                    self.acl.ordinary_user_cookies_enabled
                )
            elif destination == "clearcookies":
                if not is_owner:
                    self.api.answer_callback(
                        callback_id, admin_text("owner_only_cookies"), alert=True
                    )
                    return
                COOKIES_PATH.unlink(missing_ok=True)
                COOKIE_ALERT_PATH.unlink(missing_ok=True)
                self.pending_cookie_uploads.discard(user_id)
                text, keyboard = admin_text("cookie_cleared"), cookie_menu_keyboard(
                    self.acl.ordinary_user_cookies_enabled
                )
            else:
                self.api.answer_callback(callback_id, admin_text("invalid_menu"), alert=True)
                return
            if destination == "userlist":
                self.api.edit_message(
                    chat_id, message_id, text, keyboard, parse_mode="HTML"
                )
            else:
                self.api.edit_message(chat_id, message_id, text, keyboard)
            self.api.answer_callback(callback_id, "")
            return

        if data == "noop:0":
            self.api.answer_callback(callback_id, "")
            return

        try:
            action, target_text, *extra = data.split(":")
            target = int(target_text)
        except (ValueError, TypeError):
            self.api.answer_callback(callback_id, admin_text("invalid_action"), alert=True)
            return

        if action == "defaultquota":
            if not is_owner:
                self.api.answer_callback(callback_id, admin_text("advanced_owner_only"), alert=True)
                return
            if not 1 <= target <= MAX_DAILY_LIMIT:
                self.api.answer_callback(callback_id, admin_text("default_limit_range"), alert=True)
                return
            if self.pending_default_quotas.get(user_id) != target:
                self.api.answer_callback(callback_id, admin_text("invalid_action"), alert=True)
                return
            changed = self.acl.set_default_daily_limit(user_id, target)
            self.pending_default_quotas.pop(user_id, None)
            self.api.edit_message(
                chat_id, message_id,
                admin_text("default_limit_changed").format(limit=target, count=changed),
                advanced_status_keyboard(
                    self.acl.debug_mode(user_id), self.acl.external_access_enabled,
                    self.acl.auto_approve_enabled, is_owner, self.acl.default_daily_limit,
                ),
            )
            self.api.answer_callback(callback_id, admin_text("permission_changed"))
            return

        self.clear_pending_input(user_id)

        requested_at = None
        if action in {"approve", "deny"}:
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            application = self.acl.data["pending_applications"].get(str(target))
            if action == "approve" and not application:
                self.api.answer_callback(callback_id, admin_text("approve_missing"), alert=True)
                return
            try:
                requested_at = float(extra[1])
            except (IndexError, ValueError, TypeError):
                requested_at = None
            if (requested_at is None or not application or
                    requested_at != application.get("requested_at")):
                self.edit_pending_page(chat_id, message_id, self.acl.pending(), 0, user_menu_keyboard())
                self.api.answer_callback(callback_id, admin_text("requests_changed"), alert=True)
                return

        if action == "userspage":
            if target < 0:
                self.api.answer_callback(callback_id, admin_text("menu_invalid_page"), alert=True)
                return
            text, keyboard, _ = self.users_page(self.acl.records(), target)
            self.api.edit_message(
                chat_id, message_id, text, keyboard, parse_mode="HTML"
            )
            self.api.answer_callback(callback_id, "")
            return
        if action in {"createuser", "cancelcreate"}:
            if action == "cancelcreate":
                self.api.edit_message(
                    chat_id, message_id, admin_text("create_cancelled"), user_menu_keyboard()
                )
                self.api.answer_callback(callback_id, admin_text("cancelled"))
                return
            if not extra:
                self.api.answer_callback(callback_id, admin_text("default_quota_missing"), alert=True)
                return
            try:
                quota = int(extra[0])
                if quota < -1 or quota > MAX_DAILY_LIMIT:
                    raise ValueError
            except (ValueError, TypeError):
                self.api.answer_callback(callback_id, admin_text("default_quota_invalid"), alert=True)
                return
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            if not self.acl.ensure_managed_user(target, quota):
                self.api.answer_callback(
                    callback_id, admin_text("user_exists"), alert=True
                )
            else:
                self.api.answer_callback(callback_id, admin_text("user_created"))
            result = self.user_search_result(target, user_id)
            if result:
                result_text, keyboard = result
                self.api.edit_message(chat_id, message_id, result_text, keyboard)
            return
        if action in {"confirmquota", "cancelquota"}:
            if action == "cancelquota":
                self.api.edit_message(
                    chat_id, message_id, admin_text("change_cancelled"), user_menu_keyboard()
                )
                self.api.answer_callback(callback_id, admin_text("cancelled"))
                return
            if not extra:
                self.api.answer_callback(callback_id, admin_text("permission_missing"), alert=True)
                return
            try:
                quota = int(extra[0])
                if quota < -1 or quota > MAX_DAILY_LIMIT:
                    raise ValueError
            except (ValueError, TypeError):
                self.api.answer_callback(callback_id, admin_text("permission_invalid"), alert=True)
                return
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            if not self.acl.has_user(target):
                self.api.answer_callback(
                    callback_id, admin_text("user_missing"), alert=True
                )
                return
            self.acl.set_quota(target, quota)
            self.api.answer_callback(callback_id, admin_text("permission_changed"))
            result = self.user_search_result(target, user_id)
            if result:
                result_text, keyboard = result
                self.api.edit_message(chat_id, message_id, result_text, keyboard)
            return
        if action == "findresult":
            if not is_telegram_user_id(target):
                self.api.answer_callback(callback_id, admin_text("id_invalid"), alert=True)
                return
            result = self.user_search_result(target, user_id)
            if result is None:
                self.api.edit_message(
                    chat_id, message_id, {"zh": f"找不到 User ID {target}。", "en": f"User ID {target} not found.", "ja": f"User ID {target} が見つかりません。", "zh-cn": (f"找不到 User ID {target}。")}[ui_language()], user_menu_keyboard()
                )
            else:
                text, keyboard = result
                self.api.edit_message(chat_id, message_id, text, keyboard)
            self.api.answer_callback(callback_id, "")
            return
        if action == "statusrefresh":
            self.api.edit_message(
                chat_id,
                message_id,
                self.system_status_text(user_id),
                status_keyboard(is_owner),
            )
            self.api.answer_callback(callback_id, "")
            return
        if action in {"debugtoggle", "externaltoggle", "autoapprovetoggle"}:
            toggle, error_key, label = {
                "debugtoggle": (self.acl.toggle_debug_mode, "advanced_owner_only", "implementation"),
                "externaltoggle": (self.acl.toggle_external_access, "access_owner_only", "access_switch"),
                "autoapprovetoggle": (self.acl.toggle_auto_approve, "auto_owner_only", "auto_approve"),
            }[action]
            if not is_owner:
                self.api.answer_callback(
                    callback_id, admin_text(error_key), alert=True
                )
                return
            enabled = toggle(user_id)
            self.api.edit_message(
                chat_id,
                message_id,
                admin_text("advanced"),
                advanced_status_keyboard(
                    self.acl.debug_mode(user_id),
                    self.acl.external_access_enabled,
                    self.acl.auto_approve_enabled,
                    is_owner,
                    self.acl.default_daily_limit,
                ),
            )
            state_key = ("open" if enabled else "paused") if action == "externaltoggle" else ("on" if enabled else "off")
            state = admin_text(state_key)
            notice = f"{admin_text(label)}已{state}。" if ui_language() == "zh" else f"{admin_text(label)}: {state}"
            self.api.answer_callback(callback_id, notice)
            return
        if action == "ordinarycookiestoggle":
            if not is_owner:
                self.api.answer_callback(
                    callback_id,
                    admin_text("cookie_owner_only"),
                    alert=True,
                )
                return
            enabled = self.acl.toggle_ordinary_user_cookies(user_id)
            self.api.edit_message(
                chat_id,
                message_id,
                admin_text("cookies"),
                cookie_menu_keyboard(enabled),
            )
            self.api.answer_callback(
                callback_id,
                f"Cookies 開關已{'開啟' if enabled else '關閉'}。" if ui_language() == "zh" else f'{admin_text("cookie_switch")}: {admin_text("on" if enabled else "off")}',
            )
            return
        if action == "requestspage":
            if target < 0:
                self.api.answer_callback(callback_id, admin_text("menu_invalid_page"), alert=True)
                return
            self.edit_pending_page(chat_id, message_id, self.acl.pending(), target)
            self.api.answer_callback(callback_id, "")
            return

        if action == "approvepage":
            if target < 0:
                self.api.answer_callback(callback_id, admin_text("menu_invalid_page"), alert=True)
                return
            records = self.acl.pending()
            if not extra or extra[0] != pending_page_fingerprint(records, target):
                self.edit_pending_page(chat_id, message_id, records, target, user_menu_keyboard())
                self.api.answer_callback(callback_id, admin_text("requests_changed"), alert=True)
                return
            start = target * MANAGEMENT_PAGE_SIZE
            page_records = records[start : start + MANAGEMENT_PAGE_SIZE]
            approved = 0
            for record in page_records:
                applicant_id = int(record["user_id"])
                if not self.acl.approve(applicant_id):
                    continue
                approved += 1
                try:
                    self.api.send_message(
                        applicant_id,
                        public_text(
                            self.acl.language(applicant_id),
                            "approved",
                            limit=self.acl.default_daily_limit,
                        ),
                    )
                except (requests.RequestException, RuntimeError):
                    LOG.exception(
                        "Could not notify approved user %s", applicant_id
                    )
            self.edit_pending_page(chat_id, message_id, self.acl.pending(), target, user_menu_keyboard())
            self.api.answer_callback(callback_id, {"zh": f"已通過 {approved} 筆申請。", "en": f"Approved {approved} requests.", "ja": f"{approved} 件の申請を承認しました。", "zh-cn": (f"已通过 {approved} 笔申请。")}[ui_language()])
            return

        if action == "approve":
            if not self.acl.approve(target, requested_at=requested_at):
                self.api.answer_callback(callback_id, admin_text("approve_missing"), alert=True)
                return
            self.api.answer_callback(callback_id, {"zh": f"已通過 {target}。", "en": f"Approved {target}.", "ja": f"{target} を承認しました。", "zh-cn": (f"已通过 {target}。")}[ui_language()])
            try:
                self.api.send_message(
                    target,
                    public_text(
                        self.acl.language(target),
                        "approved",
                        limit=self.acl.default_daily_limit,
                    ),
                )
            except (requests.RequestException, RuntimeError):
                LOG.exception("Could not notify approved user %s", target)
            if message_id and extra:
                try:
                    page = max(0, int(extra[0]))
                except (ValueError, TypeError):
                    page = 0
                self.edit_pending_page(chat_id, message_id, self.acl.pending(), page)
        elif action == "deny":
            if not self.acl.deny(target, requested_at=requested_at):
                self.api.answer_callback(callback_id, admin_text("requests_changed"), alert=True)
                return
            self.api.answer_callback(callback_id, {"zh": f"已拒絕 {target}。", "en": f"Rejected {target}.", "ja": f"{target} を拒否しました。", "zh-cn": (f"已拒绝 {target}。")}[ui_language()])
            if message_id and extra:
                try:
                    page = max(0, int(extra[0]))
                except (ValueError, TypeError):
                    page = 0
                self.edit_pending_page(chat_id, message_id, self.acl.pending(), page)
        elif action in {"quotamenu", "limitmenu"}:
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            self.api.answer_callback(callback_id, admin_text("choose_access"))
            self.api.edit_message(
                chat_id,
                message_id,
                {"zh": f"修改 User ID {target} 的用戶權限：", "en": f"Change access for User ID {target}:", "ja": f"User ID {target} の権限を変更：", "zh-cn": (f"修改 User ID {target} 的用户权限：")}[ui_language()],
                quota_choices_keyboard(target, allow_admin=is_owner, default_daily_limit=self.acl.default_daily_limit),
            )
        elif action in {"quota", "limit", "ban", "unban"}:
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            if action == "ban":
                quota, label = -1, f'{admin_text("blocked")} (-1)'
            elif action == "unban":
                quota, label = 0, f'{admin_text("initialized")} (0)'
            else:
                if not extra:
                    self.api.answer_callback(callback_id, admin_text("quota_missing"), alert=True)
                    return
                value = extra[0]
                if value in {"unlimited", "none"}:
                    if not is_owner:
                        self.api.answer_callback(
                            callback_id, admin_text("owner_only_admin"), alert=True
                        )
                        return
                    quota, label = None, admin_text("admin_unlimited")
                elif value == "blocked":
                    quota, label = -1, f'{admin_text("blocked")} (-1)'
                else:
                    try:
                        quota = int(value)
                    except (ValueError, TypeError):
                        self.api.answer_callback(callback_id, admin_text("quota_invalid"), alert=True)
                        return
                    label = f'{admin_text("initialized")} (0)' if quota == 0 else str(quota)
            try:
                self.acl.set_quota(target, quota)
            except ValueError:
                self.api.answer_callback(
                    callback_id, admin_text("quota_range"), alert=True
                )
                return
            self.api.answer_callback(callback_id, {"zh": f"已設為 {label}。", "en": f"Set to {label}.", "ja": f"{label} に設定しました。", "zh-cn": (f"已设为 {label}。")}[ui_language()])
            result = self.user_search_result(target, user_id)
            if message_id and result:
                text, keyboard = result
                self.api.edit_message(chat_id, message_id, text, keyboard)
        else:
            self.api.answer_callback(callback_id, admin_text("invalid_action"), alert=True)

    def handle_document(
        self, chat_id: int, message_id: int, user_id: int, document: dict[str, Any]
    ) -> None:
        if not self.is_private_management_chat(user_id, chat_id):
            if user_id == self.acl.owner_id:
                self.api.send_message(
                    chat_id, {"zh": "Cookies 只允許在 Bot 私聊匯入。", "en": "Import Cookies only in a private chat with the bot.", "ja": "Cookies の取り込みは Bot との個別チャットでのみ可能です。", "zh-cn": ("Cookies 只允许在 Bot 私聊导入。")}[ui_language()], message_id
                )
            return
        if user_id != self.acl.owner_id:
            self.api.send_message(
                chat_id, public_text(self.acl.language(user_id), "url_only"), message_id, remove_keyboard()
            )
            return
        if user_id not in self.pending_cookie_uploads:
            self.api.send_message(
                chat_id,
                {"zh": "請先點選「匯入 Cookies」，再上傳 Netscape 格式 cookies.txt。", "en": "Select Import Cookies before uploading a Netscape-format cookies.txt file.", "ja": "先に Cookies を取り込むを選択し、Netscape 形式の cookies.txt をアップロードしてください。", "zh-cn": ("请先点选「导入 Cookies」，再上传 Netscape 格式 cookies.txt。")}[ui_language()],
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
            return
        file_size = int(document.get("file_size", 0) or 0)
        file_id = str(document.get("file_id", ""))
        if not file_id or file_size > MAX_COOKIE_BYTES:
            self.api.send_message(
                chat_id,
                {"zh": "Cookies 檔案無效或超過 1 MB。", "en": "Invalid Cookies file or larger than 1 MB.", "ja": "Cookies ファイルが無効か、1 MB を超えています。", "zh-cn": ("Cookies 文件无效或超过 1 MB。")}[ui_language()],
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
            return
        try:
            content = self.api.download_file(file_id, MAX_COOKIE_BYTES)
            cookie_text = validate_cookie_file(content)
            save_cookie_file(cookie_text)
        except (OSError, ValueError, requests.RequestException, RuntimeError) as error:
            LOG.warning("Cookie import rejected: %s", error)
            self.api.send_message(
                chat_id,
                {
                    "zh": f"匯入失敗：{error}",
                    "en": "Import failed: " + {
                        "Cookies 檔案必須小於 1 MB。": "Cookies file must be under 1 MB.",
                        "Cookies 檔案必須是 UTF-8 文字格式。": "Cookies file must be UTF-8 text.",
                        "只接受 Netscape 格式的 cookies.txt。": "Only Netscape-format cookies.txt is accepted.",
                        "檔案中找不到 X/Twitter 的有效 Cookie 記錄。": "No valid X/Twitter Cookie entry was found.",
                    }.get(str(error), "Invalid Cookies file or download failed."),
                    "ja": "取り込み失敗：" + {
                        "Cookies 檔案必須小於 1 MB。": "Cookies ファイルは 1 MB 未満にしてください。",
                        "Cookies 檔案必須是 UTF-8 文字格式。": "Cookies ファイルは UTF-8 テキストにしてください。",
                        "只接受 Netscape 格式的 cookies.txt。": "Netscape 形式の cookies.txt のみ受け付けます。",
                        "檔案中找不到 X/Twitter 的有效 Cookie 記錄。": "有効な X/Twitter の Cookie が見つかりません。",
                    }.get(str(error), "Cookies ファイルが無効か、ダウンロードに失敗しました。"),
                    "zh-cn": "导入失败：" + {
                        "Cookies 檔案必須小於 1 MB。": "Cookies 文件必须小于 1 MB。",
                        "Cookies 檔案必須是 UTF-8 文字格式。": "Cookies 文件必须是 UTF-8 文本。",
                        "只接受 Netscape 格式的 cookies.txt。": "只接受 Netscape 格式的 cookies.txt。",
                        "檔案中找不到 X/Twitter 的有效 Cookie 記錄。": "文件中没有有效的 X/Twitter Cookie。",
                    }.get(str(error), "Cookies 文件无效或下载失败。"),
                }[ui_language()],
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
            return
        self.pending_cookie_uploads.discard(user_id)
        self.api.send_message(
            chat_id,
            {"zh": "X/Twitter Cookies 已匯入並立即生效。Telegram 中的原始文件可自行刪除。", "en": "X/Twitter Cookies imported and active. You can delete the original document from Telegram.", "ja": "X/Twitter の Cookies を取り込み、すぐに反映しました。Telegram の元ファイルは削除できます。", "zh-cn": ("X/Twitter Cookies 已导入并立即生效。Telegram 中的原始文件可自行删除。")}[ui_language()],
            message_id,
            cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
        )

    def handle_owner_command(
        self,
        chat_id: int,
        message_id: int,
        actor_id: int,
        command: str,
        argument: str,
    ) -> None:
        with language_scope(self.acl.language(actor_id)):
            self._handle_owner_command(chat_id, message_id, actor_id, command, argument)

    def _handle_owner_command(
        self, chat_id: int, message_id: int, actor_id: int,
        command: str, argument: str,
    ) -> None:
        is_owner = actor_id == self.acl.owner_id
        if command == "/menu":
            self.pending_user_searches.discard(actor_id)
            self.api.remove_reply_keyboard(chat_id)
            self.api.send_message(
                chat_id,
                admin_text("menu_ready"),
                message_id,
                owner_keyboard(),
            )
        elif command == "/usermenu":
            self.pending_user_searches.discard(actor_id)
            self.api.send_message(
                chat_id, admin_text("users"), message_id, user_menu_keyboard()
            )
        elif command == "/cookiemenu":
            self.pending_user_searches.discard(actor_id)
            self.api.send_message(
                chat_id,
                admin_text("cookies"),
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
        elif command in {"/allow", "/deny", "/ban", "/unban"}:
            try:
                target = int(argument)
                if not is_telegram_user_id(target):
                    raise ValueError
            except ValueError:
                self.api.send_message(chat_id, {"zh": f"用法：{command} <User ID>", "en": f"Usage: {command} <User ID>", "ja": f"使い方：{command} <User ID>", "zh-cn": (f"用法：{command} <User ID>")}[ui_language()], message_id)
                return
            error = self.target_management_error(actor_id, target)
            if error:
                self.api.send_message(chat_id, error, message_id)
                return
            if command == "/allow":
                self.acl.add(target)
                response = {"zh": f"已允許 {target}。", "en": f"Allowed {target}.", "ja": f"{target} を許可しました。", "zh-cn": (f"已允许 {target}。")}[ui_language()]
            elif command == "/deny":
                self.acl.remove(target)
                response = {"zh": f"已移除 {target}。", "en": f"Removed {target}.", "ja": f"{target} を削除しました。", "zh-cn": (f"已移除 {target}。")}[ui_language()]
            elif command == "/ban":
                try:
                    self.acl.ban(target)
                except ValueError:
                    self.api.send_message(chat_id, admin_text("owner_cannot_ban"), message_id)
                    return
                response = {"zh": f"已永久封鎖 {target}。", "en": f"Blocked {target}.", "ja": f"{target} をブロックしました。", "zh-cn": (f"已永久封禁 {target}。")}[ui_language()]
            else:
                self.acl.unban(target)
                response = {"zh": f"已解除 {target} 的永久封鎖。", "en": f"Unblocked {target}.", "ja": f"{target} のブロックを解除しました。", "zh-cn": (f"已解除 {target} 的永久封禁。")}[ui_language()]
            self.api.send_message(chat_id, response, message_id)
        elif command == "/users":
            records = self.acl.records()
            text, keyboard, _ = self.users_page(records, 0)
            self.api.send_message(
                chat_id,
                text,
                message_id,
                keyboard,
                parse_mode="HTML",
            )
        elif command == "/requests":
            records = self.acl.pending()
            if not records:
                self.api.send_message(chat_id, admin_text("no_requests"), message_id)
            else:
                text, keyboard, _ = self.pending_page(records, 0)
                self.api.send_message(chat_id, text, message_id, keyboard)
        elif command == "/finduser":
            self.pending_user_searches.add(actor_id)
            self.api.send_message(
                chat_id,
                admin_text("find_user"),
                message_id,
                user_menu_keyboard(),
            )
        elif command == "/limit":
            try:
                target_text, limit_text = argument.split(maxsplit=1)
                target = int(target_text)
                quota = None if limit_text.lower() in {"unlimited", "none"} else int(limit_text)
                error = self.target_management_error(actor_id, target)
                if error:
                    self.api.send_message(chat_id, error, message_id)
                    return
                if quota is None and not is_owner:
                    self.api.send_message(
                        chat_id, admin_text("owner_only_admin"), message_id
                    )
                    return
                self.acl.set_quota(target, quota)
            except (ValueError, TypeError):
                self.api.send_message(
                    chat_id,
                    {"zh": "用法：/limit <User ID> <-1|0|每日次數|unlimited>。", "en": "Usage: /limit <User ID> <-1|0|daily limit|unlimited>.", "ja": "使い方：/limit <User ID> <-1|0|1日の上限|unlimited>。", "zh-cn": ("用法：/limit <User ID> <-1|0|每日次数|unlimited>。")}[ui_language()],
                    message_id,
                )
                return
            self.api.send_message(
                chat_id,
                {"zh": f"已將 {target} 設為 {admin_text('admin_unlimited') if quota is None else quota}。", "en": f"Set {target} to {admin_text('admin_unlimited') if quota is None else quota}.", "ja": f"{target} を {admin_text('admin_unlimited') if quota is None else quota} に設定しました。", "zh-cn": (f"已将 {target} 设为 {admin_text('admin_unlimited') if quota is None else quota}。")}[ui_language()],
                message_id,
            )
        elif command == "/status":
            self.api.send_message(
                chat_id,
                self.system_status_text(actor_id),
                message_id,
                status_keyboard(is_owner),
            )
        elif command == "/help":
            self.api.send_message(
                chat_id,
                owner_help_text(is_owner),
                message_id,
                owner_keyboard(),
            )
        elif command == "/cookies":
            self.pending_cookie_uploads.add(actor_id)
            self.api.send_message(
                chat_id,
                cookie_upload_text(),
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
        elif command == "/cookiehelp":
            self.api.send_message(
                chat_id,
                cookie_help_text(),
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
        elif command == "/clearcookies":
            COOKIES_PATH.unlink(missing_ok=True)
            COOKIE_ALERT_PATH.unlink(missing_ok=True)
            self.pending_cookie_uploads.discard(actor_id)
            self.api.send_message(
                chat_id,
                admin_text("cookie_cleared"),
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
        elif command == "/cancel":
            self.clear_pending_input(actor_id)
            self.api.send_message(
                chat_id, admin_text("cancelled"), message_id, owner_keyboard()
            )
        else:
            self.api.send_message(
                chat_id,
                owner_help_text(is_owner),
                message_id,
                owner_keyboard(),
            )

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                chat_id, message_id, user_id, url = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                needs_notice = False
                try:
                    self.process_url(chat_id, message_id, user_id, url)
                except Exception:
                    LOG.exception("Tweet processing failed")
                    needs_notice = True
                # Retry the failure notice, not partially delivered media. Keep
                # the durable job on shutdown so the next start can recover it.
                while True:
                    try:
                        if needs_notice and self.can_process(user_id):
                            if self.stop_event.is_set():
                                break
                            self.api.send_message(
                                chat_id,
                                public_text(self.acl.language(user_id), "failed"),
                                message_id,
                            )
                            needs_notice = False
                        self.acl.finish_job(chat_id, message_id)
                        break
                    except TelegramAPIError as error:
                        if error.status_code in {400, 403}:
                            needs_notice = False
                            continue
                        LOG.exception("Could not send failure response; retrying")
                    except Exception:
                        LOG.exception("Could not finish failed job; retrying")
                    if self.stop_event.wait(5):
                        break
            finally:
                self.jobs.task_done()

    def notify_cookie_failure(self) -> None:
        with self.cookie_alert_lock, language_scope(self.acl.language(self.acl.owner_id)):
            self._notify_cookie_failure()

    def _notify_cookie_failure(self) -> None:
        owner_id = self.acl.owner_id
        if not owner_id or not cookie_alert_due():
            return
        try:
            self.api.send_message(
                owner_id,
                {
                    "zh": "X/Twitter Cookies 可能已失效，登入型媒體抓取遭到拒絕。請使用「匯入 Cookies」更新 cookies.txt。此通知 24 小時內不會重複發送。",
                    "en": "X/Twitter Cookies may have expired; authenticated media access was denied. Use Import Cookies to update cookies.txt. This notice is limited to once per 24 hours.",
                    "ja": "X/Twitter の Cookies が失効し、ログインが必要なメディア取得が拒否された可能性があります。Cookies を取り込むから cookies.txt を更新してください。この通知は24時間以内に繰り返されません。",
                    "zh-cn": ("X/Twitter Cookies 可能已失效，登入型媒体抓取遭到拒绝。请使用「导入 Cookies」更新 cookies.txt。此通知 24 小时内不会重复发送。"),
                }[ui_language()],
                reply_markup=start_keyboard(
                    self.acl.language(owner_id),
                    True,
                    True,
                ),
            )
            record_cookie_alert()
        except (OSError, requests.RequestException, RuntimeError):
            LOG.exception("Could not notify owner about invalid cookies")

    def process_url(
        self, chat_id: int, message_id: int, user_id: int, url: str
    ) -> None:
        with language_scope(self.acl.language(user_id)):
            self._process_url(chat_id, message_id, user_id, url)

    def _process_url(
        self, chat_id: int, message_id: int, user_id: int, url: str
    ) -> None:
        if not self.can_process(user_id):
            return
        if not media_disk_available():
            raise RuntimeError("Media storage is temporarily unavailable")
        self.api.send_action(chat_id, "upload_document")
        root_tweet = fetch_fxtwitter(url)
        effective_url = fxtwitter_tweet_url(root_tweet) if root_tweet else None
        effective_url = effective_url or url
        if root_tweet:
            text, author, author_url = fxtwitter_text_author(root_tweet)
            if not text:
                text, author, author_url = fetch_tweet_text(effective_url)
        else:
            text, author, author_url = fetch_tweet_text(effective_url)
        with media_temporary_directory() as directory:
            files: list[Path] = []
            extractor_log = ""
            method = ""
            cookie_invalid = False
            fallback_oversized_videos = 0
            if root_tweet and fxtwitter_media(root_tweet):
                files, extractor_log, fallback_oversized_videos = (
                    download_fxtwitter_media(root_tweet, directory)
                )
                if files:
                    method = "FxTwitter 直連"
            if not files:
                files, extractor_log, method, cookie_invalid = download_media(
                    effective_url,
                    directory,
                    allow_cookies=(
                        self.acl.is_admin(user_id)
                        or self.acl.ordinary_user_cookies_enabled
                    ),
                )
            if cookie_invalid:
                self.notify_cookie_failure()
            fallback_tweet = root_tweet
            if not files or not text:
                fallback_tweet = fallback_tweet or fetch_fxtwitter(effective_url)
            if fallback_tweet and not text:
                text, author, author_url = fxtwitter_text_author(fallback_tweet)
            if fallback_tweet and not files and fallback_tweet is not root_tweet:
                files, extractor_log, fallback_oversized_videos = download_fxtwitter_media(
                    fallback_tweet, directory
                )
                if files:
                    method = "FxTwitter 備援"
            files, rejected = trim_files(files)
            if not files and not text and not rejected and not fallback_oversized_videos:
                LOG.warning("Post text and media unavailable after extraction: %s: %s", effective_url, extractor_log)
                if self.can_process(user_id):
                    self.api.send_message(
                        chat_id,
                        public_text(self.acl.language(user_id), "post_unavailable"),
                        message_id,
                    )
                return
            caption = media_caption(
                author,
                author_url,
                text,
                effective_url,
                self.acl.debug_mode(user_id),
                method,
            )
            previews = [
                prepare_image(path)
                if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                else path
                for path in files
            ]
            if not self.can_process(user_id):
                return
            if previews:
                try:
                    self.api.send_previews(
                        chat_id, previews, caption, parse_mode="HTML"
                    )
                except (OSError, requests.RequestException, RuntimeError):
                    LOG.exception("Could not send media previews")
                    if not self.can_process(user_id):
                        return
                    self.api.send_message(
                        chat_id,
                        caption
                        + "\n\n"
                        + html.escape(
                            public_text(self.acl.language(user_id), "preview_failed")
                        ),
                        message_id,
                        parse_mode="HTML",
                    )
            else:
                self.api.send_message(
                    chat_id, caption, message_id, parse_mode="HTML"
                )
            original_files = [
                path for path in files
                if path.suffix.lower() not in {".mp4", ".mov", ".m4v"}
            ]
            if not self.can_process(user_id):
                return
            try:
                self.api.send_documents(chat_id, original_files)
            except (OSError, requests.RequestException, RuntimeError):
                LOG.exception("Could not send original media files")
                if not self.can_process(user_id):
                    return
                self.api.send_message(
                    chat_id,
                    public_text(self.acl.language(user_id), "originals_failed"),
                )
            if not self.can_process(user_id):
                return
            rejected_videos = [
                name for name in rejected
                if Path(name).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}
            ]
            oversized_video_count = len(rejected_videos) + fallback_oversized_videos
            if oversized_video_count:
                self.api.send_message(
                    chat_id,
                    public_text(
                        self.acl.language(user_id),
                        "video_oversized",
                        count=oversized_video_count,
                    ),
                )
            rejected_other = [name for name in rejected if name not in rejected_videos]
            if rejected_other:
                self.api.send_message(
                    chat_id,
                    public_text(
                        self.acl.language(user_id),
                        "images_skipped",
                    ),
                )


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not BOT_TOKEN:
        LOG.warning("BOT_TOKEN is not configured; waiting for configuration")
        while True:
            time.sleep(300)

    api = TelegramAPI(BOT_TOKEN)
    acl = ACLStore(ACL_PATH, ENV_OWNER_ID)
    bot = Bot(api, acl)

    def stop_handler(_signum: int, _frame: Any) -> None:
        bot.stop()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    bot.start()
    bot.inline_executor.shutdown(wait=False, cancel_futures=True)
    deadline = time.monotonic() + 20
    for worker in bot.workers:
        worker.join(timeout=max(0, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
