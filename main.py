import os
import time
import re
from typing import Optional, Dict, List, Tuple
from urllib.parse import urljoin
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
import httpx

app = FastAPI(title="Simple M3U → Xtream API (Live + Movies + Series)")

# === CONFIG (use Render Environment Variables in production) ===
# Live channels (your existing sources)
_raw_live = os.getenv("M3U_URLS", os.getenv("M3U_URL", "https://example.com/your-playlist.m3u"))
LIVE_M3U_URLS = [u.strip() for u in _raw_live.split(",") if u.strip()]

# Movies & Series (defaults to the two sources you asked for)
MOVIES_M3U_URL = os.getenv(
    "MOVIES_M3U_URL",
    "https://raw.githubusercontent.com/MMonterrosaSV/IPTV/refs/heads/main/MOVIES"
)
SERIES_M3U_URL = os.getenv(
    "SERIES_M3U_URL",
    "https://raw.githubusercontent.com/MMonterrosaSV/IPTV/refs/heads/main/TV%20SHOWS"
)

USERNAME = os.getenv("XTREAM_USER", "demo")
PASSWORD = os.getenv("XTREAM_PASS", "demo")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "60"))
SERVER_URL = os.getenv("SERVER_URL", "your-render-url.onrender.com")

UPSTREAM_HEADERS = {
    "User-Agent": os.getenv("UPSTREAM_USER_AGENT", "VLC/3.0.20 LibVLC/3.0.20"),
}

# In-memory caches
_cache = {
    "live_channels": [],
    "live_categories": [],
    "vod_streams": [],
    "vod_categories": [],
    "series_list": [],          # list of series objects
    "series_categories": [],
    "series_episodes": {},      # series_id -> list of episodes
    "fetched_at": 0,
}


def _get_category_id(group_name: str, category_ids: dict, categories: list) -> str:
    if group_name not in category_ids:
        new_id = str(len(category_ids) + 1)
        category_ids[group_name] = new_id
        categories.append({
            "category_id": new_id,
            "category_name": group_name,
            "parent_id": 0,
        })
    return category_ids[group_name]


