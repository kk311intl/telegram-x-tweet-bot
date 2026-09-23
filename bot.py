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
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from PIL import Image, ImageOps


APP_NAME = "x-tweet-telegram-bot"
APP_VERSION = "1.0.0"
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
OWNER_LANGUAGE = os.environ.get("OWNER_LANGUAGE", "zh").strip().lower()
if OWNER_LANGUAGE not in {"zh", "ja", "en"}:
    raise ValueError("OWNER_LANGUAGE must be zh, ja, or en")
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
COOKIE_ALERT_INTERVAL = 24 * 60 * 60
DEFAULT_DAILY_LIMIT = 50
MANAGEMENT_PAGE_SIZE = 20
MIN_TELEGRAM_USER_ID_SHORTCUT = 100_000
MAX_TELEGRAM_USER_ID = (1 << 52) - 1
USER_LIST_NAME_WIDTH = 24
INLINE_USAGE_DEDUP_SECONDS = 5 * 60
INLINE_RESULT_LIMIT = 10
INLINE_CACHE_SECONDS = env_int("INLINE_CACHE_SECONDS", 60, 0, 3600)
TELEGRAM_RETRY_AFTER_MAX_SECONDS = 30
DAILY_REPORT_HOUR = env_int("DAILY_REPORT_HOUR", 22, 0, 23)

HTTP_LOCAL = threading.local()


def bot_date(timestamp: float | None = None) -> str:
    current = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(current, BOT_TIMEZONE).strftime("%Y-%m-%d")


def bot_hour(timestamp: float | None = None) -> int:
    current = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(current, BOT_TIMEZONE).hour


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

OWNER_BUTTONS = {
    "👤 使用者管理": "/usermenu",
    "🍪 Cookies 管理": "/cookiemenu",
    "📊 系統狀態": "/status",
    "ℹ️ 使用說明": "/help",
    "👥 使用者列表": "/users",
    "📝 申請審批": "/requests",
    "🔐 用戶權限修改": "/finduser",
    "🍪 匯入 Cookies": "/cookies",
    "📖 Cookies 說明": "/cookiehelp",
    "🗑 清除 Cookies": "/clearcookies",
    "↩️ 返回主選單": "/menu",
}

