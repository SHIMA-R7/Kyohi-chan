"""
tweet_generator.py
Gemini APIを使ってツイート文を生成するモジュール
"""

import os
import random
import urllib.request
import xml.etree.ElementTree as ET
import logging
from datetime import datetime
from google import genai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY が .env に設定されていません")

client = genai.Client(api_key=GEMINI_API_KEY)

BOT_NAME  = os.getenv("BOT_NAME", "共匪ちゃん")
BOT_THEME = os.getenv("TWEET_THEME", "新左翼や全共闘時代の極左活動家の美少女")
GREETING  = f"あなたの名前は{BOT_NAME}です。ツイートの最初に「ちゃっす！{BOT_NAME}です。」や「こんにちは、{BOT_NAME}よ！」のようにキャラクターらしく挨拶してください。"

NEWS_RSS_URLS = [
    "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=%E7%A4%BE%E4%BC%9A&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=%E6%94%BF%E6%B2%BB&hl=ja&gl=JP&ceid=JP:ja",
]

NETA_TEMPLATES = [
    "これ見て笑ったんだけど",
    "急に{topic}の話していい？",
    "{topic}、好きすぎて草",
    "わかる人にだけわかる{topic}の話",
    "{topic}←これ何？（哲学）",
    "今日の{topic}、やばすぎた（語彙力）",
    "{topic}って結局なんなんだろうな…（深夜テンション）",
    "嘘つき！{topic}じゃないじゃん！！！",
    "{topic}の話してもいいですか（してもいいですか）",
    "諸君、私は{topic}が好きだ",
    "【悲報】{topic}、存在する",
    "【朗報】{topic}、存在する",
    "{topic}、完全に理解した",
    "{topic}←人類に早すぎた概念",
    "お前ら{topic}って知ってる？（知ってる）",
]

MEMORIAL_DAYS = [
    (1,  1,  "元旦",             "新年"),
    (1,  15, "成人の日",         "新成人を祝う日"),
    (2,  3,  "節分",             "鬼は外、福は内"),
    (2,  11, "建国記念の日",     "日本の建国を祝う日"),
    (2,  14, "バレンタインデー", "チョコを渡す日"),
    (3,  3,  "ひな祭り",         "女の子の健やかな成長を祈る日"),
    (3,  8,  "国際女性デー",     "女性の権利を訴える日"),
    (3,  20, "春分の日",         "昼と夜の長さが同じになる日"),
    (4,  1,  "エイプリルフール", "嘘をついていい日"),
    (5,  1,  "メーデー",         "労働者の祭典。国際労働者の日"),
    (5,  3,  "憲法記念日",       "日本国憲法が施行された日"),
    (5,  4,  "みどりの日",       "自然に親しむ日"),
    (5,  5,  "こどもの日",       "子供の成長を祝う日"),
    (6,  23, "沖縄慰霊の日",     "沖縄戦終結の日"),
    (7,  7,  "七夕",             "願い事を書く日"),
    (8,  6,  "広島原爆の日",     "広島に原爆が投下された日"),
    (8,  9,  "長崎原爆の日",     "長崎に原爆が投下された日"),
    (8,  15, "終戦記念日",       "太平洋戦争が終結した日"),
    (9,  23, "秋分の日",         "昼と夜の長さが同じになる日"),
    (10, 10, "体育の日",         "スポーツを楽しむ日"),
    (11, 3,  "文化の日",         "文化・学術を称える日"),
    (11, 23, "勤労感謝の日",     "勤労を尊ぶ日"),
    (12, 24, "クリスマスイブ",   "クリスマス前夜"),
    (12, 25, "クリスマス",       "イエス・キリストの誕生を祝う日"),
    (12, 31, "大晦日",           "1年の最後の日"),
]


# ──────────────────────────────
# 時間帯・季節・記念日
# ──────────────────────────────

def get_time_context() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 10:
        return "朝（おはよう、朝活、起き抜けなどの雰囲気で）"
    elif 10 <= hour < 12:
        return "午前中（元気よく活動中の雰囲気で）"
    elif 12 <= hour < 14:
        return "昼（ランチタイム、お昼休みの雰囲気で）"
    elif 14 <= hour < 17:
        return "午後（まったり、作業中の雰囲気で）"
    elif 17 <= hour < 20:
        return "夕方（帰宅時間、夕暮れの雰囲気で）"
    elif 20 <= hour < 23:
        return "夜（一日の振り返り、リラックスの雰囲気で）"
    else:
        return "深夜（テンションがおかしい、夜更かし中の雰囲気で）"


def get_season_context() -> str:
    month = datetime.now().month
    if month in [3, 4, 5]:
        return "春（桜、新生活、暖かくなってきた雰囲気）"
    elif month in [6, 7, 8]:
        return "夏（暑さ、花火、夏祭り、海の雰囲気）"
    elif month in [9, 10, 11]:
        return "秋（紅葉、食欲の秋、涼しくなってきた雰囲気）"
    else:
        return "冬（寒さ、こたつ、年末年始の雰囲気）"


def get_memorial_day() -> tuple[str, str] | tuple[None, None]:
    now = datetime.now()
    for month, day, name, desc in MEMORIAL_DAYS:
        if now.month == month and now.day == day:
            return name, desc
    return None, None


# ──────────────────────────────
# ユーティリティ
# ──────────────────────────────

