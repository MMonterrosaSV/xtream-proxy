import os
import time
import re
from typing import Optional
from urllib.parse import urljoin
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
import httpx

app = FastAPI(title="Simple M3U → Xtream API")

# === CONFIG (use Render Environment Variables in production) ===
# Comma-separated list of M3U URLs to merge, e.g.:
#   M3U_URLS=https://site1.com/list.m3u,https://site2.com/list.m3u,https://site3.com/list.m3u
# M3U_URL (singular) is still read as a fallback for backwards compatibility.
_raw_urls = os.getenv("M3U_URLS", os.getenv("M3U_URL", "https://example.com/your-playlist.m3u"))
M3U_URLS = [u.strip() for u in _raw_urls.split(",") if u.strip()]

USERNAME = os.getenv("XTREAM_USER", "demo")
PASSWORD = os.getenv("XTREAM_PASS", "demo")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "600"))  # refresh every 10 min
SERVER_URL = os.getenv("SERVER_URL", "your-render-url.onrender.com")  # set this to your real deployed host

# Some providers silently block requests that don't look like a real player.
# Send a common player User-Agent on outbound requests instead of httpx's default.
UPSTREAM_HEADERS = {
    "User-Agent": os.getenv("UPSTREAM_USER_AGENT", "VLC/3.0.20 LibVLC/3.0.20"),
}

# Simple in-memory cache
_cache = {"channels": [], "categories": [], "fetched_at": 0}


def _parse_m3u_text(content: str, channels: list, category_ids: dict, categories: list, next_stream_id: int) -> int:
    """Parse one M3U's text content, appending into the shared channels/categories
    lists. Returns the next available stream_id so IDs stay unique across sources."""

    def get_category_id(group_name: str) -> str:
        if group_name not in category_ids:
            new_id = str(len(category_ids) + 1)
            category_ids[group_name] = new_id
            categories.append({
                "category_id": new_id,
                "category_name": group_name,
                "parent_id": 0,
            })
        return category_ids[group_name]

    lines = content.splitlines()
    i = 0
    stream_id = next_stream_id

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#EXTINF:"):
            name_match = re.search(r",(.+)$", line)
            name = name_match.group(1).strip() if name_match else f"Channel {stream_id}"

            group = "Uncategorized"
            logo = ""

            group_match = re.search(r'group-title="([^"]*)"', line)
            if group_match and group_match.group(1).strip():
                group = group_match.group(1).strip()

            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            if logo_match:
                logo = logo_match.group(1)

            category_id = get_category_id(group)

            # Next non-empty line should be the URL
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
                        "category_id": category_id,
                        "category_name": group,
                        "url": url,  # original stream URL
                    })
                    stream_id += 1
        i += 1

    return stream_id


def fetch_and_parse_m3u():
    now = time.time()
    if _cache["channels"] and (now - _cache["fetched_at"]) < CACHE_SECONDS:
        return _cache["channels"], _cache["categories"]

    channels = []
    category_ids = {}   # group_name -> category_id (shared across all sources)
    categories = []
    next_stream_id = 1

    errors = []
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in M3U_URLS:
            try:
                r = client.get(url)
                r.raise_for_status()
                next_stream_id = _parse_m3u_text(r.text, channels, category_ids, categories, next_stream_id)
            except Exception as e:
                # Don't let one bad source take down the whole playlist —
                # just skip it and keep going with the others.
                errors.append(f"{url}: {e}")

    if not channels and errors:
        raise HTTPException(status_code=502, detail=f"Failed to fetch any M3U source: {'; '.join(errors)}")

    _cache["channels"] = channels
    _cache["categories"] = categories
    _cache["fetched_at"] = now
    return channels, categories


def check_auth(username: Optional[str], password: Optional[str]):
    if username != USERNAME or password != PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/")
def root():
    return {"status": "ok", "message": "M3U Xtream proxy is running", "sources": len(M3U_URLS)}


@app.get("/player_api.php")
async def player_api(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
):
    check_auth(username, password)
    channels, categories = fetch_and_parse_m3u()

    # No action = the initial "login" request every Xtream player makes.
    # Must return the user_info/server_info object, not a list.
    if action is None:
        return JSONResponse({
            "user_info": {
                "username": USERNAME,
                "password": PASSWORD,
                "message": "Active",
                "auth": 1,
                "status": "Active",
                "exp_date": "4102444800",  # far future
                "is_trial": "0",
                "active_cons": "0",
                "created_at": "1609459200",
                "max_connections": "1",
                "allowed_output_formats": ["m3u8", "ts"],
            },
            "server_info": {
                "url": SERVER_URL,
                "port": "443",
                "https_port": "443",
                "server_protocol": "https",
                "rtmp_port": "0",
                "timezone": "UTC",
                "timestamp_now": int(time.time()),
                "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        })

    if action == "get_live_categories":
        return JSONResponse(categories)

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
        return JSONResponse(streams)

    return JSONResponse([])


@app.get("/get.php")
async def get_php(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    type: Optional[str] = Query("m3u_plus"),
    output: Optional[str] = Query("ts"),
):
    check_auth(username, password)
    channels, _ = fetch_and_parse_m3u()

    lines = ["#EXTM3U"]
    for ch in channels:
        logo = f' tvg-logo="{ch["stream_icon"]}"' if ch["stream_icon"] else ""
        group = f' group-title="{ch["category_name"]}"' if ch["category_name"] else ""
        lines.append(f'#EXTINF:-1{logo}{group},{ch["name"]}')
        lines.append(ch["url"])

    return PlainTextResponse("\n".join(lines), media_type="audio/x-mpegurl")


# --- Live stream proxy ---
# Accepts /live/USER/PASS/123 or /live/USER/PASS/123.ts or .m3u8 — players
# commonly append a file extension, so we strip it instead of failing to match.
@app.get("/live/{user}/{passwd}/{stream_id_ext}")
async def live_stream(user: str, passwd: str, stream_id_ext: str):
    check_auth(user, passwd)

    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    stream_id = int(match.group(1))

    channels, _ = fetch_and_parse_m3u()
    target_url = None
    for ch in channels:
        if ch["stream_id"] == stream_id:
            target_url = ch["url"]
            break

    if not target_url:
        raise HTTPException(status_code=404, detail="Stream not found")

    looks_like_hls = target_url.split("?")[0].endswith(".m3u8")

    if looks_like_hls:
        # HLS manifest: fetch the text, then rewrite every segment/sub-playlist
        # reference to an ABSOLUTE url (relative to the real source). Relative
        # paths would otherwise resolve against OUR server and 404, since the
        # player thinks it got the manifest from us.
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
                resp = await client.get(target_url)
                resp.raise_for_status()
                manifest_text = resp.text
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch HLS manifest: {e}")

        rewritten_lines = []
        for line in manifest_text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                # This line is a segment or sub-playlist URL — make it absolute
                rewritten_lines.append(urljoin(target_url, stripped))
            else:
                rewritten_lines.append(line)

        return PlainTextResponse(
            "\n".join(rewritten_lines),
            media_type="application/vnd.apple.mpegurl",
        )

    # Raw continuous stream (.ts or unspecified) — proxy bytes directly
    async def proxy_bytes():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
            async with client.stream("GET", target_url) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(proxy_bytes(), media_type="video/mp2t")
