var gamesByRoom = {};
var BASE50 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN";
var FRAME_MS = 40;
var ROUND_TIME = 99;
var ROUND_TIME_MS = 99000;
var START_X_1 = 600;
var START_X_2 = 1000;
var GROUND_Y = 550;
var SCREEN_WIDTH = 800;
var LEVEL_WIDTH = 1600;
var CAMERA_MARGIN = 120;
var MOVE_SPEED = 18;
var JUMP_SPEED = 45;
var GRAVITY = 4;
var ATTACK_LOCK_MS = 280;
var STRONG_LOCK_MS = 420;
var THROW_LOCK_MS = 520;
var ROUND_END_DELAY_MS = 800;
var CHARACTER_ANIMATIONS = {};
var CHARACTER_XML_DIR = null;
var BASE_ANIMATIONS = {
    IDLE: 0,
    WALK_FWD: 1,
    WALK_BACK: 2,
    JUMP_UP: 3,
    JUMP_FRONT: 4,
    JUMP_BACK: 5,
    LIGHT_KICK: 6,
    STRONG_KICK1: 7,
    STRONG_KICK2: 8,
    STRONG_KICK3: 9,
    LIGHT_PUNCH: 10,
    STRONG_PUNCH1: 11,
    STRONG_PUNCH2: 12,
    STRONG_PUNCH3: 13,
    THROW: 14,
    CROUCH: 15,
    LOW_LIGHT_PUNCH: 16,
    LOW_STRONG_PUNCH1: 17,
    LOW_STRONG_PUNCH2: 18,
    LOW_LIGHT_KICK: 19,
    LOW_STRONG_KICK1: 20,
    LOW_STRONG_KICK2: 21,
    JUMP_LIGHT_KICK: 22,
    JUMP_STRONG_KICK1: 23,
    JUMP_STRONG_KICK2: 24,
    JUMP_LIGHT_PUNCH: 25,
    JUMP_STRONG_PUNCH1: 26,
    JUMP_STRONG_PUNCH2: 27,
    THROWN: 28,
    LOW_BLOCK: 29,
    BLOCK: 30,
    DIZZY: 31,
    HIT: 32,
    JUMP_HIT: 33,
    LOW_HIT: 34,
    KNOCKDOWN: 35,
    RECOVER: 36,
    DEFEAT: 37,
    VICTORY: 38,
    FROZEN: 39,
    THROWN_END: 40,
    REACH: 41
};
function init()
{
    trace("=================================");
    trace("cnGame extension initialized");
    loadCharacterAnimations();
    trace("=================================");
}

function destroy()
{
    trace("cnGame extension destroyed");
}

function loadCharacterAnimations()
{
    CHARACTER_ANIMATIONS = {};
    CHARACTER_XML_DIR = resolveCharacterXmlDirectory();
    if (CHARACTER_XML_DIR == null) {
        trace("No character xml directory found for cnGame extension");
        return;
    }
    var files = CHARACTER_XML_DIR.listFiles();
    var loaded = 0;
    for (var i = 0; i < files.length; i++) {
        var file = files[i];
        if (file == null || !file.isFile()) {
            continue;
        }
        var name = String(file.getName());
        if (!name.match(/^\d+\.xml$/i)) {
            continue;
        }
        var character = parseCharacterXmlFile(file);
        if (character == null || character.charId == null) {
            continue;
        }
        CHARACTER_ANIMATIONS[String(character.charId)] = character;
        loaded++;
    }
    trace("Loaded " + loaded + " character xml files from " + String(CHARACTER_XML_DIR.getPath()));
}
function resolveCharacterXmlDirectory()
{
    var File = java.io.File;
    var exactCandidates = [
        "C:\\Program Files (x86)\\SmartFoxServerPRO_1.6.6\\Server\\webserver\\webapps\\root\\xml"
    ];
    var relativeCandidates = ["xml", "4_0/xml", "4_0", "tko/4_0", "root/xml", "root/4_0", "root/4_0/xml"];
    var userDir = new File(java.lang.System.getProperty("user.dir"));
    var current = userDir;
    var i = 0;

    for (i = 0; i < exactCandidates.length; i++) {
        var exactDir = new File(exactCandidates[i]);
        if (exactDir.exists() && exactDir.isDirectory() && countCharacterXmlFiles(exactDir) > 0) {
            return exactDir;
        }
    }

    while (current != null) {
        for (i = 0; i < relativeCandidates.length; i++) {
            var dir = new File(current, relativeCandidates[i]);
            if (dir.exists() && dir.isDirectory() && countCharacterXmlFiles(dir) > 0) {
                return dir;
            }
        }
        current = current.getParentFile();
    }

    return null;
}

