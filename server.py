#!/usr/bin/env python3
"""
server.py - the shared town under Mount Shasta. Reference implementation of PROTOCOL.md (v1).

Creatures (Bolt, Spike, ...) are clients: each tick they PERCEIVE what's around them, DECIDE with their
own brain, and ACT. This server owns only what's shared: places, who is where, items lying around,
the sky, and what happened at each place. Everything personal stays in each creature.

Run:   python server.py                 (reads config.json; copy config.example.json first)
Test:  python tests/contract_test.py    (starts its own throwaway server in test mode)

Python 3.10+, standard library only.
"""
import copy, hashlib, json, os, random, re, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = 1
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- LIMITS (published at GET /v1/world so creatures can plan around them) ----------------
LIMITS = {
    "say_gap_seconds": 30,          # one creature can't say two things within 30s
    "pair_streak": 16,              # 16 creature-to-creature lines in a row (8 back-and-forths) with no human...
    "pair_rest_seconds": 1800,      # ...and that pair rests for 30 min (a human_spoke from either ends it)
    "pair_daily_says": 60,          # lines one creature can say to another per day
    "daily_says": 200,              # lines one creature can say per day, total
    "repeat_window": 20,            # saying the exact same thing as one of your last 20 lines is refused
    "gifts_per_pair_per_day": 3,
    "leave_items_per_day": 20,      # human hides (!leave) relayed by one creature per day
    "deeds_per_window": 10,         # move/take/drop/give/do: at most 10...
    "deed_window_seconds": 300,     # ...per 5 minutes
    "requests_per_minute": 120,
    "quiet_after_seconds": 600,     # no request from a creature for 10 min -> shown as "quiet"
    "max_wait_seconds": 25,         # longest long-poll
    "say_max_chars": 400,
    "note_max_chars": 140,
    "name_max_chars": 120,
    "body_max_bytes": 8192,
}
EVENTS_PER_PLACE = 80               # ring buffer per place
EVENT_MAX_AGE = 1800                # perception only shows events from the last 30 minutes
PERCEIVE_EVENTS = 20
ACTIONS_REMEMBERED = 1000           # per creature, for idempotent retries
DEED_TYPES = {"move", "take", "drop", "give", "do"}

CONFIG, DATA, CLOCK = {}, {}, {"offset": 0.0}
LOCK = threading.RLock()
CHANGED = threading.Condition(LOCK)
STATE = {}


def now():
    return time.time() + CLOCK["offset"]


