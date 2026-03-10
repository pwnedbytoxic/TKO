#!/usr/bin/env python3
import argparse
import html
import json
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn, ThreadingTCPServer, StreamRequestHandler
from urllib.parse import parse_qs, unquote_plus

DEFAULT_TCP_PORT = 9339
DEFAULT_HTTP_PORT = 8080
HTTP_SEND_ALL_QUEUED = False

ROUNDS_LOCK = threading.Lock()
ROOMS = {
    2: {
        "id": 2,
        "n": "Lobby",
        "name": "Lobby",
        "maxu": 2,
        "maxs": 0,
        "temp": 0,
        "game": 0,
        "priv": 0,
        "lmb": 1,
        "ucnt": 1,
        "scnt": 0,
    }
}

SESSIONS_HTTP = {}
TCP_CLIENTS = {}


def make_session_id():
    return str(random.randint(100000, 999999))


def make_uid():
    return random.randint(1000, 9999)


def debug_print(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()


def apiOK_msg():
    return "<msg t='sys'><body action='apiOK' r='0' /></msg>"


def logOK_msg(uid, nick="player"):
    return (
        "<msg t='sys'><body action='logOK' r='0'>"
        f"<login id='{uid}' mod='0' n='{html.escape(nick)}' />"
        "</body></msg>"
    )


def ucount_msg(room_id=2, count=1):
    return (
        "<msg t='sys'><body action='uCount' r='0'>"
        f"<room id='{room_id}' ucnt='{count}' />"
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
                f"maxu='{r['maxu']}' "
                f"maxs='{r['maxs']}' "
                f"temp='{r['temp']}' "
                f"game='{r['game']}' "
                f"priv='{r['priv']}' "
                f"lmb='{r['lmb']}' "
                f"ucnt='{r['ucnt']}' "
                f"scnt='{r['scnt']}'"
                "></rm>"
            )
    return (
        "<msg t='sys'><body action='rmList' r='0'><rmList>"
        f"{rooms_xml}"
        "</rmList></body></msg>"
    )


def joinOK_msg(uid, room_id=2, nick="player", pid=-1):
    return (
        f"<msg t='sys'><body action='joinOK' r='{room_id}'>"
        f"<pid id='{pid}' />"
        "<uLs>"
        f"<u i='{uid}' n='{html.escape(nick)}' m='0' s='0' p='{pid}'><vars /></u>"
        "</uLs>"
        "<vars />"
        "</body></msg>"
    )


def roundTripRes_msg():
    return "<msg t='sys'><body action='roundTripRes' r='0'/></msg>"


def xt_str_frame(*parts):
    return "%" + "%".join(str(p) for p in parts) + "%"


def xt_json_frame(data_obj):
    return json.dumps({"t": "xt", "b": {"o": data_obj}}, separators=(",", ":"))


def parse_login(xml_text):
    nick = "player"
    zone = "Game"
    password = ""

    m = re.search(r"<nick><!\[CDATA\[(.*?)\]\]></nick>", xml_text, re.S)
    if m:
        nick = m.group(1) or "player"

    m = re.search(r"<pword><!\[CDATA\[(.*?)\]\]></pword>", xml_text, re.S)
    if m:
        password = m.group(1)

    m = re.search(r"<login\s+z=['\"]([^'\"]+)['\"]", xml_text)
    if m:
        zone = m.group(1)

    return zone, nick, password


def parse_xt_str_frame(frame):
    if not frame.startswith("%"):
        return None
    parts = frame[1:].split("%")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) < 4 or parts[0] != "xt":
        return None
    return {
        "t": parts[0],
        "ext": parts[1],
        "cmd": parts[2],
        "room_id": parts[3],
        "params": parts[4:],
    }


