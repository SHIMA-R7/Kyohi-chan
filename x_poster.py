"""
x_poster.py
Seleniumを使ってXにツイートを投稿するモジュール（Firefox版）
"""

import os
import time
import random
import logging
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.firefox import GeckoDriverManager

load_dotenv()

logger = logging.getLogger(__name__)

X_USERNAME           = os.getenv("X_USERNAME")
X_PASSWORD           = os.getenv("X_PASSWORD")
FIREFOX_PROFILE_PATH = os.getenv("FIREFOX_PROFILE_PATH", "")


def _human_wait(min_sec=0.5, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))


def _human_type(element, text: str):
    """1文字ずつランダム間隔でタイピング"""
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.18))


def _human_scroll(driver, times=3):
    """ランダムスクロール（ActionChainsは使わない）"""
    for _ in range(times):
        scroll_px = random.randint(100, 400)
        driver.execute_script(f"window.scrollBy(0, {scroll_px});")
        _human_wait(0.3, 1.0)
    driver.execute_script(f"window.scrollBy(0, -{random.randint(50, 200)});")
    _human_wait(0.5, 1.5)


def _build_driver() -> webdriver.Firefox:
    options = Options()

    if FIREFOX_PROFILE_PATH:
        from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
        profile = FirefoxProfile(FIREFOX_PROFILE_PATH)
        options.profile = profile
        logger.info(f"Firefoxプロファイルを使用: {FIREFOX_PROFILE_PATH}")

    service = Service(GeckoDriverManager().install())
    driver = webdriver.Firefox(service=service, options=options)
    driver.maximize_window()
    return driver


def _login(driver: webdriver.Firefox, wait: WebDriverWait):
    logger.info("Xにログイン中...")
    driver.get("https://x.com/i/flow/login")
    _human_wait(3, 5)

    el = wait.until(EC.element_to_be_clickable((By.NAME, "text")))
    _human_wait(0.5, 1.5)
    _human_type(el, X_USERNAME)
    el.send_keys(Keys.RETURN)
    _human_wait(2, 4)

    try:
        extra = driver.find_elements(By.NAME, "text")
        if extra and extra[0].is_displayed():
            logger.info("追加確認ステップを検出...")
            extra[0].clear()
            _human_type(extra[0], X_USERNAME)
            extra[0].send_keys(Keys.RETURN)
            _human_wait(2, 4)
    except Exception:
        pass

    pw = wait.until(EC.element_to_be_clickable((By.NAME, "password")))
    _human_wait(0.5, 1.5)
    _human_type(pw, X_PASSWORD)
    pw.send_keys(Keys.RETURN)
    _human_wait(4, 7)

    if "login" in driver.current_url or "flow" in driver.current_url:
        raise RuntimeError("ログインに失敗しました。")
    logger.info("ログイン完了")


def _do_post(driver: webdriver.Firefox, wait: WebDriverWait, text: str):
    """実際の投稿処理（共通）"""
    # ホームをしばらくスクロールして眺める
    logger.info("ホームを閲覧中...")
    _human_scroll(driver, times=random.randint(2, 4))
    _human_wait(2, 5)

    logger.info("ツイート入力欄を探しています...")
    tweet_box = None
    for sel in [
        '[data-testid="tweetTextarea_0"]',
        '[aria-label="ポスト文を入力"]',
        '[aria-label="Post text"]',
        'div[role="textbox"]',
    ]:
        try:
            tweet_box = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            logger.info(f"入力欄を発見: {sel}")
            break
        except Exception:
            continue

    if tweet_box is None:
        raise RuntimeError("ツイート入力欄が見つかりませんでした")

    # スクロールして入力欄を画面内に収めてからクリック
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", tweet_box)
    _human_wait(0.5, 1.0)
    driver.execute_script("arguments[0].click();", tweet_box)
    _human_wait(1, 2)
    _human_type(tweet_box, text)
    _human_wait(1, 3)

    post_button = wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, '[data-testid="tweetButtonInline"]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_button)
    _human_wait(0.5, 1.5)
    driver.execute_script("arguments[0].click();", post_button)
    _human_wait(3, 6)
    logger.info("✅ ツイートを投稿しました")


def post_tweet(text: str):
    """通常ツイート投稿"""
    driver = _build_driver()
    wait = WebDriverWait(driver, 30)
    try:
        driver.get("https://x.com/home")
        _human_wait(4, 7)

        if "login" in driver.current_url or "flow" in driver.current_url:
            if not X_USERNAME or not X_PASSWORD:
                raise ValueError("X_USERNAME / X_PASSWORD が .env に設定されていません")
            _login(driver, wait)
            driver.get("https://x.com/home")
            _human_wait(4, 7)

        _do_post(driver, wait, text)
    finally:
        _human_wait(1, 3)
        driver.quit()


def post_tweet_with_link(text: str, url: str):
    """ニュースリンク付きツイート投稿"""
    full_text = f"{text}\n{url}" if url else text
    post_tweet(full_text)


def post_tweet_with_images(text: str, image_paths: list[str]):
    """
    画像付きツイートを投稿する
    image_paths: ローカルの画像ファイルパスのリスト（最大4枚）
    """
    driver = _build_driver()
    wait = WebDriverWait(driver, 30)

    try:
        driver.get("https://x.com/home")
        _human_wait(4, 7)

        if "login" in driver.current_url or "flow" in driver.current_url:
            if not X_USERNAME or not X_PASSWORD:
                raise ValueError("X_USERNAME / X_PASSWORD が .env に設定されていません")
            _login(driver, wait)
            driver.get("https://x.com/home")
            _human_wait(4, 7)

        logger.info("ホームを閲覧中...")
        _human_scroll(driver, times=random.randint(2, 4))
        _human_wait(2, 5)

        logger.info("ツイート入力欄を探しています...")
        tweet_box = None
        for sel in [
            '[data-testid="tweetTextarea_0"]',
            '[aria-label="ポスト文を入力"]',
            '[aria-label="Post text"]',
            'div[role="textbox"]',
        ]:
            try:
                tweet_box = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                logger.info(f"入力欄を発見: {sel}")
                break
            except Exception:
                continue

        if tweet_box is None:
            raise RuntimeError("ツイート入力欄が見つかりませんでした")

        # 画像をアップロード（ファイル選択input経由）
        for i, image_path in enumerate(image_paths[:4]):
            if not os.path.exists(image_path):
                raise FileNotFoundError(f"画像ファイルが見つかりません: {image_path}")

            file_input = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'input[data-testid="fileInput"]')
                )
            )
            file_input.send_keys(os.path.abspath(image_path))
            logger.info(f"画像アップロード ({i+1}/{len(image_paths)}): {image_path}")
            _human_wait(2, 4)  # アップロード完了を待つ

        # テキスト入力
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", tweet_box)
        _human_wait(0.5, 1.0)
        driver.execute_script("arguments[0].click();", tweet_box)
        _human_wait(1, 2)
        _human_type(tweet_box, text)
        _human_wait(1, 3)

        # 投稿ボタン
        post_button = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[data-testid="tweetButtonInline"]')
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_button)
        _human_wait(0.5, 1.5)
        driver.execute_script("arguments[0].click();", post_button)
        _human_wait(3, 6)

        logger.info("✅ 画像付きツイートを投稿しました")

    finally:
        _human_wait(1, 3)
        driver.quit()