function countCharacterXmlFiles(dir)
{
    if (dir == null) {
        return 0;
    }
    var files = dir.listFiles();
    if (files == null) {
        return 0;
    }
    var count = 0;
    for (var i = 0; i < files.length; i++) {
        var file = files[i];
        if (file != null && file.isFile() && String(file.getName()).match(/^\d+\.xml$/i)) {
            count++;
        }
    }
    return count;
}
function parseCharacterXmlFile(file)
{
    try {
        return parseCharacterXmlText(readTextFile(file));
    } catch (err) {
        trace("Failed to parse character xml " + String(file.getPath()) + ": " + err);
        return null;
    }
}
function readTextFile(file)
{
    var reader = new java.io.BufferedReader(new java.io.InputStreamReader(new java.io.FileInputStream(file), "UTF-8"));
    var lines = [];
    var line = null;
    try {
        while ((line = reader.readLine()) != null) {
            lines.push(String(line));
        }
    } finally {
        reader.close();
    }
    return lines.join("\n");
}
function parseCharacterXmlText(xmlText)
{
    var data = {};
    var charId = trimString(firstMatch(xmlText, /<robot[^>]*charId\s*=\s*"([^"]+)"/i));
    data.charId = charId == null ? null : parseInt(charId, 10);
    data.name = trimString(firstMatch(xmlText, /<name>\s*([^<]+)\s*<\/name>/i));
    data.specialAnimations = parseAnimationSection(xmlText, "specialAnimations");
    data.effectAnimations = parseAnimationSection(xmlText, "effectAnimations");
    data.projectileAnimations = parseAnimationSection(xmlText, "projectileAnimations");
    data.specials = {};
    var bits = ["9", "10", "11", "12"];
    for (var i = 0; i < bits.length && i < data.specialAnimations.list.length; i++) {
        data.specials[bits[i]] = data.specialAnimations.list[i];
    }
    return data;
}
function parseAnimationSection(xmlText, sectionName)
{
    var result = {};
    result.list = [];
    result.byId = {};
    result.byName = {};
    var sectionPattern = new RegExp("<" + sectionName + ">\\s*([\\s\\S]*?)<\\/" + sectionName + ">", "i");
    var section = firstMatch(xmlText, sectionPattern);
    if (section == null) {
        return result;
    }
    var animPattern = /<anim>\s*([\s\S]*?)<\/anim>/gi;
    var match = null;
    while ((match = animPattern.exec(section)) != null) {
        var anim = parseAnimationBlock(match[1]);
        if (anim == null || anim.id == null) {
            continue;
        }
        result.list.push(anim);
        result.byId[String(anim.id)] = anim;
        if (anim.name != null) {
            result.byName[String(anim.name).toUpperCase()] = anim;
        }
    }
    return result;
}
function parseAnimationBlock(block)
{
    var anim = {};
    var idValue = trimString(firstMatch(block, /<id>\s*([^<]+)\s*<\/id>/i));
    if (idValue == null) {
        return null;
    }
    anim.id = parseInt(idValue, 10);
    anim.name = trimString(firstMatch(block, /<name>\s*([^<]+)\s*<\/name>/i));
    anim.start = numberOrDefault(trimString(firstMatch(block, /<start>\s*([^<]+)\s*<\/start>/i)), 0);
    anim.end = numberOrDefault(trimString(firstMatch(block, /<end>\s*([^<]+)\s*<\/end>/i)), 0);
    anim.rate = numberOrDefault(trimString(firstMatch(block, /<rate>\s*([^<]+)\s*<\/rate>/i)), 30);
    anim.loop = trimString(firstMatch(block, /<loop>\s*([^<]+)\s*<\/loop>/i)) == "true";
    anim.loopStart = numberOrDefault(trimString(firstMatch(block, /<loopStart>\s*([^<]+)\s*<\/loopStart>/i)), -1);
    anim.shouldRotate = trimString(firstMatch(block, /<shouldRotate>\s*([^<]+)\s*<\/shouldRotate>/i)) == "true";
    return anim;
}
function firstMatch(text, pattern)
{
    if (text == null) {
        return null;
    }
    var match = pattern.exec(String(text));
    if (match == null || match.length < 2) {
        return null;
    }
    return match[1];
}
function trimString(value)
{
    if (value == null) {
        return null;
    }
    return String(value).replace(/^\s+|\s+$/g, "");
}

function getCharacterAnimationSet(characterType)
{
    var charData = CHARACTER_ANIMATIONS[String(characterType)];
    if (charData == null) {
        loadCharacterAnimations();
        charData = CHARACTER_ANIMATIONS[String(characterType)];
    }
    return charData;
}

function getAnimationId(characterType, animationName, fallbackId)
{
    if (animationName == null) {
        return fallbackId;
    }
    if (BASE_ANIMATIONS[animationName] != null) {
        return BASE_ANIMATIONS[animationName];
    }
    var charData = getCharacterAnimationSet(characterType);
    if (charData != null) {
        var key = String(animationName).toUpperCase();
        if (charData.specialAnimations.byName[key] != null) {
            return charData.specialAnimations.byName[key].id;
        }
        if (charData.effectAnimations.byName[key] != null) {
            return charData.effectAnimations.byName[key].id;
        }
        if (charData.projectileAnimations.byName[key] != null) {
            return charData.projectileAnimations.byName[key].id;
        }
    }
    return fallbackId;
}

function setPlayerAnim(player, animationName, fallbackId)
{
    if (player == null) {
        return;
    }
    player.anim = getAnimationId(player.characterType, animationName, fallbackId);
}

function handleInternalEvent(evt)
{
    if (evt == null || evt.name == null) {
        return;
    }

    trace("Event Name: " + evt.name);

    if (evt.name == "userExit" || evt.name == "userLost" || evt.name == "logout") {
        dropUserFromGames(evt.user);
    }
}

