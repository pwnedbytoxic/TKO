#!/usr/bin/env python3
"""
sfs_bluebox_combined_v5.py

Combined SmartFox (TCP) + BlueBox (HTTP) emulator for TKO / Titanic KungFu Offensive.

- TCP server (SmartFox protocol) listens on port 9339 by default.
  Accepts null-terminated XML frames from client, replies with null-terminated frames.

- HTTP server (BlueBox/HttpBox.do) listens on port 8080 by default.
  Implements connect handshake (#sid), verChk, poll, login, getRmList, autoJoin/joinRoom flows.

Shared state:
- ROOMS (one Lobby room)
- SESSIONS_HTTP: sid -> {"created","uid","queue":[]}
- SESSIONS_TCP: mapping by connection (for logging/debug)

Run: python sfs_bluebox_combined_v5.py
"""

import argparse
import html
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn, ThreadingTCPServer, StreamRequestHandler
from urllib.parse import parse_qs, unquote_plus

# ---------- Config ----------
DEFAULT_TCP_PORT = 9339
DEFAULT_HTTP_PORT = 8080

# On HTTP poll: if True, send all queued messages in one poll; if False, send one message per poll.
HTTP_SEND_ALL_QUEUED = False

# ---------- Shared state ----------
ROUNDS_LOCK = threading.Lock()
ROOMS = {
    1: {
        "id": 1,
        "n": "Lobby",
        "name": "Lobby",
        "maxu": 10,
        "maxs": 0,
        "temp": 0,
        "game": 0,
        "priv": 0,
        "limbo": 0,
        "ucnt": 1,  # user count
        "scnt": 0,  # spectator count
    }
}

# HTTP sessions: sid -> {"created":..., "uid":..., "queue": [xmlstr,...]}
SESSIONS_HTTP = {}

# TCP client tracking: a map of uid -> connection info (for debugging), or conn->uid
TCP_CLIENTS = {}

# helper: generate ids
def make_session_id():
    return str(random.randint(100000, 999999))


def make_uid():
    return random.randint(1000, 9999)


# ---------- XML message factories ----------
def apiOK_msg():
    return "<msg t='sys'><body action='apiOK' r='0' /></msg>"


def logOK_msg(uid, nick="player"):
    # logOK (client-side handlers often expect this)
    return (
        "<msg t='sys'><body action='logOK' r='0'>"
        f"<login id='{uid}' mod='0' n='{nick}' />"
        "</body></msg>"
    )


def rmList_msg():
    with ROUNDS_LOCK:
        rooms_xml = ""
        for r in ROOMS.values():
            rooms_xml += (
                "<rm "
                f"id='{r['id']}' "
                f"n='{html.escape(r['name'])}' "
                f"name='{html.escape(r['name'])}' "
                f"maxu='{r['maxu']}' "
                f"maxs='{r['maxs']}' "
                f"temp='{r['temp']}' "
                f"game='{r['game']}' "
                f"priv='{r['priv']}' "
                f"limbo='{r['limbo']}' "
                f"ucnt='{r['ucnt']}' "
                f"scnt='{r['scnt']}'"
                " />"
            )

    return (
        "<msg t='sys'>"
        "<body action='rmList' r='0'>"
        "<rmList>"
        f"{rooms_xml}"
        "</rmList>"
        "</body>"
        "</msg>"
    )


def joinOK_msg(uid, room_id=1, nick="player"):
    return (
        "<msg t='sys'><body action='joinOK' r='0'>"
        f"<room id='{room_id}' name='{html.escape(ROOMS[room_id]['name'])}'>"
        f"<u id='{uid}' n='{html.escape(nick)}' mod='0' />"
        "</room></body></msg>"
    )


def roundTripRes_msg():
    return "<msg t='sys'><body action='roundTripRes' r='0'/></msg>"


# helper: prefix sid for BlueBox HTTP responses
def wrap_http_sid(sid, xml):
    return sid + xml


# ---------- TCP SmartFox server ----------

def debug_print(*args, **kwargs):
    """Print and flush for real-time logs"""
    print(*args, **kwargs)
    sys.stdout.flush()


