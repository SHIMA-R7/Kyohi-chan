"""
daremo_eru.py
「だれでもえるえる」- 不特定多数がXに投稿できるWebサービス

起動方法:
  python daremo_eru.py

機能:
  - XユーザーIDで認証（X API v2で存在確認）
  - 1日2件の投稿制限
  - Geminiで不適切文章フィルタリング
  - 投稿キューに保存→一定間隔でXに自動投稿
  - パスワードで管理者機能を解放
"""

import os
import json
import sqlite3
import asyncio
import hashlib
import threading
import time
import random
import urllib.request
import urllib.parse
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── 設定 ──────────────────────────────────────────────
PORT              = int(os.getenv("DAREMO_PORT", "8001"))
NGROK_AUTH_TOKEN  = os.getenv("NGROK_AUTH_TOKEN", "")
ADMIN_PASSWORD    = os.getenv("DAREMO_ADMIN_PASSWORD", "changeme")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
X_BEARER_TOKEN    = os.getenv("X_BEARER_TOKEN", "")   # X API v2 Bearer Token
POST_INTERVAL_MIN = int(os.getenv("DAREMO_INTERVAL", "30"))  # 投稿間隔（分）
DB_PATH           = Path(__file__).parent / "daremo_eru.db"
BASE_DIR          = Path(__file__).parent
# ──────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="だれでもえるえる")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

connected_ws: Set[WebSocket] = set()


# ── DB初期化 ──────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                x_user_id TEXT    NOT NULL,
                text      TEXT    NOT NULL,
                status    TEXT    NOT NULL DEFAULT 'pending',
                created_at TEXT   NOT NULL,
                posted_at  TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_count (
                x_user_id TEXT NOT NULL,
                post_date TEXT NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (x_user_id, post_date)
            )
        """)
        conn.commit()

init_db()


# ── X API ユーザー存在確認 ────────────────────────────

def verify_x_user(username: str) -> bool:
    """X API v2でユーザーが存在するか確認する"""
    if not X_BEARER_TOKEN:
        # トークン未設定時は簡易チェック（英数字+アンダースコア、4〜15文字）
        import re
        return bool(re.match(r'^[a-zA-Z0-9_]{4,15}$', username))
    try:
        url = f"https://api.twitter.com/2/users/by/username/{urllib.parse.quote(username)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {X_BEARER_TOKEN}"
        })
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read())
            return "data" in data and "id" in data["data"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        logger.warning(f"X API エラー: {e}")
        return False
    except Exception as e:
        logger.warning(f"X API 接続失敗（フォールバック）: {e}")
        # API接続失敗時はフォーマットチェックのみ
        import re
        return bool(re.match(r'^[a-zA-Z0-9_]{4,15}$', username))


# ── Gemini 安全フィルター ─────────────────────────────

def check_safety(text: str) -> tuple[bool, str]:
    """
    Geminiで投稿の安全性を確認する
    Returns: (is_safe, reason)
    """
    if not GEMINI_API_KEY:
        return True, ""
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
以下のツイート文が、Xの利用規約に違反する可能性があるかを判定してください。
判定基準:
- ヘイトスピーチ・差別的表現
- 暴力・自傷・自殺の助長
- 性的に露骨な表現
- 個人への誹謗中傷・脅迫
- スパムや詐欺的な内容
- 意味がない・極端に短い

ツイート文:「{text}」

回答は必ず以下のJSON形式のみで出力してください（前置き不要）:
{{"safe": true, "reason": ""}}
または
{{"safe": false, "reason": "違反理由を一言で"}}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"temperature": 0.1}
        )
        raw = response.text.strip()
        # JSON部分を抽出
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            return result.get("safe", True), result.get("reason", "")
        return True, ""
    except Exception as e:
        logger.warning(f"安全チェック失敗（スキップ）: {e}")
        return True, ""


# ── 投稿数カウント ────────────────────────────────────

def get_today_count(x_user_id: str) -> int:
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT count FROM daily_count WHERE x_user_id=? AND post_date=?",
            (x_user_id, today)
        ).fetchone()
    return row[0] if row else 0


def increment_count(x_user_id: str):
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO daily_count (x_user_id, post_date, count)
            VALUES (?, ?, 1)
            ON CONFLICT(x_user_id, post_date) DO UPDATE SET count = count + 1
        """, (x_user_id, today))
        conn.commit()


