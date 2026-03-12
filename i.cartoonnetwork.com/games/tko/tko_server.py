#!/usr/bin/env python3
"""
tko_server.py

Combined SmartFox TCP + BlueBox HTTP emulator for Titanic KungFu Offensive (TKO).

Usage example:
python tko_server.py --bind 0.0.0.0 --advertise-ip 192.168.1.50 --write-cnsl "C:\\path\\to\\tko\\cnsl.xml"
"""

import argparse
import html
import itertools
import json
import os
import random
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from socketserver import StreamRequestHandler, ThreadingMixIn, ThreadingTCPServer
from urllib.parse import parse_qs, unquote_plus

# ---------- Config ----------
DEFAULT_BIND = "0.0.0.0"
DEFAULT_TCP_PORT = 9339
DEFAULT_HTTP_PORT = 8080
DEFAULT_STATIC_PORT = 8000
DEFAULT_POLICY_PORT = 843

LOBBY_ROOM_ID = 2
WAITING_MATCH_ID = None
LOBBY_NAME = "Lobby"
ROOM_MAX_USERS = 2

WRAPPER_START_DELAY_SECS = 0.20
ROUND_LDED_DELAY_SECS = 0.15
ROUND_RNDS_DELAY_SECS = 0.20
ROUND_SCNT_DELAY_SECS = 0.45
ROUND_RNDO_DELAY_SECS = 0.15

HTTP_SEND_ALL_QUEUED = False

# Commands handled specially by server logic.
CNGAME_SERVER_HANDLED = {
    "rgq", "pi", "typ", "rdy", "fr", "strt", "rmch", "cu", "ka"
}

# Commands that are frequent enough that logging every relay would be noisy.
CNGAME_QUIET_RELAY = {
    "cu", "fx", "su", "cmbo", "shk", "sups", "adpj", "rmpj", "rmfx"
}

# ---------- Global state ----------
STATE_LOCK = threading.RLock()
MATCH_ID_COUNTER = itertools.count(1001)

