#!/usr/bin/env python3
"""
tko_server.py  –  TKO SmartFox/BlueBox emulator

Architecture (confirmed from cnGame.as server extension)
=========================================================
This is a SERVER-AUTHORITATIVE physics simulation, NOT an input relay.

The real SmartFox cnGame extension:
  - Runs the full game physics loop (positions, gravity, attacks, health)
  - Receives cu (input bits) from clients
  - Calls runSimulation() + sendRoundSnapshot() on each cu
  - Sends back a full world-state su packet every frame

Real su packet format (18 fields, base50 encoded positions/health):
  ["su", nextSuId, roundTimer,
   camGoal, 0,
   p1.x, p1.y, p1.anim, p1.facing, p1.health, p1.superMeter,
   1,
   p2.x, p2.y, p2.anim, p2.facing, p2.health, p2.superMeter]

Over SFS1 wire:
  %xt%su%<roomId>%<suId>%<timer>%<cam>%0%<p1x>%<p1y>%<p1anim>%<p1face>%<p1hp>%<p1sup>%1%<p2x>%<p2y>%<p2anim>%<p2face>%<p2hp>%<p2sup>%

Round sequence (no server-side countdown):
  both loaded → lded → startRound() → rnds + first su → cu drives sim

Usage: python tko_server.py --bind 0.0.0.0 --advertise-ip <ip>
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from socketserver import StreamRequestHandler, ThreadingMixIn, ThreadingTCPServer
from urllib.parse import parse_qs, unquote_plus

# ---------- Config ----------
DEFAULT_BIND = "0.0.0.0"
DEFAULT_TCP_PORT = 9339
DEFAULT_HTTP_PORT = 80
DEFAULT_STATIC_PORT = 8000
DEFAULT_POLICY_PORT = 843

LOBBY_ROOM_ID = 2
WAITING_MATCH_ID = None
LOBBY_NAME = "Lobby"
ROOM_MAX_USERS = 2

WRAPPER_START_DELAY_SECS = 0.20
ROUND_LDED_DELAY_SECS = 0.15

HTTP_SEND_ALL_QUEUED = False

# Commands handled specially by server logic (not raw-relayed).
CNGAME_SERVER_HANDLED = {
    "rgq", "pi", "typ", "rdy", "fr", "strt", "rmch", "cu", "ka", "cl", "ct", "box"
}

# Commands noisy enough that we skip per-packet log lines.
CNGAME_QUIET_RELAY = {
    "cu", "fx", "su", "cmbo", "shk", "sups", "adpj", "rmpj", "rmfx", "thrwn"
}

# ---------- Physics constants (from cnGame.as) ----------
BASE50       = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
FRAME_MS     = 40          # simulation tick rate (25 fps)
SU_TICK_HZ   = 1000 // FRAME_MS  # 25 fps
ROUND_TIME   = 99          # seconds
ROUND_TIME_MS = 99000
START_X_1    = 600
START_X_2    = 1000
GROUND_Y     = 550
SCREEN_WIDTH = 800
LEVEL_WIDTH  = 1600
CAMERA_MARGIN = 120
MOVE_SPEED   = 18
JUMP_SPEED   = 45
GRAVITY      = 4
ATTACK_LOCK_MS  = 280
STRONG_LOCK_MS  = 420
THROW_LOCK_MS   = 520
ROUND_END_DELAY_MS = 800

BASE_ANIMATIONS = {
    "IDLE": 0, "WALK_FWD": 1, "WALK_BACK": 2,
    "JUMP_UP": 3, "JUMP_FRONT": 4, "JUMP_BACK": 5,
    "LIGHT_KICK": 6, "STRONG_KICK1": 7, "STRONG_KICK2": 8, "STRONG_KICK3": 9,
    "LIGHT_PUNCH": 10, "STRONG_PUNCH1": 11, "STRONG_PUNCH2": 12, "STRONG_PUNCH3": 13,
    "THROW": 14, "CROUCH": 15,
    "LOW_LIGHT_PUNCH": 16, "LOW_STRONG_PUNCH1": 17, "LOW_STRONG_PUNCH2": 18,
    "LOW_LIGHT_KICK": 19, "LOW_STRONG_KICK1": 20, "LOW_STRONG_KICK2": 21,
    "JUMP_LIGHT_KICK": 22, "JUMP_STRONG_KICK1": 23, "JUMP_STRONG_KICK2": 24,
    "JUMP_LIGHT_PUNCH": 25, "JUMP_STRONG_PUNCH1": 26, "JUMP_STRONG_PUNCH2": 27,
    "THROWN": 28, "LOW_BLOCK": 29, "BLOCK": 30, "DIZZY": 31,
    "HIT": 32, "JUMP_HIT": 33, "LOW_HIT": 34,
    "KNOCKDOWN": 35, "RECOVER": 36, "DEFEAT": 37, "VICTORY": 38,
    "FROZEN": 39, "THROWN_END": 40, "REACH": 41,
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
CHARACTER_DATA = {}


@dataclass
class CharacterAnimation:
    id: int
    name: str


@dataclass
class CharacterDefinition:
    char_id: int
    name: str
    special_inputs: dict = field(default_factory=dict)
    special_groups: dict = field(default_factory=dict)
    super_group_key: str | None = None
    effect_by_name: dict = field(default_factory=dict)
    projectile_by_name: dict = field(default_factory=dict)
    effects: list = field(default_factory=list)
    projectiles: list = field(default_factory=list)


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


def first_int_text(node, path, default=0):
    child = node.find(path)
    if child is None or child.text is None:
        return default
    return parse_int(child.text, default)


def first_text(node, path, default=""):
    child = node.find(path)
    if child is None or child.text is None:
        return default
    return child.text.strip()


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
    """Short-form server message: %xt%<cmd>%<params...>%
    In SFS1 XT parsing the first param is treated as roomId.
    This is correct for control messages where the client handler reads
    the value from params[0] (= tokens[4] in the split).
    """
    parts = ["xt", str(cmd)]
    parts.extend(str(p) for p in params)
    return "%" + "%".join(parts) + "%"


def xt_room_msg(room_id, cmd, *params):
    """Extension/server packet with the SmartFox room-id slot populated."""
    return xt_server_msg(cmd, room_id, *params)


def xt_cngame_msg(room_id, cmd, *params):
    """Full cnGame extension envelope for raw peer-relay traffic.
    %xt%cnGame%<cmd>%<room_id>%<params...>%
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


def game_cmd_dl(room_id, opponent_ping):
    return xt_room_msg(room_id, "dl", opponent_ping)


def game_cmd_echo(room_id):
    return xt_room_msg(room_id, "echo")


def game_cmd_opp(room_id, opponent_character_type):
    return xt_room_msg(room_id, "opp", opponent_character_type)


def game_cmd_rdy(room_id, map_id=0):
    """Send rdy with mapId to opponent (from cnGame.as rdy handler)."""
    return xt_room_msg(room_id, "rdy", map_id)


def game_cmd_fr(room_id, progress):
    return xt_room_msg(room_id, "fr", progress)


def game_cmd_lded(room_id):
    return xt_room_msg(room_id, "lded")


def game_cmd_rnds(room_id, round_no):
    return xt_room_msg(room_id, "rnds", round_no)


def game_cmd_rmch(room_id):
    return xt_room_msg(room_id, "rmch", 1)


