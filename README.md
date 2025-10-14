# Radio Player - Simple Internet Radio Station Setup Guide

A minimal, single-file radio player powered by **Liquidsoap** and **Icecast**.  
No CMS. No database admin. No app required. Just your music, a stream, and a simple web player.

---

## Overview

This guide shows you how to build a self-hosted internet radio station that's lightweight, secure, and fully open source.  
It uses three core components:

| Component | Role | Example Host |
|------------|------|--------------|
| **Transcoder (Liquidsoap Host)** | Reads and plays your local music library, applies audio processing, and streams encoded MP3 audio to Icecast. | `radio.example.com` |
| **Icecast Server** | Receives the encoded audio stream and serves it to listeners, exposing now-playing metadata via JSON. | `stream.example.com` |
| **Web Server** | Hosts the single-file HTML player and optionally proxies the Icecast stream for HTTPS. | `www.example.com` |

### Data Flow

```text
Music Files → Liquidsoap (via ls_radio.py)
             ↓
         Icecast Server
             ↓
          nginx (proxy)
             ↓
          Listeners
```

**Metadata Flow:**  
`Icecast status-json.xsl` → `index.html` → artwork & now-playing info.

All three services can run on a single VPS or be separated for scalability.

---

## Quick Start

### 1. Install Dependencies

#### On Transcoder (Liquidsoap Host, Ubuntu 22.04/24.04)
```bash
sudo apt update
sudo apt install liquidsoap python3 ffmpeg sqlite3
```

**Note:** `ls_radio.py` uses only Python stdlib - no pip packages required.

#### On Icecast Server
```bash
sudo apt install icecast2
```

#### On Web Server
```bash
sudo apt install nginx
# Or use any static hosting: GitHub Pages, Netlify, Cloudflare Pages, etc.
```

---

### 2. Configure Icecast

Edit `/etc/icecast2/icecast.xml`:

```xml
<location>Your Location</location>
<admin>your-email@example.com</admin>
<hostname>stream.example.com</hostname>
...
<authentication>
    <source-password>CHANGEME-source</source-password>
    <relay-password>CHANGEME-relay</relay-password>
    <admin-user>admin</admin-user>
    <admin-password>CHANGEME-admin</admin-password>
</authentication>
...
```

**IMPORTANT:** Change all default passwords before exposing to the internet.

Then start Icecast:

```bash
sudo systemctl enable icecast2
sudo systemctl start icecast2
```

Verify it's running:
```bash
sudo systemctl status icecast2
curl -I http://localhost:8000/
```

---

### 3. Set Up the Track Selector

**Note:** `ls_radio.py` uses `os.fork()` which is POSIX/Linux-only. It will not work on Windows.

Copy `ls_radio.py` to `/usr/local/bin/` on your transcoder host:

```bash
sudo cp ls_radio.py /usr/local/bin/
sudo chmod +x /usr/local/bin/ls_radio.py
```

Create a short file of silence (Liquidsoap uses this when no track is picked):

```bash
sudo mkdir -p /usr/share/liquidsoap
sudo ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 1 -q:a 9 -acodec libmp3lame /usr/share/liquidsoap/_silence.mp3
```

The picker outputs a file path to stdout. Liquidsoap reads that path to request the next song.

#### First Run Behavior

On first run with an empty cache:
1. `pick-next` returns a random track immediately (via `quick_random_dart()`)
2. A background process forks to build the full cache
3. Subsequent picks use the cache for better separation logic
4. Cache rebuilds automatically every 24 hours (configurable via `LS_RESCAN_SEC`)

---

### 4. Configure Liquidsoap

Create a user and home directory & song picker cache directory:

```bash
sudo useradd -m -d /home/liquidsoap -s /bin/bash liquidsoap
sudo mkdir -p /var/lib/liquidsoap
sudo chown liquidsoap:liquidsoap /var/lib/liquidsoap
sudo -u liquidsoap /usr/bin/python3 /usr/local/bin/ls_radio.py init
```

Pre-build the cache (this can take a few minutes if you have a big library):
```bash
sudo -u liquidsoap /usr/bin/python3 /usr/local/bin/ls_radio.py rebuild-cache
```

