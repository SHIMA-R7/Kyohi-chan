"""
daremo_eru.py
「だれでもえるえる」- Googleログインで投稿できるWebサービス

起動方法:
  python daremo_eru.py

.envに必要な設定:
  GOOGLE_CLIENT_ID=...
  GOOGLE_CLIENT_SECRET=...
  DAREMO_SECRET_KEY=ランダムな文字列
  DAREMO_ADMIN_PASSWORD=管理者パスワード
  GEMINI_API_KEY=...
  NGROK_AUTH_TOKEN=...（任意）
  DAREMO_PORT=8001（任意）
  DAREMO_INTERVAL=30（投稿間隔分、任意）
"""

import os
import json
import sqlite3
import asyncio
import hashlib
import secrets
import threading
import time
import random
import logging
import urllib.request
import urllib.parse
from datetime import datetime, date
from pathlib import Path
from typing import Set

import uvicorn
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── 設定 ──────────────────────────────────────────────
PORT               = int(os.getenv("DAREMO_PORT", "8001"))
NGROK_AUTH_TOKEN   = os.getenv("NGROK_AUTH_TOKEN", "")
ADMIN_PASSWORD     = os.getenv("DAREMO_ADMIN_PASSWORD", "changeme")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GOOGLE_CLIENT_ID   = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SECRET_KEY         = os.getenv("DAREMO_SECRET_KEY", "change-this-secret")
POST_INTERVAL_MIN  = int(os.getenv("DAREMO_INTERVAL", "30"))
DB_PATH            = Path(__file__).parent / "daremo_eru.db"
BASE_DIR           = Path(__file__).parent
# ──────────────────────────────────────────────────────

if ADMIN_PASSWORD == "changeme":
    raise RuntimeError("DAREMO_ADMIN_PASSWORD must be set to a non-default value")
if SECRET_KEY == "change-this-secret":
    raise RuntimeError("DAREMO_SECRET_KEY must be set to a non-default value")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="だれでもえるえる")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# OAuth設定
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

connected_ws: Set[WebSocket] = set()


# ── DB初期化 ──────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT    NOT NULL,
                user_name  TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'pending',
                created_at TEXT    NOT NULL,
                posted_at  TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_count (
                user_email TEXT NOT NULL,
                post_date  TEXT NOT NULL,
                count      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_email, post_date)
            )
        """)
        conn.commit()

init_db()


# ── Gemini 安全フィルター ─────────────────────────────

def check_safety(text: str) -> tuple[bool, str]:
    if not GEMINI_API_KEY:
        return True, ""
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
以下のツイート文がXの利用規約に違反する可能性があるかを判定してください。
判定基準: ヘイトスピーチ・差別・暴力助長・性的表現・誹謗中傷・スパム

ツイート文:「{text}」

必ず以下のJSON形式のみで出力（前置き不要）:
{{"safe": true, "reason": ""}}
または
{{"safe": false, "reason": "違反理由を一言で"}}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"temperature": 0.1,
                    "automatic_function_calling": {"disable": True}}
        )
        import re
        m = re.search(r'\{.*\}', response.text.strip(), re.DOTALL)
        if m:
            result = json.loads(m.group())
            return result.get("safe", True), result.get("reason", "")
        return True, ""
    except Exception as e:
        logger.warning(f"安全チェック失敗（スキップ）: {e}")
        return True, ""


# ── 投稿数管理 ────────────────────────────────────────

def get_today_count(email: str) -> int:
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT count FROM daily_count WHERE user_email=? AND post_date=?",
            (email, today)
        ).fetchone()
    return row[0] if row else 0


def increment_count(email: str):
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO daily_count (user_email, post_date, count) VALUES (?, ?, 1)
            ON CONFLICT(user_email, post_date) DO UPDATE SET count = count + 1
        """, (email, today))
        conn.commit()


# ── キュー ────────────────────────────────────────────

def get_queue(limit: int = 50) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, user_email, user_name, text, status, created_at, posted_at "
            "FROM posts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"id": r[0], "user_email": r[1], "user_name": r[2], "text": r[3],
         "status": r[4], "created_at": r[5], "posted_at": r[6]}
        for r in rows
    ]


