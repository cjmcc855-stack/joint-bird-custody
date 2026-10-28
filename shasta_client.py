"""
shasta_client.py - drop-in client for creatures living in shasta-world (PROTOCOL.md v1).

Copy this file into your creature. Standard library only. It never raises and never blocks for long:
if the town can't be reached, calls return (False, None) and your creature keeps living on its own.

    world = WorldClient("http://100.x.y.z:8650", token, offline_cb=print)
    ok, p = world.hello(place="nest", state="awake")      # on startup / reconnect
    ok, p = world.perceive()                               # start of every tick
    ok, r = world.act("move", tick=191, to="lake")         # one action, at the end of a successful tick
    world.ack(tick=191, delivery_ids=[...])                # after your tick has SAVED the delivered items
"""
import json, time, urllib.error, urllib.request, uuid

PROTOCOL = 1


class WorldClient:
    def __init__(self, url, token, timeout=8, offline_cb=None):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.online, self.retry_at, self.backoff = True, 0.0, 60
        self.cursor = None                      # newest event id you've seen; pass it back to perceive
        self.last_error = None
        self.offline_cb = offline_cb or (lambda msg: None)

    # ---- plumbing ----
    def _call(self, method, path, body=None, timeout=None):
        """Returns (status, json) or (None, None) if the town is unreachable."""
        if not self.online and time.time() < self.retry_at:
            return None, None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                status, out = r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                status, out = e.code, json.loads(e.read() or b"{}")
            except ValueError:
                status, out = e.code, {}
            if status >= 500:
                return self._down(f"server error {status}")
        except (OSError, ValueError) as e:      # refused, timed out, DNS, Tailscale down, junk reply...
            return self._down(str(e))
        if not self.online:
            self.offline_cb("shasta-world is reachable again")
        self.online, self.backoff = True, 60
        if out.get("protocol") not in (None, PROTOCOL):
            self.last_error = {"code": "protocol_mismatch", "message": f"server speaks protocol {out.get('protocol')}"}
            return self._down(self.last_error["message"])
        return status, out

    def _down(self, why):
        if self.online:
            self.offline_cb(f"shasta-world unreachable ({why}); living on my own for now")
        self.online, self.retry_at = False, time.time() + self.backoff
        self.backoff = min(self.backoff * 2, 900)                     # 1, 2, 4, 8, 15 minutes
        return None, None

    def _result(self, status, out):
        if status is None:
            return False, None
        if out.get("ok"):
            self.last_error = None
            return True, out.get("result")
        self.last_error = out.get("error") or {"code": f"http_{status}", "message": ""}
        return False, self.last_error

    # ---- the protocol ----
    def health(self):
        status, out = self._call("GET", "/v1/health")
        return status == 200

    def world(self):
        return self._result(*self._call("GET", "/v1/world"))

    def hello(self, place, state="awake"):
        ok, p = self._result(*self._call("POST", "/v1/hello", {"place": place, "state": state}))
        if ok:
            self.cursor = p["cursor"]
        return ok, p

    def perceive(self, wait=0):
        """wait > 0 long-polls: returns as soon as something new happens where you are (max 25s)."""
        q = f"?since={self.cursor}" if self.cursor is not None else ""
        if wait and self.cursor is not None:
            q += f"&wait={min(wait, 25)}"
        ok, p = self._result(*self._call("GET", "/v1/perceive" + q, timeout=self.timeout + (wait or 0)))
        if ok:
            self.cursor = p["cursor"]
        return ok, p

    def act(self, kind, tick, **fields):
        """One action. Retries the SAME action_id once on a network blip, so it can never apply twice.
        Returns (True, result) or (False, error) where error is {"code", "message", "retry_after"?},
        or (False, None) if the town is unreachable."""
        body = dict(fields, type=kind, tick=int(tick), action_id=f"{tick}-{kind}-{uuid.uuid4().hex[:12]}")
        for attempt in range(2):
            status, out = self._call("POST", "/v1/act", body)
            if status is not None:
                return self._result(status, out)
            if attempt == 0:
                self.online, self.retry_at = True, 0       # one immediate retry before giving up
                self.backoff = max(60, self.backoff // 2)  # (don't count the blip twice)
        return False, None

    def ack(self, tick, delivery_ids):
        if not delivery_ids:
            return True, {"acked": []}
        return self.act("ack", tick, delivery_ids=list(delivery_ids))