Create `/etc/liquidsoap/stream.liq` with your stream information:

```liquidsoap
def q(s) = string.quote(s) end

def on_start_meta(m) =
  artist = if m["artist"] != "" then m["artist"] else "" end
  title  = if m["title"]  != "" then m["title"]  else "" end
  file   = if m["filename"] != "" then m["filename"] else "" end

  log("ON-AIR: #{artist} - #{title} (#{file})")

  ignore(process.run(
    "/usr/bin/python3 /usr/local/bin/ls_radio.py track-start "
    ^ "--artist " ^ q(artist) ^ " "
    ^ "--title "  ^ q(title)  ^ " "
    ^ "--path "   ^ q(file)
  ))
  m
end

def next_request() =
  uri = string.trim(process.read("/usr/bin/python3 /usr/local/bin/ls_radio.py pick-next"))
  if uri == "" then
    request.create("/usr/share/liquidsoap/_silence.mp3")
  else
    request.create(uri)
  end
end

radio = request.dynamic(next_request)
radio = map_metadata(on_start_meta, radio)

radio = mksafe(crossfade(radio))

radio = normalize(radio, target=-16.0, threshold=-22.0, window=0.5)

radio = compress(radio, 
  threshold=-18.0,
  ratio=2.5,
  attack=0.01,
  release=0.3,
  gain=3.0
)

radio = limit(radio, threshold=-0.5, attack=0.005, release=0.1)

output.icecast(
  %mp3(bitrate=192),
  host="stream.example.com", port=8000, password="YOUR-SOURCE-PASSWORD",
  mount="/live", name="Your Radio Station",
  url="https://radio.example.com", genre="Various", public=true,
  radio
)
```

**Lock down permissions** so you're not exposing your Icecast source password:

```bash
sudo mkdir -p /etc/liquidsoap
sudo chown liquidsoap:liquidsoap /etc/liquidsoap
sudo chmod 0700 /etc/liquidsoap
sudo chmod 0600 /etc/liquidsoap/stream.liq
```

Create systemd service `/etc/systemd/system/liquidsoap.service`:

```ini
[Unit]
Description=Liquidsoap Stream
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=liquidsoap
Group=liquidsoap
WorkingDirectory=/home/liquidsoap
ExecStart=/usr/bin/liquidsoap /etc/liquidsoap/stream.liq
Restart=always
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGINT
StandardOutput=journal
StandardError=journal

# ---- Tuning knobs (env) ----
Environment=LS_MUSIC_DIR=/srv/music
Environment=LS_DB=/var/lib/liquidsoap/liquidsoap.db

# Separation windows
Environment=LS_ARTIST_SEP_MIN=45
Environment=LS_TITLE_SEP_MIN=180
Environment=LS_TRACK_SEP_SEC=0

# Cache + lock behavior
Environment=LS_RESCAN_SEC=86400
Environment=LS_LOCK_STALE_SEC=3600
Environment=LS_TOP_N_DIRS=64
Environment=LS_FILES_PER_DIR_TRY=128

# Tags / scanning
Environment=LS_FFPROBE_TIMEOUT_S=0.8
Environment=LS_SCAN_EXTS=.mp3,.flac,.m4a,.ogg,.wav,.aac
Environment=LS_UNKNOWN_ARTIST_BUCKET=1

# History retention
Environment=LS_HISTORY_KEEP=10000
Environment=LS_HISTORY_KEEP_PATHS=20000

LimitNOFILE=131072
Nice=5
IOSchedulingClass=best-effort
IOSchedulingPriority=7

NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectClock=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictNamespaces=true
RestrictRealtime=true
SystemCallArchitectures=native

ProtectSystem=strict
ReadWritePaths=/var/lib/liquidsoap
ReadOnlyPaths=/srv/music
ReadOnlyPaths=/home/liquidsoap

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable liquidsoap
sudo systemctl start liquidsoap
```

**Optional sanity check:**
```bash
liquidsoap --check /etc/liquidsoap/stream.liq
```

---

### 5. Configure nginx

**Two versions available:**
- `index.html` - Requires nginx caching proxy (recommended for production)
- `index.html.nocache` - Direct iTunes API calls (simpler setup, may hit rate limits)

