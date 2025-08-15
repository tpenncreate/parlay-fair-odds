
import json
import requests
import streamlit as st

API_BASE = "https://api.opticodds.com/api/v3"

# ---------------- Core helpers ----------------
def american_to_decimal(a):
    if a is None: return None
    a = float(a)
    return 1.0 + a/100.0 if a > 0 else 1.0 - 100.0/a

def decimal_to_american(d):
    if not d or d <= 1.0: return None
    return round((d-1.0)*100.0) if d >= 2.0 else round(-100.0/(d-1.0))

def implied_prob_from_american(a):
    d = american_to_decimal(a)
    return (1.0/d) if d and d > 0 else None

def devig_two_way(p_a, p_b):
    if p_a is None or p_b is None: return (None, None)
    s = p_a + p_b
    if s <= 0: return (None, None)
    return (p_a/s, p_b/s)

def parse_boosted_odds(s):
    s = (s or "").strip()
    if not s: return None
    try:
        if s.startswith(('+','-')) or (s.isdigit() and abs(int(s)) >= 100):
            return american_to_decimal(float(s))
        val = float(s)
        if val >= 100:  # user meant +450
            return american_to_decimal(val)
        return val
    except Exception:
        return None

def kelly_fraction(p, dec_odds):
    if p is None or dec_odds is None or dec_odds <= 1.0: return None
    b = dec_odds - 1.0
    q = 1.0 - p
    return (b*p - q) / b

# ---------------- Auth & API ----------------
def resolve_api_key(user_key_input: str | None):
    # Order: explicit UI input -> secrets
    if user_key_input and user_key_input.strip():
        return user_key_input.strip()
    return st.secrets.get("ODDSJAM_API_KEY") or st.secrets.get("OPTICODDS_API_KEY")

def make_headers(api_key: str):
    # Try both common styles
    return {
        "X-Api-Key": api_key,
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

def safe_get(url: str, headers: dict, params: dict | None = None):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=25)
        if r.status_code != 200:
            return None, {"status": r.status_code, "body": _safe_json(r.text)}
        return r.json(), None
    except requests.RequestException as e:
        return None, {"error": str(e)}

def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return text[:500]

@st.cache_data(ttl=300, show_spinner=False)
def list_leagues(api_key: str):
    headers = make_headers(api_key)
    data, err = safe_get(f"{API_BASE}/leagues", headers, params={"sport": "baseball"})
    return data, err

@st.cache_data(ttl=180, show_spinner=False)
def list_active_fixtures(api_key: str, league_id: str):
    headers = make_headers(api_key)
    data, err = safe_get(f"{API_BASE}/fixtures/active", headers, params={"league": league_id})
    return data, err

@st.cache_data(ttl=90, show_spinner=False)
def get_fixture_odds_moneyline(api_key: str, fixture_id: str, sportsbook: str = "Pinnacle"):
    headers = make_headers(api_key)
    params = {"fixture_id": fixture_id, "sportsbook": sportsbook, "market": "Moneyline", "odds_format": "AMERICAN"}
    data, err = safe_get(f"{API_BASE}/fixtures/odds", headers, params=params)
    if err or not data: 
        return None, err
    home = away = home_name = away_name = None
    try:
        if data.get("data"):
            entry = data["data"][0]
            home_name = entry.get("home_team_display")
            away_name = entry.get("away_team_display")
            for odd in entry.get("odds", []):
                if (odd.get("market") or "").lower() == "moneyline" and odd.get("sportsbook") == sportsbook:
                    sel = (odd.get("selection") or "").strip()
                    price = odd.get("price")
                    if sel == home_name: home = price
                    if sel == away_name: away = price
    except Exception as e:
        return None, {"error": str(e), "raw": data}
    return {"home": home, "away": away, "home_name": home_name, "away_name": away_name}, None

# ---------------- UI ----------------
st.set_page_config(page_title="MLB Parlay — OddsJam API Key Setup", page_icon="🔐")
st.title("⚾ MLB Parlay — Fair Odds + Kelly (OddsJam API)")

with st.expander("Step 1: Enter your OddsJam/OpticOdds API key", expanded=True):
    user_key = st.text_input("API key (this overrides secrets while the app is running)", type="password", help="Paste your OddsJam/OpticOdds API key here to test quickly.")
    st.caption("Alternatively, add it in Streamlit Cloud → Settings → Secrets as `ODDSJAM_API_KEY`.")

colA, colB = st.columns([1,1])
with colA:
    if st.button("Test connection"):
        key = resolve_api_key(user_key)
        if not key:
            st.error("No API key found. Enter it above or add to Secrets as ODDSJAM_API_KEY.")
        else:
            data, err = list_leagues(key)
            if err:
                st.error(f"Leagues error: {err}")
            else:
                leagues = data.get("data") if isinstance(data, dict) else data
                st.success("✅ API key works!")
                st.json(leagues[:5] if isinstance(leagues, list) else leagues)

with colB:
    st.caption("Connection test calls /leagues?sport=baseball and shows the first items.")

st.divider()
st.subheader("Parlay builder")

key = resolve_api_key(user_key)
if not key:
    st.warning("Enter your API key above or in Secrets to continue.")
    st.stop()

