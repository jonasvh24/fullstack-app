# Pocket Heist

A Roblox tycoon-raid game in the Steal-a-Brainrot genre. You buy Critters off a conveyor, they earn cash on your base, you steal rivals' Critters and lock your base against raids. The Critters are original characters; no third-party meme IP.

All code is Luau in `src/`, synced into Roblox Studio with Rojo. The map is built from code at server start, so there is no binary place file to merge.

## Status

| Milestone | Scope | State |
|---|---|---|
| M0 | Project folder, toolchain, config, pure-logic utils, tests | Done |
| M1 | Plots, conveyor, buy, passive income, collect, save/load | Next |
| M2 | Stealing, base lock | |
| M3 | Rebirth, leaderboards, HUD polish, onboarding | |
| M4 | Mutations, timed events, monetisation, publish | |

Services and controllers marked with a later milestone are stubs that boot cleanly but do nothing yet.

## Setup

1. Install [Rokit](https://github.com/rojo-rbx/rokit), then run `rokit install` in this folder. That fetches the pinned Rojo, Selene, StyLua and Lune versions from `rokit.toml`.
2. Install the Rojo plugin in Studio: `rojo plugin install`.
3. Run `rojo serve`, open a blank Baseplate in Studio and press **Connect** in the Rojo plugin panel.

No Rokit? `cargo install --locked rojo@7.7.1 stylua@2.6.0 selene@0.31.0 lune@0.11.0` gives the same versions.

## Daily commands

| Task | Command |
|---|---|
| Live sync to Studio | `rojo serve` |
| Build a place file | `rojo build -o PocketHeist.rbxlx` |
| Unit tests (headless) | `lune run tests/run` |
| Lint | `selene src` |
| Format | `stylua src tests` |
| Format check (CI) | `stylua --check src tests` |

## Layout

```
src/shared/   -> ReplicatedStorage.Shared       config, remotes, pure utils
src/server/   -> ServerScriptService.Server     services (Init/Start)
src/client/   -> StarterPlayerScripts.Client    controllers (Init/Start)
tests/        Lune specs for everything in src/shared that avoids Roblox APIs
```

**Rules of the codebase**

- **The server is authoritative.** Clients send intents (`BuyCritter`, `StealCritter`, `LockBase`, `Rebirth`). The server checks ownership, cash, distance (`Economy.INTERACT_RANGE`) and `RateLimiter` before acting.
- **All balance numbers live in `src/shared/Config`.** `tests/Economy.spec.luau` enforces the payback curve: a rarer Critter never pays back faster than a commoner one.
- **Pure logic stays Roblox-free** so Lune can test it. Anything that touches instances is checked by a Studio playtest.

## Playtesting

- **Single player:** press Play in Studio.
- **Raiding needs two players:** Test → Clients and Servers → 2 players → Start.
- **Saving data:** Game Settings → Security → *Enable Studio Access to API Services*, then publish the place once so it has a DataStore.
