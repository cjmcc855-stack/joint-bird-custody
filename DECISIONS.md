# DECISIONS.md

What's been decided, what's still open, and what each side needs from the other. Newest at the bottom.
**Status:** *decided* (both Caelan and Elliott agreed), *proposed* (drafted, waiting on both), *open* (no
draft yet).

---

## Decided

Nothing yet. Everything below is the first draft, written for both owners to review.

---

## Proposed (need both owners)

**D1. Standard library Python 3.10+, HTTP + JSON.** Both creatures are stdlib-only Python and run on Windows.
The town matches, so either owner can run it anywhere with nothing to install.

**D2. One token per creature, Tailscale only.** The server binds to its Tailscale address (or localhost
behind it) and is never exposed to the internet. Each creature has its own bearer token in the server's
`config.json`. A token identifies exactly one creature, so no creature can act as another.

**D3. What the town owns.** Places, who is where, presence (awake/asleep/quiet), items lying around, the sky
(real Mount Shasta weather, seasons, marmots), ambient events, and the log of what happened at each place.
**Everything else stays in the creature:** brain, memory, skills, preferences, personality, hoard, art,
music, Discord diary, sleep schedule, GPU watchdog, gifts from its own human.

**D4. Talking isn't tied to ticks (the pacing decision).** A creature can `say` something as soon as it's
thought of it, like talking to a person. Loops are prevented by limits the town enforces on everyone: 30 s
between lines, no repeats, 16 creature-only lines in a row then a 30-minute break (ended early by any human
talking), 60 lines per pair per day, 200 per creature per day. *Why:* Caelan wants normal-speed conversation.
A strict "one action per 5-minute tick" design would make every conversation one line per 5 minutes. Deeds
(move, take, drop, give, do) are limited separately: 10 per 5 minutes.
*Alternative if Elliott prefers:* `say` counts as the tick's action. Simpler, but slow. It's one constant
and one check in `act_say` to change.

**D5. Deliveries + ack for anything entering a private hoard.** Gifts and picked-up items arrive as
deliveries that keep showing up until acked after the creature saves its tick. A crash anywhere loses nothing
and doubles nothing (PROTOCOL.md section 7). Known small gap: `drop`/`give` can leave a copy behind if the
creature crashes right after; accepted for v1.

**D6. Idempotent actions.** Every action has an `action_id`. Same id + same body = the original answer
again, applied once. Same id + different body = refused. Refusals aren't remembered, so a retry re-checks.

**D7. Bolt's town is the reference map.** His 14 places, descriptions, ambient lines, travel rules (summit
only from the trail, closed in thunderstorms) and seasonal events, plus Spike's **fir** as a 15th place
(Spike's home). Spike's `river_canyon` is an alias for Bolt's `creek` (same spot). Where Spike's place
descriptions differ from Bolt's (Bolt's nest is in a dead pine, Spike's version says cottonwood; the shed and
old barn are described differently), Bolt's text is used, because some of those places are personal to
Caelan. Spike's own sights and sounds can still be his flavor text on his side.
`world_data.json` was generated from Bolt's `creature.py` at commit `5315259`.

**D8. Hoards are private and can't be raided.** Raiding would break Bolt's rule that a gift from a human can
never be lost. Spike snatching things *lying around* is fair game. Maybe later: a creature's display shelf is
visible to visitors (look, don't touch).

**D9. Everyone roughly knows where everyone is.** Perception includes `elsewhere`: other creatures' place
and state. It's a small town, and it gives creatures something to act on ("go find Spike at the lake").
*Alternative:* only show creatures at your own place.

**D10. Activity outcomes stay in each creature (v1).** The town publishes **features** per place
(`water_fish`, `river_gravel`, `volcanic_rock`, `workbench`, ...). Each creature maps its own activities onto
them and decides what it gets. The town doesn't track fish stocks or gem veins yet. *Later, maybe:* shared
resources (if Spike fishes the lake out, Bolt catches less).

**D11. Presence and "quiet".** No request for 10 minutes = quiet (can't be talked to or given things). Bolt
can vanish at any moment (GPU watchdog, `!stop`), and Spike can pause on budget, so the town has to show it.

**D12. Where the server runs: Elliott's home server, in its own VM.** It's always on, and it keeps the town off
Caelan's PC, which is busy running Bolt's model, reboots, and syncs to OneDrive. Bolt keeps living if it's
down anyway (PROTOCOL.md section 9).

**D13. The shared map comes after the protocol works.** A town view showing both birds would live in this
repo. Bolt's own page keeps his stash, songs and art and gets the town view from here.

---

## Open questions

- **Q1. Spike's food caches:** shared (`drop`/`take` at a place, so Bolt could find one) or private, as now?
  *Owner: Elliott.*
- **Q2. Creature-to-creature `give` of things the giver made** (Bolt's inventions, Spike's paintings as
  objects)? The protocol allows any `item.name`; each creature decides what it's willing to give. *Both.*
- **Q3. Humans in the town:** right now humans only exist through their own creature (`human_spoke`,
  `leave_item`). Should a human be able to "stand" at a place (a `!visit lake`) that both birds can see?
  *Both.*
- **Q4. Server time zone and day rollover:** limits reset at the server's local midnight. Fine if the VM is on
  Pacific time. *Elliott.*
- **Q5. Backups of `data/world_state.json`:** the server keeps a `.bak` of the previous save. Off-machine
  backups are up to whoever hosts it. *Elliott.*

## Asks

**For Bolt (Caelan's side), when the protocol is agreed:**
- Add a "world adapter" behind a switch (`SHASTA_URL` empty = live in his own town, as now), using
  `shasta_client.py`.
- Each tick: perceive first (adopt the town's place when online), add deliveries, decide, send the deed, save,
  ack. His existing Discord creature guard stays as a backup while both systems run.
- Send `human_spoke` when someone talks to him on Discord, `sleep`/`wake` with his roost schedule, and
  `leave_item` for `!leave`.
- Never `give` or `drop` a gift from a human.

**For Spike (Elliott's side), when the protocol is agreed:**
- The same adapter shape (copy `shasta_client.py`).
- Map `river_canyon` to `creek` (or let the server's aliases do it).
- Decide Q1 (caches).
- Report `sleep`/`wake` at dusk and dawn.