PUBLIC_TEXT = {
    "zh": {
        "start_allowed": "請傳送有效的 X/Twitter 單篇貼文網址。",
        "help_allowed": (
            "使用說明\n\n"
            "1. 傳送 x.com 或 twitter.com 的單篇貼文網址。\n"
            "2. Bot 會回傳貼文文字與媒體；圖片另附未壓縮檔案。\n"
            "3. 引用貼文只處理提交網址本身，不會切換到被引用內容。\n"
            f"4. 在其他聊天輸入 {BOT_MENTION} 加上貼文網址，可直接選擇並發送媒體；"
            "內聯模式不會發送圖片原始檔案。\n"
            "5. 影片超過 50 MB 時不會處理。\n\n"
            "使用 /id 可查看自己的 Telegram User ID。"
        ),
        "language_menu": "🌐 Language",
        "help_menu": "ℹ️ 使用說明",
        "choose_language": "請選擇語言。",
        "back": "↩️ 返回",
        "start_owner": "管理選單已載入，也可以直接傳送 X/Twitter 貼文連結。",
        "access": "此帳號尚未取得使用權限。申請通常每日集中審批。",
        "apply": "申請使用權限",
        "apply_created": "申請已送出。審批通常每日集中處理。",
        "apply_pending": "你的申請仍在等待審批。",
        "apply_allowed": "你已經取得使用權限。",
        "apply_auto_approved": "申請已自動通過，現在可以開始使用。",
        "service_paused": "目前已暫停普通用戶使用，請稍後再試。",
        "invalid_url": "請傳送有效的 X/Twitter 單篇貼文網址。",
        "queue_full": "目前處理佇列已滿，請稍後再試。",
        "quota": "目前暫時無法繼續處理，請稍後再試。",
        "approved": "你的 Bot 使用申請已通過，現在可以開始使用。",
        "failed": "處理失敗，請稍後重試。",
        "url_only": "此帳號只接受 X/Twitter 單篇貼文連結。",
        "inline_apply": "開啟機器人申請使用權限",
        "video_oversized": "有 {count} 個影片超過 50 MB，已略過且未處理。",
        "images_skipped": "部分圖片超過上傳或單篇總量限制，已略過：{names}",
        "preview_failed": "媒體預覽未能傳送，請稍後重新提交這則貼文。",
        "originals_failed": "預覽已完成，但部分原始檔案傳送失敗，請稍後重試。",
        "language_set": "語言已切換為繁體中文。",
    },
    "en": {
        "start_allowed": "Send a valid single-post X/Twitter URL.",
        "help_allowed": (
            "How to use\n\n"
            "1. Send a single-post URL from x.com or twitter.com.\n"
            "2. The Bot returns the post text and media; images also include uncompressed files.\n"
            "3. For quoted posts, only the submitted post is processed; quoted content is not followed.\n"
            f"4. In another chat, type {BOT_MENTION} followed by the post URL "
            "to select and send media. Inline mode does not send original image files.\n"
            "5. Videos larger than 50 MB are not processed.\n\n"
            "Use /id to view your Telegram User ID."
        ),
        "language_menu": "🌐 Language",
        "help_menu": "ℹ️ How to use",
        "choose_language": "Choose a language.",
        "back": "↩️ Back",
        "start_owner": "The management menu is ready. You can also send an X/Twitter post URL directly.",
        "access": "This account does not have access yet. Requests are usually reviewed once a day.",
        "apply": "Request access",
        "apply_created": "Your request was submitted. Reviews are usually processed daily.",
        "apply_pending": "Your request is still pending.",
        "apply_allowed": "You already have access.",
        "apply_auto_approved": "Your request was approved automatically. You can start using the Bot now.",
        "service_paused": "Access for regular users is temporarily paused. Try again later.",
        "invalid_url": "Send a valid single-post X/Twitter URL.",
        "queue_full": "The processing queue is full. Try again later.",
        "quota": "Processing is temporarily unavailable. Try again later.",
        "approved": "Your Bot access request was approved. You can start using it now.",
        "failed": "Processing failed. Try again later.",
        "url_only": "This account only accepts single-post X/Twitter URLs.",
        "inline_apply": "Open the Bot to request access",
        "video_oversized": "{count} video(s) exceeded 50 MB and were skipped.",
        "images_skipped": "Some images exceeded the upload or per-post size limit and were skipped: {names}",
        "preview_failed": "The media preview could not be delivered. Please submit this post again later.",
        "originals_failed": "The preview was sent, but some original files could not be delivered. Try again later.",
        "language_set": "Language changed to English.",
    },
    "ja": {
        "start_allowed": "有効な X/Twitter の単一投稿URLを送信してください。",
        "help_allowed": (
            "使い方\n\n"
            "1. x.com または twitter.com の単一投稿URLを送信します。\n"
            "2. Bot が本文とメディアを返します。画像は未圧縮ファイルも送信します。\n"
            "3. 引用投稿では、引用先ではなく送信した投稿自体を処理します。\n"
            f"4. 他のチャットで {BOT_MENTION} に続けて投稿URLを入力すると、"
            "メディアを選択して送信できます。インラインモードでは画像の元ファイルを送信しません。\n"
            "5. 50 MB を超える動画は処理しません。\n\n"
            "/id で自分の Telegram User ID を確認できます。"
        ),
        "language_menu": "🌐 Language",
        "help_menu": "ℹ️ 使い方",
        "choose_language": "言語を選択してください。",
        "back": "↩️ 戻る",
        "start_owner": "管理メニューを表示しました。X/Twitter の投稿URLを直接送信することもできます。",
        "access": "このアカウントはまだ許可されていません。申請は通常1日1回まとめて審査されます。",
        "apply": "利用を申請",
        "apply_created": "申請を送信しました。通常は1日ごとに審査されます。",
        "apply_pending": "申請は審査待ちです。",
        "apply_allowed": "すでに利用が許可されています。",
        "apply_auto_approved": "申請は自動的に承認されました。すぐにBotを利用できます。",
        "service_paused": "一般ユーザーの利用を一時停止しています。しばらくしてから再試行してください。",
        "invalid_url": "有効な X/Twitter の単一投稿URLを送信してください。",
        "queue_full": "処理キューが満杯です。しばらくしてから再試行してください。",
        "quota": "現在は一時的に処理できません。しばらくしてから再試行してください。",
        "approved": "Bot の利用申請が承認されました。すぐに利用できます。",
        "failed": "処理に失敗しました。しばらくしてから再試行してください。",
        "url_only": "このアカウントでは X/Twitter の単一投稿URLのみ受け付けます。",
        "inline_apply": "Botを開いて利用を申請",
        "video_oversized": "{count} 件の動画が 50 MB を超えたため、処理せずスキップしました。",
        "images_skipped": "一部の画像がアップロードまたは投稿単位の容量上限を超えたため、スキップしました：{names}",
        "preview_failed": "メディアのプレビューを送信できませんでした。しばらくしてから、この投稿をもう一度送信してください。",
        "originals_failed": "プレビューは送信できましたが、一部の元ファイルを送信できませんでした。しばらくしてから再試行してください。",
        "language_set": "表示言語を日本語に変更しました。",
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
    label = {"zh": "聯絡管理員", "ja": "管理者に連絡", "en": "Contact the owner"}[language]
    return help_text + "\n\n" + f'<a href="{html.escape(OWNER_CONTACT_URL, quote=True)}">{label}</a>'


# Administrator UI is fixed by the deployment, unlike user-selected public text.
ADMIN_TEXT = {
    "menu": ("管理選單", "Management menu", "管理メニュー"),
    "menu_ready": ("管理選單已載入。", "Management menu loaded.", "管理メニューを表示しました。"),
    "users": ("使用者管理", "User management", "ユーザー管理"),
    "user_list": ("使用者列表", "Users", "ユーザー一覧"),
    "requests": ("申請審批", "Access requests", "利用申請"),
    "permissions": ("用戶權限修改", "Change access", "権限を変更"),
    "cookies": ("Cookies 管理", "Cookies management", "Cookies 管理"),
    "cookie_import": ("匯入 Cookies", "Import Cookies", "Cookies を取り込む"),
    "cookie_help": ("Cookies 說明", "Cookies guide", "Cookies の説明"),
    "cookie_clear": ("清除 Cookies", "Clear Cookies", "Cookies を削除"),
    "status": ("系統狀態", "System status", "システム状態"),
    "help": ("使用說明", "Help", "使い方"),
    "advanced": ("高級選項", "Advanced settings", "詳細設定"),
    "implementation": ("實現方式", "Implementation details", "実装方法"),
    "management_mode": ("管理模式", "Management mode", "管理モード"),
    "access_switch": ("使用開關", "User access", "ユーザー利用"),
    "auto_approve": ("自動通過", "Auto-approve", "自動承認"),
    "cookie_switch": ("Cookies 使用", "Use Cookies", "Cookies の使用"),
    "on": ("開啟", "On", "オン"),
    "off": ("關閉", "Off", "オフ"),
    "open": ("開放", "Open", "許可"),
    "paused": ("暫停", "Paused", "停止"),
    "back": ("返回", "Back", "戻る"),
    "refresh": ("重新整理", "Refresh", "更新"),
    "previous": ("上一頁", "Previous", "前へ"),
    "next": ("下一頁", "Next", "次へ"),
    "approve": ("通過", "Approve", "承認"),
    "deny": ("拒絕", "Reject", "拒否"),
    "approve_page": ("通過本頁全部", "Approve this page", "このページをすべて承認"),
    "owner": ("所有者", "Owner", "所有者"),
    "admin": ("管理員", "Administrator", "管理者"),
    "ordinary": ("普通", "Regular", "一般"),
    "blocked": ("封鎖", "Blocked", "ブロック"),
    "pending": ("待審批", "Pending", "審査待ち"),
    "initialized": ("初始化", "Initialized", "初期化"),
    "unlimited": ("不限", "Unlimited", "無制限"),
    "not_named": ("（未提供名稱）", "(No name)", "（名前なし）"),
    "confirm_create": ("確認建立", "Create user", "ユーザーを作成"),
    "confirm_change": ("確認修改", "Confirm change", "変更を確定"),
    "cancel": ("取消", "Cancel", "キャンセル"),
    "owner_account": ("所有者帳號", "Owner account", "所有者アカウント"),
    "admin_read_only": ("管理員帳號（唯讀）", "Administrator (read-only)", "管理者（閲覧のみ）"),
    "default_quota": ("預設許可 (50)", "Default (50)", "標準 (50)"),
    "admin_unlimited": ("不限・管理員", "Unlimited · Administrator", "無制限・管理者"),
    "no_requests": ("目前沒有待審批申請。", "No pending requests.", "審査待ちの申請はありません。"),
    "find_user": ("請輸入要修改權限的 Telegram User ID。", "Enter the Telegram User ID to manage.", "権限を変更する Telegram User ID を入力してください。"),
    "invalid_menu": ("無效選單。", "Invalid menu.", "無効なメニューです。"),
    "invalid_action": ("無效操作。", "Invalid action.", "無効な操作です。"),
    "private_only": ("管理功能只允許在 Bot 私聊使用。", "Management is available only in a private chat with the bot.", "管理機能は Bot との個別チャットでのみ利用できます。"),
    "owner_only_cookies": ("Cookies 只允許所有者管理。", "Only the owner can manage Cookies.", "Cookies を管理できるのは所有者のみです。"),
    "admin_only": ("只有管理員可以執行此操作。", "Only administrators can do this.", "この操作は管理者のみ実行できます。"),
    "language_locked": ("管理員語言由部署設定。", "Administrator language is set on the server.", "管理者の言語はサーバーで設定されます。"),
    "menu_invalid_page": ("頁碼無效。", "Invalid page number.", "ページ番号が無効です。"),
    "page_changed": ("已切換頁面。", "Page changed.", "ページを切り替えました。"),
    "current_page": ("目前頁面", "Current page", "現在のページ"),
    "cancelled": ("已取消。", "Cancelled.", "キャンセルしました。"),
    "create_cancelled": ("已取消建立使用者。", "User creation cancelled.", "ユーザー作成を中止しました。"),
    "change_cancelled": ("已取消修改權限。", "Access change cancelled.", "権限の変更を中止しました。"),
    "quota_missing": ("缺少額度。", "Missing limit.", "上限値がありません。"),
    "default_quota_missing": ("缺少預設額度。", "Missing default limit.", "初期上限値がありません。"),
    "quota_invalid": ("額度格式無效。", "Invalid limit.", "上限値が無効です。"),
    "default_quota_invalid": ("預設額度無效。", "Invalid default limit.", "初期上限値が無効です。"),
    "permission_missing": ("缺少權限值。", "Missing access value.", "権限値がありません。"),
    "permission_invalid": ("權限值無效。", "Invalid access value.", "権限値が無効です。"),
    "quota_range": ("額度必須是 -1、0 或 1 至 10000。", "The limit must be -1, 0, or 1–10000.", "上限値は -1、0、または 1～10000 にしてください。"),
    "user_created": ("使用者已建立。", "User created.", "ユーザーを作成しました。"),
    "user_exists": ("該 User ID 已存在，未覆寫現有資料。", "That User ID already exists; no data was overwritten.", "その User ID は既に存在します。データは上書きしていません。"),
    "user_missing": ("使用者已不存在，未進行修改。", "User no longer exists; nothing changed.", "ユーザーが存在しません。変更はしていません。"),
    "permission_changed": ("權限已修改。", "Access changed.", "権限を変更しました。"),
    "id_invalid": ("User ID 範圍無效。", "User ID is out of range.", "User ID が有効範囲外です。"),
    "status_refreshed": ("狀態已更新。", "Status refreshed.", "状態を更新しました。"),
    "management_off": ("管理模式已關閉，請先重新開啟。", "Management mode is off. Turn it on first.", "管理モードがオフです。先にオンにしてください。"),
    "management_admin_only": ("只有管理員可以切換管理模式。", "Only administrators can change management mode.", "管理モードの切り替えは管理者のみ可能です。"),
    "access_owner_only": ("使用開關只允許所有者切換。", "Only the owner can change user access.", "ユーザー利用の切り替えは所有者のみ可能です。"),
    "auto_owner_only": ("自動通過只允許所有者切換。", "Only the owner can change auto-approval.", "自動承認の切り替えは所有者のみ可能です。"),
    "cookie_owner_only": ("Cookies 開關只允許所有者切換。", "Only the owner can change the Cookies setting.", "Cookies 設定の切り替えは所有者のみ可能です。"),
    "cookie_cleared": ("X/Twitter Cookies 已清除。", "X/Twitter Cookies cleared.", "X/Twitter の Cookies を削除しました。"),
    "approve_missing": ("申請不存在或使用者已被封鎖。", "Request not found or user blocked.", "申請がないか、ユーザーがブロックされています。"),
    "choose_access": ("選擇用戶權限。", "Choose access.", "権限を選択してください。"),
    "owner_cannot_ban": ("不能封鎖所有者。", "The owner cannot be blocked.", "所有者はブロックできません。"),
    "owner_cannot_change": ("不能修改所有者。", "The owner cannot be changed.", "所有者は変更できません。"),
    "admin_cannot_self": ("管理員不能修改自己的權限。", "Administrators cannot change their own access.", "管理者は自分の権限を変更できません。"),
    "admin_cannot_admin": ("管理員不能修改其他管理員。", "Administrators cannot change other administrators.", "管理者は他の管理者を変更できません。"),
    "owner_only_admin": ("只有所有者可以新增管理員。", "Only the owner can add administrators.", "管理者の追加は所有者のみ可能です。"),
}


def admin_text(key: str) -> str:
    return ADMIN_TEXT[key][{"zh": 0, "en": 1, "ja": 2}[OWNER_LANGUAGE]]

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
        users = raw.get("users") or {}
        pending = raw.get("pending_applications") or {}
        if not isinstance(users, dict) or not isinstance(pending, dict):
            raise ValueError("ACL users and pending applications must be objects")
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
    def read_access_snapshot(cls, path: Path) -> dict[str, Any]:
        # Read exactly one atomic file generation; never migrate or write state.
        store = cls.__new__(cls)
        with path.open(encoding="utf-8") as source:
            store._apply_state(json.load(source), os.fstat(source.fileno()).st_mtime_ns)
        return store.export_access()

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
                if not isinstance(json.loads(current), dict):
                    raise ValueError("ACL state must be an object")
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

    def export_access(self) -> dict[str, Any]:
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
        return {"version": 1, "users": sorted(users, key=lambda item: item["user_id"])}

    def import_access(self, snapshot: dict[str, Any]) -> int:
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            raise ValueError("unsupported access snapshot")
        users = snapshot.get("users")
        if not isinstance(users, list):
            raise ValueError("access snapshot users must be a list")
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
                if quota < -1 or quota > 10000:
                    raise ValueError("quota out of range")
            updated_at = int(item.get("updated_at", 0) or 0)
            if updated_at <= 0:
                raise ValueError("invalid quota update timestamp")
            validated.append((user_id, quota, updated_at))

        changed = 0
        dirty = False
        with self.lock:
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

    def debug_mode(self, user_id: int) -> bool:
        record = self.data["users"].get(str(user_id)) or {}
        return self.is_admin(user_id) and bool(record.get("debug_mode", False))

    def management_mode(self, user_id: int) -> bool:
        record = self.data["users"].get(str(user_id)) or {}
        return self.is_admin(user_id) and bool(record.get("management_mode", True))

    def toggle_debug_mode(self, user_id: int) -> bool:
        if not self.is_admin(user_id):
            raise ValueError("user is not an administrator")
        with self.lock:
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id, "quota": None}
            )
            enabled = not bool(record.get("debug_mode", False))
            record["debug_mode"] = enabled
            self._save()
            return enabled

    def toggle_management_mode(self, user_id: int) -> bool:
        if not self.is_admin(user_id):
            raise ValueError("user is not an administrator")
        with self.lock:
            record = self.data["users"].setdefault(
                str(user_id), {"user_id": user_id, "quota": None}
            )
            enabled = not bool(record.get("management_mode", True))
            record["management_mode"] = enabled
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
        if self.is_admin(user_id):
            return OWNER_LANGUAGE
        record = self.data["users"].get(str(user_id)) or {}
        language = str(record.get("language") or "zh")
        return language if language in PUBLIC_TEXT else "zh"

    def set_language(self, user_id: int, language: str) -> None:
        if self.is_admin(user_id):
            raise ValueError("administrator language is configured at deployment")
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
        self.set_quota(user_id, DEFAULT_DAILY_LIMIT)

    def remove(self, user_id: int) -> None:
        self.set_quota(user_id, 0)

    def users(self) -> list[int]:
        return sorted(
            int(key)
            for key in self.data["users"]
            if int(key) != self.owner_id and self.is_allowed(int(key))
        )

    def has_user(self, user_id: int) -> bool:
        with self.lock:
            return str(user_id) in self.data["users"]

    def ensure_managed_user(
        self, user_id: int, initial_quota: int | None = DEFAULT_DAILY_LIMIT
    ) -> bool:
        if user_id <= 0:
            raise ValueError("user id must be positive")
        with self.lock:
            key = str(user_id)
            if key in self.data["users"]:
                return False
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
                record["quota"] = DEFAULT_DAILY_LIMIT
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

    def approve(self, user_id: int) -> bool:
        with self.lock:
            if self.is_banned(user_id):
                return False
            key = str(user_id)
            existed = key in self.data["pending_applications"]
            self.data["pending_applications"].pop(key, None)
            record = self.data["users"].setdefault(key, {"user_id": user_id})
            record["quota"] = DEFAULT_DAILY_LIMIT
            record["quota_updated_at"] = self._quota_timestamp()
            self._save()
            return existed

    def deny(self, user_id: int) -> bool:
        with self.lock:
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
        return (
            bot_hour(current) >= DAILY_REPORT_HOUR
            and self.data.get("last_daily_report_date") != bot_date(current)
        )

    def mark_daily_report(self, now: float | None = None) -> None:
        with self.lock:
            self.data["last_daily_report_date"] = bot_date(now)
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
        if quota is not None and (quota < -1 or quota > 10000):
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

    def set_limit(self, user_id: int, daily_limit: int) -> None:
        self.set_quota(user_id, daily_limit)

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


