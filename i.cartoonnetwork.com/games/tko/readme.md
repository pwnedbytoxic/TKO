# Titanic KungFu Offensive (TKO) Rehost

This repository contains work-in-progress tooling for running **Titanic KungFu Offensive**, a discontinued Cartoon Network Flash game, locally.

The original game relied on **SmartFoxServer 1.x** for multiplayer functionality. Since the original servers are long gone, this project focuses on **recreating the server behavior** so the client can run again.

The goal is to make the game playable locally by emulating the original networking stack used by the Flash client.

---

# Current Status

This project is **still in progress**, but the core connection flow is working.

Current behavior:

- Flash client loads correctly
- SmartFox connection is established
- Version check and login succeed
- Room list is returned
- Client reaches **"Checking Capacity"**

At the moment the client stops at that stage, which means one or more expected SmartFox events are still missing.

Progress is steady and the project should be **fully functional by mid-March**.

---

# Future Plans

Once the server implementation is stable, the goal is to run a **public instance of the game server**.

The plan is to host the server on my domain:

```
https://hoody.cx
```

This would allow players to connect to a live instance of the game again rather than only running it locally.

For now, development is focused on getting the protocol implementation fully working.

---

# How the Game Originally Worked

TKO used **SmartFoxServer 1.x**, which supported two connection methods.

### TCP Socket

Primary multiplayer connection.

```
Port: 9339
Protocol: SmartFox XML messages
Delimiter: NULL byte (\x00)
```

### BlueBox HTTP Tunnel

Fallback method when direct sockets were blocked.

```
Endpoint: /BlueBox/HttpBox.do
Port: 8080
Transport: HTTP POST
Parameter: sfsHttp
```

Both communication paths are implemented in the emulator.

---

# Repository Contents

### `sfs_bluebox_combined_v5.py`

Combined SmartFox server emulator.

Features:

- SmartFox TCP socket server (port **9339**)
- BlueBox HTTP tunneling server (port **8080**)
- Basic SmartFox protocol implementation
- Room and login simulation

Currently implemented SmartFox events:

```
apiOK
logOK
rmList
joinOK
uCount
roundTripRes
```

Handled client actions:

```
verChk
login
getRmList
joinRoom
autoJoin
roundTripBench
```

---

# Running the Server

Install Python 3 and run:

```bash
python sfs_bluebox_combined_v5.py
```

You should see:

```
Starting combined SFS/BlueBox emulator v5
TCP (SmartFox) port: 9339
HTTP (BlueBox) port: 8080
```

The Flash client can then connect to:

```
127.0.0.1:9339
```

---

# Development Notes

This project uses:

- **JPEXS Flash Decompiler** to inspect the client
- Decompiled SWF XML exports to understand SmartFox handlers
- Network logs to reconstruct the expected server responses

Important client handler table discovered in the SWF:

```
apiOK
logOK
rmList
joinOK
uCount
rndK
roundTripRes
userEnterRoom
roomVarsUpdate
```

This helps identify which events the client expects during initialization.

---

# Known Missing Behavior

The client currently stalls at:

```
Checking Capacity
```

This likely means one of the following events is missing or malformed:

```
rndK
roundTripRes
roomVarsUpdate
userEnterRoom
```

Work is ongoing to identify the exact packet sequence required by the client.

---

# Tools Used

- Flash Player Projector
- Ruffle (for testing)
- JPEXS Flash Decompiler
- Python 3
- SmartFoxServer 1.x documentation

---

# Goal

The immediate goal is to reach the **game lobby**.

Longer term goals:

- Character select
- Matchmaking
- Local multiplayer
- Full game emulation

---

# Disclaimer

This project is intended for **preservation and educational purposes only**.

All original game assets and intellectual property belong to **Cartoon Network / Turner Broadcasting**.