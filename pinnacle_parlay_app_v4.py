
import re
import uuid
from functools import lru_cache

import requests
import streamlit as st

# ---------- Helpers ----------
def american_to_decimal(american: float | None):
    if american is None:
        return None
    a = float(american)
    if a > 0:
        return 1.0 + (a / 100.0)
    elif a < 0:
        return 1.0 - (100.0 / a)
    return None

def decimal_to_american(decimal_odds: float | None):
    if not decimal_odds or decimal_odds <= 1.0:
        return None
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1.0) * 100.0)
    return round(-100.0 / (decimal_odds - 1.0))

def implied_prob_from_american(american: float | None):
    dec = american_to_decimal(american)
    return (1.0 / dec) if dec and dec > 0 else None

def devig_two_way(p_home: float | None, p_away: float | None):
    if p_home is None or p_away is None:
        return None, None
    s = p_home + p_away
    if s <= 0:
        return None, None
    return p_home / s, p_away / s

def normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()

def slug(s: str) -> str:
    return normalize(s).lower()

@lru_cache(maxsize=1)
def guest_key() -> str:
    cfg = requests.get("https://www.pinnacle.com/config/app.json", timeout=15).json()
    try:
        return cfg["api"]["haywire"]["apiKey"]
    except Exception:
        # Fallback scan
        def scan(x):
            if isinstance(x, dict):
                for v in x.values():
                    y = scan(v); 
                    if y: return y
            elif isinstance(x, list):
                for v in x:
                    y = scan(v); 
                    if y: return y
            elif isinstance(x, str) and len(x) >= 16:
                return x
        k = scan(cfg)
        if not k:
            raise RuntimeError("Couldn't find guest API key")
        return k

def make_headers():
    return {
        "X-API-Key": guest_key(),
        "X-Device-UUID": str(uuid.uuid4()),
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinnacle.com/"
    }

@st.cache_data(ttl=300, show_spinner=False)
def get_sports():
    r = requests.get("https://guest.api.arcadia.pinnacle.com/0.1/sports", headers=make_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=300, show_spinner=False)
def get_leagues(sport_id: int):
    r = requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{sport_id}/leagues?all=false", headers=make_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=30, show_spinner=False)
def get_matchups(league_id: int):
    r = requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/matchups", headers=make_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=30, show_spinner=False)
def get_straight_markets(league_id: int):
    r = requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/markets/straight", headers=make_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def extract_team_name(p):
    # Try multiple fields that might exist
    for k in ("name", "team", "teamName", "participantName", "altName"):
        v = p.get(k)
        if v:
            return normalize(v)
    # Some schemas nest the name
    for k in ("contestant", "competitor", "entity"):
        obj = p.get(k)
        if isinstance(obj, dict):
            for kk in ("name","teamName","displayName"):
                v = obj.get(kk)
                if v:
                    return normalize(v)
    return ""

def build_team_index(matchups):
    idx = {}
    for m in matchups or []:
        mid = m.get("id")
        participants = m.get("participants") or m.get("contestants") or []
        # Gather candidates with any recognizable name
        cand = []
        for p in participants:
            nm = extract_team_name(p)
            if not nm:
                continue
            align = slug(p.get("alignment") or p.get("side") or p.get("homeAway") or "")
            cand.append({"name": nm, "align": align or None})
        if len(cand) < 2:
            # cannot form a matchup
            continue
        # Identify home/away if possible
        home = next((c for c in cand if c["align"] == "home"), None)
        away = next((c for c in cand if c["align"] == "away"), None)
        # Fallback: take first two unique names
        if not home or not away:
            seen = []
            uniq = []
            for c in cand:
                if c["name"] not in seen:
                    seen.append(c["name"])
                    uniq.append(c)
                if len(uniq) == 2:
                    break
            if len(uniq) == 2:
                if not home: home = uniq[0]
                if not away: away = uniq[1]
        if not home or not away:
            continue

        def add(team, opp, side):
            idx.setdefault(team["name"], []).append(dict(
                matchupId=mid, side=side, team=team["name"], opponent=opp["name"]
            ))

        add(home, away, "home")
        add(away, home, "away")
    return idx

def get_ml_prices(markets, matchup_id):
    for mk in markets or []:
        if mk.get("matchupId") != matchup_id:
            continue
        t = mk.get("type","").lower()
        if t not in ("moneyline","match-winner","winner","home-away"):
            continue
        out = {}
        for pr in mk.get("prices", []):
            d = slug(pr.get("designation",""))
            if d in ("home","away"):
                out[d] = pr.get("price")
        if out:
            return out
    return {}