function handleRequest(cmd, params, user, fromRoom)
{
    if (cmd != "pi") {
        trace("cnGame received command: " + cmd);
    }

    var game = getGame(fromRoom);
    var player = ensurePlayer(game, user);
    var opponent = getOpponent(game, user);
    var payload = cloneArray(params);

    if (payload.length > 0) {
        player.lastClientMsgId = numberOrDefault(payload[0], player.lastClientMsgId);
    }

    if (cmd == "pi") {
        handlePing(game, player, opponent, payload, fromRoom);
        return;
    }

    if (cmd == "typ") {
        player.characterType = numberOrDefault(payload[1], player.characterType);
        trace("Player " + player.name + " selected character " + player.characterType);
        if (opponent != null && player.characterType != null) {
            sendToUser(opponent.user, fromRoom, ["opp", player.characterType]);
        }
        return;
    }

    if (cmd == "rdy") {
        player.ready = true;
        trace("Player " + player.name + " is ready");
        if (game.mapId == null) {
            game.mapId = chooseMap(game);
        }
        if (opponent != null) {
            sendToUser(opponent.user, fromRoom, ["rdy", game.mapId]);
        }
        tryForceLoadHandshake(game, fromRoom);
        return;
    }

    if (cmd == "fr") {
        player.loadFrame = numberOrDefault(payload[1], 0);
        if (player.loadFrame == 0 || player.loadFrame == 25 || player.loadFrame == 50 || player.loadFrame == 75 || player.loadFrame >= 100) {
            trace("Load progress " + player.name + ": " + player.loadFrame + " loaded=" + player.loaded);
        }
        if (player.loadFrame >= 100) {
            player.loaded = true;
        }
        if (opponent != null) {
            sendToUser(opponent.user, fromRoom, ["fr", player.loadFrame]);
        }
        tryStartLoaded(game, fromRoom);
        return;
    }

    if (cmd == "strt") {
        player.loaded = true;
        trace("Received strt from " + player.name);
        tryStartLoaded(game, fromRoom);
        return;
    }

    if (cmd == "rmch") {
        player.rematch = true;
        if (opponent != null) {
            sendToUser(opponent.user, fromRoom, ["rmch", 1]);
        }
        if (allPlayersHave(game, "rematch")) {
            resetForRematch(game);
        }
        return;
    }

    if (cmd == "cu") {
        player.lastKeyBits = numberOrDefault(firstData(payload), 0);
        runSimulation(game, fromRoom);
        if (game.roundStarted) {
            sendRoundSnapshot(game, fromRoom);
        }
        return;
    }

    if (cmd == "cl") {
        player.clientLag = numberOrDefault(payload[1], player.clientLag);
        if (opponent != null) {
            sendToUser(user, fromRoom, ["dl", opponent.avgPing]);
        }
        return;
    }

    if (cmd == "ct" || cmd == "box") {
        return;
    }

    trace("Unhandled cnGame command: " + cmd);
}

function getGame(fromRoom)
{
    var roomId = getRoomId(fromRoom);

    if (gamesByRoom[roomId] == null) {
        gamesByRoom[roomId] = createGame(roomId);
    }

    return gamesByRoom[roomId];
}

function createGame(roomId)
{
    var game = {};
    game.roomId = roomId;
    game.players = [];
    game.players[0] = null;
    game.players[1] = null;
    game.mapId = null;
    game.roundNumber = 0;
    game.lastTick = 0;
    game.roundStarted = false;
    game.roundStartTime = 0;
    game.roundEndTime = 0;
    game.roundResolved = false;
    game.nextSuId = 1;
    return game;
}

function ensurePlayer(game, user)
{
    var existing = getPlayer(game, user);

    if (existing != null) {
        return existing;
    }

    var index = 0;
    if (game.players[0] != null) {
        index = 1;
    }

    var player = createPlayer(user, index);
    game.players[index] = player;
    return player;
}

function createPlayer(user, index)
{
    var player = {};
    player.userId = user.getUserId();
    player.user = user;
    player.name = user.getName();
    player.index = index;
    player.characterType = null;
    player.ready = false;
    player.loaded = false;
    player.rematch = false;
    player.loadFrame = 0;
    player.avgPing = 0;
    player.clientLag = 0;
    player.lastKeyBits = 0;
    player.lastClientMsgId = 0;
    player.x = index == 0 ? START_X_1 : START_X_2;
    player.y = GROUND_Y;
    player.vy = 0;
    player.health = 1000;
    player.superMeter = 0;
    player.anim = 0;
    player.facing = index == 0;
    player.attackUntil = 0;
    player.hitUntil = 0;
    player.wins = 0;
    player.lastAttackAt = 0;
    player.knockedOut = false;
    player.specialUntil = 0;
    player.specialMove = null;
    player.specialHitDone = false;
    player.sentOppCharacterType = null;
    player.sentReadyMapId = null;
    player.sentLoadFrame = -1;
    player.sentLoaded = false;
    player.sentRematch = false;
    player.sentOpponentPing = null;
    return player;
}

