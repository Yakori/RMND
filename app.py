import os, sys, json, sqlite3, asyncio, time, threading, traceback, hashlib, secrets, shutil
from collections import deque
from contextlib import asynccontextmanager

import httpx, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

FROZEN = getattr(sys, "frozen", False)
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if FROZEN:
    BASE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "FaceitPanel")
    os.makedirs(BASE, exist_ok=True)
    if not os.path.exists(os.path.join(BASE, "config.json")):
        shutil.copy(os.path.join(RES, "config.json"), os.path.join(BASE, "config.json"))
else:
    BASE = RES
os.chdir(BASE)
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open("panel.log", "a", encoding="utf-8", buffering=1)

CFG_PATH = os.path.join(BASE, "config.json")
CFG = json.load(open(CFG_PATH, encoding="utf-8"))
cfg_lock = threading.Lock()
MULT    = float(CFG.get("price_mult", 0.75))
POLL    = int(CFG.get("poll_seconds", 120))
STEAM_DELAY = float(CFG.get("steam_delay", 1))
BATCH   = int(CFG.get("batch_size", 100))
BATCH_PAUSE = float(CFG.get("batch_pause", 15))
HOST    = os.environ.get("HOST", CFG.get("host", "0.0.0.0"))
PORT    = int(os.environ.get("PORT", CFG.get("port", 8000)))
API     = "https://open.faceit.com/data/v4"
COOKIE  = "fp_session"
AUTH_SECRET = secrets.token_hex(16)

# ── БД ──────────────────────────────────────────────────────────────────────
db = sqlite3.connect("panel.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db_lock = threading.Lock()

db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  username     TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role         TEXT NOT NULL DEFAULT 'worker',
  faceit_key   TEXT NOT NULL DEFAULT '',
  faceit_nick  TEXT NOT NULL DEFAULT '',
  min_usd      REAL NOT NULL DEFAULT 300,
  created_at   REAL NOT NULL,
  last_login   REAL
);
CREATE TABLE IF NOT EXISTS players(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id  INTEGER NOT NULL,
  nick     TEXT NOT NULL,
  steam_id TEXT,
  usd      REAL,
  status   TEXT DEFAULT 'new',
  added    REAL,
  UNIQUE(user_id, nick)
);
CREATE TABLE IF NOT EXISTS seen(
  user_id  INTEGER NOT NULL,
  match_id TEXT NOT NULL,
  PRIMARY KEY(user_id, match_id)
);
CREATE TABLE IF NOT EXISTS login_log(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL,
  ip         TEXT,
  at         REAL NOT NULL,
  ok         INTEGER NOT NULL
);
""")
db.commit()

# Миграция: добавляем колонки если их ещё нет (старая БД)
for col, ddl in [
    ("role",          "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'worker'"),
    ("faceit_key",    "ALTER TABLE users ADD COLUMN faceit_key TEXT NOT NULL DEFAULT ''"),
    ("faceit_nick",   "ALTER TABLE users ADD COLUMN faceit_nick TEXT NOT NULL DEFAULT ''"),
    ("min_usd",       "ALTER TABLE users ADD COLUMN min_usd REAL NOT NULL DEFAULT 300"),
    ("last_login",    "ALTER TABLE users ADD COLUMN last_login REAL"),
]:
    try:
        db.execute(ddl)
    except sqlite3.OperationalError:
        pass  # колонка уже есть

# Миграция таблицы players: добавляем user_id если старая схема
existing_cols = {r[1] for r in db.execute("PRAGMA table_info(players)").fetchall()}
if "user_id" not in existing_cols:
    # старая таблица без user_id — пересоздаём
    db.executescript("""
    ALTER TABLE players RENAME TO players_old;
    CREATE TABLE players(
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id  INTEGER NOT NULL DEFAULT 1,
      nick     TEXT NOT NULL,
      steam_id TEXT,
      usd      REAL,
      status   TEXT DEFAULT 'new',
      added    REAL,
      UNIQUE(user_id, nick)
    );
    INSERT INTO players(user_id, nick, steam_id, usd, status, added)
      SELECT 1, nick, steam_id, usd, status, added FROM players_old;
    DROP TABLE players_old;
    """)

# Миграция seen: добавляем user_id если старая схема
seen_cols = {r[1] for r in db.execute("PRAGMA table_info(seen)").fetchall()}
if "user_id" not in seen_cols:
    db.executescript("""
    ALTER TABLE seen RENAME TO seen_old;
    CREATE TABLE seen(
      user_id  INTEGER NOT NULL DEFAULT 1,
      match_id TEXT NOT NULL,
      PRIMARY KEY(user_id, match_id)
    );
    INSERT INTO seen(user_id, match_id) SELECT 1, match_id FROM seen_old;
    DROP TABLE seen_old;
    """)

db.commit()

# глобальные состояния ботов: user_id -> dict
bots: dict[int, dict] = {}
bots_lock = threading.Lock()
prices: dict[str, float] = {}
prices_at: float = 0.0
prices_lock = threading.Lock()


# ── Вспомогательные ─────────────────────────────────────────────────────────
def log_bot(uid: int, msg: str):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    with bots_lock:
        if uid in bots:
            bots[uid]["events"].appendleft(line)
    print(f"[user {uid}] {line}", flush=True)


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def session_token(username: str) -> str:
    return hashlib.sha256(f"{username}:{AUTH_SECRET}".encode()).hexdigest()


def get_user(request: Request):
    """Возвращает Row пользователя или None."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with db_lock:
        users = db.execute("SELECT * FROM users").fetchall()
    for u in users:
        if session_token(u["username"]) == token:
            return u
    return None


