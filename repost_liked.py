"""
repost_liked.py
tweets.jsから1000いいね以上のツイートを抽出して定期的に再投稿するスクリプト
画像付きツイートは画像も一緒に投稿。画像が見つからない場合は別のツイートを試す。

使い方:
  python repost_liked.py

.envに以下を設定:
  TWEETS_JS_PATH=tweets.jsのパス
  TWEETS_MEDIA_DIR=tweets_mediaフォルダのパス
"""

import os
import re
import json
import time
import random
import schedule
import logging
from x_poster import post_tweet, post_tweet_with_images

# ── 設定 ──────────────────────────────
TWEETS_JS_PATH   = os.getenv("TWEETS_JS_PATH",   "tweets.js")
TWEETS_MEDIA_DIR = os.getenv("TWEETS_MEDIA_DIR", "tweets_media")
MIN_LIKES        = 1000
INTERVAL_HOURS   = 6
RANDOM_MINUTES   = 30
POSTED_LOG_PATH  = "reposted.json"
MAX_RETRY        = 5   # 画像が見つからない場合の最大リトライ回数
# ──────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(os.path.expanduser("~"), "Documents", "repost_log.txt"),
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def load_tweets(path: str) -> list[dict]:
    """tweets.jsを読み込む"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r"^window\.[^=]+=\s*", "", content.strip())
    data = json.loads(content)
    return [item.get("tweet", item) for item in data]


def filter_liked(tweets: list[dict], min_likes: int) -> list[dict]:
    """いいね数でフィルタリング"""
    result = []
    for t in tweets:
        try:
            if int(t.get("favorite_count", 0)) >= min_likes:
                result.append(t)
        except (ValueError, TypeError):
            continue
    logger.info(f"全{len(tweets)}件中、{min_likes}いいね以上: {len(result)}件")
    return result


def load_posted() -> set[str]:
    if not os.path.exists(POSTED_LOG_PATH):
        return set()
    with open(POSTED_LOG_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_posted(posted: set[str]):
    with open(POSTED_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(list(posted), f, ensure_ascii=False)


def clean_text(text: str) -> str:
    """本文整形：t.co URLを除去"""
    text = re.sub(r"https://t\.co/\S+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_image_paths(tweet: dict) -> list[str]:
    """
    ツイートに添付された画像のローカルパスを返す。
    tweets_mediaフォルダ内でツイートIDをプレフィックスに持つファイルを検索。
    """
    tweet_id = tweet.get("id_str", tweet.get("id", ""))
    if not tweet_id or not os.path.isdir(TWEETS_MEDIA_DIR):
        return []

    # メディア情報をentitiesから取得
    entities = tweet.get("extended_entities", tweet.get("entities", {}))
    media_list = entities.get("media", [])

    if not media_list:
        return []

    found = []
    for media in media_list:
        media_type = media.get("type", "")
        if media_type != "photo":
            continue  # 動画・GIFはスキップ

        # tweets_mediaフォルダ内でtweet_idを含むファイルを探す
        media_url = media.get("media_url_https", media.get("media_url", ""))
        original_filename = os.path.basename(media_url.split("?")[0]) if media_url else ""

        # パターン1: tweet_id-original_filename
        candidate1 = os.path.join(TWEETS_MEDIA_DIR, f"{tweet_id}-{original_filename}")
        # パターン2: tweet_idをプレフィックスに持つファイルを検索
        if os.path.exists(candidate1):
            found.append(candidate1)
        else:
            # フォルダ内をスキャンして tweet_id が含まれるファイルを探す
            try:
                for fname in os.listdir(TWEETS_MEDIA_DIR):
                    if fname.startswith(tweet_id) and fname.lower().endswith(
                        (".jpg", ".jpeg", ".png", ".gif", ".webp")
                    ):
                        found.append(os.path.join(TWEETS_MEDIA_DIR, fname))
            except OSError:
                pass

    # 重複除去して最大4枚
    seen = set()
    unique = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:4]


def has_media(tweet: dict) -> bool:
    """ツイートにメディア（画像）が含まれているか"""
    entities = tweet.get("extended_entities", tweet.get("entities", {}))
    media_list = entities.get("media", [])
    return any(m.get("type") == "photo" for m in media_list)


def pick_tweet(candidates: list[dict], posted: set[str]) -> dict | None:
    """未投稿のツイートをランダムに1件選ぶ"""
    unposted = [t for t in candidates if t.get("id_str", t.get("id", "")) not in posted]
    if not unposted:
        logger.info("未投稿ツイートがなくなりました。投稿済みリストをリセットします。")
        posted.clear()
        save_posted(posted)
        unposted = candidates
    return random.choice(unposted) if unposted else None


def job():
    """定期実行ジョブ"""
    logger.info("=== 再投稿ジョブ開始 ===")
    try:
        tweets     = load_tweets(TWEETS_JS_PATH)
        candidates = filter_liked(tweets, MIN_LIKES)
        posted     = load_posted()

        # 画像が見つかるまで最大MAX_RETRY回試みる
        for attempt in range(1, MAX_RETRY + 1):
            tweet = pick_tweet(candidates, posted)
            if tweet is None:
                logger.warning("投稿できるツイートが見つかりませんでした")
                break

            tweet_id   = tweet.get("id_str", tweet.get("id", ""))
            raw_text   = tweet.get("full_text", tweet.get("text", ""))
            tweet_text = f"【L³ポスト復刻】{clean_text(raw_text)}"
            likes      = tweet.get("favorite_count", "?")

            # 画像を探す
            if has_media(tweet):
                image_paths = get_image_paths(tweet)
                if not image_paths:
                    logger.warning(
                        f"試行{attempt}/{MAX_RETRY}: 画像ファイルが見つかりません "
                        f"(id:{tweet_id}) → 別のツイートを試します"
                    )
                    # このツイートは今回スキップ（postedには記録しない）
                    # candidates から除外して次のループへ
                    candidates = [t for t in candidates if t.get("id_str", t.get("id", "")) != tweet_id]
                    continue
            else:
                # テキストのみのツイートも画像なしとして扱い別を探す
                logger.info(
                    f"試行{attempt}/{MAX_RETRY}: 画像なしツイート (id:{tweet_id}) → 別のツイートを試します"
                )
                candidates = [t for t in candidates if t.get("id_str", t.get("id", "")) != tweet_id]
                continue

            # 画像が見つかった → 投稿
            logger.info(f"選択ツイート (id:{tweet_id}, ❤️{likes}, 画像{len(image_paths)}枚):\n{tweet_text}")

            wait_sec = random.uniform(60, 300)
            logger.info(f"投稿前に {wait_sec:.0f}秒 待機...")
            time.sleep(wait_sec)

            post_tweet_with_images(tweet_text, image_paths)
            logger.info("✅ 再投稿成功！")

            posted.add(tweet_id)
            save_posted(posted)
            break

        else:
            logger.error(f"{MAX_RETRY}回試みましたが投稿できませんでした")

    except Exception as e:
        logger.error(f"❌ エラー: {e}")

    _reschedule()


def _reschedule():
    schedule.clear("repost_job")
    offset   = random.randint(-RANDOM_MINUTES, RANDOM_MINUTES)
    next_min = max(INTERVAL_HOURS * 60 + offset, 30)
    schedule.every(next_min).minutes.do(job).tag("repost_job")
    logger.info(f"次回再投稿: {next_min}分後")


def main():
    logger.info("🚀 再投稿スケジューラー起動")
    logger.info(f"対象ファイル: {TWEETS_JS_PATH}")
    logger.info(f"メディアフォルダ: {TWEETS_MEDIA_DIR}")
    logger.info(f"最低いいね数: {MIN_LIKES}")

    try:
        tweets     = load_tweets(TWEETS_JS_PATH)
        candidates = filter_liked(tweets, MIN_LIKES)
        if not candidates:
            logger.error(f"{MIN_LIKES}いいね以上のツイートが見つかりません。MIN_LIKESを下げてください。")
            return
    except FileNotFoundError:
        logger.error(f"tweets.jsが見つかりません: {TWEETS_JS_PATH}")
        return

    # 起動時テスト投稿
    logger.info("🔁 起動時テスト投稿...")
    job()

    logger.info("待機中... (Ctrl+C で終了)")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