function getPlayer(game, user)
{
    for (var i = 0; i < game.players.length; i++) {
        var player = game.players[i];
        if (player != null && player.userId == user.getUserId()) {
            player.user = user;
            player.name = user.getName();
            return player;
        }
    }

    return null;
}

function getOpponent(game, user)
{
    var player = getPlayer(game, user);
    if (player == null) {
        return null;
    }

    if (player.index == 0) {
        return game.players[1];
    }

    return game.players[0];
}

function handlePing(game, player, opponent, payload, fromRoom)
{
    if (payload.length > 1) {
        player.avgPing = numberOrDefault(payload[1], player.avgPing);
    }

    sendToUser(player.user, fromRoom, ["echo"]);

    if (opponent != null) {
        if (player.sentOpponentPing != opponent.avgPing) {
            sendToUser(player.user, fromRoom, ["dl", opponent.avgPing]);
            player.sentOpponentPing = opponent.avgPing;
        }

        syncOpponentState(game, player, opponent, fromRoom);
    }

    runSimulation(game, fromRoom);
    if (game.roundStarted) {
        sendRoundSnapshot(game, fromRoom);
    }
}

function syncOpponentState(game, player, opponent, fromRoom)
{
    if (player == null || opponent == null) {
        return;
    }

    if (opponent.characterType != null && player.sentOppCharacterType != opponent.characterType) {
        sendToUser(player.user, fromRoom, ["opp", opponent.characterType]);
        player.sentOppCharacterType = opponent.characterType;
    }

    if (opponent.ready) {
        if (game.mapId == null) {
            game.mapId = chooseMap(game);
        }

        if (player.sentReadyMapId != game.mapId) {
            sendToUser(player.user, fromRoom, ["rdy", game.mapId]);
            player.sentReadyMapId = game.mapId;
        }
    }

    if (player.sentLoadFrame != opponent.loadFrame) {
        sendToUser(player.user, fromRoom, ["fr", opponent.loadFrame]);
        player.sentLoadFrame = opponent.loadFrame;
    }

    if (opponent.loaded && !player.sentLoaded) {
        sendToUser(player.user, fromRoom, ["lded"]);
        player.sentLoaded = true;
    }

    if (opponent.rematch && !player.sentRematch) {
        sendToUser(player.user, fromRoom, ["rmch", 1]);
        player.sentRematch = true;
    }
}

function tryForceLoadHandshake(game, fromRoom)
{
    if (game == null || game.roundStarted) {
        return;
    }

    if (game.players[0] == null || game.players[1] == null) {
        return;
    }

    if (!game.players[0].ready || !game.players[1].ready) {
        return;
    }

    if (game.players[0].characterType == null || game.players[1].characterType == null) {
        return;
    }

    if (!game.players[0].loaded) {
        game.players[0].loadFrame = 100;
        game.players[0].loaded = true;
        sendToUser(game.players[1].user, fromRoom, ["fr", 100]);
    }

    if (!game.players[1].loaded) {
        game.players[1].loadFrame = 100;
        game.players[1].loaded = true;
        sendToUser(game.players[0].user, fromRoom, ["fr", 100]);
    }

    trace("Forced load handshake after both players became ready");
    tryStartLoaded(game, fromRoom);
}

function tryStartLoaded(game, fromRoom)
{
    if (game == null || game.roundStarted) {
        return;
    }

    if (game.players[0] != null && game.players[1] != null) {
        trace("Loaded check p1=" + game.players[0].loaded + "(" + game.players[0].loadFrame + ") p2=" + game.players[1].loaded + "(" + game.players[1].loadFrame + ")");
    }

    if (allPlayersHave(game, "loaded")) {
        trace("All players loaded, sending lded");
        sendToAll(game, fromRoom, ["lded"]);
        startRound(game, fromRoom);
    }
}

function runSimulation(game, fromRoom)
{
    if (!game.roundStarted) {
        return;
    }

    var now = nowMs();
    if (game.lastTick == 0) {
        game.lastTick = now;
        return;
    }

    if (now - game.lastTick < FRAME_MS) {
        return;
    }

    while (now - game.lastTick >= FRAME_MS) {
        game.lastTick += FRAME_MS;
        simulateFrame(game, fromRoom, game.lastTick);
    }
}

function simulateFrame(game, fromRoom, now)
{
    var p1 = game.players[0];
    var p2 = game.players[1];

    if (p1 == null || p2 == null) {
        return;
    }

    if (game.roundResolved) {
        if (now >= game.roundEndTime) {
            resolveRoundEnd(game, fromRoom);
        }
        return;
    }

    updateFacing(p1, p2);
    updatePlayer(game, p1, p2, now, fromRoom);
    updatePlayer(game, p2, p1, now, fromRoom);
    clampPlayers(p1, p2);

    var elapsed = now - game.roundStartTime;
    if (elapsed >= ROUND_TIME_MS) {
        finishRound(game, fromRoom, 0, true);
        return;
    }

    if (p1.health <= 0 && p2.health <= 0) {
        finishRound(game, fromRoom, 0, false);
        return;
    }

    if (p1.health <= 0) {
        finishRound(game, fromRoom, 2, false);
        return;
    }

    if (p2.health <= 0) {
        finishRound(game, fromRoom, 1, false);
        return;
    }
}