def require_admin(request: Request):
    u = get_user(request)
    if u and u["role"] == "admin":
        return u
    return None


# ── Цены (loot.farm — публичный API без авторизации) ────────────────────────
class RateLimited(Exception):
    pass


async def ensure_prices(c: httpx.AsyncClient):
    global prices_at
    with prices_lock:
        if prices and time.time() - prices_at < 3600:
            return
    import json as _j, gzip as _gz

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as sc:
        r = await sc.get(
            "https://loot.farm/fullprice.json",
            headers={"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0"},
        )

    if r.status_code != 200:
        raise RuntimeError(f"loot.farm вернул {r.status_code}")

    raw = r.content
    if raw[:2] == b'\x1f\x8b':
        raw = _gz.decompress(raw)

    data = _j.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError(f"loot.farm: неожиданный формат ответа")

    new_prices: dict[str, float] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        price_cents = item.get("price")  # цена в центах USD
        if name and price_cents and price_cents > 0:
            new_prices[name] = round(price_cents / 100 * MULT, 2)

    if len(new_prices) < 1000:
        raise RuntimeError(f"loot.farm: слишком мало предметов ({len(new_prices)})")

    with prices_lock:
        prices.clear()
        prices.update(new_prices)
        prices_at = time.time()
    print(f"цены обновлены (loot.farm): {len(prices)} предметов", flush=True)


async def inventory_usd(c: httpx.AsyncClient, steam_id: str) -> float | None:
    r = await c.get(f"https://steamcommunity.com/inventory/{steam_id}/730/2",
                    params={"l": "english", "count": 2000})
    if r.status_code == 429:
        raise RateLimited()
    if r.status_code != 200:
        return None
    d = r.json()
    if not isinstance(d, dict):
        return None
    with prices_lock:
        names = {x["classid"]: x["market_hash_name"] for x in d.get("descriptions", [])}
        return sum(prices.get(names.get(a["classid"]), 0) for a in d.get("assets", []))


