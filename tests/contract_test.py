#!/usr/bin/env python3
"""
contract_test.py - checks a shasta-world server against PROTOCOL.md v1.

    python tests/contract_test.py              starts a throwaway server in test mode and runs everything
    python tests/contract_test.py --keep       same, but leaves the temp folder for a look afterwards

Any server that claims protocol 1 must pass this. Any creature client can also use it as a list of
exactly how the town answers. Standard library only. Ends with PROBLEMS: 0 when everything holds.
"""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PROBLEMS = []
TOK = {"bolt": "b" * 40, "spike": "s" * 40, "wren": "w" * 40}


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        PROBLEMS.append(what)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Town:
    def __init__(self, keep=False):
        self.dir = tempfile.mkdtemp(prefix="shasta_test_")
        self.keep = keep
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = {"bind": "127.0.0.1", "port": self.port, "data_dir": "data", "test_mode": True,
               "creatures": {"bolt": {"name": "Bolt", "kind": "crow", "token": TOK["bolt"]},
                             "spike": {"name": "Spike", "kind": "Steller's jay", "token": TOK["spike"]},
                             "wren": {"name": "Wren", "kind": "wren", "token": TOK["wren"]}}}
        self.cfg = os.path.join(self.dir, "config.json")
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)
        self.proc = None
        self.start()

    def start(self):
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), self.cfg],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/v1/health", timeout=1)
                return
            except OSError:
                time.sleep(0.05)
        raise SystemExit("server didn't start:\n" + (self.proc.stdout.read() if self.proc.poll() is not None else ""))

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)

    def call(self, method, path, body=None, who="bolt", raw=None, token=None):
        headers = {"Content-Type": "application/json"}
        tok = token if token is not None else TOK.get(who)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def act(self, who, kind, n=[0], **fields):
        n[0] += 1
        body = dict(fields, type=kind, tick=n[0], action_id=f"t{n[0]}")
        return self.call("POST", "/v1/act", body, who=who)

    def hello(self, who, place, state="awake"):
        return self.call("POST", "/v1/hello", {"place": place, "state": state}, who=who)

    def perceive(self, who, since=None, wait=None):
        q = []
        if since is not None:
            q.append(f"since={since}")
        if wait is not None:
            q.append(f"wait={wait}")
        return self.call("GET", "/v1/perceive" + ("?" + "&".join(q) if q else ""), who=who)

    def test(self, path, body=None):
        return self.call("POST", f"/v1/_test/{path}", body or {})

    def advance(self, seconds):
        self.test("clock", {"advance": seconds})


def code(resp):
    return (resp[1].get("error") or {}).get("code")