function updatePlayer(game, player, opponent, now, fromRoom)
{
    if (player == null || opponent == null) {
        return;
    }

    if (player.knockedOut) {
        setPlayerAnim(player, "DEFEAT", 37);
        return;
    }

    if (player.specialUntil > now && player.specialMove != null) {
        applySpecialMovement(game, player, opponent, now, fromRoom);
    } else if (player.hitUntil > now) {
        if (player.y < GROUND_Y) {
            setPlayerAnim(player, "JUMP_HIT", 33);
        } else {
            setPlayerAnim(player, "HIT", 32);
        }
    } else if (player.attackUntil <= now) {
        applyMovement(player, opponent, now);
        maybeAttack(game, player, opponent, now, fromRoom);
    }

    if (player.y < GROUND_Y || player.vy != 0) {
        player.y += player.vy;
        player.vy += GRAVITY;
        if (player.y >= GROUND_Y) {
            player.y = GROUND_Y;
            player.vy = 0;
            if (player.attackUntil <= now && player.hitUntil <= now) {
                setPlayerAnim(player, "IDLE", 0);
            }
        }
    }

    if (player.attackUntil <= now && player.hitUntil <= now && player.y == GROUND_Y && isNeutralAnim(player.anim) == false) {
        setPlayerAnim(player, "IDLE", 0);
    }
}

function applyMovement(player, opponent, now)
{
    var bits = player.lastKeyBits;
    var moveLeft = hasBit(bits, 2);
    var moveRight = hasBit(bits, 3);
    var down = hasBit(bits, 1);
    var up = hasBit(bits, 0);

    if (player.y == GROUND_Y && up) {
        player.vy = -JUMP_SPEED;
        if (moveLeft || moveRight) {
            setPlayerAnim(player, player.facing ? "JUMP_FRONT" : "JUMP_BACK", player.facing ? 4 : 5);
        } else {
            setPlayerAnim(player, "JUMP_UP", 3);
        }
    }

    if (player.y == GROUND_Y) {
        if (moveLeft && !moveRight) {
            player.x -= MOVE_SPEED;
            setPlayerAnim(player, player.facing ? "WALK_BACK" : "WALK_FWD", player.facing ? 2 : 1);
        } else if (moveRight && !moveLeft) {
            player.x += MOVE_SPEED;
            setPlayerAnim(player, player.facing ? "WALK_FWD" : "WALK_BACK", player.facing ? 1 : 2);
        } else if (down) {
            setPlayerAnim(player, "CROUCH", 15);
        } else {
            setPlayerAnim(player, "IDLE", 0);
        }
    } else {
        if (moveLeft && !moveRight) {
            player.x -= MOVE_SPEED * 0.6;
        } else if (moveRight && !moveLeft) {
            player.x += MOVE_SPEED * 0.6;
        }

        if (moveLeft && !moveRight) {
            setPlayerAnim(player, player.facing ? "JUMP_BACK" : "JUMP_FRONT", player.facing ? 5 : 4);
        } else if (moveRight && !moveLeft) {
            setPlayerAnim(player, player.facing ? "JUMP_FRONT" : "JUMP_BACK", player.facing ? 4 : 5);
        } else {
            setPlayerAnim(player, "JUMP_UP", 3);
        }
    }
}

function maybeAttack(game, player, opponent, now, fromRoom)
{
    var bits = player.lastKeyBits;
    var attack = null;
    var crouching = player.y == GROUND_Y && hasBit(bits, 1);
    var airborne = player.y < GROUND_Y;
    var forward = player.facing ? 1 : -1;

    if (hasBit(bits, 13) && player.superMeter >= 100) {
        attack = {anim:getAnimationId(player.characterType, "STRONG_PUNCH3", 13), damage:180, range:180, lock:STRONG_LOCK_MS, superCost:100, shake:10, superEvent:true};
    } else if (hasBit(bits, 8)) {
        attack = {anim:getAnimationId(player.characterType, "THROW", 14), damage:90, range:120, lock:THROW_LOCK_MS, shake:8, thrown:true};
    } else {
        attack = getCharacterSpecialAttack(player, bits, forward);
    }

    if (attack == null && hasBit(bits, 7)) {
        attack = {anim:airborne ? getAnimationId(player.characterType, "JUMP_STRONG_KICK1", 23) : (crouching ? getAnimationId(player.characterType, "LOW_STRONG_KICK1", 20) : getAnimationId(player.characterType, "STRONG_KICK1", 7)), damage:70, range:155, lock:STRONG_LOCK_MS, shake:7};
    } else if (attack == null && hasBit(bits, 6)) {
        attack = {anim:airborne ? getAnimationId(player.characterType, "JUMP_LIGHT_KICK", 22) : (crouching ? getAnimationId(player.characterType, "LOW_LIGHT_KICK", 19) : getAnimationId(player.characterType, "LIGHT_KICK", 6)), damage:40, range:135, lock:ATTACK_LOCK_MS, shake:4};
    } else if (attack == null && hasBit(bits, 5)) {
        attack = {anim:airborne ? getAnimationId(player.characterType, "JUMP_STRONG_PUNCH1", 26) : (crouching ? getAnimationId(player.characterType, "LOW_STRONG_PUNCH1", 17) : getAnimationId(player.characterType, "STRONG_PUNCH1", 11)), damage:60, range:145, lock:STRONG_LOCK_MS, shake:6};
    } else if (attack == null && hasBit(bits, 4)) {
        attack = {anim:airborne ? getAnimationId(player.characterType, "JUMP_LIGHT_PUNCH", 25) : (crouching ? getAnimationId(player.characterType, "LOW_LIGHT_PUNCH", 16) : getAnimationId(player.characterType, "LIGHT_PUNCH", 10)), damage:35, range:125, lock:ATTACK_LOCK_MS, shake:3};
    }

    if (attack == null) {
        return;
    }

    player.anim = attack.anim;
    player.attackUntil = now + attack.lock;
    player.lastAttackAt = now;

    if (attack.dash != null || attack.jump != null) {
        player.specialUntil = now + attack.lock;
        player.specialMove = attack;
        player.specialHitDone = false;
        if (attack.jump != null && player.y == GROUND_Y) {
            player.vy = attack.jump;
        }
    }

    if (attack.superEvent) {
        player.superMeter = 0;
        sendToAll(game, fromRoom, ["sups", player.index, 600]);
    }

    if (attack.dash == null && Math.abs(player.x - opponent.x) <= attack.range && Math.abs(player.y - opponent.y) <= 120) {
        applyAttackHit(game, player, opponent, attack, fromRoom, now);
    }
}

