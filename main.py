from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import json, os, re, asyncio, sqlite3, hashlib, secrets
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(BASE_DIR, "projects")
os.makedirs(SAVE_DIR, exist_ok=True)
print(f"💾 保存先: {SAVE_DIR}")

# ── アカウント管理（SQLite） ────────────────────────
DB_PATH = os.path.join(BASE_DIR, 'users.db')

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "username TEXT UNIQUE NOT NULL,"
        "salt TEXT NOT NULL,"
        "hashed TEXT NOT NULL,"
        "created TEXT DEFAULT (datetime('now'))"
        ")"
    )
    con.commit(); con.close()

def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()

def create_user(username: str, password: str) -> dict:
    salt = secrets.token_hex(16)
    hashed = hash_password(password, salt)
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute('INSERT INTO users (username, salt, hashed) VALUES (?, ?, ?)', (username, salt, hashed))
        con.commit()
        return {'ok': True}
    except sqlite3.IntegrityError:
        return {'ok': False, 'error': 'そのユーザー名は既に使われています'}
    finally:
        try: con.close()
        except: pass

def verify_user(username: str, password: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute('SELECT salt, hashed FROM users WHERE username=?', (username,)).fetchone()
    con.close()
    if not row: return False
    return hash_password(password, row[0]) == row[1]

init_db()
# ────────────────────────────────────────────────────



app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

all_clients: list[WebSocket] = []
project_rooms: dict[str, dict] = defaultdict(dict)
project_state: dict[str, dict] = {}

# ── 非同期保存用（スレッドプール + デバウンス） ──
_persist_executor = ThreadPoolExecutor(max_workers=2)
_pending_saves: dict[str, asyncio.Task] = {}

def get_client_project(ws):
    for pname, room in project_rooms.items():
        if ws in room: return pname
    return None

def leave_all_rooms(ws):
    for room in project_rooms.values(): room.pop(ws, None)

def safe_name(name: str) -> str:
    return re.sub(r'[^\w\-_ ]', '', name).strip()[:40]

def default_state():
    return {"tasks": {}, "stickies": {}, "images": {}, "canvas": None, "texts": {}, "autoSave": False}

def to_dict(lst_or_dict, key="id"):
    if isinstance(lst_or_dict, list):
        return {item[key]: item for item in lst_or_dict if key in item}
    return lst_or_dict or {}

def load_state_from_file(project: str) -> dict:
    path = os.path.join(SAVE_DIR, f"{project}.json")
    if not os.path.exists(path):
        print(f"📂 {project}.json なし → 空で開始")
        return default_state()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        state = {
            "tasks":    to_dict(data.get("tasks",    [])),
            "stickies": to_dict(data.get("stickies", [])),
            "images":   to_dict(data.get("images",   [])),
            "canvas":   data.get("canvas"),
            "texts":    to_dict(data.get("texts",    [])),
            "autoSave": data.get("autoSave", False),
        }
        print(f"📂 {project}.json 読込: tasks={len(state['tasks'])}, stickies={len(state['stickies'])}, texts={len(state['texts'])}, canvas={'yes' if state['canvas'] else 'no'}")
        return state
    except Exception as e:
        print(f"⚠ 読込エラー ({project}): {e}")
        return default_state()

def _write_state_to_disk(project: str, data: dict):
    """同期書き込み（スレッドプール内で実行 → event loop をブロックしない）"""
    path = os.path.join(SAVE_DIR, f"{project}.json")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"💾 {project}.json 書込: tasks={len(data['tasks'])}, stickies={len(data['stickies'])}, canvas={'yes' if data['canvas'] else 'no'}")
    except Exception as e:
        print(f"⚠ 書込エラー ({project}): {e}")

async def _debounced_persist(project: str):
    """500ms 待ってまとめて書き込む。その間に追加操作が来たらキャンセルされる"""
    await asyncio.sleep(0.5)
    state = project_state.get(project)
    if state is None:
        return
    data = {
        "tasks":    list(state["tasks"].values()),
        "stickies": list(state["stickies"].values()),
        "images":   list(state["images"].values()),
        "canvas":   state.get("canvas"),
        "texts":    list(state.get("texts", {}).values()),
        "autoSave": state.get("autoSave", False),
    }
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_persist_executor, _write_state_to_disk, project, data)

def persist_state(project: str):
    """ノンブロッキング保存スケジューリング（デバウンス付き・即リターン）"""
    old = _pending_saves.get(project)
    if old and not old.done():
        old.cancel()
    _pending_saves[project] = asyncio.create_task(_debounced_persist(project))