#### Option A: With nginx Caching (Recommended)

Add to your nginx http block (usually in `/etc/nginx/nginx.conf`):

```nginx
# Enable album art caching
proxy_cache_path /var/cache/nginx/itunes keys_zone=itunes:10m inactive=14d max_size=2g;
resolver 1.1.1.1 1.0.0.1 valid=300s ipv6=off;

# Rudimentary scraper blocking
map $http_user_agent $block_scraper {
    default 0;
    ~*(curl|wget|python|php|go-http-client|scrapy|httpclient) 1;
}

# Rate limiting
limit_conn_zone $binary_remote_addr zone=connperip:10m;
limit_req_zone $binary_remote_addr zone=reqperip:10m rate=30r/m;

# Icecast upstream
upstream icecast {
    server 127.0.0.1:8000;
    keepalive 32;
}
```

Create `/etc/nginx/sites-available/radio.example.com`:

```nginx
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name radio.example.com;

    ssl_certificate /etc/letsencrypt/live/radio.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/radio.example.com/privkey.pem;

    # Block scrapers
    if ($block_scraper) { return 403; }

    # Force HTTPS
    if ($scheme != "https") {
        return 301 https://$host$request_uri;
    }

    client_max_body_size 10m;
    sendfile on;
    keepalive_timeout 15;
    keepalive_requests 1000;

    # Web player
    location /radio {
        alias /var/www/html/radio;
        try_files $uri /index.html =404;
    }

    # Stream endpoint
    location = /live {
        default_type "";

        if ($request_method = HEAD) {
            add_header Accept-Ranges "bytes";
            return 200;
        }

        proxy_pass http://icecast/live;
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        gzip off;
        gunzip off;
        proxy_set_header Accept-Encoding "";
        proxy_buffering off;
        proxy_request_buffering off;

        proxy_set_header Icy-MetaData "0";
        proxy_hide_header icy-metaint;
        proxy_hide_header icy-name;
        proxy_hide_header icy-url;

        add_header Accept-Ranges "bytes" always;
        add_header Access-Control-Allow-Origin "*" always;
        add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS" always;
        add_header Access-Control-Expose-Headers "Content-Length,Content-Range,Accept-Ranges" always;
        add_header Cache-Control "no-store" always;

        proxy_read_timeout 12h;
        send_timeout 12h;
        proxy_redirect off;
        limit_conn connperip 3;
    }

    # Status JSON
    location = /status-json.xsl {
        add_header Access-Control-Allow-Origin "*" always;
        add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_request_buffering off;

        limit_req zone=reqperip burst=30 nodelay;

        proxy_pass http://icecast;
        proxy_redirect off;
    }

    # iTunes search proxy with caching
    location /itunes/search {
        proxy_pass https://itunes.apple.com/search$is_args$args;

        proxy_cache itunes;
        proxy_cache_valid 200 302 7d;
        proxy_cache_valid 404 5m;
        proxy_ignore_headers Set-Cookie Expires Cache-Control;

        proxy_hide_header Content-Disposition;
        add_header Content-Type "application/json; charset=utf-8";
        add_header X-Cache-Status $upstream_cache_status always;
    }

    # iTunes artwork proxy with caching
    location /itunes/art {
        if ($arg_u = "") { return 400; }

        proxy_pass $arg_u;
        proxy_ssl_server_name on;
        proxy_set_header Host $proxy_host;

        proxy_cache itunes;
        proxy_cache_key $arg_u;
        proxy_cache_valid 200 30d;
        proxy_cache_lock on;
        proxy_ignore_headers Set-Cookie Expires Cache-Control;

        proxy_hide_header Content-Disposition;
        add_header Content-Type "image/jpeg";
        add_header X-Cache-Status $upstream_cache_status always;
    }
}
```