def _parse_m3u_text(
    content: str,
    live_channels: list,
    live_cat_ids: dict,
    live_categories: list,
    vod_streams: list,
    vod_cat_ids: dict,
    vod_categories: list,
    series_map: dict,          # name -> series dict
    series_cat_ids: dict,
    series_categories: list,
    next_live_id: int,
    next_vod_id: int,
    next_series_id: int,
) -> Tuple[int, int, int]:
    """Parse one M3U. Returns updated (next_live_id, next_vod_id, next_series_id)."""
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            # Extract attributes
            name_match = re.search(r",(.+)$", line)
            name = name_match.group(1).strip() if name_match else "Unknown"

            group = "Uncategorized"
            logo = ""
            item_type = "live"  # default

            group_match = re.search(r'group-title="([^"]*)"', line)
            if group_match and group_match.group(1).strip():
                group = group_match.group(1).strip()

            logo_match = re.search(r'tvg-logo="([^"]*)"', line)
            if logo_match:
                logo = logo_match.group(1)

            type_match = re.search(r'type="([^"]*)"', line)
            if type_match:
                item_type = type_match.group(1).lower()

            # Next non-empty line = URL
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i >= len(lines):
                break
            url = lines[i].strip()
            if not url or url.startswith("#"):
                i += 1
                continue

            if item_type == "movie":
                cat_id = _get_category_id(group, vod_cat_ids, vod_categories)
                vod_streams.append({
                    "num": next_vod_id,
                    "name": name,
                    "stream_type": "movie",
                    "stream_id": next_vod_id,
                    "stream_icon": logo,
                    "rating": "0",
                    "rating_5based": 0,
                    "added": str(int(time.time())),
                    "category_id": cat_id,
                    "container_extension": "mp4",
                    "custom_sid": "",
                    "direct_source": url,
                    "url": url,
                })
                next_vod_id += 1

            elif item_type == "series":
                # Try to extract series name + SxxExx from the title
                # e.g. "A KNIGHT OF THE SEVEN KINGDOMS S01E01 The Hedge Knight"
                series_name = name
                season_num = 1
                episode_num = 1
                episode_title = name

                se_match = re.search(r"(.+?)\s+S(\d+)E(\d+)\s*(.*)$", name, re.IGNORECASE)
                if se_match:
                    series_name = se_match.group(1).strip()
                    season_num = int(se_match.group(2))
                    episode_num = int(se_match.group(3))
                    episode_title = se_match.group(4).strip() or f"Episode {episode_num}"

                cat_id = _get_category_id(group, series_cat_ids, series_categories)

                if series_name not in series_map:
                    series_map[series_name] = {
                        "num": next_series_id,
                        "name": series_name,
                        "series_id": next_series_id,
                        "cover": logo,
                        "plot": "",
                        "cast": "",
                        "director": "",
                        "genre": group,
                        "releaseDate": "",
                        "last_modified": str(int(time.time())),
                        "rating": "0",
                        "rating_5based": 0,
                        "backdrop_path": [],
                        "youtube_trailer": "",
                        "episode_run_time": "0",
                        "category_id": cat_id,
                        "episodes": {},  # season -> list of episodes
                    }
                    next_series_id += 1

                series = series_map[series_name]
                season_key = str(season_num)
                if season_key not in series["episodes"]:
                    series["episodes"][season_key] = []

                # episode id = unique across everything (we reuse next_vod_id style)
                ep_id = next_vod_id
                next_vod_id += 1

                series["episodes"][season_key].append({
                    "id": str(ep_id),
                    "episode_num": episode_num,
                    "title": episode_title,
                    "container_extension": "mp4",
                    "info": {
                        "movie_image": logo,
                        "plot": "",
                        "releasedate": "",
                        "rating": 0,
                    },
                    "custom_sid": "",
                    "added": str(int(time.time())),
                    "season": season_num,
                    "direct_source": url,
                    "url": url,
                })

            else:
                # live (default)
                cat_id = _get_category_id(group, live_cat_ids, live_categories)
                live_channels.append({
                    "num": next_live_id,
                    "name": name,
                    "stream_type": "live",
                    "stream_id": next_live_id,
                    "stream_icon": logo,
                    "epg_channel_id": "",
                    "category_id": cat_id,
                    "category_name": group,
                    "url": url,
                })
                next_live_id += 1

        i += 1

    return next_live_id, next_vod_id, next_series_id


def fetch_and_parse_all():
    now = time.time()
    if _cache["live_channels"] and (now - _cache["fetched_at"]) < CACHE_SECONDS:
        return

    live_channels = []
    live_cat_ids = {}
    live_categories = []
    vod_streams = []
    vod_cat_ids = {}
    vod_categories = []
    series_map = {}
    series_cat_ids = {}
    series_categories = []

    next_live_id = 1
    next_vod_id = 1
    next_series_id = 1

    errors = []

    all_urls = LIVE_M3U_URLS + [MOVIES_M3U_URL, SERIES_M3U_URL]

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in all_urls:
            if not url:
                continue
            try:
                r = client.get(url, headers=UPSTREAM_HEADERS)
                r.raise_for_status()
                next_live_id, next_vod_id, next_series_id = _parse_m3u_text(
                    r.text,
                    live_channels, live_cat_ids, live_categories,
                    vod_streams, vod_cat_ids, vod_categories,
                    series_map, series_cat_ids, series_categories,
                    next_live_id, next_vod_id, next_series_id,
                )
            except Exception as e:
                errors.append(f"{url}: {e}")

    if not live_channels and not vod_streams and not series_map and errors:
        raise HTTPException(status_code=502, detail=f"Failed to fetch any M3U source: {'; '.join(errors)}")

    # Build final series list + episodes lookup
    series_list = list(series_map.values())
    series_episodes = {}
    for s in series_list:
        series_episodes[s["series_id"]] = s["episodes"]

    _cache["live_channels"] = live_channels
    _cache["live_categories"] = live_categories
    _cache["vod_streams"] = vod_streams
    _cache["vod_categories"] = vod_categories
    _cache["series_list"] = series_list
    _cache["series_categories"] = series_categories
    _cache["series_episodes"] = series_episodes
    _cache["fetched_at"] = now


