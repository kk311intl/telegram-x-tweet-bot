# Telegram X／Twitter 貼文媒體 Bot · v3.10.1

[中文](#中文) / [日本語](#日本語) / [English](#english)

Demo: [@TwitterPreviewerBot](https://t.me/TwitterPreviewerBot)

## 中文

不熟悉程式時，可把儲存庫連結和以下提示詞交給能讀檔、執行指令的 AI：

> 請先讀這個儲存庫的 README、VERSION、bot.py、video_downloader.py、config_cli.py、requirements.txt、deploy.sh、systemd 服務檔及測試，再帶我在自己的 Debian／Ubuntu 主機安裝目前 main 分支的程式。這個 Bot 用 Python、systemd、Telegram Bot API、gallery-dl／yt-dlp 和 Pillow 取得 X/Twitter 單篇貼文的文字與媒體。我不會寫程式，請解釋命令及預期結果，只問尚缺的主機登入方式、Bot Token、Telegram 用戶 ID 等必要資料，其餘使用文件預設。建立 Bot、登入授權、安全輸入 Token／Cookies 和確認 SSH 金鑰由我親自完成；不要讓我把憑證貼到聊天或 Git。先檢查既有服務與資料，未經確認不得覆寫、重啟或重設已有部署、Webhook、用戶資料或憑證。沿用現有程式，不改寫架構。最後檢查服務、/start、公開貼文及四種介面語言，帶我在 Telegram 確認收到文字與媒體；無法驗證的步驟請明說。

傳送單篇 X/Twitter 貼文網址，即可取得作者連結、文字、圖片與影片，也可內聯分享。所有用戶，包括所有者與管理員，都能在「Language」選擇简体中文、繁體中文、English、日本語。介面預設繁體中文（`zh`），不隨客戶端語言自動切換；指令選單與 Bot 簡介則配合客戶端語言。

已接收的貼文會在原訊息加上 👀，處理結束後移除；無法識別的一般內容標示 🤔，並保留文字提示。

以下指令使用 main 的最新程式。GitHub 的「Latest Release」只代表最近一次正式發布，可能落後於 main；Release 說明與 tag 對應當時版本。

### 部署

需要 Debian／Ubuntu、systemd、git、Python 3.10+ 和 root 或 sudo 權限。主機須能連接 Telegram 與媒體來源。部署腳本會準備 Python 3.12 及缺少的 ffmpeg，在獨立環境測試後才切換；服務或設定檢查失敗會還原程式。Bot 採長輪詢（long polling），不需 Webhook 或開放入站埠。

先在 BotFather 建立 Bot。以下指令用於尚未安裝本 Bot 的主機，最後一步會在終端隱藏輸入 Token；不要把 Token 放進命令引數。

```sh
git clone https://github.com/kk311intl/telegram-x-tweet-bot.git
cd telegram-x-tweet-bot
sudo bash deploy.sh
sudo x-tweet-bot-config set-token
```

未設定 Token 時服務會等待。設定後向 Bot 傳送 `/id`，取得自己的用戶 ID，再執行：

```sh
sudo x-tweet-bot-config set-owner YOUR_TELEGRAM_USER_ID
sudo x-tweet-bot-config status
systemctl status x-tweet-telegram-bot.service --no-pager
```

### 設定與使用

設定在 `/etc/x-tweet-telegram-bot.env`，僅 root 可讀；狀態在 `/var/lib/x-tweet-telegram-bot`。不要把這些檔案或其備份放進 Git。

修改額度初值、聯絡連結、時區與排程、媒體大小、佇列上限、暫存限制或 Inline 快取時間後，執行 `sudo x-tweet-bot-config reload`。Bot 會暫停接收，等已接收的工作完成後一起套用；設定無效則不套用，最多等待 10 分鐘。命令逾時或等待期間服務重啟時，結果未確認，不代表設定未套用；先確認服務再重試。Bot 內已儲存的預設額度仍優先，既有用戶額度不變。時區／換日調整不會重算歷史統計。Token、身份與並行數仍需重啟。熱載入只傳遞白名單設定，不讓 Bot 讀取 root 的環境檔。

| 設定 | 預設 | 用途 |
| --- | --- | --- |
| `BOT_TIMEZONE` | UTC | 每日額度與簡報使用的 IANA 時區。 |
| `DAILY_RESET_HOUR` | 0 | 該時區每日換日的整點，0–23。 |
| `DAILY_REPORT_HOUR` | 0 | 向所有者發送用量與待審申請簡報的整點，0–23。 |
| `DEFAULT_DAILY_LIMIT` | 50 | 新授權普通用戶額度，1–100000；Bot 內設定優先，不批次改現有額度。 |
| `OWNER_CONTACT_URL` | 空白 | 使用說明頁的「問題反饋」按鈕連結，例如 https://t.me/YOUR_USERNAME；空白則不顯示。 |
| `WORKER_COUNT` | 2 | 貼文並行數，1–8。 |
| `MIN_FREE_DISK_BYTES` | 1073741824 | 可用空間低於 1 GiB 時不接受新下載。 |
| `TEMP_SOFT_LIMIT_BYTES` | 1073741824 | 媒體暫存達到 1 GiB 時不接受新下載；非硬配額。 |
| `TEMP_RETENTION_HOURS` | 24 | 清理過期且未使用的媒體暫存；啟動時及每小時檢查。 |

簡報與換日設為同一時刻時，統計剛結束的完整額度日；否則顯示發送當下的用量。例如兩者都設為 0，就會在午夜推送前一天的完整統計。用量按扣除額度的次數計算，不等於成功傳送次數。其他上限與設定範圍見 `bot.py`，依賴版本見 `requirements.txt`。

管理操作在 Bot 私聊進行：輸入用戶 ID 查詢詳情，或輸入 `ID 額度` 並確認修改。`-1` 為封鎖，`0` 為未授權的初始化狀態，正數為每日額度。只有所有者能授予不限額的管理員權限，也只有所有者能使用高級選項與用戶控制。輸入 `/cancel` 可取消。

「預設額度」只影響之後新授權的普通用戶。「批量修改」輸入 `A B`（例如 `50 100`），確認後將現有額度為 A 的已授權普通用戶改為 B，不改預設額度或當日用量。A、B 須為不同的 1–100000 整數；初始化、待審、封鎖、所有者及管理員不參與。列表以 CNS／CNT／JA／EN 顯示語言，完整用戶資料可用 ID 查詢。

內聯分享須在 BotFather 開啟 Inline Mode。Cookies 可由所有者匯入，普通用戶是否使用則由所有者開關控制。只在需要時匯入，建議使用獨立帳號；Cookies 是登入憑證，不要提交或轉傳。

圖片向 X 請求原圖尺寸（`orig`），不以縮圖替代原檔；原圖並非一定有 4K。影片從高畫質開始，超過下載上限就清除暫存並嘗試下一級，匿名來源與混合貼文亦同；不重新壓縮。圖片與影片均附送下載檔案，預覽可能另行縮放。影片仍受 Telegram 的 50 MB 上限限制。長文會節錄並保留原文連結，不繼續取得引用貼文。來源不可用或所有版本均超限時，可能只能取得部分內容。

X/Twitter 的 GIF 使用來源 MP4 預覽及文件附件，不轉為 `.gif`；多媒體貼文維持相簿預覽。

用量總覽提供今日、7 日及 30 日統計，按設定的換日時間計算並包含今天；活躍人數跨日去重。每日用量保留 30 天，歷史不足時只計算已記錄的用量。

「運行狀態」顯示 Bot 與下載器最近 24 小時的流量，按分鐘估算，重啟後保留。紀錄不足 24 小時會註明；無法取得時不會用整臺主機的流量代替。此功能需要 systemd 的 `IPAccounting=yes`。

### 維護與驗證

更新前先備份設定與狀態，確認工作目錄沒有未保存的修改，再於原儲存庫目錄更新 main：

```sh
git switch main
git pull --ff-only
sudo bash deploy.sh
```

部署先測試候選版本並建立程式還原副本，再等目前工作完成才重啟，保留設定與用戶資料；等待逾時取消更新並恢復接收，切換失敗會回滾程式。這是短暫停機更新，不是程式碼原地熱替換；首次從 3.8.x 升級仍走原有重啟流程。維護時可用 `sudo x-tweet-bot-config drain` 暫停接收，最多等待工作完成 10 分鐘，成功後保留 120 秒交接期，再自動恢復接收。`sudo x-tweet-bot-config resume` 可提早恢復，也可取消手動等待中的請求；部署期間會鎖定控制操作，不允許另一個 `reload`／`resume` 打斷切換。Git 無法直接更新時先處理分歧，不重設本機修改。版本號相同但檔案不同時，部署腳本會拒絕更新；先核對來源，不強行覆蓋。

新安裝的簡報預設為 UTC 00:00。已有環境檔的排程不會被升級覆寫；若原先設為 22 點，需自行將 `DAILY_REPORT_HOUR` 改為 `0` 並執行 `sudo x-tweet-bot-config reload`。

檢查服務與日誌可用 `systemctl status x-tweet-telegram-bot.service` 及 `sudo journalctl -u x-tweet-telegram-bot.service -n 50 --no-pager`。分享日誌前先遮蔽私人內容。

本儲存庫不含異機備份系統。`export-access` 可不停機匯出權限與預設額度，不含姓名、語言、用量或憑證。`import-access` 只允許在服務完全停止（`inactive` 或 `failed`，且 `MainPID=0`）時匯入，不覆蓋較新資料。

完整復原還需要環境設定與狀態目錄的備份，內容須來自同一時間點，並保留原檔所有權與權限。不要直接覆寫運行中的資料。程式不會為騰出磁碟空間自動刪除憑證、用戶資料或損壞後保留的復原檔。

安裝 `requirements.txt` 中的依賴後，可執行 `python3 -m unittest -q test_bot.py`。部署後先檢查服務與狀態，再測試下載；第二行請換成真實公開且含文字與媒體的貼文網址：

```sh
sudo /opt/x-tweet-telegram-bot/verify_deploy.sh
sudo env TEST_URL='https://x.com/example/status/123456789' /opt/x-tweet-telegram-bot/verify_deploy.sh
```

下載測試不使用 Cookies、不發送 Telegram 訊息，結束後會清除暫存。最後仍須在 Telegram 確認文字與媒體接收。

原始碼 © 2026 kk311intl，採 [GNU GPL v3.0 only](LICENSE)（`GPL-3.0-only`）。requests、requests-toolbelt、Pillow、yt-dlp、gallery-dl 各自沿用上游授權；再分發含依賴的執行包時，須另行確認其授權要求。

## 日本語

プログラミングに慣れていない場合は、ファイルを読んでコマンドを実行できる AI にリポジトリのリンクと次の指示を渡せます。

> まず README、VERSION、bot.py、video_downloader.py、config_cli.py、requirements.txt、deploy.sh、systemd のサービスファイル、テストを読んで、現在の main ブランチを私の Debian／Ubuntu サーバーに導入する手順を案内してください。この Bot は Python、systemd、Telegram Bot API、gallery-dl／yt-dlp、Pillow で X/Twitter の単一投稿から本文とメディアを取得します。私はコードを書けないので、コマンドの意味と期待する結果を説明してください。接続方法、Bot Token、Telegram ユーザー ID など、足りない情報だけを尋ね、ほかは文書の初期値を使ってください。Bot の作成、ログイン承認、Token／Cookies の安全な入力、SSH 鍵の確認は私が行います。認証情報をチャットや Git に貼らせないでください。既存のサービスとデータを確認し、承認なしに既存環境を上書き・再起動したり、Webhook・ユーザーデータ・認証情報を初期化したりしないでください。今の構成を使い、作り直さないでください。最後にサービス、/start、公開投稿、4言語の画面を確認し、私が Telegram で本文とメディアの受信を確かめられるよう案内してください。未確認の作業は明示してください。

X/Twitter の単一投稿の URL を送ると、投稿者へのリンク、本文、画像、動画を取得できます。インライン共有にも対応しています。所有者や管理者を含む全ユーザーが「Language」で简体中文・繁體中文・English・日本語を選べます。画面の初期言語は繁體中文（`zh`）で、端末の言語に合わせて自動変更されません。コマンドメニューと Bot の紹介文は端末の言語に対応します。

受け付けた投稿のメッセージに 👀 を付け、処理が終わると外します。通常の入力で内容を認識できない場合は 🤔 を付け、案内文も返信します。

### 導入

Debian／Ubuntu、systemd、git、Python 3.10+、root または sudo 権限が必要です。サーバーから Telegram とメディア配信元に接続できる必要があります。導入スクリプトは Python 3.12 と不足している ffmpeg を用意し、別の環境でテストしてから切り替えます。サービスや設定の確認に失敗した場合はプログラムを元に戻します。ロングポーリングを使うため、Webhook や受信ポートの開放は不要です。

先に BotFather で Bot を作成します。次のコマンドは、この Bot をまだ導入していないサーバーで実行してください。最後に Token を入力しますが、画面には表示されません。Token をコマンド引数に含めないでください。

以下の手順は main の最新コードを使います。GitHub の「Latest Release」は直近の正式リリースで、main より古い場合があります。リリース説明とタグは公開時点の内容を示します。

```sh
git clone https://github.com/kk311intl/telegram-x-tweet-bot.git
cd telegram-x-tweet-bot
sudo bash deploy.sh
sudo x-tweet-bot-config set-token
```

Token が未設定の間はサービスが待機します。設定後、Bot に `/id` を送って自分のユーザー ID を確認し、次を実行します。

```sh
sudo x-tweet-bot-config set-owner YOUR_TELEGRAM_USER_ID
sudo x-tweet-bot-config status
systemctl status x-tweet-telegram-bot.service --no-pager
```

### 設定と利用

設定ファイルは `/etc/x-tweet-telegram-bot.env` にあり、root のみ読み取れます。状態データは `/var/lib/x-tweet-telegram-bot` に保存されます。これらのファイルやバックアップを Git に入れないでください。

初期上限、問い合わせ先、タイムゾーン・時刻、メディアサイズ、キュー上限、一時ファイル制限、Inline キャッシュ時間を変更したら `sudo x-tweet-bot-config reload` を実行します。受付を一時停止し、受付済みの処理が終わってから一括適用します。不正な設定は適用せず、待機は最大10分です。時間切れや待機中のサービス再起動では結果を確認できず、未適用とは限りません。サービスを確認してから再試行してください。Bot 内で保存した既定上限が優先され、既存ユーザーの上限は変わりません。タイムゾーン・日付切替の変更で過去の統計は再計算しません。Token、識別情報、並列数は再起動が必要です。許可した設定だけを渡し、Bot に root の環境ファイルは読ませません。

| 設定 | 初期値 | 用途 |
| --- | --- | --- |
| `BOT_TIMEZONE` | UTC | 日次上限・レポート用の IANA タイムゾーン。 |
| `DAILY_RESET_HOUR` | 0 | 利用日の切り替え時刻、現地の 0–23 時。 |
| `DAILY_REPORT_HOUR` | 0 | 所有者に使用量と審査待ち申請のレポートを送る時刻、0–23 時。 |
| `DEFAULT_DAILY_LIMIT` | 50 | 新規承認ユーザーの上限、1–100000。Bot 内の設定が優先し、既存の上限は変えません。 |
| `OWNER_CONTACT_URL` | 空欄 | 使い方ページの「問い合わせ」ボタンのリンク。例：https://t.me/YOUR_USERNAME。空欄なら非表示。 |
| `WORKER_COUNT` | 2 | 投稿処理の並列数、1–8。 |
| `MIN_FREE_DISK_BYTES` | 1073741824 | 空き容量が 1 GiB 未満なら新規ダウンロードを停止。 |
| `TEMP_SOFT_LIMIT_BYTES` | 1073741824 | メディア一時ファイルが 1 GiB に達したら受付停止。ハード上限ではありません。 |
| `TEMP_RETENTION_HOURS` | 24 | 未使用の古いメディア一時ファイルを起動時と1時間ごとに確認・削除。 |

レポートと利用日の切り替えを同じ時刻にすると、終了した1日分の使用量を報告します。それ以外は送信時点の使用量です。両方を 0 にすると、午前0時に前日分を報告します。使用量は上限から差し引いた回数で、送信成功数ではありません。その他の上限と設定範囲は `bot.py`、依存パッケージのバージョンは `requirements.txt` を参照してください。

管理操作は Bot との個別チャットで行います。ユーザー ID を入力すると詳細を表示し、`ID 上限値` を入力すると確認後に変更します。`-1` はブロック、`0` は未承認の初期状態、正の数は1日の上限です。無制限の管理者権限の付与、詳細設定、ユーザー制御は所有者専用です。入力は `/cancel` で中止できます。

「標準上限」は今後承認する一般ユーザーだけに適用されます。「一括変更」では `A B`（例：`50 100`）を入力し、確認後に上限が A の承認済み一般ユーザーを B に変更します。標準上限や本日の使用量は変わりません。A と B は異なる 1–100000 の整数で、初期状態・審査待ち・ブロック・所有者・管理者は対象外です。一覧では CNS／CNT／JA／EN で言語を表示し、詳しいユーザー情報は ID で検索できます。

インライン共有には BotFather で Inline Mode を有効にしてください。Cookies は所有者が取り込み、一般ユーザーに使わせるかどうかも所有者が設定します。必要な場合だけ取り込み、専用アカウントの使用をお勧めします。Cookies はログインの認証情報なので、Git に入れたり他人に転送したりしないでください。

画像は X の原寸版（`orig`）を要求し、縮小版で代用しません。必ずしも4Kではありません。動画は高画質から試し、容量制限を超えると一時ファイルを削除して次の画質に下げます。匿名取得や画像との混在でも同様で、再圧縮はしません。取得した画像・動画を添付し、プレビューは縮小される場合があります。動画には Telegram の 50 MB 上限が適用されます。長文は原文リンクを残して抜粋し、引用先までは取得しません。配信元が利用できない場合や全画質が上限を超える場合は、一部しか取得できないことがあります。

X/Twitter の GIF は元の MP4 をプレビューと添付ファイルに使い、`.gif` には変換しません。複数メディアの投稿はアルバムでプレビューします。

利用状況には本日・7日間・30日間の統計を表示します。設定した日付の切り替え時刻を基準に本日を含め、利用者は期間内で重複して数えません。日別使用量は30日分を保存し、履歴が足りない場合は記録済みの使用量のみを集計します。

「稼働状況」の通信量は Bot とダウンローダーの直近24時間分を、分単位で概算したものです。再起動後も記録を保持し、24時間分に満たない場合はその旨を表示します。取得できない場合にサーバー全体の通信量で代用することはありません。systemd の `IPAccounting=yes` が必要です。

### 保守と検証

更新前に設定と状態データをバックアップし、作業ディレクトリに未保存の変更がないことを確認してから、元のリポジトリで main を更新します。

```sh
git switch main
git pull --ff-only
sudo bash deploy.sh
```

更新は候補版をテストしてプログラムの復元用コピーを作り、受付済みの処理が完了してから再起動します。設定とユーザーデータは保持します。待機が時間切れになれば更新を中止して受付を再開し、切替に失敗すればプログラムを戻します。短い停止を伴う更新であり、実行中のコードの置換ではありません。3.8.x からの初回更新は従来の再起動方式です。保守時は `sudo x-tweet-bot-config drain` で受付を止め、処理の完了を最大10分待ちます。成功後は切替用に120秒間停止を保ち、その後は自動再開します。`sudo x-tweet-bot-config resume` で早めに再開したり、手動で開始した待機中の要求を取り消したりできます。デプロイ中は排他ロックにより、別の `reload`／`resume` が切替に割り込むことを防ぎます。Git で更新できない場合は、ローカル変更をリセットせずに差分を確認してください。同じバージョンでファイルが異なると導入は拒否されます。

新規導入では UTC 00:00 にレポートを送信します。既存の環境ファイルの時刻は更新時に変更されません。22時から変更する場合は `DAILY_REPORT_HOUR=0` に設定して `sudo x-tweet-bot-config reload` を実行してください。

サービスとログは `systemctl status x-tweet-telegram-bot.service` と `sudo journalctl -u x-tweet-telegram-bot.service -n 50 --no-pager` で確認できます。ログを共有する前に個人情報や認証情報を伏せてください。

このリポジトリには、別サーバーへのバックアップ機能は含まれていません。`export-access` は稼働中でも権限と標準上限を出力できますが、名前・言語・使用量・認証情報は含みません。`import-access` はサービスが完全に停止している場合（`inactive` または `failed` で `MainPID=0`）だけ利用でき、新しいデータは上書きしません。

完全な復元には、同じ時点の設定ファイルと状態データのバックアップが必要です。元のファイル所有者と権限も保持してください。稼働中のデータを直接置き換えないでください。容量確保のために認証情報、ユーザーデータ、破損後に残した復旧ファイルを自動削除することはありません。

`requirements.txt` の依存パッケージをインストールすると、`python3 -m unittest -q test_bot.py` を実行できます。導入後はサービスと状態を確認してから、ダウンロードを試します。2行目の URL は本文とメディアを含む実際の公開投稿に置き換えてください。

```sh
sudo /opt/x-tweet-telegram-bot/verify_deploy.sh
sudo env TEST_URL='https://x.com/example/status/123456789' /opt/x-tweet-telegram-bot/verify_deploy.sh
```

ダウンロードの確認には Cookies を使わず、Telegram にメッセージも送りません。一時ファイルは終了後に削除されます。最後に Telegram で本文とメディアの受信を確認してください。

ソースコード © 2026 kk311intl のライセンスは [GNU GPL v3.0 only](LICENSE)（`GPL-3.0-only`）です。requests、requests-toolbelt、Pillow、yt-dlp、gallery-dl はそれぞれ元のライセンスに従います。依存パッケージを含めて再配布する場合は、それらのライセンス要件も確認してください。

## English

If you do not write code, give the repository link and this prompt to an AI that can read files and run commands:

> First read the README, VERSION, bot.py, video_downloader.py, config_cli.py, requirements.txt, deploy.sh, systemd service file and tests, then walk me through installing the current main branch on my Debian/Ubuntu server. This bot uses Python, systemd, the Telegram Bot API, gallery-dl/yt-dlp and Pillow to retrieve text and media from a single X/Twitter post. I do not write code, so explain the commands and expected results. Ask only for missing server access, the bot token, my Telegram user ID or other required information; use the documented defaults otherwise. I will create the bot, approve logins, enter tokens/cookies securely and confirm SSH keys myself. Do not ask me to paste credentials into chat or Git. Check existing services and data first; do not overwrite, restart or reset an existing deployment, webhook, user data or credentials without approval. Use the project as it is, without redesigning it. Check the service, /start, a public post and all four interface languages, and guide me through confirming text and media reception in Telegram. Say clearly when a step could not be verified.

Send a single X/Twitter post URL to receive a link to its author, text, images and videos. Inline sharing is also supported. All users, including the owner and administrators, can choose 简体中文, 繁體中文, English or 日本語 in Language. The interface defaults to Traditional Chinese (`zh`) and does not switch with the client's language. Command menus and the bot's profile follow the client's language.

Accepted posts get a 👀 reaction on the original message, removed when processing ends. Unrecognized ordinary input gets a 🤔 reaction as well as a text prompt.

### Deploy

You need Debian/Ubuntu, systemd, git, Python 3.10+ and root or sudo access. The server must be able to connect to Telegram and the media sources. The deployment script prepares Python 3.12 and any missing ffmpeg installation, tests in a separate environment before switching, and restores the previous code if service or configuration checks fail. The bot uses long polling, so no webhook or inbound port is needed.

Create the bot in BotFather first. Run these commands on a server where this bot is not already installed. The last command prompts for the token without showing it on screen; never put it in command arguments.

The commands below use the latest code on main. GitHub's Latest Release is the most recent formal release and may lag behind main; its notes and tag describe that release's snapshot.

```sh
git clone https://github.com/kk311intl/telegram-x-tweet-bot.git
cd telegram-x-tweet-bot
sudo bash deploy.sh
sudo x-tweet-bot-config set-token
```

The service waits until a token is configured. Then send `/id` to the bot to find your user ID and run:

```sh
sudo x-tweet-bot-config set-owner YOUR_TELEGRAM_USER_ID
sudo x-tweet-bot-config status
systemctl status x-tweet-telegram-bot.service --no-pager
```

### Configure and use

Settings are in `/etc/x-tweet-telegram-bot.env`, readable only by root. State is stored in `/var/lib/x-tweet-telegram-bot`. Do not put these files or their backups in Git.

After changing the initial quota, contact URL, timezone/schedule, media sizes, queue limit, temporary-file limits or Inline cache lifetime, run `sudo x-tweet-bot-config reload`. Intake pauses until accepted jobs finish, then settings apply together. Invalid settings are rejected; waiting is limited to 10 minutes. A timeout or service restart while waiting leaves the outcome unconfirmed, not necessarily unchanged; check the service before retrying. The saved in-bot quota default still takes priority, and existing quotas stay unchanged. Timezone/day-boundary changes do not recalculate historical statistics. Tokens, identity and worker counts still require a restart. Only allowlisted settings are passed to the bot; it cannot read root's environment file.

| Setting | Default | Purpose |
| --- | --- | --- |
| `BOT_TIMEZONE` | UTC | IANA time zone for daily limits and reports. |
| `DAILY_RESET_HOUR` | 0 | Local hour when the usage day rolls over, 0–23. |
| `DAILY_REPORT_HOUR` | 0 | Local hour for sending the owner a report of usage and pending requests, 0–23. |
| `DEFAULT_DAILY_LIMIT` | 50 | Quota for newly approved regular users, 1–100000; the in-bot default takes priority and existing quotas stay unchanged. |
| `OWNER_CONTACT_URL` | empty | Help-page Feedback button URL, e.g. https://t.me/YOUR_USERNAME; omitted when empty. |
| `WORKER_COUNT` | 2 | Concurrent post jobs, 1–8. |
| `MIN_FREE_DISK_BYTES` | 1073741824 | Stop accepting downloads below 1 GiB free. |
| `TEMP_SOFT_LIMIT_BYTES` | 1073741824 | Stop accepting downloads at 1 GiB of media temporary files; not a hard quota. |
| `TEMP_RETENTION_HOURS` | 24 | Check and remove expired, unused media temporary files at startup and hourly. |

When the report and reset hours match, the report covers the completed usage day. Otherwise it shows usage at send time. Set both to 0 to report the full previous day at midnight. Usage counts quota deductions, not successful deliveries. See `bot.py` for other limits and configuration ranges, and `requirements.txt` for dependency versions.

Manage users in a private chat with the bot. Enter a user ID to view details, or `ID quota` to change access after confirmation. `-1` blocks the user, `0` means initialized but unapproved, and positive numbers set the daily quota. Granting unlimited administrator access, Advanced settings and User controls are owner-only. Enter `/cancel` to cancel input.

Default limit applies only to regular users approved afterwards. In Bulk change, enter `A B` (e.g. `50 100`) and confirm to change approved regular users whose quota is A to B. The default and today's usage stay unchanged. A and B must be different integers from 1 to 100000; initialized, pending, blocked, owner and administrator accounts are excluded. The list shows languages as CNS/CNT/JA/EN; search by ID for full user details.

Enable Inline Mode in BotFather for inline sharing. The owner can import cookies and control whether regular users may use them. Import them only when needed, preferably from a dedicated account. Cookies are login credentials: do not commit them or share them with others.

Photos request X's original-size variant (`orig`), not a thumbnail substitute; this does not always mean 4K. Videos start at high quality. If a download exceeds the limit, its temporary files are removed before trying the next quality, including anonymous sources and mixed-media posts. Videos are not re-encoded. Downloaded files accompany previews, which may be resized. Videos remain subject to Telegram's 50 MB limit. Long text is excerpted with a source link, and quoted posts are not retrieved. Unavailable sources or variants that all exceed the limit may prevent complete retrieval.

X/Twitter GIFs use the source MP4 for both previews and file attachments, without conversion to `.gif`. Multi-media posts keep album previews.

Usage overview shows today, 7 days and 30 days, including today and following the configured daily reset time. Active users are counted once per period. Daily totals are kept for 30 days; incomplete history includes only recorded usage.

Runtime status shows traffic for the bot and its downloaders over the last 24 hours, estimated by minute and retained across restarts. It indicates when less than 24 hours of data are available. If counters are unavailable, it does not substitute server-wide traffic. This requires systemd's `IPAccounting=yes`.

### Maintain and verify

Back up settings and state and make sure there are no unsaved local changes, then update main in the existing repository directory:

```sh
git switch main
git pull --ff-only
sudo bash deploy.sh
```

Deployment tests the candidate and saves a copy of the installed program before waiting for accepted jobs to finish, then restarts while preserving settings and user data. A drain timeout cancels the update and resumes intake; a failed switch rolls back the program. This is a short-stop update, not in-process code replacement. The first upgrade from 3.8.x still uses the old restart flow. For maintenance, `sudo x-tweet-bot-config drain` pauses intake and waits up to 10 minutes for jobs to finish. After success, intake stays paused for a 120-second handoff window, then resumes automatically. `sudo x-tweet-bot-config resume` resumes it early or cancels a pending manual request. During deployment, an exclusive lock prevents another `reload` or `resume` from interrupting the switch. If Git cannot fast-forward, resolve the differences without resetting local changes. The installer rejects changed files with an unchanged version number; check the source rather than forcing an overwrite.

New installations report at 00:00 UTC. Upgrades do not overwrite schedules in existing environment files. To change an existing 22:00 schedule, set `DAILY_REPORT_HOUR=0` and run `sudo x-tweet-bot-config reload`.

Check the service and logs with `systemctl status x-tweet-telegram-bot.service` and `sudo journalctl -u x-tweet-telegram-bot.service -n 50 --no-pager`. Redact private information before sharing logs.

This repository does not include cross-server backups. `export-access` can export permissions and the default quota without stopping the bot, but does not include names, languages, usage or credentials. `import-access` requires the service to be fully stopped (`inactive` or `failed`, with `MainPID=0`) and will not overwrite newer data.

For full recovery, back up environment settings and state from the same point in time, preserving file ownership and permissions. Never overwrite data while the bot is running. Credentials, user data and recovery files kept after corruption are not automatically deleted to reclaim disk space.

Install the dependencies in `requirements.txt`, then run `python3 -m unittest -q test_bot.py`. After deployment, check the service and state, then test downloading. Replace the URL on the second line with a real public post containing text and media:

```sh
sudo /opt/x-tweet-telegram-bot/verify_deploy.sh
sudo env TEST_URL='https://x.com/example/status/123456789' /opt/x-tweet-telegram-bot/verify_deploy.sh
```

The download check uses no cookies, sends no Telegram messages and removes its temporary files afterwards. Finally, confirm text and media reception in Telegram.

Source © 2026 kk311intl is licensed under [GNU GPL v3.0 only](LICENSE) (`GPL-3.0-only`). requests, requests-toolbelt, Pillow, yt-dlp and gallery-dl retain their upstream licenses. Check their license requirements when redistributing a bundled build.
