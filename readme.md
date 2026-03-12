# Titanic KungFu Offensive (TKO) Rehost

This repository contains a work-in-progress Python emulator for **Titanic KungFu Offensive**, a discontinued Cartoon Network Flash game that originally relied on **SmartFoxServer 1.x**.

The goal of the project is to restore the original multiplayer flow by recreating the networking stack the Flash client expects:

- SmartFox XML socket traffic
- BlueBox HTTP tunneling
- Flash socket policy serving
- enough game-specific `cnGame` behavior to reach a full multiplayer round

---

## Current Status

The project is **well past basic connectivity** and is now in the final multiplayer protocol-debugging phase.

### Working now

- Flash client connects successfully
- version check and login succeed
- lobby / room list flow works
- BlueBox fallback is implemented
- two clients can join the same match
- character select works
- stage loading works
- round intro / countdown flow works
- the incorrect immediate **DRAW** at round start has been fixed

### Current blocker

The game is now stuck in the **live round synchronization** stage.

What this means in practice:

- both players load into the same match
- both characters drop into the arena
- the round appears to begin
- local input is reaching the server and affecting the scene
- but the server is still not emitting the final live sync packet layout the client expects, so movement / camera / timer behavior is not yet correct

In other words: **startup, matchmaking, and round bootstrapping mostly work; the remaining issue is authoritative in-round sync.**

---

## Multiplayer Findings So Far

These are the main protocol discoveries from debugging the client and server:

- `rndo` is **not** a round-start packet; sending it at startup causes the client to immediately resolve the round as a result screen
- `cu` appears to carry **player input / pad bitfields**, not full world state
- `fr` is not sufficient by itself to decide that live gameplay has started
- the missing piece now appears to be the exact **server-returned live sync packet layout** used after the countdown
- timer handling is also tied to the same live-sync phase and still needs final alignment

This is why the project is now very close visually, but not yet fully playable.

---

## How the Original Game Connected

TKO used **SmartFoxServer 1.x** with two transport paths.

### TCP Socket

Primary multiplayer connection.

```text
Port: 9339
Protocol: SmartFox XML messages
Delimiter: NULL byte (\x00)
```

### BlueBox HTTP Tunnel

Fallback method when direct sockets were blocked.

```text
Endpoint: /BlueBox/HttpBox.do
Port: 8080
Transport: HTTP POST
Parameter: sfsHttp
```

The emulator implements both paths.

---

## Repository Contents

### `tko_server.py`

Combined SmartFox / BlueBox emulator.

Current responsibilities include:

- SmartFox TCP server
- BlueBox HTTP tunnel server
- Flash socket policy server
- room / lobby handling
- matchmaking
- basic match orchestration
- game-specific `cnGame` packet handling under active development

---

## Running the Server

Install Python 3, then run the server from the repository directory.

### Usage example

```bash
python tko_server.py --bind 0.0.0.0 --advertise-ip 192.168.1.50 --write-cnsl "C:\path\to\tko\cnsl.xml"
```

### What the main options do

- `--bind`  
  Bind address for the TCP, HTTP, and policy servers

- `--advertise-ip`  
  The IP address written into `cnsl.xml` so the Flash client knows which host to connect to

- `--write-cnsl`  
  Writes a `cnsl.xml` file for the client using the advertised IP

### Other useful options

```text
--tcp-port      SmartFox TCP port (default: 9339)
--http-port     BlueBox HTTP port (default: 8080)
--policy-port   Flash socket policy port (default: 843)
--static-dir    Optional directory to serve over HTTP
--static-port   Static file server port (default: 8000)
--send-all-http Send all queued BlueBox messages at once
```

### Expected startup output

You should see output similar to:

```text
Starting TKO server
Bind host: 0.0.0.0
Advertise IP: 192.168.1.50
TCP (SmartFox) port: 9339
HTTP (BlueBox) port: 8080
Policy port: 843
Use this cnsl.xml entry: <server name="local">192.168.1.50</server>
```

---

## Client Setup

Point the game client at the generated `cnsl.xml` entry for your local server, for example:

```xml
<server name="local">192.168.1.50</server>
```

If you are hosting the XML yourself, make sure the client loads the updated file and that the advertised IP matches the machine running `tko_server.py`.

---

## Development Notes

This project is being debugged by combining:

- packet logging from the emulator
- SWF decompilation / XML exports
- live testing in Ruffle and projector environments
- comparison against surviving gameplay footage

The current work is focused almost entirely on matching the original in-round multiplayer packet flow closely enough for a full fight to play correctly.

---

## Near-Term Goal

The immediate goal is now **full multiplayer round completion**:

- correct live movement
- correct timer behavior
- correct attacks / specials
- correct round resolution
- rematch / next-round handling

Once that works, the remaining cleanup should be much smaller than the protocol work already completed.

---

## Future Plans

Once the server implementation is stable, the long-term goal is to host a public instance so the game can be played without local setup.

Planned location:

```text
https://www.hoody.cx/playtko
```

---

## Tools Used

- Python 3
- Ruffle
- Flash Player Projector
- JPEXS Flash Decompiler
- SmartFoxServer 1.x documentation
- packet capture / protocol logging

---

## Disclaimer

This project is intended for **preservation and educational purposes only**.

All original game assets and intellectual property belong to **Cartoon Network / Turner Broadcasting**.
