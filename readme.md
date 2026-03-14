# Titanic KungFu Offensive (TKO) Rehost

This repository contains a Python emulator for **Titanic KungFu Offensive**, the discontinued Cartoon Network Flash fighting game that originally ran on **SmartFoxServer 1.x**.

The project now restores the full connection flow and a playable multiplayer match loop:

- Flash policy server
- SmartFox TCP emulation
- BlueBox HTTP emulation
- static file serving for the Flash assets
- login, lobby, matchmaking, match join, round start, rematch
- server-authoritative `cnGame` simulation with synthesized `su` snapshots
- character specials loaded from the original `4_0/*.xml` files

## Live Server

**The public server is currently down, but will be hosted at [`tko.hoody.cx`](http://tko.hoody.cx/GameContainer.swf).**

If you want the easiest way to launch the game, use **Ruffle Desktop** from the official downloads page:

- [Ruffle Desktop](https://ruffle.rs/downloads)

Then open this URL inside Ruffle Desktop:

```text
http://tko.hoody.cx/GameContainer.swf
```

That URL loads the game container directly from the live server.

## Current Status

The project is no longer in the “basic connectivity only” stage. The current server can already do the following:

- login works
- lobby and room list work
- matchmaking works
- both players enter the match
- rounds start and progress without the old immediate-DRAW bug
- timer, camera, movement, attacks, throws, health, meter, and rematch logic are server-driven
- character special buttons are mapped by grouped move families from the original XML, so buttons land on entry-point animations instead of hold/drop/miss frames
- in-match XT packets now include the SmartFox room-id slot so result, rematch, and visual-effect packets parse correctly on the client
- BlueBox fallback works
- the same HTTP server now serves both the Flash assets and the BlueBox endpoint

## Still In Progress

The remaining work is mostly polish and fidelity against the original game footage:

- tighten special-effect packet behavior for every character
- continue validating round-start announcer timing and round-transition audio against recorded gameplay
- improve projectile/effect lifecycle fidelity
- continue refining “super KO” presentation and per-character attack visuals
- keep matching camera, hit reactions, and round flow as closely as possible to the original game

## How the Original Game Connects

TKO uses two network paths:

### TCP Socket

```text
Port: 9339
Protocol: SmartFox XML socket traffic
Delimiter: NULL byte (\x00)
```

### BlueBox HTTP Tunnel

```text
Endpoint: /BlueBox/HttpBox.do
Port: 80
Transport: HTTP POST
Parameter: sfsHttp
```

The emulator supports both.

## Repository Contents

### `i.cartoonnetwork.com/games/tko/tko_server.py`

Combined emulator and asset host. Current responsibilities:

- Flash policy server
- SmartFox TCP server
- BlueBox HTTP server
- static HTTP file serving
- lobby and matchmaking
- authoritative in-match simulation
- character special parsing from original XML

## Running the Server

Run the server from the repository directory:

```bash
python i.cartoonnetwork.com/games/tko/tko_server.py --bind 0.0.0.0 --advertise-ip YOUR.PUBLIC.IP --static-dir i.cartoonnetwork.com/games/tko --write-cnsl "i.cartoonnetwork.com/games/tko/cnsl.xml"
```

Important note:

- Port `80` often requires elevated privileges or an admin shell, depending on your OS and firewall setup.

### Main options

- `--bind`
  Bind address for TCP, HTTP, and policy services.
- `--advertise-ip`
  IP address written into `cnsl.xml`.
- `--write-cnsl`
  Writes a `cnsl.xml` for the client.
- `--static-dir`
  Directory served over HTTP. This should usually be `i.cartoonnetwork.com/games/tko`.

### Useful defaults

```text
--tcp-port      9339
--http-port     80
--policy-port   843
--static-port   8000
```

## Public Hosting

If you want players on the internet to connect to your server, forward these TCP ports to the host machine:

- `80`
- `843`
- `9339`

The current matchmaking is already server-global, so players who point at the same public server will enter the same public queue and be matched there.

For quick monitoring, the server also exposes:

```text
http://YOUR.HOST/status.json
```

## Development Notes

This emulator has been reconstructed using:

- packet logging
- decompiled ActionScript
- exported XML/SWF assets
- live Ruffle testing
- comparison against surviving gameplay footage

The current focus is fidelity, not just connectivity.

Recent findings:

- forcing the load handshake too early can trigger round announcer audio before the actual stage round begins
- the original `specialAnimations` lists contain both button-entry animations and follow-up phase animations (`START` / `FLY` / `HIT` / `MISS` / `SUPER`), so button mapping needs to target entry-point move families rather than blindly consuming hold/drop/miss frames
- several server-authored in-match packets must include the SmartFox room-id field on the wire, or the client misreads `rndo`, `win`, `rmch`, and synthesized effect packets

## Disclaimer

This project is for preservation and educational purposes only.

All original game assets and intellectual property belong to **Cartoon Network / Turner Broadcasting**.