def owner_keyboard(management_mode: bool = True) -> dict[str, Any]:
    management_state = admin_text("on" if management_mode else "off")
    return {
        "inline_keyboard": [
            [
                {"text": f'👤 {admin_text("users")}', "callback_data": "nav:users"},
                {
                    "text": f'🛠 {admin_text("management_mode")}: {management_state}',
                    "callback_data": "managementtoggle:0",
                },
            ],
            [
                {"text": f'📊 {admin_text("status")}', "callback_data": "nav:status"},
                {"text": f'ℹ️ {admin_text("help")}', "callback_data": "nav:help"},
            ],
        ],
    }


def administrator_keyboard(
    is_owner: bool, management_mode: bool = True
) -> dict[str, Any]:
    return owner_keyboard(management_mode)


def user_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": f'👥 {admin_text("user_list")}', "callback_data": "nav:userlist"},
                {"text": f'📝 {admin_text("requests")}', "callback_data": "nav:requests"},
            ],
            [{"text": f'🔐 {admin_text("permissions")}', "callback_data": "nav:finduser"}],
            [{"text": f'↩️ {admin_text("back")}: {admin_text("menu")}', "callback_data": "nav:main"}],
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
                "text": f'{admin_text("cookie_switch")}{"：" if OWNER_LANGUAGE != "en" else ": "}{cookie_state}',
                "callback_data": "ordinarycookiestoggle:0",
            }],
            [{"text": f'🗑 {admin_text("cookie_clear")}', "callback_data": "nav:clearcookies"}],
            [{"text": f'↩️ {admin_text("back")}: {admin_text("advanced")}', "callback_data": "nav:advanced"}],
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


def display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.category(character) in {"Mn", "Me", "Cf"}:
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def truncate_display(value: str, max_width: int) -> str:
    if display_width(value) <= max_width:
        return value
    target = max(1, max_width - 1)
    result = []
    current = 0
    for character in value:
        character_width = display_width(character)
        if current + character_width > target:
            break
        result.append(character)
        current += character_width
    return "".join(result).rstrip() + "…"


def language_row(selected: str) -> list[dict[str, str]]:
    return [
        {
            "text": ("✓ " if selected == language else "") + label,
            "callback_data": f"lang:{language}",
        }
        for language, label in (("zh", "繁體中文"), ("en", "English"), ("ja", "日本語"))
    ]


def start_keyboard(
    language: str,
    is_admin: bool,
    is_allowed: bool,
    is_owner: bool = False,
    management_mode: bool = True,
) -> dict[str, Any]:
    rows = []
    if is_admin and management_mode:
        rows.extend(
            administrator_keyboard(is_owner, management_mode)["inline_keyboard"]
        )
    elif not is_allowed:
        rows.append([{
            "text": public_text(language, "apply"),
            "callback_data": "apply",
        }])
    if is_admin and not management_mode:
        rows.append([{
            "text": f'🛠 {admin_text("management_mode")}: {admin_text("off")}',
            "callback_data": "managementtoggle:0",
        }])
    if not is_admin or not management_mode:
        actions = []
        if not is_admin:
            actions.append({
                "text": public_text(language, "language_menu"),
                "callback_data": "public:language",
            })
        actions.append({
            "text": public_text(language, "help_menu"),
            "callback_data": "public:help",
        })
        rows.append(actions)
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


def pending_keyboard(
    records: list[dict[str, Any]], page: int = 0
) -> dict[str, Any]:
    rows = []
    start = page * MANAGEMENT_PAGE_SIZE
    for record in records[start : start + MANAGEMENT_PAGE_SIZE]:
        user_id = int(record["user_id"])
        label = user_label(record)[:24]
        rows.append([
            {"text": f'{admin_text("approve")} {label}', "callback_data": f"approve:{user_id}:{page}"},
            {"text": admin_text("deny"), "callback_data": f"deny:{user_id}:{page}"},
        ])
    rows.append([{
        "text": admin_text("approve_page"),
        "callback_data": f"approvepage:{page}",
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
) -> dict[str, Any]:
    user_id = int(record["user_id"])
    if user_id == owner_id:
        return {"inline_keyboard": [
            [{"text": admin_text("owner_account"), "callback_data": "noop:0"}],
            [{"text": f'↩️ {admin_text("users")}', "callback_data": "nav:users"}],
        ]}
    if can_modify:
        return quota_choices_keyboard(user_id, allow_admin=allow_admin)
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


def status_keyboard() -> dict[str, Any]:
    rows = [[{"text": f'🔄 {admin_text("refresh")}', "callback_data": "statusrefresh:0"}]]
    rows.append([{
        "text": f'⚙️ {admin_text("advanced")}',
        "callback_data": "nav:advanced",
    }])
    rows.append([{"text": f'↩️ {admin_text("back")}: {admin_text("menu")}', "callback_data": "nav:main"}])
    return {"inline_keyboard": rows}


def advanced_status_keyboard(
    debug_mode: bool,
    external_access_enabled: bool = True,
    auto_approve_enabled: bool = False,
    can_configure: bool = True,
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
            "text": f'🌐 {admin_text("access_switch")}{"：" if OWNER_LANGUAGE != "en" else ": "}{external_state}',
            "callback_data": "externaltoggle:0",
        }],
        [{
            "text": f'✅ {admin_text("auto_approve")}{"：" if OWNER_LANGUAGE != "en" else ": "}{auto_approve_state}',
            "callback_data": "autoapprovetoggle:0",
        }],
    ] if can_configure else []
    return {"inline_keyboard": [
        *owner_rows,
        [{
            "text": f'🐞 {admin_text("implementation")}{"：" if OWNER_LANGUAGE != "en" else ": "}{debug_state}',
            "callback_data": "debugtoggle:0",
        }],
        [{"text": f'↩️ {admin_text("back")}: {admin_text("status")}', "callback_data": "nav:status"}],
    ]}


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if OWNER_LANGUAGE == "en":
        return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"
    if OWNER_LANGUAGE == "ja":
        return f"{days}日 {hours}時間" if days else f"{hours}時間 {minutes}分" if hours else f"{minutes}分"
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分"
    return f"{minutes} 分"