def claim_post(post_id: int | None = None) -> tuple[int, str] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if post_id is None:
            row = conn.execute(
                "SELECT id, text FROM posts WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, text FROM posts WHERE id=? AND status='pending'",
                (post_id,)
            ).fetchone()
        if not row:
            conn.rollback()
            return None
        conn.execute("UPDATE posts SET status='posting' WHERE id=?", (row[0],))
        conn.commit()
        return row[0], row[1]


# ── WebSocket ─────────────────────────────────────────

async def broadcast(message: dict):
    dead = set()
    for ws in connected_ws:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_ws.difference_update(dead)

@app.websocket("/ws/public")
async def ws_public(websocket: WebSocket):
    await websocket.accept()
    connected_ws.add(websocket)
    await websocket.send_json({"type": "queue", "data": [
        {"id": p["id"], "user_name": p["user_name"],
         "text": p["text"], "status": p["status"], "created_at": p["created_at"]}
        for p in get_queue() if p["status"] == "pending"
    ]})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_ws.discard(websocket)


# ── Google OAuth ──────────────────────────────────────

@app.get("/auth/login")
async def auth_login(request: Request):
    # ngrok経由の場合はhttpsのリダイレクトURIを使う
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        user = token.get("userinfo")
        if not user:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {token['access_token']}"}
                )
                user = res.json()
        request.session["user"] = {
            "email": user.get("email", ""),
            "name":  user.get("name", "anonymous"),
            "picture": user.get("picture", ""),
        }
        return RedirectResponse("/")
    except Exception as e:
        logger.error(f"OAuth失敗: {e}")
        return RedirectResponse("/?error=auth_failed")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/api/me")
async def api_me(request: Request):
    user = request.session.get("user")
    if not user:
        return {"logged_in": False}
    remaining = max(0, 2 - get_today_count(user["email"]))
    return {"logged_in": True, "name": user["name"],
            "picture": user.get("picture", ""), "remaining": remaining}


# ── 投稿API ───────────────────────────────────────────

class PostRequest(BaseModel):
    text: str

class AdminAction(BaseModel):
    password: str
    action: str
    post_id: int | None = None


@app.post("/api/submit")
async def submit_post(req: PostRequest, request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="ログインしてください")

    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="テキストを入力してください")
    if len(text) > 130:
        raise HTTPException(status_code=400, detail="130文字以内で入力してください")
    if get_today_count(user["email"]) >= 2:
        raise HTTPException(status_code=429, detail="本日の投稿上限（2件）に達しました")

    is_safe, reason = check_safety(text)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"投稿できない内容です：{reason}")

    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO posts (user_email, user_name, text, status, created_at) VALUES (?,?,?,'pending',?)",
            (user["email"], user["name"], text, now)
        )
        conn.commit()
    increment_count(user["email"])

    remaining = max(0, 2 - get_today_count(user["email"]))
    queue_len = sum(1 for p in get_queue(200) if p["status"] == "pending")

    await broadcast({"type": "queue", "data": [
        {"id": p["id"], "user_name": p["user_name"],
         "text": p["text"], "status": p["status"], "created_at": p["created_at"]}
        for p in get_queue() if p["status"] == "pending"
    ]})
    logger.info(f"[投稿受付] {user['name']} ({user['email']}): {text}")
    return {"ok": True, "remaining": remaining, "queue_position": queue_len}


@app.get("/api/queue")
def api_queue():
    return {"queue": [
        {"id": p["id"], "user_name": p["user_name"],
         "text": p["text"], "status": p["status"], "created_at": p["created_at"]}
        for p in get_queue()
    ]}


@app.post("/api/admin")
async def admin_action(req: AdminAction):
    if not secrets.compare_digest(
        hashlib.sha256(req.password.encode()).hexdigest(),
        hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest(),
    ):
        raise HTTPException(status_code=403, detail="パスワードが違います")

    if req.action == "get_queue":
        return {"ok": True, "queue": get_queue(200)}

    elif req.action == "delete" and req.post_id:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM posts WHERE id=?", (req.post_id,))
            conn.commit()
        await broadcast({"type": "queue", "data": [
            {"id": p["id"], "user_name": p["user_name"],
             "text": p["text"], "status": p["status"], "created_at": p["created_at"]}
            for p in get_queue() if p["status"] == "pending"
        ]})
        return {"ok": True}

    elif req.action == "force_post" and req.post_id:
        claimed = claim_post(req.post_id)
        if not claimed:
            raise HTTPException(status_code=404, detail="投稿が見つかりません")
        threading.Thread(target=_post_now, args=claimed, daemon=True).start()
        return {"ok": True}

    raise HTTPException(status_code=400, detail="不明なアクション")


