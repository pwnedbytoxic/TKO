#!/usr/bin/env python3
"""
tko_server.py

Combined SmartFox TCP + BlueBox HTTP emulator for Titanic KungFu Offensive (TKO).

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

LOBBY_ROOM_ID = 2
LOBBY_NAME = "Lobby"
ROOM_MAX_USERS = 2

WRAPPER_START_DELAY_SECS = 0.20
ROUND_START_DELAY_SECS = 0.35
LOAD_FALLBACK_DELAY_SECS = 3.0

HTTP_SEND_ALL_QUEUED = False

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

# HTTP sessions: sid -> {"created":..., "uid":..., "nick":..., "queue":[...]}
SESSIONS_HTTP = {}

# Active TCP handlers
TCP_CLIENTS = set()

# Active matches
MATCHES = {}

# ---------- Helpers ----------

def debug_print(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()


def make_session_id():
    return str(random.randint(100000, 999999))


def make_uid():
    return random.randint(1000, 9999)


def auto_detect_advertise_ip():
    # UDP connect does not require the target to reply; it is a common way to discover
    # the preferred outbound LAN address.
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

    # Fallback: hostname lookup.
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


def apiOK_msg():
    return "<msg t='sys'><body action='apiOK' r='0' /></msg>"


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
            # SysHandler.handleRoomList() expects lmb, not limbo.
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
    return "<msg t='sys'><body action='roundTripRes' r='0' /></msg>"


def xt_server_msg(cmd, *params):
    # TKO accepts short-form server XT strings where the client-side data array is:
    #   [cmd, param1, param2, ...]
    parts = ["xt", str(cmd)]
    parts.extend(str(p) for p in params)
    return "%" + "%".join(parts) + "%"


def xt_wrapper_game_join(match_id, my_index):
    # _gjs params used by GameContainer:
    #   [0]=_gjs, [1]=gameId, [2]=placeholder/current game token, [3]=myPlayerIndex
    return xt_server_msg("_gjs", match_id, "match", my_index)


def xt_wrapper_opponent_join(match_id, opponent_index, opponent_name):
    # _oj params used by GameContainer:
    #   [0]=_oj, [1]=unused token, [2]=opponentIndex, [3]=opponentName
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
    # Client outbound frames use full SmartFox XT format:
    #   %xt%<ext>%<cmd>%<roomId>%<param1>%<param2>%...%
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
    created: float = field(default_factory=time.time)


@dataclass
class MatchState:
    match_id: int
    players: dict[int, MatchPlayer] = field(default_factory=dict)
    round_no: int = 1
    lded_sent: bool = False
    rnds_sent: bool = False
    scnt_sent: bool = False
    rndo_sent: bool = False
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
    with STATE_LOCK:
        if getattr(handler, "match_id", None) in MATCHES:
            match = MATCHES[handler.match_id]
            full = match.full()
            created = False
            return match, full, created

        # Find the oldest waiting match.
        waiting = None
        for match in MATCHES.values():
            if len(match.players) == 1:
                waiting = match
                break

        if waiting is None:
            match = MatchState(match_id=next(MATCH_ID_COUNTER))
            player_index = 1
            created = True
            MATCHES[match.match_id] = match
        else:
            match = waiting
            player_index = 2
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
        full = match.full()
        return match, full, created


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

    if peer.fr_progress > 0:
        me.conn.send_tcp(game_cmd_fr(peer.fr_progress))

    if match.lded_sent:
        me.conn.send_tcp(game_cmd_lded())

    if match.rnds_sent:
        me.conn.send_tcp(game_cmd_rnds(match.round_no))

    if match.scnt_sent:
        me.conn.send_tcp(game_cmd_scnt(0))

    if match.rndo_sent:
        me.conn.send_tcp(game_cmd_rndo())


def maybe_schedule_round_start(match):
    with STATE_LOCK:
        if not match.full():
            return
        if match.rndo_sent:
            return

        p1 = match.get(1)
        p2 = match.get(2)
        if not p1 or not p2:
            return

        both_loaded = (
            (p1.loaded or p1.fr_progress >= 100) and
            (p2.loaded or p2.fr_progress >= 100)
        )

        if not both_loaded:
            return

        if match.lded_sent:
            return

        match.lded_sent = True
        mid = match.match_id
        round_no = match.round_no

    def do_round_start_sequence():
        time.sleep(0.15)
        with STATE_LOCK:
            current = MATCHES.get(mid)
            if not current:
                return
            players = [current.get(1), current.get(2)]
            for mp in players:
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(game_cmd_lded())

        time.sleep(0.20)
        with STATE_LOCK:
            current = MATCHES.get(mid)
            if not current:
                return
            current.rnds_sent = True
            players = [current.get(1), current.get(2)]
            for mp in players:
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(game_cmd_rnds(round_no))

        # Start countdown
        for value in (3, 2, 1, 0):
            time.sleep(0.45)
            with STATE_LOCK:
                current = MATCHES.get(mid)
                if not current:
                    return
                current.scnt_sent = True
                players = [current.get(1), current.get(2)]
                for mp in players:
                    if mp and not getattr(mp.conn, "closed", False):
                        mp.conn.send_tcp(game_cmd_scnt(value))

        time.sleep(0.15)
        with STATE_LOCK:
            current = MATCHES.get(mid)
            if not current:
                return
            current.rndo_sent = True
            players = [current.get(1), current.get(2)]
            for mp in players:
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(game_cmd_rndo())

    threading.Thread(target=do_round_start_sequence, daemon=True).start()


def notify_match_ready(match):
    p1 = match.get(1)
    p2 = match.get(2)
    if not p1 or not p2:
        return

    # Wrapper-level opponent info must be in place before _strt, because GameData.onGameReady()
    # reads names and player indices from GameContainer.
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
    with STATE_LOCK:
        mid = getattr(handler, "match_id", None)
        if mid is None:
            return
        match = MATCHES.get(mid)
        if not match:
            handler.match_id = None
            handler.player_index = None
            handler.match_player = None
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

        if not match.players:
            remove_match_if_empty(mid)


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
            zone, raw_nick, password = parse_login(xml)
            uid = make_uid()
            nick = sanitize_nick(raw_nick, uid)
            self.uid = uid
            self.nick = nick
            self.zone = zone
            self.password = password
            debug_print(f"[TCP] login -> uid={uid} nick={nick!r} zone={zone!r} pword={password!r}")

            self.send_tcp(logOK_msg(uid, nick))
            self.send_tcp(rmList_msg())
            self.send_tcp(joinOK_msg(uid, room_id=LOBBY_ROOM_ID, nick=nick, pid=-1))
            self.send_tcp(ucount_msg(room_id=LOBBY_ROOM_ID, count=current_logged_in_count()))
            self.send_tcp(json.dumps({"t": "xt", "b": {"o": {"_cmd": "_logOK", "id": LOBBY_ROOM_ID, "name": nick}}}, separators=(",", ":")))
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

        # Friendly quit / keepalive sometimes appear via other extensions; handle the useful ones generically.
        if cmd == "rgq":
            cleanup_handler_from_match(self, explicit_quit=True)
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

    def handle_cngame_xt(self, cmd, params):
        if cmd == "rgq":
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} rgq")
            cleanup_handler_from_match(self, explicit_quit=True)
            return
        match = get_match_for_handler(self)
        if not match:
            debug_print(f"[TCP] cnGame {cmd} ignored because client is not assigned to a match")
            return

        player = match.get(self.player_index)
        peer = match.other(self.player_index)
        if not player:
            debug_print(f"[TCP] cnGame {cmd} ignored because match state is missing player record")
            return

        first_seen = not player.cn_seen
        player.cn_seen = True

        if first_seen:
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} entered cnGame")
            flush_peer_state_to_player(match, player.player_index)

        # Client outbound game messages always include:
        #   params[0] = local msg counter
        #   params[1] = payload / value
        # We only need the value field here.
        value = params[1] if len(params) > 1 else (params[0] if params else None)

        if cmd == "pi":
            player.ping = parse_int(value, 0)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} pi={player.ping}")
            if peer and peer.cn_seen and player.ping is not None:
                peer.conn.send_tcp(game_cmd_dl(player.ping))
            return

        if cmd == "typ":
            player.character_type = parse_int(value, 0)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} typ={player.character_type}")
            if peer and peer.cn_seen and player.character_type is not None:
                peer.conn.send_tcp(game_cmd_opp(player.character_type))
            return

        if cmd == "rdy":
            player.ready = True
            player.ready_value = parse_int(value, 1)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} rdy={player.ready_value}")
            if peer and peer.cn_seen:
                peer.conn.send_tcp(game_cmd_rdy(player.ready_value))
            return

        if cmd == "fr":
            player.fr_progress = parse_progress(value)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} fr={player.fr_progress}")
            if peer and peer.cn_seen:
                peer.conn.send_tcp(game_cmd_fr(player.fr_progress))
            maybe_schedule_round_start(match)
            return

        if cmd == "strt":
            player.loaded = True
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} strt=loaded")
            maybe_schedule_round_start(match)
            return

        if cmd == "rmch":
            player.rematch = True
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} requested rematch")
            if peer and peer.cn_seen:
                peer.conn.send_tcp(game_cmd_rmch())
            if match.full() and all(p.rematch for p in match.players.values()):
                # Reset all flags.
                match.lded_sent = False
                match.rnds_sent = False
                match.scnt_sent = False
                match.rndo_sent = False
                if match.load_fallback_timer is not None:
                    try:
                        match.load_fallback_timer.cancel()
                    except Exception:
                        pass
                    match.load_fallback_timer = None
                for mp in match.players.values():
                    mp.ready = False
                    mp.fr_progress = 0
                    mp.loaded = False
                    mp.rematch = False
            return

        if cmd == "cu":
            # Live-authoritative fight simulation is not implemented here yet.
            # We log it so the next reverse-engineering step has the raw traffic.
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} cu params={params}")
            return

        if cmd == "ka":
            return

        debug_print(f"[TCP] Unhandled cnGame command: {cmd} params={params}")

    def process_json(self, text):
        debug_print(f"[TCP] Unhandled inbound JSON frame: {text}")

    def send_tcp(self, payload):
        if payload is None or self.closed:
            return
        try:
            encoded = (payload + "\x00").encode("utf-8")
            with self.send_lock:
                self.conn.sendall(encoded)
            debug_print(f"[TCP] Sent: {payload}")
        except Exception as exc:
            debug_print(f"[TCP] Error sending to {self.addr}: {exc}")


class ThreadedTCPServer(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- HTTP BlueBox server ----------
#
# This remains intentionally minimal. The direct TCP socket path is the path that
# the current TKO/Ruffle workflow is already using successfully. BlueBox is kept
# available as a fallback for verChk/login/roomlist.

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

        if action == "login" or "action='login'" in payload:
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
            q.append(json.dumps({"t": "xt", "b": {"o": {"_cmd": "_logOK", "id": LOBBY_ROOM_ID, "name": nick}}}, separators=(",", ":")))
            return self._send_text("")

        if "getRmList" in payload:
            return self._send_text(sid + rmList_msg())

        # Minimal XT fallback.
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

def run_servers(bind_host, tcp_port, http_port, static_dir=None, static_port=None):
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
    parser = argparse.ArgumentParser(description="TKO SmartFox/BlueBox emulator with real 2-player pre-round matchmaking")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="Bind address for TCP/HTTP servers (default 0.0.0.0)")
    parser.add_argument("--advertise-ip", default=None, help="IP address to write into cnsl.xml; defaults to auto-detect")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="SmartFox TCP port (default 9339)")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT, help="BlueBox HTTP port (default 8080)")
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
    )


if __name__ == "__main__":
    main()