# ── WebSocket ─────────────────────────────────────────

async def broadcast_admin(message: dict):
    dead = set()
    for ws in connected_ws:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_ws.difference_update(dead)

@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    await websocket.accept()
    connected_ws.add(websocket)
    # 接続時にキュー情報を送信
    await websocket.send_json({"type": "queue", "data": get_queue()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_ws.discard(websocket)


# ── キュー取得 ────────────────────────────────────────

def get_queue(limit: int = 50) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, x_user_id, text, status, created_at, posted_at "
            "FROM posts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"id": r[0], "x_user_id": r[1], "text": r[2],
         "status": r[3], "created_at": r[4], "posted_at": r[5]}
        for r in rows
    ]


# ── API ───────────────────────────────────────────────

class VerifyRequest(BaseModel):
    username: str

class PostRequest(BaseModel):
    x_user_id: str
    text: str

class AdminAction(BaseModel):
    password: str
    action: str
    post_id: int | None = None

def check_admin(password: str):
    if hashlib.sha256(password.encode()).hexdigest() != \
       hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest():
        raise HTTPException(status_code=403, detail="パスワードが違います")


@app.post("/api/verify")
async def verify_user(req: VerifyRequest):
    """XユーザーIDの存在確認"""
    username = req.username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="ユーザーIDを入力してください")

    exists = verify_x_user(username)
    if not exists:
        raise HTTPException(status_code=404, detail="Xに存在しないユーザーIDです")

    remaining = max(0, 2 - get_today_count(username))
    return {"ok": True, "username": username, "remaining": remaining}


@app.post("/api/submit")
async def submit_post(req: PostRequest):
    """投稿をキューに追加"""
    username = req.x_user_id.lstrip("@").strip()
    text = req.text.strip()

    if not text:
        raise HTTPException(status_code=400, detail="テキストを入力してください")
    if len(text) > 130:
        raise HTTPException(status_code=400, detail="130文字以内で入力してください")

    # 1日2件制限
    if get_today_count(username) >= 2:
        raise HTTPException(status_code=429, detail="本日の投稿上限（2件）に達しました")

    # 安全チェック
    is_safe, reason = check_safety(text)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"投稿できない内容です：{reason}")

    # キューに保存
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO posts (x_user_id, text, status, created_at) VALUES (?, ?, 'pending', ?)",
            (username, text, now)
        )
        conn.commit()
    increment_count(username)

    remaining = max(0, 2 - get_today_count(username))
    queue_len = len([p for p in get_queue(200) if p["status"] == "pending"])

    await broadcast_admin({"type": "queue", "data": get_queue()})
    logger.info(f"[投稿受付] @{username}: {text}")
    return {"ok": True, "remaining": remaining, "queue_position": queue_len}


@app.get("/api/queue")
def api_get_queue():
    return {"queue": get_queue()}


@app.post("/api/admin")
async def admin_action(req: AdminAction):
    """管理者操作（削除・強制投稿）"""
    check_admin(req.password)

    if req.action == "delete" and req.post_id:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM posts WHERE id=?", (req.post_id,))
            conn.commit()
        await broadcast_admin({"type": "queue", "data": get_queue()})
        return {"ok": True}

    elif req.action == "force_post" and req.post_id:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT text FROM posts WHERE id=? AND status='pending'",
                (req.post_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="投稿が見つかりません")
        _post_now(req.post_id, row[0])
        await broadcast_admin({"type": "queue", "data": get_queue()})
        return {"ok": True}

    elif req.action == "get_queue":
        return {"ok": True, "queue": get_queue(200)}

    raise HTTPException(status_code=400, detail="不明なアクション")