class SmartFoxTCPHandler(StreamRequestHandler):
    def setup(self):
        super().setup()
        self.addr = self.client_address
        self.conn = self.request
        self.conn.settimeout(60)
        self.uid = None
        self.nick = "player"
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
                while b"\x00" in buffer:
                    frame, buffer = buffer.split(b"\x00", 1)
                    text = frame.decode(errors="replace").strip()
                    if not text:
                        continue
                    debug_print(f"[TCP] Received: {text}")
                    self.process_frame(text)
        except socket.timeout:
            debug_print(f"[TCP] Timeout for {self.addr}")
        except Exception as e:
            debug_print(f"[TCP] Error for {self.addr}: {e}")
        finally:
            if self.uid:
                debug_print(f"[TCP] Client {self.addr} (uid {self.uid}) disconnected")
            else:
                debug_print(f"[TCP] Client {self.addr} disconnected")

    def process_frame(self, frame):
        if frame.startswith("<"):
            self.process_xml(frame)
            return
        if frame.startswith("%"):
            self.process_xt_str(frame)
            return
        if frame.startswith("{"):
            self.process_json(frame)
            return
        debug_print(f"[TCP] Unhandled non-XML frame: {frame}")

    def process_xml(self, xml):
        act_m = re.search(r"action=['\"]?([^'\"> ]+)", xml)
        action = act_m.group(1) if act_m else None

        if "verChk" in xml:
            self.send_tcp(apiOK_msg())
            return

        if action == "login" or "<login" in xml:
            zone, nick, password = parse_login(xml)
            uid = make_uid()
            self.uid = uid
            self.nick = nick or "player"
            TCP_CLIENTS[self] = {
                "uid": uid,
                "nick": self.nick,
                "addr": self.addr,
                "time": time.time(),
                "zone": zone,
                "password": password,
            }
            debug_print(f"[TCP] login -> uid={uid} nick={self.nick!r} zone={zone!r} pword={password!r}")

            self.send_tcp(logOK_msg(uid, self.nick))
            self.send_tcp(rmList_msg())
            self.send_tcp(joinOK_msg(uid, room_id=2, nick=self.nick, pid=-1))
            self.send_tcp(ucount_msg(room_id=2, count=1))
            self.send_tcp(xt_json_frame({"_cmd": "_logOK", "id": 2, "name": self.nick}))
            return

        if "getRmList" in xml:
            self.send_tcp(rmList_msg())
            return

        if action in ("autoJoin", "joinRoom", "join"):
            if not self.uid:
                self.uid = make_uid()
            self.send_tcp(joinOK_msg(self.uid, room_id=2, nick=self.nick, pid=-1))
            self.send_tcp(ucount_msg(room_id=2, count=1))
            return

        if "roundTripBench" in xml or "roundTrip" in xml:
            self.send_tcp(roundTripRes_msg())
            return

        debug_print("[TCP] No XML handler matched")

    def process_xt_str(self, frame):
        msg = parse_xt_str_frame(frame)
        if not msg:
            debug_print(f"[TCP] Bad XT STR frame: {frame}")
            return

        debug_print(f"[TCP] XT STR parsed: {msg}")
        cmd = msg["cmd"]

        if cmd == "rlj":
            self.send_tcp(xt_str_frame("xt", "_ljs"))
            return

        if cmd == "rlp":
            self.send_tcp(xt_str_frame("xt", "_slp"))
            return

        if cmd == "rgf":
            self.send_tcp(xt_str_frame("xt", "_gjs"))

            def delayed_start():
                time.sleep(0.2)
                try:
                    self.send_tcp(xt_str_frame("xt", "_strt"))
                except Exception:
                    pass

            threading.Thread(target=delayed_start, daemon=True).start()
            return

        if cmd == "ka":
            return

        if cmd == "rgq":
            return

        debug_print(f"[TCP] Unhandled XT STR command: {cmd}")

    def process_json(self, text):
        debug_print(f"[TCP] Unhandled inbound JSON frame: {text}")

    def send_tcp(self, payload):
        try:
            self.conn.sendall((payload + "\x00").encode("utf-8"))
            debug_print(f"[TCP] Sent: {payload}")
        except Exception as e:
            debug_print(f"[TCP] Error sending to {self.addr}: {e}")