# ── Бот одного пользователя ──────────────────────────────────────────────────
async def poll_user(uid: int):
    checks = 0
    checked: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c, \
               httpx.AsyncClient(timeout=30, follow_redirects=True) as fc:
        while True:
            try:
                with db_lock:
                    row = db.execute(
                        "SELECT faceit_key, faceit_nick, min_usd FROM users WHERE id=?", (uid,)
                    ).fetchone()
                if not row or not row["faceit_key"] or not row["faceit_nick"]:
                    with bots_lock:
                        bots[uid]["state"] = "нет настроек"
                    await asyncio.sleep(POLL)
                    continue

                key  = row["faceit_key"]
                nick = row["faceit_nick"]
                min_usd = float(row["min_usd"])

                fc.headers["Authorization"] = f"Bearer {key}"
                await ensure_prices(c)

                with bots_lock:
                    bots[uid]["state"] = "смотрю матчи"

                r = await fc.get(f"{API}/players", params={"nickname": nick})
                if r.status_code != 200:
                    raise RuntimeError(f"FACEIT {r.status_code}: проверь ключ/ник")
                me_id = r.json()["player_id"]

                items = []
                for game in ("cs2", "csgo"):
                    r = await fc.get(f"{API}/players/{me_id}/history",
                                     params={"game": game, "limit": 20})
                    items = r.json().get("items", []) if r.status_code == 200 else []
                    if items:
                        break

                with db_lock:
                    new = [m for m in items if not db.execute(
                        "SELECT 1 FROM seen WHERE user_id=? AND match_id=?",
                        (uid, m["match_id"])).fetchone()]
                log_bot(uid, f"матчей: {len(items)}, новых: {len(new)}")

                for m in new:
                    mid = m["match_id"]
                    r2 = await fc.get(f"{API}/matches/{mid}")
                    if r2.status_code != 200:
                        continue
                    for team in r2.json().get("teams", {}).values():
                        for p in team.get("roster", []):
                            pnick, sid = p.get("nickname"), p.get("game_player_id")
                            if not pnick or not sid or pnick == nick:
                                continue
                            with db_lock:
                                already = db.execute(
                                    "SELECT 1 FROM players WHERE user_id=? AND nick=?",
                                    (uid, pnick)).fetchone()
                            if already:
                                continue
                            if time.time() - checked.get(pnick, 0) < 6 * 3600:
                                continue
                            with bots_lock:
                                bots[uid]["state"] = f"проверяю {pnick}"
                            usd = "retry"
                            for _ in range(3):
                                try:
                                    usd = await inventory_usd(c, sid)
                                    break
                                except RateLimited:
                                    log_bot(uid, "Steam просит подождать, жду 60 сек")
                                    await asyncio.sleep(60)
                            if usd == "retry":
                                continue
                            checked[pnick] = time.time()
                            if usd is None:
                                log_bot(uid, f"{pnick}: инвентарь закрыт")
                            elif usd >= min_usd:
                                with db_lock:
                                    db.execute(
                                        "INSERT OR IGNORE INTO players"
                                        "(user_id,nick,steam_id,usd,added) VALUES(?,?,?,?,?)",
                                        (uid, pnick, sid, round(usd), time.time()))
                                    db.commit()
                                log_bot(uid, f"{pnick}: ${usd:.0f} добавлен")
                            else:
                                log_bot(uid, f"{pnick}: ${usd:.0f} мало")
                            checks += 1
                            if checks % BATCH == 0:
                                await asyncio.sleep(BATCH_PAUSE)
                            else:
                                await asyncio.sleep(STEAM_DELAY)

                    with db_lock:
                        db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (uid, mid))
                        db.commit()

                with bots_lock:
                    bots[uid]["state"] = "жду новых матчей"

            except asyncio.CancelledError:
                raise
            except Exception as e:
                with bots_lock:
                    bots[uid]["state"] = "ошибка"
                log_bot(uid, f"ОШИБКА: {e}")
                traceback.print_exc()

            await asyncio.sleep(POLL)


