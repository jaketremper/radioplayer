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
  LS_TOP_N_DIRS, LS_FILES_PER_DIR_TRY,
  LS_EVERGREEN_DIR, LS_SLOT_PRE_SEC, LS_SLOT_POST_SEC
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

# Evergreen / scheduled content
# Drop audio files into LS_EVERGREEN_DIR to have one played automatically
# near each quarter-hour boundary. If the directory is empty or unset,
# the feature is silently disabled and normal music plays uninterrupted.
EVERGREEN_DIR = os.environ.get("LS_EVERGREEN_DIR", "/var/lib/liquidsoap/evergreen")
SLOT_PRE_SEC  = int(os.environ.get("LS_SLOT_PRE_SEC",  "150"))  # seconds before :00/:15/:30/:45
SLOT_POST_SEC = int(os.environ.get("LS_SLOT_POST_SEC", "150"))  # seconds after

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

# ------------- SLOT LOGIC -------------
SLOT_MINUTES = (0, 15, 30, 45)

def current_slot_id() -> int:
    """
    Return an integer uniquely identifying the current quarter-hour slot,
    or -1 if we're not inside any slot window.

    Slot ID = hour * 4 + slot_index.

    With default 2.5-min pre/post windows:
      :00 -> 57:30–02:30  (wraps hour boundary)
      :15 -> 12:30–17:30
      :30 -> 27:30–32:30
      :45 -> 42:30–47:30

    Returns -1 if EVERGREEN_DIR is not configured or doesn't exist.
    """
    if not EVERGREEN_DIR:
        return -1

    t = time.localtime()
    total_secs = t.tm_min * 60 + t.tm_sec  # seconds into current hour

    for i, m in enumerate(SLOT_MINUTES):
        slot_secs = m * 60

        if m == 0:
            # :00 wraps the hour boundary
            secs_after  = total_secs        # seconds past :00
            secs_before = 3600 - total_secs # seconds until next :00
            if secs_after <= SLOT_POST_SEC:
                return t.tm_hour * 4 + 0
            if secs_before <= SLOT_PRE_SEC:
                return ((t.tm_hour + 1) % 24) * 4 + 0
        else:
            diff = total_secs - slot_secs
            if -SLOT_PRE_SEC <= diff <= SLOT_POST_SEC:
                return t.tm_hour * 4 + i

    return -1

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

-- tracks which quarter-hour slots have already had a clip served
CREATE TABLE IF NOT EXISTS evergreen_played (
  slot_id INTEGER PRIMARY KEY,
  ts      REAL NOT NULL
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
    now = now_ts()
    if artist_norm:
        con.execute("INSERT INTO last_artist_play(artist_norm, ts) VALUES(?,?) ON CONFLICT(artist_norm) DO UPDATE SET ts=excluded.ts", (artist_norm, now))
    if title_norm:
        con.execute("INSERT INTO last_title_play(title_norm, ts) VALUES(?,?) ON CONFLICT(title_norm) DO UPDATE SET ts=excluded.ts", (title_norm, now))
    if TRACK_SEP and path:
        con.execute("INSERT INTO last_path_play(path, ts) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET ts=excluded.ts", (path, now))
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

# ------------- EVERGREEN -------------
def pick_evergreen() -> str:
    """Pick a random audio file from the evergreen directory. Returns empty string if none available."""
    try:
        files = [
            os.path.join(EVERGREEN_DIR, f)
            for f in os.listdir(EVERGREEN_DIR)
            if is_audio_file(f)
        ]
        if files:
            return random.choice(files)
    except Exception:
        pass
    return ""

def slot_already_served(con, slot_id: int) -> bool:
    r = con.execute("SELECT ts FROM evergreen_played WHERE slot_id=?", (slot_id,)).fetchone()
    return r is not None

def mark_slot_served(con, slot_id: int):
    con.execute(
        "INSERT INTO evergreen_played(slot_id, ts) VALUES(?,?) ON CONFLICT(slot_id) DO UPDATE SET ts=excluded.ts",
        (slot_id, now_ts())
    )
    # Keep last 200 entries (well over a day's worth at 4 slots/hour)
    con.execute("DELETE FROM evergreen_played WHERE rowid NOT IN (SELECT rowid FROM evergreen_played ORDER BY ts DESC LIMIT 200)")

# ------------- PICKING -------------
def pick_from_cache(con):
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

    if not try_acquire_lock(con, "cache_builder", LOCK_STALE):
        return

    try:
        pid = os.fork()
    except Exception:
        release_lock(con, "cache_builder")
        return

    if pid != 0:
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

    # Evergreen slot check: if we're within the window of a quarter-hour boundary
    # and haven't already served a clip for this slot, return a clip instead of music.
    # If EVERGREEN_DIR is empty or missing, this is silently skipped.
    slot_id = current_slot_id()
    if slot_id >= 0 and not slot_already_served(con, slot_id):
        clip = pick_evergreen()
        if clip:
            mark_slot_served(con, slot_id)
            print(clip, end="")
            return

    # Normal music pick
    r = pick_from_cache(con)
    if r:
        path = r["path"]
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
