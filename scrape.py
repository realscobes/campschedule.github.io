import json
import os
import time
from collections import deque

import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("TMDB_ACCESS_TOKEN")
API_HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
}

RATE_LIMIT = 38  # stay under the 40 req/s cap
_request_times = deque()


def api_get(url, params=None):
    """GET from the TMDB API with rate-limiting and 429 retry."""
    while True:
        now = time.time()
        while _request_times and _request_times[0] < now - 1.0:
            _request_times.popleft()
        if len(_request_times) >= RATE_LIMIT:
            sleep_for = 1.0 - (now - _request_times[0])
            if sleep_for > 0:
                time.sleep(sleep_for)

        resp = requests.get(url, headers=API_HEADERS, params=params, timeout=10)
        _request_times.append(time.time())

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 2))
            print(f"  429 rate limited — sleeping {retry_after}s")
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        return resp.json()


def find_tmdb_id(imdb_id):
    data = api_get(
        f"https://api.themoviedb.org/3/find/{imdb_id}",
        params={"external_source": "imdb_id"},
    )
    results = data.get("movie_results", [])
    return results[0]["id"] if results else None


def fetch_movie(tmdb_id):
    return api_get(f"https://api.themoviedb.org/3/movie/{tmdb_id}")


def fetch_credits(tmdb_id):
    return api_get(f"https://api.themoviedb.org/3/movie/{tmdb_id}/credits")


def download_poster(poster_path, imdb_id):
    """Download poster from TMDB CDN (not rate-limited by the API quota)."""
    if not poster_path:
        return None
    ext = os.path.splitext(poster_path)[1] or ".jpg"
    filename = f"{imdb_id}{ext}"
    filepath = os.path.join("images", filename)
    if os.path.exists(filepath):
        print(f"  poster already cached: {filename}")
        return filename
    url = f"https://image.tmdb.org/t/p/w500{poster_path}"
    for attempt in range(3):
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 2)))
            continue
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        print(f"  Downloaded poster: {filename}")
        return filename
    print(f"  WARNING: could not download poster for {imdb_id}")
    return None


def main():
    with open("schedule.json", encoding="utf-8") as f:
        schedule = json.load(f)

    os.makedirs("images", exist_ok=True)

    films = [film for day in schedule for film in day["films"]]
    details = {}

    for film in films:
        title = film["title"]
        imdb_id = film["imdbId"]
        print(f"Processing: {title} ({imdb_id})")

        tmdb_id = find_tmdb_id(imdb_id)
        if not tmdb_id:
            print(f"  WARNING: no TMDB match found, skipping")
            continue

        movie = fetch_movie(tmdb_id)
        credits = fetch_credits(tmdb_id)

        director = next(
            (p["name"] for p in credits.get("crew", []) if p["job"] == "Director"),
            None,
        )
        stars = [p["name"] for p in credits.get("cast", [])[:3]]
        poster_file = download_poster(movie.get("poster_path"), imdb_id)

        details[title] = {
            "director": director,
            "stars": stars,
            "tagline": movie.get("tagline") or None,
            "summary": movie.get("overview") or None,
            "poster": poster_file,
        }
        print(f"  director={director}  stars={stars}")

    details = dict(sorted(details.items()))

    with open("details.json", "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2, ensure_ascii=False)

    print(f"\nDone — {len(details)} films written to details.json")


if __name__ == "__main__":
    main()