class ThreadedTCPServer(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class BlueBoxHTTPRequestHandler(BaseHTTPRequestHandler):
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

        if sfs == "connect":
            sid = make_session_id()
            SESSIONS_HTTP[sid] = {"created": time.time(), "uid": None, "nick": "player", "queue": []}
            return self._send_text("#" + sid)

        m = re.match(r"^(\d+)(.*)$", sfs, re.S)
        if not m:
            return self._send_text("")

        sid, payload = m.group(1), m.group(2).strip()
        if sid not in SESSIONS_HTTP:
            return self._send_text("ERR#01")

        if payload.startswith("poll"):
            q = SESSIONS_HTTP[sid].get("queue", [])
            if not q:
                return self._send_text("")
            if HTTP_SEND_ALL_QUEUED:
                out = "".join([sid + msg for msg in q])
                SESSIONS_HTTP[sid]["queue"] = []
                return self._send_text(out)
            msg = q.pop(0)
            return self._send_text(sid + msg)

        act_m = re.search(r"action=['\"]?([^'\"> ]+)", payload)
        action = act_m.group(1) if act_m else None

        if "verChk" in payload:
            return self._send_text(sid + apiOK_msg())

        if action == "login" or "action='login'" in payload:
            zone, nick, password = parse_login(payload)
            uid = make_uid()
            sess = SESSIONS_HTTP[sid]
            sess["uid"] = uid
            sess["nick"] = nick
            q = sess.setdefault("queue", [])
            q.append(logOK_msg(uid, nick))
            q.append(rmList_msg())
            q.append(joinOK_msg(uid, room_id=2, nick=nick, pid=-1))
            q.append(ucount_msg(room_id=2, count=1))
            q.append(xt_json_frame({"_cmd": "_logOK", "id": 2, "name": nick}))
            return self._send_text("")

        if "getRmList" in payload:
            return self._send_text(sid + rmList_msg())

        if payload.startswith("%"):
            msg = parse_xt_str_frame(payload)
            if msg:
                cmd = msg["cmd"]
                if cmd == "rlj":
                    return self._send_text(sid + xt_str_frame("xt", "_ljs"))
                if cmd == "rlp":
                    return self._send_text(sid + xt_str_frame("xt", "_slp"))
                if cmd == "rgf":
                    sess = SESSIONS_HTTP[sid]
                    q = sess.setdefault("queue", [])
                    q.append(xt_str_frame("xt", "_gjs"))
                    q.append(xt_str_frame("xt", "_strt"))
                    return self._send_text("")
            return self._send_text("")

        if action in ("autoJoin", "joinRoom", "join") or "autoJoin" in payload or "joinRoom" in payload:
            uid = SESSIONS_HTTP[sid].get("uid") or make_uid()
            nick = SESSIONS_HTTP[sid].get("nick") or "player"
            return self._send_text(sid + joinOK_msg(uid, room_id=2, nick=nick, pid=-1))

        if "roundTripBench" in payload or "roundTrip" in payload or "rndK" in payload:
            return self._send_text(sid + roundTripRes_msg())

        return self._send_text("")

    def _send_text(self, txt):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if isinstance(txt, str):
            txt = txt.encode("utf-8")
        self.wfile.write(txt)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_servers(tcp_port, http_port):
    tcp_server = ThreadedTCPServer(("0.0.0.0", tcp_port), SmartFoxTCPHandler)
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, name="tcp-server", daemon=True)
    tcp_thread.start()
    debug_print(f"SmartFox TCP emulator listening on port {tcp_port}")

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


def main():
    parser = argparse.ArgumentParser(description="Combined SmartFox TCP + BlueBox HTTP emulator (TKO fix)")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--send-all-http", action="store_true")
    args = parser.parse_args()

    global HTTP_SEND_ALL_QUEUED
    HTTP_SEND_ALL_QUEUED = bool(args.send_all_http)

    debug_print("Starting combined SFS/BlueBox emulator (TKO fix)")
    debug_print("TCP (SmartFox) port:", args.tcp_port)
    debug_print("HTTP (BlueBox) port:", args.http_port)
    run_servers(args.tcp_port, args.http_port)


if __name__ == "__main__":
    main()