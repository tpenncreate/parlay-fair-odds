
import re
import time
import uuid
from functools import lru_cache

import requests
import streamlit as st

# ---------- Odds helpers ----------
def american_to_decimal(american: float | int | None):
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

def implied_prob_from_american(american: float | int | None):
    dec = american_to_decimal(american)
    return (1.0 / dec) if dec and dec > 0 else None

def implied_prob_from_decimal(decimal_odds: float | None):
    if not decimal_odds or decimal_odds <= 1.0:
        return None
    return 1.0 / decimal_odds

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

def parse_boosted_odds(s: str):
    s = normalize(str(s))
    if not s:
        return None
    try:
        if s.startswith(('+','-')) or (s.isdigit() and abs(int(s)) >= 100):
            return american_to_decimal(float(s))
        val = float(s)
        if val >= 100:
            return american_to_decimal(val)
        return val
    except Exception:
        return None

# ---------- Networking helpers ----------
def backoff_get(url, headers, tries=3, timeout=20):
    delay = 0.8
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception:
            time.sleep(delay)
            delay *= 1.8
    return None  # tolerate failure

@lru_cache(maxsize=1)
def guest_key() -> str:
    r = backoff_get("https://www.pinnacle.com/config/app.json", headers={"User-Agent":"Mozilla/5.0"})
    if not r:
        raise RuntimeError("Couldn't load Pinnacle app config for guest key")
    cfg = r.json()
    try:
        return cfg["api"]["haywire"]["apiKey"]
    except Exception:
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
        "Referer": "https://www.pinnacle.com/",
        "Origin": "https://www.pinnacle.com"
    }

@st.cache_data(ttl=300, show_spinner=False)
def get_sports():
    r = backoff_get("https://guest.api.arcadia.pinnacle.com/0.1/sports", headers=make_headers())
    return r.json() if r else []

