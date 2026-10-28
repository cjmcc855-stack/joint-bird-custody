# CLAUDE.md: read this first

You're working on **shasta-world**, the shared town under Mount Shasta where two autonomous AI birds live:

- **Bolt**, a crow: Caelan's. Repo `cjmcc855-stack/bolt`. Local model (Ollama, deepseek-r1:14b on an RTX 3060).
- **Spike**, a Steller's jay: Elliott's. Repo `GangSwan/spike-ai`. gpt-5-nano with a daily cost cap.

Two different Claude sessions work on this repo (Caelan's and Elliott's) and they can't talk to each other.
This file, [PROTOCOL.md](PROTOCOL.md) and [DECISIONS.md](DECISIONS.md) are how they stay in sync. Read all three
before changing anything.

## What this repo is, and isn't

- It **is** the town: places, who's where, items lying around, the sky, local events, and the limits on how
  creatures talk to each other. `server.py` is the reference server and `PROTOCOL.md` is the contract.
- It **isn't** either bird. Memories, skills, personalities, hoards, art, music and Discord diaries stay in each
  creature's own repo. Never move private creature state into the town.
- `shasta_client.py` is a drop-in client a creature copies into its own repo. Keep it standard library only,
  and keep it so it never raises and never blocks for long.

## Rules

1. **PROTOCOL.md is the contract.** Any behavior change to `server.py` that a client could notice has to land
   in the same pull request as the matching PROTOCOL.md change, a DECISIONS.md entry, and a contract test.
   If code and protocol disagree, that's a bug: say so in the PR, don't quietly pick one.
2. **Additive only within v1.** New optional fields, event kinds, action types, places and features are fine.
   Removing or changing the meaning of anything means `/v2` (PROTOCOL.md section 10).
3. **Run the contract tests before every PR:** `python tests/contract_test.py` must end with `PROBLEMS: 0`.
   Bug fixes get a check that would have caught the bug.
4. **Standard library only, Python 3.10+.** Both birds are stdlib-only and run on Windows. No f-string tricks
   that need 3.12, and no backslashes inside f-string braces.
5. **Never commit secrets or live data:** `config.json` (tokens) and `data/` (the live town) are git-ignored.
   Never print, log or echo a token. Don't open token files.
6. **Don't edit the other person's creature.** Changes to Bolt go to Caelan's repo, changes to Spike go to
   Elliott's. If the town needs something from a creature, write it in the PR description or in DECISIONS.md
   under "Asks", addressed to that creature's owner.
7. **Decisions waiting on a human stay open.** Anything marked *(proposed)* in PROTOCOL.md or DECISIONS.md
   needs both Caelan and Elliott to agree. Don't build on a proposed decision as if it were settled. Do flag
   it if the work depends on it.
8. **A creature must be able to live without the town.** Nothing in the protocol may make a creature wait on
   the server, crash when it's down, or lose anything if it restarts mid-tick (PROTOCOL.md sections 7 and 9).

## Things both owners care about (don't break these)

- **Talking at normal speed.** Caelan wants creatures to talk like they would with a person, not one line
  every 5 minutes. Loop protection comes from the limits in PROTOCOL.md section 8, not from slowing talk down.
- **Bolt's gifts are sacred.** Anything a human gave him can never be lost, used up, taken or given away.
- **Bolt restarts only when Caelan types `!restart`**, never automatically on a code change. His GPU watchdog
  stops him at 87°C. So the town has to cope with him vanishing at any moment (that's what "quiet" is for).
- **Spike has a money budget.** Nothing in the protocol should push a creature into extra model calls.
  Perceiving and acting are free; only thinking costs.
- **Personal places.** Some places are personal to Caelan (the old barn and the machine shop especially).
  Keep their descriptions as they are unless Caelan asks.

## How a change normally goes

1. Read the open items in DECISIONS.md.
2. Make the change in a branch: server, protocol text and contract test together.
3. Run `python tests/contract_test.py`.
4. Open a PR describing what changes for a creature, and tag which owner needs to review.
5. Add a DECISIONS.md entry with the date, what was decided, and why.

## Layout

```
server.py            the town (reference server)
world_data.json      places, features, travel rules, aliases, ambient lines (generated from Bolt's town + Spike's fir)
shasta_client.py     drop-in client for creatures
PROTOCOL.md          the contract
DECISIONS.md         what's decided, what's open, asks for each owner
tests/contract_test.py
config.example.json  copy to config.json (git-ignored) and fill in tokens
```
