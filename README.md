# kuroarchive
#!/usr/bin/env python3
"""
KuroArchive Online — Cloud-Deployable Editorial Anime Discovery & Tracker Sync
Designed for zero-cost deployment on Streamlit Community Cloud (giving you a live .streamlit.app URL)
or Hugging Face Spaces, with multi-tracker sync (AniList, MAL, Kitsu) and AI recommendations.
"""

import os
import sys
import json
import sqlite3
import urllib.request
import urllib.error
import urllib.parse
import re
from typing import Dict, List, Any, Optional, Tuple
import streamlit as st

DATABASE_FILE = "kuroarchive_online.db"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
ANILIST_GRAPHQL_ENDPOINT = "https://graphql.anilist.co"
JIKAN_MAL_ENDPOINT = "https://api.jikan.moe/v4"
KITSU_API_ENDPOINT = "https://kitsu.io/api/edge"

def normalize_title(title: str) -> str:
    """Normalize anime titles for deterministic deduplication."""
    if not title:
        return ""
    clean = re.sub(r"[^a-zA-Z0-9]", "", title.lower())
    return clean.strip()

def http_json_request(url: str, data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, method: str = "GET") -> Tuple[int, Any]:
    """Execute standard library HTTP JSON requests with robust error handling."""
    req_headers = {"User-Agent": "KuroArchiveOnline/3.0 (Cinema Archival Engine)"}
    if headers:
        req_headers.update(headers)

    encoded_data = None
    if data is not None:
        req_headers["Content-Type"] = "application/json"
        req_headers["Accept"] = "application/json"
        encoded_data = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=encoded_data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            status = resp.status
            raw_body = resp.read().decode("utf-8")
            return status, json.loads(raw_body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(error_body)
        except Exception:
            return e.code, {"error": error_body or str(e)}
    except Exception as ex:
        return 500, {"error": str(ex)}

class DatabaseManager:
    """Manages persistent SQLite storage for exclusions, credentials, and bookmarks."""

    def __init__(self, db_path: str = DATABASE_FILE):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracker_configs (
                    tracker TEXT PRIMARY KEY,
                    username TEXT,
                    token TEXT,
                    logged_count INTEGER DEFAULT 0,
                    last_synced TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exclusions_vault (
                    norm_title TEXT PRIMARY KEY,
                    raw_title TEXT NOT NULL,
                    romaji_title TEXT,
                    source TEXT,
                    media_id INTEGER,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for t in ["anilist", "mal", "kitsu"]:
                cursor.execute(
                    "INSERT OR IGNORE INTO tracker_configs (tracker, username, token, logged_count) VALUES (?, '', '', 0)",
                    (t,)
                )
            conn.commit()

    def get_tracker_configs(self) -> Dict[str, Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT * FROM tracker_configs").fetchall()
            return {r["tracker"]: dict(r) for r in rows}

    def update_tracker_config(self, tracker: str, username: str, token: str = ""):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tracker_configs SET username = ?, token = ? WHERE tracker = ?",
                (username.strip(), token.strip(), tracker)
            )
            conn.commit()

    def update_tracker_stats(self, tracker: str, count: int):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tracker_configs SET logged_count = ?, last_synced = CURRENT_TIMESTAMP WHERE tracker = ?",
                (count, tracker)
            )
            conn.commit()

    def add_exclusions_batch(self, entries: List[Dict[str, Any]]) -> int:
        added = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for item in entries:
                norm = normalize_title(item.get("title", ""))
                if not norm:
                    continue
                cursor.execute("""
                    INSERT OR REPLACE INTO exclusions_vault (norm_title, raw_title, romaji_title, source, media_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    norm,
                    item.get("title", ""),
                    item.get("romaji", ""),
                    item.get("source", "Manual"),
                    item.get("media_id", None)
                ))
                added += 1
            conn.commit()
        return added

    def get_all_exclusions(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT * FROM exclusions_vault ORDER BY logged_at DESC").fetchall()
            return [dict(r) for r in rows]

    def remove_exclusion(self, norm_title: str):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM exclusions_vault WHERE norm_title = ?", (norm_title,))
            conn.commit()

    def clear_vault(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM exclusions_vault")
            conn.commit()

class TrackerSyncEngine:
    """Synchronizes watch history from AniList, MyAnimeList, and Kitsu."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def sync_anilist(self, username: str, token: str = "") -> Tuple[bool, int, str]:
        if not username:
            return False, 0, "AniList username cannot be empty."

        query = """
        query ($userName: String) {
          MediaListCollection(userName: $userName, type: ANIME) {
            lists {
              name
              entries {
                mediaId
                media {
                  id
                  title {
                    english
                    romaji
                  }
                }
              }
            }
          }
        }
        """
        payload = {"query": query, "variables": {"userName": username}}
        status, response = http_json_request(ANILIST_GRAPHQL_ENDPOINT, data=payload, method="POST")

        if status != 200 or "errors" in response:
            err_msg = response.get("errors", [{}])[0].get("message", "Failed to query AniList GraphQL API.")
            return False, 0, err_msg

        lists = response.get("data", {}).get("MediaListCollection", {}).get("lists", [])
        items_to_add = []
        for lst in lists:
            list_name = lst.get("name", "List")
            for entry in lst.get("entries", []):
                media = entry.get("media", {})
                t_eng = media.get("title", {}).get("english")
                t_rom = media.get("title", {}).get("romaji")
                chosen_title = t_eng or t_rom
                if chosen_title:
                    items_to_add.append({
                        "title": chosen_title,
                        "romaji": t_rom,
                        "source": f"AniList ({list_name})",
                        "media_id": entry.get("mediaId") or media.get("id")
                    })

        count = self.db.add_exclusions_batch(items_to_add)
        self.db.update_tracker_stats("anilist", count)
        if token:
            self.db.update_tracker_config("anilist", username, token)
        return True, count, f"Imported {count} entries from AniList (@{username})."

    def sync_mal(self, username: str) -> Tuple[bool, int, str]:
        if not username:
            return False, 0, "MyAnimeList username cannot be empty."

        url = f"{JIKAN_MAL_ENDPOINT}/users/{urllib.parse.quote(username)}/animelist/completed"
        status, response = http_json_request(url)

        if status != 200:
            return False, 0, f"MAL query failed (status {status}). Verify profile is public on MyAnimeList."

        data_list = response.get("data", [])
        items_to_add = []
        for item in data_list:
            entry = item.get("entry", {})
            title = entry.get("title")
            if title:
                items_to_add.append({
                    "title": title,
                    "romaji": "",
                    "source": "MyAnimeList (Completed)",
                    "media_id": entry.get("mal_id")
                })

        count = self.db.add_exclusions_batch(items_to_add)
        self.db.update_tracker_stats("mal", count)
        return True, count, f"Imported {count} entries from MyAnimeList (@{username})."

    def sync_kitsu(self, username: str) -> Tuple[bool, int, str]:
        if not username:
            return False, 0, "Kitsu username cannot be empty."

        user_url = f"{KITSU_API_ENDPOINT}/users?filter[name]={urllib.parse.quote(username)}"
        status, user_resp = http_json_request(user_url)
        if status != 200 or not user_resp.get("data"):
            return False, 0, f"Could not find Kitsu profile for '{username}'."

        user_id = user_resp["data"][0]["id"]
        lib_url = f"{KITSU_API_ENDPOINT}/library-entries?filter[userId]={user_id}&filter[kind]=anime&include=anime&page[limit]=50"
        status, lib_resp = http_json_request(lib_url)
        if status != 200:
            return False, 0, "Failed to retrieve Kitsu library entries."

        items_to_add = []
        for included in lib_resp.get("included", []):
            if included.get("type") == "anime":
                attrs = included.get("attributes", {})
                title = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en")
                if title:
                    items_to_add.append({
                        "title": title,
                        "romaji": attrs.get("titles", {}).get("en_jp"),
                        "source": "Kitsu.io",
                        "media_id": included.get("id")
                    })

        count = self.db.add_exclusions_batch(items_to_add)
        self.db.update_tracker_stats("kitsu", count)
        return True, count, f"Imported {count} entries from Kitsu (@{username})."

    def write_mark_watched_anilist(self, title: str, media_id: Optional[int] = None) -> Tuple[bool, str]:
        configs = self.db.get_tracker_configs()
        token = configs.get("anilist", {}).get("token", "").strip()
        if not token:
            return False, "Logged locally. (No AniList write token registered)."

        resolved_id = media_id
        if not resolved_id:
            search_query = """
            query ($search: String) {
              Media(search: $search, type: ANIME) {
                id
              }
            }
            """
            status, res = http_json_request(ANILIST_GRAPHQL_ENDPOINT, data={"query": search_query, "variables": {"search": title}}, method="POST")
            if status == 200 and res.get("data", {}).get("Media"):
                resolved_id = res["data"]["Media"]["id"]

        if not resolved_id:
            return False, "Logged locally. (Could not resolve an AniList Media ID)."

        mutation = """
        mutation ($mediaId: Int, $status: MediaListStatus) {
          SaveMediaListEntry (mediaId: $mediaId, status: $status) {
            id
            status
          }
        }
        """
        headers = {"Authorization": f"Bearer {token}"}
        status, res = http_json_request(
            ANILIST_GRAPHQL_ENDPOINT,
            data={"query": mutation, "variables": {"mediaId": resolved_id, "status": "COMPLETED"}},
            headers=headers,
            method="POST"
        )

        if status == 200 and not res.get("errors"):
            return True, f"Synchronized '{title}' directly to your live AniList profile."
        return False, f"AniList mutation error: {res.get('errors')}"

class ArchivalCurationEngine:
    """Generates authoritative, film-literate recommendations guaranteed against duplicates."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    def query_curated_works(
        self,
        platforms: List[str],
        is_archival: bool,
        focus_themes: List[str],
        api_key: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        exclusions = self.db.get_all_exclusions()
        exclusion_set = {e["norm_title"] for e in exclusions}
        exclusion_sample = [e["raw_title"] for e in exclusions[:150]]

        if api_key:
            try:
                results = self._generate_with_gemini(api_key, platforms, is_archival, focus_themes, exclusion_sample)
                unseen = [r for r in results if normalize_title(r.get("title", "")) not in exclusion_set]
                if unseen:
                    self._enrich_posters(unseen)
                    return unseen
            except Exception as e:
                st.warning(f"AI API lookup fallback: {e}")

        return self._get_fallback_catalog(platforms, is_archival, exclusion_set)

    def _generate_with_gemini(
        self,
        api_key: str,
        platforms: List[str],
        is_archival: bool,
        focus_themes: List[str],
        exclusion_sample: List[str]
    ) -> List[Dict[str, Any]]:
        theme_desc = "; ".join(focus_themes) if focus_themes else "Masterwork narrative anime cinema and visionary direction"
        licensing_desc = (
            "UNRESTRICTED ARCHIVAL (HIGH SEAS): Explicitly prioritize legendary out-of-print 80s/90s OVAs, vintage arthouse films, and unlicensed masterpieces."
            if is_archival else
            f"STRICT STREAMING AVAILABILITY: Must be legitimately accessible on: {', '.join(platforms)}."
        )

        prompt = f"""You are an esteemed film archivist and scholar of Japanese animation.
Recommend exactly 6 distinct, exceptional anime works that match the requested criteria.
Tone must be scholarly, formal, and authoritative. Avoid generic fan hype, slang, and platitudes.
Examine directorial style, animation technique, screenplay craftsmanship, and historic relevance.

Aesthetic Vectors: {theme_desc}
Licensing Criteria: {licensing_desc}

STRICT EXCLUSION BLACKLIST (User has already watched all of these; under no circumstances recommend any of them):
{', '.join(exclusion_sample) if exclusion_sample else 'None recorded.'}

Respond STRICTLY with a valid JSON array of 6 objects with these keys:
title, romajiTitle, year, format, director, studio, genres (array of strings), curatorAnalysis (2-3 scholarly sentences), synopsis, matchRating (integer 85-99), licencingPlatforms (array of strings), isArchivalRare (boolean).
"""
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.4
            }
        }
        url = f"{GEMINI_ENDPOINT}?key={api_key}"
        status, resp = http_json_request(url, data=payload, method="POST")
        if status != 200:
            raise RuntimeError(f"Gemini API error (Status {status}): {resp}")

        raw_text = resp["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw_text)

    def _get_fallback_catalog(self, platforms: List[str], is_archival: bool, exclusion_set: set) -> List[Dict[str, Any]]:
        curated_vault = [
            {
                "title": "Paranoia Agent",
                "romajiTitle": "Mousou Dairinin",
                "year": "2004",
                "format": "TV Series",
                "director": "Satoshi Kon",
                "studio": "Madhouse",
                "genres": ["Psychological", "Mystery", "Social Satire"],
                "curatorAnalysis": "Satoshi Kon's television opus dissecting urban neurosis and collective escapism through the phantom figure of Shonen Bat.",
                "synopsis": "A string of street assaults across Tokyo collapses the boundary between psychic guilt and metropolitan hysteria.",
                "matchRating": 98,
                "licencingPlatforms": ["Crunchyroll", "Prime Video"],
                "isArchivalRare": False,
                "coverImage": "https://placehold.co/400x560/0b0f19/93c5fd?text=Paranoia+Agent"
            },
            {
                "title": "Memories",
                "romajiTitle": "Memorīzu",
                "year": "1995",
                "format": "Anthology Feature",
                "director": "Katsuhiro Otomo, Koji Morimoto",
                "studio": "Studio 4°C, Madhouse",
                "genres": ["Sci-Fi", "Space Opera", "Psychological"],
                "curatorAnalysis": "The pinnacle of cel-drawn speculative fiction, anchored by Koji Morimoto's 'Magnetic Rose'—an operatic meditation on derelict orbital tombs.",
                "synopsis": "Three standalone visions spanning a haunted derelict space vessel, a biochemical farce, and an authoritarian cannon citadel.",
                "matchRating": 97,
                "licencingPlatforms": ["Archival Vault", "Retro Physical"],
                "isArchivalRare": True,
                "coverImage": "https://placehold.co/400x560/0b0f19/d97706?text=Memories+1995"
            },
            {
                "title": "The Tatami Galaxy",
                "romajiTitle": "Yojouhan Shinwa Taikei",
                "year": "2010",
                "format": "TV Series",
                "director": "Masaaki Yuasa",
                "studio": "Madhouse",
                "genres": ["Avant-Garde", "Existential Comedy", "Romance"],
                "curatorAnalysis": "Yuasa's kinetic, dialogue-dense tour de force interrogating the myth of the 'rose-colored campus life' through recursive temporal resets.",
                "synopsis": "An unnamed Kyoto student repeatedly rewinds his freshman semester in a quixotic quest to seize romantic self-actualization.",
                "matchRating": 96,
                "licencingPlatforms": ["Crunchyroll"],
                "isArchivalRare": False,
                "coverImage": "https://placehold.co/400x560/0b0f19/38bdf8?text=Tatami+Galaxy"
            },
            {
                "title": "Angel's Egg",
                "romajiTitle": "Tenshi no Tamago",
                "year": "1985",
                "format": "OVA Film",
                "director": "Mamoru Oshii",
                "studio": "Studio Deen",
                "genres": ["Surrealism", "Theological Drama", "Art Cinema"],
                "curatorAnalysis": "Mamoru Oshii and Yoshitaka Amano's austere, dialogue-sparse meditation on faith, doctrine, and biblical aftermath in haunting chiaroscuro.",
                "synopsis": "A silent girl protects a mysterious avian egg in a flooded, ruined city, shadowed by a wanderer bearing a cross-shaped rifle.",
                "matchRating": 99,
                "licencingPlatforms": ["High Seas Archival", "Unlicensed Classic"],
                "isArchivalRare": True,
                "coverImage": "https://placehold.co/400x560/0b0f19/e2e8f0?text=Angels+Egg"
            },
            {
                "title": "Kaiba",
                "romajiTitle": "Kaiba",
                "year": "2008",
                "format": "TV Series",
                "director": "Masaaki Yuasa",
                "studio": "Madhouse",
                "genres": ["Sci-Fi", "Dystopian", "Philosophical"],
                "curatorAnalysis": "Employing an intentionally deceptive Tezuka-esque round aesthetic to dissect the brutal commodification of memory chips and corporeal bodies.",
                "synopsis": "An amnesiac boy awakens in a cosmos where memories can be bought, stolen, and transferred into expendable bodies.",
                "matchRating": 95,
                "licencingPlatforms": ["Crunchyroll"],
                "isArchivalRare": False,
                "coverImage": "https://placehold.co/400x560/0b0f19/a78bfa?text=Kaiba"
            },
            {
                "title": "Royal Space Force: The Wings of Honnêamise",
                "romajiTitle": "Ouritsu Uchuugun: Honneamise no Tsubasa",
                "year": "1987",
                "format": "Feature Film",
                "director": "Hiroyuki Yamaga",
                "studio": "Gainax",
                "genres": ["Historical Fiction", "Space Program", "Drama"],
                "curatorAnalysis": "Gainax's founding masterwork—a peerless exercise in bespoke world-building and sakuga animation detailing humanity's first venture into manned spaceflight.",
                "synopsis": "In a decaying empire, an aimless cadet joins a ridiculed space initiative, gradually becoming the focal point of human aspiration.",
                "matchRating": 94,
                "licencingPlatforms": ["Archival Vault", "Prime Video"],
                "isArchivalRare": True,
                "coverImage": "https://placehold.co/400x560/0b0f19/fbbf24?text=Honneamise"
            }
        ]

        filtered = []
        for work in curated_vault:
            if normalize_title(work["title"]) in exclusion_set:
                continue
            if is_archival or any(p in platforms for p in work["licencingPlatforms"]):
                filtered.append(work)
        return filtered

    def _enrich_posters(self, works: List[Dict[str, Any]]):
        """Fetch high-res AniList cover art for generated recommendations."""
        for w in works:
            if not w.get("coverImage"):
                q = """query ($search: String) { Media(search: $search, type: ANIME) { coverImage { large } id } }"""
                status, res = http_json_request(ANILIST_GRAPHQL_ENDPOINT, data={"query": q, "variables": {"search": w["title"]}}, method="POST")
                if status == 200 and res.get("data", {}).get("Media"):
                    m = res["data"]["Media"]
                    w["coverImage"] = m.get("coverImage", {}).get("large")
                    w["media_id"] = m.get("id")

st.set_page_config(
    page_title="KuroArchive — Editorial Anime Discovery",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700&family=Plus+Jakarta+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Plus Jakarta Sans', sans-serif;
    }
    
    .kuro-title {
        font-family: 'Cinzel', serif;
        letter-spacing: 0.08em;
        font-weight: 700;
        font-size: 2.1rem;
        margin-bottom: 0.2rem;
    }
    
    .kuro-sub {
        font-size: 0.85rem;
        color: #94a3b8;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        font-family: 'JetBrains Mono', monospace;
    }

    .kuro-card {
        background-color: #0b0f19;
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 16px;
    }
</style>
""", unsafe_allow_html=True)

# Initialize singletons
if "db" not in st.session_state:
    st.session_state.db = DatabaseManager(DATABASE_FILE)
if "sync" not in st.session_state:
    st.session_state.sync = TrackerSyncEngine(st.session_state.db)
if "curator" not in st.session_state:
    st.session_state.curator = ArchivalCurationEngine(st.session_state.db)
if "current_recommendations" not in st.session_state:
    st.session_state.current_recommendations = []

db: DatabaseManager = st.session_state.db
sync: TrackerSyncEngine = st.session_state.sync
curator: ArchivalCurationEngine = st.session_state.curator

with st.sidebar:
    st.markdown('<p class="kuro-sub">Multi-Tracker Synchronization</p>', unsafe_allow_html=True)
    st.markdown('### 📡 Connected Profiles')

    configs = db.get_tracker_configs()
    exclusions = db.get_all_exclusions()

    with st.expander("AniList Integration", expanded=True):
        al_user = st.text_input("AniList Username", value=configs["anilist"]["username"] or "")
        al_tok = st.text_input("Personal Access Token (for direct writes)", value=configs["anilist"]["token"] or "", type="password")
        if st.button("Sync AniList", use_container_width=True):
            if al_user:
                db.update_tracker_config("anilist", al_user, al_tok)
                ok, cnt, msg = sync.sync_anilist(al_user, al_tok)
                if ok:
                    st.success(f"Synchronized {cnt} titles from AniList!")
                else:
                    st.error(msg)
                st.rerun()

    with st.expander("MyAnimeList (MAL) Integration"):
        mal_user = st.text_input("MAL Username", value=configs["mal"]["username"] or "")
        if st.button("Sync MyAnimeList", use_container_width=True):
            if mal_user:
                db.update_tracker_config("mal", mal_user)
                ok, cnt, msg = sync.sync_mal(mal_user)
                if ok:
                    st.success(f"Synchronized {cnt} titles from MAL!")
                else:
                    st.error(msg)
                st.rerun()

    with st.expander("Kitsu.io Integration"):
        kitsu_user = st.text_input("Kitsu Username", value=configs["kitsu"]["username"] or "")
        if st.button("Sync Kitsu", use_container_width=True):
            if kitsu_user:
                db.update_tracker_config("kitsu", kitsu_user)
                ok, cnt, msg = sync.sync_kitsu(kitsu_user)
                if ok:
                    st.success(f"Synchronized {cnt} titles from Kitsu!")
                else:
                    st.error(msg)
                st.rerun()

    st.markdown("---")
    st.markdown(f"**Exclusion Vault:** `{len(exclusions)} titles blacklisted`")
    manual_title = st.text_input("Manual Exclusion Add", placeholder="e.g. Akira, Monster")
    if st.button("Log to Blacklist"):
        if manual_title.strip():
            db.add_exclusions_batch([{"title": manual_title.strip(), "source": "Manual Web"}])
            st.success(f"Blacklisted '{manual_title.strip()}'")
            st.rerun()

    if st.button("Purge All Exclusions", type="secondary"):
        db.clear_vault()
        st.warning("Exclusion vault cleared.")
        st.rerun()

st.markdown('<p class="kuro-sub">Deterministic Deduplication & Cinema Discovery</p>', unsafe_allow_html=True)
st.markdown('<h1 class="kuro-title">KUROARCHIVE</h1>', unsafe_allow_html=True)
st.caption("Synchronizes watch archives across AniList, MyAnimeList, and Kitsu into a persistent exclusion index.")

col_lic, col_modes = st.columns([2, 1])

with col_lic:
    st.markdown("##### 📺 Distribution Licences")
    platforms = st.multiselect(
        "Select your authorized streaming platforms:",
        ["Crunchyroll", "Netflix", "Hulu", "HIDIVE", "Prime Video", "Disney+"],
        default=["Crunchyroll", "Netflix"]
    )

with col_modes:
    st.markdown("##### 🏴‍☠️ Archival Scope")
    is_archival = st.toggle("Unrestricted Archival Index (High Seas)", value=True, help="Includes out-of-print 80s/90s OVAs, fan restorations, and unstreamed arthouse classics.")

col_theme, col_key = st.columns([2, 1])
with col_theme:
    focus_themes = st.multiselect(
        "Aesthetic & Thematic Vectors:",
        [
            "Psychological & Existential Drama",
            "Masterwork Direction & Sakuga Craftsmanship",
            "Subversive Sci-Fi & Speculative Tech",
            "Atmospheric Neo-Noir & Crime Mystery",
            "Contemplative Slice of Life & Realism",
            "Rare Archival & Overlooked Cinema"
        ],
        default=["Psychological & Existential Drama"]
    )
with col_key:
    gemini_key = st.text_input("Gemini API Key (Optional)", type="password", placeholder="Leave blank to use internal engine", value=os.environ.get("GEMINI_API_KEY", ""))

if st.button("Compile Curated Selections", type="primary", use_container_width=True):
    with st.spinner("Filtering against multi-tracker exclusions and generating archival recommendations..."):
        recs = curator.query_curated_works(
            platforms=platforms,
            is_archival=is_archival,
            focus_themes=focus_themes,
            api_key=gemini_key if gemini_key else None
        )
        st.session_state.current_recommendations = recs
        st.success(f"Generated {len(recs)} guaranteed unseen recommendations.")

st.markdown("---")
st.markdown("### 🎬 Curated Archival Selections")

current_recs = st.session_state.current_recommendations

if not current_recs:
    st.info("Select your platforms or Archival Index, then tap **Compile Curated Selections**.")
else:
    cols = st.columns(3)
    for idx, work in enumerate(current_recs):
        with cols[idx % 3]:
            poster = work.get("coverImage") or f"https://placehold.co/400x560/0b0f19/e2e8f0?text={urllib.parse.quote(work['title'])}"
            st.image(poster, use_container_width=True)
            st.markdown(f"#### {work['title']}")
            st.caption(f"{work.get('romajiTitle', '')} • {work.get('year', '')} • {work.get('format', 'Anime')}")
            
            # Match badge
            match_score = work.get("matchRating", 95)
            st.markdown(f"**Index Score:** `{match_score}% Match`")
            
            # Platforms
            plats = work.get("licencingPlatforms", [])
            st.markdown(f"**Availability:** {', '.join(plats)}")
            
            # Scholarly Analysis
            analysis = work.get("curatorAnalysis") or work.get("synopsis", "")
            st.markdown(f"*{analysis}*")
            
            # Action: Log as watched
            btn_key = f"watch_{idx}_{normalize_title(work['title'])}"
            if st.button("✓ I've Watched This (Log to Vault)", key=btn_key, use_container_width=True):
                # Add to local exclusion vault
                db.add_exclusions_batch([{
                    "title": work["title"],
                    "romaji": work.get("romajiTitle", ""),
                    "source": "Logged from KuroArchive",
                    "media_id": work.get("media_id")
                }])
                # Direct AniList live mutation if token exists
                ok_al, msg_al = sync.write_mark_watched_anilist(work["title"], work.get("media_id"))
                st.toast(f"Blacklisted '{work['title']}'! {msg_al}")
                st.session_state.current_recommendations = [r for i, r in enumerate(current_recs) if i != idx]
                st.rerun()

st.markdown("---")
st.caption("KuroArchive Online • Authoritative Japanese Animation Discovery Engine")