@st.cache_data(ttl=300, show_spinner=False)
def get_leagues(sport_id: int):
    r = backoff_get(f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{sport_id}/leagues?all=false", headers=make_headers())
    return r.json() if r else []

@st.cache_data(ttl=30, show_spinner=False)
def get_matchups(league_id: int):
    r = backoff_get(f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/matchups", headers=make_headers())
    return r.json() if r else []

@st.cache_data(ttl=30, show_spinner=False)
def get_straight_markets(league_id: int):
    r = backoff_get(f"https://guest.api.arcadia.pinnacle.com/0.1/leagues/{league_id}/markets/straight", headers=make_headers())
    return r.json() if r else []

@st.cache_data(ttl=20, show_spinner=False)
def get_matchup_markets(matchup_id: int):
    r = backoff_get(f"https://guest.api.arcadia.pinnacle.com/0.1/matchups/{matchup_id}/markets", headers=make_headers())
    return r.json() if r else []

# ---------- Data shaping ----------
def extract_team_name(p):
    for k in ("name", "team", "teamName", "participantName", "altName"):
        v = p.get(k)
        if v:
            return normalize(v)
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
        cand = []
        for p in participants:
            nm = extract_team_name(p)
            if not nm:
                continue
            align = slug(p.get("alignment") or p.get("side") or p.get("homeAway") or "")
            cand.append({"name": nm, "align": align or None})
        if len(cand) < 2:
            continue
        home = next((c for c in cand if c["align"] == "home"), None)
        away = next((c for c in cand if c["align"] == "away"), None)
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
                home = home or uniq[0]
                away = away or uniq[1]
        if not home or not away:
            continue
        def add(team, opp, side):
            idx.setdefault(team["name"], []).append(dict(
                matchupId=mid, side=side, team=team["name"], opponent=opp["name"]
            ))
        add(home, away, "home")
        add(away, home, "away")
    return idx

def is_full_game_period(period_obj):
    if isinstance(period_obj, dict):
        num = period_obj.get("number") or period_obj.get("id") or period_obj.get("periodId")
        return num == 0
    if isinstance(period_obj, list):
        for item in period_obj:
            if isinstance(item, dict):
                num = item.get("number") or item.get("id") or item.get("periodId")
                if num == 0:
                    return True
    return False

def candidate_overround_from_prices(prices):
    home = None; away = None
    for pr in prices:
        d = slug(pr.get("designation",""))
        if d == "home":
            home = pr.get("price")
        elif d == "away":
            away = pr.get("price")
    if home is None or away is None:
        return None
    ph = implied_prob_from_american(home)
    pa = implied_prob_from_american(away)
    if ph is None or pa is None:
        return None
    return (ph + pa) - 1.0

def choose_candidate(candidates):
    if not candidates:
        return None
    full_game = [c for c in candidates if is_full_game_period(c.get("period"))]
    pool = full_game if full_game else candidates
    with_or = [(c, candidate_overround_from_prices(c.get("prices") or [])) for c in pool]
    viable = [(c, orr) for c, orr in with_or if orr is not None and orr > 0]
    if viable:
        return sorted(viable, key=lambda x: x[1])[0][0]
    open_pool = [c for c in pool if (c.get("status") or "").lower() in ("open","enabled","active","visible","live","prelive")]
    return open_pool[0] if open_pool else pool[0]

def collect_ml_candidates(markets, matchup_id):
    out = []
    for mk in markets or []:
        if mk.get("matchupId") != matchup_id:
            continue
        t = (mk.get("type") or "").lower()
        if t not in ("moneyline","match-winner","winner","home-away"):
            continue
        prices = mk.get("prices") or []
        if not prices:
            continue
        out.append({
            "period": mk.get("period"),
            "status": (mk.get("status") or "").lower(),
            "type": t,
            "prices": prices
        })
    return out

def get_ml_prices_any_source(league_id, matchup_id):
    # 1) league straight
    lm = get_straight_markets(league_id)
    cand = collect_ml_candidates(lm, matchup_id) if lm else []
    if cand:
        chosen = choose_candidate(cand)
        if chosen:
            prices = {}
            for pr in chosen.get("prices", []):
                d = slug(pr.get("designation",""))
                if d in ("home","away"):
                    prices[d] = pr.get("price")
            if prices:
                return prices

    # 2) per-matchup markets (fallback)
    mm = get_matchup_markets(matchup_id)
    cand = collect_ml_candidates(mm, matchup_id) if mm else []
    if cand:
        chosen = choose_candidate(cand)
        if chosen:
            prices = {}
            for pr in chosen.get("prices", []):
                d = slug(pr.get("designation",""))
                if d in ("home","away"):
                    prices[d] = pr.get("price")
            if prices:
                return prices

    return {}

# ---------- Kelly ----------
def kelly_fraction(p: float, dec_odds: float):
    if p is None or dec_odds is None or dec_odds <= 1.0:
        return None
    b = dec_odds - 1.0
    q = 1.0 - p
    return (b * p - q) / b

# ---------- UI ----------
st.set_page_config(page_title="MLB Parlay Fair Odds + Kelly (ML only)", page_icon="🎯")
st.title("🎯 MLB Parlay FAIR Value + Kelly (Moneyline only)")

# Refresh control
col1, col2 = st.columns([1,1])
with col1:
    if st.button("🔄 Refresh data (clear cache)"):
        st.cache_data.clear()
with col2:
    st.caption("Using Pinnacle guest feed. If empty, refresh or try later.")

# Resolve Baseball
sports = get_sports()
baseball_sport = None
for s in sports:
    nm = (s.get("name","") or "").lower()
    if any(k in nm for k in ["baseball","mlb"]):
        baseball_sport = s
        break
if not baseball_sport:
    st.error("Couldn't find Baseball in sports list.")
    st.stop()

# Fetch leagues and score them
leagues = get_leagues(baseball_sport.get("id"))
if not leagues:
    st.error("No baseball leagues available right now.")
    st.stop()

scored = []
for lg in leagues:
    lid = lg.get("id")
    lname = (lg.get("name","") or "")
    lslug = lname.lower()
    m = get_matchups(lid)
    bonus = 1000 if ("mlb" in lslug or "major league baseball" in lslug) else 0
    score = (len(m) if isinstance(m, list) else 0) + bonus
    scored.append((score, lname, lid, len(m) if isinstance(m, list) else 0))

# Choose best league
scored.sort(reverse=True)
best_score, best_name, league_id, best_count = scored[0]

# Allow user override
options = [f"{name} (id={lid}, games={cnt})" for _, name, lid, cnt in scored[:10]]
choice = st.selectbox("League (auto-detected at top; you can override)", options, index=0)
try:
    league_id = int(choice.split("id=")[1].split(",")[0])
except Exception:
    pass

st.caption(f"Using league: **{best_name}** (id={league_id})")

# Build team list
matchups = get_matchups(league_id)
team_index = build_team_index(matchups)
all_team_names = sorted(team_index.keys())

if not all_team_names:
    st.warning("No teams listed for the selected league right now.")
    st.stop()

selected_teams = st.multiselect("Choose teams for your parlay (moneyline)", options=all_team_names, default=[])
boosted_input = st.text_input("Boosted final odds (American like +450 or decimal like 5.50)", value="+450")

if st.button("Calculate FAIR value & Kelly"):
    if not selected_teams:
        st.warning("Pick at least one team.")
        st.stop()

    results = []
    fair_prob_parlay = 1.0
    fair_dec_parlay = 1.0
    for name in selected_teams:
        selection = team_index[name][0]
        prices = get_ml_prices_any_source(league_id, selection["matchupId"])
        if not prices:
            continue
        p_home = implied_prob_from_american(prices.get("home"))
        p_away = implied_prob_from_american(prices.get("away"))
        f_home, f_away = devig_two_way(p_home, p_away)
        fair = f_home if selection["side"]=="home" else f_away
        res = dict(
            **selection,
            price_home=prices.get("home"),
            price_away=prices.get("away"),
            selected_price=prices.get(selection["side"]),
            fair_prob=fair,
            fair_decimal=(1.0/fair) if fair else None,
            fair_american=decimal_to_american((1.0/fair) if fair else None)
        )
        results.append(res)
        fair_prob_parlay *= res["fair_prob"]
        fair_dec_parlay *= res["fair_decimal"]

    if not results:
        st.error("Couldn't locate moneyline prices for selected teams (feed may be limiting or markets not listed yet).")
        st.stop()

    st.subheader("Individual legs (full-game ML closest match)")
    for r in results:
        st.markdown(
            f"- **{r['team']}** vs **{r['opponent']}** ({r['side']})  "
            f"Home ML: {r['price_home']:+} | Away ML: {r['price_away']:+}  "
            f"→ **Fair prob** {r['fair_prob']:.3f}  "
            f"(**Fair dec** {r['fair_decimal']:.3f}, **Fair Amer** {r['fair_american']:+})"
        )

    # Parlay fair odds (multiplicative)
    st.divider()
    st.subheader("Parlay FAIR value")
    st.metric("Fair decimal odds", f"{fair_dec_parlay:.3f}", delta=f"Fair American {decimal_to_american(fair_dec_parlay):+}")
    st.caption(f"Fair parlay probability ≈ {fair_prob_parlay:.4f}")

    # Kelly calculation
    offered_dec = parse_boosted_odds(boosted_input)
    if not offered_dec:
        st.error("Couldn't parse boosted odds. Enter like +450 or 5.50")
        st.stop()

    def kelly_fraction(p: float, dec_odds: float):
        if p is None or dec_odds is None or dec_odds <= 1.0:
            return None
        b = dec_odds - 1.0
        q = 1.0 - p
        return (b * p - q) / b

    f_full = kelly_fraction(fair_prob_parlay, offered_dec)
    if f_full is None:
        st.error("Kelly fraction could not be computed (check boosted odds).")
        st.stop()

    f_quarter = max(0.0, f_full * 0.25)
    b = offered_dec - 1.0
    ev_per_dollar = fair_prob_parlay * b - (1.0 - fair_prob_parlay)

    st.divider()
    st.subheader("Kelly staking")
    st.write(f"**Offered (boosted) decimal odds:** {offered_dec:.3f}  (American {decimal_to_american(offered_dec):+})")
    st.write(f"**Fair win probability (parlay):** {fair_prob_parlay:.4%}")
    st.write(f"**Full Kelly fraction:** {f_full:.3%}")
    st.success(f"**Quarter‑Kelly stake:** {f_quarter:.3%} of bankroll")
    st.caption(f"Expected value per $1 staked at offered odds: {ev_per_dollar:.4f}")