def quota_choices_keyboard(
    user_id: int, allow_admin: bool = True
) -> dict[str, Any]:
    rows = [
            [
                {"text": admin_text("default_quota"), "callback_data": f"quota:{user_id}:50"},
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


def limit_choices_keyboard(user_id: int) -> dict[str, Any]:
    return quota_choices_keyboard(user_id)


def access_request_text(user_id: int, language: str = "zh") -> str:
    return (
        f"Telegram User ID: {user_id}\n"
        f"{public_text(language, 'access')}"
    )


def cookie_upload_text() -> str:
    return {
        "en": "Upload a Netscape-format cookies.txt file (maximum 1 MB). Select Cookies guide if needed. The file passes through Telegram; use /cancel to cancel.",
        "ja": "Netscape 形式の cookies.txt をアップロードしてください（上限 1 MB）。必要なら Cookies の説明を開いてください。ファイルは Telegram を経由します。/cancel で中止できます。",
    }.get(OWNER_LANGUAGE) or (
        "請上傳 Netscape 格式的 cookies.txt，檔案上限 1 MB。\n"
        "不清楚如何取得時，點選「Cookies 說明」。\n"
        "文件會經 Telegram 傳送；輸入 /cancel 可取消。"
    )


def cookie_help_text() -> str:
    return {
        "en": "X/Twitter Cookies\n1. Sign in to x.com in a browser, ideally with a dedicated bot account.\n2. Use a trusted tool to export Netscape cookies.txt for x.com/twitter.com only.\n3. Select Import Cookies and upload the file as a document.\n4. After success, delete the original document from Telegram.\n\nCookies are login credentials. Do not export other sites or forward them. Import again if the X session expires.",
        "ja": "X/Twitter の Cookies\n1. ブラウザーで x.com にログインします。Bot 専用アカウントを推奨します。\n2. 信頼できるツールで x.com/twitter.com のみの Netscape 形式 cookies.txt を書き出します。\n3. Cookies を取り込むを選び、ファイルを文書として送信します。\n4. 成功後、Telegram の元ファイルを削除します。\n\nCookies はログイン資格情報です。他のサイトの情報を含めたり、第三者に転送したりしないでください。セッション失効時は再度取り込んでください。",
    }.get(OWNER_LANGUAGE) or (
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
    if OWNER_LANGUAGE == "en":
        role = ("The owner can manage users and administrators, Cookies, access, and auto-approval." if is_owner else "Administrators can manage regular users but cannot change the owner, themselves, other administrators, or owner-only settings.")
        return (
            "Owner guide" if is_owner else "Administrator guide"
        ) + f"\n\nSend one X/Twitter post URL to retrieve text and media. In another chat, use {BOT_MENTION} followed by a URL for inline media. Images include uncompressed files; videos are limited to 50 MB. Quoted content is not followed.\n\nUsers and requests are shown 20 per page. A quota of -1 blocks, 0 initializes, a positive number is the daily limit, and unlimited means administrator. Send a User ID to search, or a User ID and quota to change access; changes require confirmation.\n\n{role}\nManagement commands work only in a private chat with the bot. Daily usage resets at midnight in {BOT_TIMEZONE_NAME}; the report is sent at {DAILY_REPORT_HOUR:02d}:00."
    if OWNER_LANGUAGE == "ja":
        role = ("所有者はユーザーと管理者、Cookies、利用許可、自動承認を管理できます。" if is_owner else "管理者は一般ユーザーを管理できますが、所有者、自分自身、他の管理者、所有者専用設定は変更できません。")
        return (
            "所有者向けガイド" if is_owner else "管理者向けガイド"
        ) + f"\n\nX/Twitter の単一投稿URLを送信すると本文とメディアを取得します。他のチャットでは {BOT_MENTION} とURLでインライン送信できます。画像は元ファイルも送り、動画の上限は 50 MB です。引用先はたどりません。\n\nユーザーと申請は1ページ20件です。権限は -1 がブロック、0 が初期化、正の数が1日の上限、無制限が管理者です。User ID で検索し、User ID と上限値で変更できます。変更には確認が必要です。\n\n{role}\n管理操作は Bot との個別チャットのみで使えます。利用回数は {BOT_TIMEZONE_NAME} の午前0時にリセットされ、日次レポートは {DAILY_REPORT_HOUR:02d}:00 に送信されます。"
    title = "所有者管理說明" if is_owner else "管理員使用說明"
    role_scope = (
        "• 可建立普通使用者，並升級或降級管理員。\n"
        "• 可在系統狀態的高級選項管理 Cookies、全域使用開關與自動通過。\n"
        if is_owner
        else
        "• 可建立及修改普通、初始化或封鎖使用者。\n"
        "• 不能修改 Owner、自己或其他管理員，也不能新增管理員。\n"
        "• 高級選項只能使用實現方式；Cookies、全域使用開關與自動通過只允許 Owner 操作。\n"
        "• 抓取貼文時仍可使用 Owner 已配置的 Cookies，不受 Cookies 開關影響。\n"
    )
    return (
        f"{title}\n\n"
        "使用方式\n"
        "• 傳送單篇 X/Twitter 貼文網址即可取得文字與媒體。\n"
        f"• 在其他聊天輸入 {BOT_MENTION} 加貼文網址，可用內聯結果發送媒體；不會附圖片原始檔案。\n"
        "• 引用貼文只處理提交網址本身的文字與媒體，不會切換到被引用內容。\n"
        "• 圖片會提供預覽及未壓縮檔案；影片上限為 50 MB。\n\n"
        "擷取順序\n"
        "• 優先使用 FxTwitter metadata 從可信任的 twimg.com 直連下載媒體。\n"
        "• 直連未取得檔案時，才依序嘗試 gallery-dl 匿名、gallery-dl Cookies、"
        "yt-dlp 匿名及 yt-dlp Cookies。\n"
        "• 普通用戶 Cookies 關閉時，會跳過普通使用者的兩個 Cookies 階段。\n\n"
        "使用者管理\n"
        "• 使用者與申請列表每頁顯示 20 筆。\n"
        "• 用戶權限規則：-1 封鎖、0 初始化、正整數為每日額度、不限為管理員。\n"
        "• 可直接傳送 User ID 查詢，或傳送「User ID 數字」建立或修改；均需二次確認。\n"
        "• 純數字快捷辨識範圍為 100000 至 52-bit 正整數。\n"
        f"{role_scope}"
        "• 管理操作只允許在 Bot 私聊執行。\n\n"
        "系統管理\n"
        "• 系統狀態可查看佇列、用量、使用者、Cookies 與全域開關狀態。\n"
        "• Owner 可在高級選項管理 Cookies、使用開關與自動通過。自動通過開啟時，申請者立即取得每日 50 次的普通使用權限。\n"
        "• 實現方式是每位管理員的個人設定，只影響自己的結果。\n"
        "• 管理模式可暫時按普通用戶規則測試，並隨時切換恢復。"
    )


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
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="cookies-", suffix=".tmp", dir=COOKIES_PATH.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(COOKIES_PATH)
        COOKIE_ALERT_PATH.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"last_sent": current}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


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
            data["reply_parameters"] = json.dumps({"message_id": reply_to})
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

    def configure_commands(self, owner_id: int) -> None:
        command_sets = {
            "": [
                {"command": "start", "description": "啟動並顯示操作選單"},
                {"command": "id", "description": "顯示我的 User ID"},
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
                "傳送單篇 X/Twitter 貼文連結，機器人會回傳作者連結、貼文文字、圖片與影片。"
                "媒體可提供預覽；圖片另附未壓縮檔案。取得授權後，也可在其他聊天"
                f"透過 {BOT_MENTION} 加網址使用內聯媒體。",
            ),
            "zh": (
                "傳送 X/Twitter 貼文連結，取得文字、圖片與影片。",
                "傳送單篇 X/Twitter 貼文連結，機器人會回傳作者連結、貼文文字、圖片與影片。"
                "媒體可提供預覽；圖片另附未壓縮檔案。取得授權後，也可在其他聊天"
                f"透過 {BOT_MENTION} 加網址使用內聯媒體。",
            ),
            "en": (
                "Send an X/Twitter post URL to retrieve its text, images and videos.",
                "Send a single X/Twitter post URL to receive the author profile link, text, images and videos. "
                "Media previews are included, and original image files are sent without "
                f"compression. Once authorized, use {BOT_MENTION} plus a URL inline "
                "in other chats.",
            ),
            "ja": (
                "X/Twitterの投稿URLから本文・画像・動画を取得します。",
                "X/Twitterの単一投稿URLを送信すると、投稿者リンク・本文・画像・動画を取得できます。"
                "メディアのプレビューに加え、画像は未圧縮のファイルも送信されます。"
                f"承認後は他のチャットで {BOT_MENTION} とURLを入力して"
                "インラインメディアを利用できます。",
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

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

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


def run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    LOG.info("Running extractor: %s", command[0])
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env={**os.environ, "HOME": str(STATE_DIR)},
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
        heading = html.escape(truncate_text(url, heading_limit))
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
            formats = [
                entry for entry in candidate.get("formats") or []
                if isinstance(entry, dict)
                and entry.get("container") == "mp4"
                and entry.get("url")
            ]
            if formats:
                formats.sort(
                    key=lambda entry: (
                        int(entry.get("width") or 0)
                        * int(entry.get("height") or 0),
                        int(entry.get("bitrate") or 0),
                    ),
                    reverse=True,
                )
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
        {"zh": "取得方式：FxTwitter Inline", "en": "Method: FxTwitter Inline", "ja": "取得方法：FxTwitter Inline"}[OWNER_LANGUAGE] if debug else "",
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
    if OWNER_LANGUAGE != "zh":
        method = {
            "FxTwitter 直連": "FxTwitter direct",
            "FxTwitter 備援": "FxTwitter fallback",
            "gallery-dl（匿名）": "gallery-dl (anonymous)",
            "gallery-dl（Cookies）": "gallery-dl (Cookies)",
            "yt-dlp（匿名）": "yt-dlp (anonymous)",
            "yt-dlp（Cookies）": "yt-dlp (Cookies)",
        }.get(method, method)
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
        }[OWNER_LANGUAGE] if debug else "",
    )


def download_fxtwitter_media(
    tweet: dict[str, Any], directory: Path
) -> tuple[list[Path], str, int]:
    downloaded: list[Path] = []
    total = 0
    oversized_videos = 0
    for index, item in enumerate(fxtwitter_media(tweet), start=1):
        media_type = str(item.get("type") or "")
        item_limit = MAX_VIDEO_BYTES if media_type in {"video", "gif"} else MAX_MEDIA_BYTES
        candidates = [item]
        if media_type in {"video", "gif"}:
            formats = [
                entry for entry in item.get("formats") or []
                if isinstance(entry, dict)
                and entry.get("container") == "mp4"
                and entry.get("url")
            ]
            formats.sort(
                key=lambda entry: (
                    int(entry.get("width") or 0) * int(entry.get("height") or 0),
                    int(entry.get("bitrate") or 0),
                ),
                reverse=True,
            )
            candidates = formats or candidates

        item_downloaded = False
        item_oversized = False
        seen_urls: set[str] = set()
        for candidate in candidates:
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
                with trusted_twimg_response(media_url, timeout=(10, 120)) as response:
                    content_length = response.headers.get("Content-Length", "")
                    if content_length.isdigit() and int(content_length) > item_limit:
                        item_oversized = True
                        continue
                    size = 0
                    with target.open("wb") as handle:
                        for chunk in response.iter_content(128 * 1024):
                            if not chunk:
                                continue
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
            except (OSError, ValueError, requests.RequestException):
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
    result = run_command([*command, url], timeout=240)
    files = media_files(directory)
    method = "gallery-dl（匿名）" if files else ""
    cookie_invalid = False

    if not files and allow_cookies and COOKIES_PATH.exists():
        result = run_command(
            [*command, "--cookies", str(COOKIES_PATH), url], timeout=240
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
            "--restrict-filenames",
            "--merge-output-format",
            "mp4",
            "--format",
            "best[filesize<45M]/best[filesize_approx<45M]/worst",
            "--output",
            str(directory / "%(id)s.%(ext)s"),
        ]
        result = run_command([*fallback, url], timeout=240)
        files = media_files(directory)
        if files:
            method = "yt-dlp（匿名）"

        if not files and allow_cookies and COOKIES_PATH.exists():
            result = run_command(
                [*fallback, "--cookies", str(COOKIES_PATH), url], timeout=240
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
            tuple[str, bool], tuple[float, list[dict[str, Any]]]
        ] = {}
        self.pending_cookie_uploads: set[int] = set()
        self.pending_user_searches: set[int] = set()
        self.inline_usage_lock = threading.Lock()
        self.inline_usage: dict[tuple[int, str], float] = {}
        self.started_at = time.time()

    def target_management_error(self, actor_id: int, target_id: int) -> str | None:
        if not is_telegram_user_id(target_id):
            return {"zh": "User ID 不在 Telegram 的有效範圍內。", "en": "User ID is outside Telegram's valid range.", "ja": "User ID が Telegram の有効範囲外です。"}[OWNER_LANGUAGE]
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
        try:
            self.api.configure_commands(self.acl.owner_id)
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
            except (requests.RequestException, RuntimeError, ValueError):
                LOG.exception("Polling failed")
                time.sleep(5)

    def stop(self) -> None:
        self.stop_event.set()

    def can_process(self, user_id: int) -> bool:
        return self.acl.is_allowed(user_id) and (
            self.acl.external_access_enabled
            or (self.acl.is_admin(user_id) and self.acl.management_mode(user_id))
        )

    def handle_update(self, update: dict[str, Any]) -> None:
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
        management_mode = self.acl.management_mode(user_id)
        admin_mode = is_admin and management_mode
        is_private_chat = self.is_private_management_chat(user_id, chat_id)

        document = message.get("document")
        if isinstance(document, dict):
            if admin_mode:
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
                        is_owner,
                        management_mode,
                    ),
                )
            return

        text = str(message.get("text", "")).strip()
        if not text:
            return

        if admin_mode and text in OWNER_BUTTONS:
            text = OWNER_BUTTONS[text]

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()

        if (
            admin_mode
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
                    {"zh": "User ID 必須是 1 至 2^52-1 的正整數。請重新點選「用戶權限修改」。", "en": "User ID must be between 1 and 2^52-1. Select Change access again.", "ja": "User ID は 1～2^52-1 の整数にしてください。権限を変更をもう一度選んでください。"}[OWNER_LANGUAGE],
                    message_id,
                    user_menu_keyboard(),
                )
                return
            if is_owner and not self.acl.has_user(target):
                self.api.send_message(
                    chat_id,
                    {"zh": f"資料庫中沒有 User ID {target}。\n確認以每日額度 {DEFAULT_DAILY_LIMIT} 建立此使用者？", "en": f"User ID {target} is not in the database.\nCreate with a daily limit of {DEFAULT_DAILY_LIMIT}?", "ja": f"User ID {target} はデータベースにありません。\n1日の上限 {DEFAULT_DAILY_LIMIT} で作成しますか？"}[OWNER_LANGUAGE],
                    message_id,
                    confirm_new_user_keyboard(target, DEFAULT_DAILY_LIMIT),
                )
                return
            self.send_user_search_result(chat_id, message_id, target, user_id)
            return

        if admin_mode:
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
                quota = DEFAULT_DAILY_LIMIT
                if len(numeric_parts) == 2:
                    try:
                        quota = int(numeric_parts[1])
                        if quota < -1 or quota > 10000:
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
                        {"zh": f"資料庫中沒有 User ID {target}。\n確認以每日額度 {quota} 建立此使用者？", "en": f"User ID {target} is not in the database.\nCreate with a daily limit of {quota}?", "ja": f"User ID {target} はデータベースにありません。\n1日の上限 {quota} で作成しますか？"}[OWNER_LANGUAGE],
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
                        {"zh": f"確認修改 User ID {target} 的權限？\n目前：{current_label}\n新值：{quota}", "en": f"Change access for User ID {target}?\nCurrent: {current_label}\nNew: {quota}", "ja": f"User ID {target} の権限を変更しますか？\n現在：{current_label}\n新しい値：{quota}"}[OWNER_LANGUAGE],
                        message_id,
                        confirm_quota_change_keyboard(target, quota),
                    )
                    return
                self.send_user_search_result(chat_id, message_id, target, user_id)
                return

        if command == "/start":
            is_allowed = self.acl.is_allowed(user_id)
            text_key = "start_owner" if admin_mode else "start_allowed"
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
                    is_owner,
                    management_mode,
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
                    chat_id, {"zh": "Owner 認領只允許在 Bot 私聊完成。", "en": "Owner claim is available only in a private chat with the bot.", "ja": "所有者の登録は Bot との個別チャットでのみ可能です。"}[OWNER_LANGUAGE], message_id
                )
                return
            if self.acl.claim(user_id, argument):
                self.api.configure_commands(user_id)
                self.api.remove_reply_keyboard(chat_id)
                self.api.send_message(
                    chat_id, {"zh": "Owner 設定完成，管理選單已載入。", "en": "Owner configured. Management menu loaded.", "ja": "所有者を設定し、管理メニューを表示しました。"}[OWNER_LANGUAGE], message_id, owner_keyboard()
                )
            else:
                self.api.send_message(chat_id, {"zh": "認領失敗或 Owner 已存在。", "en": "Claim failed or an owner already exists.", "ja": "登録に失敗したか、所有者が既に存在します。"}[OWNER_LANGUAGE], message_id)
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
            if not admin_mode:
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
                    is_owner,
                    management_mode,
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
                "/cancel",
            } and not is_owner:
                self.api.send_message(
                    chat_id,
                    admin_text("owner_only_cookies"),
                    message_id,
                    administrator_keyboard(False),
                )
                return
            if command != "/finduser":
                self.pending_user_searches.discard(user_id)
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
        if not admin_mode and not self.acl.external_access_enabled:
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
        privileged_mode = (
            self.acl.is_admin(user_id) and self.acl.management_mode(user_id)
        )
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
        try:
            if user_id is not None and not self.can_process(user_id):
                self.api.answer_inline_query(query_id, [])
                return
            now = time.monotonic()
            key = (url, debug)
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
            "zh": f"每日使用簡報（{report_date}，{BOT_TIMEZONE_NAME}）\n\n活躍使用者：{active}\n處理次數：{total}\n待審批：{len(records)}",
            "en": f"Daily usage report ({report_date}, {BOT_TIMEZONE_NAME})\n\nActive users: {active}\nProcessed requests: {total}\nPending approvals: {len(records)}",
            "ja": f"日次利用レポート（{report_date}、{BOT_TIMEZONE_NAME}）\n\n利用ユーザー：{active}\n処理回数：{total}\n審査待ち：{len(records)}",
        }[OWNER_LANGUAGE]
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
            "zh": f"使用者列表（第 {page + 1}/{pages} 頁，共 {len(records)} 位）",
            "en": f"Users (page {page + 1}/{pages}, {len(records)} total)",
            "ja": f"ユーザー一覧（{page + 1}/{pages} ページ、計 {len(records)} 人）",
        }[OWNER_LANGUAGE]
        lines = [
            title,
            {"zh": "ID｜狀態｜額度｜今日使用｜名稱", "en": "ID | Status | Limit | Used today | Name", "ja": "ID｜状態｜上限｜本日の利用｜名前"}[OWNER_LANGUAGE],
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
            username = telegram_username(record)
            if username:
                id_text = f'<a href="https://t.me/{username}">{user_id}</a>'
            else:
                id_text = f"<code>{user_id}</code>"
            name = html.escape(
                truncate_display(user_name(record), USER_LIST_NAME_WIDTH)
            )
            usage = int(record.get("usage_count", 0) or 0)
            lines.append(
                f"{id_text}｜{status}｜{quota_text}｜{usage}｜<code>{name}</code>"
            )
        return "\n".join(lines), users_page_keyboard(page, len(records)), page

    def pending_page(
        self, records: list[dict[str, Any]], page: int
    ) -> tuple[str, dict[str, Any], int]:
        pages = max(1, (len(records) + MANAGEMENT_PAGE_SIZE - 1) // MANAGEMENT_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * MANAGEMENT_PAGE_SIZE
        lines = [{
            "zh": f"待審批使用申請（第 {page + 1}/{pages} 頁，共 {len(records)} 筆）：",
            "en": f"Pending requests (page {page + 1}/{pages}, {len(records)} total):",
            "ja": f"審査待ちの申請（{page + 1}/{pages} ページ、計 {len(records)} 件）：",
        }[OWNER_LANGUAGE]]
        for record in records[start : start + MANAGEMENT_PAGE_SIZE]:
            lines.append(f"• {user_label(record)}｜ID {record['user_id']}")
        return "\n".join(lines), pending_keyboard(records, page), page

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
            status, quota_text = ("普通使用者" if OWNER_LANGUAGE == "zh" else admin_text("ordinary")), str(quota)
        can_modify = (
            actor_id is None
            or self.target_management_error(actor_id, target) is None
        )
        used = int(record.get("usage_count", 0) or 0)
        summary = {
            "zh": f"{user_label(record)}\nUser ID：{target}\n狀態：{status}\n每日額度：{quota_text}\n今日已使用：{used} 次",
            "en": f"{user_label(record)}\nUser ID: {target}\nStatus: {status}\nDaily limit: {quota_text}\nUsed today: {used}",
            "ja": f"{user_label(record)}\nUser ID：{target}\n状態：{status}\n1日の上限：{quota_text}\n本日の利用：{used} 回",
        }[OWNER_LANGUAGE]
        return (
            summary,
            searched_user_keyboard(
                record,
                self.acl.owner_id,
                can_modify,
                allow_admin=actor_id == self.acl.owner_id,
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
                    "zh": f"找不到 User ID {target}。此使用者可能尚未與 Bot 互動。",
                    "en": f"User ID {target} was not found. They may not have interacted with the bot yet.",
                    "ja": f"User ID {target} が見つかりません。まだ Bot を利用していない可能性があります。",
                }[OWNER_LANGUAGE],
                message_id,
                user_menu_keyboard(),
            )
            return
        text, keyboard = result
        self.api.send_message(chat_id, text, message_id, keyboard)

    def system_status_text(self, viewer_id: int) -> str:
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
        cookies_set = COOKIES_PATH.exists()
        if OWNER_LANGUAGE == "en":
            state = lambda enabled: "On" if enabled else "Off"
            return (
                f"System status\n\nService: {'Running' if any(worker.is_alive() for worker in self.workers) else 'Worker stopped'}\n"
                f"Uptime: {format_duration(time.time() - self.started_at)}\n"
                f"Queue: {queue_size}/{MAX_QUEUE} ({queue_percent}%)\n\n"
                f"Users\nRecords: {len(records)} | Regular: {ordinary} | Administrators: {administrators} | Initialized: {initialized} | Pending: {pending} | Blocked: {banned}\n"
                f"Active today: {active_today} | Processed today: {interactions} | At quota: {exhausted}\n"
                f"Usage reset: 00:00 {BOT_TIMEZONE_NAME}\nDaily report: {DAILY_REPORT_HOUR:02d}:00 {BOT_TIMEZONE_NAME}\n\n"
                f"X Cookies: {'Configured' if cookies_set else 'Not configured'}\n"
                f"Cookies for regular users: {state(self.acl.ordinary_user_cookies_enabled)}\n"
                f"User access: {'Open' if self.acl.external_access_enabled else 'Paused'}\n"
                f"Auto-approve: {state(self.acl.auto_approve_enabled)}\n"
                f"Management mode: {state(self.acl.management_mode(viewer_id))}\n"
                f"Implementation details: {state(self.acl.debug_mode(viewer_id))}"
            )
        if OWNER_LANGUAGE == "ja":
            state = lambda enabled: "オン" if enabled else "オフ"
            return (
                f"システム状態\n\nサービス：{'稼働中' if any(worker.is_alive() for worker in self.workers) else 'ワーカー停止'}\n"
                f"稼働時間：{format_duration(time.time() - self.started_at)}\n"
                f"待機列：{queue_size}/{MAX_QUEUE}（{queue_percent}%）\n\n"
                f"ユーザー\n記録：{len(records)}｜一般：{ordinary}｜管理者：{administrators}｜初期化：{initialized}｜審査待ち：{pending}｜ブロック：{banned}\n"
                f"本日の利用者：{active_today}｜処理回数：{interactions}｜上限到達：{exhausted}\n"
                f"利用回数のリセット：00:00 {BOT_TIMEZONE_NAME}\n日次レポート：{DAILY_REPORT_HOUR:02d}:00 {BOT_TIMEZONE_NAME}\n\n"
                f"X Cookies：{'設定済み' if cookies_set else '未設定'}\n"
                f"一般ユーザーの Cookies：{state(self.acl.ordinary_user_cookies_enabled)}\n"
                f"ユーザー利用：{'許可' if self.acl.external_access_enabled else '停止'}\n"
                f"自動承認：{state(self.acl.auto_approve_enabled)}\n"
                f"管理モード：{state(self.acl.management_mode(viewer_id))}\n"
                f"実装方法：{state(self.acl.debug_mode(viewer_id))}"
            )
        return (
            "系統狀態\n\n"
            f"服務：{'正常' if any(worker.is_alive() for worker in self.workers) else '工作執行緒未運行'}\n"
            f"運行時間：{format_duration(time.time() - self.started_at)}\n"
            f"處理佇列：{queue_size}/{MAX_QUEUE}（{queue_percent}%）\n\n"
            "使用者\n"
            f"總記錄：{len(records)}｜普通：{ordinary}｜管理員：{administrators}｜"
            f"初始化：{initialized}｜待審批：{pending}｜封鎖：{banned}\n"
            f"今日活躍：{active_today}｜今日互動：{interactions}｜已達額度：{exhausted}\n"
            f"統計重置：每日 00:00 {BOT_TIMEZONE_NAME}\n"
            f"每日簡報：{DAILY_REPORT_HOUR:02d}:00 {BOT_TIMEZONE_NAME}\n\n"
            f"X Cookies：{'已設定' if COOKIES_PATH.exists() else '未設定'}\n"
            f"Cookies 開關："
            f"{'開啟' if self.acl.ordinary_user_cookies_enabled else '關閉'}\n"
            f"使用開關：{'開放' if self.acl.external_access_enabled else '暫停'}\n"
            f"自動通過：{'開啟' if self.acl.auto_approve_enabled else '關閉'}\n"
            f"管理模式：{'開啟' if self.acl.management_mode(viewer_id) else '關閉'}\n"
            f"實現方式：{'開啟' if self.acl.debug_mode(viewer_id) else '關閉'}"
        )

    def handle_callback(self, callback: dict[str, Any]) -> None:
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
        management_mode = self.acl.management_mode(user_id)

        if data.startswith("lang:"):
            if is_admin:
                self.api.answer_callback(callback_id, admin_text("language_locked"), alert=True)
                return
            selected = data.split(":", 1)[1]
            try:
                self.acl.set_language(user_id, selected)
            except ValueError:
                self.api.answer_callback(callback_id, "Unsupported language", alert=True)
                return
            is_allowed = self.acl.is_allowed(user_id)
            text_key = (
                "start_owner" if is_admin and management_mode else "start_allowed"
            )
            text = (
                public_text(selected, text_key)
                if is_allowed
                else access_request_text(user_id, selected)
            )
            self.api.edit_message(
                chat_id,
                message_id,
                text,
                start_keyboard(
                    selected,
                    is_admin,
                    is_allowed,
                    is_owner,
                    management_mode,
                ),
            )
            self.api.answer_callback(
                callback_id, public_text(selected, "language_set")
            )
            return

        if data.startswith("public:"):
            destination = data.split(":", 1)[1]
            back_keyboard = {"inline_keyboard": [[{
                "text": public_text(language, "back"),
                "callback_data": "public:main",
            }]]}
            if destination == "language":
                if is_admin:
                    self.api.answer_callback(callback_id, admin_text("language_locked"), alert=True)
                    return
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
                        if is_admin and management_mode
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
                        is_owner,
                        management_mode,
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

        if data == "managementtoggle:0":
            if not is_admin:
                self.api.answer_callback(
                    callback_id, admin_text("management_admin_only"), alert=True
                )
                return
            if not self.is_private_management_chat(user_id, chat_id):
                self.api.answer_callback(
                    callback_id, admin_text("private_only"), alert=True
                )
                return
            enabled = self.acl.toggle_management_mode(user_id)
            if enabled:
                text = admin_text("menu")
                keyboard = administrator_keyboard(is_owner, True)
            else:
                self.pending_user_searches.discard(user_id)
                text = public_text(language, "start_allowed")
                keyboard = start_keyboard(
                    language, True, True, is_owner, False
                )
            self.api.edit_message(chat_id, message_id, text, keyboard)
            self.api.answer_callback(
                callback_id, f"管理模式已{'開啟' if enabled else '關閉'}。" if OWNER_LANGUAGE == "zh" else f'{admin_text("management_mode")}: {admin_text("on" if enabled else "off")}'
            )
            return

        if not is_admin:
            self.api.answer_callback(callback_id, admin_text("admin_only"), alert=True)
            return
        if not management_mode:
            self.api.answer_callback(
                callback_id, admin_text("management_off"), alert=True
            )
            return
        if not self.is_private_management_chat(user_id, chat_id):
            self.api.answer_callback(
                callback_id, admin_text("private_only"), alert=True
            )
            return

        if data.startswith("nav:"):
            destination = data.split(":", 1)[1]
            self.pending_user_searches.discard(user_id)
            if destination == "main":
                text, keyboard = admin_text("menu"), administrator_keyboard(is_owner, True)
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
                keyboard = status_keyboard()
            elif destination == "advanced":
                text = self.system_status_text(user_id)
                keyboard = advanced_status_keyboard(
                    self.acl.debug_mode(user_id),
                    self.acl.external_access_enabled,
                    self.acl.auto_approve_enabled,
                    is_owner,
                )
            elif destination == "help":
                text, keyboard = owner_help_text(is_owner), administrator_keyboard(
                    is_owner, True
                )
            elif destination == "userlist":
                text, keyboard, _ = self.users_page(self.acl.records(), 0)
            elif destination == "requests":
                records = self.acl.pending()
                if records:
                    text, keyboard, _ = self.pending_page(records, 0)
                else:
                    text, keyboard = admin_text("no_requests"), user_menu_keyboard()
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
            self.api.answer_callback(callback_id, admin_text("current_page"))
            return

        try:
            action, target_text, *extra = data.split(":")
            target = int(target_text)
        except (ValueError, TypeError):
            self.api.answer_callback(callback_id, admin_text("invalid_action"), alert=True)
            return

        if action == "userspage":
            if target < 0:
                self.api.answer_callback(callback_id, admin_text("menu_invalid_page"), alert=True)
                return
            text, keyboard, _ = self.users_page(self.acl.records(), target)
            self.api.edit_message(
                chat_id, message_id, text, keyboard, parse_mode="HTML"
            )
            self.api.answer_callback(callback_id, admin_text("page_changed"))
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
                if quota < -1 or quota > 10000:
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
                if quota < -1 or quota > 10000:
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
                    chat_id, message_id, {"zh": f"找不到 User ID {target}。", "en": f"User ID {target} not found.", "ja": f"User ID {target} が見つかりません。"}[OWNER_LANGUAGE], user_menu_keyboard()
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
                status_keyboard(),
            )
            self.api.answer_callback(callback_id, admin_text("status_refreshed"))
            return
        if action == "debugtoggle":
            enabled = self.acl.toggle_debug_mode(user_id)
            self.api.edit_message(
                chat_id,
                message_id,
                self.system_status_text(user_id),
                advanced_status_keyboard(
                    enabled,
                    self.acl.external_access_enabled,
                    self.acl.auto_approve_enabled,
                    is_owner,
                ),
            )
            self.api.answer_callback(
                callback_id, f"實現方式已{'開啟' if enabled else '關閉'}。" if OWNER_LANGUAGE == "zh" else f'{admin_text("implementation")}: {admin_text("on" if enabled else "off")}'
            )
            return
        if action == "externaltoggle":
            if not is_owner:
                self.api.answer_callback(
                    callback_id, admin_text("access_owner_only"), alert=True
                )
                return
            enabled = self.acl.toggle_external_access(user_id)
            self.api.edit_message(
                chat_id,
                message_id,
                self.system_status_text(user_id),
                advanced_status_keyboard(
                    self.acl.debug_mode(user_id),
                    enabled,
                    self.acl.auto_approve_enabled,
                    is_owner,
                ),
            )
            self.api.answer_callback(
                callback_id, f"使用開關已{'開放' if enabled else '暫停'}。" if OWNER_LANGUAGE == "zh" else f'{admin_text("access_switch")}: {admin_text("open" if enabled else "paused")}'
            )
            return
        if action == "autoapprovetoggle":
            if not is_owner:
                self.api.answer_callback(
                    callback_id, admin_text("auto_owner_only"), alert=True
                )
                return
            enabled = self.acl.toggle_auto_approve(user_id)
            self.api.edit_message(
                chat_id,
                message_id,
                self.system_status_text(user_id),
                advanced_status_keyboard(
                    self.acl.debug_mode(user_id),
                    self.acl.external_access_enabled,
                    enabled,
                    is_owner,
                ),
            )
            self.api.answer_callback(
                callback_id, f"自動通過已{'開啟' if enabled else '關閉'}。" if OWNER_LANGUAGE == "zh" else f'{admin_text("auto_approve")}: {admin_text("on" if enabled else "off")}'
            )
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
                f"Cookies 開關已{'開啟' if enabled else '關閉'}。" if OWNER_LANGUAGE == "zh" else f'{admin_text("cookie_switch")}: {admin_text("on" if enabled else "off")}',
            )
            return
        if action == "requestspage":
            if target < 0:
                self.api.answer_callback(callback_id, admin_text("menu_invalid_page"), alert=True)
                return
            records = self.acl.pending()
            if not records:
                self.api.edit_message(chat_id, message_id, admin_text("no_requests"))
            else:
                text, keyboard, _ = self.pending_page(records, target)
                self.api.edit_message(chat_id, message_id, text, keyboard)
            self.api.answer_callback(callback_id, admin_text("page_changed"))
            return

        if action == "approvepage":
            if target < 0:
                self.api.answer_callback(callback_id, admin_text("menu_invalid_page"), alert=True)
                return
            records = self.acl.pending()
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
                            limit=DEFAULT_DAILY_LIMIT,
                        ),
                    )
                except (requests.RequestException, RuntimeError):
                    LOG.exception(
                        "Could not notify approved user %s", applicant_id
                    )
            remaining = self.acl.pending()
            if remaining:
                text, keyboard, _ = self.pending_page(remaining, target)
                self.api.edit_message(chat_id, message_id, text, keyboard)
            else:
                self.api.edit_message(
                    chat_id,
                    message_id,
                    admin_text("no_requests"),
                    user_menu_keyboard(),
                )
            self.api.answer_callback(callback_id, {"zh": f"已通過 {approved} 筆申請。", "en": f"Approved {approved} requests.", "ja": f"{approved} 件の申請を承認しました。"}[OWNER_LANGUAGE])
            return

        if action == "approve":
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            if not self.acl.approve(target):
                self.api.answer_callback(callback_id, admin_text("approve_missing"), alert=True)
                return
            self.api.answer_callback(callback_id, {"zh": f"已通過 {target}。", "en": f"Approved {target}.", "ja": f"{target} を承認しました。"}[OWNER_LANGUAGE])
            try:
                self.api.send_message(
                    target,
                    public_text(
                        self.acl.language(target),
                        "approved",
                        limit=DEFAULT_DAILY_LIMIT,
                    ),
                )
            except (requests.RequestException, RuntimeError):
                LOG.exception("Could not notify approved user %s", target)
            if message_id and extra:
                try:
                    page = max(0, int(extra[0]))
                except (ValueError, TypeError):
                    page = 0
                records = self.acl.pending()
                if records:
                    text, keyboard, _ = self.pending_page(records, page)
                    self.api.edit_message(chat_id, message_id, text, keyboard)
                else:
                    self.api.edit_message(chat_id, message_id, admin_text("no_requests"))
        elif action == "deny":
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            self.acl.deny(target)
            self.api.answer_callback(callback_id, {"zh": f"已拒絕 {target}。", "en": f"Rejected {target}.", "ja": f"{target} を拒否しました。"}[OWNER_LANGUAGE])
            if message_id and extra:
                try:
                    page = max(0, int(extra[0]))
                except (ValueError, TypeError):
                    page = 0
                records = self.acl.pending()
                if records:
                    text, keyboard, _ = self.pending_page(records, page)
                    self.api.edit_message(chat_id, message_id, text, keyboard)
                else:
                    self.api.edit_message(chat_id, message_id, admin_text("no_requests"))
        elif action in {"quotamenu", "limitmenu"}:
            error = self.target_management_error(user_id, target)
            if error:
                self.api.answer_callback(callback_id, error, alert=True)
                return
            self.api.answer_callback(callback_id, admin_text("choose_access"))
            self.api.edit_message(
                chat_id,
                message_id,
                {"zh": f"修改 User ID {target} 的用戶權限：", "en": f"Change access for User ID {target}:", "ja": f"User ID {target} の権限を変更："}[OWNER_LANGUAGE],
                quota_choices_keyboard(target, allow_admin=is_owner),
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
            self.api.answer_callback(callback_id, {"zh": f"已設為 {label}。", "en": f"Set to {label}.", "ja": f"{label} に設定しました。"}[OWNER_LANGUAGE])
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
                    chat_id, {"zh": "Cookies 只允許在 Bot 私聊匯入。", "en": "Import Cookies only in a private chat with the bot.", "ja": "Cookies の取り込みは Bot との個別チャットでのみ可能です。"}[OWNER_LANGUAGE], message_id
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
                {"zh": "請先點選「匯入 Cookies」，再上傳 Netscape 格式 cookies.txt。", "en": "Select Import Cookies before uploading a Netscape-format cookies.txt file.", "ja": "先に Cookies を取り込むを選択し、Netscape 形式の cookies.txt をアップロードしてください。"}[OWNER_LANGUAGE],
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
            return
        file_size = int(document.get("file_size", 0) or 0)
        file_id = str(document.get("file_id", ""))
        if not file_id or file_size > MAX_COOKIE_BYTES:
            self.api.send_message(
                chat_id,
                {"zh": "Cookies 檔案無效或超過 1 MB。", "en": "Invalid Cookies file or larger than 1 MB.", "ja": "Cookies ファイルが無効か、1 MB を超えています。"}[OWNER_LANGUAGE],
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
                }[OWNER_LANGUAGE],
                message_id,
                cookie_menu_keyboard(self.acl.ordinary_user_cookies_enabled),
            )
            return
        self.pending_cookie_uploads.discard(user_id)
        self.api.send_message(
            chat_id,
            {"zh": "X/Twitter Cookies 已匯入並立即生效。Telegram 中的原始文件可自行刪除。", "en": "X/Twitter Cookies imported and active. You can delete the original document from Telegram.", "ja": "X/Twitter の Cookies を取り込み、すぐに反映しました。Telegram の元ファイルは削除できます。"}[OWNER_LANGUAGE],
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
        is_owner = actor_id == self.acl.owner_id
        if command == "/menu":
            self.pending_user_searches.discard(actor_id)
            self.api.remove_reply_keyboard(chat_id)
            self.api.send_message(
                chat_id,
                admin_text("menu_ready"),
                message_id,
                administrator_keyboard(is_owner),
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
                self.api.send_message(chat_id, {"zh": f"用法：{command} <User ID>", "en": f"Usage: {command} <User ID>", "ja": f"使い方：{command} <User ID>"}[OWNER_LANGUAGE], message_id)
                return
            error = self.target_management_error(actor_id, target)
            if error:
                self.api.send_message(chat_id, error, message_id)
                return
            if command == "/allow":
                self.acl.add(target)
                response = {"zh": f"已允許 {target}。", "en": f"Allowed {target}.", "ja": f"{target} を許可しました。"}[OWNER_LANGUAGE]
            elif command == "/deny":
                self.acl.remove(target)
                response = {"zh": f"已移除 {target}。", "en": f"Removed {target}.", "ja": f"{target} を削除しました。"}[OWNER_LANGUAGE]
            elif command == "/ban":
                try:
                    self.acl.ban(target)
                except ValueError:
                    self.api.send_message(chat_id, admin_text("owner_cannot_ban"), message_id)
                    return
                response = {"zh": f"已永久封鎖 {target}。", "en": f"Blocked {target}.", "ja": f"{target} をブロックしました。"}[OWNER_LANGUAGE]
            else:
                self.acl.unban(target)
                response = {"zh": f"已解除 {target} 的永久封鎖。", "en": f"Unblocked {target}.", "ja": f"{target} のブロックを解除しました。"}[OWNER_LANGUAGE]
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
                    {"zh": "用法：/limit <User ID> <-1|0|每日次數|unlimited>。", "en": "Usage: /limit <User ID> <-1|0|daily limit|unlimited>.", "ja": "使い方：/limit <User ID> <-1|0|1日の上限|unlimited>。"}[OWNER_LANGUAGE],
                    message_id,
                )
                return
            self.api.send_message(
                chat_id,
                {"zh": f"已將 {target} 設為 {admin_text('admin_unlimited') if quota is None else quota}。", "en": f"Set {target} to {admin_text('admin_unlimited') if quota is None else quota}.", "ja": f"{target} を {admin_text('admin_unlimited') if quota is None else quota} に設定しました。"}[OWNER_LANGUAGE],
                message_id,
            )
        elif command == "/status":
            self.api.send_message(
                chat_id,
                self.system_status_text(actor_id),
                message_id,
                status_keyboard(),
            )
        elif command == "/help":
            self.api.send_message(
                chat_id,
                owner_help_text(is_owner),
                message_id,
                administrator_keyboard(is_owner),
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
            self.pending_cookie_uploads.discard(actor_id)
            self.api.send_message(
                chat_id, admin_text("cancelled"), message_id, administrator_keyboard(is_owner)
            )
        else:
            self.api.send_message(
                chat_id,
                owner_help_text(is_owner),
                message_id,
                administrator_keyboard(is_owner),
            )

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                chat_id, message_id, user_id, url = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process_url(chat_id, message_id, user_id, url)
                self.acl.finish_job(chat_id, message_id)
            except Exception:
                LOG.exception("Tweet processing failed")
                try:
                    if not self.can_process(user_id):
                        self.acl.finish_job(chat_id, message_id)
                        continue
                    self.api.send_message(
                        chat_id,
                        public_text(self.acl.language(user_id), "failed"),
                        message_id,
                    )
                    self.acl.finish_job(chat_id, message_id)
                except Exception:
                    LOG.exception("Could not send failure response")
            finally:
                self.jobs.task_done()

    def notify_cookie_failure(self) -> None:
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
                }[OWNER_LANGUAGE],
                reply_markup=start_keyboard(
                    self.acl.language(owner_id),
                    True,
                    True,
                    True,
                    self.acl.management_mode(owner_id),
                ),
            )
            record_cookie_alert()
        except (OSError, requests.RequestException, RuntimeError):
            LOG.exception("Could not notify owner about invalid cookies")

    def process_url(
        self, chat_id: int, message_id: int, user_id: int, url: str
    ) -> None:
        if not self.can_process(user_id):
            return
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
        with tempfile.TemporaryDirectory(prefix="tweet-", dir=TMP_DIR) as temporary:
            directory = Path(temporary)
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
            sent_messages: list[dict[str, Any]] = []
            if not self.can_process(user_id):
                return
            if previews:
                try:
                    sent_messages = self.api.send_previews(
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
                        names=", ".join(rejected_other),
                    ),
                )
            if not files:
                LOG.warning("No media downloaded for %s: %s", effective_url, extractor_log)


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