function applySpecialMovement(game, player, opponent, now, fromRoom)
{
    var move = player.specialMove;
    if (move == null) {
        return;
    }

    if (move.dash != null) {
        player.x += move.dash;
    }

    if (!player.specialHitDone && Math.abs(player.x - opponent.x) <= move.range && Math.abs(player.y - opponent.y) <= 140) {
        player.specialHitDone = true;
        applyAttackHit(game, player, opponent, move, fromRoom, now);
    }

    if (player.specialUntil <= now) {
        player.specialMove = null;
        player.specialHitDone = false;
    }
}

function applyAttackHit(game, player, opponent, attack, fromRoom, now)
{
    opponent.health -= attack.damage;
    if (opponent.health < 0) {
        opponent.health = 0;
    }
    opponent.hitUntil = now + 220;
    setPlayerAnim(opponent, opponent.y < GROUND_Y ? "JUMP_HIT" : "HIT", opponent.y < GROUND_Y ? 33 : 32);
    player.superMeter += attack.damage >= 90 ? 18 : 10;
    if (player.superMeter > 100) {
        player.superMeter = 100;
    }

    if (attack.thrown) {
        sendToAll(game, fromRoom, ["thrwn", player.index + 1]);
        setPlayerAnim(opponent, "THROWN", 28);
    }

    if (attack.shake > 0) {
        sendToAll(game, fromRoom, ["shk", attack.shake]);
    }
}

function getCharacterSpecialAttack(player, bits, forward)
{
    var charData = getCharacterAnimationSet(player.characterType);
    var spec = null;
    var specBit = null;

    if (hasBit(bits, 10)) {
        specBit = "10";
    } else if (hasBit(bits, 9)) {
        specBit = "9";
    } else if (hasBit(bits, 12)) {
        specBit = "12";
    } else if (hasBit(bits, 11)) {
        specBit = "11";
    }

    if (specBit == null || charData == null) {
        return null;
    }

    spec = charData.specials[specBit];
    if (spec == null) {
        return null;
    }

    return inferSpecialAttackFromName(spec, forward, specBit == "10" || specBit == "12");
}