ROOMS = {
    LOBBY_ROOM_ID: {
        "id": LOBBY_ROOM_ID,
        "name": LOBBY_NAME,
        "maxu": ROOM_MAX_USERS,
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
TCP_CLIENTS = set()
MATCHES = {}


# ---------- Helpers ----------

def debug_print(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()


def make_session_id():
    return str(random.randint(100000, 999999))


def make_uid():
    return random.randint(1000, 9999)


def make_rndk():
    return str(random.randint(100000, 999999))


def auto_detect_advertise_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and ip != "0.0.0.0":
            return ip
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass

    return "127.0.0.1"


def sanitize_nick(nick, uid):
    nick = (nick or "").strip()
    if not nick:
        nick = f"PLAYER{uid}"
    return nick.upper()


def current_logged_in_count():
    with STATE_LOCK:
        count = 0
        for client in list(TCP_CLIENTS):
            if getattr(client, "uid", None) is not None and not getattr(client, "closed", False):
                count += 1
        return max(1, count)


def parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return default


def parse_progress(value):
    s = str(value).strip().lower()
    if s == "true":
        return 100
    if s == "false":
        return 0
    return max(0, min(100, parse_int(s, 0)))


def socket_policy_xml(ports="*"):
    return (
        '<?xml version="1.0"?>'
        '<cross-domain-policy>'
        f'<allow-access-from domain="*" to-ports="{ports}" secure="false" />'
        '</cross-domain-policy>'
    )


def is_policy_request(text):
    return "policy-file-request" in text


def apiOK_msg():
    # Use explicit open/close body for maximum SFS 1.x compatibility.
    return "<msg t='sys'><body action='apiOK' r='0'></body></msg>"


def rndK_msg():
    return (
        "<msg t='sys'><body action='rndK' r='0'>"
        f"<k><![CDATA[{make_rndk()}]]></k>"
        "</body></msg>"
    )


def logOK_msg(uid, nick):
    return (
        "<msg t='sys'><body action='logOK' r='0'>"
        f"<login id='{uid}' mod='0' n='{html.escape(nick)}' />"
        "</body></msg>"
    )


def rmList_msg():
    with STATE_LOCK:
        rooms_xml = ""
        for r in ROOMS.values():
            ucnt = current_logged_in_count()
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
                f"ucnt='{ucnt}' "
                f"scnt='{r['scnt']}'"
                "></rm>"
            )
    return (
        "<msg t='sys'><body action='rmList' r='0'><rmList>"
        f"{rooms_xml}"
        "</rmList></body></msg>"
    )


def ucount_msg(room_id=LOBBY_ROOM_ID, count=None):
    if count is None:
        count = current_logged_in_count()
    return (
        "<msg t='sys'><body action='uCount' r='0'>"
        f"<room id='{room_id}' ucnt='{count}' />"
        "</body></msg>"
    )


def joinOK_msg(uid, room_id=LOBBY_ROOM_ID, nick="PLAYER", pid=-1):
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
    return "<msg t='sys'><body action='roundTripRes' r='0'></body></msg>"


def xt_server_msg(cmd, *params):
    parts = ["xt", str(cmd)]
    parts.extend(str(p) for p in params)
    return "%" + "%".join(parts) + "%"


def xt_cngame_msg(room_id, cmd, *params):
    """Preserve the full cnGame XT envelope for peer-relayed traffic.

    Client->server gameplay packets arrive as:
        %xt%cnGame%CMD%ROOM%P1%P2%...%

    The short server form (e.g. %xt%opp%0%26%) is correct for synthetic
    control packets generated by the emulator, but NOT for raw peer gameplay
    relay.  fr/cu/strt and any unknown cnGame packets must keep the cnGame
    extension token and room id intact, or the recipient never routes them into
    the live gameplay handler.
    """
    parts = ["xt", "cnGame", str(cmd), str(room_id)]
    parts.extend(str(p) for p in params)
    return "%" + "%".join(parts) + "%"


def xt_wrapper_game_join(match_id, my_index):
    return xt_server_msg("_gjs", match_id, "match", my_index)


def xt_wrapper_opponent_join(match_id, opponent_index, opponent_name):
    return xt_server_msg("_oj", match_id, opponent_index, opponent_name)


def xt_wrapper_game_start():
    return xt_server_msg("_strt")


def xt_wrapper_opponent_quit():
    return xt_server_msg("_oq")


def xt_wrapper_opponent_lost():
    return xt_server_msg("_ol")


def game_cmd_dl(opponent_ping):
    return xt_server_msg("dl", 0, opponent_ping)


def game_cmd_opp(opponent_character_type):
    return xt_server_msg("opp", 0, opponent_character_type)


def game_cmd_rdy(ready_value=1):
    return xt_server_msg("rdy", 0, ready_value)


def game_cmd_fr(progress):
    return xt_server_msg("fr", 0, progress)


def game_cmd_lded():
    return xt_server_msg("lded")


def game_cmd_rnds(round_no):
    return xt_server_msg("rnds", 0, round_no)


def game_cmd_scnt(value):
    return xt_server_msg("scnt", 0, value)


def game_cmd_ct(seconds_left):
    # ct behaves like a plain short server command, not a 0-prefixed room/control packet.
    # With the extra 0, the HUD only shows 0/1 instead of a 99->0 countdown.
    return xt_server_msg("ct", max(0, parse_int(seconds_left, 0)))


def game_cmd_su(msg_id, p1_pad_bits, p2_pad_bits):
    # cu's second parameter is a pad/input bitmask. The first parameter is a client-local
    # frame counter, which should NOT be mirrored back into the authoritative server sync.
    # The closest-fit live format is a short server update keyed by a server sequence id
    # plus both players' current pad states.
    return xt_server_msg("su", msg_id, p1_pad_bits, p2_pad_bits)


def game_cmd_rndo():
    return xt_server_msg("rndo")


def game_cmd_rmch():
    return xt_server_msg("rmch", 0, 1)


def write_cnsl_xml(path, advertise_ip):
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<servers>\n"
        f"  <server name='local'>{advertise_ip}</server>\n"
        "</servers>\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return path


def parse_login(xml_text):
    nick = ""
    zone = "Game"
    password = ""

    m = re.search(r"<nick><!\[CDATA\[(.*?)\]\]></nick>", xml_text, re.S)
    if m:
        nick = m.group(1)

    m = re.search(r"<pword><!\[CDATA\[(.*?)\]\]></pword>", xml_text, re.S)
    if m:
        password = m.group(1)

    m = re.search(r"<login\s+z=['\"]([^'\"]+)['\"]", xml_text)
    if m:
        zone = m.group(1)

    return zone, nick, password


def parse_client_xt_frame(frame):
    if not frame.startswith("%"):
        return None
    parts = frame[1:].split("%")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) < 4 or parts[0] != "xt":
        return None
    return {
        "ext": parts[1],
        "cmd": parts[2],
        "room_id": parts[3],
        "params": parts[4:],
    }


# ---------- Match state ----------

@dataclass
class MatchPlayer:
    conn: object
    uid: int
    nick: str
    player_index: int
    match_id: int
    ping: int | None = None
    character_type: int | None = None
    ready: bool = False
    ready_value: int = 1
    fr_progress: int = 0
    loaded: bool = False
    rematch: bool = False
    cn_seen: bool = False
    wrapper_join_sent: bool = False
    wrapper_start_sent: bool = False
    last_cu_frame: int = 0
    last_cu_bits: int = 0
    created: float = field(default_factory=time.time)


@dataclass
class MatchState:
    match_id: int
    players: dict[int, MatchPlayer] = field(default_factory=dict)
    round_no: int = 1
    round_sequence_started: bool = False
    round_live: bool = False
    lded_sent: bool = False
    rnds_sent: bool = False
    scnt_sent: bool = False
    scnt_value: int | None = None
    rndo_sent: bool = False
    su_seq: int = 0
    round_time_left: int = 99
    clock_generation: int = 0
    load_fallback_timer: threading.Timer | None = None
    created: float = field(default_factory=time.time)

    def full(self):
        return len(self.players) == 2

    def get(self, idx):
        return self.players.get(idx)

    def other(self, idx):
        return self.players.get(2 if idx == 1 else 1)


def get_match_for_handler(handler):
    with STATE_LOCK:
        mid = getattr(handler, "match_id", None)
        if mid is None:
            return None
        return MATCHES.get(mid)


def ensure_player_joined_match(handler):
    global WAITING_MATCH_ID

    with STATE_LOCK:
        # If this handler is already in a live match, keep it there.
        existing_mid = getattr(handler, "match_id", None)
        if existing_mid is not None:
            existing = MATCHES.get(existing_mid)
            if existing and getattr(handler, "player_index", None) in existing.players:
                return existing, existing.full(), False

        waiting_match = None

        if WAITING_MATCH_ID is not None:
            m = MATCHES.get(WAITING_MATCH_ID)
            if m and len(m.players) == 1:
                only_player = next(iter(m.players.values()))
                if only_player and only_player.conn is not handler and not getattr(only_player.conn, "closed", False):
                    waiting_match = m
                else:
                    WAITING_MATCH_ID = None
            else:
                WAITING_MATCH_ID = None

        if waiting_match is None:
            match = MatchState(match_id=next(MATCH_ID_COUNTER))
            MATCHES[match.match_id] = match
            WAITING_MATCH_ID = match.match_id
            player_index = 1
            created = True
        else:
            match = waiting_match
            player_index = 2 if 1 in match.players else 1
            WAITING_MATCH_ID = None
            created = False

        player = MatchPlayer(
            conn=handler,
            uid=handler.uid,
            nick=handler.nick,
            player_index=player_index,
            match_id=match.match_id,
        )

        match.players[player_index] = player
        handler.match_id = match.match_id
        handler.player_index = player_index
        handler.match_player = player

        return match, match.full(), created


def flush_peer_state_to_player(match, player_index):
    me = match.get(player_index)
    peer = match.other(player_index)
    if not me or not peer:
        return
    if getattr(me.conn, "closed", False):
        return

    if peer.ping is not None:
        me.conn.send_tcp(game_cmd_dl(peer.ping))
    if peer.character_type is not None:
        me.conn.send_tcp(game_cmd_opp(peer.character_type))
    if peer.ready:
        me.conn.send_tcp(game_cmd_rdy(peer.ready_value))
    if match.lded_sent:
        me.conn.send_tcp(game_cmd_lded())
    if match.rnds_sent:
        me.conn.send_tcp(game_cmd_rnds(match.round_no))
    if match.scnt_value is not None:
        me.conn.send_tcp(game_cmd_scnt(match.scnt_value))
    if match.rndo_sent:
        me.conn.send_tcp(game_cmd_rndo())


def match_prefight_ready(match):
    if not match.full():
        return False

    for idx in (1, 2):
        player = match.get(idx)
        if not player or getattr(player.conn, "closed", False):
            return False
        if not player.cn_seen:
            return False
        if player.ping is None:
            return False
        if player.character_type is None:
            return False
        if not player.ready:
            return False

    return True


def match_load_ready(match):
    if not match.full():
        return False

    for idx in (1, 2):
        player = match.get(idx)
        if not player:
            return False
        if not (player.loaded or player.fr_progress >= 100):
            return False

    return True


def maybe_schedule_round_start(match, reason=""):
    with STATE_LOCK:
        if not match.full():
            return
        if match.round_sequence_started or match.round_live or match.rndo_sent:
            return
        if not match_prefight_ready(match):
            return
        if not match_load_ready(match):
            return

        match.round_sequence_started = True
        mid = match.match_id
        round_no = match.round_no

    debug_print(f"[GAME] Match {mid} scheduling round start (reason={reason or 'ready'})")

    def do_round_start_sequence():
        time.sleep(ROUND_LDED_DELAY_SECS)
        with STATE_LOCK:
            current = MATCHES.get(mid)
            if not current:
                return
            current.lded_sent = True
            for mp in (current.get(1), current.get(2)):
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(game_cmd_lded())

        time.sleep(ROUND_RNDS_DELAY_SECS)
        with STATE_LOCK:
            current = MATCHES.get(mid)
            if not current:
                return
            current.rnds_sent = True
            for mp in (current.get(1), current.get(2)):
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(game_cmd_rnds(round_no))

        for value in (3, 2, 1):
            time.sleep(ROUND_SCNT_DELAY_SECS)
            with STATE_LOCK:
                current = MATCHES.get(mid)
                if not current:
                    return
                current.scnt_sent = True
                current.scnt_value = value
                for mp in (current.get(1), current.get(2)):
                    if mp and not getattr(mp.conn, "closed", False):
                        mp.conn.send_tcp(game_cmd_scnt(value))

        time.sleep(ROUND_RNDO_DELAY_SECS)
        with STATE_LOCK:
            current = MATCHES.get(mid)
            if not current:
                return
            # IMPORTANT: rndo is NOT a round-start packet.
            # In the client, incoming rndo is handled as round-result / round-over
            # data and immediately drives DRAW / WIN / LOSE / TKO UI based on its params.
            # Sending bare %xt%rndo% here makes the clients interpret missing params as 0,
            # which produces an immediate DRAW as soon as the countdown completes.
            # After the last countdown tick, the round simply becomes live; do not emit rndo.
            current.round_live = True
            current.round_time_left = 99
            current.clock_generation += 1
            generation = current.clock_generation
            for mp in (current.get(1), current.get(2)):
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(game_cmd_ct(current.round_time_left), quiet=True)
            debug_print(f"[GAME] Match {mid} countdown complete; round is now live (no rndo sent, ct=99)")
        start_match_clock(mid, generation)

    threading.Thread(target=do_round_start_sequence, daemon=True).start()


def start_match_clock(match_id, generation):
    def run_clock():
        while True:
            time.sleep(1.0)
            with STATE_LOCK:
                current = MATCHES.get(match_id)
                if not current:
                    return
                if current.clock_generation != generation:
                    return
                if not current.round_live:
                    return
                if current.round_time_left <= 0:
                    return
                current.round_time_left -= 1
                packet = game_cmd_ct(current.round_time_left)
                for mp in (current.get(1), current.get(2)):
                    if mp and not getattr(mp.conn, "closed", False):
                        mp.conn.send_tcp(packet, quiet=True)
    threading.Thread(target=run_clock, daemon=True).start()


def emit_su(match):
    with STATE_LOCK:
        current = MATCHES.get(match.match_id)
        if not current:
            return
        current.su_seq += 1
        seq = current.su_seq
        p1 = current.get(1)
        p2 = current.get(2)
        p1_pad = p1.last_cu_bits if p1 else 0
        p2_pad = p2.last_cu_bits if p2 else 0
        packet = game_cmd_su(seq, p1_pad, p2_pad)
        recipients = [p1, p2]
    for mp in recipients:
        if mp and not getattr(mp.conn, "closed", False):
            mp.conn.send_tcp(packet, quiet=True)


def notify_match_ready(match):
    p1 = match.get(1)
    p2 = match.get(2)
    if not p1 or not p2:
        return

    p1.conn.send_tcp(xt_wrapper_opponent_join(match.match_id, p2.player_index, p2.nick))
    p2.conn.send_tcp(xt_wrapper_opponent_join(match.match_id, p1.player_index, p1.nick))

    def delayed_wrapper_start():
        time.sleep(WRAPPER_START_DELAY_SECS)
        with STATE_LOCK:
            current = MATCHES.get(match.match_id)
            if not current:
                return
            for idx in (1, 2):
                mp = current.get(idx)
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(xt_wrapper_game_start())

    threading.Thread(target=delayed_wrapper_start, daemon=True).start()


def remove_match_if_empty(match_id):
    with STATE_LOCK:
        match = MATCHES.get(match_id)
        if not match:
            return
        if not match.players:
            if match.load_fallback_timer is not None:
                try:
                    match.load_fallback_timer.cancel()
                except Exception:
                    pass
            MATCHES.pop(match_id, None)


def cleanup_handler_from_match(handler, explicit_quit=False):
    global WAITING_MATCH_ID

    with STATE_LOCK:
        mid = getattr(handler, "match_id", None)
        if mid is None:
            return

        match = MATCHES.get(mid)
        if not match:
            handler.match_id = None
            handler.player_index = None
            handler.match_player = None
            if WAITING_MATCH_ID == mid:
                WAITING_MATCH_ID = None
            return

        my_index = getattr(handler, "player_index", None)
        peer = match.other(my_index) if my_index in (1, 2) else None

        if my_index in match.players:
            match.players.pop(my_index, None)

        handler.match_id = None
        handler.player_index = None
        handler.match_player = None

        if peer and not getattr(peer.conn, "closed", False):
            if explicit_quit:
                peer.conn.send_tcp(xt_wrapper_opponent_quit())
            else:
                peer.conn.send_tcp(xt_wrapper_opponent_lost())

        if WAITING_MATCH_ID == mid:
            WAITING_MATCH_ID = None

        if not match.players:
            MATCHES.pop(mid, None)
            return

        # One player remains: make this the waiting match again.
        if len(match.players) == 1:
            remaining = next(iter(match.players.values()))
            if remaining and not getattr(remaining.conn, "closed", False):
                WAITING_MATCH_ID = mid
            else:
                MATCHES.pop(mid, None)


# ---------- Policy server ----------

class FlashPolicyHandler(StreamRequestHandler):

    def handle(self):
        try:
            self.request.settimeout(5)
            data = b""
            while b"\x00" not in data and len(data) < 4096:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                data += chunk

            text = data.decode(errors="replace").strip("\x00").strip()
            debug_print(f"[POLICY] Received: {text}")

            if is_policy_request(text):
                response = socket_policy_xml(
                    f"{DEFAULT_TCP_PORT},{DEFAULT_HTTP_PORT},{DEFAULT_STATIC_PORT},{DEFAULT_POLICY_PORT}"
                )
                self.request.sendall((response + "\x00").encode("utf-8"))
                debug_print("[POLICY] Sent socket policy")
        except Exception as exc:
            debug_print(f"[POLICY] Error: {exc}")


# ---------- TCP SmartFox server ----------

class SmartFoxTCPHandler(StreamRequestHandler):
    def setup(self):
        super().setup()
        self.addr = self.client_address
        self.conn = self.request
        self.conn.settimeout(300)
        self.send_lock = threading.Lock()
        self.uid = None
        self.nick = None
        self.zone = "Game"
        self.password = ""
        self.match_id = None
        self.player_index = None
        self.match_player = None
        self.closed = False
        self.login_done = False

        with STATE_LOCK:
            TCP_CLIENTS.add(self)

        debug_print(f"[TCP] Connection from {self.addr}")

    def finish(self):
        self.closed = True
        with STATE_LOCK:
            TCP_CLIENTS.discard(self)
        try:
            super().finish()
        except Exception:
            pass

    def complete_login(self, zone="Game", raw_nick="", password="find", send_rndk=False):
        if self.login_done:
            return

        self.login_done = True

        uid = self.uid or make_uid()
        nick = self.nick or sanitize_nick(raw_nick, uid)

        self.uid = uid
        self.nick = nick
        self.zone = zone or "Game"
        self.password = password or "find"

        debug_print(f"[TCP] login -> uid={uid} nick={nick!r} zone={self.zone!r} pword={self.password!r}")

        # Keep this off by default. Earlier evidence showed rndK before login can break the client.
        if send_rndk:
            self.send_tcp(rndK_msg())
            time.sleep(0.03)

        # Preserve the sequence that previously worked once the client had logged in naturally.
        self.send_tcp(logOK_msg(uid, nick))
        time.sleep(0.03)

        self.send_tcp(rmList_msg())
        time.sleep(0.03)

        self.send_tcp(joinOK_msg(uid, room_id=LOBBY_ROOM_ID, nick=nick, pid=-1))
        time.sleep(0.03)

        self.send_tcp(ucount_msg(room_id=LOBBY_ROOM_ID, count=current_logged_in_count()))
        time.sleep(0.03)

        self.send_tcp(
            json.dumps(
                {"t": "xt", "b": {"o": {"_cmd": "_logOK", "id": LOBBY_ROOM_ID, "name": nick}}},
                separators=(",", ":"),
            )
        )

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
        except Exception as exc:
            debug_print(f"[TCP] Error for {self.addr}: {exc}")
        finally:
            self.closed = True
            cleanup_handler_from_match(self, explicit_quit=False)
            if self.uid:
                debug_print(f"[TCP] Client {self.addr} (uid {self.uid}, nick {self.nick}) disconnected")
            else:
                debug_print(f"[TCP] Client {self.addr} disconnected")

    def process_frame(self, frame):
        if is_policy_request(frame):
            self.send_tcp(socket_policy_xml(
                f"{DEFAULT_TCP_PORT},{DEFAULT_HTTP_PORT},{DEFAULT_STATIC_PORT},{DEFAULT_POLICY_PORT}"
            ))
            return

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

            def send_delayed_rndk():
                time.sleep(0.20)
                if self.closed or self.login_done:
                    return
                debug_print("[TCP] Sending delayed rndK after apiOK")
                self.send_tcp(rndK_msg())

            threading.Thread(target=send_delayed_rndk, daemon=True).start()
            return

        if action == "login" or "<login" in xml:
            if self.login_done:
                debug_print("[TCP] Duplicate login ignored")
                return

            zone, raw_nick, password = parse_login(xml)
            self.complete_login(zone=zone, raw_nick=raw_nick, password=password, send_rndk=False)
            return

        if "getRmList" in xml:
            self.send_tcp(rmList_msg())
            return

        if action in ("autoJoin", "joinRoom", "join"):
            if not self.uid:
                self.uid = make_uid()
            if not self.nick:
                self.nick = sanitize_nick("", self.uid)
            self.send_tcp(joinOK_msg(self.uid, room_id=LOBBY_ROOM_ID, nick=self.nick, pid=-1))
            self.send_tcp(ucount_msg(room_id=LOBBY_ROOM_ID, count=current_logged_in_count()))
            return

        if action == "leaveRoom":
            debug_print("[TCP] leaveRoom received")
            return

        if "roundTripBench" in xml or "roundTrip" in xml:
            self.send_tcp(roundTripRes_msg())
            return

        debug_print("[TCP] No XML handler matched")

    def process_xt_str(self, frame):
        msg = parse_client_xt_frame(frame)
        if not msg:
            debug_print(f"[TCP] Bad XT STR frame: {frame}")
            return

        debug_print(f"[TCP] XT STR parsed: {msg}")
        ext = msg["ext"]
        cmd = msg["cmd"]
        params = msg["params"]

        if ext == "Lobby":
            self.handle_lobby_xt(cmd, params)
            return

        if ext == "cnGame":
            self.handle_cngame_xt(cmd, params)
            return

        if cmd == "rgf":
            old_mid = getattr(self, "match_id", None)
            match, full, created = ensure_player_joined_match(self)
            if not self.match_player:
                self.match_player = match.get(self.player_index)

            if created:
                debug_print(f"[MM] Created match {match.match_id} as P{self.player_index} ({self.nick})")
            elif old_mid == match.match_id and not full:
                debug_print(f"[MM] Reusing waiting match {match.match_id} as P{self.player_index} ({self.nick})")
            else:
                debug_print(f"[MM] Joined match {match.match_id} as P{self.player_index} ({self.nick})")

            self.send_tcp(xt_wrapper_game_join(match.match_id, self.player_index))

            if full:
                p1 = match.get(1)
                p2 = match.get(2)
                if p1 and p2:
                    debug_print(f"[MM] Match {match.match_id} paired: P1={p1.nick} vs P2={p2.nick}")
                notify_match_ready(match)
            else:
                debug_print(f"[MM] Match {match.match_id} waiting for opponent")
            return

        if cmd == "ka":
            return

        debug_print(f"[TCP] Unhandled XT STR command: ext={ext!r} cmd={cmd!r}")

    def handle_lobby_xt(self, cmd, params):
        if cmd == "rlj":
            self.send_tcp(xt_server_msg("_ljs"))
            return

        if cmd == "rlp":
            self.send_tcp(xt_server_msg("_slp"))
            return

        if cmd == "rgf":
            match, full, created = ensure_player_joined_match(self)
            if not self.match_player:
                self.match_player = match.get(self.player_index)

            debug_print(f"[MM] {'Created' if created else 'Joined'} match {match.match_id} as P{self.player_index} ({self.nick})")
            self.send_tcp(xt_wrapper_game_join(match.match_id, self.player_index))

            if full:
                p1 = match.get(1)
                p2 = match.get(2)
                if p1 and p2:
                    debug_print(f"[MM] Match {match.match_id} paired: P1={p1.nick} vs P2={p2.nick}")
                notify_match_ready(match)
            else:
                debug_print(f"[MM] Match {match.match_id} waiting for opponent")
            return

        if cmd == "rgq":
            cleanup_handler_from_match(self, explicit_quit=True)
            return

        if cmd == "ka":
            return

        debug_print(f"[TCP] Unhandled Lobby XT command: {cmd}")

    def broadcast_cngame_to_match(self, match, cmd, params, quiet=False):
        packet = xt_cngame_msg(match.match_id, cmd, *params)
        sent = False
        for idx in (1, 2):
            mp = match.get(idx)
            if not mp or getattr(mp.conn, "closed", False):
                continue
            try:
                mp.conn.send_tcp(packet, quiet=quiet)
                sent = True
            except Exception:
                pass
        return sent

    def handle_cngame_xt(self, cmd, params):
        match = get_match_for_handler(self)
        if not match:
            debug_print(f"[TCP] cnGame {cmd} ignored because client is not assigned to a match")
            return

        player = match.get(self.player_index)
        peer = match.other(self.player_index)
        if not player:
            debug_print(f"[TCP] cnGame {cmd} ignored because match state is missing player record")
            return

        if cmd == "rgq":
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} rgq")
            cleanup_handler_from_match(self, explicit_quit=True)
            return

        first_seen = not player.cn_seen
        player.cn_seen = True

        if first_seen:
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} entered cnGame")
            flush_peer_state_to_player(match, player.player_index)

        value = params[1] if len(params) > 1 else (params[0] if params else None)

        if cmd == "pi":
            player.ping = parse_int(value, 0)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} pi={player.ping}")
            if peer and peer.cn_seen and player.ping is not None:
                peer.conn.send_tcp(game_cmd_dl(player.ping))
            maybe_schedule_round_start(match, reason="pi")
            return

        if cmd == "typ":
            player.character_type = parse_int(value, 0)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} typ={player.character_type}")
            # Preserve original cnGame traffic for both clients. The game appears to
            # route live/network state through server-returned packets, not pure peer relay.
            self.broadcast_cngame_to_match(match, "typ", params, quiet=True)
            if peer and peer.cn_seen and not getattr(peer.conn, "closed", False):
                peer.conn.send_tcp(game_cmd_opp(player.character_type))
            maybe_schedule_round_start(match, reason="typ")
            return

        if cmd == "rdy":
            player.ready = True
            player.ready_value = parse_int(value, 1)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} rdy={player.ready_value}")
            self.broadcast_cngame_to_match(match, "rdy", params, quiet=True)
            if peer and peer.cn_seen and not getattr(peer.conn, "closed", False):
                peer.conn.send_tcp(game_cmd_rdy(player.ready_value), quiet=True)
            maybe_schedule_round_start(match, reason="rdy")
            return

        if cmd == "fr":
            seq = params[0] if len(params) > 0 else "0"
            raw_value = params[1] if len(params) > 1 else (params[0] if params else "0")
            progress = parse_progress(raw_value)

            if not match.round_sequence_started and not match.round_live:
                player.fr_progress = max(player.fr_progress, progress)
                debug_print(
                    f"[GAME] Match {match.match_id} P{player.player_index} "
                    f"fr={progress} (max={player.fr_progress})"
                )
                maybe_schedule_round_start(match, reason="fr")
            else:
                debug_print(
                    f"[GAME] Match {match.match_id} P{player.player_index} "
                    f"live fr seq={seq} value={raw_value}"
                )

            self.broadcast_cngame_to_match(match, "fr", params, quiet=True)
            return

        if cmd == "strt":
            player.loaded = True
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} strt=loaded")
            self.broadcast_cngame_to_match(match, "strt", params, quiet=True)
            maybe_schedule_round_start(match, reason="strt")
            return

        if cmd == "rmch":
            player.rematch = True
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} requested rematch")
            if peer and peer.cn_seen:
                peer.conn.send_tcp(game_cmd_rmch())
            if match.full() and all(p.rematch for p in match.players.values()):
                match.round_no += 1
                match.round_sequence_started = False
                match.round_live = False
                match.lded_sent = False
                match.rnds_sent = False
                match.scnt_sent = False
                match.scnt_value = None
                match.rndo_sent = False
                match.su_seq = 0
                match.round_time_left = 99
                match.clock_generation += 1
                if match.load_fallback_timer is not None:
                    try:
                        match.load_fallback_timer.cancel()
                    except Exception:
                        pass
                    match.load_fallback_timer = None
                for mp in match.players.values():
                    mp.ready = False
                    mp.ready_value = 1
                    mp.fr_progress = 0
                    mp.loaded = False
                    mp.rematch = False
            return

        if cmd == "cu":
            frame = parse_int(params[0], 0) if len(params) >= 1 else 0
            pad_bits = parse_int(params[1], 0) if len(params) >= 2 else 0
            player.last_cu_frame = frame
            player.last_cu_bits = pad_bits
            if frame % 60 == 0:
                debug_print(
                    f"[GAME] Match {match.match_id} P{player.player_index} "
                    f"cu frame={frame} pad={pad_bits}"
                )
            # IMPORTANT: cu is not a world-state packet. The second param is a pad/input bitmask
            # (the logs show directional/button bits such as 1/2/4/8 and 16384/32768 changing
            # with key presses), so blindly relaying cnGame cu does not drive the remote sim.
            # Feed both clients the server-returned sequenced pad stream instead.
            emit_su(match)
            return

        if cmd == "ka":
            return

        relayed = self.broadcast_cngame_to_match(
            match,
            cmd,
            params,
            quiet=(cmd in CNGAME_QUIET_RELAY),
        )
        if relayed:
            if cmd not in CNGAME_QUIET_RELAY:
                debug_print(f"[GAME] Relayed cnGame cmd={cmd} from P{player.player_index} params={params}")
            return

        debug_print(f"[TCP] Unhandled cnGame command: {cmd} params={params}")


    def process_json(self, text):
        debug_print(f"[TCP] Unhandled inbound JSON frame: {text}")

    def send_tcp(self, payload, quiet=False):
        if payload is None or self.closed:
            return
        try:
            encoded = (payload + "\x00").encode("utf-8")
            with self.send_lock:
                self.conn.sendall(encoded)
            if not quiet:
                debug_print(f"[TCP] Sent: {payload}")
        except Exception as exc:
            debug_print(f"[TCP] Error sending to {self.addr}: {exc}")


