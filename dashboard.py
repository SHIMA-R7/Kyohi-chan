"""
dashboard.py
Web管理画面サーバー（FastAPI + WebSocket + ngrok）

起動方法:
  python dashboard.py

別ネットワークからアクセス: ngrokのURLをターミナルに表示します
"""

import os
import json
import asyncio
import threading
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── 設定 ──────────────────────────────
DASHBOARD_PORT  = int(os.getenv("DASHBOARD_PORT", "8000"))
NGROK_AUTH_TOKEN = os.getenv("NGROK_AUTH_TOKEN", "")  # ngrokトークン（任意）
DASHBOARD_ADMIN_TOKEN = os.getenv("DASHBOARD_ADMIN_TOKEN", "")
LOG_FILE        = Path(os.path.expanduser("~")) / "Documents" / "tweet_log.txt"
POSTED_LOG      = Path("reposted.json")
ERU_COUNT_LOG   = Path("eru_count.json")
# ──────────────────────────────────────

app = FastAPI(title="Bot管理画面")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

logger = logging.getLogger(__name__)
post_lock = threading.Lock()

# WebSocket接続管理
connected_clients: Set[WebSocket] = set()

# Bot状態管理（auto_tweet.pyと共有するためにファイルベース）
STATE_FILE = Path("bot_state.json")

def require_admin(request: Request):
    if not DASHBOARD_ADMIN_TOKEN:
        if NGROK_AUTH_TOKEN:
            raise HTTPException(
                status_code=500,
                detail="DASHBOARD_ADMIN_TOKEN is required when ngrok is enabled",
            )
        return

    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
    token = (
        request.headers.get("x-dashboard-token")
        or bearer
        or request.query_params.get("token")
        or ""
    )
    if not secrets.compare_digest(token, DASHBOARD_ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="admin token is required")

def read_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"running": True, "paused": False}

def write_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

# ── WebSocketブロードキャスト ──────────

async def broadcast(message: dict):
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    # 接続時に最新ログを送信
    logs = read_log_tail(100)
    await websocket.send_json({"type": "init_logs", "data": logs})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.discard(websocket)

# ── ログ読み込み ──────────────────────

def read_log_tail(n: int = 100) -> list[str]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return lines[-n:]

# ── API ──────────────────────────────

class TweetRequest(BaseModel):
    text: str
    mode: str = "normal"  # normal / news / eru / neta