def compute_leg(selection, markets):
    prices = get_ml_prices(markets, selection["matchupId"])
    if not prices:
        return None
    p_home = implied_prob_from_american(prices.get("home"))
    p_away = implied_prob_from_american(prices.get("away"))
    if p_home is None or p_away is None:
        return None
    f_home, f_away = devig_two_way(p_home, p_away)
    fair = f_home if selection["side"]=="home" else f_away
    return dict(
        **selection,
        price_home=prices.get("home"),
        price_away=prices.get("away"),
        selected_price=prices.get(selection["side"]),
        fair_prob=fair,
        fair_decimal=(1.0/fair) if fair else None,
        fair_american=decimal_to_american((1.0/fair) if fair else None)
    )

# ---------- UI ----------
st.set_page_config(page_title="Parlay Fair Odds (No‑Vig)", page_icon="🎯")
st.title("🎯 Parlay FAIR Value (no‑vig) — Picker Mode (v4)")
st.caption("Improved parser for matchups. If teams still don't appear, toggle Debug and share the snapshot.")

colA, colB = st.columns([1,1])
with colA:
    refresh = st.button("🔄 Force refresh (clear cache)", use_container_width=True)
with colB:
    show_debug = st.toggle("Show debug info", value=True)

if refresh:
    st.cache_data.clear()

sports = get_sports()
sport_names = [s.get("name","") for s in sports]
default_sport_idx = next((i for i,n in enumerate(sport_names) if "baseball" in (n or "").lower()), 0)
sport_name = st.selectbox("Sport", sport_names, index=default_sport_idx)
sport = next(s for s in sports if s.get("name")==sport_name)
sport_id = sport.get("id")

leagues = get_leagues(sport_id)
league_names = [l.get("name","") for l in leagues]
default_league_idx = next((i for i,n in enumerate(league_names) if "mlb" in (n or "").lower() or "major league baseball" in (n or "").lower()), 0)
league_name = st.selectbox("League", league_names, index=default_league_idx if league_names else 0)
league = next((l for l in leagues if l.get("name")==league_name), None)
league_id = league.get("id") if league else None

if show_debug:
    st.write({"sport_id": sport_id, "league_id": league_id, "num_leagues": len(leagues)})

if not league_id:
    st.stop()

matchups = get_matchups(league_id)
markets = get_straight_markets(league_id)

if show_debug and matchups:
    st.write("Sample matchup keys:", sorted(list(matchups[0].keys())))

team_index = build_team_index(matchups)
all_team_names = sorted(team_index.keys())

if show_debug:
    st.write({
        "num_matchups": len(matchups or []),
        "num_markets": len(markets or []),
        "sample_teams": all_team_names[:20]
    })

if not all_team_names:
    st.warning("No current teams parsed. If debug shows big num_matchups, please copy the 'Sample matchup keys' above so we can adapt the parser.")
    st.stop()

st.markdown("**Available teams right now:**")
st.write(", ".join(all_team_names))

selected_teams = st.multiselect("Choose teams for your parlay (moneyline)", options=all_team_names, default=[])

if st.button("Calculate FAIR value"):
    if not selected_teams:
        st.warning("Pick at least one team.")
        st.stop()

    results = []
    for name in selected_teams:
        selection = team_index[name][0]
        res = compute_leg(selection, markets)
        if res:
            results.append(res)

    if not results:
        st.error("Couldn't locate moneyline prices for the selected teams.")
        st.stop()

    st.subheader("Individual legs")
    for r in results:
        st.markdown(
            f"- **{r['team']}** vs **{r['opponent']}** ({r['side']})  "
            f"ML: {r['selected_price']:+}  →  **Fair prob** {r['fair_prob']:.3f}  "
            f"(**Fair dec** {r['fair_decimal']:.3f}, **Fair Amer** {r['fair_american']:+})"
        )

    fair_prob_parlay = 1.0
    fair_dec_parlay = 1.0
    for r in results:
        fair_prob_parlay *= r["fair_prob"]
        fair_dec_parlay *= r["fair_decimal"]

    st.divider()
    st.subheader("Parlay FAIR value")
    st.metric("Fair decimal odds", f"{fair_dec_parlay:.3f}", delta=f"Fair American {decimal_to_american(fair_dec_parlay):+}")
    st.caption(f"Fair parlay probability ≈ {fair_prob_parlay:.4f}")
