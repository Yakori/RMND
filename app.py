import os, sys, json, sqlite3, asyncio, time, threading, traceback, hashlib, secrets, shutil
from collections import deque
from contextlib import asynccontextmanager

import httpx, uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

FROZEN = getattr(sys, "frozen", False)
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
def _write_default_config(path: str):
    with open(path, "w", encoding="utf-8") as _f:
        json.dump({"faceit_key": "", "faceit_nick": "", "min_usd": 300,
                   "price_mult": 0.75, "poll_seconds": 120, "steam_delay": 1,
                   "batch_size": 100, "batch_pause": 15,
                   "host": "0.0.0.0", "port": 8000}, _f, indent=2)


def _copy_example_config(dest: str) -> bool:
    for example in (os.path.join(BASE, "config.example.json"), os.path.join(RES, "config.example.json")):
        if os.path.exists(example):
            shutil.copy(example, dest)
            return True
    return False


if FROZEN:
    BASE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "FaceitPanel")
    os.makedirs(BASE, exist_ok=True)
else:
    BASE = RES
os.chdir(BASE)
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open("panel.log", "a", encoding="utf-8", buffering=1)

CFG_PATH = os.path.join(BASE, "config.json")
if not os.path.exists(CFG_PATH):
    if not _copy_example_config(CFG_PATH):
        _write_default_config(CFG_PATH)
    print("Создан config.json из шаблона — зайди в настройки панели", flush=True)
try:
    with open(CFG_PATH, encoding="utf-8") as _f:
        CFG = json.load(_f)
except json.JSONDecodeError as _e:
    sys.exit(f"config.json повреждён ({CFG_PATH}): {_e}")
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
  faceit_bearer TEXT NOT NULL DEFAULT '',
  faceit_id    TEXT NOT NULL DEFAULT '',
  created_at   REAL NOT NULL,
  last_login   REAL
);
CREATE TABLE IF NOT EXISTS players(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  nick       TEXT NOT NULL,
  steam_id   TEXT,
  usd        REAL,
  status     TEXT DEFAULT 'new',
  added      REAL,
  faceit_pid TEXT NOT NULL DEFAULT '',
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
    ("role",           "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'worker'"),
    ("faceit_key",     "ALTER TABLE users ADD COLUMN faceit_key TEXT NOT NULL DEFAULT ''"),
    ("faceit_nick",    "ALTER TABLE users ADD COLUMN faceit_nick TEXT NOT NULL DEFAULT ''"),
    ("min_usd",        "ALTER TABLE users ADD COLUMN min_usd REAL NOT NULL DEFAULT 300"),
    ("last_login",     "ALTER TABLE users ADD COLUMN last_login REAL"),
    ("faceit_bearer",  "ALTER TABLE users ADD COLUMN faceit_bearer TEXT NOT NULL DEFAULT ''"),
    ("faceit_id",      "ALTER TABLE users ADD COLUMN faceit_id TEXT NOT NULL DEFAULT ''"),
]:
    try:
        db.execute(ddl)
    except sqlite3.OperationalError as err:
        if "duplicate column" not in str(err).lower():
            raise

# Миграция таблицы players: добавляем user_id если старая схема
existing_cols = {r[1] for r in db.execute("PRAGMA table_info(players)").fetchall()}
if "user_id" not in existing_cols:
    # старая таблица без user_id — пересоздаём
    db.executescript("""
    ALTER TABLE players RENAME TO players_old;
    CREATE TABLE players(
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id    INTEGER NOT NULL DEFAULT 1,
      nick       TEXT NOT NULL,
      steam_id   TEXT,
      usd        REAL,
      status     TEXT DEFAULT 'new',
      added      REAL,
      faceit_pid TEXT NOT NULL DEFAULT '',
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

player_cols = {r[1] for r in db.execute("PRAGMA table_info(players)").fetchall()}
if "faceit_pid" not in player_cols:
    db.execute("ALTER TABLE players ADD COLUMN faceit_pid TEXT NOT NULL DEFAULT ''")

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
        bot = bots.get(uid)
        if bot is not None:
            bot["events"].appendleft(line)
    print(f"[user {uid}] {line}", flush=True)


def set_bot_state(uid: int, state: str):
    with bots_lock:
        bot = bots.get(uid)
        if bot is not None:
            bot["state"] = state


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def session_token(username: str) -> str:
    return hashlib.sha256(f"{username}:{AUTH_SECRET}".encode()).hexdigest()


def get_user(request: Request):
    """Возвращает Row пользователя или None.
    Заголовок X-Panel-Token нужен расширению: cookie SameSite=Lax
    не уходит в cross-origin POST с chrome-extension://."""
    token = request.cookies.get(COOKIE) or request.headers.get("x-panel-token")
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


async def ensure_prices(_c: httpx.AsyncClient):
    global prices_at
    with prices_lock:
        if prices and time.time() - prices_at < 3600:
            return

    # Используем urllib напрямую — без авто-сжатия httpx
    import json as _j, gzip as _gz, urllib.request as _ur

    def _fetch() -> bytes:
        req = _ur.Request(
            "https://market.csgo.com/api/v2/prices/USD.json",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"},
        )
        with _ur.urlopen(req, timeout=60) as resp:
            return resp.read()

    raw = await asyncio.get_event_loop().run_in_executor(None, _fetch)

    if raw[:2] == b'\x1f\x8b':
        raw = _gz.decompress(raw)

    data = _j.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"market.csgo.com: неожиданный ответ: {str(data)[:120]}")

    items = data.get("items", [])
    new_prices: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("market_hash_name")
        price = item.get("price")
        if name and price:
            new_prices[name] = round(float(price) * MULT, 2)

    if len(new_prices) < 1000:
        raise RuntimeError(f"market.csgo.com: слишком мало предметов ({len(new_prices)})")

    with prices_lock:
        prices.clear()
        prices.update(new_prices)
        prices_at = time.time()
    print(f"цены обновлены (market.csgo.com): {len(prices)} предметов", flush=True)


