#!/usr/bin/env python3
"""
ls_radio.py — SQLite-backed picker + history for Liquidsoap.

Subcommands:
  pick-next         -> prints a path (or empty string) to stdout and exits.
  track-start       -> record actual on-air start times; args: --artist --title --path
  rebuild-cache     -> force cache rebuild now (runs in-foreground)
  init              -> create DB schema if missing
  vacuum            -> sqlite VACUUM

Environment vars (see systemd unit for defaults):
  LS_DB, LS_MUSIC_DIR, LS_RESCAN_SEC, LS_LOCK_STALE_SEC,
  LS_ARTIST_SEP_MIN, LS_TITLE_SEP_MIN, LS_TRACK_SEP_SEC,
  LS_FFPROBE_TIMEOUT_S, LS_SCAN_EXTS, LS_UNKNOWN_ARTIST_BUCKET,
  LS_HISTORY_KEEP, LS_HISTORY_KEEP_PATHS,
  LS_TOP_N_DIRS, LS_FILES_PER_DIR_TRY
"""

import os, sys, time, json, random, pathlib, subprocess, tempfile, argparse, atexit, signal, sqlite3, re, unicodedata

# ------------- ENV -------------
DB_PATH     = os.environ.get("LS_DB", "/var/lib/liquidsoap/liquidsoap.db")
MUSIC_DIR   = os.environ.get("LS_MUSIC_DIR", "/srv/music")

RE_SCAN_SEC = int(os.environ.get("LS_RESCAN_SEC", "86400"))
LOCK_STALE  = int(os.environ.get("LS_LOCK_STALE_SEC", "3600"))

ARTIST_SEP  = int(os.environ.get("LS_ARTIST_SEP_MIN", "45")) * 60
TITLE_SEP   = int(os.environ.get("LS_TITLE_SEP_MIN",  "180")) * 60
TRACK_SEP   = int(os.environ.get("LS_TRACK_SEP_SEC",  "0"))

FFPROBE_TIMEOUT = float(os.environ.get("LS_FFPROBE_TIMEOUT_S", "0.8"))
UNKNOWN_ARTIST_BUCKET = os.environ.get("LS_UNKNOWN_ARTIST_BUCKET", "1") == "1"

HISTORY_KEEP        = int(os.environ.get("LS_HISTORY_KEEP", "10000"))
HISTORY_KEEP_PATHS  = int(os.environ.get("LS_HISTORY_KEEP_PATHS", "20000"))

SCAN_EXTS = set(e.strip().lower() for e in os.environ.get("LS_SCAN_EXTS",".mp3,.flac,.m4a,.ogg,.wav,.aac").split(","))

TOP_N_DIRS        = int(os.environ.get("LS_TOP_N_DIRS", "64"))
FILES_PER_DIR_TRY = int(os.environ.get("LS_FILES_PER_DIR_TRY", "128"))

random.seed(time.time_ns())

# ------------- UTIL -------------
def now_ts(): return time.time()

def key_norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[\W_]+", "", s)

def is_audio_file(name: str) -> bool:
    return pathlib.Path(name).suffix.lower() in SCAN_EXTS

def ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

# ------------- DB -------------
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  val TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  mtime REAL NOT NULL,
  artist_norm TEXT,
  title_norm TEXT,
  artist_raw TEXT,
  title_raw TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_artist_norm ON files(artist_norm);
CREATE INDEX IF NOT EXISTS idx_files_title_norm  ON files(title_norm);