def check_auth(username: Optional[str], password: Optional[str]):
    if username != USERNAME or password != PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "M3U Xtream proxy is running (Live + Movies + Series)",
        "live_sources": len(LIVE_M3U_URLS),
        "movies_url": MOVIES_M3U_URL,
        "series_url": SERIES_M3U_URL,
    }


@app.get("/player_api.php")
async def player_api(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    series_id: Optional[str] = Query(None),
    vod_id: Optional[str] = Query(None),
):
    check_auth(username, password)
    fetch_and_parse_all()

    # Login / account info
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
                "allowed_output_formats": ["m3u8", "ts", "mp4"],
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

    # ---------- LIVE ----------
    if action == "get_live_categories":
        return JSONResponse(_cache["live_categories"])

    if action == "get_live_streams":
        streams = []
        for ch in _cache["live_channels"]:
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

    # ---------- VOD / MOVIES ----------
    if action == "get_vod_categories":
        return JSONResponse(_cache["vod_categories"])

    if action == "get_vod_streams":
        return JSONResponse(_cache["vod_streams"])

    if action == "get_vod_info":
        # optional, some players ask for it
        if not vod_id:
            return JSONResponse({})
        for v in _cache["vod_streams"]:
            if str(v["stream_id"]) == str(vod_id):
                return JSONResponse({
                    "info": {
                        "name": v["name"],
                        "movie_image": v["stream_icon"],
                        "plot": "",
                        "cast": "",
                        "director": "",
                        "genre": "",
                        "releaseDate": "",
                        "rating": v.get("rating", "0"),
                        "duration_secs": 0,
                        "duration": "00:00:00",
                    },
                    "movie_data": {
                        "stream_id": v["stream_id"],
                        "name": v["name"],
                        "added": v["added"],
                        "category_id": v["category_id"],
                        "container_extension": "mp4",
                        "custom_sid": "",
                        "direct_source": v["url"],
                    }
                })
        return JSONResponse({})

    # ---------- SERIES ----------
    if action == "get_series_categories":
        return JSONResponse(_cache["series_categories"])

    if action == "get_series":
        # Return list of series (without full episode data)
        result = []
        for s in _cache["series_list"]:
            result.append({
                "num": s["num"],
                "name": s["name"],
                "series_id": s["series_id"],
                "cover": s["cover"],
                "plot": s["plot"],
                "cast": s["cast"],
                "director": s["director"],
                "genre": s["genre"],
                "releaseDate": s["releaseDate"],
                "last_modified": s["last_modified"],
                "rating": s["rating"],
                "rating_5based": s["rating_5based"],
                "backdrop_path": s["backdrop_path"],
                "youtube_trailer": s["youtube_trailer"],
                "episode_run_time": s["episode_run_time"],
                "category_id": s["category_id"],
            })
        return JSONResponse(result)

    if action == "get_series_info":
        if not series_id:
            return JSONResponse({})
        sid = int(series_id)
        for s in _cache["series_list"]:
            if s["series_id"] == sid:
                return JSONResponse({
                    "seasons": [
                        {"season_number": int(k), "name": f"Season {k}", "cover": s["cover"]}
                        for k in sorted(s["episodes"].keys(), key=int)
                    ],
                    "info": {
                        "name": s["name"],
                        "cover": s["cover"],
                        "plot": s["plot"],
                        "cast": s["cast"],
                        "director": s["director"],
                        "genre": s["genre"],
                        "releaseDate": s["releaseDate"],
                        "last_modified": s["last_modified"],
                        "rating": s["rating"],
                        "rating_5based": s["rating_5based"],
                        "backdrop_path": s["backdrop_path"],
                        "youtube_trailer": s["youtube_trailer"],
                        "episode_run_time": s["episode_run_time"],
                        "category_id": s["category_id"],
                    },
                    "episodes": s["episodes"],
                })
        return JSONResponse({})

    return JSONResponse([])