Enable site and ensure cache directory:
```bash
sudo mkdir -p /var/cache/nginx
sudo chown www-data:www-data /var/cache/nginx
sudo ln -s /etc/nginx/sites-available/radio.example.com /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

#### Option B: Without nginx Caching (Simple Setup)

Use `index.html.nocache` instead of `index.html`. You still need nginx to proxy the stream but can skip the iTunes caching locations.

**Warning:** Without caching, you may hit iTunes API rate limits if you have many concurrent listeners.

---

### 6. Deploy Web Player

#### Create Fallback Artwork

The player needs fallback images when album art isn't found. Create three sizes:

```bash
# Option 1: From an existing logo/image
sudo apt install imagemagick
convert your-logo.png -resize 512x512 artwork-512.png
convert your-logo.png -resize 192x192 artwork-192.png
convert your-logo.png -resize 96x96 artwork-096.png

# Option 2: Simple colored placeholder
convert -size 512x512 xc:#2563eb -pointsize 72 -fill white -gravity center \
  -annotate +0+0 "RADIO\nPLAYER" artwork-512.png
convert -size 192x192 xc:#2563eb -pointsize 32 -fill white -gravity center \
  -annotate +0+0 "RADIO\nPLAYER" artwork-192.png
convert -size 96x96 xc:#2563eb -pointsize 16 -fill white -gravity center \
  -annotate +0+0 "RADIO\nPLAYER" artwork-096.png
```

#### Configure and Deploy

Edit `index.html`:

```javascript
const STREAM_URL   = 'https://radio.example.com/live';
const STATUS_URL   = 'https://radio.example.com/status-json.xsl';
const TARGET_MOUNT = '/live';
```

Also update the external player link:
```html
<a id="openExternal" class="btn ghost" href="https://radio.example.com/live.m3u" rel="noopener">Open in external player</a>
```

Upload files:

```bash
sudo mkdir -p /var/www/html/radio
sudo cp index.html /var/www/html/radio/
sudo cp artwork-*.png /var/www/html/radio/
sudo chown -R www-data:www-data /var/www/html/radio
```

#### Create M3U Playlist (Optional)

If Icecast doesn't auto-generate `live.m3u`, create one:

```bash
echo "https://radio.example.com/live" | sudo tee /usr/share/icecast2/web/live.m3u
```

#### Static Hosting Alternatives

If you prefer not to run your own nginx:

- **GitHub Pages** → Push to repo, enable Pages  
- **Netlify** → Drag & drop folder  
- **Cloudflare Pages** → Connect Git repo  

**Note:** You'll still need a server for Icecast/Liquidsoap, and you must use `index.html.nocache` with static hosting.

---

## Verify Your Setup

After installation, test each component:

```bash
# 1. Test the picker
sudo -u liquidsoap /usr/local/bin/ls_radio.py pick-next
# Should output a file path

# 2. Check Icecast is running
curl -I http://localhost:8000/live
# Should return 200 OK (or ICY headers)

# 3. Check status JSON
curl http://localhost:8000/status-json.xsl
# Should return JSON metadata

# 4. Check Liquidsoap logs
sudo journalctl -u liquidsoap -n 50 --no-pager

# 5. Test the web player
curl -I https://radio.example.com/radio/
# Should return 200 OK

