import os
import time
import re
from typing import Optional, List

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
import httpx

app = FastAPI(title="Simple M3U → Xtream API")

# === CONFIG (use Render Environment Variables in production) ===
# Comma-separated list of M3U playlist URLs
M3U_URLS_RAW = os.getenv("M3U_URLS", os.getenv("M3U_URL", "https://example.com/your-playlist.m3u"))
M3U_URLS: List[str] = [u.strip() for u in M3U_URLS_RAW.split(",") if u.strip()]

USERNAME = os.getenv("XTREAM_USER", "demo")
PASSWORD = os.getenv("XTREAM_PASS", "demo")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "300"))  # refresh interval

# Simple in-memory cache
_cache = {"channels": [], "fetched_at": 0, "sources_ok": 0, "sources_failed": []}


def _parse_m3u(content: str, start_id: int):
    """Parse a single M3U's text content into channel dicts, starting stream_id at start_id."""
    channels = []
    lines = content.splitlines()
    i = 0
    stream_id = start_id
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            name_match = re.search(r",(.+)$", line)
            name = name_match.group(1).strip() if name_match else f"Channel {stream_id}"

            group = "Uncategorized"
            group_match = re.search(r'group-title="([^"]*)"', line)
            if group_match:
                group = group_match.group(1)

            logo = ""
            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            if logo_match:
                logo = logo_match.group(1)

            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    channels.append({
                        "num": stream_id,
                        "name": name,
                        "stream_type": "live",
                        "stream_id": stream_id,
                        "stream_icon": logo,
                        "epg_channel_id": "",
                        "category_id": "1",
                        "category_name": group,
                        "url": url,
                    })
                    stream_id += 1
        i += 1
    return channels, stream_id


def fetch_and_parse_m3u():
    now = time.time()
    if _cache["channels"] and (now - _cache["fetched_at"]) < CACHE_SECONDS:
        return _cache["channels"]

    all_channels = []
    next_id = 1
    ok_count = 0
    failed = []

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in M3U_URLS:
            try:
                r = client.get(
                    url,
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                    params={"_": int(now)},  # cache-bust CDNs/proxies in front of the source
                )
                r.raise_for_status()
                content = r.text
            except Exception as e:
                failed.append({"url": url, "error": str(e)})
                continue

            parsed, next_id = _parse_m3u(content, next_id)
            all_channels.extend(parsed)
            ok_count += 1

    # Only replace the cache if at least one source succeeded — never wipe
    # existing good data because every source happened to fail once.
    if ok_count > 0:
        _cache["channels"] = all_channels
        _cache["fetched_at"] = now
        _cache["sources_ok"] = ok_count
        _cache["sources_failed"] = failed
    else:
        # keep serving stale cache, but record the failure
        _cache["sources_failed"] = failed
        if not _cache["channels"]:
            raise HTTPException(status_code=502, detail=f"Failed to fetch all M3U sources: {failed}")

    return _cache["channels"]


def check_auth(username: Optional[str], password: Optional[str]):
    if username != USERNAME or password != PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/")
def root():
    return {"status": "ok", "message": "M3U Xtream proxy is running"}


@app.get("/debug/cache")
def debug_cache():
    """Unauthenticated cache-status endpoint — safe to hit from your cron/uptime pinger."""
    now = time.time()
    return {
        "m3u_sources_configured": M3U_URLS,
        "sources_ok_last_fetch": _cache["sources_ok"],
        "sources_failed_last_fetch": _cache["sources_failed"],
        "fetched_at": _cache["fetched_at"],
        "age_seconds": (now - _cache["fetched_at"]) if _cache["fetched_at"] else None,
        "cache_seconds": CACHE_SECONDS,
        "channel_count": len(_cache["channels"]),
    }


@app.get("/refresh")
def force_refresh():
    """Unauthenticated manual refresh trigger — point your cron here instead of '/'."""
    _cache["fetched_at"] = 0  # force fetch_and_parse_m3u() to treat cache as expired
    channels = fetch_and_parse_m3u()
    return {"status": "refreshed", "channel_count": len(channels), "fetched_at": _cache["fetched_at"]}


@app.get("/player_api.php")
async def player_api(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
):
    check_auth(username, password)
    channels = fetch_and_parse_m3u()

    if action == "get_live_categories":
        cats = [{"category_id": "1", "category_name": "All Channels", "parent_id": 0}]
        return JSONResponse(cats)

    if action == "get_live_streams":
        streams = []
        for ch in channels:
            streams.append({
                "num": ch["num"],
                "name": ch["name"],
                "stream_type": "live",
                "stream_id": ch["stream_id"],
                "stream_icon": ch["stream_icon"],
                "epg_channel_id": "",
                "added": str(int(time.time())),
                "category_id": "1",
                "custom_sid": "",
                "tv_archive": 0,
                "direct_source": ch["url"],
                "tv_archive_duration": 0,
            })
        return JSONResponse(streams)

    if action is None:
        # Login/handshake call — this MUST return user_info/server_info,
        # not a channel or category list, or players will reject it as
        # an invalid/malformed response.
        return JSONResponse({
            "user_info": {
                "username": USERNAME,
                "password": PASSWORD,
                "message": "Active",
                "auth": 1,
                "status": "Active",
                "exp_date": "4102444800",
                "is_trial": "0",
                "active_cons": "0",
                "created_at": "1609459200",
                "max_connections": "1",
                "allowed_output_formats": ["m3u8", "ts"],
            },
            "server_info": {
                "url": os.getenv("SERVER_URL", "your-render-url.onrender.com"),
                "port": "443",
                "https_port": "443",
                "server_protocol": "https",
                "rtmp_port": "0",
                "timezone": "UTC",
                "timestamp_now": int(time.time()),
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        })

    return JSONResponse([])


@app.get("/get.php")
async def get_php(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    type: Optional[str] = Query("m3u_plus"),
    output: Optional[str] = Query("ts"),
):
    check_auth(username, password)
    channels = fetch_and_parse_m3u()

    lines = ["#EXTM3U"]
    for ch in channels:
        logo = f' tvg-logo="{ch["stream_icon"]}"' if ch["stream_icon"] else ""
        group = f' group-title="{ch["category_name"]}"' if ch["category_name"] else ""
        lines.append(f'#EXTINF:-1{logo}{group},{ch["name"]}')
        lines.append(ch["url"])

    return PlainTextResponse("\n".join(lines), media_type="audio/x-mpegurl")


@app.get("/live/{user}/{passwd}/{stream_id}")
async def live_stream(user: str, passwd: str, stream_id: int):
    check_auth(user, passwd)
    channels = fetch_and_parse_m3u()
    for ch in channels:
        if ch["stream_id"] == stream_id:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(ch["url"])
    raise HTTPException(status_code=404, detail="Stream not found")