def run(keep):
    T = Town(keep)
    try:
        T.test("ambient_off")
        print("1. basics")
        s, b = T.call("GET", "/v1/health", token="")
        check(s == 200 and b.get("protocol") == 1, "health answers without a token and says protocol 1")
        check(T.call("GET", "/v1/world", token="nope" * 10)[0] == 401, "a wrong token gets 401")
        check(code(T.call("POST", "/v1/hello", raw=b"{not json")) == "bad_json", "junk JSON gets bad_json")
        check(T.call("POST", "/v1/hello", raw=b"x" * 9000)[0] == 413, "an oversized body gets 413")
        s, b = T.call("GET", "/v1/world")
        w = b.get("result", {})
        check(s == 200 and "creek" in w.get("places", {}) and "fir" in w["places"], "GET /v1/world lists the places, incl. the fir")
        check(w.get("aliases", {}).get("river_canyon") == "creek", "Spike's 'river_canyon' is an alias for the creek")
        check("pair_streak" in w.get("limits", {}), "limits are published in /v1/world")
        check(code(T.perceive("bolt")) == "hello_first", "perceive before hello is refused with hello_first")
        check(code(T.act("bolt", "move", to="lake")) == "hello_first", "acting before hello is refused with hello_first")

        print("2. hello and perception")
        check(code(T.hello("bolt", "atlantis")) == "unknown_place", "hello to a made-up place is refused")
        s, b = T.hello("bolt", "nest")
        p = b["result"]
        check(s == 200 and p["you"]["place"] == "nest" and p["place"]["home_of"] == "bolt", "hello puts Bolt in his nest")
        for k in ("now", "cursor", "you", "place", "sky", "here", "elsewhere", "items", "events", "deliveries"):
            check(k in p, f"perception has '{k}'")
        check({"kind", "season", "is_day", "marmots_awake"} <= set(p["sky"]), "sky has kind, season, is_day, marmots_awake")
        s, b = T.hello("spike", "river canyon")
        check(b["result"]["you"]["place"] == "creek", "hello resolves aliases ('river canyon' -> creek)")
        T.hello("spike", "nest")
        p = T.perceive("bolt")[1]["result"]
        check([c["id"] for c in p["here"]] == ["spike"], "Bolt sees Spike when Spike is at the nest")
        check(any(e["kind"] == "arrive" and e["who"] == "spike" for e in p["events"]), "Bolt sees Spike arrive")
        cur = p["cursor"]
        check(T.perceive("bolt", since=cur)[1]["result"]["events"] == [], "perceive since=cursor shows nothing old")

        print("3. moving")
        check(code(T.act("bolt", "move", to="summit")) == "travel_blocked", "can't fly straight to the summit")
        check(code(T.act("bolt", "move", to="nest")) == "already_here", "moving to where you are is refused")
        check(code(T.act("bolt", "move", to="moon")) == "unknown_place", "moving to a made-up place is refused")
        s, b = T.act("bolt", "move", to="trail")
        check(s == 200 and b["result"]["place"] == "mountain_trail", "move resolves aliases ('trail')")
        T.test("weather", {"kind": "thunderstorm"})
        check(code(T.act("bolt", "move", to="summit")) == "travel_blocked", "the summit is closed in a thunderstorm")
        T.test("weather", {"kind": "clear"})
        check(T.act("bolt", "move", to="summit")[0] == 200, "the summit is open from the trail in clear weather")
        ev = T.perceive("spike", since=cur)[1]["result"]["events"]
        check(any(e["kind"] == "depart" and e["who"] == "bolt" for e in ev), "Spike saw Bolt leave the nest")
        el = T.perceive("spike")[1]["result"]["elsewhere"]
        check(any(c["id"] == "bolt" and c["place"] == "summit" for c in el), "Spike knows Bolt is at the summit")
        T.act("bolt", "move", to="mountain_trail")
        T.act("bolt", "move", to="lake")
        T.act("spike", "move", to="lake")

        print("4. talking")
        check(code(T.act("bolt", "say", text="hey", to="wren")) == "not_here", "talking to someone who isn't here is refused")
        check(code(T.act("bolt", "say", text="hey", to="nobody")) == "unknown_creature", "talking to a made-up creature is refused")
        check(code(T.act("bolt", "say", text="hey", to="bolt")) == "bad_request", "talking to yourself (to=self) is refused")
        s, b = T.act("bolt", "say", text="Spike! the trout are jumping", to="spike")
        check(s == 200 and b["result"]["heard_by"] == ["spike"], "Bolt talks to Spike at the lake")
        r = T.act("bolt", "say", text="seriously look", to="spike")
        check(code(r) == "too_fast" and r[1]["error"].get("retry_after", 0) >= 1, "a second line within 30s is too_fast, with retry_after")
        T.advance(31)
        check(code(T.act("bolt", "say", text="Spike! the trout are jumping", to="spike")) == "repeat", "repeating yourself is refused")
        check(code(T.act("bolt", "say", text="x" * 401, to="spike")) == "too_long", "lines over 400 characters are refused")
        ev = T.perceive("spike", since=cur)[1]["result"]["events"]
        said = [e for e in ev if e["kind"] == "say"]
        check(said and said[-1]["said"] == "Spike! the trout are jumping" and said[-1]["to"] == "spike", "Spike hears exactly what Bolt said")

        print("5. long conversations get a break")
        turns, refused = 0, None
        for i in range(40):
            who, other = ("spike", "bolt") if i % 2 == 0 else ("bolt", "spike")
            T.advance(31)
            r = T.act(who, "say", text=f"line number {i} about fish and feathers {i * 7}", to=other)
            if r[0] != 200:
                refused = r
                break
            turns += 1
        check(refused is not None and code(refused) == "resting", f"after {turns + 1} lines in a row they're told to rest")
        check(turns + 1 == 16 or turns == 15, "the break starts at the 16th creature-only line")
        T.advance(31)
        T.act("bolt", "human_spoke", by="Caelan")
        check(T.act("spike", "say", text="oh hi Caelan", to="bolt")[0] == 200, "a human talking ends the break early")
        for i in range(15):
            T.advance(31)
            T.act("bolt" if i % 2 == 0 else "spike", "say", text=f"round two {i} {i * 13}", to="spike" if i % 2 == 0 else "bolt")
        T.advance(31)
        check(code(T.act("bolt", "say", text="one more thing", to="spike")) == "resting", "the second streak rests again")
        T.advance(1801)
        T.perceive("spike")                    # (Spike checks in, or he'd count as quiet after 30 silent minutes)
        check(T.act("bolt", "say", text="ok I'm back", to="spike")[0] == 200, "after 30 minutes they can talk again")

        print("6. sleep and quiet")
        T.act("spike", "sleep")
        T.advance(31)
        check(code(T.act("bolt", "say", text="psst", to="spike")) == "asleep", "you can't talk to a sleeping creature")
        check(code(T.act("spike", "move", to="fir")) == "you_are_asleep", "a sleeping creature can't move")
        check(T.act("spike", "wake")[0] == 200, "wake works")
        T.advance(601)
        T.perceive("bolt")
        T.advance(31)
        check(code(T.act("bolt", "say", text="are you there", to="spike")) == "quiet", "a creature that stopped checking in shows as quiet")
        check(T.perceive("spike")[0] == 200, "Spike checks in again")
        T.advance(31)
        check(T.act("bolt", "say", text="there you are", to="spike")[0] == 200, "and can be talked to again")

        print("7. items lying around")
        s, b = T.act("bolt", "leave_item", place="creek", item={"name": "a spinner lure"}, by="Caelan")
        check(s == 200, "Bolt relays Caelan hiding a lure at the creek (from anywhere)")
        T.act("spike", "move", to="creek")
        items = T.perceive("spike")[1]["result"]["items"]
        lure = next((i for i in items if i["name"] == "a spinner lure"), None)
        check(lure and lure["left_by"] == "Caelan", "Spike finds the lure at the creek, left by Caelan")
        s, b = T.act("spike", "take", item_id=lure["id"])
        check(s == 200 and b["result"]["delivery_id"].startswith("dl_"), "taking it creates a delivery to the taker")
        T.act("bolt", "move", to="creek")
        check(code(T.act("bolt", "take", item_id=lure["id"])) == "item_gone", "things exist once: Bolt can't take it too")
        d = T.perceive("spike")[1]["result"]["deliveries"]
        check(len(d) == 1 and d[0]["item"]["name"] == "a spinner lure", "the delivery shows up in Spike's perception")
        check(T.perceive("spike")[1]["result"]["deliveries"] == d, "and keeps showing up until it's acked (crash-safe)")
        T.act("spike", "ack", delivery_ids=[d[0]["delivery_id"]])
        check(T.perceive("spike")[1]["result"]["deliveries"] == [], "ack clears it")
        check(T.act("spike", "ack", delivery_ids=[d[0]["delivery_id"]])[1]["result"]["acked"] == [], "acking twice is harmless")
        s, b = T.act("bolt", "drop", item={"name": "a bent washer"})
        check(s == 200 and any(i["name"] == "a bent washer" for i in T.perceive("spike")[1]["result"]["items"]), "dropped things lie where they fell")

        print("8. gifts")
        s, b = T.act("spike", "give", to="bolt", item={"name": "a blue feather"})
        check(s == 200, "Spike gives Bolt a feather")
        d = T.perceive("bolt")[1]["result"]["deliveries"]
        check(any(x["from"] == "spike" and x["why"] == "gift" for x in d), "Bolt gets it as a delivery from Spike")
        T.act("spike", "give", to="bolt", item={"name": "a pinecone scale"})
        T.act("spike", "give", to="bolt", item={"name": "a sap bead"})
        check(code(T.act("spike", "give", to="bolt", item={"name": "a fourth thing"})) == "gift_cap", "a 4th gift in a day is refused")
        check(code(T.act("bolt", "give", to="wren", item={"name": "x"})) == "not_here", "you can only give to someone who's here")
        T.act("bolt", "sleep")
        T.hello("wren", "creek")
        check(T.act("wren", "give", to="bolt", item={"name": "a seed"})[0] == 200, "you can leave a gift beside a sleeping creature")
        T.act("bolt", "wake")

        print("9. retries can't double anything")
        body = {"type": "drop", "tick": 999, "action_id": "same-id-1", "item": {"name": "a marble"}}
        r1 = T.call("POST", "/v1/act", body, who="bolt")
        r2 = T.call("POST", "/v1/act", body, who="bolt")
        n = sum(1 for i in T.perceive("bolt")[1]["result"]["items"] if i["name"] == "a marble")
        check(r1[0] == r2[0] == 200 and r2[1].get("replayed") is True and n == 1, "the same action_id twice applies once, second says replayed")
        check(r2[1]["result"] == r1[1]["result"], "a replay returns the original answer")
        body2 = dict(body, item={"name": "a different marble"})
        check(code(T.call("POST", "/v1/act", body2, who="bolt")) == "action_id_reused", "reusing an action_id for something else is refused")
        check(code(T.call("POST", "/v1/act", {"type": "dance", "tick": 1, "action_id": "z1"})) == "unknown_action", "unknown action types are refused")
        check(code(T.call("POST", "/v1/act", {"type": "do", "tick": -1, "action_id": "z2", "activity": "fish"})) == "bad_request", "a negative tick is refused")

        print("10. doing things")
        check(T.act("bolt", "do", activity="sift", feature="river_gravel", note="Bolt sifts the gravel bar")[0] == 200, "sifting river gravel at the creek works")
        check(code(T.act("bolt", "do", activity="tinker", feature="workbench")) == "no_such_feature", "there's no workbench at the creek")
        ev = T.perceive("spike", since=0)[1]["result"]["events"]
        check(any(e["kind"] == "do" and e["text"] == "Bolt sifts the gravel bar" for e in ev), "Spike sees what Bolt is doing")
        T.advance(400)
        got = 0
        for _ in range(12):
            if T.act("wren", "do", activity="hop")[0] == 200:
                got += 1
        check(got == 10, f"at most 10 deeds per 5 minutes ({got} went through)")

        print("11. long-poll")
        pend = T.perceive("bolt")[1]["result"]["deliveries"]
        T.act("bolt", "ack", delivery_ids=[d["delivery_id"] for d in pend])     # pending deliveries also wake a wait
        cur = T.perceive("bolt")[1]["result"]["cursor"]
        T.advance(31)
        timer = threading.Timer(0.6, lambda: T.act("spike", "say", text="wake up and look at this", to="bolt"))
        timer.start()
        t0 = time.time()
        p = T.perceive("bolt", since=cur, wait=10)[1]["result"]
        dt = time.time() - t0
        timer.join()
        check(0.4 < dt < 5 and any(e["kind"] == "say" and e["who"] == "spike" for e in p["events"]),
              f"a waiting perceive returns as soon as Spike talks ({dt:.1f}s)")
        t0 = time.time()
        T.perceive("bolt", since=T.perceive("bolt")[1]["result"]["cursor"], wait=1)
        check(0.8 < time.time() - t0 < 4, "with nothing new it waits out the timeout")

        print("12. the client helper against a live town")
        from shasta_client import WorldClient
        T.advance(301)                         # (Wren used up his deeds in section 10)
        wc = WorldClient(T.url, TOK["wren"])
        ok, p = wc.hello(place="lake")
        check(ok and p["you"]["place"] == "lake" and wc.cursor == p["cursor"], "WorldClient.hello works and keeps the cursor")
        ok, r = wc.act("move", tick=5, to="summit")
        check(not ok and r["code"] == "travel_blocked" and wc.online, "a refusal comes back as (False, error) and the client stays online")
        ok, r = wc.act("drop", tick=5, item={"name": "a wren feather"})
        check(ok and r["item_id"].startswith("it_"), "a deed comes back as (True, result)")
        T.act("spike", "move", to="lake") if T.perceive("spike")[1]["result"]["you"]["place"] != "lake" else None
        T.advance(31)
        T.act("spike", "give", to="wren", item={"name": "a pinecone"})
        ok, p = wc.perceive()
        dl = [d["delivery_id"] for d in p["deliveries"]]
        check(ok and dl, "the gift shows up in the client's perception")
        ok, r = wc.ack(tick=6, delivery_ids=dl)
        check(ok and r["acked"] == dl and wc.perceive()[1]["deliveries"] == [], "WorldClient.ack clears it")

        print("13. survives a restart")
        before = T.perceive("bolt")[1]["result"]
        T.stop()
        T.start()
        after = T.perceive("bolt")[1]["result"]
        check(after["you"]["place"] == before["you"]["place"], "positions survive a server restart")
        check([i["id"] for i in after["items"]] == [i["id"] for i in before["items"]], "items survive a server restart")
        check(after["cursor"] >= before["cursor"], "the event cursor never goes backwards")
    finally:
        T.stop()
        if keep:
            print(f"\n(temp folder kept: {T.dir})")
        else:
            shutil.rmtree(T.dir, ignore_errors=True)

    print("14. client helper with the town down")
    from shasta_client import WorldClient
    msgs = []
    c = WorldClient(f"http://127.0.0.1:{free_port()}", "x" * 40, timeout=1, offline_cb=msgs.append)
    t0 = time.time()
    ok, p = c.perceive()
    ok2, r = c.act("move", tick=1, to="lake")
    check(not ok and p is None and not ok2 and r is None, "with the town down, calls return (False, None) instead of raising")
    check(time.time() - t0 < 5 and msgs and not c.online, "it gives up fast and reports going offline")
    t0 = time.time()
    c.perceive()
    check(time.time() - t0 < 0.1, "while backing off it doesn't even try (no waiting on a dead server)")


if __name__ == "__main__":
    run("--keep" in sys.argv)
    print(f"\nPROBLEMS: {len(PROBLEMS)}")
    for p in PROBLEMS:
        print("  " + p)
    sys.exit(1 if PROBLEMS else 0)