# 6. Check nginx cache (if using caching)
sudo ls -la /var/cache/nginx/itunes/
# Should show cached files after first artwork lookup
```

---

## How It Works

### Architecture Decisions

#### Why SQLite?

The previous iteration attempted to maintain play history in memory, which led to:
- State loss on restarts
- Race conditions with concurrent requests
- No persistence of separation logic

SQLite provides:
- Persistent state across restarts
- WAL mode for concurrent reads/writes
- Simple file-based storage (no separate DB server)
- Atomic transactions for history tracking

#### Track Selection Algorithm

The picker uses a two-pass approach:

1. **Strict Pass**: Sample 2000 random tracks and return the first that doesn't violate separation rules
2. **Least-Violating Pass**: If all tracks violate rules, pick the one that's "least recently violated"

This ensures:
- Fast selection (constant time)
- Good randomness
- Smart handling of small libraries where rules can't always be satisfied

### Separation Logic

The picker prevents repetition using three rules:

- **Artist Separation** (`LS_ARTIST_SEP_MIN=45`): Same artist won't play again for 45 minutes
- **Title Separation** (`LS_TITLE_SEP_MIN=180`): Same song title won't play again for 3 hours  
- **Track Separation** (`LS_TRACK_SEP_SEC=0`): Same exact file won't play again for X seconds (0=disabled)

**How it works:**
1. When Liquidsoap calls `pick-next`, the picker queries the cache
2. It checks the last play time for artist/title/path against separation windows
3. If a track passes all checks, it's selected immediately
4. If no tracks pass, it selects the "least violating" (oldest last-play timestamp)
5. The selection is stamped in the database
6. When the track actually starts on-air, `track-start` overwrites with the precise timestamp

**Example:** With a 1000-track library and 45-min artist separation:
- If you have 50 unique artists, you'll rarely hit the "least-violating" path
- If you have 5 unique artists, the picker will often choose the artist that's been off-air longest

---

## Features

### Web Player
- One HTML file — no frameworks or dependencies  
- Works on desktop, mobile, and tablet  
- Lock-screen controls via Media Session API  
- Automatic album art via iTunes search  
- Smart reconnection handling  
- Responsive, mobile-first layout  

### Track Selector (`ls_radio.py`)
- Returns immediately even during cache rebuilds
- Background cache refresh for optimal performance
- Configurable artist & title separation windows
- Supports MP3, FLAC, M4A, OGG, WAV, AAC
- Reads metadata via `ffprobe`
- SQLite-backed play history
- Fast random selection with smart sampling
- Prevents multiple simultaneous cache rescans
- No external dependencies (Python stdlib only)

### Audio Processing
- Smooth crossfades between tracks
- Loudness normalization (EBU R128-style)
- Gentle compression for consistent dynamics  
- Brick-wall limiter to prevent clipping  
- Automatic fallback to silence when no tracks available

---

## Customization

### Colors (in `index.html`)

```css
:root {
  --bg: #0b1220;      /* Background */
  --fg: #e8eefc;      /* Text color */
  --muted: #9bb0d0;   /* Muted text */
  --accent: #79a8ff;  /* Buttons/links */
  --card: #121b30;    /* Card background */
}
```

### Station Name

**In `index.html`:**
- Update `<title>` tag
- Change `artist: 'Your Station Name'` in `setMediaSession()`

**In `stream.liq`:**
- Update `name="Your Station Name"`
- Update `url="https://radio.example.com"`

### Audio Processing

Adjust dynamics in `stream.liq`:

```liquidsoap
# Louder, more aggressive sound
radio = normalize(radio, target=-14.0, threshold=-20.0, window=0.5)
radio = compress(radio, threshold=-15.0, ratio=4.0, attack=0.005, release=0.2, gain=2.0)
radio = limit(radio, threshold=-0.5, attack=0.002, release=0.1)

# Gentler, more dynamic sound
radio = normalize(radio, target=-18.0, threshold=-24.0, window=0.5)
radio = compress(radio, threshold=-20.0, ratio=2.0, attack=0.02, release=0.4, gain=1.0)
radio = limit(radio, threshold=-1.0, attack=0.005, release=0.2)
```

### Separation Windows

For large libraries (>10k tracks):
```ini
Environment=LS_ARTIST_SEP_MIN=90
Environment=LS_TITLE_SEP_MIN=360
Environment=LS_TRACK_SEP_SEC=0
```

For small libraries (<500 tracks):
```ini
Environment=LS_ARTIST_SEP_MIN=15
Environment=LS_TITLE_SEP_MIN=60
Environment=LS_TRACK_SEP_SEC=0
```

---

## Performance Tuning

### For Large Libraries (>50k tracks)

```ini
Environment=LS_TOP_N_DIRS=128
Environment=LS_FILES_PER_DIR_TRY=256
Environment=LS_FFPROBE_TIMEOUT_S=1.5
Environment=LS_RESCAN_SEC=172800  # 48 hours
```

Monitor cache rebuild time:
```bash
sudo journalctl -u liquidsoap | grep "cache_builder"
```

### For Small Libraries (<1000 tracks)

```ini
Environment=LS_TOP_N_DIRS=32
Environment=LS_FILES_PER_DIR_TRY=64
Environment=LS_ARTIST_SEP_MIN=15  # Lower separation windows
Environment=LS_TITLE_SEP_MIN=60
```

More aggressive settings cause "least violating" picks more often, which is fine for small libraries.

### For Network-Mounted Music

If your music lives on NFS/SMB:

```ini
Environment=LS_FFPROBE_TIMEOUT_S=2.0
Environment=LS_RESCAN_SEC=43200  # 12 hours (slower network I/O)
```

---

## Database Maintenance

The SQLite database will grow over time as play history accumulates. Periodically vacuum it to reclaim space:

```bash
sudo -u liquidsoap /usr/local/bin/ls_radio.py vacuum
```

Or set up a weekly cron job:

```bash
echo "0 3 * * 0 liquidsoap /usr/local/bin/ls_radio.py vacuum" | sudo crontab -u liquidsoap -
```

**Manual database inspection:**
```bash
sudo sqlite3 /var/lib/liquidsoap/liquidsoap.db
sqlite> SELECT COUNT(*) FROM files;
sqlite> SELECT COUNT(*) FROM last_artist_play;
sqlite> SELECT artist_raw, title_raw, datetime(ts, 'unixepoch') FROM last_artist_play 
        JOIN files ON last_artist_play.artist_norm = files.artist_norm 
        ORDER BY ts DESC LIMIT 10;
