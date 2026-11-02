import json
import requests
import streamlit as st

st.set_page_config(
    page_title="KuroArchive | Editorial Anime Discovery",
    page_icon="📼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Editorial Styling
st.markdown(
    """
    <style>
    .stApp {
        background-color: #0c0d10;
        color: #e5e7eb;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    h1, h2, h3 {
        color: #f9fafb !important;
        font-weight: 600 !important;
        letter-spacing: -0.02em;
    }
    .archive-card {
        background: #14161d;
        border: 1px solid #232734;
        border-radius: 8px;
        padding: 20px;
        margin-bottom: 18px;
    }
    .archive-tag {
        display: inline-block;
        background: #1f2430;
        color: #9ca3af;
        border: 1px solid #2e3547;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.75rem;
        margin-right: 6px;
        margin-bottom: 6px;
    }
    .high-seas-badge {
        display: inline-block;
        background: #3b181e;
        color: #fca5a5;
        border: 1px solid #7f1d1d;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    </style>
""",
    unsafe_allow_html=True,
)

# State Initialization
if "watched_vault" not in st.session_state:
    st.session_state.watched_vault = set()
if "recommendations" not in st.session_state:
    st.session_state.recommendations = []
if "sync_logs" not in st.session_state:
    st.session_state.sync_logs = []


# Tracker Sync Functions
def sync_anilist(username: str) -> int:
    query = """
    query ($userName: String) {
      MediaListCollection(userName: $userName, type: ANIME) {
        lists {
          entries {
            media {
              title {
                romaji
                english
              }
            }
          }
        }
      }
    }
    """
    try:
        response = requests.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"userName": username}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            lists = (
                data.get("data", {})
                .get("MediaListCollection", {})
                .get("lists", [])
            )
            count = 0
            for lst in lists:
                for entry in lst.get("entries", []):
                    titles = entry.get("media", {}).get("title", {})
                    if titles.get("romaji"):
                        st.session_state.watched_vault.add(
                            titles["romaji"].strip().lower()
                        )
                        count += 1
                    if titles.get("english"):
                        st.session_state.watched_vault.add(
                            titles["english"].strip().lower()
                        )
            return count
    except Exception as e:
        st.sidebar.error(f"AniList error: {e}")
    return 0


def sync_mal(username: str) -> int:
    try:
        url = f"https://api.jikan.moe/v4/users/{username}/animelist/completed"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            entries = response.json().get("data", [])
            count = 0
            for item in entries:
                anime = item.get("entry", {})
                title = anime.get("title")
                if title:
                    st.session_state.watched_vault.add(title.strip().lower())
                    count += 1
            return count
    except Exception as e:
        st.sidebar.error(f"MyAnimeList error: {e}")
    return 0


def sync_kitsu(username: str) -> int:
    try:
        user_res = requests.get(
            f"https://kitsu.io/api/edge/users?filter[name]={username}",
            headers={"Accept": "application/vnd.api+json"},
            timeout=10,
        )
        if user_res.status_code == 200:
            users = user_res.json().get("data", [])
            if users:
                user_id = users[0]["id"]
                entries_res = requests.get(
                    f"https://kitsu.io/api/edge/library-entries?filter[userId]={user_id}&filter[kind]=anime&include=anime&page[limit]=50",
                    headers={"Accept": "application/vnd.api+json"},
                    timeout=10,
                )
                if entries_res.status_code == 200:
                    payload = entries_res.json()
                    included = {
                        item["id"]: item["attributes"]["canonicalTitle"]
                        for item in payload.get("included", [])
                        if item.get("type") == "anime"
                    }
                    count = 0
                    for entry in payload.get("data", []):
                        rel = (
                            entry.get("relationships", {})
                            .get("anime", {})
                            .get("data", {})
                        )
                        anime_id = rel.get("id")
                        if anime_id and anime_id in included:
                            st.session_state.watched_vault.add(
                                included[anime_id].strip().lower()
                            )
                            count += 1
                    return count
    except Exception as e:
        st.sidebar.error(f"Kitsu error: {e}")
    return 0