# ---------------------------------------------------------------------------
# Physics helpers (ported from cnGame.as)
# ---------------------------------------------------------------------------

def encode_base50(value):
    """Encode integer to 2-char base50 string. Matches cnGame.as encodeBase50()."""
    negative = value < 0
    if negative:
        value = -value
    value = min(int(value), 2499)
    s = BASE50[value // 50] + BASE50[value % 50]
    return ("-" + s) if negative else s


def now_ms():
    return time.time() * 1000.0


def has_bit(bits, n):
    return bool(bits & (1 << n))


def is_neutral_anim(anim):
    return anim in (0, 1, 2, 3, 4, 5, 15)


# ---------------------------------------------------------------------------
# su world-state snapshot (from cnGame.as sendRoundSnapshot)
# ---------------------------------------------------------------------------
def game_cmd_su_snapshot(match_id, su_id, round_timer, f0, f1):
    """Build authoritative world-state su packet. f0/f1 are FighterState objects."""
    min_x = min(f0.x, f1.x) - CAMERA_MARGIN
    max_x = max(f0.x, f1.x) + CAMERA_MARGIN
    cam = int(((min_x + max_x) / 2) - (SCREEN_WIDTH / 2))
    if cam > min_x:
        cam = int(min_x)
    if cam + SCREEN_WIDTH < max_x:
        cam = int(max_x - SCREEN_WIDTH)
    cam = max(0, min(LEVEL_WIDTH - SCREEN_WIDTH, cam))
    return xt_room_msg(
        match_id, "su",
        su_id, round_timer,
        encode_base50(cam), 0,
        encode_base50(int(f0.x)), encode_base50(int(f0.y)),
        f0.anim, (1 if f0.facing else 0),
        encode_base50(f0.health), f0.super_meter,
        1,
        encode_base50(int(f1.x)), encode_base50(int(f1.y)),
        f1.anim, (1 if f1.facing else 0),
        encode_base50(f1.health), f1.super_meter,
    )


def game_cmd_rndo(room_id, p1_wins=0, p2_wins=0, time_up=0, winner=0, perfect="false", comeback="false"):
    """Round-over packet from cnGame.as resolveRoundEnd.
    winner: 1=P1, 2=P2, 0=draw.  time_up: 1 if timer expired."""
    return xt_room_msg(room_id, "rndo", p1_wins, p2_wins, time_up, winner, perfect, comeback)


def game_cmd_win(room_id, winner_zero_based):
    return xt_room_msg(room_id, "win", winner_zero_based)


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


def _parse_animation_section(root, section_name):
    section = root.find(section_name)
    if section is None:
        return []
    out = []
    for anim in section.findall("anim"):
        anim_id = first_int_text(anim, "id", -1)
        if anim_id < 0:
            continue
        out.append(CharacterAnimation(id=anim_id, name=first_text(anim, "name", "").upper()))
    return out


SPECIAL_SUFFIX_TOKENS = {
    "LIGHT", "STRONG", "START", "FLY", "END", "HIT", "MISS", "HOLD", "DROP",
    "ATTACK", "REACH", "SWING", "RECOVER", "THROW", "LIGHT1", "LIGHT2", "LIGHT3",
    "STRONG1", "STRONG2", "STRONG3",
}


def common_prefix_tokens(names):
    token_lists = [[tok for tok in name.split("_") if tok] for name in names if name]
    if not token_lists:
        return []
    prefix = token_lists[0][:]
    for tokens in token_lists[1:]:
        i = 0
        while i < min(len(prefix), len(tokens)) and prefix[i] == tokens[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    if prefix and prefix[0] == "ANIM":
        prefix = prefix[1:]
    return prefix


def normalize_special_group_name(name, prefix_tokens):
    tokens = [tok for tok in (name or "").upper().split("_") if tok]
    if prefix_tokens and tokens[:len(prefix_tokens)] == prefix_tokens:
        tokens = tokens[len(prefix_tokens):]
    if not tokens:
        return name.upper()
    while len(tokens) > 1 and tokens[-1] in SPECIAL_SUFFIX_TOKENS:
        tokens.pop()
    return "_".join(tokens)


def choose_group_entry(group, strong=False):
    names = [(anim.name or "").upper() for anim in group]
    if strong:
        for anim, name in zip(group, names):
            if "STRONG" in name:
                return anim
    else:
        for anim, name in zip(group, names):
            if "LIGHT" in name:
                return anim
    for anim, name in zip(group, names):
        if "START" in name or "REACH" in name:
            return anim
    return group[0] if group else None


def find_phase_animation(group, *tokens):
    required = tuple(tok.upper() for tok in tokens if tok)
    for anim in group:
        name = (anim.name or "").upper()
        if all(tok in name for tok in required):
            return anim
    return None


def build_special_input_map(special_list):
    if not special_list:
        return {}, {}, None
    prefix_tokens = common_prefix_tokens([anim.name for anim in special_list])
    groups = []
    current_key = None
    current_group = []
    for anim in special_list:
        key = normalize_special_group_name(anim.name, prefix_tokens)
        if key != current_key:
            if current_group:
                groups.append((current_key, current_group))
            current_key = key
            current_group = [anim]
        else:
            current_group.append(anim)
    if current_group:
        groups.append((current_key, current_group))

    grouped = {key: group for key, group in groups}
    input_map = {}
    super_group_key = None
    non_super_groups = []
    for key, group in groups:
        if "SUPER" in key:
            super_group_key = key
        else:
            non_super_groups.append((key, group))

    primary_group_keys = []
    if non_super_groups:
        group_key, group = non_super_groups[0]
        input_map["9"] = {"group_key": group_key, "anim": choose_group_entry(group, strong=False)}
        input_map["10"] = {"group_key": group_key, "anim": choose_group_entry(group, strong=True) or input_map["9"]["anim"]}
        primary_group_keys.append(group_key)
    if len(non_super_groups) >= 2:
        group_key, group = non_super_groups[1]
        input_map["11"] = {"group_key": group_key, "anim": choose_group_entry(group, strong=False)}
        input_map["12"] = {"group_key": group_key, "anim": choose_group_entry(group, strong=True) or input_map["11"]["anim"]}
        primary_group_keys.append(group_key)
    elif non_super_groups:
        group_key, group = non_super_groups[0]
        input_map["11"] = {"group_key": group_key, "anim": choose_group_entry(group, strong=False)}
        input_map["12"] = {"group_key": group_key, "anim": choose_group_entry(group, strong=True) or input_map["11"]["anim"]}

    if super_group_key is None:
        leftover_keys = [key for key, _group in groups if key not in primary_group_keys]
        if leftover_keys:
            super_group_key = leftover_keys[-1]

    return input_map, grouped, super_group_key


def load_character_data(base_dir):
    global CHARACTER_DATA
    xml_dir = os.path.join(base_dir, "4_0")
    data = {}
    if not os.path.isdir(xml_dir):
        CHARACTER_DATA = {}
        return 0

    for entry in os.listdir(xml_dir):
        if not re.fullmatch(r"\d+\.xml", entry):
            continue
        path = os.path.join(xml_dir, entry)
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue
        char_id = parse_int(root.attrib.get("charId"), -1)
        if char_id < 0:
            continue
        special_list = _parse_animation_section(root, "specialAnimations")
        effect_list = _parse_animation_section(root, "effectAnimations")
        projectile_list = _parse_animation_section(root, "projectileAnimations")
        input_map, grouped_specials, super_group_key = build_special_input_map(special_list)
        data[char_id] = CharacterDefinition(
            char_id=char_id,
            name=first_text(root, "name", f"Robot {char_id}"),
            special_inputs=input_map,
            special_groups=grouped_specials,
            super_group_key=super_group_key,
            effect_by_name={anim.name: anim for anim in effect_list if anim.name},
            projectile_by_name={anim.name: anim for anim in projectile_list if anim.name},
            effects=effect_list,
            projectiles=projectile_list,
        )
    CHARACTER_DATA = data
    return len(data)


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

# ---------------------------------------------------------------------------
# Game physics (server-authoritative simulation ported from cnGame.as)
# ---------------------------------------------------------------------------

@dataclass
class FighterState:
    """Per-fighter physics and game state (mirrors player object in cnGame.as)."""
    index: int                    # 0 = P1, 1 = P2  (0-based, matches JS)
    character_type: int | None = None
    x: float = 600.0
    y: float = float(GROUND_Y)
    vy: float = 0.0
    health: int = 1000
    super_meter: int = 0
    anim: int = 0                 # animation id (BASE_ANIMATIONS values)
    facing: bool = True           # True = facing right (P1 default)
    attack_until: float = 0.0    # ms timestamp
    hit_until: float = 0.0       # ms timestamp
    knocked_out: bool = False
    special_until: float = 0.0
    special_move: object = None
    special_hit_done: bool = False
    last_hit_was_super: bool = False
    combo_hits: int = 0
    last_key_bits: int = 0
    wins: int = 0


def reset_fighter_for_round(f, x, facing):
    """Re-initialise fighter for a new round (resetFighterForRound in cnGame.as)."""
    f.x = float(x)
    f.y = float(GROUND_Y)
    f.vy = 0.0
    f.facing = facing
    f.health = 1000
    f.super_meter = 0
    f.anim = BASE_ANIMATIONS["IDLE"]
    f.attack_until = 0.0
    f.hit_until = 0.0
    f.knocked_out = False
    f.special_until = 0.0
    f.special_move = None
    f.special_hit_done = False
    f.last_hit_was_super = False
    f.combo_hits = 0
    f.last_key_bits = 0


def update_facing(f0, f1):
    f0.facing = f0.x <= f1.x
    f1.facing = f1.x < f0.x


def clamp_players(f0, f1):
    if f0.x < 150:
        f0.x = 150.0
    if f1.x > 1450:
        f1.x = 1450.0
    if f0.x > f1.x - 120:
        center = (f0.x + f1.x) / 2.0
        f0.x = center - 60.0
        f1.x = center + 60.0


def apply_movement(fighter, opponent, now):
    bits = fighter.last_key_bits
    move_left  = has_bit(bits, 2)   # bit 2 = left  (4)
    move_right = has_bit(bits, 3)   # bit 3 = right (8)
    down       = has_bit(bits, 1)   # bit 1 = down  (2)
    up         = has_bit(bits, 0)   # bit 0 = up    (1)

    on_ground = fighter.y >= GROUND_Y

    if on_ground and up:
        fighter.vy = -float(JUMP_SPEED)
        if move_left or move_right:
            fighter.anim = BASE_ANIMATIONS["JUMP_FRONT" if fighter.facing else "JUMP_BACK"]
        else:
            fighter.anim = BASE_ANIMATIONS["JUMP_UP"]

    if on_ground:
        if move_left and not move_right:
            fighter.x -= MOVE_SPEED
            fighter.anim = BASE_ANIMATIONS["WALK_BACK" if fighter.facing else "WALK_FWD"]
        elif move_right and not move_left:
            fighter.x += MOVE_SPEED
            fighter.anim = BASE_ANIMATIONS["WALK_FWD" if fighter.facing else "WALK_BACK"]
        elif down:
            fighter.anim = BASE_ANIMATIONS["CROUCH"]
        else:
            fighter.anim = BASE_ANIMATIONS["IDLE"]
    else:
        if move_left and not move_right:
            fighter.x -= MOVE_SPEED * 0.6
            fighter.anim = BASE_ANIMATIONS["JUMP_BACK" if fighter.facing else "JUMP_FRONT"]
        elif move_right and not move_left:
            fighter.x += MOVE_SPEED * 0.6
            fighter.anim = BASE_ANIMATIONS["JUMP_FRONT" if fighter.facing else "JUMP_BACK"]
        else:
            fighter.anim = BASE_ANIMATIONS["JUMP_UP"]


def get_character_definition(character_type):
    return CHARACTER_DATA.get(parse_int(character_type, -1))


def get_character_special_attack(fighter, bits):
    char_data = get_character_definition(getattr(fighter, "character_type", None))
    spec_bit = None
    if has_bit(bits, 10):
        spec_bit = "10"
    elif has_bit(bits, 9):
        spec_bit = "9"
    elif has_bit(bits, 12):
        spec_bit = "12"
    elif has_bit(bits, 11):
        spec_bit = "11"

    if spec_bit is None or not char_data:
        return None

    spec_info = char_data.special_inputs.get(spec_bit)
    if not spec_info:
        return None
    spec = spec_info["anim"]
    group = char_data.special_groups.get(spec_info["group_key"], [spec])
    return infer_special_attack_from_name(char_data, spec, group, fighter.facing, spec_bit in ("10", "12"))


def get_character_super_attack(fighter):
    char_data = get_character_definition(getattr(fighter, "character_type", None))
    if not char_data or not char_data.super_group_key:
        return None
    group = char_data.special_groups.get(char_data.super_group_key, [])
    anim = choose_group_entry(group, strong=True)
    if not anim:
        return None
    attack = infer_special_attack_from_name(char_data, anim, group, fighter.facing, True)
    attack["super"] = True
    attack["damage"] = max(attack.get("damage", 0), 180)
    attack["shake"] = max(attack.get("shake", 0), 10)
    return attack


def infer_special_attack_from_name(char_data, spec, group, facing_right, strong):
    name = (spec.name or "").upper()
    forward = 1 if facing_right else -1
    attack = {
        "anim": spec.id,
        "damage": 105 if strong else 75,
        "range": 210 if strong else 175,
        "lock": 680 if strong else 540,
        "shake": 9 if strong else 6,
        "special_name": name,
    }
    attack["fly_anim"] = getattr(find_phase_animation(group, "FLY"), "id", None)
    attack["hit_anim"] = getattr(find_phase_animation(group, "HIT"), "id", None)
    attack["miss_anim"] = getattr(find_phase_animation(group, "MISS"), "id", None)
    attack["end_anim"] = getattr(find_phase_animation(group, "END"), "id", None)
    attack["hold_anim"] = getattr(find_phase_animation(group, "HOLD"), "id", None)
    attack["attack_anim"] = getattr(find_phase_animation(group, "ATTACK"), "id", None)

    if any(token in name for token in ("FIREBALL", "GRENADE", "SPIT", "DISK", "LASER", "SWORD", "BLASTER", "TIMEBALL", "ELEC", "GUNS", "BUBBY", "FOODTHROW")):
        attack["range"] = 320 if strong else 260
        attack["lock"] = 720 if strong else 600
        attack["shake"] = 10 if strong else 7
        attack["projectile_id"] = choose_visual_animation_id(char_data.projectile_by_name, char_data.projectiles, name)
    elif any(token in name for token in ("WHIP", "VINESPIKE", "WAVES", "ROCKS", "CAKE", "DEBRIS", "HANDS", "CLOUD", "SPIKE")):
        attack["range"] = 250 if strong else 205
        attack["lock"] = 650 if strong else 540
        attack["shake"] = 8 if strong else 6
        attack["effect_id"] = choose_visual_animation_id(char_data.effect_by_name, char_data.effects, name)

    if any(token in name for token in ("DASH", "RUSH", "HEADBUTT", "SLIDE", "PHASE", "TIMEWALK")):
        attack["dash"] = (24 if strong else 18) * forward

    if any(token in name for token in ("FLIPKICK", "FALLKICK", "STOMP", "SPLASH", "SWINGKICK", "MONKEYSWING", "VERT_KICK", "SPINKICK", "JAKEDROP")):
        attack["dash"] = (16 if strong else 12) * forward
        attack["jump"] = -18 if strong else -12

    if "UPPERCUT" in name:
        attack["damage"] = 115 if strong else 85
        attack["range"] = 185 if strong else 150
        attack["jump"] = -14 if strong else -8

    if "GRAB" in name or "THROW" in name:
        attack["damage"] = 120 if strong else 90
        attack["range"] = 165 if strong else 145
        attack["thrown"] = True

    if attack.get("projectile_id") is None and char_data.projectiles:
        attack["projectile_id"] = choose_visual_animation_id(char_data.projectile_by_name, char_data.projectiles, name)
    if attack.get("effect_id") is None and char_data.effects:
        attack["effect_id"] = choose_visual_animation_id(char_data.effect_by_name, char_data.effects, name)
    return attack


def choose_visual_animation_id(by_name, fallback_list, special_name):
    if not fallback_list:
        return None
    upper = (special_name or "").upper()
    tokens = [token for token in re.split(r"[^A-Z0-9]+", upper) if token]
    for token in tokens:
        for anim_name, anim in by_name.items():
            if token and token in anim_name:
                return anim.id
    return fallback_list[0].id


def apply_special_movement(match, fighter, opponent, attack, now):
    extra = []
    remaining = max(0.0, fighter.special_until - now)
    elapsed = max(0.0, attack["lock"] - remaining)

    fighter.anim = attack["anim"]
    if fighter.special_hit_done:
        fighter.anim = attack.get("hit_anim") or attack.get("attack_anim") or attack["anim"]
    elif attack.get("attack_anim") is not None and elapsed >= max(120.0, attack["lock"] * 0.45):
        fighter.anim = attack["attack_anim"]

    if attack.get("dash") is not None:
        fighter.x += attack["dash"]

    if not fighter.special_hit_done and abs(fighter.x - opponent.x) <= attack["range"] and abs(fighter.y - opponent.y) <= 140:
        fighter.special_hit_done = True
        extra.extend(_apply_attack_hit(match, fighter, opponent, attack, now))
        fighter.anim = attack.get("hit_anim") or attack.get("attack_anim") or fighter.anim

    if fighter.special_until <= now:
        fighter.anim = attack.get("miss_anim") or attack.get("end_anim") or BASE_ANIMATIONS["IDLE"]
        fighter.special_move = None
        fighter.special_hit_done = False

    return extra


def _apply_attack_hit(match, attacker, defender, attack, now):
    """Apply damage and effects. Returns list of extra broadcast packets."""
    extra = []
    chained_hit = defender.hit_until > now
    defender.health = max(0, defender.health - attack["damage"])
    defender.hit_until = now + 220.0
    if defender.y < GROUND_Y:
        defender.anim = BASE_ANIMATIONS["JUMP_HIT"]
    else:
        defender.anim = BASE_ANIMATIONS["HIT"]

    attacker.super_meter = min(100, attacker.super_meter + (18 if attack["damage"] >= 90 else 10))
    attacker.last_hit_was_super = bool(attack.get("super") or attack.get("special_name"))
    attacker.combo_hits = attacker.combo_hits + 1 if chained_hit else 1
    defender.combo_hits = 0

    if attacker.combo_hits >= 2:
        extra.append(xt_room_msg(match.match_id, "cmbo", attacker.index, attacker.combo_hits))

    if attack.get("thrown"):
        extra.append(xt_room_msg(match.match_id, "thrwn", attacker.index + 1))
        defender.anim = BASE_ANIMATIONS["THROWN"]

    if attack.get("effect_id") is not None:
        extra.append(
            xt_room_msg(
                match.match_id,
                "fx",
                attacker.index,
                attack["effect_id"],
                encode_base50(int(defender.x)),
                encode_base50(int(defender.y)),
            )
        )
    if attack.get("projectile_id") is not None:
        extra.append(
            xt_room_msg(
                match.match_id,
                "adpj",
                attacker.index,
                attack["projectile_id"],
                encode_base50(int(attacker.x)),
                encode_base50(int(attacker.y)),
                attack.get("dash", 0),
            )
        )

    shake = attack.get("shake", 0)
    if shake > 0:
        extra.append(xt_room_msg(match.match_id, "shk", shake))

    return extra


def maybe_attack(match, fighter, opponent, now):
    """Decide and apply attack if button bits say so.
    Returns list of extra broadcast packets (shk, thrwn, sups)."""
    bits = fighter.last_key_bits
    extra = []

    # Flags
    crouching = fighter.y >= GROUND_Y and has_bit(bits, 1)
    airborne  = fighter.y < GROUND_Y

    attack = None

    # Super (bit 13, 8192) — costs 100 super meter
    if has_bit(bits, 13) and fighter.super_meter >= 100:
        attack = get_character_super_attack(fighter)
        if attack is None:
            attack = {"anim": BASE_ANIMATIONS["STRONG_PUNCH3"], "damage": 180,
                      "range": 180, "lock": STRONG_LOCK_MS, "shake": 10, "super": True}

    elif has_bit(bits, 8):
        attack = {"anim": BASE_ANIMATIONS["THROW"], "damage": 90,
                  "range": 120, "lock": THROW_LOCK_MS, "shake": 8, "thrown": True}
    else:
        attack = get_character_special_attack(fighter, bits)
    if attack is None and has_bit(bits, 7):
        a = BASE_ANIMATIONS["JUMP_STRONG_KICK1" if airborne else ("LOW_STRONG_KICK1" if crouching else "STRONG_KICK1")]
        attack = {"anim": a, "damage": 70, "range": 155, "lock": STRONG_LOCK_MS, "shake": 7}
    elif attack is None and has_bit(bits, 6):
        a = BASE_ANIMATIONS["JUMP_LIGHT_KICK" if airborne else ("LOW_LIGHT_KICK" if crouching else "LIGHT_KICK")]
        attack = {"anim": a, "damage": 40, "range": 135, "lock": ATTACK_LOCK_MS, "shake": 4}
    elif attack is None and has_bit(bits, 5):
        a = BASE_ANIMATIONS["JUMP_STRONG_PUNCH1" if airborne else ("LOW_STRONG_PUNCH1" if crouching else "STRONG_PUNCH1")]
        attack = {"anim": a, "damage": 60, "range": 145, "lock": STRONG_LOCK_MS, "shake": 6}
    elif attack is None and has_bit(bits, 4):
        a = BASE_ANIMATIONS["JUMP_LIGHT_PUNCH" if airborne else ("LOW_LIGHT_PUNCH" if crouching else "LIGHT_PUNCH")]
        attack = {"anim": a, "damage": 35, "range": 125, "lock": ATTACK_LOCK_MS, "shake": 3}

    if attack is None:
        return extra

    fighter.anim = attack["anim"]
    fighter.attack_until = now + attack["lock"]

    if attack.get("super"):
        fighter.super_meter = 0
        extra.append(xt_room_msg(match.match_id, "sups", fighter.index, 600))

    if attack.get("dash") is not None or attack.get("jump") is not None:
        fighter.special_until = now + attack["lock"]
        fighter.special_move = attack
        fighter.special_hit_done = False
        if attack.get("jump") is not None and fighter.y >= GROUND_Y:
            fighter.vy = float(attack["jump"])

    # Check hit range
    in_range = (abs(fighter.x - opponent.x) <= attack["range"] and abs(fighter.y - opponent.y) <= 120)
    if attack.get("dash") is None and in_range:
        extra.extend(_apply_attack_hit(match, fighter, opponent, attack, now))

    return extra


def update_player(match, fighter, opponent, now):
    """Single-fighter update tick (updatePlayer in cnGame.as)."""
    if fighter.knocked_out:
        fighter.anim = BASE_ANIMATIONS["DEFEAT"]
        return []

    extra = []

    if fighter.special_until > now and fighter.special_move is not None:
        extra = apply_special_movement(match, fighter, opponent, fighter.special_move, now)
    elif fighter.hit_until > now:
        fighter.anim = BASE_ANIMATIONS["JUMP_HIT" if fighter.y < GROUND_Y else "HIT"]
    elif fighter.attack_until <= now:
        apply_movement(fighter, opponent, now)
        extra = maybe_attack(match, fighter, opponent, now)

    # Gravity
    if fighter.y < GROUND_Y or fighter.vy != 0:
        fighter.y += fighter.vy
        fighter.vy += GRAVITY
        if fighter.y >= GROUND_Y:
            fighter.y = float(GROUND_Y)
            fighter.vy = 0.0
            if fighter.attack_until <= now and fighter.hit_until <= now:
                fighter.anim = BASE_ANIMATIONS["IDLE"]

    if (fighter.attack_until <= now and fighter.hit_until <= now and
            fighter.y >= GROUND_Y and not is_neutral_anim(fighter.anim)):
        fighter.anim = BASE_ANIMATIONS["IDLE"]

    return extra


def simulate_frame(match, now):
    """One 40 ms physics tick. Returns list of extra packets to broadcast."""
    f0 = match.fighters[0]
    f1 = match.fighters[1]
    extra = []

    if match.round_resolved:
        if now >= match.round_end_time:
            # resolve_round_end will be called after this frame
            match._resolve_pending = True
        return extra

    update_facing(f0, f1)
    extra.extend(update_player(match, f0, f1, now))
    extra.extend(update_player(match, f1, f0, now))
    clamp_players(f0, f1)

    elapsed = now - match.round_start_time
    if elapsed >= ROUND_TIME_MS:
        _finish_round(match, winner_1based=0, time_up=True)
        return extra

    if f0.health <= 0 and f1.health <= 0:
        _finish_round(match, winner_1based=0, time_up=False)
    elif f0.health <= 0:
        _finish_round(match, winner_1based=2, time_up=False)
    elif f1.health <= 0:
        _finish_round(match, winner_1based=1, time_up=False)

    return extra


def run_simulation(match):
    """Advance simulation by all elapsed 40 ms ticks. Returns extra packets."""
    if not match.round_started:
        return []
    now = now_ms()
    if match.last_tick == 0:
        match.last_tick = now
        return []
    extra = []
    while now - match.last_tick >= FRAME_MS:
        match.last_tick += FRAME_MS
        extra.extend(simulate_frame(match, match.last_tick))
    return extra


def _finish_round(match, winner_1based, time_up):
    """Mark round as resolved (finishRound in cnGame.as)."""
    if match.round_resolved:
        return
    match.round_resolved = True
    match.round_end_time = now_ms() + ROUND_END_DELAY_MS
    match._resolve_pending = False
    match.pending_winner = winner_1based
    match.pending_time_up = time_up
    f0, f1 = match.fighters[0], match.fighters[1]
    if winner_1based == 1:
        f1.knocked_out = True
        f1.anim = BASE_ANIMATIONS["DEFEAT"]
    elif winner_1based == 2:
        f0.knocked_out = True
        f0.anim = BASE_ANIMATIONS["DEFEAT"]


def _build_rndo_packets(match):
    """Build rndo (and optionally win) packets for broadcast. Returns list."""
    f0, f1 = match.fighters[0], match.fighters[1]
    winner  = match.pending_winner
    time_up = match.pending_time_up

    if winner == 1:
        f0.wins += 1
        perfect = "true" if f0.health == 1000 else "false"
    elif winner == 2:
        f1.wins += 1
        perfect = "true" if f1.health == 1000 else "false"
    else:
        perfect = "false"

    pkts = []
    match_winner = -1
    if f0.wins >= 2:
        match_winner = 0
    elif f1.wins >= 2:
        match_winner = 1

    if match_winner != -1:
        pkts.append(game_cmd_win(match.match_id, match_winner))

    pkts.append(game_cmd_rndo(
        match.match_id,
        p1_wins=f0.wins, p2_wins=f1.wins,
        time_up=1 if time_up else 0,
        winner=winner,
        perfect=perfect, comeback="false",
    ))

    if match_winner != -1:
        match.round_started = False  # stop new su after match ends

    return pkts, match_winner


def _build_su_packet(match):
    """Compute round timer and return the current su snapshot packet."""
    f0, f1 = match.fighters[0], match.fighters[1]
    elapsed = now_ms() - match.round_start_time
    timer = max(0, ROUND_TIME - int(elapsed / 1000))
    return game_cmd_su_snapshot(match.match_id, match.next_su_id, timer, f0, f1)


def game_start_round(match):
    """Initialise physics and start broadcasting (startRound in cnGame.as).
    Returns (rnds_packet, first_su_packet)."""
    match.round_number += 1
    match.round_started = True
    match.round_resolved = False
    match._resolve_pending = False
    match.round_start_time = now_ms()
    match.round_end_time = 0.0
    match.last_tick = match.round_start_time
    match.next_su_id = 1

    f0 = match.fighters[0]
    f1 = match.fighters[1]
    reset_fighter_for_round(f0, START_X_1, facing=True)
    reset_fighter_for_round(f1, START_X_2, facing=False)

    rnds_pkt = game_cmd_rnds(match.match_id, match.round_number)
    su_pkt   = _build_su_packet(match)
    match.next_su_id += 1
    return rnds_pkt, su_pkt


def game_reset_for_rematch(match):
    """Reset all game state between rematches (resetForRematch in cnGame.as)."""
    match.map_id = None
    match.round_number = 0
    match.round_started = False
    match.round_resolved = False
    match._resolve_pending = False
    match.next_su_id = 1
    match.pending_winner = 0
    match.pending_time_up = False
    match.round_start_time = 0.0
    match.round_end_time = 0.0
    match.last_tick = 0.0
    for f in match.fighters:
        f.wins = 0
        f.last_key_bits = 0


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
    client_lag: int = 0
    last_client_msg_id: int = 0
    sent_opp_character_type: int | None = None
    sent_ready_map_id: int | None = None
    sent_load_frame: int = -1
    sent_loaded: bool = False
    sent_rematch: bool = False
    sent_opponent_ping: int | None = None
    created: float = field(default_factory=time.time)


@dataclass
class MatchState:
    match_id: int
    players: dict = field(default_factory=dict)
    # Lobby/load state
    round_no: int = 1            # displayed round number (increments each startRound)
    round_sequence_started: bool = False
    round_live: bool = False     # kept for guard in maybe_schedule_round_start
    lded_sent: bool = False
    rnds_sent: bool = False
    map_id: int = None
    # Physics / game state (mirrors cnGame.as game object)
    fighters: list = field(default_factory=lambda: [
        FighterState(index=0, x=float(START_X_1), facing=True),
        FighterState(index=1, x=float(START_X_2), facing=False),
    ])
    round_number: int = 0        # incremented each startRound (JS: game.roundNumber)
    round_started: bool = False
    round_resolved: bool = False
    _resolve_pending: bool = False
    round_start_time: float = 0.0
    round_end_time: float = 0.0
    last_tick: float = 0.0
    next_su_id: int = 1
    pending_winner: int = 0
    pending_time_up: bool = False
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
        me.conn.send_tcp(game_cmd_dl(match.match_id, peer.ping))
    if peer.character_type is not None:
        me.conn.send_tcp(game_cmd_opp(match.match_id, peer.character_type))
    if peer.ready:
        map_id = match.map_id if match.map_id is not None else (match.match_id % 5)
        me.conn.send_tcp(game_cmd_rdy(match.match_id, map_id))
    if peer.fr_progress >= 0:
        me.conn.send_tcp(game_cmd_fr(match.match_id, peer.fr_progress))
    if match.lded_sent:
        me.conn.send_tcp(game_cmd_lded(match.match_id))
    if peer.rematch:
        me.conn.send_tcp(game_cmd_rmch(match.match_id))
    if match.rnds_sent and match.round_started:
        me.conn.send_tcp(game_cmd_rnds(match.match_id, match.round_number))
        # Send a fresh su snapshot so the late-arriving client sees current state
        su_pkt = _build_su_packet(match)
        me.conn.send_tcp(su_pkt, quiet=True)


def sync_opponent_state_to_player(match, player, opponent):
    if not player or not opponent or getattr(player.conn, "closed", False):
        return

    opponent_ping = opponent.ping if opponent.ping is not None else 0
    if player.sent_opponent_ping != opponent_ping:
        player.conn.send_tcp(game_cmd_dl(match.match_id, opponent_ping))
        player.sent_opponent_ping = opponent_ping

    if opponent.character_type is not None and player.sent_opp_character_type != opponent.character_type:
        player.conn.send_tcp(game_cmd_opp(match.match_id, opponent.character_type))
        player.sent_opp_character_type = opponent.character_type

    if opponent.ready:
        if match.map_id is None:
            match.map_id = match.match_id % 5
        if player.sent_ready_map_id != match.map_id:
            player.conn.send_tcp(game_cmd_rdy(match.match_id, match.map_id))
            player.sent_ready_map_id = match.map_id

    if player.sent_load_frame != opponent.fr_progress:
        player.conn.send_tcp(game_cmd_fr(match.match_id, opponent.fr_progress))
        player.sent_load_frame = opponent.fr_progress

    if opponent.loaded and not player.sent_loaded:
        player.conn.send_tcp(game_cmd_lded(match.match_id))
        player.sent_loaded = True

    if opponent.rematch and not player.sent_rematch:
        player.conn.send_tcp(game_cmd_rmch(match.match_id))
        player.sent_rematch = True


def reset_player_sync_state(player):
    player.sent_opp_character_type = None
    player.sent_ready_map_id = None
    player.sent_load_frame = -1
    player.sent_loaded = False
    player.sent_rematch = False
    player.sent_opponent_ping = None


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


def try_force_load_handshake(match):
    if match.round_started or not match.full():
        return

    p1 = match.get(1)
    p2 = match.get(2)
    if not p1 or not p2:
        return
    if not p1.ready or not p2.ready:
        return
    if p1.character_type is None or p2.character_type is None:
        return
    if max(p1.fr_progress, p2.fr_progress) <= 0 and not p1.loaded and not p2.loaded:
        return

    if not p1.loaded:
        p1.fr_progress = 100
        p1.loaded = True
        p2.sent_load_frame = 100
        if p2.cn_seen and not getattr(p2.conn, "closed", False):
            p2.conn.send_tcp(game_cmd_fr(match.match_id, 100))

    if not p2.loaded:
        p2.fr_progress = 100
        p2.loaded = True
        p1.sent_load_frame = 100
        if p1.cn_seen and not getattr(p1.conn, "closed", False):
            p1.conn.send_tcp(game_cmd_fr(match.match_id, 100))


def maybe_schedule_round_start(match, reason=""):
    with STATE_LOCK:
        if not match.full():
            return
        if match.round_sequence_started or match.round_live or match.round_started:
            return
        if not match_prefight_ready(match):
            return
        if not match_load_ready(match):
            return
        match.round_sequence_started = True
        mid = match.match_id
    debug_print(f"[GAME] Match {mid} scheduling round start (reason={reason or 'ready'})")
    threading.Thread(target=_do_round_start_sequence, args=(mid,), daemon=True).start()


def _do_round_start_sequence(mid):
    """Thread: brief delay, then lded → startRound (rnds + first su).
    No server-side countdown — client handles that on receiving rnds."""
    # Brief delay so both clients are ready before game packets arrive.
    with STATE_LOCK:
        match = MATCHES.get(mid)
        if not match:
            return
        match.lded_sent = True
        _broadcast(match, game_cmd_lded(match.match_id))
        for mp in match.players.values():
            reset_player_sync_state(mp)
            mp.sent_loaded = True

    # Small gap then start round (matches JS tryStartLoaded → startRound sequence)
    with STATE_LOCK:
        match = MATCHES.get(mid)
        if not match or match.round_started:
            return
        match.round_live = True
        match.rnds_sent = True
        rnds_pkt, su_pkt = game_start_round(match)
        debug_print(f"[GAME] Match {mid} round {match.round_number} started")

    _broadcast(match, rnds_pkt)
    _broadcast(match, su_pkt, quiet=True)


def _broadcast(match, packet, quiet=False):
    """Send packet to all connected players in match."""
    for idx in (1, 2):
        mp = match.get(idx)
        if mp and not getattr(mp.conn, "closed", False):
            try:
                mp.conn.send_tcp(packet, quiet=quiet)
            except Exception:
                pass


def notify_match_ready(match):
    p1 = match.get(1)
    p2 = match.get(2)
    if not p1 or not p2:
        return
    p1.conn.send_tcp(xt_wrapper_opponent_join(match.match_id, p2.player_index, p2.nick))
    p2.conn.send_tcp(xt_wrapper_opponent_join(match.match_id, p1.player_index, p1.nick))

    def delayed_start():
        time.sleep(WRAPPER_START_DELAY_SECS)
        with STATE_LOCK:
            current = MATCHES.get(match.match_id)
            if not current:
                return
            for idx in (1, 2):
                mp = current.get(idx)
                if mp and not getattr(mp.conn, "closed", False):
                    mp.conn.send_tcp(xt_wrapper_game_start())

    threading.Thread(target=delayed_start, daemon=True).start()


def remove_match_if_empty(match_id):
    with STATE_LOCK:
        match = MATCHES.get(match_id)
        if not match:
            return
        if not match.players:
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
        debug_print(f"[TCP] login -> uid={uid} nick={nick!r} zone={self.zone!r}")
        if send_rndk:
            self.send_tcp(rndK_msg())
            time.sleep(0.03)
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
                debug_print(f"[TCP] Client {self.addr} (uid {self.uid}) disconnected")
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
            debug_print(f"[TCP] Unhandled JSON frame: {frame}")
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
            return

        if "roundTripBench" in xml or "roundTrip" in xml:
            self.send_tcp(roundTripRes_msg())
            return

        debug_print("[TCP] No XML handler matched")

    def process_xt_str(self, frame):
        msg = parse_client_xt_frame(frame)
        if not msg:
            debug_print(f"[TCP] Bad XT frame: {frame}")
            return
        debug_print(f"[TCP] XT parsed: {msg}")
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
            self._do_rgf()
            return

        if cmd == "ka":
            return

        debug_print(f"[TCP] Unhandled XT: ext={ext!r} cmd={cmd!r}")

    def _do_rgf(self):
        old_mid = getattr(self, "match_id", None)
        match, full, created = ensure_player_joined_match(self)
        if not self.match_player:
            self.match_player = match.get(self.player_index)
        if created:
            debug_print(f"[MM] Created match {match.match_id} as P{self.player_index} ({self.nick})")
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

    def handle_lobby_xt(self, cmd, params):
        if cmd == "rlj":
            self.send_tcp(xt_server_msg("_ljs"))
            return
        if cmd == "rlp":
            self.send_tcp(xt_server_msg("_slp"))
            return
        if cmd == "rgf":
            self._do_rgf()
            return
        if cmd == "rgq":
            cleanup_handler_from_match(self, explicit_quit=True)
            return
        if cmd == "ka":
            return
        debug_print(f"[TCP] Unhandled Lobby XT: {cmd}")

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
            debug_print(f"[TCP] cnGame {cmd} ignored: not in a match")
            return

        player = match.get(self.player_index)
        peer = match.other(self.player_index)
        if not player:
            debug_print(f"[TCP] cnGame {cmd} ignored: missing player record")
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
        if params:
            player.last_client_msg_id = parse_int(params[0], player.last_client_msg_id)

        # Utility: value from params considering that params[0] is room_id-equivalent for
        # most cnGame client packets: %xt%cnGame%cmd%room%val% → params=["val"]
        # But for cu: params=["frame","pad"] (two values).
        value = params[1] if len(params) > 1 else (params[0] if params else None)

        # ------------------------------------------------------------------ pi
        if cmd == "pi":
            player.ping = parse_int(value, 0)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} pi={player.ping}")
            self.send_tcp(game_cmd_echo(match.match_id))
            if peer:
                sync_opponent_state_to_player(match, player, peer)
            su_pkt = None
            extra_pkts = []
            resolve_pkts = []
            with STATE_LOCK:
                if match.round_started:
                    extra_pkts = run_simulation(match)
                    if match._resolve_pending and match.round_resolved:
                        match._resolve_pending = False
                        rndo_pkts, match_winner = _build_rndo_packets(match)
                        resolve_pkts = rndo_pkts
                        if match_winner == -1:
                            rnds_pkt, su_pkt_new = game_start_round(match)
                            resolve_pkts.append(rnds_pkt)
                            resolve_pkts.append(su_pkt_new)
                    elif match.round_started:
                        su_pkt = _build_su_packet(match)
                        match.next_su_id += 1
            for pkt in extra_pkts:
                _broadcast(match, pkt, quiet=True)
            for pkt in resolve_pkts:
                _broadcast(match, pkt)
            if su_pkt:
                _broadcast(match, su_pkt, quiet=True)
            maybe_schedule_round_start(match, reason="pi")
            return

        # ----------------------------------------------------------------- typ
        if cmd == "typ":
            player.character_type = parse_int(value, 0)
            match.fighters[player.player_index - 1].character_type = player.character_type
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} typ={player.character_type}")
            if peer and peer.cn_seen and not getattr(peer.conn, "closed", False):
                peer.conn.send_tcp(game_cmd_opp(match.match_id, player.character_type))
                peer.sent_opp_character_type = player.character_type
            maybe_schedule_round_start(match, reason="typ")
            return

        # ----------------------------------------------------------------- rdy
        if cmd == "rdy":
            player.ready = True
            player.ready_value = parse_int(value, 1)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} rdy")
            # Compute and store mapId (chooseMap in cnGame.as = roomId % 5)
            if match.map_id is None:
                match.map_id = match.match_id % 5
            # Send rdy + mapId to opponent (not a raw relay)
            if peer and peer.cn_seen and not getattr(peer.conn, "closed", False):
                peer.conn.send_tcp(game_cmd_rdy(match.match_id, match.map_id))
                peer.sent_ready_map_id = match.map_id
            maybe_schedule_round_start(match, reason="rdy")
            return

        # ------------------------------------------------------------------ fr
        if cmd == "fr":
            raw_value = params[1] if len(params) > 1 else (params[0] if params else "0")
            progress = parse_progress(raw_value)
            if not match.round_sequence_started and not match.round_live:
                player.fr_progress = max(player.fr_progress, progress)
                if player.fr_progress >= 100:
                    player.loaded = True
                debug_print(
                    f"[GAME] Match {match.match_id} P{player.player_index} "
                    f"fr={progress} (max={player.fr_progress})"
                )
                maybe_schedule_round_start(match, reason="fr")
            if peer and peer.cn_seen and not getattr(peer.conn, "closed", False):
                peer.conn.send_tcp(game_cmd_fr(match.match_id, player.fr_progress))
                peer.sent_load_frame = player.fr_progress
            return

        # ---------------------------------------------------------------- strt
        if cmd == "strt":
            player.loaded = True
            player.fr_progress = max(player.fr_progress, 100)
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} strt=loaded")
            maybe_schedule_round_start(match, reason="strt")
            return

        # ---------------------------------------------------------------- rmch
        if cmd == "rmch":
            player.rematch = True
            debug_print(f"[GAME] Match {match.match_id} P{player.player_index} rematch")
            if peer and peer.cn_seen:
                peer.conn.send_tcp(game_cmd_rmch(match.match_id))
                peer.sent_rematch = True
            if match.full() and all(p.rematch for p in match.players.values()):
                # Both agreed — reset game state for new round
                game_reset_for_rematch(match)
                match.round_sequence_started = False
                match.round_live = False
                match.lded_sent = False
                match.rnds_sent = False
                for mp in match.players.values():
                    mp.ready = True
                    mp.ready_value = 1
                    mp.fr_progress = 100
                    mp.loaded = True
                    mp.rematch = False
                    reset_player_sync_state(mp)
                maybe_schedule_round_start(match, reason="rmch")
            return

        # ------------------------------------------------------------------ cu
        if cmd == "cu":
            # cu format: %xt%cnGame%cu%<room>%<frame>%<pad_bits>%
            # params[0]=frame (client-local counter, ignored by server sim)
            # params[1]=pad bitmask (hasBit(bits,N) selects actions)
            pad_bits = parse_int(params[1], 0) if len(params) >= 2 else 0
            player.last_cu_bits = pad_bits

            # Fighter index: player_index is 1-based; fighters[] is 0-based
            fi = player.player_index - 1  # 0 or 1

            su_pkt = None
            extra_pkts = []
            resolve_pkts = []

            with STATE_LOCK:
                if match.round_started:
                    match.fighters[fi].last_key_bits = pad_bits
                    extra_pkts = run_simulation(match)
                    # Check for pending round resolution (ROUND_END_DELAY_MS elapsed)
                    if match._resolve_pending and match.round_resolved:
                        match._resolve_pending = False
                        rndo_pkts, match_winner = _build_rndo_packets(match)
                        resolve_pkts = rndo_pkts
                        if match_winner == -1:
                            # Not over yet — start next round
                            rnds_pkt, su_pkt_new = game_start_round(match)
                            resolve_pkts.append(rnds_pkt)
                            resolve_pkts.append(su_pkt_new)
                    elif match.round_started:
                        su_pkt = _build_su_packet(match)
                        match.next_su_id += 1

            # Send all packets outside the lock
            for pkt in extra_pkts:
                _broadcast(match, pkt, quiet=True)
            for pkt in resolve_pkts:
                _broadcast(match, pkt)
            if su_pkt:
                _broadcast(match, su_pkt, quiet=True)
            return

        # ------------------------------------------------------------------ ka / cl / ct / box
        if cmd == "cl":
            player.client_lag = parse_int(value, player.client_lag)
            if peer:
                self.send_tcp(game_cmd_dl(match.match_id, peer.ping if peer.ping is not None else 0))
            return

        if cmd in ("ka", "ct", "box"):
            return

        # -------- Unknown: raw-relay to match (fx, cmbo, shk, sups, etc.) ----
        relayed = self.broadcast_cngame_to_match(
            match, cmd, params, quiet=(cmd in CNGAME_QUIET_RELAY)
        )
        if relayed:
            if cmd not in CNGAME_QUIET_RELAY:
                debug_print(f"[GAME] Relayed cnGame cmd={cmd} from P{player.player_index}")
            return

        debug_print(f"[TCP] Unhandled cnGame cmd={cmd} params={params}")

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