def iso(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def today(t=None):
    return datetime.fromtimestamp(t if t is not None else now()).strftime("%Y-%m-%d")


# ---------------- STORAGE ----------------
def state_path():
    return os.path.join(CONFIG.get("data_dir", HERE), "world_state.json")


def blank_state():
    return {"next_event": 1, "next_item": 1, "next_delivery": 1, "creatures": {}, "items": {}, "events": {},
            "deliveries": {}, "actions": {}, "ambient_at": {},
            "limits": {"last_say": {}, "recent": {}, "pairs": {}, "daily": {}, "deeds": {}, "minute": {}}}


def load_state():
    for fp in (state_path(), state_path() + ".bak"):
        try:
            with open(fp, encoding="utf-8") as f:
                st = json.load(f)
            base = blank_state()
            for k, v in base.items():
                st.setdefault(k, v)
            for k, v in base["limits"].items():
                st["limits"].setdefault(k, v)
            return st
        except (FileNotFoundError, ValueError):
            continue
    return blank_state()


def save_state():
    """Atomic: write a temp file, keep the last good save as .bak, swap in."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    for _ in range(20):
        try:
            if os.path.exists(path):
                with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                    dst.write(src.read())
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    raise RuntimeError("couldn't save world_state.json")


# ---------------- THE SKY ----------------
SKY = {"kind": "clear", "temp_f": None, "wind_mph": None, "sunrise": "06:30", "sunset": "18:00", "real": False, "t": 0}


def weather_kind(code, temp, wind):
    """Same buckets Bolt has always used, so both birds describe the same sky the same way."""
    kind = ("thunderstorm" if code >= 95 else "snow" if code in (71, 73, 75, 77, 85, 86)
            else "rain" if code in (63, 65, 67, 81, 82) else "light rain" if 51 <= code <= 82
            else "foggy" if code in (45, 48) else "cloudy" if code in (2, 3) else "clear")
    if kind in ("clear", "cloudy") and wind >= 20:
        kind = "windy"
    if kind == "clear" and temp <= 40:
        kind = "cold and bright"
    return kind


def refresh_sky():
    if CONFIG.get("test_mode") or time.time() - SKY["t"] < 1800:
        return
    spot = DATA["weather_spot"]
    q = urllib.parse.urlencode({"latitude": spot["lat"], "longitude": spot["lon"],
                                "current": "temperature_2m,weather_code,wind_speed_10m", "daily": "sunrise,sunset",
                                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto",
                                "forecast_days": 1})
    try:
        req = urllib.request.Request(f"https://api.open-meteo.com/v1/forecast?{q}",
                                     headers={"User-Agent": "shasta-world/1 (hobby project)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        cur = d["current"]
        code, temp, wind = int(cur["weather_code"]), round(cur["temperature_2m"]), round(cur["wind_speed_10m"])
        SKY.update(kind=weather_kind(code, temp, wind), temp_f=temp, wind_mph=wind, real=True, t=time.time(),
                   sunrise=d["daily"]["sunrise"][0][-5:], sunset=d["daily"]["sunset"][0][-5:])
    except Exception:
        SKY["t"] = time.time() - 1500             # offline: keep the last sky, try again in 5 minutes


def season(t=None):
    m = datetime.fromtimestamp(t if t is not None else now()).month
    return "winter" if m in (12, 1, 2) else "spring" if m in (3, 4, 5) else "summer" if m in (6, 7, 8) else "fall"


def sky_now():
    t = now()
    hm = datetime.fromtimestamp(t).strftime("%H:%M")
    marm = DATA["marmots"]
    return {"kind": SKY["kind"], "temp_f": SKY["temp_f"], "wind_mph": SKY["wind_mph"], "real": SKY["real"],
            "season": season(t), "sunrise": SKY["sunrise"], "sunset": SKY["sunset"],
            "is_day": SKY["sunrise"] <= hm < SKY["sunset"],
            "marmots_awake": datetime.fromtimestamp(t).month in marm["awake_months"]}


# ---------------- WORLD HELPERS ----------------
def resolve_place(name):
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    if key in DATA["places"]:
        return key
    key2 = key.replace(" ", "_")
    if key2 in DATA["places"]:
        return key2
    return DATA["aliases"].get(key) or DATA["aliases"].get(key2)


def presence(cid, t=None):
    c = STATE["creatures"].get(cid)
    if not c:
        return None
    t = t if t is not None else now()
    if t - c["last_seen"] > LIMITS["quiet_after_seconds"]:
        return "quiet"
    return c["state"]


def creature_public(cid):
    info, c = CONFIG["creatures"][cid], STATE["creatures"].get(cid, {})
    return {"id": cid, "name": info["name"], "kind": info.get("kind", ""), "place": c.get("place"),
            "state": presence(cid) or "unknown", "since": iso(c["since"]) if c.get("since") else None}


def add_event(place, kind, who, text, **extra):
    ev = {"id": STATE["next_event"], "t": iso(now()), "ts": now(), "place": place, "kind": kind, "who": who,
          "text": text}
    ev.update({k: v for k, v in extra.items() if v is not None})
    STATE["next_event"] += 1
    ring = STATE["events"].setdefault(place, [])
    ring.append(ev)
    del ring[:-EVENTS_PER_PLACE]
    CHANGED.notify_all()
    return ev


def public_event(ev):
    return {k: v for k, v in ev.items() if k != "ts"}


def maybe_ambient(place):
    """Background life (trains, the guard dog...), added lazily when someone looks: about every 6-15 minutes."""
    t = now()
    if t < STATE["ambient_at"].get(place, 0):
        return
    STATE["ambient_at"][place] = t + random.uniform(360, 900)
    p = DATA["places"][place]
    pool = list(p.get("ambient", [])) + DATA["season_ambient"].get(season(t), {}).get(place, [])
    marm = DATA["marmots"]
    if datetime.fromtimestamp(t).month not in marm["awake_months"]:
        pool = [e for e in pool if "marmot" not in e]
        if place in marm["places"]:
            pool.append(marm["asleep_line"])
    if pool:
        add_event(place, "ambient", None, random.choice(pool))


def deliver(to, sender, item, why):
    d = {"delivery_id": f"dl_{STATE['next_delivery']}", "from": sender, "item": item, "why": why, "t": iso(now())}
    STATE["next_delivery"] += 1
    STATE["deliveries"].setdefault(to, []).append(d)
    CHANGED.notify_all()
    return d


def perception(cid, since=None):
    c = STATE["creatures"][cid]
    place = c["place"]
    maybe_ambient(place)
    t = now()
    ring = [e for e in STATE["events"].get(place, []) if t - e["ts"] <= EVENT_MAX_AGE]
    evs = [e for e in ring if e["id"] > since] if since is not None else ring[-10:]
    p = DATA["places"][place]
    others = [k for k in CONFIG["creatures"] if k != cid and k in STATE["creatures"]]
    return {
        "now": iso(t),
        "cursor": STATE["next_event"] - 1,
        "you": creature_public(cid),
        "place": {"id": place, "name": p["name"], "description": p["description"], "features": p["features"],
                  "home_of": p.get("home_of")},
        "sky": sky_now(),
        "here": [creature_public(k) for k in others if STATE["creatures"][k]["place"] == place],
        "elsewhere": [{"id": k, "name": CONFIG["creatures"][k]["name"], "place": STATE["creatures"][k]["place"],
                       "state": presence(k)} for k in others if STATE["creatures"][k]["place"] != place],
        "items": [{k: v for k, v in i.items() if k != "ts"} for i in STATE["items"].get(place, [])],
        "events": [public_event(e) for e in evs[-PERCEIVE_EVENTS:]],
        "deliveries": list(STATE["deliveries"].get(cid, [])),
    }


def has_news(cid, since):
    """Long-poll wakes for: anything new at your place, or a delivery."""
    c = STATE["creatures"].get(cid)
    if not c:
        return False
    if STATE["deliveries"].get(cid):
        return True
    return any(e["id"] > since and e["who"] != cid for e in STATE["events"].get(c["place"], []))


# ---------------- ERRORS ----------------
class Refuse(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after

    def body(self):
        err = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            err["retry_after"] = max(1, int(self.retry_after + 0.999))
        return {"ok": False, "protocol": PROTOCOL, "error": err}


def text_field(body, key, max_len, required=True):
    v = body.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise Refuse(400, "bad_request", f"'{key}' must be a non-empty string")
    v = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", v).strip()
    if len(v) > max_len:
        raise Refuse(400, "too_long", f"'{key}' is limited to {max_len} characters")
    return v


def item_field(body):
    item = body.get("item")
    if not isinstance(item, dict):
        raise Refuse(400, "bad_request", "'item' must be an object like {\"name\": \"a blue feather\"}")
    return {"name": text_field(item, "name", LIMITS["name_max_chars"])}


def count(bucket, key, t=None, add=0):
    day = STATE["limits"]["daily"].setdefault(today(t), {})
    for old in [d for d in STATE["limits"]["daily"] if d != today(t)]:
        del STATE["limits"]["daily"][old]
    b = day.setdefault(bucket, {})
    b[key] = b.get(key, 0) + add
    return b[key]


def norm(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:300]


# ---------------- ACTIONS ----------------
def check_deed_rate(cid, t):
    lst = [x for x in STATE["limits"]["deeds"].get(cid, []) if t - x < LIMITS["deed_window_seconds"]]
    if len(lst) >= LIMITS["deeds_per_window"]:
        raise Refuse(429, "too_many_deeds", "slow down: too many actions in the last 5 minutes",
                     lst[0] + LIMITS["deed_window_seconds"] - t)
    lst.append(t)
    STATE["limits"]["deeds"][cid] = lst


def needs_company(cid, other, verb):
    if other not in CONFIG["creatures"]:
        raise Refuse(404, "unknown_creature", f"there's no creature called '{other}'")
    if other == cid:
        raise Refuse(400, "bad_request", f"you can't {verb} yourself")
    oc = STATE["creatures"].get(other)
    if not oc or oc["place"] != STATE["creatures"][cid]["place"]:
        raise Refuse(409, "not_here", f"{CONFIG['creatures'][other]['name']} isn't here")
    if presence(other) == "quiet":
        raise Refuse(409, "quiet", f"{CONFIG['creatures'][other]['name']} has gone quiet (not checking in)")


def act_move(cid, body, t):
    c = STATE["creatures"][cid]
    to = resolve_place(text_field(body, "to", 60))
    if not to:
        raise Refuse(404, "unknown_place", f"there's no place called '{body.get('to')}'",)
    if to == c["place"]:
        raise Refuse(409, "already_here", f"you're already at {DATA['places'][to]['name']}")
    rule = DATA.get("travel", {}).get(to, {})
    if rule.get("only_from") and c["place"] not in rule["only_from"]:
        names = " or ".join(DATA["places"][p]["name"] for p in rule["only_from"])
        raise Refuse(409, "travel_blocked", f"you can only get to {DATA['places'][to]['name']} from {names}")
    if SKY["kind"] in rule.get("closed_in", []):
        raise Refuse(409, "travel_blocked", f"{DATA['places'][to]['name']} is closed off: {SKY['kind']}")
    check_deed_rate(cid, t)
    name, frm = CONFIG["creatures"][cid]["name"], c["place"]
    add_event(frm, "depart", cid, f"{name} flies off toward {DATA['places'][to]['name']}", to_place=to)
    c["place"], c["since"] = to, t
    ev = add_event(to, "arrive", cid, f"{name} arrives from {DATA['places'][frm]['name']}", from_place=frm)
    return {"place": to}, ev


def act_say(cid, body, t):
    text = text_field(body, "text", LIMITS["say_max_chars"])
    to = body.get("to")
    name, place = CONFIG["creatures"][cid]["name"], STATE["creatures"][cid]["place"]
    if to is not None:
        needs_company(cid, to, "talk to")
        if presence(to) == "asleep":
            raise Refuse(409, "asleep", f"{CONFIG['creatures'][to]['name']} is asleep")
        partners = [to]
    else:
        partners = [k for k, c in STATE["creatures"].items()
                    if k != cid and k in CONFIG["creatures"] and c["place"] == place and presence(k) == "awake"]
    L = STATE["limits"]
    last = L["last_say"].get(cid, 0)
    if t - last < LIMITS["say_gap_seconds"]:
        raise Refuse(429, "too_fast", "you just said something; give it a moment", last + LIMITS["say_gap_seconds"] - t)
    if norm(text) in L["recent"].get(cid, []):
        raise Refuse(409, "repeat", "you just said that")
    if count("says", cid, t) >= LIMITS["daily_says"]:
        raise Refuse(429, "daily_cap", "you've talked a lot today; rest your voice until tomorrow",
                     (datetime.fromtimestamp(t).replace(hour=0, minute=0, second=0) + timedelta(days=1)).timestamp() - t)
    for p in partners:
        pair = L["pairs"].get("|".join(sorted((cid, p))), {})
        if t < pair.get("rest_until", 0):
            raise Refuse(429, "resting", f"you and {CONFIG['creatures'][p]['name']} have talked a lot; take a break",
                         pair["rest_until"] - t)
        if count("pair_says", f"{cid}>{p}", t) >= LIMITS["pair_daily_says"]:
            raise Refuse(429, "daily_cap", f"you've said plenty to {CONFIG['creatures'][p]['name']} today")
    L["last_say"][cid] = t
    L["recent"][cid] = (L["recent"].get(cid, []) + [norm(text)])[-LIMITS["repeat_window"]:]
    count("says", cid, t, 1)
    rested = []
    for p in partners:
        count("pair_says", f"{cid}>{p}", t, 1)
        key = "|".join(sorted((cid, p)))
        pair = L["pairs"].setdefault(key, {"count": 0, "rest_until": 0})
        pair["count"] += 1
        if pair["count"] >= LIMITS["pair_streak"]:
            pair.update(count=0, rest_until=t + LIMITS["pair_rest_seconds"])
            rested.append(p)
    ev = add_event(place, "say", cid, f"{name}: {text}", to=to, said=text)
    return {"heard_by": partners, "rest_started_with": rested}, ev


def act_human_spoke(cid, body, t):
    by = text_field(body, "by", 40)
    for key, pair in STATE["limits"]["pairs"].items():
        if cid in key.split("|"):
            pair.update(count=0, rest_until=0)
    c = STATE["creatures"][cid]
    ev = add_event(c["place"], "human", cid, f"{by} is talking with {CONFIG['creatures'][cid]['name']}", by=by)
    return {"breaks_ended": True}, ev


def act_give(cid, body, t):
    to = body.get("to")
    needs_company(cid, to, "give a gift to")
    item = item_field(body)
    if count("gifts", f"{cid}>{to}", t) >= LIMITS["gifts_per_pair_per_day"]:
        raise Refuse(429, "gift_cap", f"you've already given {CONFIG['creatures'][to]['name']} plenty today")
    check_deed_rate(cid, t)
    count("gifts", f"{cid}>{to}", t, 1)
    d = deliver(to, cid, item, "gift")
    name = CONFIG["creatures"][cid]["name"]
    ev = add_event(STATE["creatures"][cid]["place"], "give", cid,
                   f"{name} gives {CONFIG['creatures'][to]['name']} {item['name']}", to=to, item=item)
    return {"delivery_id": d["delivery_id"]}, ev


def act_take(cid, body, t):
    iid = text_field(body, "item_id", 40)
    place = STATE["creatures"][cid]["place"]
    items = STATE["items"].get(place, [])
    hit = next((i for i in items if i["id"] == iid), None)
    if not hit:
        raise Refuse(409, "item_gone", "that isn't here anymore")
    check_deed_rate(cid, t)
    items.remove(hit)
    item = {"name": hit["name"], "left_by": hit.get("left_by")}
    d = deliver(cid, None, item, "taken")
    ev = add_event(place, "take", cid, f"{CONFIG['creatures'][cid]['name']} picks up {hit['name']}", item=item)
    return {"delivery_id": d["delivery_id"], "item": item}, ev


def place_item(place, name, left_by, t):
    it = {"id": f"it_{STATE['next_item']}", "name": name, "left_by": left_by, "since": iso(t), "ts": t}
    STATE["next_item"] += 1
    STATE["items"].setdefault(place, []).append(it)
    return it


def act_drop(cid, body, t):
    item = item_field(body)
    check_deed_rate(cid, t)
    place = STATE["creatures"][cid]["place"]
    it = place_item(place, item["name"], cid, t)
    ev = add_event(place, "drop", cid, f"{CONFIG['creatures'][cid]['name']} leaves {item['name']} here", item=item)
    return {"item_id": it["id"]}, ev


def act_leave_item(cid, body, t):
    place = resolve_place(text_field(body, "place", 60))
    if not place:
        raise Refuse(404, "unknown_place", f"there's no place called '{body.get('place')}'")
    item, by = item_field(body), text_field(body, "by", 40)
    if count("leave", cid, t) >= LIMITS["leave_items_per_day"]:
        raise Refuse(429, "daily_cap", "that's a lot of hidden things for one day")
    count("leave", cid, t, 1)
    it = place_item(place, item["name"], by, t)
    ev = add_event(place, "leave_item", None, f"{by} hid {item['name']} here", item=item, by=by)
    return {"item_id": it["id"], "place": place}, ev


def act_do(cid, body, t):
    activity = text_field(body, "activity", 40)
    if not re.fullmatch(r"[a-z][a-z_ ]*", activity):
        raise Refuse(400, "bad_request", "'activity' is a short lowercase word like 'fish' or 'sift'")
    place = STATE["creatures"][cid]["place"]
    feature = body.get("feature")
    if feature is not None and feature not in DATA["places"][place]["features"]:
        raise Refuse(409, "no_such_feature", f"there's no {feature} at {DATA['places'][place]['name']}")
    note = text_field(body, "note", LIMITS["note_max_chars"], required=False)
    check_deed_rate(cid, t)
    name = CONFIG["creatures"][cid]["name"]
    ev = add_event(place, "do", cid, note if note else f"{name} is busy: {activity.replace('_', ' ')}",
                   activity=activity, feature=feature)
    return {}, ev


def act_sleep(cid, body, t):
    c = STATE["creatures"][cid]
    c["state"], c["since"] = "asleep", t
    return {"state": "asleep"}, add_event(c["place"], "sleep", cid, f"{CONFIG['creatures'][cid]['name']} falls asleep")


def act_wake(cid, body, t):
    c = STATE["creatures"][cid]
    c["state"], c["since"] = "awake", t
    return {"state": "awake"}, add_event(c["place"], "wake", cid, f"{CONFIG['creatures'][cid]['name']} wakes up")


def act_ack(cid, body, t):
    ids = body.get("delivery_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise Refuse(400, "bad_request", "'delivery_ids' must be a list of strings")
    pending = STATE["deliveries"].get(cid, [])
    acked = [d["delivery_id"] for d in pending if d["delivery_id"] in ids]
    STATE["deliveries"][cid] = [d for d in pending if d["delivery_id"] not in ids]
    return {"acked": acked}, None


ACTS = {"move": act_move, "say": act_say, "human_spoke": act_human_spoke, "give": act_give, "take": act_take,
        "drop": act_drop, "leave_item": act_leave_item, "do": act_do, "sleep": act_sleep, "wake": act_wake,
        "ack": act_ack}
ASLEEP_OK = {"wake", "ack", "human_spoke", "leave_item"}


def handle_act(cid, body):
    aid, tick, kind = body.get("action_id"), body.get("tick"), body.get("type")
    if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", aid):
        raise Refuse(400, "bad_request", "'action_id' must be 1-80 characters of letters, digits, _ . : -")
    if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
        raise Refuse(400, "bad_request", "'tick' must be a whole number >= 0")
    if kind not in ACTS:
        raise Refuse(400, "unknown_action", f"unknown action type '{kind}'. Known: {', '.join(sorted(ACTS))}")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    seen = STATE["actions"].setdefault(cid, {})
    if aid in seen:
        if seen[aid]["hash"] != digest:
            raise Refuse(409, "action_id_reused", "that action_id was already used for a different action")
        return seen[aid]["status"], dict(seen[aid]["body"], replayed=True)
    c = STATE["creatures"].get(cid)
    if not c:
        raise Refuse(409, "hello_first", "say hello first (POST /v1/hello) so the town knows where you are")
    if c["state"] == "asleep" and kind not in ASLEEP_OK:
        raise Refuse(409, "you_are_asleep", "you're asleep; send a wake action first")
    snapshot = copy.deepcopy(STATE)                   # all-or-nothing: a refusal changes nothing
    try:
        result, ev = ACTS[kind](cid, body, now())
    except Refuse:
        STATE.clear()
        STATE.update(snapshot)
        raise                                         # refusals aren't remembered: nothing happened, so a retry re-checks
    out = {"ok": True, "protocol": PROTOCOL, "result": result, "event": public_event(ev) if ev else None}
    seen = STATE["actions"].setdefault(cid, {})
    seen[aid] = {"hash": digest, "status": 200, "body": out}
    while len(seen) > ACTIONS_REMEMBERED:
        del seen[next(iter(seen))]
    return 200, out


def handle_hello(cid, body):
    place = resolve_place(text_field(body, "place", 60))
    if not place:
        raise Refuse(404, "unknown_place", f"there's no place called '{body.get('place')}'")
    state = body.get("state", "awake")
    if state not in ("awake", "asleep"):
        raise Refuse(400, "bad_request", "'state' is 'awake' or 'asleep'")
    t = now()
    c = STATE["creatures"].get(cid)
    name = CONFIG["creatures"][cid]["name"]
    if not c or c["place"] != place:
        STATE["creatures"][cid] = {"place": place, "state": state, "since": t, "last_seen": t}
        add_event(place, "arrive", cid, f"{name} shows up")
    else:
        if c["state"] != state:
            c["state"], c["since"] = state, t
        c["last_seen"] = t
    return perception(cid)


def world_info():
    return {"protocol": PROTOCOL, "town": DATA["town"], "limits": LIMITS, "travel": DATA.get("travel", {}),
            "features": DATA["features"], "aliases": DATA["aliases"],
            "places": {k: {kk: vv for kk, vv in v.items() if kk != "ambient"} for k, v in DATA["places"].items()},
            "creatures": [creature_public(k) for k in CONFIG["creatures"]], "sky": sky_now(), "now": iso(now())}


# ---------------- HTTP ----------------
TOKENS = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "shasta-world/1"

    def log_message(self, *a):
        if CONFIG.get("log_requests"):
            super().log_message(*a)

    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def creature(self):
        auth = self.headers.get("Authorization", "")
        cid = TOKENS.get(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if not cid:
            raise Refuse(401, "unauthorized", "missing or wrong token")
        t = now()
        lst = [x for x in STATE["limits"]["minute"].get(cid, []) if t - x < 60]
        if len(lst) >= LIMITS["requests_per_minute"]:
            raise Refuse(429, "slow_down", "too many requests", lst[0] + 60 - t)
        lst.append(t)
        STATE["limits"]["minute"][cid] = lst
        if cid in STATE["creatures"]:
            STATE["creatures"][cid]["last_seen"] = t     # every request is a heartbeat
        return cid

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > LIMITS["body_max_bytes"]:
            raise Refuse(413, "too_large", f"requests are limited to {LIMITS['body_max_bytes']} bytes")
        try:
            out = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Refuse(400, "bad_json", "the body isn't valid JSON") from None
        if not isinstance(out, dict):
            raise Refuse(400, "bad_json", "the body must be a JSON object")
        return out

    def route(self, method):
        url = urllib.parse.urlparse(self.path)
        path, query = url.path.rstrip("/"), urllib.parse.parse_qs(url.query)
        if method == "GET" and path == "/v1/health":
            return 200, {"ok": True, "protocol": PROTOCOL, "now": iso(now())}
        if path.startswith("/v1/_test/"):
            return self.test_hook(method, path)
        with LOCK:
            cid = self.creature()
            refresh_sky()
            if method == "GET" and path == "/v1/world":
                return 200, {"ok": True, "protocol": PROTOCOL, "result": world_info()}
            if method == "POST" and path == "/v1/hello":
                out = handle_hello(cid, self.body())
                save_state()
                return 200, {"ok": True, "protocol": PROTOCOL, "result": out}
            if method == "GET" and path == "/v1/perceive":
                if cid not in STATE["creatures"]:
                    raise Refuse(409, "hello_first", "say hello first (POST /v1/hello)")
                try:
                    since = int(query["since"][0]) if "since" in query else None
                    wait = min(float(query.get("wait", ["0"])[0]), LIMITS["max_wait_seconds"])
                except ValueError:
                    raise Refuse(400, "bad_request", "'since' is an event id and 'wait' is seconds") from None
                if since is not None and wait > 0:
                    end = time.time() + wait
                    while not has_news(cid, since) and time.time() < end:
                        CHANGED.wait(end - time.time())
                out = perception(cid, since)
                save_state()
                return 200, {"ok": True, "protocol": PROTOCOL, "result": out}
            if method == "POST" and path == "/v1/act":
                status, out = handle_act(cid, self.body())
                save_state()
                return status, out
        raise Refuse(404, "not_found", f"no such endpoint: {method} {path}")

    def test_hook(self, method, path):
        """Only in test_mode: lets the contract tests move the clock and change the weather."""
        if not CONFIG.get("test_mode") or method != "POST":
            raise Refuse(404, "not_found", "no such endpoint")
        body = self.body()
        with LOCK:
            if path == "/v1/_test/clock":
                CLOCK["offset"] += float(body.get("advance", 0))
            elif path == "/v1/_test/weather":
                SKY["kind"] = str(body.get("kind", "clear"))
            elif path == "/v1/_test/ambient_off":
                for p in DATA["places"]:
                    STATE["ambient_at"][p] = float("inf")
            else:
                raise Refuse(404, "not_found", "no such test hook")
            return 200, {"ok": True, "now": iso(now()), "sky": SKY["kind"]}

    def dispatch(self, method):
        try:
            status, body = self.route(method)
        except Refuse as r:
            status, body = r.status, r.body()
        except Exception as e:                        # a bug: say so, don't crash the server
            print(f"[{iso(now())}] error: {e!r}", file=sys.stderr)
            status, body = 500, {"ok": False, "protocol": PROTOCOL, "error": {"code": "server_error", "message": str(e)}}
        self.reply(status, body)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


def load(config_path):
    global CONFIG, DATA, STATE
    with open(config_path, encoding="utf-8") as f:
        CONFIG = json.load(f)
    base = os.path.dirname(os.path.abspath(config_path))
    CONFIG["data_dir"] = os.path.join(base, CONFIG.get("data_dir", "data"))
    with open(os.path.join(HERE, "world_data.json"), encoding="utf-8") as f:
        DATA = json.load(f)
    TOKENS.clear()
    for cid, info in CONFIG["creatures"].items():
        tok = str(info.get("token", ""))
        if len(tok) < 32 or "CHANGE" in tok:
            sys.exit(f"config: creature '{cid}' needs a real token (32+ random characters)")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", cid):
            sys.exit(f"config: creature id '{cid}' must be lowercase letters/digits/_")
        TOKENS[tok] = cid
    STATE = load_state()
    for k in list(STATE["creatures"]):
        if k not in CONFIG["creatures"]:
            del STATE["creatures"][k]


def main():
    cfg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    if not os.path.exists(cfg):
        sys.exit("No config.json. Copy config.example.json to config.json and fill in the tokens.")
    load(cfg)
    bind, port = CONFIG.get("bind", "127.0.0.1"), int(CONFIG.get("port", 8650))
    srv = Server((bind, port), Handler)
    print(f"shasta-world (protocol {PROTOCOL}) listening on http://{bind}:{port}"
          + ("  [TEST MODE]" if CONFIG.get("test_mode") else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