# Leagues
ldata, lerr = list_leagues(key)
if lerr or not ldata:
    st.error(f"Leagues error: {lerr}")
    st.stop()

leagues = ldata.get("data", [])
if not leagues:
    st.error("No baseball leagues returned. Check your plan/permissions or try again later.")
    st.stop()

# Pick MLB (or let user choose)
display = [f"{l.get('name')} (id={l.get('id')})" for l in leagues]
default_idx = 0
for i, l in enumerate(leagues):
    nm = (l.get("name","") or "").lower()
    if "mlb" in nm or "major league baseball" in nm or l.get("id") == "mlb":
        default_idx = i
        break
choice = st.selectbox("League", display, index=default_idx)
league_id = choice.split("id=")[1].split(")")[0]

# Fixtures
fdata, ferr = list_active_fixtures(key, league_id)
if ferr or not fdata:
    st.error(f"Fixtures error: {ferr}")
    st.stop()

fixtures = fdata.get("data", [])
if not fixtures:
    st.warning("No active fixtures right now for that league.")
    st.stop()

# Build leg options (home/away for each fixture)
options = []
fixture_lookup = {}
for fx in fixtures:
    fid = fx.get("id")
    home = fx.get("home_team_display")
    away = fx.get("away_team_display")
    start = fx.get("start_date")
    label_home = f"{home} vs {away} — pick {home} (home) — {start} — id:{fid}::home"
    label_away = f"{home} vs {away} — pick {away} (away) — {start} — id:{fid}::away"
    options.append(label_home); fixture_lookup[label_home] = (fid,"home")
    options.append(label_away); fixture_lookup[label_away] = (fid,"away")

picks = st.multiselect("Choose ML legs", options=options)
boosted_input = st.text_input("Boosted final odds (e.g., +450 or 5.50)", value="+450")
sportsbook = st.selectbox("Sportsbook", ["Pinnacle","FanDuel","DraftKings","BetMGM"], index=0)

if st.button("Calculate FAIR value & Kelly"):
    if not picks:
        st.warning("Pick at least one leg.")
        st.stop()

    legs = []
    parlay_prob = 1.0
    parlay_dec = 1.0

    for pick in picks:
        fid, side = fixture_lookup[pick]
        prices, perr = get_fixture_odds_moneyline(key, fid, sportsbook)
        if perr or not prices:
            st.error(f"Odds error for fixture {fid}: {perr}")
            st.stop()
        if prices["home"] is None or prices["away"] is None:
            st.error(f"{sportsbook} moneyline not available for fixture {fid}. Try another book.")
            st.stop()
        p_home = implied_prob_from_american(prices["home"])
        p_away = implied_prob_from_american(prices["away"])
        f_home, f_away = devig_two_way(p_home, p_away)
        fair_prob = f_home if side == "home" else f_away
        fair_dec = 1.0 / fair_prob if fair_prob else None
        legs.append({
            "fixture": fid,
            "home": prices["home_name"],
            "away": prices["away_name"],
            "price_home": prices["home"],
            "price_away": prices["away"],
            "side": side,
            "fair_prob": fair_prob,
            "fair_dec": fair_dec,
            "fair_amer": decimal_to_american(fair_dec) if fair_dec else None
        })
        parlay_prob *= fair_prob
        parlay_dec *= fair_dec

    st.subheader("Legs")
    for lg in legs:
        team = lg["home"] if lg["side"]=="home" else lg["away"]
        opp  = lg["away"] if lg["side"]=="home" else lg["home"]
        st.markdown(
            f"- **{team}** vs **{opp}** ({lg['side']}) — {sportsbook} ML: "
            f"Home {lg['price_home']:+}, Away {lg['price_away']:+} → "
            f"**Fair prob** {lg['fair_prob']:.3f} (**Fair dec** {lg['fair_dec']:.3f}, **Fair Amer** {lg['fair_amer']:+})"
        )

    st.divider()
    st.subheader("Parlay FAIR value")
    st.metric("Fair decimal odds", f"{parlay_dec:.3f}", delta=f"Fair American {decimal_to_american(parlay_dec):+}")
    st.caption(f"Fair parlay probability ≈ {parlay_prob:.4f}")

    offered_dec = parse_boosted_odds(boosted_input)
    if not offered_dec:
        st.error("Couldn't parse boosted odds. Enter like +450 or 5.50")
        st.stop()

    k_full = kelly_fraction(parlay_prob, offered_dec)
    if k_full is None:
        st.error("Kelly fraction could not be computed (check boosted odds).")
        st.stop()

    k_quarter = max(0.0, k_full * 0.25)
    b = offered_dec - 1.0
    ev_per_dollar = parlay_prob * b - (1.0 - parlay_prob)

    st.divider()
    st.subheader("Kelly staking")
    st.write(f"**Offered (boosted) decimal odds:** {offered_dec:.3f} (American {decimal_to_american(offered_dec):+})")
    st.write(f"**Fair win probability (parlay):** {parlay_prob:.4%}")
    st.write(f"**Full Kelly fraction:** {k_full:.3%}")
    st.success(f"**Quarter‑Kelly stake:** {k_quarter:.3%} of bankroll")
    st.caption(f"Expected value per $1 staked at offered odds: {ev_per_dollar:.4f}")
