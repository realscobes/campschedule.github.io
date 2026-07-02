import json
import os
import re
import time
from collections import deque

import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("TMDB_ACCESS_TOKEN")
if not ACCESS_TOKEN:
    raise SystemExit("Error: TMDB_ACCESS_TOKEN is not set. Add it to your .env file and try again.")
API_HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
}

RATE_LIMIT = 38  # stay under the 40 req/s cap
TITLE_YEAR_RE = re.compile(r'^(.+?)\s*[\(\[](\d{4})[\)\]](?:_[a-zA-Z]+)?$')


def parse_title_year(raw):
    """Extract (title, year) from 'Title (Year)' format, or (None, None) if not a film entry."""
    m = TITLE_YEAR_RE.match(raw)
    if m:
        return m.group(1).strip(), m.group(2)
    return None, None


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


def find_tmdb_id(imdb_id, title=None, year=None):
    data = api_get(
        f"https://api.themoviedb.org/3/find/{imdb_id}",
        params={"external_source": "imdb_id"},
    )
    results = data.get("movie_results", [])
    if results:
        return results[0]["id"]
    if not title:
        return None
    # Secondary: search by title (and year if available) for a more accurate match
    print(f"  IMDb ID lookup missed — trying title search for '{title}' ({year})")
    params = {"query": title, "include_adult": True}
    if year:
        params["year"] = year
    search = api_get(
        "https://api.themoviedb.org/3/search/movie",
        params=params,
    )
    candidates = search.get("results", [])
    if not candidates:
        return None
    # Prefer an exact title match (case-insensitive); otherwise take top result
    title_lower = title.lower()
    for c in candidates:
        if c.get("title", "").lower() == title_lower:
            print(f"  Matched by title: {c['title']} (TMDB {c['id']})")
            return c["id"]
    print(f"  Using top search result: {candidates[0]['title']} (TMDB {candidates[0]['id']})")
    return candidates[0]["id"]


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


def download_poster_url(url, imdb_id):
    """Download a poster from a direct URL (used by OMDb fallback)."""
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    filename = f"{imdb_id}{ext}"
    filepath = os.path.join("images", filename)
    if os.path.exists(filepath):
        print(f"  poster already cached: {filename}")
        return filename
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


def fetch_from_omdb(imdb_id):
    """Fallback: fetch movie data from OMDb API."""
    try:
        resp = requests.get(
            "https://www.omdbapi.com/",
            params={"i": imdb_id, "apikey": OMDB_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  OMDb error: {e}")
        return None

    if data.get("Response") == "False":
        print(f"  OMDb: {data.get('Error', 'no result')}")
        return None

    def clean(val):
        return val if val and val != "N/A" else None

    director = clean(data.get("Director"))
    actors_raw = clean(data.get("Actors")) or ""
    stars = [a.strip() for a in actors_raw.split(",") if a.strip()][:3]
    tagline = clean(data.get("Tagline"))
    summary = clean(data.get("Plot"))

    poster_file = None
    poster_url = clean(data.get("Poster"))
    if poster_url:
        poster_file = download_poster_url(poster_url, imdb_id)

    print(f"  [OMDb] director={director}  stars={stars}")
    return {
        "director": director,
        "stars": stars,
        "tagline": tagline,
        "summary": summary,
        "poster": poster_file,
    }


def main():
    with open("schedule.json", encoding="utf-8") as f:
        schedule = json.load(f)

    os.makedirs("images", exist_ok=True)

    films = [film for day in schedule for film in day["films"]]
    details = {}
    failed = []

    for film in films:
        raw_title = film["title"]
        clean_title, year = parse_title_year(raw_title)
        if clean_title is None:
            print(f"Skipping: {raw_title}")
            continue
        imdb_id = film["imdbId"]
        print(f"Processing: {raw_title} ({imdb_id})")

        tmdb_id = find_tmdb_id(imdb_id, clean_title, year)
        if tmdb_id:
            movie = fetch_movie(tmdb_id)
            credits = fetch_credits(tmdb_id)

            director = next(
                (p["name"] for p in credits.get("crew", []) if p["job"] == "Director"),
                None,
            )
            stars = [p["name"] for p in credits.get("cast", [])[:3]]
            poster_file = download_poster(movie.get("poster_path"), tmdb_id)

            details[raw_title] = {
                "title": clean_title,
                "year": year,
                "director": director,
                "stars": stars,
                "tagline": movie.get("tagline") or None,
                "summary": movie.get("overview") or None,
                "poster": poster_file,
            }
            print(f"  director={director}  stars={stars}")
        else:
            print(f"  WARNING: no TMDB match found, skipping")
            failed.append((raw_title, imdb_id))
            continue

    details = dict(sorted(details.items()))

    with open("details.json", "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2, ensure_ascii=False)

    print(f"\nDone — {len(details)} films written to details.json")
    if failed:
        print(f"\n{len(failed)} film(s) need manual entry in details.json:")
        for title, imdb_id in failed:
            print(f"  {imdb_id}  {title}")


if __name__ == "__main__":
    main()