def _steam_names(descriptions) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    by_pair: dict[tuple[str, str], str] = {}
    by_class: dict[str, str] = {}
    for item in descriptions or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("classid") or "")
        name = item.get("market_hash_name") or ""
        if not cid or not name:
            continue
        iid = str(item.get("instanceid") or "0")
        by_pair[(cid, iid)] = name
        by_class.setdefault(cid, name)
    return by_pair, by_class


async def inventory_usd(c: httpx.AsyncClient, steam_id: str) -> float | None:
    with prices_lock:
        price_map = dict(prices)
    total = 0.0
    start_assetid = None
    for _ in range(8):
        params = {"l": "english", "count": 2000}
        if start_assetid:
            params["start_assetid"] = start_assetid
        r = await c.get(f"https://steamcommunity.com/inventory/{steam_id}/730/2", params=params)
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code != 200:
            return None
        try:
            d = r.json()
        except Exception:
            return None
        if not isinstance(d, dict) or d.get("success") is False:
            return None
        by_pair, by_class = _steam_names(d.get("descriptions") or [])
        for asset in d.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            cid = str(asset.get("classid") or "")
            iid = str(asset.get("instanceid") or "0")
            name = by_pair.get((cid, iid)) or by_class.get(cid)
            if not name:
                continue
            try:
                amount = int(asset.get("amount") or 1)
            except (TypeError, ValueError):
                amount = 1
            total += price_map.get(name, 0) * max(amount, 1)
        if not d.get("more_items") or not d.get("last_assetid"):
            break
        nxt = str(d["last_assetid"])
        if nxt == start_assetid:
            break
        start_assetid = nxt
    return total


# ── FACEIT friend requests ────────────────────────────────────────────────────
FACEIT_API2 = "https://api.faceit.com"


async def faceit_send_friend(api_key: str, bearer: str, my_faceit_id: str, target_nick: str) -> tuple[bool, str, str]:
    """Заявка в друзья. Open Data API — ключ сервера, friend API — сессионный bearer.
    Возвращает (успех, сообщение, faceit_id цели)."""
    if not api_key:
        return False, "нет FACEIT API ключа", ""
    if not bearer:
        return False, "bearer не указан в настройках", ""
    if not my_faceit_id:
        return False, "нет faceit_id — сохрани ник и API ключ", ""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/players", params={"nickname": target_nick},
                        headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            return False, f"не найден игрок {target_nick}", ""
        try:
            target_id = r.json().get("player_id") or ""
        except Exception:
            return False, "некорректный ответ FACEIT", ""
        if not target_id:
            return False, "не удалось получить player_id", ""

        r2 = await c.post(
            f"{FACEIT_API2}/friend-requests/v1/users/{my_faceit_id}/requests",
            json={"targetUserId": target_id},
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        )
        if r2.status_code in (200, 201, 204):
            return True, "заявка отправлена", target_id
        return False, f"ошибка {r2.status_code}: {r2.text[:120]}", target_id


