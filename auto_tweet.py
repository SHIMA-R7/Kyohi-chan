"""
メインスケジューラー（通常Bot + 復刻Bot統合版）
"""

import os
import re
import json
import time
import random
import schedule
import logging
from tweet_generator import (
    generate_memorial_tweet,
    get_memorial_day,
    generate_intro_tweet,
    generate_general_tweet,
    generate_news_tweet,
    generate_eru_tweet,
    generate_neta_tweet,
)
from x_poster import post_tweet, post_tweet_with_link, post_tweet_with_images, get_random_tweet_id, post_reply

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(os.path.expanduser("~"), "Documents", "tweet_log.txt"),
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =====================
# ★ 設定 ★
# =====================
INTERVAL_HOURS   = 3    # 通常投稿間隔（時間）
RANDOM_MINUTES   = 30   # ±この分でランダムにずらす
ERU_HOUR         = 7    # えるえる投稿時刻
ERU_MINUTE_RANGE = 15   # えるえる ±この分でランダム

REPOST_INTERVAL_HOURS = 6     # 復刻投稿間隔（時間）
REPOST_RANDOM_MINUTES = 30    # ±この分でランダムにずらす
TWEETS_JS_PATH        = os.getenv("TWEETS_JS_PATH",   "tweets.js")
TWEETS_MEDIA_DIR      = os.getenv("TWEETS_MEDIA_DIR", "tweets_media")
MIN_LIKES             = 1000
MAX_RETRY             = 5
POSTED_LOG_PATH       = "reposted.json"
ERU_COUNT_LOG_PATH    = "eru_count.json"  # えるえる連続日数記録
# =====================

_use_news_next = False
_tweet_count = 0  # 季節ツイートのタイミング管理

# ── 投稿キュー（同時投稿を防ぐロック） ──
_is_posting = False


# ───────────────────────────────────────
# えるえるカウンター
# ───────────────────────────────────────

