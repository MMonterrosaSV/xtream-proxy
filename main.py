import os
import time
import re
import hashlib
from typing import Optional

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
import httpx

app = FastAPI(title="Simple M3U -> Xtream API")

# === CONFIG (use Render Environment Variables in production) ===
M3U_URL = os.getenv("M3U_URL", "https://example.com/your-playlist.m3u")  # <-- your real M3U URL
USERNAME = os.getenv("XTREAM_USER", "demo")
PASSWORD = os.getenv("XTREAM_PASS", "demo")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "300"))  # refresh every 5 min

# Simple in-memory cache
_cache = {"m3u": None, "channels": [], "categories": [], "fetched_at": 0}

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def stable_id(*parts: str) -> int:
    """
    Deterministic ID derived from stable channel attributes (name + url),
    NOT from position in the file. This keeps stream_id consistent across
    refetches even if the upstream M3U reorders entries, so players don't
    end up pointing at the wrong (or a "not updated") channel.
    """
    key = "|".join(parts)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    # Keep it within a safe positive int range for players that assume int IDs
    return int(digest[:8], 16) % 1_000_000_000


def fetch_and_parse_m3u(force: bool = False):
    now = time.time()
    if not force and _cache["m3u"] and (now - _cache["fetched_at"]) < CACHE_SECONDS:
        return _cache["channels"], _cache["categories"]

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            # Cache-bust the upstream request too, in case the M3U host
            # or any CDN in front of it caches based on the URL.
            r = client.get(M3U_URL, headers={"Cache-Control": "no-cache"})
            r.raise_for_status()
            content = r.text
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch M3U: {e}")

    channels = []
    categories = {}  # name -> category_id
    next_cat_id = 1

    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            name_match = re.search(r",(.+)$", line)
            name = name_match.group(1).strip() if name_match else "Channel"

            group = "Uncategorized"
            group_match = re.search(r'group-title="([^"]*)"', line)
            if group_match:
                group = group_match.group(1).strip() or "Uncategorized"

            logo = ""
            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            if logo_match:
                logo = logo_match.group(1)

            # Next non-empty line should be the URL
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1

            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    if group not in categories:
                        categories[group] = next_cat_id
                        next_cat_id += 1
                    cat_id = categories[group]

                    sid = stable_id(name, url)
                    channels.append({
                        "num": sid,
                        "name": name,
                        "stream_type": "live",
                        "stream_id": sid,
                        "stream_icon": logo,
                        "epg_channel_id": "",
                        "category_id": str(cat_id),
                        "category_name": group,
                        "url": url,
                    })
                i += 1
        else:
            i += 1

    category_list = [
        {"category_id": str(cid), "category_name": name, "parent_id": 0}
        for name, cid in categories.items()
    ]

    _cache["m3u"] = content
    _cache["channels"] = channels
    _cache["categories"] = category_list
    _cache["fetched_at"] = now
    return channels, category_list


def check_auth(username: Optional[str], password: Optional[str]):
    if username != USERNAME or password != PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/")
def root():
    return {"status": "ok", "message": "M3U Xtream proxy is running"}


@app.get("/player_api.php")
async def player_api(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    refresh: Optional[int] = Query(0),
):
    check_auth(username, password)
    channels, categories = fetch_and_parse_m3u(force=bool(refresh))

    if action == "get_live_categories":
        return JSONResponse(categories, headers=NO_CACHE_HEADERS)

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
                "category_id": ch["category_id"],
                "custom_sid": "",
                "tv_archive": 0,
                "direct_source": ch["url"],
                "tv_archive_duration": 0,
            })
        return JSONResponse(streams, headers=NO_CACHE_HEADERS)

    if action is None:
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
                "url": "your-render-url.onrender.com",
                "port": "443",
                "https_port": "443",
                "server_protocol": "https",
                "rtmp_port": "0",
                "timezone": "UTC",
                "timestamp_now": int(time.time()),
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        }, headers=NO_CACHE_HEADERS)

    return JSONResponse([], headers=NO_CACHE_HEADERS)


@app.get("/get.php")
async def get_php(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    type: Optional[str] = Query("m3u_plus"),
    output: Optional[str] = Query("ts"),
    refresh: Optional[int] = Query(0),
):
    check_auth(username, password)
    channels, _ = fetch_and_parse_m3u(force=bool(refresh))

    lines = ["#EXTM3U"]
    for ch in channels:
        logo = f' tvg-logo="{ch["stream_icon"]}"' if ch["stream_icon"] else ""
        group = f' group-title="{ch["category_name"]}"' if ch["category_name"] else ""
        lines.append(f'#EXTINF:-1{logo}{group},{ch["name"]}')
        lines.append(ch["url"])

    return PlainTextResponse(
        "\n".join(lines),
        media_type="audio/x-mpegurl",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/live/{user}/{passwd}/{stream_id}")
async def live_stream(user: str, passwd: str, stream_id: int):
    check_auth(user, passwd)
    channels, _ = fetch_and_parse_m3u()
    for ch in channels:
        if ch["stream_id"] == stream_id:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(ch["url"])
    raise HTTPException(status_code=404, detail="Stream not found")