async def ensure_bot(uid: int):
    with bots_lock:
        if uid in bots and not bots[uid]["task"].done():
            return
        task = asyncio.create_task(poll_user(uid))
        bots[uid] = {"task": task, "state": "запускаюсь", "events": deque(maxlen=40)}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # запускаем боты для всех существующих пользователей
    with db_lock:
        uids = [r["id"] for r in db.execute("SELECT id FROM users").fetchall()]
    for uid in uids:
        await ensure_bot(uid)
    print(f"{time.strftime('%H:%M:%S')}  панель: http://127.0.0.1:{PORT}", flush=True)
    yield
    with bots_lock:
        for b in bots.values():
            b["task"].cancel()
    await asyncio.gather(*[b["task"] for b in bots.values()], return_exceptions=True)


app = FastAPI(title="FACEIT панель", lifespan=lifespan)

# ── CSS для страниц авторизации ───────────────────────────────────────────────
AUTH_CSS = (
    ":root{--bg:#0a0c0f;--p1:#11141a;--p2:#171b22;--line:#232a33;--fg:#eaeef2;--mut:#8994a1;"
    "--blue:#8ea7c0;--blue2:#5f7a96;--red:#ef6b72;color-scheme:dark}"
    "*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:grid;place-items:center;"
    "background:radial-gradient(900px 500px at 85%% -10%%,#1b2430 0,transparent 60%%),#0a0c0f;"
    "color:#eaeef2;font:15px/1.4 'Segoe UI Variable','Segoe UI',system-ui,sans-serif}"
    ".card{width:min(420px,94vw);background:#11141a;border:1px solid #232a33;border-radius:20px;padding:32px}"
    ".logo{display:flex;align-items:center;gap:10px;margin-bottom:24px}"
    ".logo i{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#8ea7c0,#5f7a96);"
    "display:grid;place-items:center;color:#0a0c0f;font-style:normal;font-weight:700;font-size:17px}"
    ".logo span{font:600 20px/1 Bahnschrift,'Segoe UI',sans-serif}"
    "h2{font:600 22px/1.2 Bahnschrift,'Segoe UI',sans-serif;margin:0 0 6px}"
    ".sub{color:#8994a1;font-size:14px;margin:0 0 22px}"
    "label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:#8994a1;margin-bottom:14px}"
    "input{width:100%%;padding:12px 14px;border-radius:12px;border:1px solid #232a33;"
    "background:#171b22;color:#eaeef2;font:inherit;outline:none;transition:border-color .15s}"
    "input:focus{border-color:#5f7a96}"
    ".btn{display:block;width:100%%;margin-top:8px;padding:12px;border:0;border-radius:12px;"
    "font:600 15px/1 inherit;cursor:pointer;background:linear-gradient(135deg,#8ea7c0,#5f7a96);"
    "color:#0a0c0f;transition:opacity .15s}"
    ".btn:hover{opacity:.88}"
    ".err{color:#ef6b72;font-size:13px;min-height:1.2em;margin-top:10px}"
    ".link{text-align:center;margin-top:18px;font-size:13px;color:#8994a1}"
    ".link a{color:#8ea7c0;text-decoration:none}"
    ".link a:hover{text-decoration:underline}"
)


def login_page(err=""):
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Вход — FACEIT панель</title>'
        '<style>' + AUTH_CSS + '</style></head><body>'
        '<div class="card">'
        '<div class="logo"><i>F</i><span>Faceit панель</span></div>'
        '<h2>Вход</h2><p class="sub">Введи логин и пароль.</p>'
        '<form method="post" action="/login">'
        '<label>Логин<input type="text" name="username" placeholder="username" autofocus required autocomplete="username"></label>'
        '<label>Пароль<input type="password" name="password" placeholder="••••••••" required autocomplete="current-password"></label>'
        '<button class="btn" type="submit">Войти</button>'
        f'<div class="err">{err}</div>'
        '</form>'
        '<div class="link">Нет аккаунта? <a href="/register">Зарегистрироваться</a></div>'
        '</div></body></html>'
    )


