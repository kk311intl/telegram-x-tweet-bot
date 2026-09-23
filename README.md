# X/Twitter → Telegram Bot · v3.1.1

[中文](#中文) / [日本語](#日本語) / [English](#english)

## 中文

**給 AI coding agent 的無代碼部署提示詞**

> 請先讀取這個 repository 的 README、`deploy.sh`、`bot.py`、`config_cli.py`、systemd unit 和測試，再協助我在自己的 Debian／Ubuntu Linux 主機部署這個以 Python、systemd 和 Telegram Bot API 運行的 X/Twitter 單篇貼文媒體 Bot。我沒有程式經驗，請逐步說明每條命令的用途與預期結果，只詢問真正缺少的資料（主機登入方式、Bot Token、我的 Telegram User ID、管理介面語言與時區）。Telegram Bot 的建立、主機登入授權、Token 的安全輸入和任何 SSH 金鑰確認須由我本人完成；不要要求我把 Secret 貼到聊天、issue 或 Git。先查是否已有同名服務與資料，未經確認不得覆寫或重啟既有部署，不要重設原有用戶資料、Webhook 或憑證。依現有工程部署，不自行改寫架構；每一步執行後檢查結果，排錯後再繼續，最後驗證服務、Bot `/start`、一條公開貼文及三種介面語言（管理語言由部署設定，普通用戶自行切換）。對未實際驗證的步驟明確標示，不要聲稱已完成。

這個 Bot 接收單篇 `x.com`／`twitter.com` 貼文網址，回傳作者連結、文字與媒體；支援授權、每日額度及內聯分享。普通用戶可各自選繁體中文、日本語或 English；Owner 與管理員的介面語言由部署時的 `OWNER_LANGUAGE` 固定設定。

需求：一台可連 Telegram 與 X 的 Debian／Ubuntu Linux 主機、root/sudo、Python 3.10+（部署腳本會安裝 Python 3.12 執行環境）、systemd、可用的 Telegram Bot Token。部署腳本會安裝 `ffmpeg` 與 Python 依賴，建立專用系統用戶及 `/var/lib/x-tweet-telegram-bot` 私有資料目錄。Bot 使用 long polling，不需要 Webhook 或入站埠。依賴版本見 `requirements.txt`。

在全新主機上 clone 本 repository，於工程目錄執行：

```sh
sudo bash deploy.sh
sudo x-tweet-bot-config set-token
sudo x-tweet-bot-config set-owner YOUR_TELEGRAM_USER_ID
sudo x-tweet-bot-config status
systemctl status x-tweet-telegram-bot.service --no-pager
```

`set-token` 在本機隱藏輸入並向 Telegram 驗證；請勿把 Token 寫在命令列。`/id` 可取得自己的 Telegram User ID。`deploy.sh` 初次啟動時如尚未設定 Token，服務只會等待設定，不會對 Telegram 發訊。設定保存在 root 才能讀的 `/etc/x-tweet-telegram-bot.env`；不要提交它。修改後 `sudo systemctl restart x-tweet-telegram-bot.service`。

| 設定 | 預設 | 用途 |
| --- | --- | --- |
| `OWNER_LANGUAGE` | `zh` | Owner／管理員介面：`zh`、`ja`、`en`。不改變普通用戶的個別語言。 |
| `BOT_TIMEZONE` | `UTC` | IANA 時區，如 `Asia/Tokyo`；用於每日額度與簡報。 |
| `DAILY_RESET_HOUR` | `0` | 該時區每日換日的整點，0–23。 |
| `DAILY_REPORT_HOUR` | `22` | 該時區 0–23 點，向 Owner 發送每日用量與待審摘要。 |
| `DEFAULT_DAILY_LIMIT` | `50` | 新獲授權的普通用戶預設每日額度，1–100000；不改動既有用戶額度。 |
| `OWNER_CONTACT_URL` | 空白 | 可選的使用說明聯絡連結，如 `https://example.com/contact`；空白則不顯示。 |
| `OWNER_CONTACT_LABEL` | 空白 | 可選的聯絡連結文字；空白時使用對應語言的預設文字。 |
| `WORKER_COUNT` | `2` | 貼文處理並行數，範圍 1–8。 |

其他可調參數有 `MAX_QUEUE`、`MAX_MEDIA_BYTES`、`MAX_TOTAL_BYTES`、`INLINE_WORKER_COUNT`、`INLINE_MAX_PENDING`、`INLINE_CACHE_SECONDS`；預設值與安全範圍見 `deploy.sh`、`bot.py`。每日簡報按當地曆日只發一次；若設定在換日前，統計屬上一個額度日。Owner 可在私聊管理授權、Cookies、普通用戶開關及自動通過。Cookies 是登入憑證，只在需要登入型媒體時匯入，建議用獨立帳號；原始檔不要提交或轉傳。普通用戶的語言按鈕、申請流程及每日額度由程式管理；自動通過的狀態不會透露給未授權用戶。內聯分享須先在 BotFather 開啟 Inline Mode。

管理介面只在 Bot 私聊使用：傳送 User ID 查找，或傳送「User ID 額度」並確認修改；-1 封鎖、0 初始化、正數為每日上限，只有 Owner 能授予不限額的管理員權限。圖片會附原始檔，影片上限 50 MB；引用貼文不會遞迴擷取。媒體先嘗試 FxTwitter／twimg 直連，再依序使用 gallery-dl、yt-dlp 的匿名或 Cookies 模式；普通用戶的 Cookies 階段受 Owner 開關控制。

驗證：在工程目錄執行 `python3 -m unittest -q test_bot.py`（需先安裝 `requirements.txt`），部署後可執行 `sudo /opt/x-tweet-telegram-bot/verify_deploy.sh` 檢查資料與網路監聽；加 `TEST_URL=https://x.com/example/status/123456789`（換成真實公開單篇貼文）才會進行網路擷取測試。最後須本人在 Telegram 實際傳送公開貼文確認媒體輸出；自動測試不能代替此步。

自有原始碼 © 2026 kk311intl，以 [GNU GPL v3.0 only](LICENSE)（`GPL-3.0-only`）授權。`requests`、`Pillow`、`yt-dlp`、`gallery-dl` 各自遵循上游授權；再分發含依賴的執行包時須另外核對其義務。

## 日本語

**AI coding agent に渡す、コードを書かずに導入するための指示**

> まずこの repository の README、`deploy.sh`、`bot.py`、`config_cli.py`、systemd unit、テストを読んでください。そのうえで、Python・systemd・Telegram Bot API を使う X/Twitter 単一投稿メディア Bot を、私の Debian／Ubuntu Linux サーバーへ導入してください。私はプログラミングに詳しくありません。各コマンドの意味と期待結果を説明し、サーバーへのログイン方法、Bot Token、私の Telegram User ID、管理画面の言語、タイムゾーンなど、本当に不足している値だけを尋ねてください。Bot の作成、ログイン承認、Token の安全な入力、SSH 鍵の確認は私自身が行います。Secret をチャット・issue・Git に貼らせないでください。同名サービスや既存データを先に調べ、確認なしに既存環境を上書き・再起動せず、ユーザーデータ、Webhook、資格情報を初期化しないでください。構成を独断で変えず、各段階を実行して結果を確認し、最後にサービス、Bot の `/start`、公開投稿1件、3言語表示を検証してください。管理者の言語は導入設定、一般ユーザーの言語は本人の選択です。未確認の作業を完了済みと報告しないでください。

この Bot は `x.com`／`twitter.com` の単一投稿URLから、投稿者リンク、本文、メディアを返します。利用許可、日次上限、インライン共有にも対応します。一般ユーザーは繁體中文・日本語・English を個別に選択でき、所有者と管理者の表示言語は `OWNER_LANGUAGE` で固定します。

必要なものは、Telegram と X に接続できる Debian／Ubuntu Linux、root/sudo、Python 3.10+（導入スクリプトは Python 3.12 の実行環境を準備）、systemd、Telegram Bot Token です。`deploy.sh` は `ffmpeg` と Python 依存関係を導入し、専用ユーザーと `/var/lib/x-tweet-telegram-bot` を作ります。通信は long polling で、Webhook や受信ポートは不要です。依存バージョンは `requirements.txt` を参照してください。

新しいサーバーで repository を clone し、工程ディレクトリで実行します：

```sh
sudo bash deploy.sh
sudo x-tweet-bot-config set-token
sudo x-tweet-bot-config set-owner YOUR_TELEGRAM_USER_ID
sudo x-tweet-bot-config status
systemctl status x-tweet-telegram-bot.service --no-pager
```

`set-token` は端末で非表示入力し、Telegram で検証します。Token をコマンド引数に入れないでください。自分の Telegram User ID は `/id` で確認できます。初回起動時に Token がなければ、Bot は設定を待つだけです。設定は root のみが読める `/etc/x-tweet-telegram-bot.env` に保存します。Git に入れないでください。設定変更後は `sudo systemctl restart x-tweet-telegram-bot.service` を実行します。

| 設定 | 初期値 | 用途 |
| --- | --- | --- |
| `OWNER_LANGUAGE` | `zh` | 所有者・管理者の表示：`zh`、`ja`、`en`。一般ユーザーの個別設定には影響しません。 |
| `BOT_TIMEZONE` | `UTC` | `Asia/Tokyo` などの IANA タイムゾーン。日次上限とレポートに適用します。 |
| `DAILY_RESET_HOUR` | `0` | 日次上限を切り替える現地時刻（0–23 時）。 |
| `DAILY_REPORT_HOUR` | `22` | その地域の 0–23 時。利用状況と審査待ちを所有者へ通知します。 |
| `DEFAULT_DAILY_LIMIT` | `50` | 新しく許可した一般ユーザーの1日あたりの初期上限（1–100000）。既存の上限は変更しません。 |
| `OWNER_CONTACT_URL` | 空欄 | 任意の問い合わせ先URL（例：`https://example.com/contact`）。空欄なら表示しません。 |
| `OWNER_CONTACT_LABEL` | 空欄 | 任意の連絡先リンク表示名。空欄なら各言語の初期表示名を使います。 |
| `WORKER_COUNT` | `2` | 投稿処理の並列数。1–8。 |

`MAX_QUEUE`、`MAX_MEDIA_BYTES`、`MAX_TOTAL_BYTES`、`INLINE_WORKER_COUNT`、`INLINE_MAX_PENDING`、`INLINE_CACHE_SECONDS` も調整できます。初期値と有効範囲は `deploy.sh` と `bot.py` を参照してください。レポートは現地の暦日ごとに1回送信し、切り替え前の時刻に設定した場合は前の利用日を集計します。所有者は個別チャットで権限、Cookies、一般ユーザーの利用、自動承認を管理できます。Cookies はログイン資格情報です。必要な場合だけ、専用アカウントを使って取り込み、元ファイルを Git に入れたり転送したりしないでください。一般ユーザーは個別に言語を選び、利用申請と上限を利用できます。自動承認の状態は未承認ユーザーへ表示されません。インライン共有には BotFather で Inline Mode を有効にしてください。

管理操作は Bot との個別チャットのみで行います。User ID で検索し、「User ID 上限値」で変更して確認します。-1 はブロック、0 は初期化、正の数は1日の上限で、無制限の管理者権限を付与できるのは所有者だけです。画像には元ファイルを添付し、動画は 50 MB まで。引用先はたどりません。メディアは FxTwitter／twimg の直リンクを優先し、次に gallery-dl、yt-dlp の匿名・Cookies モードを試します。一般ユーザーの Cookies 使用は所有者の設定に従います。

テストは依存関係を入れたうえで `python3 -m unittest -q test_bot.py`。導入後は `sudo /opt/x-tweet-telegram-bot/verify_deploy.sh` でデータと受信ポートを確認できます。実際の公開単一投稿URLを `TEST_URL=https://x.com/example/status/123456789` の形で渡した場合だけ、ネットワーク経由の取得も確認します。最後に本人が Telegram で投稿URLを送ってメディアを確認してください。自動テストだけでは代替できません。

自作コードは © 2026 kk311intl、[GNU GPL v3.0 only](LICENSE)（`GPL-3.0-only`）で公開します。`requests`、`Pillow`、`yt-dlp`、`gallery-dl` は各自の上流ライセンスに従います。依存関係を含む実行形式を再配布する場合は、その義務も確認してください。

## English

**No-code deployment prompt for an AI coding agent**

> Read this repository's README, `deploy.sh`, `bot.py`, `config_cli.py`, systemd units, and tests first. Then help me deploy this Python/systemd X/Twitter single-post media bot, which uses the Telegram Bot API, on my own Debian or Ubuntu Linux server. I am not a programmer: explain each command and its expected result, and ask only for missing values such as server access, Bot Token, my Telegram User ID, administrator language, and time zone. I must personally create the bot, approve logins, enter the Token securely, and confirm SSH keys. Do not ask me to paste secrets into chat, issues, or Git. Check for existing services and data before acting; do not overwrite or restart an existing installation without confirmation, and do not reset users, Webhooks, or credentials. Follow the current project without redesigning it. Execute and check each step, troubleshoot failures, and finally verify the service, `/start`, one public post, and all three interface languages (administrator language is deployment-wide; regular users choose individually). Mark any step you could not actually verify instead of claiming success.

Send a single `x.com` or `twitter.com` post URL to receive its author link, text, and media. The bot also supports access approval, daily limits, and inline sharing. Regular users choose 繁體中文, 日本語, or English individually; the owner and administrators use one deployment setting, `OWNER_LANGUAGE`.

You need a Debian or Ubuntu Linux server that can reach Telegram and X, root/sudo, Python 3.10+ (the deployment script installs a Python 3.12 runtime), systemd, and a Telegram Bot Token. `deploy.sh` installs `ffmpeg` and Python packages, then creates a dedicated system user and private state directory at `/var/lib/x-tweet-telegram-bot`. It uses long polling, so no Webhook or inbound port is needed. Pinned dependencies are in `requirements.txt`.

Clone this repository onto a fresh server and run from its directory:

```sh
sudo bash deploy.sh
sudo x-tweet-bot-config set-token
sudo x-tweet-bot-config set-owner YOUR_TELEGRAM_USER_ID
sudo x-tweet-bot-config status
systemctl status x-tweet-telegram-bot.service --no-pager
```

`set-token` accepts hidden terminal input and validates the Token with Telegram; never put it in command arguments. Send `/id` to the bot to find your Telegram User ID. Before the Token is configured, the first service start just waits. The settings file is `/etc/x-tweet-telegram-bot.env`, readable only by root; never commit it. After editing settings, run `sudo systemctl restart x-tweet-telegram-bot.service`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `OWNER_LANGUAGE` | `zh` | Owner/admin UI: `zh`, `ja`, or `en`. Does not change individual user languages. |
| `BOT_TIMEZONE` | `UTC` | IANA zone such as `Asia/Tokyo`, used for daily limits and reports. |
| `DAILY_RESET_HOUR` | `0` | Local hour when the daily usage period rolls over, 0–23. |
| `DAILY_REPORT_HOUR` | `22` | Hour 0–23 in that zone for the owner's usage/pending report. |
| `DEFAULT_DAILY_LIMIT` | `50` | Initial daily quota for newly approved regular users, 1–100000; existing quotas stay unchanged. |
| `OWNER_CONTACT_URL` | empty | Optional help-page contact link, e.g. `https://example.com/contact`; omitted when empty. |
| `OWNER_CONTACT_LABEL` | empty | Optional contact-link text; defaults to a translated label. |
| `WORKER_COUNT` | `2` | Concurrent post jobs, from 1 to 8. |

You can also tune `MAX_QUEUE`, `MAX_MEDIA_BYTES`, `MAX_TOTAL_BYTES`, `INLINE_WORKER_COUNT`, `INLINE_MAX_PENDING`, and `INLINE_CACHE_SECONDS`; see `deploy.sh` and `bot.py` for defaults and bounds. The report is sent once per local calendar day; if its time precedes the reset, it summarizes the previous usage day. The owner can manage access, Cookies, regular-user availability, and auto-approval in a private chat. Cookies are login credentials: import them only when needed, preferably from a dedicated account, and never commit or forward the file. Regular users keep their own language selection, access requests, and daily limits. Unapproved users are not told whether auto-approval is enabled. Enable Inline Mode in BotFather if you want inline sharing.

Management works only in a private chat with the bot. Send a User ID to search, or a User ID and quota to change access and confirm it: -1 blocks, 0 initializes, and a positive number sets the daily limit. Only the owner can grant unlimited administrator access. Images include original files; videos are capped at 50 MB, and quoted posts are not followed. Media retrieval prefers FxTwitter／twimg direct links, then gallery-dl and yt-dlp anonymously or with Cookies; the owner's switch controls Cookie use for regular users.

For local tests, install `requirements.txt` and run `python3 -m unittest -q test_bot.py`. After deployment, `sudo /opt/x-tweet-telegram-bot/verify_deploy.sh` checks state and inbound listeners. Set `TEST_URL=https://x.com/example/status/123456789` to a real public single-post URL to include network retrieval. Finally, personally send a public post to the bot in Telegram and inspect the media; automated checks do not replace that test.

Project source © 2026 kk311intl is licensed under [GNU GPL v3.0 only](LICENSE) (`GPL-3.0-only`). `requests`, `Pillow`, `yt-dlp`, and `gallery-dl` retain their upstream licenses; check their obligations separately if you redistribute a bundled build.