def log_to_anilist_live(title: str, token: str) -> bool:
    if not token:
        return False
    search_query = """
    query ($search: String) {
      Media(search: $search, type: ANIME) {
        id
      }
    }
    """
    mutation = """
    mutation ($mediaId: Int) {
      SaveMediaListEntry(mediaId: $mediaId, status: COMPLETED) {
        id
        status
      }
    }
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        search_res = requests.post(
            "https://graphql.anilist.co",
            json={"query": search_query, "variables": {"search": title}},
            headers=headers,
            timeout=10,
        )
        if search_res.status_code == 200:
            media_id = (
                search_res.json().get("data", {}).get("Media", {}).get("id")
            )
            if media_id:
                mut_res = requests.post(
                    "https://graphql.anilist.co",
                    json={
                        "query": mutation,
                        "variables": {"mediaId": media_id},
                    },
                    headers=headers,
                    timeout=10,
                )
                return mut_res.status_code == 200
    except Exception:
        pass
    return False


def get_ai_recommendations(
    gemini_key: str,
    taste_notes: str,
    platforms: list,
    high_seas: bool,
    vault: set,
) -> list:
    excluded_sample = list(vault)[:120]

    system_instruction = (
        "You are an authoritative, editorial film and anime archivist. "
        "Recommend strictly 4 distinctive anime titles that match the user's taste criteria. "
        "CRITICAL: Never recommend any title present in the exclusion list. "
        "Output ONLY a raw, valid JSON array of objects without Markdown formatting or fences."
    )

    platform_constraint = (
        "Focus on titles officially licensed on: " + ", ".join(platforms)
        if not high_seas
        else "HIGH SEAS / ARCHIVAL MODE ACTIVE: Include  2000/2026 OVAs, and unstreamed masterworks irrespective of commercial licensing."
    )

    prompt = f"""
    Taste notes: {taste_notes}
    Platform filter: {platform_constraint}
    Titles to strictly EXCLUDE (user has already watched): {json.dumps(excluded_sample)}

    Return exactly 4 entries in this JSON structure:
    [
      {{
        "title": "Title in English or Romaji",
        "year": 1998,
        "director_studio": "Director name / Studio name",
        "format": "TV Series | OVA | Film",
        "platforms": ["Crunchyroll", "Archival Index"],
        "editorial_thesis": "Formal 2-3 sentence critique explaining why this fits their profile.",
        "comparable_to": "Similar aesthetic or thematic reference"
      }}
    ]
    """

    if gemini_key:
        clean_key = gemini_key.strip()
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={clean_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "systemInstruction": {
                    "parts": [{"text": system_instruction}]
                },
                "generationConfig": {"temperature": 0.3},
            }
            res = requests.post(url, json=payload, timeout=40)
            if res.status_code == 200:
                raw_text = (
                    res.json()["candidates"][0]["content"]["parts"][0]["text"]
                )
                cleaned = (
                    raw_text.replace("```json", "")
                    .replace("```", "")
                    .strip()
                )
                return json.loads(cleaned)
            else:
                st.sidebar.error(f"Gemini API Error ({res.status_code}): {res.text}")
        except Exception as e:
            st.sidebar.error(f"Recommendation generation error: {e}")

    # Fallback curated pool if key is missing or fails
    curated_pool = [
        {
            "title": "Mushoku Tensei: Jobless Reincarnation",
            "year": 2026,
            "director_studio": "Manabu Okamoto / egg flim",
            "format": "TV Series (22 eps)",
            "platforms": ["Crunchyroll", "hulu", "disney+"],
            "editorial_thesis": "a 34-year-old unnamed, jobless Japanese shut-in who dies under unfortunate circumstances and is reincarnated into a magical fantasy world.",
            "comparable_to": "that time that i got reincarneted as a slime, Ergo Proxy",
        },
        {
            "title": "Ergo Proxy",
            "year": 2004,
            "director_studio": "Shukou Murase / Manglobe",
            "format": "TV Series (23 eps)",
            "platforms": ["Crunchyroll"],
            "editorial_thesis": "A philosophical cyberpunk investigation into self-awareness, Gnostic theology, and android autonomy within a domed post-apocalyptic utopia.",
            "comparable_to": "Texhnolyze, Psycho-Pass",
        },
        {
            "title": "Serial Experiments Lain",
            "year": 1998,
            "director_studio": "Ryutaro Nakamura / Triangle Staff",
            "format": "TV Series (13 eps)",
            "platforms": ["Funimation / Archival"],
            "editorial_thesis": "A prophetic exploration of wired connectivity, dissolution of personal identity, and the boundary between physical reality and cyberspace.",
            "comparable_to": "Texhnolyze, Evangelion",
        },
        {
            "title": "Paranoia Agent",
            "year": 2004,
            "director_studio": "Satoshi Kon / Madhouse",
            "format": "TV Series (13 eps)",
            "platforms": ["Crunchyroll"],
            "editorial_thesis": "A brilliant psychological satire on modern societal pressure and mass hysteria centered around a mysterious rollerblading assailant.",
            "comparable_to": "Perfect Blue, Paprika",
        },
    ]

    return [
        item for item in curated_pool
        if item["title"].lower() not in st.session_state.watched_vault
    ][:4]


# Sidebar: Tracker Synchronizer
st.sidebar.markdown("### Tracker Synchronization")
st.sidebar.caption(
    "Import watch history to enforce strict duplicate exclusion."
)

anilist_user = st.sidebar.text_input("AniList Username")
mal_user = st.sidebar.text_input("MyAnimeList Username")
kitsu_user = st.sidebar.text_input("Kitsu Username")
anilist_token = st.sidebar.text_input(
    "AniList Access Token (Optional for write-back)", type="password"
)

if st.sidebar.button("Synchronize Libraries", use_container_width=True):
    total_imported = 0
    with st.sidebar.status("Connecting to tracker APIs..."):
        if anilist_user:
            c = sync_anilist(anilist_user)
            st.write(f"AniList: {c} titles indexed")
            total_imported += c
        if mal_user:
            c = sync_mal(mal_user)
            st.write(f"MyAnimeList: {c} titles indexed")
            total_imported += c
        if kitsu_user:
            c = sync_kitsu(kitsu_user)
            st.write(f"Kitsu: {c} titles indexed")
            total_imported += c

    st.sidebar.success(
        f"Vault updated: {len(st.session_state.watched_vault)} excluded titles"
    )

st.sidebar.divider()
st.sidebar.markdown(
    f"**Exclusion Vault:** `{len(st.session_state.watched_vault)}` titles active"
)

manual_entry = st.sidebar.text_input("Manually exclude a title")
if st.sidebar.button("Add to Vault", use_container_width=True):
    if manual_entry:
        st.session_state.watched_vault.add(manual_entry.strip().lower())
        st.sidebar.info(f"Added '{manual_entry}' to vault.")

st.sidebar.divider()
gemini_api_key = st.sidebar.text_input(
    "Gemini API Key",
    type="password",
    help=(
        "Optional: Enables real-time custom generation. If blank, uses the"
        " curated archive."
    ),
)

# Main Layout
st.title("KUROARCHIVE")
st.caption(
    "Editorial Anime Discovery Engine • Zero-Duplicate Cross-Tracker"
    " Deduplication"
)

col1, col2 = st.columns([2, 1])

with col1:
    taste_notes = st.text_area(
        "Taste Criteria & Directorial Preferences",
        placeholder=(
            "e.g., Psychological cyberpunk, philosophical sci-fi with"
            " deliberate pacing, surreal horror, pre-digital 90s cel"
            " aesthetics..."
        ),
        height=100,
    )

with col2:
    st.markdown("**Distribution Licensing**")
    selected_platforms = []
    c1, c2 = st.columns(2)
    with c1:
        if st.checkbox("Crunchyroll", value=True):
            selected_platforms.append("Crunchyroll")
        if st.checkbox("Netflix"):
            selected_platforms.append("Netflix")
        if st.checkbox("Hulu"):
            selected_platforms.append("Hulu")
    with c2:
        if st.checkbox("HIDIVE"):
            selected_platforms.append("HIDIVE")
        if st.checkbox("Prime Video"):
            selected_platforms.append("Prime Video")
        if st.checkbox("Disney+"):
            selected_platforms.append("Disney+")

    high_seas_mode = st.toggle(
        "High Seas / Unrestricted Archival",
        value=False,
        help=(
            "Ignore commercial licensing barriers to recommend unstreamed"
            " OVAs, vintage movies, and fan restorations."
        ),
    )

if st.button("Generate Curated Selections", type="primary"):
    with st.spinner("Synthesizing recommendations against exclusion vault..."):
        results = get_ai_recommendations(
            gemini_key=gemini_api_key,
            taste_notes=taste_notes
            or "High-caliber psychological, philosophical anime",
            platforms=selected_platforms,
            high_seas=high_seas_mode,
            vault=st.session_state.watched_vault,
        )
        st.session_state.recommendations = results

# Render Recommendations
if st.session_state.recommendations:
    st.markdown("### Selected Titles")
    for idx, rec in enumerate(st.session_state.recommendations):
        title = rec.get("title", "Untitled")
        title_lower = title.strip().lower()

        if title_lower in st.session_state.watched_vault:
            continue

        badge_html = '<span class="high-seas-badge">🏴‍☠️ High Seas / Archival</span>' if high_seas_mode else ''
        card_html = (
            f'<div class="archive-card">'
            f'<div style="display: flex; justify-content: space-between; align-items: baseline;">'
            f'<h3 style="margin: 0;">{title} <span style="font-size: 0.9rem; color: #9ca3af; font-weight: normal;">({rec.get("year", "N/A")})</span></h3>'
            f'<span style="font-size: 0.8rem; color: #9ca3af;">{rec.get("format", "Feature")}</span>'
            f'</div>'
            f'<p style="color: #6b7280; font-size: 0.85rem; margin-top: 4px; margin-bottom: 12px;">{rec.get("director_studio", "Archive Record")}</p>'
            f'<p style="font-size: 0.95rem; line-height: 1.6; margin-bottom: 14px;">{rec.get("editorial_thesis", "")}</p>'
            f'<div style="margin-bottom: 10px;">'
            f'<span class="archive-tag">Reference: {rec.get("comparable_to", "N/A")}</span>'
            f'{badge_html}'
            f'</div>'
            f'</div>'
        )

        with st.container():
            st.markdown(card_html, unsafe_allow_html=True)

            btn_col1, btn_col2 = st.columns([1, 15])
            with btn_col1:
                if st.button(
                    "Mark Watched",
                    key=f"log_{idx}",
                    use_container_width=True,
                ):
                    st.session_state.watched_vault.add(title_lower)
                    if anilist_token:
                        updated = log_to_anilist_live(title, anilist_token)
                        if updated:
                            st.success(
                                f"Updated AniList live entry for '{title}'."
                            )
                        else:
                            st.info(
                                f"Added '{title}' to local vault (AniList update skipped)."
                            )
                    else:
                        st.info(f"Added '{title}' to local exclusion vault.")
                    st.rerun()