def register_page(err=""):
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Регистрация — FACEIT панель</title>'
        '<style>' + AUTH_CSS + '</style></head><body>'
        '<div class="card">'
        '<div class="logo"><i>F</i><span>Faceit панель</span></div>'
        '<h2>Регистрация</h2><p class="sub">Создай аккаунт для доступа к панели.</p>'
        '<form method="post" action="/register">'
        '<label>Логин<input type="text" name="username" placeholder="username" autofocus required autocomplete="username" minlength="3" maxlength="32"></label>'
        '<label>Пароль<input type="password" name="password" placeholder="••••••••" required autocomplete="new-password" minlength="4"></label>'
        '<label>Повтори пароль<input type="password" name="password2" placeholder="••••••••" required autocomplete="new-password" minlength="4"></label>'
        '<button class="btn" type="submit">Создать аккаунт</button>'
        f'<div class="err">{err}</div>'
        '</form>'
        '<div class="link">Уже есть аккаунт? <a href="/login">Войти</a></div>'
        '</div></body></html>'
    )


# ── Middleware ────────────────────────────────────────────────────────────────
@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/register", "/health"):
        return await call_next(request)
    if get_user(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_get():
    return login_page()


@app.post("/login")
async def login_post(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    ip = request.client.host if request.client else "?"
    if not username or not password:
        return HTMLResponse(login_page("Заполни все поля"), status_code=400)
    with db_lock:
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = row and row["password_hash"] == hash_password(password)
    with db_lock:
        db.execute("INSERT INTO login_log(username,ip,at,ok) VALUES(?,?,?,?)",
                   (username, ip, time.time(), 1 if ok else 0))
        if ok:
            db.execute("UPDATE users SET last_login=? WHERE username=?", (time.time(), username))
        db.commit()
    if not ok:
        return HTMLResponse(login_page("Неверный логин или пароль"), status_code=401)
    # запустить бота если ещё не запущен
    await ensure_bot(row["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, session_token(username), httponly=True, samesite="lax")
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_get():
    return register_page()


@app.post("/register")
async def register_post(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    password2 = str(form.get("password2", ""))
    if not username or not password:
        return HTMLResponse(register_page("Заполни все поля"), status_code=400)
    if len(username) < 3:
        return HTMLResponse(register_page("Логин минимум 3 символа"), status_code=400)
    if len(password) < 4:
        return HTMLResponse(register_page("Пароль минимум 4 символа"), status_code=400)
    if password != password2:
        return HTMLResponse(register_page("Пароли не совпадают"), status_code=400)
    # первый пользователь = admin
    with db_lock:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    role = "admin" if count == 0 else "worker"
    try:
        with db_lock:
            db.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                (username, hash_password(password), role, time.time()))
            uid = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            db.commit()
    except sqlite3.IntegrityError:
        return HTMLResponse(register_page("Такой логин уже занят"), status_code=400)
    await ensure_bot(uid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, session_token(username), httponly=True, samesite="lax")
    return resp


@app.api_route("/logout", methods=["GET", "POST"])
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ── Основные страницы ─────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(RES, "index.html"))


# ── API: игроки ───────────────────────────────────────────────────────────────
@app.get("/api/players")
def api_players(request: Request):
    u = get_user(request)
    with db_lock:
        rows = db.execute(
            "SELECT nick,usd,status,steam_id FROM players WHERE user_id=? ORDER BY added DESC",
            (u["id"],)).fetchall()
    return [{"nick": r["nick"], "usd": r["usd"], "status": r["status"], "steam": r["steam_id"]}
            for r in rows]


class St(BaseModel):
    status: str


@app.post("/api/players/{nick}")
def api_set_status(nick: str, b: St, request: Request):
    u = get_user(request)
    if b.status in ("new", "sent", "accepted"):
        with db_lock:
            db.execute("UPDATE players SET status=? WHERE user_id=? AND nick=?",
                       (b.status, u["id"], nick))
            db.commit()


@app.delete("/api/players/{nick}")
def api_delete_player(nick: str, request: Request):
    u = get_user(request)
    with db_lock:
        db.execute("DELETE FROM players WHERE user_id=? AND nick=?", (u["id"], nick))
        db.commit()


# ── API: статус бота ──────────────────────────────────────────────────────────
@app.get("/api/status")
def api_status(request: Request):
    u = get_user(request)
    uid = u["id"]
    with bots_lock:
        bot = bots.get(uid, {})
        state = bot.get("state", "не запущен")
        events = list(bot.get("events", []))
    return {
        "state": state,
        "events": events,
        "username": u["username"],
        "role": u["role"],
    }


# ── API: настройки (per-user) ─────────────────────────────────────────────────
class SettingsIn(BaseModel):
    faceit_key: str
    faceit_nick: str
    min_usd: float


@app.get("/api/settings")
def api_get_settings(request: Request):
    u = get_user(request)
    return {
        "faceit_key": u["faceit_key"],
        "faceit_nick": u["faceit_nick"],
        "min_usd": u["min_usd"],
    }


@app.post("/api/settings")
def api_save_settings(b: SettingsIn, request: Request):
    u = get_user(request)
    key = b.faceit_key.strip()
    nick = b.faceit_nick.strip()
    usd = float(b.min_usd)
    if not key:
        return JSONResponse({"error": "укажи FACEIT API ключ"}, status_code=400)
    if not nick:
        return JSONResponse({"error": "укажи FACEIT ник"}, status_code=400)
    if usd < 0:
        return JSONResponse({"error": "минимальная цена не может быть меньше 0"}, status_code=400)
    with db_lock:
        db.execute(
            "UPDATE users SET faceit_key=?, faceit_nick=?, min_usd=? WHERE id=?",
            (key, nick, usd, u["id"]))
        db.execute("DELETE FROM players WHERE user_id=? AND usd < ?", (u["id"], usd))
        db.commit()
    return {"ok": True, "faceit_key": key, "faceit_nick": nick, "min_usd": usd}


# ── API: admin — список пользователей ─────────────────────────────────────────
@app.get("/api/admin/users")
def api_admin_users(request: Request):
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    with db_lock:
        rows = db.execute(
            "SELECT id,username,role,faceit_nick,created_at,last_login FROM users ORDER BY id"
        ).fetchall()
    result = []
    for r in rows:
        with bots_lock:
            state = bots.get(r["id"], {}).get("state", "—")
        result.append({
            "id": r["id"],
            "username": r["username"],
            "role": r["role"],
            "faceit_nick": r["faceit_nick"],
            "state": state,
            "created_at": r["created_at"],
            "last_login": r["last_login"],
        })
    return result


@app.delete("/api/admin/users/{uid}")
def api_admin_delete_user(uid: int, request: Request):
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    me = require_admin(request)
    if me["id"] == uid:
        return JSONResponse({"error": "нельзя удалить себя"}, status_code=400)
    with bots_lock:
        if uid in bots:
            bots[uid]["task"].cancel()
            del bots[uid]
    with db_lock:
        db.execute("DELETE FROM players WHERE user_id=?", (uid,))
        db.execute("DELETE FROM seen WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
    return {"ok": True}


class RoleIn(BaseModel):
    role: str


@app.post("/api/admin/users/{uid}/role")
def api_admin_set_role(uid: int, b: RoleIn, request: Request):
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if b.role not in ("admin", "worker"):
        return JSONResponse({"error": "неверная роль"}, status_code=400)
    with db_lock:
        db.execute("UPDATE users SET role=? WHERE id=?", (b.role, uid))
        db.commit()
    return {"ok": True}


# ── API: admin — лог входов ───────────────────────────────────────────────────
@app.get("/api/admin/login_log")
def api_admin_login_log(request: Request):
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    with db_lock:
        rows = db.execute(
            "SELECT username,ip,at,ok FROM login_log ORDER BY at DESC LIMIT 100"
        ).fetchall()
    return [{"username": r["username"], "ip": r["ip"],
             "at": r["at"], "ok": bool(r["ok"])} for r in rows]


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