class SmartFoxTCPHandler(StreamRequestHandler):
    """
    Each client connection handled here.
    SmartFox Flash clients often send null-terminated XML messages.
    We'll read until null (\x00) and process messages.
    """

    def setup(self):
        super().setup()
        self.addr = self.client_address
        self.conn = self.request
        self.conn.settimeout(60)  # 60s idle timeout
        self.uid = None
        debug_print(f"[TCP] Connection from {self.addr}")

    def handle(self):
        buffer = b""
        try:
            while True:
                chunk = self.conn.recv(4096)
                if not chunk:
                    debug_print(f"[TCP] Connection closed by {self.addr}")
                    break
                buffer += chunk
                # messages are null-terminated - split
                while b"\x00" in buffer:
                    frame, buffer = buffer.split(b"\x00", 1)
                    text = frame.decode(errors="replace").strip()
                    if not text:
                        continue
                    debug_print(f"[TCP] Received: {text}")
                    self.process_message(text)
        except socket.timeout:
            debug_print(f"[TCP] Timeout for {self.addr}")
        except Exception as e:
            debug_print(f"[TCP] Error for {self.addr}: {e}")
        finally:
            # cleanup
            if self.uid:
                debug_print(f"[TCP] Client {self.addr} (uid {self.uid}) disconnected")
            else:
                debug_print(f"[TCP] Client {self.addr} disconnected")

    def process_message(self, xml):
        """Handle incoming SmartFox TCP XML messages (very lightweight parser)"""
        # action attr: action='xxx'
        act_m = re.search(r"action=['\"]?([^'\"> ]+)", xml)
        action = act_m.group(1) if act_m else None

        # version check
        if "verChk" in xml:
            # correct response is apiOK
            debug_print("[TCP] verChk -> sending apiOK")
            self.send_tcp(apiOK_msg())

            return

        # login
        if action == "login" or "<login" in xml:
            uid = make_uid()
            self.uid = uid
            TCP_CLIENTS[self] = {"uid": uid, "addr": self.addr, "time": time.time()}
            debug_print(f"[TCP] login -> assigned uid {uid} - sending logOK, rmList, uCount, joinOK")
            # send logOK
            self.send_tcp(logOK_msg(uid))
            self.send_tcp(rmList_msg())

            self.send_tcp(
            "<msg t='sys'><body action='uCount' r='0'><room id='1' ucount='1'/></body></msg>"
            )

            self.send_tcp(joinOK_msg(uid))
            return

        # getRmList (explicit request)
        if "getRmList" in xml or "getRmList".lower() in xml.lower():
            debug_print("[TCP] getRmList -> sending rmList")
            self.send_tcp(rmList_msg())
            return

        if action in ("autoJoin", "joinRoom", "join"):
            # ack join by sending joinOK
            if not self.uid:
                self.uid = make_uid()
            debug_print(f"[TCP] join request -> send joinOK for uid {self.uid}")
            self.send_tcp(joinOK_msg(self.uid))
            return

        # roundTripBench => respond with roundTripRes
        if "roundTripBench" in xml or "roundTrip" in xml:
            debug_print("[TCP] roundTripBench -> send roundTripRes")
            self.send_tcp(roundTripRes_msg())
            return

        debug_print("[TCP] No handler matched for incoming TCP XML")

    def send_tcp(self, xml):
        """Send xml followed by null byte as SmartFox expects"""
        try:
            payload = (xml + "\x00").encode()
            self.conn.sendall(payload)
            debug_print(f"[TCP] Sent: {xml}")
        except Exception as e:
            debug_print(f"[TCP] Error sending to {self.addr}: {e}")