from http.server import HTTPServer as _HTTPServer


class BlueBoxHTTPRequestHandler(SimpleHTTPRequestHandler):
    server_version = "TKOBlueBox/1.0"

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

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

    def do_GET(self):
        if self.path == "/status.json":
            with STATE_LOCK:
                active_matches = [
                    {
                        "matchId": match.match_id,
                        "players": [p.nick for p in match.players.values()],
                        "round": match.round_number,
                        "started": match.round_started,
                    }
                    for match in MATCHES.values()
                ]
                waiting = WAITING_MATCH_ID
                payload = json.dumps(
                    {
                        "onlinePlayers": current_logged_in_count(),
                        "activeMatches": active_matches,
                        "waitingMatchId": waiting,
                    },
                    separators=(",", ":"),
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
            return
        if self.path == "/BlueBox/HttpBox.do":
            self.send_error(405, "BlueBox endpoint expects POST")
            return
        return super().do_GET()

    def do_HEAD(self):
        if self.path == "/BlueBox/HttpBox.do":
            self.send_error(405, "BlueBox endpoint expects POST")
            return
        return super().do_HEAD()

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


class ThreadedHTTPServer(ThreadingMixIn, _HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- Optional static file server ----------

class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        debug_print("[STATIC] " + fmt % args)


class ThreadedStaticHTTPServer(ThreadingMixIn, _HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- Boot ----------

def run_servers(bind_host, tcp_port, http_port, static_dir=None, static_port=None, policy_port=DEFAULT_POLICY_PORT):
    policy_server = ThreadedTCPServer((bind_host, policy_port), FlashPolicyHandler)
    threading.Thread(target=policy_server.serve_forever, name="policy-server", daemon=True).start()
    debug_print(f"Flash policy server listening on {bind_host}:{policy_port}")

    tcp_server = ThreadedTCPServer((bind_host, tcp_port), SmartFoxTCPHandler)
    threading.Thread(target=tcp_server.serve_forever, name="tcp-server", daemon=True).start()
    debug_print(f"SmartFox TCP emulator listening on {bind_host}:{tcp_port}")

    http_root = static_dir or os.getcwd()
    http_handler = partial(BlueBoxHTTPRequestHandler, directory=http_root)
    http_server = ThreadedHTTPServer((bind_host, http_port), http_handler)
    threading.Thread(target=http_server.serve_forever, name="http-server", daemon=True).start()
    debug_print(f"HTTP server listening on {bind_host}:{http_port} serving {http_root} with BlueBox at /BlueBox/HttpBox.do")

    static_server = None
    if static_dir:
        handler = partial(QuietStaticHandler, directory=static_dir)
        static_server = ThreadedStaticHTTPServer((bind_host, static_port), handler)
        threading.Thread(target=static_server.serve_forever, name="static-server", daemon=True).start()
        debug_print(f"Static file server on {bind_host}:{static_port} serving {static_dir}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        debug_print("Shutting down...")
    finally:
        for srv in [policy_server, tcp_server, http_server, static_server]:
            if srv:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description="TKO SmartFox/BlueBox emulator")
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--advertise-ip", default=None)
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--policy-port", type=int, default=DEFAULT_POLICY_PORT)
    parser.add_argument("--send-all-http", action="store_true")
    parser.add_argument("--static-dir", default=None)
    parser.add_argument("--static-port", type=int, default=DEFAULT_STATIC_PORT)
    parser.add_argument("--write-cnsl", default=None)
    args = parser.parse_args()

    global HTTP_SEND_ALL_QUEUED
    HTTP_SEND_ALL_QUEUED = bool(args.send_all_http)

    advertise_ip = args.advertise_ip or auto_detect_advertise_ip()
    base_dir = os.path.dirname(os.path.abspath(__file__))
    loaded_chars = load_character_data(base_dir)

    cnsl_target = args.write_cnsl
    if cnsl_target is None and args.static_dir:
        cnsl_target = os.path.join(args.static_dir, "cnsl.xml")

    if cnsl_target:
        os.makedirs(os.path.dirname(os.path.abspath(cnsl_target)), exist_ok=True)
        write_cnsl_xml(cnsl_target, advertise_ip)
        debug_print(f"Wrote cnsl.xml to: {cnsl_target}")

    debug_print("Starting TKO server (PATCHED)")
    debug_print("Bind host:", args.bind)
    debug_print("Advertise IP:", advertise_ip)
    debug_print("TCP (SmartFox) port:", args.tcp_port)
    debug_print("HTTP (BlueBox) port:", args.http_port)
    debug_print("Policy port:", args.policy_port)
    debug_print("Character XML loaded:", loaded_chars)
    debug_print(f"su ticker: {SU_TICK_HZ} fps")
    debug_print(f'Use this cnsl.xml entry: <server name="local">{advertise_ip}</server>')

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