# ── 自動投稿 ─────────────────────────────────────────

def _post_now(post_id: int, text: str):
    import sys
    sys.path.insert(0, str(BASE_DIR))
    try:
        from x_poster import post_tweet
        full_text = f"{text} #だれでもえるえる"
        post_tweet(full_text)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE posts SET status='posted', posted_at=? WHERE id=?",
                (datetime.now().isoformat(), post_id)
            )
            conn.commit()
        logger.info(f"[投稿完了] id:{post_id} {full_text}")
    except Exception as e:
        logger.error(f"[投稿失敗] id:{post_id}: {e}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE posts SET status='failed' WHERE id=?", (post_id,))
            conn.commit()


def auto_post_loop():
    while True:
        time.sleep(POST_INTERVAL_MIN * 60 + random.randint(-300, 300))
        claimed = claim_post()
        if claimed:
            logger.info(f"[自動投稿] id:{claimed[0]}")
            _post_now(*claimed)


# ── lifespan ──────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=auto_post_loop, daemon=True).start()
    if NGROK_AUTH_TOKEN:
        try:
            import ngrok
            listener = await ngrok.forward(PORT, authtoken=NGROK_AUTH_TOKEN)
            print(f"\n🌐 ngrok URL: {listener.url()}\n")
            print(f"⚠️  Google OAuthのリダイレクトURIに追加: {listener.url()}/auth/callback\n")
        except Exception as e:
            print(f"ngrok起動失敗: {e}")
    else:
        print(f"\n⚠️  ローカルのみ: http://localhost:{PORT}\n")
    yield

app.router.lifespan_context = lifespan


# ── HTML ─────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>だれでもえるえる</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
:root{
  --red:#e8001a;--black:#080808;--panel:#111;
  --border:#222;--text:#e8e8e8;--muted:#555;
  --green:#00e676;--yellow:#ffd600;
  --mono:'Share Tech Mono',monospace;
  --sans:'Noto Sans JP',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--black);color:var(--text);font-family:var(--sans);min-height:100vh;display:flex;flex-direction:column;align-items:center}