-- last on-air start time by normalized keys
CREATE TABLE IF NOT EXISTS last_artist_play (
  artist_norm TEXT PRIMARY KEY,
  ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS last_title_play (
  title_norm  TEXT PRIMARY KEY,
  ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS last_path_play (
  path TEXT PRIMARY KEY,
  ts REAL NOT NULL
);

-- simple lock to serialize heavy cache refreshes
CREATE TABLE IF NOT EXISTS locks (
  name TEXT PRIMARY KEY,
  pid  INTEGER NOT NULL,
  ts   REAL NOT NULL
);
"""

def db_connect():
    ensure_dir(DB_PATH)
    con = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con

def db_init(con):
    for stmt in SCHEMA.strip().split(";\n\n"):
        if stmt.strip():
            con.executescript(stmt + ";")

def meta_get(con, key, default=None):
    cur = con.execute("SELECT val FROM meta WHERE key=?", (key,))
    r = cur.fetchone()
    return json.loads(r["val"]) if r else default

def meta_set(con, key, val):
    con.execute("INSERT INTO meta(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, json.dumps(val)))

# ------------- TAGS -------------
def ffprobe_tags(path: str):
    artist = ""; title = ""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=artist,title,album_artist,albumartist,performer,AlbumArtist,ALBUMARTIST,ARTIST,TITLE,PERFORMER",
                "-of", "json", path,
            ],
            stderr=subprocess.DEVNULL, text=True, timeout=FFPROBE_TIMEOUT
        )
        data = json.loads(out)
        tags = (data.get("format") or {}).get("tags") or {}
        tags_norm = { (k or "").lower(): (v or "").strip() for k, v in tags.items() }
        for key in ("artist","albumartist","album_artist","album artist","performer"):
            v = tags_norm.get(key, "")
            if v:
                artist = v; break
        title = tags_norm.get("title","")
    except Exception:
        pass

    base = pathlib.Path(path).stem.strip()
    if not title:
        title = base
    if not artist and " - " in base:
        a, _t = base.split(" - ", 1)
        artist = a

    return artist, title

# ------------- SCAN / CACHE -------------
def scan_paths(root_dir: str):
    stack = [root_dir]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False) and is_audio_file(e.name):
                        yield e.path
        except (PermissionError, FileNotFoundError):
            continue

def refresh_cache(con):
    # build into temp table then swap for minimal lock
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("""
        CREATE TEMP TABLE t_new_files (
          path TEXT PRIMARY KEY,
          mtime REAL NOT NULL,
          artist_norm TEXT,
          title_norm TEXT,
          artist_raw TEXT,
          title_raw TEXT
        );
        """)

        for p in scan_paths(MUSIC_DIR):
            try:
                st = os.stat(p)
            except (FileNotFoundError, PermissionError):
                continue
            a_raw, t_raw = ffprobe_tags(p)
            a_norm = key_norm(a_raw) or ("__unknown__" if UNKNOWN_ARTIST_BUCKET else None)
            t_norm = key_norm(t_raw) or None

            con.execute(
                "INSERT OR REPLACE INTO t_new_files(path, mtime, artist_norm, title_norm, artist_raw, title_raw) VALUES(?,?,?,?,?,?)",
                (p, st.st_mtime, a_norm, t_norm, a_raw, t_raw)
            )

        # Replace old with new
        con.execute("DELETE FROM files;")
        con.execute("INSERT INTO files SELECT * FROM t_new_files;")
        meta_set(con, "generated_at", now_ts())
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

# ------------- LOCK -------------
def try_acquire_lock(con, name: str, stale_sec: int) -> bool:
    pid = os.getpid()
    now = now_ts()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT pid, ts FROM locks WHERE name=?", (name,)).fetchone()
        ok = False
        if row is None:
            con.execute("INSERT INTO locks(name,pid,ts) VALUES(?,?,?)", (name, pid, now))
            ok = True
        else:
            old_pid, ts = int(row["pid"]), float(row["ts"])
            if (now - ts) > stale_sec:
                con.execute("UPDATE locks SET pid=?, ts=? WHERE name=?", (pid, now, name))
                ok = True
        con.execute("COMMIT")
        return ok
    except Exception:
        con.execute("ROLLBACK")
        return False

def release_lock(con, name: str):
    try:
        con.execute("DELETE FROM locks WHERE name=?", (name,))
    except Exception:
        pass

# ------------- HISTORY / SEPARATION -------------
def too_recent(con, artist_norm: str, title_norm: str, path: str) -> bool:
    now = now_ts()
    if artist_norm:
        r = con.execute("SELECT ts FROM last_artist_play WHERE artist_norm=?", (artist_norm,)).fetchone()
        if r and (now - float(r["ts"]) < ARTIST_SEP): return True
    if title_norm:
        r = con.execute("SELECT ts FROM last_title_play WHERE title_norm=?", (title_norm,)).fetchone()
        if r and (now - float(r["ts"]) < TITLE_SEP): return True
    if TRACK_SEP and path:
        r = con.execute("SELECT ts FROM last_path_play WHERE path=?", (path,)).fetchone()
        if r and (now - float(r["ts"]) < TRACK_SEP): return True
    return False

def violation_score(con, artist_norm: str, title_norm: str, path: str) -> float:
    now = now_ts()
    def age(table, keycol, keyval):
        if not keyval: return 10**9
        r = con.execute(f"SELECT ts FROM {table} WHERE {keycol}=?", (keyval,)).fetchone()
        return now - float(r["ts"]) if r else 10**9
    sa = age("last_artist_play","artist_norm",artist_norm)
    st = age("last_title_play","title_norm",title_norm)
    sp = age("last_path_play","path",path) if TRACK_SEP else 10**9
    return min(sa, st, sp)

def stamp_selection(con, path: str, artist_norm: str, title_norm: str):
    # optional pre-stamp at selection time (best effort)
    now = now_ts()
    if artist_norm:
        con.execute("INSERT INTO last_artist_play(artist_norm, ts) VALUES(?,?) ON CONFLICT(artist_norm) DO UPDATE SET ts=excluded.ts", (artist_norm, now))
    if title_norm:
        con.execute("INSERT INTO last_title_play(title_norm, ts) VALUES(?,?) ON CONFLICT(title_norm) DO UPDATE SET ts=excluded.ts", (title_norm, now))
    if TRACK_SEP and path:
        con.execute("INSERT INTO last_path_play(path, ts) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET ts=excluded.ts", (path, now))
    # prune by LRU-ish age via row count limits
    # SQLite doesn't have easy LRU eviction; rely on count cap with oldest trim
    # Keep it simple and cheap:
    con.execute("DELETE FROM last_artist_play WHERE rowid NOT IN (SELECT rowid FROM last_artist_play ORDER BY ts DESC LIMIT ?)", (HISTORY_KEEP,))
    con.execute("DELETE FROM last_title_play  WHERE rowid NOT IN (SELECT rowid FROM last_title_play  ORDER BY ts DESC LIMIT ?)", (HISTORY_KEEP,))
    con.execute("DELETE FROM last_path_play   WHERE rowid NOT IN (SELECT rowid FROM last_path_play   ORDER BY ts DESC LIMIT ?)", (HISTORY_KEEP_PATHS,))

def track_start(con, artist: str, title: str, path: str):
    a_norm = key_norm(artist) or ("__unknown__" if UNKNOWN_ARTIST_BUCKET else None)
    t_norm = key_norm(title) or None
    now = now_ts()
    if a_norm:
        con.execute("INSERT INTO last_artist_play(artist_norm, ts) VALUES(?,?) ON CONFLICT(artist_norm) DO UPDATE SET ts=excluded.ts", (a_norm, now))
    if t_norm:
        con.execute("INSERT INTO last_title_play(title_norm, ts) VALUES(?,?) ON CONFLICT(title_norm) DO UPDATE SET ts=excluded.ts", (t_norm, now))
    if path:
        con.execute("INSERT INTO last_path_play(path, ts) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET ts=excluded.ts", (path, now))
    con.execute("DELETE FROM last_artist_play WHERE rowid NOT IN (SELECT rowid FROM last_artist_play ORDER BY ts DESC LIMIT ?)", (HISTORY_KEEP,))
    con.execute("DELETE FROM last_title_play  WHERE rowid NOT IN (SELECT rowid FROM last_title_play  ORDER BY ts DESC LIMIT ?)", (HISTORY_KEEP,))
    con.execute("DELETE FROM last_path_play   WHERE rowid NOT IN (SELECT rowid FROM last_path_play   ORDER BY ts DESC LIMIT ?)", (HISTORY_KEEP_PATHS,))

# ------------- PICKING -------------
def pick_from_cache(con):
    # Randomize traversal but keep it SQL-simple
    # We'll sample a chunk (to avoid scanning the whole table) then strict-pass, then least-violating.
    # If your library is huge and you want smarter sampling, we can add reservoir logic later.
    rows = con.execute("SELECT path, artist_norm, title_norm FROM files ORDER BY random() LIMIT 2000").fetchall()
    if not rows:
        return None

    # Strict pass
    for r in rows:
        if not too_recent(con, r["artist_norm"], r["title_norm"], r["path"]):
            return r

    # Least-violating
    best = None; best_score = -1.0
    for r in rows:
        sc = violation_score(con, r["artist_norm"], r["title_norm"], r["path"])
        if sc > best_score:
            best, best_score = r, sc
    return best

def quick_random_dart():
    # 1) top-level
    try:
        with os.scandir(MUSIC_DIR) as it:
            entries = [e for e in it]
    except Exception:
        entries = []
    random.shuffle(entries)
    entries = entries[:TOP_N_DIRS] if TOP_N_DIRS > 0 else entries

    files = [e.path for e in entries if e.is_file(follow_symlinks=False) and is_audio_file(e.name)]
    if files:
        return random.choice(files)

    # 2) peek into a few dirs
    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    random.shuffle(dirs)
    for d in dirs:
        try:
            with os.scandir(d.path) as it:
                fs = [e.path for e in it if e.is_file(follow_symlinks=False) and is_audio_file(e.name)]
        except Exception:
            continue
        if fs:
            random.shuffle(fs)
            return fs[0 if len(fs) == 1 else min(len(fs)-1, os.urandom(1)[0] % min(len(fs), FILES_PER_DIR_TRY))]

    # 3) shallow walk
    for p in scan_paths(MUSIC_DIR):
        return p
    return None

def ensure_fresh_cache_async(con):
    gen = meta_get(con, "generated_at", 0) or 0
    if (now_ts() - float(gen)) <= RE_SCAN_SEC and con.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        return  # fresh enough

    # Try lock; if we win, fork a builder and parent returns immediately.
    if not try_acquire_lock(con, "cache_builder", LOCK_STALE):
        return

    try:
        pid = os.fork()
    except Exception:
        # Can't fork; build inline (blocking). Better to release lock and do nothing here.
        release_lock(con, "cache_builder")
        return

    if pid != 0:
        # parent: we're done; child builds
        return

    # child process
    try:
        refresh_cache(con)
    except Exception:
        pass
    finally:
        release_lock(con, "cache_builder")
        os._exit(0)

# ------------- CLI -------------
def cmd_pick_next():
    con = db_connect(); db_init(con)
    ensure_fresh_cache_async(con)

    # Try cached pick
    r = pick_from_cache(con)
    if r:
        path = r["path"]
        # best-effort selection stamp (on-air overwrite will happen later)
        stamp_selection(con, path, r["artist_norm"], r["title_norm"])
        print(path, end="")
        return

    # Cold start fallback
    p = quick_random_dart()
    if p:
        try:
            a_raw, t_raw = ffprobe_tags(p)
            a_norm = key_norm(a_raw) or ("__unknown__" if UNKNOWN_ARTIST_BUCKET else None)
            t_norm = key_norm(t_raw) or None
            stamp_selection(con, p, a_norm, t_norm)
        except Exception:
            pass
        print(p, end="")
        return

    # Nothing -> let LS fall back to silence
    print("", end="")

def cmd_track_start(args):
    con = db_connect(); db_init(con)
    track_start(con, args.artist or "", args.title or "", args.path or "")

def cmd_rebuild_cache():
    con = db_connect(); db_init(con)
    refresh_cache(con)

def cmd_init():
    con = db_connect(); db_init(con)

def cmd_vacuum():
    con = db_connect(); db_init(con)
    con.execute("VACUUM")

def main():
    ap = argparse.ArgumentParser(prog="ls_radio.py", add_help=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pick-next")

    ap_ts = sub.add_parser("track-start")
    ap_ts.add_argument("--artist", default="", help="artist (raw)")
    ap_ts.add_argument("--title",  default="", help="title (raw)")
    ap_ts.add_argument("--path",   default="", help="full file path")

    sub.add_parser("rebuild-cache")
    sub.add_parser("init")
    sub.add_parser("vacuum")

    args = ap.parse_args()
    if args.cmd == "pick-next":      cmd_pick_next()
    elif args.cmd == "track-start":  cmd_track_start(args)
    elif args.cmd == "rebuild-cache":cmd_rebuild_cache()
    elif args.cmd == "init":         cmd_init()
    elif args.cmd == "vacuum":       cmd_vacuum()

if __name__ == "__main__":
    main()