@app.get("/api/status")
def get_status():
    state = read_state()
    eru = {}
    if ERU_COUNT_LOG.exists():
        with open(ERU_COUNT_LOG, encoding="utf-8") as f:
            eru = json.load(f)
    posted_count = 0
    if POSTED_LOG.exists():
        with open(POSTED_LOG, encoding="utf-8") as f:
            posted_count = len(json.load(f))
    return {
        "running": state.get("running", True),
        "paused": state.get("paused", False),
        "eru_count": eru.get("count", 0),
        "eru_last_date": eru.get("last_date", ""),
        "reposted_count": posted_count,
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/api/logs")
def get_logs(n: int = 200):
    return {"logs": read_log_tail(n)}

@app.post("/api/pause")
def pause_bot(request: Request):
    require_admin(request)
    state = read_state()
    state["paused"] = True
    write_state(state)
    asyncio.run(broadcast({"type": "status", "paused": True}))
    return {"ok": True, "paused": True}

@app.post("/api/resume")
def resume_bot(request: Request):
    require_admin(request)
    state = read_state()
    state["paused"] = False
    write_state(state)
    asyncio.run(broadcast({"type": "status", "paused": False}))
    return {"ok": True, "paused": False}

@app.post("/api/post")
def manual_post(req: TweetRequest, request: Request):
    """手動テスト投稿"""
    require_admin(request)
    import importlib, sys
    try:
        # tweet_generatorを動的インポート
        sys.path.insert(0, str(Path(__file__).parent))
        tg = importlib.import_module("tweet_generator")
        xp = importlib.import_module("x_poster")

        mode = req.mode
        if req.text.strip():
            # テキスト指定があればそのまま投稿
            text = req.text
        elif mode == "news":
            text, url = tg.generate_news_tweet()
            if url:
                text = f"{text}\n{url}"
        elif mode == "eru":
            from datetime import date
            day = 1
            if ERU_COUNT_LOG.exists():
                with open(ERU_COUNT_LOG, encoding="utf-8") as f:
                    day = json.load(f).get("count", 1)
            text = tg.generate_eru_tweet(day_count=day)
        elif mode == "neta":
            text = tg.generate_neta_tweet()
        else:
            text = tg.generate_general_tweet()

        acquired = post_lock.acquire(blocking=False)
        if not acquired:
            raise HTTPException(status_code=409, detail="another post is already running")

        # 別スレッドで投稿（ブロッキング回避）
        def _post():
            try:
                xp.post_tweet(text)
                logger.info(f"[手動投稿] {text}")
            finally:
                post_lock.release()
        threading.Thread(target=_post, daemon=True).start()

        return {"ok": True, "text": text}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── ログファイル監視 ──────────────────

async def tail_log():
    """ログファイルの新着行をWebSocketで配信"""
    if not LOG_FILE.exists():
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch()

    with open(LOG_FILE, encoding="utf-8") as f:
        f.seek(0, 2)  # ファイル末尾へ
        while True:
            line = f.readline()
            if line:
                await broadcast({"type": "log", "data": line.rstrip()})
            else:
                await asyncio.sleep(1)

@app.on_event("startup")
async def startup():
    asyncio.create_task(tail_log())
    # ngrok起動
    if NGROK_AUTH_TOKEN:
        try:
            import ngrok
            listener = await ngrok.forward(DASHBOARD_PORT, authtoken=NGROK_AUTH_TOKEN)
            print(f"\n🌐 ngrok URL: {listener.url()}\n")
        except Exception as e:
            print(f"ngrok起動失敗: {e}")
    else:
        print(f"\n⚠️  NGROK_AUTH_TOKEN未設定。ローカルのみアクセス可能: http://localhost:{DASHBOARD_PORT}\n")
        print("ngrokを使う場合は https://ngrok.com で登録してトークンを .env に設定してください\n")

# ── フロントエンド ────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bot管理画面</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root {
    --red: #e8001a;
    --red-dim: #7a0010;
    --black: #0a0a0a;
    --panel: #111;
    --border: #2a2a2a;
    --text: #e0e0e0;
    --muted: #666;
    --green: #00e676;
    --yellow: #ffd600;
    --mono: 'Share Tech Mono', monospace;
    --sans: 'Noto Sans JP', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--black);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
  }

  /* ヘッダー */
  header {
    background: var(--panel);
    border-bottom: 2px solid var(--red);
    padding: 16px 24px;
    display: flex;
    align-items: center;
    gap: 16px;
    position: sticky; top: 0; z-index: 100;
  }
  .logo {
    font-size: 22px; font-weight: 900;
    color: var(--red);
    letter-spacing: 0.05em;
  }
  .logo span { color: var(--text); }
  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
    animation: pulse 2s infinite;
  }
  .status-dot.paused { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); animation: none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .status-label { font-size: 12px; color: var(--muted); font-family: var(--mono); }

  /* レイアウト */
  .layout {
    display: grid;
    grid-template-columns: 300px 1fr;
    grid-template-rows: auto 1fr;
    gap: 16px;
    padding: 16px;
    height: calc(100vh - 62px);
  }

  /* パネル共通 */
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 16px;
  }
  .panel-title {
    font-size: 11px; font-weight: 700;
    color: var(--muted);
    letter-spacing: 0.15em;
    text-transform: uppercase;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
  }

  /* ステータスパネル */
  .stats { grid-column: 1; grid-row: 1; }
  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat-item { background: #1a1a1a; border-radius: 3px; padding: 10px 12px; }
  .stat-val { font-size: 24px; font-weight: 900; font-family: var(--mono); color: var(--red); }
  .stat-label { font-size: 10px; color: var(--muted); margin-top: 2px; letter-spacing: 0.1em; }

  /* コントロールパネル */
  .controls { grid-column: 1; grid-row: 2; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; }

  /* ボタン */
  .btn {
    padding: 10px 16px; border: none; border-radius: 3px;
    font-family: var(--mono); font-size: 13px; font-weight: 700;
    cursor: pointer; transition: all 0.15s; letter-spacing: 0.05em;
    width: 100%;
  }
  .btn-red { background: var(--red); color: #fff; }
  .btn-red:hover { background: #ff1a33; }
  .btn-green { background: #00c853; color: #000; }
  .btn-green:hover { background: var(--green); }
  .btn-outline { background: transparent; color: var(--text); border: 1px solid var(--border); }
  .btn-outline:hover { border-color: var(--red); color: var(--red); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* 手動投稿フォーム */
  .post-form { display: flex; flex-direction: column; gap: 8px; }
  .post-form select, .post-form textarea {
    background: #1a1a1a; border: 1px solid var(--border);
    color: var(--text); font-family: var(--sans); font-size: 13px;
    padding: 8px 10px; border-radius: 3px; resize: vertical;
    width: 100%;
  }
  .post-form select:focus, .post-form textarea:focus {
    outline: none; border-color: var(--red);
  }
  .post-form textarea { min-height: 80px; }

  /* ログパネル */
  .log-panel {
    grid-column: 2; grid-row: 1 / 3;
    display: flex; flex-direction: column;
  }
  .log-panel .panel-title { display: flex; align-items: center; gap: 8px; }
  .log-live {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--red); animation: pulse 1s infinite;
  }
  #log-container {
    flex: 1; overflow-y: auto; font-family: var(--mono);
    font-size: 11px; line-height: 1.7;
    background: #080808; padding: 12px;
    border-radius: 3px; border: 1px solid #1a1a1a;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-info  { color: #8bc8ff; }
  .log-warn  { color: var(--yellow); }
  .log-error { color: #ff6b6b; }
  .log-ok    { color: var(--green); }

  /* トースト */
  #toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--red); color: #fff;
    padding: 10px 20px; border-radius: 3px;
    font-family: var(--mono); font-size: 13px;
    transform: translateY(60px); opacity: 0;
    transition: all 0.3s; pointer-events: none; z-index: 999;
  }
  #toast.show { transform: translateY(0); opacity: 1; }
  #toast.ok { background: #00c853; color: #000; }

  /* スクロールバー */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: #111; }
  ::-webkit-scrollbar-thumb { background: var(--red-dim); border-radius: 2px; }

  .section-title {
    font-size: 10px; font-weight: 700;
    color: var(--muted); letter-spacing: 0.12em;
    text-transform: uppercase; margin-bottom: 6px;
  }
  .divider { border: none; border-top: 1px solid var(--border); margin: 4px 0; }
</style>
</head>
<body>

<header>
  <div class="logo">共匪ちゃん<span>BOT</span></div>
  <div class="status-dot" id="status-dot"></div>
  <div class="status-label" id="status-label">RUNNING</div>
</header>

<div class="layout">

  <!-- ステータス -->
  <div class="panel stats">
    <div class="panel-title">STATUS</div>
    <div class="stat-grid">
      <div class="stat-item">
        <div class="stat-val" id="eru-count">-</div>
        <div class="stat-label">えるえる連続日数</div>
      </div>
      <div class="stat-item">
        <div class="stat-val" id="reposted-count">-</div>
        <div class="stat-label">復刻投稿済み件数</div>
      </div>
      <div class="stat-item" style="grid-column:1/-1">
        <div class="stat-val" id="eru-last" style="font-size:14px">-</div>
        <div class="stat-label">えるえる最終投稿日</div>
      </div>
    </div>
  </div>

  <!-- コントロール -->
  <div class="controls">

    <!-- Bot制御 -->
    <div class="panel">
      <div class="panel-title">BOT CONTROL</div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-red" id="btn-pause" onclick="pauseBot()">⏸ 一時停止</button>
        <button class="btn btn-green" id="btn-resume" onclick="resumeBot()" disabled>▶ 再開</button>
      </div>
    </div>

    <!-- 手動投稿 -->
    <div class="panel">
      <div class="panel-title">MANUAL POST</div>
      <div class="post-form">
        <div class="section-title">投稿モード</div>
        <select id="post-mode">
          <option value="normal">一般ツイート（AI生成）</option>
          <option value="news">ニュースツイート（AI生成）</option>
          <option value="eru">デイリーえるえる</option>
          <option value="neta">ネタツイート（AI生成）</option>
          <option value="custom">カスタム（テキスト直接入力）</option>
        </select>
        <div class="section-title" style="margin-top:6px">テキスト（空欄でAI生成）</div>
        <textarea id="post-text" placeholder="ここに入力するとそのまま投稿（カスタムモード）"></textarea>
        <button class="btn btn-red" onclick="manualPost()">🚀 テスト投稿</button>
      </div>
    </div>

    <!-- ログフィルター -->
    <div class="panel">
      <div class="panel-title">LOG FILTER</div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <button class="btn btn-outline" onclick="filterLog('all')">全て表示</button>
        <button class="btn btn-outline" onclick="filterLog('error')">エラーのみ</button>
        <button class="btn btn-outline" onclick="filterLog('post')">投稿ログのみ</button>
        <button class="btn btn-outline" onclick="clearLog()">ログをクリア</button>
      </div>
    </div>

  </div>

  <!-- ログ -->
  <div class="panel log-panel">
    <div class="panel-title">
      <span class="log-live"></span>
      LIVE LOG
    </div>
    <div id="log-container"></div>
  </div>

</div>

<div id="toast"></div>

<script>
const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
let allLogs = [];
let currentFilter = 'all';
let isPaused = false;

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'init_logs') {
    allLogs = msg.data;
    renderLogs();
  } else if (msg.type === 'log') {
    allLogs.push(msg.data);
    if (allLogs.length > 500) allLogs.shift();
    appendLog(msg.data);
  } else if (msg.type === 'status') {
    setPaused(msg.paused);
  }
};

function classifyLog(line) {
  if (line.includes('❌') || line.includes('ERROR')) return 'log-error';
  if (line.includes('WARNING')) return 'log-warn';
  if (line.includes('✅') || line.includes('成功')) return 'log-ok';
  return 'log-info';
}

function renderLogs() {
  const container = document.getElementById('log-container');
  container.innerHTML = '';
  allLogs.forEach(line => {
    if (shouldShow(line)) appendLogLine(line);
  });
  container.scrollTop = container.scrollHeight;
}

function appendLog(line) {
  if (!shouldShow(line)) return;
  appendLogLine(line);
  const c = document.getElementById('log-container');
  c.scrollTop = c.scrollHeight;
}

function appendLogLine(line) {
  const div = document.createElement('div');
  div.className = `log-line ${classifyLog(line)}`;
  div.textContent = line;
  document.getElementById('log-container').appendChild(div);
}

function shouldShow(line) {
  if (currentFilter === 'all') return true;
  if (currentFilter === 'error') return line.includes('ERROR') || line.includes('❌');
  if (currentFilter === 'post') return line.includes('✅') || line.includes('生成ツイート') || line.includes('投稿');
  return true;
}

function filterLog(f) {
  currentFilter = f;
  renderLogs();
}

function clearLog() {
  allLogs = [];
  document.getElementById('log-container').innerHTML = '';
}

function setPaused(paused) {
  isPaused = paused;
  document.getElementById('status-dot').className = 'status-dot' + (paused ? ' paused' : '');
  document.getElementById('status-label').textContent = paused ? 'PAUSED' : 'RUNNING';
  document.getElementById('btn-pause').disabled = paused;
  document.getElementById('btn-resume').disabled = !paused;
}

const urlToken = new URLSearchParams(location.search).get('token');
if (urlToken) localStorage.setItem('dashboardAdminToken', urlToken);
const adminToken = localStorage.getItem('dashboardAdminToken') || '';

function authHeaders(extra={}) {
  return adminToken ? {...extra, 'X-Dashboard-Token': adminToken} : extra;
}

async function pauseBot() {
  await fetch('/api/pause', {method:'POST', headers: authHeaders()});
  setPaused(true);
  showToast('一時停止しました');
}

async function resumeBot() {
  await fetch('/api/resume', {method:'POST', headers: authHeaders()});
  setPaused(false);
  showToast('再開しました', true);
}

async function manualPost() {
  const mode = document.getElementById('post-mode').value;
  const text = document.getElementById('post-text').value;
  showToast('投稿中...');
  try {
    const res = await fetch('/api/post', {
      method: 'POST',
      headers: authHeaders({'Content-Type':'application/json'}),
      body: JSON.stringify({text, mode})
    });
    const data = await res.json();
    if (data.ok) {
      showToast('投稿しました！', true);
      document.getElementById('post-text').value = '';
    } else {
      showToast('エラー: ' + data.detail);
    }
  } catch(e) {
    showToast('通信エラー');
  }
}

function showToast(msg, ok=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show' + (ok ? ' ok' : '');
  setTimeout(() => t.className = '', 2500);
}

// ステータス定期取得
async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const d = await res.json();
    document.getElementById('eru-count').textContent = d.eru_count + '日';
    document.getElementById('reposted-count').textContent = d.reposted_count + '件';
    document.getElementById('eru-last').textContent = d.eru_last_date || '-';
    setPaused(d.paused);
  } catch(e) {}
}
fetchStatus();
setInterval(fetchStatus, 10000);
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

if __name__ == "__main__":
    print(f"🚀 管理画面起動: http://localhost:{DASHBOARD_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=DASHBOARD_PORT, log_level="warning")