async def faceit_check_friends(bearer: str, my_faceit_id: str) -> set[str]:
    """Возвращает множество faceit_id друзей."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{FACEIT_API2}/friend-requests/v1/users/{my_faceit_id}/friends",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if r.status_code != 200:
            return set()
        data = r.json()
        # ответ может быть списком или {"friends": [...]}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("friends", data.get("items", []))
        else:
            items = []
        ids = set()
        for item in items or []:
            if isinstance(item, str) and item:
                ids.add(item)
            elif isinstance(item, dict):
                pid = item.get("userId") or item.get("id") or item.get("player_id") or ""
                if pid:
                    ids.add(pid)
        return ids


async def get_my_faceit_id(api_key: str, nick: str) -> str:
    """player_id по нику через Open Data API (серверный ключ, не сессионный JWT)."""
    if not api_key or not nick:
        return ""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/players", params={"nickname": nick},
                        headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            return ""
        try:
            return r.json().get("player_id") or ""
        except Exception:
            return ""


# ── Бот одного пользователя ──────────────────────────────────────────────────
async def poll_user(uid: int):
    checks = 0
    checked: dict[str, float] = {}
    last_friends_check = 0.0

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c, \
               httpx.AsyncClient(timeout=30, follow_redirects=True) as fc:
        while True:
            try:
                with db_lock:
                    row = db.execute(
                        "SELECT faceit_key, faceit_nick, min_usd, faceit_bearer, faceit_id FROM users WHERE id=?",
                        (uid,),
                    ).fetchone()
                if not row or not row["faceit_key"] or not row["faceit_nick"]:
                    set_bot_state(uid, "нет настроек")
                    await asyncio.sleep(POLL)
                    continue

                key = row["faceit_key"]
                nick = row["faceit_nick"]
                min_usd = float(row["min_usd"])
                bearer = row["faceit_bearer"] or ""
                my_fid = row["faceit_id"] or ""

                fc.headers["Authorization"] = f"Bearer {key}"
                await ensure_prices(c)

                me = await fc.get(f"{API}/players", params={"nickname": nick})
                if me.status_code != 200:
                    raise RuntimeError(f"FACEIT {me.status_code}: проверь ключ/ник")
                try:
                    me_id = me.json().get("player_id") or ""
                except Exception:
                    me_id = ""
                if not me_id:
                    raise RuntimeError("FACEIT не вернул player_id")
                if my_fid != me_id:
                    my_fid = me_id
                    with db_lock:
                        db.execute("UPDATE users SET faceit_id=? WHERE id=?", (my_fid, uid))
                        db.commit()

                if bearer and my_fid and time.time() - last_friends_check > 120:
                    last_friends_check = time.time()
                    try:
                        friend_ids = await faceit_check_friends(bearer, my_fid)
                        if friend_ids:
                            with db_lock:
                                sent = db.execute(
                                    "SELECT nick, faceit_pid FROM players WHERE user_id=? AND status='sent'",
                                    (uid,),
                                ).fetchall()
                            for s in sent:
                                pnick = s["nick"]
                                pid = s["faceit_pid"] or ""
                                if not pid:
                                    pr = await fc.get(f"{API}/players", params={"nickname": pnick})
                                    if pr.status_code != 200:
                                        continue
                                    try:
                                        pid = pr.json().get("player_id") or ""
                                    except Exception:
                                        pid = ""
                                    if pid:
                                        with db_lock:
                                            db.execute(
                                                "UPDATE players SET faceit_pid=? WHERE user_id=? AND nick=?",
                                                (pid, uid, pnick),
                                            )
                                            db.commit()
                                if pid and pid in friend_ids:
                                    with db_lock:
                                        db.execute(
                                            "UPDATE players SET status='accepted' WHERE user_id=? AND nick=?",
                                            (uid, pnick),
                                        )
                                        db.commit()
                                    log_bot(uid, f"{pnick}: принял заявку → accepted")
                    except Exception as fe:
                        log_bot(uid, f"проверка друзей: {fe}")

                set_bot_state(uid, "смотрю матчи")

                items = []
                for game in ("cs2", "csgo"):
                    hr = await fc.get(
                        f"{API}/players/{me_id}/history",
                        params={"game": game, "limit": 20},
                    )
                    if hr.status_code != 200:
                        continue
                    try:
                        got = hr.json().get("items") or []
                    except Exception:
                        got = []
                    if got:
                        items = got
                        break

                with db_lock:
                    new = [
                        m for m in items
                        if isinstance(m, dict) and m.get("match_id") and not db.execute(
                            "SELECT 1 FROM seen WHERE user_id=? AND match_id=?",
                            (uid, m["match_id"]),
                        ).fetchone()
                    ]
                log_bot(uid, f"матчей: {len(items)}, новых: {len(new)}")

                if not new:
                    set_bot_state(uid, "жду новых матчей")
                    await asyncio.sleep(POLL)
                    continue

                for m in new:
                    mid = m["match_id"]
                    r2 = await fc.get(f"{API}/matches/{mid}")
                    if r2.status_code != 200:
                        continue
                    try:
                        teams = r2.json().get("teams") or {}
                    except Exception:
                        continue
                    if not isinstance(teams, dict):
                        continue
                    incomplete = False
                    for team in teams.values():
                        if not isinstance(team, dict):
                            continue
                        for p in team.get("roster") or []:
                            if not isinstance(p, dict):
                                continue
                            pnick, sid = p.get("nickname"), p.get("game_player_id")
                            if not pnick or not sid or pnick == nick:
                                continue
                            with db_lock:
                                already = db.execute(
                                    "SELECT 1 FROM players WHERE user_id=? AND nick=?",
                                    (uid, pnick),
                                ).fetchone()
                            if already:
                                continue
                            if time.time() - checked.get(pnick, 0) < 6 * 3600:
                                continue
                            set_bot_state(uid, f"проверяю {pnick}")
                            usd = "retry"
                            for attempt in range(3):
                                try:
                                    usd = await inventory_usd(c, sid)
                                    break
                                except RateLimited:
                                    wait = 300 + attempt * 60
                                    log_bot(uid, f"Steam rate limit, жду {wait} сек")
                                    set_bot_state(uid, f"Steam rate limit, жду {wait}с")
                                    await asyncio.sleep(wait)
                                except Exception as inv_err:
                                    log_bot(uid, f"{pnick}: инвентарь {inv_err}")
                                    usd = None
                                    break
                            if usd == "retry":
                                incomplete = True
                                continue
                            checked[pnick] = time.time()
                            if usd is None:
                                log_bot(uid, f"{pnick}: инвентарь закрыт")
                            elif usd >= min_usd:
                                with db_lock:
                                    db.execute(
                                        "INSERT OR IGNORE INTO players"
                                        "(user_id,nick,steam_id,usd,added) VALUES(?,?,?,?,?)",
                                        (uid, pnick, sid, round(usd), time.time()),
                                    )
                                    db.commit()
                                log_bot(uid, f"{pnick}: ${usd:.0f} добавлен")
                            else:
                                log_bot(uid, f"{pnick}: ${usd:.0f} мало")
                            checks += 1
                            if checks % BATCH == 0:
                                await asyncio.sleep(BATCH_PAUSE)
                            else:
                                await asyncio.sleep(STEAM_DELAY)

                    if incomplete:
                        log_bot(uid, f"матч {mid}: не все инвентари проверены, повторю позже")
                        continue
                    with db_lock:
                        db.execute(
                            "INSERT OR IGNORE INTO seen(user_id, match_id) VALUES(?,?)",
                            (uid, mid),
                        )
                        db.commit()

                set_bot_state(uid, "жду новых матчей")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                set_bot_state(uid, "ошибка")
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
    "background:radial-gradient(900px 500px at 85% -10%,#1b2430 0,transparent 60%),#0a0c0f;"
    "color:#eaeef2;font:15px/1.4 'Segoe UI Variable','Segoe UI',system-ui,sans-serif}"
    ".card{width:min(420px,94vw);background:#11141a;border:1px solid #232a33;border-radius:20px;padding:32px}"
    ".logo{display:flex;align-items:center;gap:10px;margin-bottom:24px}"
    ".logo i{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#8ea7c0,#5f7a96);"
    "display:grid;place-items:center;color:#0a0c0f;font-style:normal;font-weight:700;font-size:17px}"
    ".logo span{font:600 20px/1 Bahnschrift,'Segoe UI',sans-serif}"
    "h2{font:600 22px/1.2 Bahnschrift,'Segoe UI',sans-serif;margin:0 0 6px}"
    ".sub{color:#8994a1;font-size:14px;margin:0 0 22px}"
    "label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:#8994a1;margin-bottom:14px}"
    "input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #232a33;"
    "background:#171b22;color:#eaeef2;font:inherit;outline:none;transition:border-color .15s}"
    "input:focus{border-color:#5f7a96}"
    ".btn{display:block;width:100%;margin-top:8px;padding:12px;border:0;border-radius:12px;"
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
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path in ("/login", "/register", "/health"):
        return await call_next(request)
    if get_user(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


# Последний middleware становится внешним и отвечает на preflight до проверки сессии.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://[a-p]{32}",
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Panel-Token"],
)


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
async def api_set_status(nick: str, b: St, request: Request):
    u = get_user(request)
    if b.status not in ("new", "sent", "accepted"):
        return JSONResponse({"error": "invalid status"}, status_code=400)
    with db_lock:
        db.execute("UPDATE players SET status=? WHERE user_id=? AND nick=?",
                   (b.status, u["id"], nick))
        db.commit()
    # При переводе в sent — автоматически отправляем инвайт на FACEIT
    result = {"ok": True, "invite": None}
    if b.status == "sent":
        bearer = u["faceit_bearer"] or ""
        my_fid = u["faceit_id"] or ""
        if bearer and not my_fid and u["faceit_key"] and u["faceit_nick"]:
            my_fid = await get_my_faceit_id(u["faceit_key"], u["faceit_nick"])
            if my_fid:
                with db_lock:
                    db.execute("UPDATE users SET faceit_id=? WHERE id=?", (my_fid, u["id"]))
                    db.commit()
        if bearer:
            ok, msg, pid = await faceit_send_friend(u["faceit_key"], bearer, my_fid, nick)
            log_bot(u["id"], f"инвайт → {nick}: {msg}")
            result["invite"] = msg
            if pid:
                with db_lock:
                    db.execute(
                        "UPDATE players SET faceit_pid=? WHERE user_id=? AND nick=?",
                        (pid, u["id"], nick),
                    )
                    db.commit()
        else:
            result["invite"] = "bearer не указан — статус сохранён, заявка не отправлена"
    return result


@app.delete("/api/players/{nick}")
def api_delete_player(nick: str, request: Request):
    u = get_user(request)
    with db_lock:
        db.execute("DELETE FROM players WHERE user_id=? AND nick=?", (u["id"], nick))
        db.commit()
    return {"ok": True}


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
    faceit_bearer: str = ""


@app.get("/api/settings")
def api_get_settings(request: Request):
    u = get_user(request)
    return {
        "faceit_key": u["faceit_key"],
        "faceit_nick": u["faceit_nick"],
        "min_usd": u["min_usd"],
        "faceit_bearer": u["faceit_bearer"] or "",
    }


@app.post("/api/settings")
def api_save_settings(b: SettingsIn, request: Request):
    u = get_user(request)
    key    = b.faceit_key.strip()
    nick   = b.faceit_nick.strip()
    usd    = float(b.min_usd)
    bearer = b.faceit_bearer.strip()
    if not key:
        return JSONResponse({"error": "укажи FACEIT API ключ"}, status_code=400)
    if not nick:
        return JSONResponse({"error": "укажи FACEIT ник"}, status_code=400)
    if usd < 0:
        return JSONResponse({"error": "минимальная цена не может быть меньше 0"}, status_code=400)
    nick_changed = nick != (u["faceit_nick"] or "")
    with db_lock:
        if nick_changed:
            db.execute(
                "UPDATE users SET faceit_key=?, faceit_nick=?, min_usd=?, faceit_bearer=?, faceit_id='' WHERE id=?",
                (key, nick, usd, bearer, u["id"]))
        else:
            db.execute(
                "UPDATE users SET faceit_key=?, faceit_nick=?, min_usd=?, faceit_bearer=? WHERE id=?",
                (key, nick, usd, bearer, u["id"]))
        if usd > float(u["min_usd"] or 0):
            db.execute("DELETE FROM players WHERE user_id=? AND usd < ?", (u["id"], usd))
        db.commit()
    return {"ok": True, "faceit_key": key, "faceit_nick": nick, "min_usd": usd, "faceit_bearer": bearer}


# ── API: обновление bearer токена через расширение ───────────────────────────
class TokenIn(BaseModel):
    token: str


@app.post("/api/token")
async def api_set_token(b: TokenIn, request: Request):
    u = get_user(request)
    token = b.token.strip()
    if not token:
        return JSONResponse({"error": "токен пустой"}, status_code=400)
    # Получаем faceit_id по токену
    my_fid = u["faceit_id"] or ""
    if u["faceit_nick"] and u["faceit_key"]:
        resolved = await get_my_faceit_id(u["faceit_key"], u["faceit_nick"])
        if resolved:
            my_fid = resolved
    with db_lock:
        db.execute("UPDATE users SET faceit_bearer=?, faceit_id=? WHERE id=?",
                   (token, my_fid, u["id"]))
        db.commit()
    log_bot(u["id"], "bearer токен обновлён через расширение")
    return {"ok": True, "faceit_id": my_fid}


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
    me = require_admin(request)
    if not me:
        return JSONResponse({"error": "forbidden"}, status_code=403)
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
