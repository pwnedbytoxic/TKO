#!/usr/bin/env python3
# bluebox_emulator_v4.py
# Minimal, robust SmartFox BlueBox emulator v4
# - run in the same folder where you serve GameContainer.swf (python -m http.server 8000)
# - responds to connect/verChk/poll/login/getRmList/autoJoin/joinRoom
# - by default sends 1 queued message per poll (change SEND_ALL_QUEUED)
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote_plus
import random, re, time, argparse, sys

HOST = "0.0.0.0"
PORT = 8080

# If True, send all queued messages on a single poll (older approach).
# If False (default), send exactly one queued message per poll (safe).
SEND_ALL_QUEUED = False

SESSIONS = {}  # sid -> {"created":..., "uid":..., "queue": [xml1, xml2,...] }

def make_session_id():
    return str(random.randint(100000, 999999))

def wrap_with_sid(sid, xml):
    return sid + xml

def verchk_reply(ver):
    return f"<msg t='sys'><body action='verChk' r='0'><ver v='{ver}'/></body></msg>"

def apiOK_msg():
    return "<msg t='sys'><body action='apiOK' r='0' /></msg>"

def logOK_msg(uid, nick="player"):
    return (
        "<msg t='sys'>"
        "<body action='logOK' r='0'>"
        f"<login id='{uid}' mod='0' n='{nick}' />"
        "</body>"
        "</msg>"
    )

def rmList_msg():
    # conservative structure including common attributes the client expects
    return (
        "<msg t='sys'>"
        "<body action='rmList' r='0'>"
        "<rmList>"
        # id, n (name), maxu (max users), temp, game, priv, limbo
        "<rm id='1' n='Lobby' maxu='10' temp='0' game='0' priv='0' limbo='0' />"
        "</rmList>"
        "</body>"
        "</msg>"
    )

def joinOK_msg(uid):
    return (
        "<msg t='sys'>"
        "<body action='joinOK' r='0'>"
        "<room id='1' n='Lobby'>"
        f"<u id='{uid}' n='player' mod='0' />"
        "</room>"
        "</body>"
        "</msg>"
    )

def debug_print(*a, **k):
    print(*a, **k)
    sys.stdout.flush()

class Handler(BaseHTTPRequestHandler):
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
        debug_print("\n--- POST /BlueBox/HttpBox.do ---")
        debug_print("Raw body (first 300):", raw[:300])
        debug_print("Decoded sfsHttp (first 400):", sfs[:400])

        # 1) handshake
        if sfs == "connect":
            sid = make_session_id()
            SESSIONS[sid] = {"created": time.time(), "uid": None, "queue": []}
            response = "#" + sid
            debug_print("-> handshake reply:", response)
            return self._send_text(response)

        # 2) session-prefixed payloads
        m = re.match(r"^(\d+)(.*)$", sfs, re.S)
        if not m:
            debug_print("Unknown sfsHttp format; replying empty")
            return self._send_text("")

        sid, payload = m.group(1), m.group(2).strip()
        debug_print("Session:", sid, "Payload snippet:", payload[:300])

        if sid not in SESSIONS:
            debug_print("Unknown session id:", sid, " -> reply ERR#01")
            return self._send_text("ERR#01")

        # convenient action extractor
        act_m = re.search(r"action=['\"]?([^'\"> ]+)['\"]?", payload)
        action = act_m.group(1) if act_m else None

        # 3) poll handling: send queued messages
        if payload.startswith("poll"):
            q = SESSIONS[sid].get("queue", [])
            if q:
                if SEND_ALL_QUEUED:
                    out = "".join([sid + msg for msg in q])
                    SESSIONS[sid]["queue"] = []
                    debug_print("-> delivering ALL queued messages (count {})".format(len(q)))
                    return self._send_text(out)
                else:
                    # send exactly one queued message for this poll
                    msg = q.pop(0)
                    SESSIONS[sid]["queue"] = q
                    out = sid + msg
                    debug_print("-> delivering ONE queued message; remaining:", len(q))
                    return self._send_text(out)
            else:
                debug_print("Poll -> no queued events, returning empty")
                return self._send_text("")

        # 4) verChk
        if "verChk" in payload:
            mv = re.search(r"<ver\s+v=['\"]?(\d+)['\"]?", payload)
            ver = mv.group(1) if mv else "158"
            reply = verchk_reply(ver)
            debug_print("-> replying verChk:", reply)
            return self._send_text(reply)

        # 5) login request: queue apiOK + logOK (server will deliver via subsequent polls)
        if action == "login" or "action='login'" in payload:
            uid = random.randint(1000, 9999)
            SESSIONS[sid]["uid"] = uid

            # Queue the correct basic initialization events
            enqueue = SESSIONS[sid].setdefault("queue", [])
            enqueue.append(apiOK_msg())
            enqueue.append(logOK_msg(uid))
            # do not enqueue rmList here; wait for client's getRmList (or we can enqueue if you prefer)
            debug_print("-> queued apiOK + logOK for sid", sid, "uid", uid)
            return self._send_text("")

        # 6) client's request for room list (getRmList): reply now (in same HTTP response)
        if action in ("getRmList", "getRmList".lower()) or "getRmList" in payload:
            debug_print("-> client requested getRmList; sending rmList now")
            return self._send_text(sid + rmList_msg())

        # 7) autoJoin or joinRoom requests - reply joinOK immediately
        # Accept common names: "autoJoin", "joinRoom", "join"
        if action in ("autoJoin", "joinRoom", "join"):
            uid = SESSIONS[sid].get("uid") or random.randint(1000,9999)
            debug_print("-> join request received; sending joinOK (uid {})".format(uid))
            return self._send_text(sid + joinOK_msg(uid))

        # 8) unknown payload - echo nothing but log it (we show payload)
        debug_print("No handler matched for payload; replying empty")
        return self._send_text("")

    def _send_text(self, txt):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if isinstance(txt, str):
            txt = txt.encode()
        self.wfile.write(txt)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BlueBox SmartFox emulator v4")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--send-all", action="store_true", help="Send all queued messages on poll instead of one.")
    args = parser.parse_args()
    SEND_ALL_QUEUED = bool(args.send_all)
    HOST = args.host
    PORT = args.port
    print("BlueBox emulator v4 starting on {}:{}".format(HOST, PORT))
    HTTPServer((HOST, PORT), Handler).serve_forever()