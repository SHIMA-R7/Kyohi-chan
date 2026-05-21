# 🤖 自動ツイートシステム（共匪ちゃん）

Gemini APIでツイート文を自動生成し、Seleniumで X に定期投稿するシステムです。

---

## 📁 ファイル構成

```
kyohi-chan/
├── auto_tweet.py        # メインスケジューラー（通常Bot）
├── tweet_generator.py   # Gemini APIでツイート生成
├── x_poster.py          # SeleniumでXに投稿
├── repost_liked.py      # 過去ツイート復刻スケジューラー
├── .env                 # 設定ファイル（要作成）
├── reposted.json        # 投稿済みID記録（自動生成）
└── README.md            # このファイル
```

---

## 🚀 セットアップ

### 1. 必要なライブラリをインストール

```powershell
& C:/Users/yugo/AppData/Local/Microsoft/WindowsApps/python3.13.exe -m pip install `
  google-genai selenium schedule python-dotenv pytrends webdriver-manager
```

### 2. .env を作成

以下の内容で `.env` ファイルを作成してください。

```env
# Gemini APIキー（https://aistudio.google.com/ で取得）
GEMINI_API_KEY=your_gemini_api_key_here

# Xアカウント情報（プロファイルを使う場合は不要）
X_USERNAME=your_email@example.com
X_PASSWORD=your_password

# FirefoxプロファイルのパスをFirefoxで about:profiles を開いて確認
# 「ルートディレクトリ」のパスを入力する
FIREFOX_PROFILE_PATH=C:\Users\yugo\AppData\Roaming\Mozilla\Firefox\Profiles\xxxxxxxx.default-release

# ボット設定
BOT_NAME=共匪ちゃん
TWEET_THEME=新左翼や全共闘時代の極左活動家の美少女

# 復刻ツイート用（repost_liked.py で使用）
TWEETS_JS_PATH=C:\Users\yugo\Downloads\twitter-archive\data\tweets.js
TWEETS_MEDIA_DIR=C:\Users\yugo\Downloads\twitter-archive\data\tweets_media
```

### 3. Firefoxでログイン

Firefoxを開いてXにログインしておく。以後はプロファイルを使って自動ログインします。

---

## ▶️ 実行方法

### 通常Bot（auto_tweet.py）

```powershell
& C:/Users/yugo/AppData/Local/Microsoft/WindowsApps/python3.13.exe c:/Users/yugo/kyohi-chan/auto_tweet.py
```

### 復刻Bot（repost_liked.py）

```powershell
& C:/Users/yugo/AppData/Local/Microsoft/WindowsApps/python3.13.exe c:/Users/yugo/kyohi-chan/repost_liked.py
```

2つは別々のターミナルで同時に起動できます。

---

## 🎯 機能一覧

### auto_tweet.py（通常Bot）

| 機能 | 内容 |
|---|---|
| 一般ツイート | 「ちゃっす！〇〇です。」風の挨拶から始まるキャラクターらしい日常つぶやき |
| ニュースツイート | 「【〇〇通信】」形式でGoogle Newsのニュースに反応、リンク付き |
| ネタツイート | 流行り構文（「諸君、私は〜が好きだ」など）を使ったシュールなボケ |
| デイリーえるえる | 毎朝7時ごろ「えるえるえる…」×1〜12個を #デイリーえるえる タグ付きで投稿 |
| 自己紹介 | 起動時に1回だけ自己紹介ツイートを投稿 |

**投稿ロジック：**
- 3時間ごと（±30分ランダム）に一般・ニュースを交互投稿
- 約20%の確率で通常投稿の代わりにネタツイートが発火
- 全ツイートの末尾に `#BOT_NAME` を自動付与

**ツイート形式：**
```
一般:   ちゃっす！共匪ちゃんです。今日もバリケードで… #資本主義打倒 #共匪ちゃん
ニュース: 【共匪ちゃん通信】〇〇というニュースについて… #共匪ちゃん
        https://news.google.com/...
ネタ:   【悲報】資本主義、存在する #共匪ちゃん
える:   えるえるえるえる
        #デイリーえるえる #共匪ちゃん
```

### repost_liked.py（復刻Bot）

| 機能 | 内容 |
|---|---|
| ツイート抽出 | tweets.jsから1000いいね以上のツイートを自動抽出 |
| 画像付き投稿 | tweets_mediaフォルダから画像を探して一緒に投稿（最大4枚） |
| 画像なし時の動作 | 画像が見つからないツイートはスキップして別のツイートを試す（最大5回） |
| 重複防止 | reposted.jsonに投稿済みIDを記録。全部回したら自動リセット |
| 復刻テキスト | 本文の先頭に「【復刻】」を自動付与 |
| URL除去 | t.co の短縮URLは自動で除去してから投稿 |

**投稿ロジック：**
- 6時間ごと（±30分ランダム）に1件投稿
- 投稿前に1〜5分ランダム待機（Bot検出対策）

---

## 🛡️ Bot検出対策

| 対策 | 内容 |
|---|---|
| タイピング | 1文字ずつ0.05〜0.18秒間隔でランダム入力 |
| ページ閲覧 | ツイート前にホームをランダムスクロール |
| 投稿前待機 | 1〜5分ランダム待機してから投稿 |
| 投稿間隔 | ±30分のランダムオフセット |
| scrollIntoView | 要素をビューポート内に収めてからクリック |

---

## ⚙️ カスタマイズ

### 投稿間隔を変えたい

`auto_tweet.py` の先頭付近：
```python
INTERVAL_HOURS = 3    # 投稿間隔（時間）
RANDOM_MINUTES = 30   # ±この分でランダムにずらす
ERU_HOUR       = 7    # えるえる投稿時刻
```

### いいね数の閾値を変えたい

`repost_liked.py` の先頭付近：
```python
MIN_LIKES = 1000   # ここを変更
```

### モデルを変えたい

`tweet_generator.py` の `_call_gemini` 関数：
```python
model="gemini-2.5-flash",  # ここを変更
```

現在利用可能な主なモデル（2026年5月時点）：
- `gemini-2.5-flash` … 高品質・標準
- `gemini-2.5-flash-lite` … 低コスト版

---

## 💰 コスト目安

`gemini-2.5-flash` 使用時、1日9回投稿で **約1〜2円/日**。

出力トークンが主なコスト要因。`tweet_generator.py` の `max_output_tokens` で上限を制御しています。

---

## 📝 ログ

| ファイル | 場所 |
|---|---|
| 通常Botログ | `C:\Users\ユーザー名\Documents\tweet_log.txt` |
| 復刻Botログ | `C:\Users\ユーザー名\Documents\repost_log.txt` |
| 投稿済みID | `reposted.json`（スクリプトと同じフォルダ） |

---

## ⚠️ 注意事項

- Xの利用規約に反する可能性があります。スパム・大量投稿は避けてください。
- Xのサイト構造が変更されるとSeleniumのセレクターが壊れることがあります。その際は `x_poster.py` の `data-testid` 属性値を更新してください。
- `.env` ファイルをGitにコミットしないよう `.gitignore` に追加してください。
- Firefoxのプロファイルを使う場合、スクリプト実行中はそのプロファイルで別のFirefoxを起動しないでください。