@app.get("/get.php")
async def get_php(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    type: Optional[str] = Query("m3u_plus"),
    output: Optional[str] = Query("ts"),
):
    check_auth(username, password)
    fetch_and_parse_all()

    lines = ["#EXTM3U"]

    # Live
    for ch in _cache["live_channels"]:
        logo = f' tvg-logo="{ch["stream_icon"]}"' if ch["stream_icon"] else ""
        group = f' group-title="{ch["category_name"]}"' if ch["category_name"] else ""
        lines.append(f'#EXTINF:-1{logo}{group},{ch["name"]}')
        lines.append(ch["url"])

    # Movies
    for v in _cache["vod_streams"]:
        logo = f' tvg-logo="{v["stream_icon"]}"' if v["stream_icon"] else ""
        group = f' group-title="Movies"'  # or use real category if you prefer
        lines.append(f'#EXTINF:-1 type="movie"{logo}{group},{v["name"]}')
        lines.append(v["url"])

    # Series episodes
    for s in _cache["series_list"]:
        for season, eps in s["episodes"].items():
            for ep in eps:
                logo = f' tvg-logo="{ep["info"]["movie_image"]}"' if ep["info"]["movie_image"] else ""
                title = f'{s["name"]} S{season.zfill(2)}E{str(ep["episode_num"]).zfill(2)} {ep["title"]}'
                lines.append(f'#EXTINF:-1 type="series"{logo} group-title="{s["genre"]}",{title}')
                lines.append(ep["url"])

    return PlainTextResponse("\n".join(lines), media_type="audio/x-mpegurl")


# ---------- Stream proxies ----------

async def _proxy_stream(target_url: str):
    looks_like_hls = target_url.split("?")[0].endswith(".m3u8")
    if looks_like_hls:
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
                rewritten_lines.append(urljoin(target_url, stripped))
            else:
                rewritten_lines.append(line)
        return PlainTextResponse(
            "\n".join(rewritten_lines),
            media_type="application/vnd.apple.mpegurl",
        )

    async def proxy_bytes():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers=UPSTREAM_HEADERS) as client:
            async with client.stream("GET", target_url) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(proxy_bytes(), media_type="video/mp2t")


@app.get("/live/{user}/{passwd}/{stream_id_ext}")
async def live_stream(user: str, passwd: str, stream_id_ext: str):
    check_auth(user, passwd)
    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    stream_id = int(match.group(1))

    fetch_and_parse_all()
    for ch in _cache["live_channels"]:
        if ch["stream_id"] == stream_id:
            return await _proxy_stream(ch["url"])
    raise HTTPException(status_code=404, detail="Stream not found")


@app.get("/movie/{user}/{passwd}/{stream_id_ext}")
async def movie_stream(user: str, passwd: str, stream_id_ext: str):
    check_auth(user, passwd)
    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    stream_id = int(match.group(1))

    fetch_and_parse_all()
    for v in _cache["vod_streams"]:
        if v["stream_id"] == stream_id:
            return await _proxy_stream(v["url"])
    raise HTTPException(status_code=404, detail="Movie not found")


@app.get("/series/{user}/{passwd}/{stream_id_ext}")
async def series_stream(user: str, passwd: str, stream_id_ext: str):
    """
    Xtream players request episodes as /series/USER/PASS/{episode_id}.ext
    """
    check_auth(user, passwd)
    match = re.match(r"^(\d+)", stream_id_ext)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid stream id")
    episode_id = match.group(1)

    fetch_and_parse_all()
    for series_id, seasons in _cache["series_episodes"].items():
        for season, eps in seasons.items():
            for ep in eps:
                if str(ep["id"]) == episode_id:
                    return await _proxy_stream(ep["url"])
    raise HTTPException(status_code=404, detail="Episode not found")