def get_or_load_state(project: str) -> dict:
    if project not in project_state:
        project_state[project] = load_state_from_file(project)
    return project_state[project]


@app.post("/auth/register")
async def register(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return JSONResponse({"ok": False, "error": "ユーザー名とパスワードを入力してください"}, status_code=400)
    if len(username) < 2 or len(username) > 20:
        return JSONResponse({"ok": False, "error": "ユーザー名は2〜20文字にしてください"}, status_code=400)
    if not re.match(r'^[a-zA-Z0-9_\-぀-鿿]+$', username):
        return JSONResponse({"ok": False, "error": "使えない文字が含まれています"}, status_code=400)
    if len(password) < 4:
        return JSONResponse({"ok": False, "error": "パスワードは4文字以上にしてください"}, status_code=400)
    result = create_user(username, password)
    if not result['ok']:
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)

@app.post("/auth/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if verify_user(username, password):
        return JSONResponse({"ok": True, "username": username})
    return JSONResponse({"ok": False, "error": "ユーザー名またはパスワードが違います"}, status_code=401)

@app.get("/projects")
async def list_projects():
    return {"projects": sorted([f[:-5] for f in os.listdir(SAVE_DIR) if f.endswith('.json')])}

@app.post("/projects/{name}/save")
async def save_project(name: str, request: Request):
    n = safe_name(name)
    if not n: return JSONResponse({"ok": False}, status_code=400)
    new_data = await request.json()
    state = get_or_load_state(n)

    has_state = any(k in new_data for k in ("tasks", "stickies", "images", "canvas", "texts"))
    if has_state:
        if "tasks"    in new_data: state["tasks"]    = to_dict(new_data["tasks"])
        if "stickies" in new_data: state["stickies"] = to_dict(new_data["stickies"])
        if "images"   in new_data: state["images"]   = to_dict(new_data["images"])
        if "canvas"   in new_data: state["canvas"]   = new_data["canvas"]
        if "texts"    in new_data: state["texts"]    = to_dict(new_data["texts"])

    if "autoSave" in new_data:
        state["autoSave"] = new_data["autoSave"]

    # autoSaveのみの更新でも has_state でも同じデバウンス保存 → event loop をブロックしない
    persist_state(n)
    return {"ok": True, "name": n}

@app.get("/projects/{name}/load")
async def load_project_api(name: str):
    n = safe_name(name)
    state = get_or_load_state(n)
    return JSONResponse({
        "tasks":    list(state["tasks"].values()),
        "stickies": list(state["stickies"].values()),
        "images":   list(state["images"].values()),
        "canvas":   state.get("canvas"),
        "autoSave": state.get("autoSave", False),
    })

@app.delete("/projects/{name}")
async def delete_project_route(name: str):
    n = safe_name(name)
    # 保留中の保存タスクをキャンセル
    old = _pending_saves.get(n)
    if old and not old.done():
        old.cancel()
    path = os.path.join(SAVE_DIR, f"{n}.json")
    if os.path.exists(path): os.remove(path)
    project_state.pop(n, None); project_rooms.pop(n, None)
    return {"ok": True}

async def broadcast_to_project(project: str, message: dict, sender=None):
    data = json.dumps(message, ensure_ascii=False)
    dead = []
    for client in list(project_rooms.get(project, {}).keys()):
        if client is sender: continue
        try: await client.send_text(data)
        except: dead.append(client)
    for d in dead:
        project_rooms[project].pop(d, None)
        if d in all_clients: all_clients.remove(d)

async def broadcast_to_all(message: dict, sender=None):
    data = json.dumps(message, ensure_ascii=False)
    dead = []
    for client in all_clients:
        if client is sender: continue
        try: await client.send_text(data)
        except: dead.append(client)
    for d in dead: all_clients.remove(d)

async def _keepalive_ping(ws: WebSocket, interval: int = 20):
    """定期的にpingを送り、NATタイムアウト・プロキシ切断を防ぐ"""
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send_text(json.dumps({"type": "ping"}))
    except Exception:
        pass  # 切断済み → 自然終了

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    all_clients.append(websocket)
    print(f"✅ 接続: {len(all_clients)}人")
    ping_task = asyncio.create_task(_keepalive_ping(websocket))
    try:
        while True:
            raw = await websocket.receive_text()
            try: message = json.loads(raw)
            except: continue
            msg_type = message.get("type")
            payload  = message.get("payload", {})

            # keepalive応答は無視
            if msg_type == "pong":
                continue

            if msg_type == "join_project":
                project = safe_name(payload.get("project", "default"))
                leave_all_rooms(websocket)
                project_rooms[project][websocket] = True
                state = get_or_load_state(project)
                await websocket.send_text(json.dumps({"type": "sync_state", "payload": {
                    "tasks":    list(state["tasks"].values()),
                    "stickies": list(state["stickies"].values()),
                    "images":   list(state["images"].values()),
                    "canvas":   state.get("canvas"),
                    "texts":    list(state.get("texts", {}).values()),
                }}, ensure_ascii=False))
                print(f"📁 {project} 入室: tasks={len(state['tasks'])}")
                continue

            current_project = get_client_project(websocket) or "default"
            state = get_or_load_state(current_project)

            if   msg_type == "add_task":
                state["tasks"][payload["id"]] = payload; persist_state(current_project)
            elif msg_type == "move_task":
                if payload.get("id") in state["tasks"]:
                    state["tasks"][payload["id"]]["status"] = payload["status"]; persist_state(current_project)
            elif msg_type == "delete_task":
                state["tasks"].pop(payload.get("id"), None); persist_state(current_project)
            elif msg_type == "add_comment":
                if payload.get("taskId") in state["tasks"]:
                    state["tasks"][payload["taskId"]].setdefault("comments", []).append(payload["comment"]); persist_state(current_project)
            elif msg_type == "add_sticky":
                state["stickies"][payload["id"]] = payload; persist_state(current_project)
            elif msg_type == "move_sticky":
                if payload.get("id") in state["stickies"]:
                    state["stickies"][payload["id"]].update({"x": payload["x"], "y": payload["y"]}); persist_state(current_project)
            elif msg_type == "update_sticky":
                if payload.get("id") in state["stickies"]:
                    state["stickies"][payload["id"]]["text"] = payload["text"]; persist_state(current_project)
            elif msg_type == "resize_sticky":
                if payload.get("id") in state["stickies"]:
                    state["stickies"][payload["id"]].update({
                        "width": payload.get("width"), "height": payload.get("height"),
                        "x": payload.get("x"), "y": payload.get("y"),
                    }); persist_state(current_project)
            elif msg_type == "delete_sticky":
                state["stickies"].pop(payload.get("id"), None); persist_state(current_project)
            elif msg_type == "add_image":
                state["images"][payload["id"]] = payload; persist_state(current_project)
            elif msg_type == "move_image":
                if payload.get("id") in state["images"]:
                    state["images"][payload["id"]].update({"x": payload["x"], "y": payload["y"]}); persist_state(current_project)
            elif msg_type == "delete_image":
                state["images"].pop(payload.get("id"), None); persist_state(current_project)
            elif msg_type == "canvas_snapshot":
                state["canvas"] = payload.get("strokes"); persist_state(current_project); continue
            elif msg_type == "clear_drawing":
                state["canvas"] = None; persist_state(current_project)
            elif msg_type == "clear_all":
                state["stickies"].clear(); state["images"].clear()
                state["canvas"] = None; state.setdefault("texts", {}).clear()
                persist_state(current_project)
            elif msg_type == "add_text":
                state.setdefault("texts", {})[payload["id"]] = payload; persist_state(current_project)
            elif msg_type == "move_text":
                if payload.get("id") in state.get("texts", {}):
                    state["texts"][payload["id"]].update({"x": payload["x"], "y": payload["y"]}); persist_state(current_project)
            elif msg_type == "update_text":
                if payload.get("id") in state.get("texts", {}):
                    state["texts"][payload["id"]]["text"] = payload["text"]; persist_state(current_project)
            elif msg_type == "delete_text":
                state.setdefault("texts", {}).pop(payload.get("id"), None); persist_state(current_project)

            if msg_type in (
                "add_task", "move_task", "delete_task", "add_comment",
                "draw_stroke", "clear_drawing",
                "add_sticky", "move_sticky", "update_sticky", "resize_sticky", "delete_sticky",
                "add_image", "move_image", "delete_image",
                "clear_all", "set_template",
                "add_text", "move_text", "update_text", "delete_text",
            ):
                await broadcast_to_project(current_project, message, sender=websocket)
            elif msg_type == "project_list_changed":
                await broadcast_to_all(message, sender=websocket)
            elif msg_type != "canvas_snapshot":
                print(f"⚠ 未知: {msg_type}")

    except WebSocketDisconnect:
        ping_task.cancel()
        leave_all_rooms(websocket)
        if websocket in all_clients: all_clients.remove(websocket)
        print(f"🔌 切断: {len(all_clients)}人")


# ── フロントエンドの配信 ────────────────────────────
# API・WebSocket と同じオリジンから HTML を配信する（ルートの最後に登録すること）。
# これで接続先のアドレスをHTMLに書かずに済み、どの環境でも `uvicorn main:app` だけで動く。
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