def _load_eru_count() -> dict:
    """えるえる連続日数を読み込む"""
    if not os.path.exists(ERU_COUNT_LOG_PATH):
        return {"last_date": "", "count": 0}
    with open(ERU_COUNT_LOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_eru_count(data: dict):
    with open(ERU_COUNT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def _get_eru_day_count() -> int:
    """今日のえるえる日数を返す（連続していなければリセット）"""
    from datetime import date, timedelta
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    data = _load_eru_count()

    if data["last_date"] == today:
        # 今日すでに投稿済み → 同じカウントを返す
        return data["count"]
    elif data["last_date"] == yesterday:
        # 昨日投稿済み → 連続カウントアップ
        new_count = data["count"] + 1
    else:
        # 連続が途切れた → リセット
        new_count = 1

    _save_eru_count({"last_date": today, "count": new_count})
    return new_count


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")

def _is_paused() -> bool:
    """dashboard.pyからの一時停止指示を確認する"""
    import json
    try:
        if not os.path.exists(STATE_FILE):
            return False
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("paused", False)
    except Exception:
        return False

def _wait_if_paused():
    """一時停止中は再開されるまでブロック"""
    if _is_paused():
        logger.info("⏸ 一時停止中... 再開を待っています")
        while _is_paused():
            time.sleep(10)
        logger.info("▶ 再開しました")


def _pre_wait():
    wait = random.uniform(60, 300)
    logger.info(f"投稿前に {wait:.0f}秒 待機...")
    time.sleep(wait)


def _safe_post(fn, *args, **kwargs):
    """投稿中フラグを立てて排他制御する"""
    global _is_posting
    if _is_posting:
        logger.info("別の投稿が進行中のため待機...")
        while _is_posting:
            time.sleep(10)
    _is_posting = True
    try:
        fn(*args, **kwargs)
    finally:
        _is_posting = False


# ───────────────────────────────────────
# 通常Bot
# ───────────────────────────────────────

def job():
    global _use_news_next, _tweet_count
    _wait_if_paused()
    _tweet_count += 1

    # 記念日チェック
    memorial_name, memorial_desc = get_memorial_day()

    # 優先度: 記念日 > ニュース/季節/一般
    if memorial_name:
        mode = f"記念日（{memorial_name}）"
    elif _use_news_next:
        mode = "ニュース"
    elif _tweet_count % 30 == 0:
        mode = "季節"
    else:
        mode = "一般"

    logger.info(f"=== ツイート生成・投稿ジョブ開始 [{mode}] ===")
    try:
        _pre_wait()
        if memorial_name:
            tweet_text = generate_memorial_tweet(memorial_name, memorial_desc)
            logger.info(f"生成ツイート:\n{tweet_text}")
            _safe_post(post_tweet, tweet_text)
        elif _use_news_next:
            tweet_text, news_url = generate_news_tweet()
            logger.info(f"生成ツイート:\n{tweet_text}\nURL: {news_url}")
            _safe_post(post_tweet_with_link, tweet_text, news_url)
        elif mode == "季節":
            tweet_text = generate_general_tweet(force_seasonal=True)
            logger.info(f"生成ツイート:\n{tweet_text}")
            _safe_post(post_tweet, tweet_text)
        else:
            tweet_text = generate_general_tweet()
            logger.info(f"生成ツイート:\n{tweet_text}")
            _post_or_reply(tweet_text)
        logger.info("✅ 投稿成功！")
        _use_news_next = not _use_news_next
    except Exception as e:
        logger.error(f"❌ エラー: {e}")
        _use_news_next = not _use_news_next
    _reschedule()


def eru_job():
    _wait_if_paused()
    logger.info("=== デイリーえるえる投稿 ===")
    try:
        offset = random.randint(0, ERU_MINUTE_RANGE * 60)
        logger.info(f"えるえる投稿まで {offset}秒 待機...")
        time.sleep(offset)
        day_count = _get_eru_day_count()
        tweet_text = generate_eru_tweet(day_count=day_count)
        logger.info(f"えるえるツイート({day_count}日目):\n{tweet_text}")
        _safe_post(post_tweet, tweet_text)
        logger.info("✅ えるえる投稿成功！")
    except Exception as e:
        logger.error(f"❌ えるえるエラー: {e}")


def neta_job():
    _wait_if_paused()
    logger.info("=== ネタツイート投稿 ===")
    try:
        _pre_wait()
        tweet_text = generate_neta_tweet()
        logger.info(f"ネタツイート:\n{tweet_text}")
        _safe_post(post_tweet, tweet_text)
        logger.info("✅ ネタ投稿成功！")
    except Exception as e:
        logger.error(f"❌ ネタエラー: {e}")
    _reschedule()


def _post_or_reply(tweet_text: str, reply_chance: float = 0.15):
    """15%の確率でタイムラインのランダムツイートにリプライ、それ以外は通常投稿"""
    if random.random() < reply_chance:
        logger.info("🎲 リプライモード発動！タイムラインからツイートを取得中...")
        tweet_id = get_random_tweet_id()
        if tweet_id:
            _safe_post(post_reply, tweet_text, tweet_id)
            return
        logger.warning("ツイートID取得失敗 → 通常投稿にフォールバック")
    _safe_post(post_tweet, tweet_text)


def _reschedule():
    schedule.clear("tweet_job")
    offset   = random.randint(-RANDOM_MINUTES, RANDOM_MINUTES)
    next_min = max(INTERVAL_HOURS * 60 + offset, 30)
    if random.random() < 0.2:
        schedule.every(next_min).minutes.do(neta_job).tag("tweet_job")
        logger.info(f"次回: ネタツイート（{next_min}分後）")
    else:
        schedule.every(next_min).minutes.do(job).tag("tweet_job")
        logger.info(f"次回: 通常投稿（{next_min}分後）")


# ───────────────────────────────────────
# 復刻Bot
# ───────────────────────────────────────

def _load_tweets() -> list[dict]:
    with open(TWEETS_JS_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    content = re.sub(r"^window\.[^=]+=\s*", "", content.strip())
    data = json.loads(content)
    return [item.get("tweet", item) for item in data]


def _filter_liked(tweets: list[dict]) -> list[dict]:
    result = []
    for t in tweets:
        try:
            if int(t.get("favorite_count", 0)) >= MIN_LIKES:
                result.append(t)
        except (ValueError, TypeError):
            continue
    logger.info(f"復刻対象: {len(result)}件（{MIN_LIKES}いいね以上）")
    return result


def _load_posted() -> set[str]:
    if not os.path.exists(POSTED_LOG_PATH):
        return set()
    with open(POSTED_LOG_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def _save_posted(posted: set[str]):
    with open(POSTED_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(list(posted), f, ensure_ascii=False)


def _clean_text(text: str) -> str:
    text = re.sub(r"https://t\.co/\S+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _get_image_paths(tweet: dict) -> list[str]:
    tweet_id = tweet.get("id_str", tweet.get("id", ""))
    if not tweet_id or not os.path.isdir(TWEETS_MEDIA_DIR):
        return []
    entities   = tweet.get("extended_entities", tweet.get("entities", {}))
    media_list = entities.get("media", [])
    found = []
    for media in media_list:
        if media.get("type") != "photo":
            continue
        media_url = media.get("media_url_https", media.get("media_url", ""))
        original_filename = os.path.basename(media_url.split("?")[0]) if media_url else ""
        candidate = os.path.join(TWEETS_MEDIA_DIR, f"{tweet_id}-{original_filename}")
        if os.path.exists(candidate):
            found.append(candidate)
        else:
            try:
                for fname in os.listdir(TWEETS_MEDIA_DIR):
                    if fname.startswith(tweet_id) and fname.lower().endswith(
                        (".jpg", ".jpeg", ".png", ".gif", ".webp")
                    ):
                        found.append(os.path.join(TWEETS_MEDIA_DIR, fname))
            except OSError:
                pass
    seen, unique = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:4]


def _has_media(tweet: dict) -> bool:
    entities   = tweet.get("extended_entities", tweet.get("entities", {}))
    media_list = entities.get("media", [])
    return any(m.get("type") == "photo" for m in media_list)


def _pick_tweet(candidates: list[dict], posted: set[str]) -> dict | None:
    unposted = [t for t in candidates if t.get("id_str", t.get("id", "")) not in posted]
    if not unposted:
        logger.info("未投稿ツイートがなくなりました。投稿済みリストをリセットします。")
        posted.clear()
        _save_posted(posted)
        unposted = candidates
    return random.choice(unposted) if unposted else None


def repost_job():
    _wait_if_paused()
    logger.info("=== 復刻投稿ジョブ開始 ===")
    try:
        tweets     = _load_tweets()
        candidates = _filter_liked(tweets)
        posted     = _load_posted()

        for attempt in range(1, MAX_RETRY + 1):
            tweet = _pick_tweet(candidates, posted)
            if tweet is None:
                logger.warning("投稿できるツイートが見つかりませんでした")
                break

            tweet_id   = tweet.get("id_str", tweet.get("id", ""))
            raw_text   = tweet.get("full_text", tweet.get("text", ""))
            original_url = f"https://x.com/i/web/status/{tweet_id}"
            tweet_text = f"【L³ポスト復刻】{_clean_text(raw_text)}\n{original_url}"
            likes      = tweet.get("favorite_count", "?")

            if _has_media(tweet):
                image_paths = _get_image_paths(tweet)
                if not image_paths:
                    logger.warning(f"試行{attempt}/{MAX_RETRY}: 画像が見つかりません(id:{tweet_id}) → スキップ")
                    candidates = [t for t in candidates if t.get("id_str", t.get("id", "")) != tweet_id]
                    continue
                # 画像あり → 画像付き投稿
                logger.info(f"復刻ツイート (id:{tweet_id}, ❤️{likes}, 画像{len(image_paths)}枚):\n{tweet_text}")
                _pre_wait()
                _safe_post(post_tweet_with_images, tweet_text, image_paths)
            else:
                # 画像なし → テキストのみ投稿
                logger.info(f"復刻ツイート (id:{tweet_id}, ❤️{likes}, テキストのみ):\n{tweet_text}")
                _pre_wait()
                _safe_post(post_tweet, tweet_text)

            logger.info("✅ 復刻投稿成功！")
            posted.add(tweet_id)
            _save_posted(posted)
            break

        else:
            logger.error(f"{MAX_RETRY}回試みましたが投稿できませんでした")

    except FileNotFoundError:
        logger.warning(f"tweets.jsが見つかりません（{TWEETS_JS_PATH}）。復刻投稿をスキップします。")
    except Exception as e:
        logger.error(f"❌ 復刻エラー: {e}")

    _reschedule_repost()


def _reschedule_repost():
    schedule.clear("repost_job")
    offset   = random.randint(-REPOST_RANDOM_MINUTES, REPOST_RANDOM_MINUTES)
    next_min = max(REPOST_INTERVAL_HOURS * 60 + offset, 30)
    schedule.every(next_min).minutes.do(repost_job).tag("repost_job")
    logger.info(f"次回復刻投稿: {next_min}分後")


# ───────────────────────────────────────
# メイン
# ───────────────────────────────────────

def main():
    logger.info("🚀 自動ツイートスケジューラー起動（統合版）")

    # えるえる毎朝登録
    schedule.every().day.at(f"{ERU_HOUR:02d}:00").do(eru_job)
    logger.info(f"えるえる登録: 毎朝{ERU_HOUR:02d}:00（±{ERU_MINUTE_RANGE}分）")

    # 自己紹介ツイート
    logger.info("=== 自己紹介ツイート ===")
    try:
        intro = generate_intro_tweet()
        logger.info(f"自己紹介:\n{intro}")
        _safe_post(post_tweet, intro)
        logger.info("✅ 自己紹介成功！")
    except Exception as e:
        logger.error(f"❌ 自己紹介エラー: {e}")

    # 自己紹介後2〜5分待ってからテスト投稿
    wait_sec = random.uniform(120, 300)
    logger.info(f"🔁 テスト投稿まで {wait_sec:.0f}秒 待機...")
    time.sleep(wait_sec)
    job()

    # 復刻Botも起動時に1回実行してスケジュール登録
    repost_job()

    logger.info("待機中... (Ctrl+C で終了)")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