function inferSpecialAttackFromName(spec, forward, strong)
{
    var name = String(spec.name).toUpperCase();
    var attack = {};
    attack.anim = spec.id;
    attack.damage = strong ? 105 : 75;
    attack.range = strong ? 210 : 175;
    attack.lock = strong ? 680 : 540;
    attack.shake = strong ? 9 : 6;

    if (name.indexOf("FIREBALL") != -1 || name.indexOf("GRENADE") != -1 || name.indexOf("SPIT") != -1 || name.indexOf("DISK") != -1 || name.indexOf("LASER") != -1 || name.indexOf("SWORD") != -1 || name.indexOf("BLASTER") != -1 || name.indexOf("TIMEBALL") != -1 || name.indexOf("ELEC") != -1 || name.indexOf("GUNS") != -1 || name.indexOf("BUBBY") != -1 || name.indexOf("FOODTHROW") != -1) {
        attack.range = strong ? 320 : 260;
        attack.lock = strong ? 720 : 600;
        attack.shake = strong ? 10 : 7;
    } else if (name.indexOf("WHIP") != -1 || name.indexOf("VINESPIKE") != -1 || name.indexOf("WAVES") != -1 || name.indexOf("ROCKS") != -1 || name.indexOf("CAKE") != -1 || name.indexOf("DEBRIS") != -1 || name.indexOf("HANDS") != -1 || name.indexOf("CLOUD") != -1 || name.indexOf("SPIKE") != -1) {
        attack.range = strong ? 250 : 205;
        attack.lock = strong ? 650 : 540;
        attack.shake = strong ? 8 : 6;
    }

    if (name.indexOf("DASH") != -1 || name.indexOf("RUSH") != -1 || name.indexOf("HEADBUTT") != -1 || name.indexOf("SLIDE") != -1 || name.indexOf("PHASE") != -1 || name.indexOf("TIMEWALK") != -1) {
        attack.dash = (strong ? 24 : 18) * forward;
    }

    if (name.indexOf("FLIPKICK") != -1 || name.indexOf("FALLKICK") != -1 || name.indexOf("STOMP") != -1 || name.indexOf("SPLASH") != -1 || name.indexOf("SWINGKICK") != -1 || name.indexOf("MONKEYSWING") != -1 || name.indexOf("VERT_KICK") != -1 || name.indexOf("SPINKICK") != -1 || name.indexOf("JAKEDROP") != -1) {
        attack.dash = (strong ? 16 : 12) * forward;
        attack.jump = strong ? -18 : -12;
    }

    if (name.indexOf("UPPERCUT") != -1) {
        attack.damage = strong ? 115 : 85;
        attack.range = strong ? 185 : 150;
        attack.jump = strong ? -14 : -8;
    }

    if (name.indexOf("GRAB") != -1 || name.indexOf("THROW") != -1) {
        attack.damage = strong ? 120 : 90;
        attack.range = strong ? 165 : 145;
        attack.thrown = true;
    }

    return attack;
}
function finishRound(game, fromRoom, winnerIndexOneBased, timeUp)
{
    if (game.roundResolved) {
        return;
    }

    game.roundResolved = true;
    game.roundEndTime = nowMs() + ROUND_END_DELAY_MS;
    game.pendingWinner = winnerIndexOneBased;
    game.pendingTimeUp = timeUp;

    if (winnerIndexOneBased == 1) {
        game.players[1].knockedOut = true;
        setPlayerAnim(game.players[1], "DEFEAT", 37);
    } else if (winnerIndexOneBased == 2) {
        game.players[0].knockedOut = true;
        setPlayerAnim(game.players[0], "DEFEAT", 37);
    }
}

function resolveRoundEnd(game, fromRoom)
{
    var p1 = game.players[0];
    var p2 = game.players[1];
    var winner = game.pendingWinner;
    var timeUp = game.pendingTimeUp ? 1 : 0;
    var perfect = "false";
    var comeback = "false";
    var matchWinnerZeroBased = -1;

    if (winner == 1) {
        p1.wins++;
        if (p1.health == 1000) {
            perfect = "true";
        }
    } else if (winner == 2) {
        p2.wins++;
        if (p2.health == 1000) {
            perfect = "true";
        }
    }

    if (p1.wins >= 2) {
        matchWinnerZeroBased = 0;
    } else if (p2.wins >= 2) {
        matchWinnerZeroBased = 1;
    }

    if (matchWinnerZeroBased != -1) {
        sendToAll(game, fromRoom, ["win", matchWinnerZeroBased]);
    }

    sendToAll(game, fromRoom, ["rndo", p1.wins, p2.wins, timeUp, winner, perfect, comeback]);

    if (matchWinnerZeroBased != -1) {
        game.roundStarted = false;
        return;
    }

    startRound(game, fromRoom);
}

function startRound(game, fromRoom)
{
    var p1 = game.players[0];
    var p2 = game.players[1];
    if (p1 == null || p2 == null) {
        return;
    }

    game.roundNumber++;
    game.roundStarted = true;
    game.roundResolved = false;
    game.roundStartTime = nowMs();
    game.roundEndTime = 0;
    game.lastTick = game.roundStartTime;

    resetFighterForRound(p1, START_X_1, true);
    resetFighterForRound(p2, START_X_2, false);

    sendToAll(game, fromRoom, ["rnds", game.roundNumber]);
    sendRoundSnapshot(game, fromRoom);
}


function resetFighterForRound(player, x, facing)
{
    player.x = x;
    player.y = GROUND_Y;
    player.vy = 0;
    player.facing = facing;
    player.health = 1000;
    player.superMeter = 0;
    setPlayerAnim(player, "IDLE", 0);
    player.attackUntil = 0;
    player.hitUntil = 0;
    player.knockedOut = false;
    player.specialUntil = 0;
    player.specialMove = null;
    player.specialHitDone = false;
    player.lastKeyBits = 0;
}

function sendRoundSnapshot(game, fromRoom)
{
    var p1 = game.players[0];
    var p2 = game.players[1];
    if (p1 == null || p2 == null) {
        return;
    }

    var elapsed = nowMs() - game.roundStartTime;
    var roundTimer = ROUND_TIME - Math.floor(elapsed / 1000);
    if (roundTimer < 0) {
        roundTimer = 0;
    }

    var minVisibleX = Math.min(p1.x, p2.x) - CAMERA_MARGIN;
    var maxVisibleX = Math.max(p1.x, p2.x) + CAMERA_MARGIN;
    var camGoal = Math.floor(((minVisibleX + maxVisibleX) / 2) - (SCREEN_WIDTH / 2));
    if (camGoal > minVisibleX) {
        camGoal = Math.floor(minVisibleX);
    }
    if (camGoal + SCREEN_WIDTH < maxVisibleX) {
        camGoal = Math.floor(maxVisibleX - SCREEN_WIDTH);
    }
    if (camGoal < 0) {
        camGoal = 0;
    }
    if (camGoal > LEVEL_WIDTH - SCREEN_WIDTH) {
        camGoal = LEVEL_WIDTH - SCREEN_WIDTH;
    }
    var msg = [
        "su",
        game.nextSuId,
        roundTimer,
        encodeBase50(camGoal),
        0,
        encodeBase50(Math.floor(p1.x)),
        encodeBase50(Math.floor(p1.y)),
        p1.anim,
        p1.facing ? "1" : "0",
        encodeBase50(p1.health),
        p1.superMeter,
        1,
        encodeBase50(Math.floor(p2.x)),
        encodeBase50(Math.floor(p2.y)),
        p2.anim,
        p2.facing ? "1" : "0",
        encodeBase50(p2.health),
        p2.superMeter
    ];

    game.nextSuId++;
    sendToAll(game, fromRoom, msg);
}