class ThreadedTCPServer(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- HTTP BlueBox server ----------

class BlueBoxHTTPRequestHandler(BaseHTTPRequestHandler):
    """
    Implements /BlueBox/HttpBox.do POST handler.
    Accepts form encoded body containing sfsHttp parameter (urlencoded).
    """

    def _read_sfsHttp(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode(errors="replace")
        params = parse_qs(raw)
        sfs = params.get("sfsHttp", [""])[0]
        sfs = unquote_plus(sfs)
        return raw, sfs

    def do_POST(self):
        if self.path != "/BlueBox/HttpBox.do":
            self.send_error(404)
            return

        raw, sfs = self._read_sfsHttp()
        debug_print("\n[HTTP] POST /BlueBox/HttpBox.do")
        debug_print("[HTTP] Raw body (first 300):", raw[:300])
        debug_print("[HTTP] Decoded sfsHttp (first 400):", sfs[:400])

        # handshake
        if sfs == "connect":
            sid = make_session_id()
            SESSIONS_HTTP[sid] = {"created": time.time(), "uid": None, "queue": []}
            response = "#" + sid
            debug_print("[HTTP] connect -> handshake reply:", response)
            return self._send_text(response)

        # session-prefixed payloads
        m = re.match(r"^(\d+)(.*)$", sfs, re.S)
        if not m:
            debug_print("[HTTP] Unknown sfsHttp format; replying empty")
            return self._send_text("")

        sid, payload = m.group(1), m.group(2).strip()
        debug_print("[HTTP] Session:", sid, "Payload snippet:", payload[:300])

        if sid not in SESSIONS_HTTP:
            debug_print("[HTTP] Unknown session id:", sid, " -> reply ERR#01")
            return self._send_text("ERR#01")

        # poll (server delivers queued messages on poll)
        if payload.startswith("poll"):
            q = SESSIONS_HTTP[sid].get("queue", [])
            if q:
                if HTTP_SEND_ALL_QUEUED:
                    out = "".join([sid + msg for msg in q])
                    SESSIONS_HTTP[sid]["queue"] = []
                    debug_print(f"[HTTP] -> delivering ALL queued messages (count {len(q)})")
                    return self._send_text(out)
                else:
                    # send one queued message
                    msg = q.pop(0)
                    SESSIONS_HTTP[sid]["queue"] = q
                    out = sid + msg
                    debug_print(f"[HTTP] -> delivering ONE queued message; remaining: {len(q)}")
                    return self._send_text(out)
            else:
                debug_print("[HTTP] Poll -> no queued events, returning empty")
                return self._send_text("")

        # basic action autodetect
        act_m = re.search(r"action=['\"]?([^'\"> ]+)", payload)
        action = act_m.group(1) if act_m else None

        # verChk -> reply verChk? NO. SmartFox expects apiOK.
        if "verChk" in payload:
            # send apiOK in HTTP response
            reply = apiOK_msg()
            debug_print("[HTTP] verChk -> replying apiOK")
            return self._send_text(sid + reply)

        # login: queue apiOK + logOK; client will poll to pick them up
        if action == "login" or "action='login'" in payload:
            uid = make_uid()
            SESSIONS_HTTP[sid]["uid"] = uid
            enqueue = SESSIONS_HTTP[sid].setdefault("queue", [])
            enqueue.append(apiOK_msg())
            enqueue.append(logOK_msg(uid))
            # Do not enqueue rmList automatically; let client request it with getRmList
            debug_print(f"[HTTP] login -> queued apiOK + logOK for sid {sid} uid {uid}")
            return self._send_text("")

        # getRmList: client wants the room list right now
        if action == "getRmList" or "getRmList" in payload:
            debug_print("[HTTP] getRmList -> sending rmList now")
            return self._send_text(sid + rmList_msg())

        # autoJoin / joinRoom / join: respond joinOK immediately
        if action in ("autoJoin", "joinRoom", "join") or "autoJoin" in payload or "joinRoom" in payload:
            uid = SESSIONS_HTTP[sid].get("uid") or make_uid()
            debug_print(f"[HTTP] {action or 'join'} request -> sending joinOK (uid {uid})")
            return self._send_text(sid + joinOK_msg(uid))

        # roundTripBench -> reply roundTripRes
        if "roundTripBench" in payload or "roundTrip" in payload or "rndK" in payload:
            debug_print("[HTTP] roundTripBench -> replying roundTripRes")
            return self._send_text(sid + roundTripRes_msg())

        # fallback
        debug_print("[HTTP] No handler matched for payload; replying empty")
        return self._send_text("")

    def _send_text(self, txt):
        # txt is bytes or str
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if isinstance(txt, str):
            txt = txt.encode()
        self.wfile.write(txt)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- Boot both servers ----------

def run_servers(tcp_port, http_port):
    # Start TCP server (SmartFox)
    tcp_server = ThreadedTCPServer(("0.0.0.0", tcp_port), SmartFoxTCPHandler)
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, name="tcp-server", daemon=True)
    tcp_thread.start()
    debug_print(f"SmartFox TCP emulator listening on port {tcp_port}")

    # Start HTTP server (BlueBox)
    http_server = ThreadedHTTPServer(("0.0.0.0", http_port), BlueBoxHTTPRequestHandler)
    http_thread = threading.Thread(target=http_server.serve_forever, name="http-server", daemon=True)
    http_thread.start()
    debug_print(f"BlueBox HTTP emulator listening on port {http_port}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        debug_print("Shutting down servers...")
        tcp_server.shutdown()
        http_server.shutdown()
        tcp_server.server_close()
        http_server.server_close()


# ---------- CLI ----------
def main():
    parser = argparse.ArgumentParser(description="Combined SmartFox TCP + BlueBox HTTP emulator (v5)")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="SmartFox TCP port (default 9339)")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT, help="BlueBox HTTP port (default 8080)")
    parser.add_argument("--send-all-http", action="store_true", help="HTTP poll: send all queued messages at once")
    args = parser.parse_args()
    global HTTP_SEND_ALL_QUEUED
    HTTP_SEND_ALL_QUEUED = bool(args.send_all_http)

    debug_print("Starting combined SFS/BlueBox emulator v5")
    debug_print("TCP (SmartFox) port:", args.tcp_port)
    debug_print("HTTP (BlueBox) port:", args.http_port)
    run_servers(args.tcp_port, args.http_port)


if __name__ == "__main__":
    main()