# ── 自動投稿ループ ────────────────────────────────────

def _post_now(post_id: int, text: str):
    """実際にXへ投稿する"""
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from x_poster import post_tweet
        full_text = f"{text} #だれでもえるえる"
        post_tweet(full_text)
        now = datetime.now().isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE posts SET status='posted', posted_at=? WHERE id=?",
                (now, post_id)
            )
            conn.commit()
        logger.info(f"[投稿完了] id:{post_id} {full_text}")
    except Exception as e:
        logger.error(f"[投稿失敗] id:{post_id}: {e}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE posts SET status='failed' WHERE id=?", (post_id,)
            )
            conn.commit()


def auto_post_loop():
    """一定間隔でキューから1件ずつ投稿する"""
    while True:
        time.sleep(POST_INTERVAL_MIN * 60 + random.randint(-300, 300))
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT id, text FROM posts WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if row:
            logger.info(f"[自動投稿] id:{row[0]}")
            _post_now(row[0], row[1])
        else:
            logger.info("[自動投稿] キューが空です")


@app.on_event("startup")
async def startup():
    threading.Thread(target=auto_post_loop, daemon=True).start()
    if NGROK_AUTH_TOKEN:
        try:
            import ngrok
            listener = await ngrok.forward(PORT, authtoken=NGROK_AUTH_TOKEN)
            print(f"\n🌐 ngrok URL: {listener.url()}\n")
        except Exception as e:
            print(f"ngrok起動失敗: {e}")
    else:
        print(f"\n⚠️  ローカルのみ: http://localhost:{PORT}\n")


# ── フロントエンド HTML ───────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>だれでもえるえる</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
:root {
  --red:#e8001a; --black:#080808; --panel:#111;
  --border:#222; --text:#e8e8e8; --muted:#555;
  --green:#00e676; --yellow:#ffd600;
  --mono:'Share Tech Mono',monospace;
  --sans:'Noto Sans JP',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--black);color:var(--text);font-family:var(--sans);min-height:100vh;display:flex;flex-direction:column;align-items:center}

/* ヘッダー */
header{width:100%;background:var(--panel);border-bottom:2px solid var(--red);padding:18px 24px;text-align:center}
.logo{font-size:28px;font-weight:900;letter-spacing:.05em}
.logo em{color:var(--red);font-style:normal}
.tagline{font-size:12px;color:var(--muted);margin-top:4px;font-family:var(--mono)}

/* メインカード */
.card{width:100%;max-width:560px;margin:32px 16px;background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:28px}
.section{margin-bottom:24px}
.label{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}

/* 入力 */
input,textarea{
  width:100%;background:#0e0e0e;border:1px solid var(--border);
  color:var(--text);font-family:var(--sans);font-size:14px;
  padding:10px 12px;border-radius:4px;transition:border .15s;
}
input:focus,textarea:focus{outline:none;border-color:var(--red)}
textarea{min-height:100px;resize:vertical}
.char-count{font-size:11px;color:var(--muted);text-align:right;margin-top:4px;font-family:var(--mono)}
.char-count.warn{color:var(--yellow)}
.char-count.over{color:var(--red)}