function updateFacing(p1, p2)
{
    p1.facing = p1.x <= p2.x;
    p2.facing = p2.x < p1.x;
}

function clampPlayers(p1, p2)
{
    if (p1.x < 150) {
        p1.x = 150;
    }
    if (p2.x > 1450) {
        p2.x = 1450;
    }
    if (p1.x > p2.x - 120) {
        var center = (p1.x + p2.x) / 2;
        p1.x = center - 60;
        p2.x = center + 60;
    }
}

function chooseMap(game)
{
    var seed = game.roomId;
    if (seed < 0) {
        seed = 0;
    }
    return seed % 5;
}

function allPlayersHave(game, prop)
{
    if (game.players[0] == null || game.players[1] == null) {
        return false;
    }

    return game.players[0][prop] && game.players[1][prop];
}

function sendToAll(game, fromRoom, message)
{
    for (var i = 0; i < game.players.length; i++) {
        if (game.players[i] != null) {
            sendToUser(game.players[i].user, fromRoom, cloneArray(message));
        }
    }
}

function sendToUser(user, fromRoom, message)
{
    if (user == null || message == null) {
        return;
    }

    _server.sendResponse(message, getRoomId(fromRoom), null, [user], _server.PROTOCOL_STR);
}

function dropUserFromGames(user)
{
    if (user == null) {
        return;
    }

    for (var key in gamesByRoom) {
        var game = gamesByRoom[key];
        var player = getPlayer(game, user);
        if (player == null) {
            continue;
        }

        var opponent = getOpponent(game, user);
        if (opponent != null) {
            opponent.wins++;
            sendToUser(opponent.user, game.roomId, ["win", opponent.index]);
            sendToUser(opponent.user, game.roomId, ["rndo", game.players[0] != null ? game.players[0].wins : 0, game.players[1] != null ? game.players[1].wins : 0, 0, opponent.index + 1, "false", "false"]);
        }

        game.players[player.index] = null;

        if (game.players[0] == null && game.players[1] == null) {
            delete gamesByRoom[key];
        }
    }
}

function getRoomId(fromRoom)
{
    if (fromRoom == null) {
        return -1;
    }

    if (typeof(fromRoom) == "number") {
        return fromRoom;
    }

    if (fromRoom.getId != null) {
        return fromRoom.getId();
    }

    if (fromRoom.getRoomId != null) {
        return fromRoom.getRoomId();
    }

    if (fromRoom.id != null) {
        return fromRoom.id;
    }

    if (fromRoom.roomId != null) {
        return fromRoom.roomId;
    }

    if (fromRoom._id != null) {
        return fromRoom._id;
    }

    return -1;
}

function cloneArray(source)
{
    var copy = [];
    if (source == null) {
        return copy;
    }

    for (var i = 0; i < source.length; i++) {
        copy.push(source[i]);
    }

    return copy;
}

function firstData(params)
{
    if (params == null || params.length < 2) {
        return null;
    }

    return params[1];
}

function numberOrDefault(value, fallback)
{
    var parsed = Number(value);
    if (isNaN(parsed)) {
        return fallback;
    }
    return parsed;
}

function nowMs()
{
    return (new Date()).getTime();
}

function hasBit(bits, bit)
{
    return (bits & (1 << bit)) != 0;
}

function isNeutralAnim(anim)
{
    return anim == 0 || anim == 1 || anim == 2 || anim == 3 || anim == 4 || anim == 5 || anim == 15;
}

function encodeBase50(value)
{
    var negative = false;
    if (value < 0) {
        negative = true;
        value = -value;
    }

    if (value > 2499) {
        value = 2499;
    }

    var high = Math.floor(value / 50);
    var low = value % 50;
    var out = BASE50.charAt(high) + BASE50.charAt(low);
    if (negative) {
        out = "-" + out;
    }
    return out;
}

function resetForRematch(game)
{
    game.mapId = null;
    game.roundNumber = 0;
    game.roundStarted = false;
    game.roundResolved = false;
    game.nextSuId = 1;

    for (var i = 0; i < game.players.length; i++) {
        if (game.players[i] != null) {
            game.players[i].ready = false;
            game.players[i].loaded = false;
            game.players[i].rematch = false;
            game.players[i].loadFrame = 0;
            game.players[i].lastKeyBits = 0;
            game.players[i].lastAttackAt = 0;
            game.players[i].wins = 0;
        }
    }
}

