def fetch_random_news() -> tuple[str, str] | tuple[None, None]:
    try:
        req = urllib.request.Request(
            random.choice(NEWS_RSS_URLS),
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            root = ET.fromstring(res.read())
        items = root.findall(".//item")
        if not items:
            return None, None
        item = random.choice(items)
        title_el = item.find("title")
        link_el  = item.find("link")
        title = title_el.text if title_el is not None else ""
        url   = link_el.text  if link_el  is not None else ""
        if " - " in title:
            title = title.rsplit(" - ", 1)[0]
        logger.info(f"取得ニュース: {title} / {url}")
        return title, url
    except Exception as e:
        logger.warning(f"ニュース取得失敗: {e}")
        return None, None


def fetch_google_trends() -> list[str]:
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="ja-JP", tz=540, timeout=(10, 25))
        df = pytrends.trending_searches(pn="japan")
        trends = df[0].tolist()
        logger.info(f"トレンド取得: {trends[:5]}")
        return trends
    except Exception as e:
        logger.warning(f"トレンド取得失敗: {e}")
        return []


def _trim(text: str, max_len=140) -> str:
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len - 1] + "…"
    return text


def _call_gemini(prompt: str) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=genai.types.GenerateContentConfig(
            system_instruction=(
                "必ずツイート本文のみを1件出力せよ。"
                "文章は必ず完結した形で終わらせること。"
                "前置き・説明・かぎかっこ・番号は不要。"
            ),
            temperature=0.9,
            automatic_function_calling=genai.types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
        contents=prompt,
    )
    return response.text.strip()


def _append_hashtag(text: str, extra_tags: list[str] = []) -> str:
    tags = [f"#{BOT_NAME}"] + [f"#{t}" for t in extra_tags]
    for tag in tags:
        if tag not in text:
            text = text + " " + tag
    return _trim(text)


# ──────────────────────────────
# 各ツイート生成関数
# ──────────────────────────────

def generate_intro_tweet() -> str:
    prompt = f"""
あなたはXのボットキャラクターです。
キャラクター設定: {BOT_THEME}
{GREETING}

Xに投稿する自己紹介ツイートを1件作成してください。ただし、これがあなたの最初のツイートではありません。

ルール:
- 日本語・120文字以内
- キャラクターらしい挨拶と自己紹介・絵文字1〜3個
- 【名前】や#ハッシュタグは不要（自動付与）
"""
    return _append_hashtag(_call_gemini(prompt))


def generate_general_tweet(force_seasonal: bool = False) -> str:
    time_ctx   = get_time_context()
    season_ctx = get_season_context()

    trends = fetch_google_trends()
    trend_hint = ""
    if trends and not force_seasonal:
        word = random.choice(trends[:10])
        trend_hint = f"今のトレンドワード「{word}」を自然に絡めてもOK。"

    seasonal_instruction = (
        f"今は{season_ctx}なので、季節感を前面に出したツイートにしてください。"
        if force_seasonal else
        f"今は{season_ctx}なので、自然な範囲で季節感を混ぜてもOK。"
    )

    prompt = f"""
あなたはXのボットキャラクターです。
キャラクター設定: {BOT_THEME}
{GREETING}
現在の時間帯: {time_ctx}
{seasonal_instruction}
{trend_hint}

キャラクターとして日常のつぶやきを1件作成してください。

ルール:
- 日本語・120文字以内
- 冒頭はキャラクターらしい挨拶から
- 【{BOT_NAME}】という表記は使わない
- 絵文字1〜3個・ハッシュタグ1〜2個（#名前は自動付与）
"""
    return _append_hashtag(_call_gemini(prompt))


def generate_memorial_tweet(name: str, desc: str) -> str:
    prompt = f"""
あなたはXのボットキャラクターです。
キャラクター設定: {BOT_THEME}
{GREETING}
今日は「{name}」（{desc}）です。

この記念日にちなんだツイートをキャラクターらしく1件作成してください。

ルール:
- 日本語・120文字以内
- 冒頭はキャラクターらしい挨拶から
- 記念日の意味をキャラクターなりに解釈する
- 絵文字1〜3個・ハッシュタグ1〜2個（#名前は自動付与）
"""
    return _append_hashtag(_call_gemini(prompt))


def generate_news_tweet() -> tuple[str, str]:
    news_title, news_url = fetch_random_news()
    news_line = f"参考ニュース: 「{news_title}」" if news_title else "（最近の社会情勢について）"
    time_ctx = get_time_context()

    prompt = f"""
あなたはXのボットキャラクターです。
キャラクター設定: {BOT_THEME}
{news_line}
現在の時間帯: {time_ctx}

上記のニュースへの感想・意見ツイートを1件作成してください。

ルール:
- 日本語・100文字以内（タグとURLを追加するため）
- 冒頭は【{BOT_NAME}通信】から（挨拶不要）
- 絵文字1〜2個・ハッシュタグ1個（#名前は自動付与）
"""
    text = _append_hashtag(_call_gemini(prompt))
    return text, news_url or ""


def generate_eru_tweet(day_count: int = 1) -> str:
    count = random.randint(1, 12)
    text = f"{'える' * count}\n#デイリーえるえる {day_count}日目"
    return _append_hashtag(text)


def generate_neta_tweet() -> str:
    template = random.choice(NETA_TEMPLATES)
    prompt = f"""
あなたはXのボットキャラクターです。
キャラクター設定: {BOT_THEME}

構文「{template}」を使ったネタツイートを1件作成してください。
{{topic}}はキャラクターに合ったボケに差し替えてください。

ルール:
- 日本語・100文字以内
- 脈絡のないボケ・シュールさ歓迎
- 挨拶・【名前】表記不要・絵文字0〜2個・ハッシュタグ不要（自動付与）
"""
    return _append_hashtag(_call_gemini(prompt))
