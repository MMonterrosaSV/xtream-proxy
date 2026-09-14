```python
import os
import time
import re
import hashlib
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse, RedirectResponse

app = FastAPI(title="M3U -> Xtream API Proxy")


# ============================================================
# CONFIGURATION
# ============================================================

# Your original M3U playlist.
M3U_URL = os.getenv(
    "M3U_URL",
    "https://example.com/your-playlist.m3u"
)

# Xtream credentials.
USERNAME = os.getenv("XTREAM_USER", "demo")
PASSWORD = os.getenv("XTREAM_PASS", "demo")

# How long to keep the upstream M3U in memory.
# Default: 300 seconds = 5 minutes.
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "300"))

# Optional:
# If set, this should be something like:
#
# https://xtream-proxy-xxxx.onrender.com
#
# If not set, the server automatically determines the URL
# from the incoming request.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")


# ============================================================
# CACHE
# ============================================================

_cache = {
    "m3u": None,
    "channels": [],
    "categories": [],
    "fetched_at": 0,
    "last_error": None,
}


NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


# ============================================================
# HELPERS
# ============================================================

def stable_id(name: str, group: str, logo: str = "") -> int:
    """
    Create a stable stream ID.

    IMPORTANT:
    The stream URL is intentionally NOT included.

    This means if the upstream provider changes the actual
    stream URL for SELF LIVE EVENTS, the Xtream stream ID
    remains the same as long as the channel metadata remains
    the same.
    """

    key = f"{name.strip().lower()}|{group.strip().lower()}|{logo.strip()}"

    digest = hashlib.sha1(
        key.encode("utf-8")
    ).hexdigest()

    # Positive integer suitable for IPTV players.
    return int(digest[:8], 16) % 1_000_000_000


def get_server_url(request: Request) -> str:
    """
    Determine the public URL that should be returned in
    Xtream's server_info.

    PUBLIC_URL takes priority.

    Otherwise use the incoming request URL.
    """

    if PUBLIC_URL:
        return PUBLIC_URL

    # Render normally supplies the correct host through
    # the incoming request.
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")

    if forwarded_host:
        scheme = forwarded_proto or "https"
        return f"{scheme}://{forwarded_host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def check_auth(
    username: Optional[str],
    password: Optional[str]
):
    """
    Validate Xtream credentials.
    """

    if username != USERNAME or password != PASSWORD:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )


def parse_attribute(line: str, attribute: str) -> str:
    """
    Extract an M3U attribute.

    Example:

    group-title="Sports"

    returns:

    Sports
    """

    match = re.search(
        rf'{re.escape(attribute)}="([^"]*)"',
        line,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return ""


def extract_channel_name(line: str) -> str:
    """
    Extract channel name from:

    #EXTINF:-1 ... ,Channel Name
    """

    if "," in line:
        return line.split(",", 1)[1].strip()

    return "Channel"


# ============================================================
# M3U FETCH / PARSE
# ============================================================

def fetch_and_parse_m3u(force: bool = False):
    """
    Fetch and parse the upstream M3U.

    Normal behavior:
        Use cached playlist until CACHE_SECONDS expires.

    force=True:
        Immediately fetch a fresh playlist.

    If a refresh fails but an older playlist exists:
        Keep serving the last known good playlist.
    """

    now = time.time()

    cache_is_valid = (
        _cache["m3u"] is not None
        and (now - _cache["fetched_at"]) < CACHE_SECONDS
    )

    if not force and cache_is_valid:
        return (
            _cache["channels"],
            _cache["categories"]
        )

    # --------------------------------------------------------
    # Fetch upstream M3U
    # --------------------------------------------------------

    try:
        cache_buster = int(now)

        separator = "&" if "?" in M3U_URL else "?"

        fetch_url = (
            f"{M3U_URL}"
            f"{separator}_xtream_refresh={cache_buster}"
        )

        headers = {
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131 Safari/537.36"
            ),
        }

        with httpx.Client(
            timeout=45.0,
            follow_redirects=True
        ) as client:

            response = client.get(
                fetch_url,
                headers=headers
            )

            response.raise_for_status()

            content = response.text

    except Exception as e:

        error = str(e)

        _cache["last_error"] = error

        # If we already have a working playlist, DO NOT
        # destroy it because the upstream failed temporarily.
        if _cache["m3u"] is not None:

            return (
                _cache["channels"],
                _cache["categories"]
            )

        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch M3U: {error}"
        )

    # --------------------------------------------------------
    # Parse M3U
    # --------------------------------------------------------

    channels = []

    categories = {}
    next_category_id = 1

    lines = content.splitlines()

    i = 0

    while i < len(lines):

        line = lines[i].strip()

        if not line.startswith("#EXTINF:"):
            i += 1
            continue

        # ----------------------------------------------------
        # Channel metadata
        # ----------------------------------------------------

        name = extract_channel_name(line)

        group = parse_attribute(
            line,
            "group-title"
        )

        if not group:
            group = "Uncategorized"

        logo = parse_attribute(
            line,
            "tvg-logo"
        )

        tvg_id = parse_attribute(
            line,
            "tvg-id"
        )

        # ----------------------------------------------------
        # Find next URL
        # ----------------------------------------------------

        url = ""

        j = i + 1

        while j < len(lines):

            candidate = lines[j].strip()

            if not candidate:
                j += 1
                continue

            if candidate.startswith("#"):
                j += 1
                continue

            url = candidate
            break

        if not url:
            i += 1
            continue

        # ----------------------------------------------------
        # Category
        # ----------------------------------------------------

        if group not in categories:

            categories[group] = next_category_id

            next_category_id += 1

        category_id = categories[group]

        # ----------------------------------------------------
        # Stable Xtream stream ID
        # ----------------------------------------------------

        stream_id = stable_id(
            name,
            group,
            tvg_id or logo
        )

        channel = {
            "num": stream_id,
            "name": name,
            "stream_type": "live",
            "stream_id": stream_id,
            "stream_icon": logo,
            "epg_channel_id": tvg_id,
            "category_id": str(category_id),
            "category_name": group,
            "url": url,
        }

        channels.append(channel)

        # Continue after the URL.
        i = j + 1

    # --------------------------------------------------------
    # Build category list
    # --------------------------------------------------------

    category_list = [
        {
            "category_id": str(category_id),
            "category_name": category_name,
            "parent_id": 0,
        }
        for category_name, category_id
        in categories.items()
    ]

    # --------------------------------------------------------
    # IMPORTANT:
    # Only replace the cache after a successful parse.
    # --------------------------------------------------------

    _cache["m3u"] = content
    _cache["channels"] = channels
    _cache["categories"] = category_list
    _cache["fetched_at"] = time.time()
    _cache["last_error"] = None

    return (
        channels,
        category_list
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():

    return JSONResponse(
        {
            "status": "ok",
            "message": "M3U Xtream proxy is running",
        },
        headers=NO_CACHE_HEADERS,
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    now = time.time()

    cache_age = (
        now - _cache["fetched_at"]
        if _cache["fetched_at"]
        else None
    )

    return JSONResponse(
        {
            "status": "ok",
            "cached": _cache["m3u"] is not None,
            "channels": len(_cache["channels"]),
            "categories": len(_cache["categories"]),
            "cache_age_seconds": cache_age,
            "cache_seconds": CACHE_SECONDS,
            "last_error": _cache["last_error"],
        },
        headers=NO_CACHE_HEADERS,
    )


# ============================================================
# DEBUG
# ============================================================

@app.get("/debug")
async def debug(
    request: Request
):

    server_url = get_server_url(request)

    now = time.time()

    cache_age = (
        now - _cache["fetched_at"]
        if _cache["fetched_at"]
        else None
    )

    return JSONResponse(
        {
            "server_url": server_url,
            "m3u_url_configured": bool(M3U_URL),
            "username_configured": bool(USERNAME),
            "password_configured": bool(PASSWORD),
            "cache_seconds": CACHE_SECONDS,
            "cache_age_seconds": cache_age,
            "channels": len(_cache["channels"]),
            "categories": len(_cache["categories"]),
            "last_error": _cache["last_error"],
            "fetched_at": (
                time.strftime(
                    "%Y-%m-%d %H:%M:%S UTC",
                    time.gmtime(
                        _cache["fetched_at"]
                    )
                )
                if _cache["fetched_at"]
                else None
            ),
        },
        headers=NO_CACHE_HEADERS,
    )


# ============================================================
# XTREAM PLAYER API
# ============================================================

@app.get("/player_api.php")
async def player_api(
    request: Request,
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    refresh: Optional[int] = Query(0),
):

    check_auth(username, password)

    force_refresh = bool(refresh)

    channels, categories = fetch_and_parse_m3u(
        force=force_refresh
    )

    # --------------------------------------------------------
    # Login / account information
    # --------------------------------------------------------

    if action is None:

        server_url = get_server_url(request)

        return JSONResponse(
            {
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
                    "allowed_output_formats": [
                        "m3u8",
                        "ts"
                    ],
                },

                "server_info": {
                    "url": server_url,
                    "port": "443",
                    "https_port": "443",
                    "server_protocol": "https",
                    "rtmp_port": "0",
                    "timezone": "UTC",
                    "timestamp_now": int(time.time()),
                    "time_now": time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
            },
            headers=NO_CACHE_HEADERS,
        )

    # --------------------------------------------------------
    # Categories
    # --------------------------------------------------------

    if action == "get_live_categories":

        return JSONResponse(
            categories,
            headers=NO_CACHE_HEADERS
        )

    # --------------------------------------------------------
    # Live streams
    # --------------------------------------------------------

    if action == "get_live_streams":

        streams = []

        for channel in channels:

            streams.append(
                {
                    "num": channel["num"],
                    "name": channel["name"],
                    "stream_type": "live",
                    "stream_id": channel["stream_id"],
                    "stream_icon": channel["stream_icon"],
                    "epg_channel_id": channel[
                        "epg_channel_id"
                    ],
                    "added": str(
                        int(_cache["fetched_at"])
                    ),
                    "category_id": channel[
                        "category_id"
                    ],
                    "custom_sid": "",
                    "tv_archive": 0,
                    "direct_source": channel["url"],
                    "tv_archive_duration": 0,
                }
            )

        return JSONResponse(
            streams,
            headers=NO_CACHE_HEADERS
        )

    # --------------------------------------------------------
    # Individual stream information
    # --------------------------------------------------------

    if action == "get_short_epg":

        return JSONResponse(
            [],
            headers=NO_CACHE_HEADERS
        )

    # Unknown action
    return JSONResponse(
        [],
        headers=NO_CACHE_HEADERS
    )


# ============================================================
# XTREAM M3U
# ============================================================

@app.get("/get.php")
async def get_php(
    request: Request,
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    type: Optional[str] = Query("m3u_plus"),
    output: Optional[str] = Query("ts"),
    refresh: Optional[int] = Query(0),
):

    check_auth(username, password)

    channels, _ = fetch_and_parse_m3u(
        force=bool(refresh)
    )

    lines = [
        "#EXTM3U"
    ]

    for channel in channels:

        logo = ""

        if channel["stream_icon"]:

            logo = (
                f' tvg-logo="{channel["stream_icon"]}"'
            )

        group = ""

        if channel["category_name"]:

            group = (
                f' group-title="{channel["category_name"]}"'
            )

        lines.append(
            f'#EXTINF:-1{logo}{group},'
            f'{channel["name"]}'
        )

        # Use our Xtream endpoint instead of exposing
        # the original source URL.
        #
        # This makes the returned M3U work consistently
        # through the proxy.

        server_url = get_server_url(request)

        stream_url = (
            f"{server_url}/live/"
            f"{USERNAME}/"
            f"{PASSWORD}/"
            f"{channel['stream_id']}.ts"
        )

        lines.append(stream_url)

    return PlainTextResponse(
        "\n".join(lines),
        media_type="audio/x-mpegurl",
        headers=NO_CACHE_HEADERS,
    )


# ============================================================
# LIVE STREAM
# ============================================================

@app.get(
    "/live/{user}/{passwd}/{stream_id}"
)
@app.get(
    "/live/{user}/{passwd}/{stream_id}.ts"
)
async def live_stream(
    user: str,
    passwd: str,
    stream_id: int,
):

    check_auth(
        user,
        passwd
    )

    # Do NOT force a refresh here.
    #
    # Stream playback should use the existing channel list.
    # The playlist/API refresh controls when the channel
    # metadata is updated.

    channels, _ = fetch_and_parse_m3u()

    for channel in channels:

        if channel["stream_id"] == stream_id:

            return RedirectResponse(
                channel["url"],
                status_code=302,
            )

    raise HTTPException(
        status_code=404,
        detail="Stream not found"
    )


# ============================================================
# MANUAL REFRESH ENDPOINT
# ============================================================

@app.get("/refresh")
async def manual_refresh(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
):

    check_auth(
        username,
        password
    )

    channels, categories = fetch_and_parse_m3u(
        force=True
    )

    return JSONResponse(
        {
            "status": "refreshed",
            "channels": len(channels),
            "categories": len(categories),
            "fetched_at": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC",
                time.gmtime(
                    _cache["fetched_at"]
                )
            ),
        },
        headers=NO_CACHE_HEADERS,
    )
```