header{width:100%;background:var(--panel);border-bottom:2px solid var(--red);padding:18px 24px;text-align:center}
.logo{font-size:28px;font-weight:900;letter-spacing:.05em}
.logo em{color:var(--red);font-style:normal}
.tagline{font-size:12px;color:var(--muted);margin-top:4px;font-family:var(--mono)}
.card{width:100%;max-width:560px;margin:32px 16px;background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:28px}
.label{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}
.section{margin-bottom:20px}
textarea{width:100%;background:#0e0e0e;border:1px solid var(--border);color:var(--text);font-family:var(--sans);font-size:14px;padding:10px 12px;border-radius:4px;min-height:100px;resize:vertical}
textarea:focus{outline:none;border-color:var(--red)}
.char-count{font-size:11px;color:var(--muted);text-align:right;margin-top:4px;font-family:var(--mono)}
.char-count.warn{color:var(--yellow)}.char-count.over{color:var(--red)}
.btn{width:100%;padding:12px;border:none;border-radius:4px;font-family:var(--mono);font-size:14px;font-weight:700;cursor:pointer;transition:all .15s;letter-spacing:.04em}
.btn-red{background:var(--red);color:#fff}.btn-red:hover{background:#ff1a33}.btn-red:disabled{opacity:.4;cursor:not-allowed}
.btn-google{background:#fff;color:#333;display:flex;align-items:center;justify-content:center;gap:10px;font-family:var(--sans)}
.btn-google:hover{background:#f0f0f0}
.btn-outline{background:transparent;color:var(--muted);border:1px solid var(--border);margin-top:8px}
.btn-outline:hover{border-color:var(--red);color:var(--red)}
.user-bar{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#0e0e0e;border-radius:4px;border:1px solid var(--border);margin-bottom:16px}
.avatar{width:36px;height:36px;border-radius:50%;object-fit:cover;background:var(--red);flex-shrink:0}
.avatar-placeholder{width:36px;height:36px;border-radius:50%;background:var(--red);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:16px;flex-shrink:0}
.user-name{font-weight:700;font-size:14px;flex:1}
.user-remain{font-size:11px;color:var(--muted);font-family:var(--mono)}
.sep{border:none;border-top:1px solid var(--border);margin:20px 0}
.msg{padding:10px 14px;border-radius:4px;font-size:13px;margin-top:12px;display:none}
.msg.show{display:block}
.msg-ok{background:#00c85322;border:1px solid #00c85355;color:var(--green)}
.msg-err{background:#e8001a22;border:1px solid #e8001a55;color:#ff6b6b}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-family:var(--mono);font-weight:700}
.badge-warn{background:#ffd60022;color:var(--yellow);border:1px solid #ffd60055}
.badge-ok{background:#00c85322;color:var(--green);border:1px solid #00c85355}
.badge-err{background:#e8001a22;color:var(--red);border:1px solid #e8001a55}
.queue-item{padding:12px;border:1px solid var(--border);border-radius:4px;margin-bottom:8px;background:#0e0e0e}
.queue-meta{font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:6px;display:flex;gap:8px;align-items:center}
.queue-text{font-size:13px;line-height:1.5}
.queue-empty{text-align:center;color:var(--muted);font-size:13px;padding:20px}
.admin-panel{display:none}.admin-panel.open{display:block}
.admin-header{font-size:11px;font-weight:700;color:var(--red);letter-spacing:.12em;text-transform:uppercase;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.admin-actions{display:flex;gap:6px;margin-top:6px}
.btn-sm{padding:5px 10px;font-size:11px;border:none;border-radius:3px;cursor:pointer;font-family:var(--mono);font-weight:700}
.btn-del{background:#e8001a33;color:var(--red);border:1px solid #e8001a55}.btn-del:hover{background:var(--red);color:#fff}
.btn-force{background:#00c85333;color:var(--green);border:1px solid #00c85355}.btn-force:hover{background:var(--green);color:#000}
input[type=password]{width:100%;background:#0e0e0e;border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:14px;padding:10px 12px;border-radius:4px}
input[type=password]:focus{outline:none;border-color:var(--red)}
#toast{position:fixed;bottom:24px;right:24px;background:var(--red);color:#fff;padding:10px 18px;border-radius:4px;font-family:var(--mono);font-size:13px;transform:translateY(60px);opacity:0;transition:all .3s;pointer-events:none;z-index:999}
#toast.show{transform:translateY(0);opacity:1}#toast.ok{background:#00c853;color:#000}
@media(max-width:600px){.card{margin:16px 8px;padding:20px}}
</style>
</head>
<body>
<header>
  <div class="logo">だれでも<em>えるえる</em></div>
  <div class="tagline">Googleアカウントでログインして投稿 · 1日2件まで · #だれでもえるえる</div>
</header>

<div class="card">

  <!-- ログイン前 -->
  <div id="view-login">
    <div class="section" style="text-align:center;padding:8px 0 16px">
      <div style="font-size:15px;margin-bottom:20px;line-height:1.7;color:var(--muted)">
        Googleアカウントでログインすると<br>えるえるを投稿できます
      </div>
      <a href="/auth/login" style="text-decoration:none;display:block">
        <button class="btn btn-google">
          <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.2l6.7-6.7C35.8 2.5 30.3 0 24 0 14.7 0 6.8 5.4 3 13.3l7.8 6C12.7 13 17.9 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h12.7c-.6 3-2.3 5.5-4.8 7.2l7.5 5.8c4.4-4.1 7.1-10.1 7.1-17z"/><path fill="#FBBC05" d="M10.8 28.7A14.5 14.5 0 0 1 9.5 24c0-1.6.3-3.2.8-4.7L2.5 13.3A23.9 23.9 0 0 0 0 24c0 3.9.9 7.5 2.5 10.7l8.3-6z"/><path fill="#34A853" d="M24 48c6.3 0 11.6-2.1 15.5-5.7l-7.5-5.8c-2.1 1.4-4.8 2.3-8 2.3-6.1 0-11.3-4.1-13.2-9.7l-8.3 6C6.8 42.6 14.7 48 24 48z"/></svg>
          Googleでログイン
        </button>
      </a>
    </div>
    <div style="font-size:12px;color:var(--muted);line-height:1.8;text-align:center">
      📌 1日2件まで投稿できます<br>
      📌 不適切な内容は自動でブロックされます<br>
      📌 投稿は順番にXへ自動送信されます
    </div>
  </div>

  <!-- ログイン後 -->
  <div id="view-post" style="display:none">
    <div class="user-bar">
      <img id="user-pic" class="avatar" src="" alt="" onerror="this.style.display='none'">
      <div id="user-pic-placeholder" class="avatar-placeholder" style="display:none">?</div>
      <div style="flex:1">
        <div class="user-name" id="user-name-disp"></div>
        <div class="user-remain" id="user-remain-disp"></div>
      </div>
      <a href="/auth/logout" style="text-decoration:none">
        <button class="btn btn-outline" style="width:auto;padding:6px 12px;font-size:12px">ログアウト</button>
      </a>
    </div>

    <div class="section">
      <div class="label">投稿内容</div>
      <textarea id="post-text" placeholder="えるえるえる… （130文字以内）" maxlength="130" oninput="updateCount()"></textarea>
      <div class="char-count" id="char-count">0 / 130</div>
    </div>
    <button class="btn btn-red" onclick="submitPost()" id="btn-submit">えるえる投稿する 🐣</button>
    <div class="msg" id="post-msg"></div>
  </div>

  <hr class="sep">

  <!-- キュー -->
  <div class="section">
    <div class="label">投稿待ちキュー</div>
    <div id="queue-list"><div class="queue-empty">読み込み中...</div></div>
  </div>

  <hr class="sep">

  <!-- 管理者 -->
  <div class="section">
    <div class="label">管理者</div>
    <div style="display:flex;gap:8px">
      <input type="password" id="admin-pw" placeholder="パスワード" onkeydown="if(event.key==='Enter')adminLogin()">
      <button class="btn btn-red" style="width:auto;padding:10px 20px;white-space:nowrap" onclick="adminLogin()">ログイン</button>
    </div>
  </div>
  <div class="admin-panel" id="admin-panel">
    <div class="admin-header">⚙️ ADMIN PANEL</div>
    <div id="admin-queue"></div>
  </div>

</div>

<div id="toast"></div>

<script>
let adminPw = null;

// WebSocket
function connectWS() {
  const ws = new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/public`);
  ws.onmessage = e => { const m=JSON.parse(e.data); if(m.type==='queue') renderQueue(m.data); };
  ws.onclose = () => setTimeout(connectWS, 3000);
}
connectWS();

// ログイン状態チェック
async function checkMe() {
  const res = await fetch('/api/me');
  const d = await res.json();
  if (d.logged_in) {
    document.getElementById('view-login').style.display = 'none';
    document.getElementById('view-post').style.display = 'block';
    const pic = document.getElementById('user-pic');
    if (d.picture) {
      pic.src = d.picture;
      pic.style.display = 'block';
      document.getElementById('user-pic-placeholder').style.display = 'none';
    } else {
      pic.style.display = 'none';
      document.getElementById('user-pic-placeholder').style.display = 'flex';
    }
    document.getElementById('user-name-disp').textContent = d.name;
    updateRemaining(d.remaining);
    if (d.remaining === 0) {
      document.getElementById('btn-submit').disabled = true;
      showMsg('post-msg', '本日の投稿上限（2件）に達しています', true);
    }
  }
}
checkMe();

function updateRemaining(n) {
  const el = document.getElementById('user-remain-disp');
  el.textContent = `本日あと ${n} 件投稿できます`;
  el.style.color = n===0?'var(--red)':n===1?'var(--yellow)':'var(--green)';
}

function updateCount() {
  const len = document.getElementById('post-text').value.length;
  const el = document.getElementById('char-count');
  el.textContent = `${len} / 130`;
  el.className = 'char-count'+(len>120?(len>=130?' over':' warn'):'');
}

async function submitPost() {
  const text = document.getElementById('post-text').value.trim();
  if (!text) return showMsg('post-msg','内容を入力してください',true);
  if (text.length > 130) return showMsg('post-msg','130文字以内で入力してください',true);
  document.getElementById('btn-submit').disabled = true;
  showMsg('post-msg','送信中（安全チェック中）...', false);
  try {
    const res = await fetch('/api/submit',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text})
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail);
    document.getElementById('post-text').value='';
    updateCount();
    updateRemaining(d.remaining);
    showMsg('post-msg',`✅ 受付完了！キュー${d.queue_position}番目です`,false);
    showToast('投稿をキューに追加しました！',true);
    if(d.remaining===0) document.getElementById('btn-submit').disabled=true;
  } catch(e) {
    showMsg('post-msg',`❌ ${e.message}`,true);
    document.getElementById('btn-submit').disabled=false;
  }
}

function renderQueue(items) {
  const el = document.getElementById('queue-list');
  if (!items.length) { el.innerHTML='<div class="queue-empty">現在キューは空です</div>'; return; }
  el.innerHTML = items.map((item,idx)=>`
    <div class="queue-item">
      <div class="queue-meta">
        <span class="badge badge-warn">待機中 #${idx+1}</span>
        <span>${escHtml(item.user_name)}</span>
        <span>${item.created_at.slice(0,16).replace('T',' ')}</span>
      </div>
      <div class="queue-text">${escHtml(item.text)}</div>
    </div>
  `).join('');
  if(adminPw) {
    fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:adminPw,action:'get_queue'})})
    .then(r=>r.json()).then(d=>renderAdminQueue(d.queue));
  }
}

async function adminLogin() {
  const pw = document.getElementById('admin-pw').value;
  if(!pw) return;
  try {
    const res = await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw,action:'get_queue'})});
    if(!res.ok) throw new Error('パスワードが違います');
    const d = await res.json();
    adminPw = pw;
    document.getElementById('admin-panel').classList.add('open');
    renderAdminQueue(d.queue);
    showToast('管理者ログイン成功',true);
  } catch(e){ showToast(e.message); }
}

function renderAdminQueue(items) {
  const el = document.getElementById('admin-queue');
  if(!items.length){el.innerHTML='<div class="queue-empty">投稿なし</div>';return;}
  el.innerHTML = items.map(item=>`
    <div class="queue-item">
      <div class="queue-meta">
        <span class="badge ${item.status==='pending'?'badge-warn':item.status==='posted'?'badge-ok':'badge-err'}">
          ${item.status==='pending'?'待機':item.status==='posted'?'投稿済':'失敗'}
        </span>
        <span>${escHtml(item.user_name)}</span>
        <span>#${item.id}</span>
        <span>${item.created_at.slice(0,16).replace('T',' ')}</span>
      </div>
      <div class="queue-text">${escHtml(item.text)}</div>
      ${item.status==='pending'?`
      <div class="admin-actions">
        <button class="btn-sm btn-force" onclick="adminAct('force_post',${item.id})">▶ 今すぐ投稿</button>
        <button class="btn-sm btn-del" onclick="adminAct('delete',${item.id})">🗑 削除</button>
      </div>`:''}
    </div>
  `).join('');
}

async function adminAct(action,id){
  try{
    const res=await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:adminPw,action,post_id:id})});
    if(!res.ok) throw new Error((await res.json()).detail);
    showToast(action==='delete'?'削除しました':'投稿しました',true);
    const d=await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:adminPw,action:'get_queue'})}).then(r=>r.json());
    renderAdminQueue(d.queue);
  }catch(e){showToast(e.message);}
}

function showMsg(id,text,isErr){
  const el=document.getElementById(id);
  el.textContent=text; el.className='msg show '+(isErr?'msg-err':'msg-ok');
}
function showToast(msg,ok=false){
  const t=document.getElementById('toast');
  t.textContent=msg; t.className='show'+(ok?' ok':'');
  setTimeout(()=>t.className='',2500);
}
function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

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