```

---

## Troubleshooting

### Stream won't start

**Check Icecast:**
```bash
sudo systemctl status icecast2
sudo journalctl -u icecast2 -n 50
```

**Check Liquidsoap:**
```bash
sudo systemctl status liquidsoap
sudo journalctl -u liquidsoap -n 50 --no-pager
```

**Common issues:**
- Icecast password mismatch between `stream.liq` and `icecast.xml`
- Firewall blocking port 8000
- Liquidsoap can't read music directory

### No songs playing

**Check music directory permissions:**
```bash
sudo -u liquidsoap ls /srv/music
```

**Test the picker manually:**
```bash
sudo -u liquidsoap /usr/local/bin/ls_radio.py pick-next
# Should output a file path, not empty string
```

**Check database:**
```bash
sudo -u liquidsoap sqlite3 /var/lib/liquidsoap/liquidsoap.db "SELECT COUNT(*) FROM files;"
# Should be > 0 after cache build
```

**Rebuild cache manually:**
```bash
sudo -u liquidsoap /usr/local/bin/ls_radio.py rebuild-cache
```

### Player shows "connecting" forever

**Check CORS headers:**
```bash
curl -I https://radio.example.com/live | grep -i access-control
# Should show: Access-Control-Allow-Origin: *
```

**Check browser console** (F12 → Console):
- Mixed content errors? (HTTP stream on HTTPS page)
- CORS errors? (nginx config missing)
- 404 on stream URL? (wrong URL in index.html)

**Verify stream is actually running:**
```bash
curl -I http://localhost:8000/live
# Should return ICY headers or 200 OK
```

### Metadata not updating

**Check status endpoint:**
```bash
curl http://localhost:8000/status-json.xsl | jq .
# Should return valid JSON
```

**Check CORS on status endpoint:**
```bash
curl -I https://radio.example.com/status-json.xsl | grep -i access-control
```

**Check mount point name:**
- Must match in `stream.liq` (`mount="/live"`)
- Must match in `index.html` (`TARGET_MOUNT = '/live'`)
- Must match in nginx (`location = /live`)

### Artwork not loading

**Check nginx cache:**
```bash
sudo ls -la /var/cache/nginx/itunes/
# Should show cache files after first artwork lookup
```

**Check browser console:**
- 404 on `/itunes/search`? nginx config missing
- 404 on `/itunes/art`? nginx config missing
- No errors but still broken? iTunes API rate limiting

**Test iTunes proxy manually:**
```bash
curl "https://radio.example.com/itunes/search?term=test&entity=song&limit=1"
# Should return iTunes API JSON
```

### Same songs keep repeating

**Check library size vs separation windows:**
```bash
# How many tracks?
sudo -u liquidsoap sqlite3 /var/lib/liquidsoap/liquidsoap.db "SELECT COUNT(*) FROM files;"