class ThreadedTCPServer(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- HTTP BlueBox server ----------

class BlueBoxHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "TKOBlueBox/1.0"

    def log_message(self, fmt, *args):
        debug_print("[HTTP] " + fmt % args)

    def _read_sfsHttp(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode(errors="replace")
        params = parse_qs(raw)
        sfs = params.get("sfsHttp", [""])[0]
        sfs = unquote_plus(sfs)
        return raw, sfs

    def _send_text(self, txt):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if isinstance(txt, str):
            txt = txt.encode("utf-8")
        self.wfile.write(txt)

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
            SESSIONS_HTTP[sid] = {"created": time.time(), "uid": None, "nick": None, "queue": []}
            return self._send_text("#" + sid)

        m = re.match(r"^(\d+)(.*)$", sfs, re.S)
        if not m:
            return self._send_text("")

        sid, payload = m.group(1), m.group(2).strip()
        if sid not in SESSIONS_HTTP:
            return self._send_text("ERR#01")

        sess = SESSIONS_HTTP[sid]

        if payload.startswith("poll"):
            q = sess.get("queue", [])
            if not q:
                return self._send_text("")
            if HTTP_SEND_ALL_QUEUED:
                out = "".join(sid + item for item in q)
                sess["queue"] = []
                return self._send_text(out)
            item = q.pop(0)
            return self._send_text(sid + item)

        act_m = re.search(r"action=['\"]?([^'\"> ]+)", payload)
        action = act_m.group(1) if act_m else None

        if "verChk" in payload:
            return self._send_text(sid + apiOK_msg())

        if action == "login" or "action='login'" in payload or "<login" in payload:
            zone, raw_nick, password = parse_login(payload)
            uid = make_uid()
            nick = sanitize_nick(raw_nick, uid)
            sess["uid"] = uid
            sess["nick"] = nick
            q = sess.setdefault("queue", [])
            q.append(logOK_msg(uid, nick))
            q.append(rmList_msg())
            q.append(joinOK_msg(uid, room_id=LOBBY_ROOM_ID, nick=nick, pid=-1))
            q.append(ucount_msg(room_id=LOBBY_ROOM_ID, count=current_logged_in_count()))
            q.append(
                json.dumps(
                    {"t": "xt", "b": {"o": {"_cmd": "_logOK", "id": LOBBY_ROOM_ID, "name": nick}}},
                    separators=(",", ":"),
                )
            )
            return self._send_text("")

        if "getRmList" in payload:
            return self._send_text(sid + rmList_msg())

        if payload.startswith("%"):
            parsed = parse_client_xt_frame(payload)
            if parsed:
                ext = parsed["ext"]
                cmd = parsed["cmd"]
                if ext == "Lobby" and cmd == "rlj":
                    return self._send_text(sid + xt_server_msg("_ljs"))
                if ext == "Lobby" and cmd == "rlp":
                    return self._send_text(sid + xt_server_msg("_slp"))
            return self._send_text("")

        if action in ("autoJoin", "joinRoom", "join") or "autoJoin" in payload or "joinRoom" in payload:
            uid = sess.get("uid") or make_uid()
            nick = sess.get("nick") or sanitize_nick("", uid)
            return self._send_text(sid + joinOK_msg(uid, room_id=LOBBY_ROOM_ID, nick=nick, pid=-1))

        if "roundTripBench" in payload or "roundTrip" in payload or "rndK" in payload:
            return self._send_text(sid + roundTripRes_msg())

        return self._send_text("")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- Optional static file server ----------

class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        debug_print("[STATIC] " + fmt % args)


class ThreadedStaticHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- Boot ----------

def run_servers(bind_host, tcp_port, http_port, static_dir=None, static_port=None, policy_port=DEFAULT_POLICY_PORT):
    policy_server = ThreadedTCPServer((bind_host, policy_port), FlashPolicyHandler)
    policy_thread = threading.Thread(target=policy_server.serve_forever, name="policy-server", daemon=True)
    policy_thread.start()
    debug_print(f"Flash policy server listening on {bind_host}:{policy_port}")

    tcp_server = ThreadedTCPServer((bind_host, tcp_port), SmartFoxTCPHandler)
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, name="tcp-server", daemon=True)
    tcp_thread.start()
    debug_print(f"SmartFox TCP emulator listening on {bind_host}:{tcp_port}")

    http_server = ThreadedHTTPServer((bind_host, http_port), BlueBoxHTTPRequestHandler)
    http_thread = threading.Thread(target=http_server.serve_forever, name="http-server", daemon=True)
    http_thread.start()
    debug_print(f"BlueBox HTTP emulator listening on {bind_host}:{http_port}")

    static_server = None
    if static_dir:
        handler = partial(QuietStaticHandler, directory=static_dir)
        static_server = ThreadedStaticHTTPServer((bind_host, static_port), handler)
        static_thread = threading.Thread(target=static_server.serve_forever, name="static-server", daemon=True)
        static_thread.start()
        debug_print(f"Static file server listening on {bind_host}:{static_port} serving {static_dir}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        debug_print("Shutting down servers...")
    finally:
        try:
            policy_server.shutdown()
            policy_server.server_close()
        except Exception:
            pass
        try:
            tcp_server.shutdown()
            tcp_server.server_close()
        except Exception:
            pass
        try:
            http_server.shutdown()
            http_server.server_close()
        except Exception:
            pass
        if static_server:
            try:
                static_server.shutdown()
                static_server.server_close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="TKO SmartFox/BlueBox emulator with multiplayer relay")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="Bind address for TCP/HTTP servers (default 0.0.0.0)")
    parser.add_argument("--advertise-ip", default=None, help="IP address to write into cnsl.xml; defaults to auto-detect")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="SmartFox TCP port (default 9339)")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT, help="BlueBox HTTP port (default 8080)")
    parser.add_argument("--policy-port", type=int, default=DEFAULT_POLICY_PORT, help="Flash socket policy port (default 843)")
    parser.add_argument("--send-all-http", action="store_true", help="HTTP poll: send all queued messages at once")
    parser.add_argument("--static-dir", default=None, help="Optional directory to serve as a simple HTTP file server")
    parser.add_argument("--static-port", type=int, default=DEFAULT_STATIC_PORT, help="Static file server port (default 8000)")
    parser.add_argument("--write-cnsl", default=None, help="Optional path to write cnsl.xml; if omitted and --static-dir is set, writes <static-dir>/cnsl.xml")
    args = parser.parse_args()

    global HTTP_SEND_ALL_QUEUED
    HTTP_SEND_ALL_QUEUED = bool(args.send_all_http)

    advertise_ip = args.advertise_ip or auto_detect_advertise_ip()

    cnsl_target = args.write_cnsl
    if cnsl_target is None and args.static_dir:
        cnsl_target = os.path.join(args.static_dir, "cnsl.xml")

    if cnsl_target:
        os.makedirs(os.path.dirname(os.path.abspath(cnsl_target)), exist_ok=True)
        write_cnsl_xml(cnsl_target, advertise_ip)
        debug_print(f"Wrote cnsl.xml to: {cnsl_target}")

    debug_print("Starting TKO server")
    debug_print("Bind host:", args.bind)
    debug_print("Advertise IP:", advertise_ip)
    debug_print("TCP (SmartFox) port:", args.tcp_port)
    debug_print("HTTP (BlueBox) port:", args.http_port)
    debug_print("Policy port:", args.policy_port)
    if args.static_dir:
        debug_print("Static file dir:", args.static_dir)
        debug_print("Static file port:", args.static_port)
    debug_print(f"Use this cnsl.xml entry: <server name=\"local\">{advertise_ip}</server>")

    run_servers(
        bind_host=args.bind,
        tcp_port=args.tcp_port,
        http_port=args.http_port,
        static_dir=args.static_dir,
        static_port=args.static_port,
        policy_port=args.policy_port,
    )


if __name__ == "__main__":
    main()