/* ボタン */
.btn{width:100%;padding:12px;border:none;border-radius:4px;font-family:var(--mono);font-size:14px;font-weight:700;cursor:pointer;transition:all .15s;letter-spacing:.04em}
.btn-red{background:var(--red);color:#fff}
.btn-red:hover{background:#ff1a33}
.btn-red:disabled{opacity:.4;cursor:not-allowed}
.btn-outline{background:transparent;color:var(--muted);border:1px solid var(--border);margin-top:8px}
.btn-outline:hover{border-color:var(--red);color:var(--red)}

/* ステータス */
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-family:var(--mono);font-weight:700}
.badge-ok{background:#00c85322;color:var(--green);border:1px solid #00c85355}
.badge-warn{background:#ffd60022;color:var(--yellow);border:1px solid #ffd60055}
.badge-err{background:#e8001a22;color:var(--red);border:1px solid #e8001a55}
.user-info{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#0e0e0e;border-radius:4px;border:1px solid var(--border)}
.avatar{width:32px;height:32px;border-radius:50%;background:var(--red);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:14px;flex-shrink:0}
.user-name{font-weight:700;font-size:14px}
.user-sub{font-size:11px;color:var(--muted);font-family:var(--mono)}

/* キュー */
.queue-item{padding:12px;border:1px solid var(--border);border-radius:4px;margin-bottom:8px;background:#0e0e0e}
.queue-meta{font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:6px;display:flex;gap:8px;align-items:center}
.queue-text{font-size:13px;line-height:1.5}
.queue-empty{text-align:center;color:var(--muted);font-size:13px;padding:20px}

/* 管理パネル */
.admin-panel{display:none}
.admin-panel.open{display:block}
.admin-header{font-size:11px;font-weight:700;color:var(--red);letter-spacing:.12em;text-transform:uppercase;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.admin-actions{display:flex;gap:6px;margin-top:6px}
.btn-sm{padding:5px 10px;font-size:11px;border:none;border-radius:3px;cursor:pointer;font-family:var(--mono);font-weight:700}
.btn-del{background:#e8001a33;color:var(--red);border:1px solid #e8001a55}
.btn-del:hover{background:var(--red);color:#fff}
.btn-force{background:#00c85333;color:var(--green);border:1px solid #00c85355}
.btn-force:hover{background:var(--green);color:#000}

/* メッセージ */
.msg{padding:10px 14px;border-radius:4px;font-size:13px;margin-top:12px;display:none}
.msg.show{display:block}
.msg-ok{background:#00c85322;border:1px solid #00c85355;color:var(--green)}
.msg-err{background:#e8001a22;border:1px solid #e8001a55;color:#ff6b6b}

/* セパレーター */
.sep{border:none;border-top:1px solid var(--border);margin:20px 0}

/* トースト */
#toast{position:fixed;bottom:24px;right:24px;background:var(--red);color:#fff;padding:10px 18px;border-radius:4px;font-family:var(--mono);font-size:13px;transform:translateY(60px);opacity:0;transition:all .3s;pointer-events:none;z-index:999}
#toast.show{transform:translateY(0);opacity:1}
#toast.ok{background:#00c853;color:#000}

@media(max-width:600px){.card{margin:16px 8px;padding:20px}}
</style>
</head>
<body>

<header>
  <div class="logo">だれでも<em>えるえる</em></div>
  <div class="tagline">powered by #だれでもえるえる</div>
</header>

<div class="card">

  <!-- Step1: ユーザー認証 -->
  <div id="step-auth">
    <div class="section">
      <div class="label">XのユーザーIDを入力</div>
      <div style="display:flex;gap:8px;margin-top:4px">
        <input type="text" id="input-username" placeholder="@なしで入力（例: elmo_bot）" maxlength="15"
          onkeydown="if(event.key==='Enter')verifyUser()">
        <button class="btn btn-red" style="width:auto;padding:10px 20px;white-space:nowrap" onclick="verifyUser()" id="btn-verify">確認</button>
      </div>
      <div class="msg" id="auth-msg"></div>
    </div>
    <div class="section" style="color:var(--muted);font-size:12px;line-height:1.8">
      <div>📌 1日2件まで投稿できます</div>
      <div>📌 不適切な内容は自動でブロックされます</div>
      <div>📌 投稿は順番にXへ自動送信されます</div>
    </div>
  </div>

  <!-- Step2: 投稿フォーム -->
  <div id="step-post" style="display:none">
    <div class="user-info" id="user-info">
      <div class="avatar" id="user-avatar">?</div>
      <div>
        <div class="user-name" id="user-name-disp"></div>
        <div class="user-sub" id="user-remain-disp"></div>
      </div>
    </div>

    <hr class="sep">

    <div class="section">
      <div class="label">投稿内容</div>
      <textarea id="post-text" placeholder="えるえるえる… （130文字以内）" maxlength="130"
        oninput="updateCount()"></textarea>
      <div class="char-count" id="char-count">0 / 130</div>
    </div>

    <button class="btn btn-red" onclick="submitPost()" id="btn-submit">えるえる投稿する 🐣</button>
    <button class="btn btn-outline" onclick="resetUser()">← ユーザーIDを変える</button>
    <div class="msg" id="post-msg"></div>
  </div>

  <hr class="sep">

  <!-- キュー表示 -->
  <div class="section">
    <div class="label">投稿待ちキュー</div>
    <div id="queue-list"><div class="queue-empty">読み込み中...</div></div>
  </div>

  <hr class="sep">

  <!-- 管理者ログイン -->
  <div class="section">
    <div class="label">管理者</div>
    <div style="display:flex;gap:8px">
      <input type="password" id="admin-pw" placeholder="パスワード" onkeydown="if(event.key==='Enter')adminLogin()">
      <button class="btn btn-red" style="width:auto;padding:10px 20px;white-space:nowrap" onclick="adminLogin()">ログイン</button>
    </div>
  </div>

  <!-- 管理パネル -->
  <div class="admin-panel" id="admin-panel">
    <div class="admin-header">⚙️ ADMIN PANEL</div>
    <div id="admin-queue"></div>
  </div>

</div>

<div id="toast"></div>

<script>
let currentUser = null;
let adminPw = null;
let ws = null;

// WebSocket接続
function connectWS() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/admin`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'queue') renderQueue(msg.data);
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}
connectWS();

// ── ユーザー認証 ──
async function verifyUser() {
  const username = document.getElementById('input-username').value.trim().replace(/^@/, '');
  if (!username) return showMsg('auth-msg', 'ユーザーIDを入力してください', true);

  document.getElementById('btn-verify').disabled = true;
  showMsg('auth-msg', '確認中...', false, 'info');

  try {
    const res = await fetch('/api/verify', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);

    currentUser = data.username;
    document.getElementById('step-auth').style.display = 'none';
    document.getElementById('step-post').style.display = 'block';
    document.getElementById('user-avatar').textContent = username[0].toUpperCase();
    document.getElementById('user-name-disp').textContent = '@' + data.username;
    updateRemaining(data.remaining);

    if (data.remaining === 0) {
      document.getElementById('btn-submit').disabled = true;
      showMsg('post-msg', '本日の投稿上限（2件）に達しています', true);
    }
  } catch(e) {
    showMsg('auth-msg', e.message, true);
  } finally {
    document.getElementById('btn-verify').disabled = false;
  }
}

function updateRemaining(n) {
  const el = document.getElementById('user-remain-disp');
  el.textContent = `本日あと ${n} 件投稿できます`;
  el.style.color = n === 0 ? 'var(--red)' : n === 1 ? 'var(--yellow)' : 'var(--green)';
}

function resetUser() {
  currentUser = null;
  document.getElementById('step-auth').style.display = 'block';
  document.getElementById('step-post').style.display = 'none';
  document.getElementById('input-username').value = '';
  document.getElementById('post-text').value = '';
  document.getElementById('char-count').textContent = '0 / 130';
  hideMsg('auth-msg'); hideMsg('post-msg');
}

// ── 投稿 ──
function updateCount() {
  const len = document.getElementById('post-text').value.length;
  const el = document.getElementById('char-count');
  el.textContent = `${len} / 130`;
  el.className = 'char-count' + (len > 120 ? (len >= 130 ? ' over' : ' warn') : '');
}

async function submitPost() {
  const text = document.getElementById('post-text').value.trim();
  if (!text) return showMsg('post-msg', '内容を入力してください', true);
  if (text.length > 130) return showMsg('post-msg', '130文字以内で入力してください', true);

  document.getElementById('btn-submit').disabled = true;
  showMsg('post-msg', '送信中...', false, 'info');

  try {
    const res = await fetch('/api/submit', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({x_user_id: currentUser, text})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);

    document.getElementById('post-text').value = '';
    updateCount();
    updateRemaining(data.remaining);
    showMsg('post-msg', `✅ 投稿受付完了！キュー${data.queue_position}番目です`, false);
    showToast('投稿をキューに追加しました！', true);
    if (data.remaining === 0) document.getElementById('btn-submit').disabled = true;

  } catch(e) {
    showMsg('post-msg', `❌ ${e.message}`, true);
    document.getElementById('btn-submit').disabled = false;
  }
}

// ── キュー表示 ──
function renderQueue(items) {
  const pending = items.filter(i => i.status === 'pending');
  const el = document.getElementById('queue-list');
  if (pending.length === 0) {
    el.innerHTML = '<div class="queue-empty">現在キューは空です</div>';
  } else {
    el.innerHTML = pending.map((item, idx) => `
      <div class="queue-item">
        <div class="queue-meta">
          <span class="badge badge-warn">待機中 #${idx+1}</span>
          <span>@${item.x_user_id}</span>
          <span>${item.created_at.slice(0,16).replace('T',' ')}</span>
        </div>
        <div class="queue-text">${escHtml(item.text)}</div>
      </div>
    `).join('');
  }

  // 管理者キューも更新
  if (adminPw) renderAdminQueue(items);
}

// ── 管理者 ──
async function adminLogin() {
  const pw = document.getElementById('admin-pw').value;
  if (!pw) return;
  try {
    const res = await fetch('/api/admin', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pw, action: 'get_queue'})
    });
    if (!res.ok) throw new Error('パスワードが違います');
    const data = await res.json();
    adminPw = pw;
    document.getElementById('admin-panel').classList.add('open');
    renderAdminQueue(data.queue);
    showToast('管理者ログイン成功', true);
  } catch(e) {
    showToast(e.message);
  }
}

function renderAdminQueue(items) {
  const el = document.getElementById('admin-queue');
  if (!items.length) { el.innerHTML = '<div class="queue-empty">投稿なし</div>'; return; }
  el.innerHTML = items.map(item => `
    <div class="queue-item">
      <div class="queue-meta">
        <span class="badge ${item.status==='pending'?'badge-warn':item.status==='posted'?'badge-ok':'badge-err'}">
          ${item.status==='pending'?'待機':item.status==='posted'?'投稿済':'失敗'}
        </span>
        <span>@${item.x_user_id}</span>
        <span>#${item.id}</span>
        <span>${item.created_at.slice(0,16).replace('T',' ')}</span>
      </div>
      <div class="queue-text">${escHtml(item.text)}</div>
      ${item.status==='pending' ? `
      <div class="admin-actions">
        <button class="btn-sm btn-force" onclick="adminAct('force_post',${item.id})">▶ 今すぐ投稿</button>
        <button class="btn-sm btn-del" onclick="adminAct('delete',${item.id})">🗑 削除</button>
      </div>` : ''}
    </div>
  `).join('');
}

async function adminAct(action, id) {
  try {
    const res = await fetch('/api/admin', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: adminPw, action, post_id: id})
    });
    if (!res.ok) throw new Error((await res.json()).detail);
    showToast(action === 'delete' ? '削除しました' : '投稿しました', true);
  } catch(e) { showToast(e.message); }
}

// ── ユーティリティ ──
function showMsg(id, text, isErr, type) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg show ' + (isErr ? 'msg-err' : 'msg-ok');
}
function hideMsg(id) {
  document.getElementById(id).className = 'msg';
}
function showToast(msg, ok=false) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show' + (ok?' ok':'');
  setTimeout(() => t.className='', 2500);
}
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// 初期キュー取得
fetch('/api/queue').then(r=>r.json()).then(d=>renderQueue(d.queue));
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

if __name__ == "__main__":
    print(f"🚀 だれでもえるえる起動: http://localhost:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