# How many unique artists?
sudo -u liquidsoap sqlite3 /var/lib/liquidsoap/liquidsoap.db "SELECT COUNT(DISTINCT artist_norm) FROM files;"
```

If you have 5 artists and 45-min artist separation, you'll hear repeats quickly.

**Check database has history:**
```bash
sudo sqlite3 /var/lib/liquidsoap/liquidsoap.db "SELECT COUNT(*) FROM last_artist_play;"
# Should be > 0 after first few tracks
```

**Solution:** Lower separation windows or increase library size.

### Cache rebuilds taking too long

**Monitor rebuild progress:**
```bash
sudo journalctl -u liquidsoap -f
# Watch for messages about cache building
```

**Check disk I/O:**
```bash
sudo apt install sysstat
iostat -x 5
```

**Solutions:**
- Increase `LS_RESCAN_SEC` to reduce rebuild frequency
- Reduce library size or split into multiple directories
- Increase `LS_FFPROBE_TIMEOUT_S` if network-mounted

---

## Common Pitfalls

### Liquidsoap won't start

```bash
# Check music directory permissions
sudo -u liquidsoap ls -la /srv/music

# Verify database directory exists
ls -la /var/lib/liquidsoap

# Check Icecast password matches in stream.liq
grep "password=" /etc/liquidsoap/stream.liq

# Test Liquidsoap config syntax
liquidsoap --check /etc/liquidsoap/stream.liq
```

### Player loads but no audio

- **Mixed content:** Browser blocks HTTP streams on HTTPS pages
  - Solution: Use nginx to proxy the stream over HTTPS
- **Wrong stream URL:** Check `STREAM_URL` in `index.html`
- **Icecast not streaming:** `curl -I http://localhost:8000/live`

### nginx cache not working

```bash
# Check cache directory exists and is writable
ls -la /var/cache/nginx/itunes/
sudo chown -R www-data:www-data /var/cache/nginx

# Check cache is enabled in config
grep "proxy_cache_path" /etc/nginx/nginx.conf

# Test cache headers
curl -I "https://radio.example.com/itunes/search?term=test&entity=song&limit=1"
# Should show: X-Cache-Status: MISS (first time) or HIT (cached)
```

---

## File Structure

```text
radioplayer/
├── index.html              # Web player (with nginx c
├── artwork-512.png         # Fallback album art
├── artwork-192.png
├── artwork-096.png
├─ls_radio.py         # Track selector script
└── config_examples/
    ├── stream.liq          # Liquidsoap config
    ├── icecast.xml         # Icecast config
    ├── nginx               # nginx config
    └── liquidsoap.service  # systemd service
```

---

## Customization

### Colors (in `index.html`)
```css
:root {
  --bg: #0b1220;      /* Background */
  --fg: #e8eefc;      /* Text color */
  --muted: #9bb0d0;   /* Muted text */
  --accent: #79a8ff;  /* Buttons/links */
}
```

### Station Name
- In `index.html`:
  - Update `<title>` tag 
  - Change `artist: 'Your Station Name'` in `setMediaSession()`
- In `stream.liq`:
  - Update `name="Your Station Name"`
   
### Audio Processing
Adjust dynamics in `stream.liq`:
```liquidsoap
radio = normalize(radio)
radio = compress(radio, threshold=-12.0, ratio=3.0, attack=0.005, release=0.2, gain=0.0)
radio = limit(radio, threshold=-1.0, attack=0.002, release=0.2)
```

---
    
## Troubleshooting
    
### Stream won't start
- Check Icecast password matches in both configs
- Verify firewall allows port 8000
- Check logs: `sudo journalctl -u liquidsoap -f`
- Verify Icecast is running: `sudo systemctl status icecast2`

### No songs playing
- Verify `LS_MUSIC_DIR` path is correct and contains audio files
- Check file permissions: `ls -la /srv/music`
- Test picker manually: `python3 /usr/local/bin/ls_radio.py`
- Check Liquidsoap service (sudo journalctl -xeu liquidsoap) logs for errors
  
### Player shows "connecting" forever
- Check CORS headers in nginx config
- Verify stream URL is accessible: `curl -I https://radio.example.com/live`
- Check browser console for JavaScript errors
- Ensure HTTPS is working (mixed content blocks HTTP streams)

### Cache rebuilds taking too long
- Increase `LS_RESCAN_SEC` to reduce rebuild frequency (default: 24 hours)
- Reduce library size or split into multiple directories
- Check disk I/O performance with `iostat`
