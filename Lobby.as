var CLIENT_LOBBY_ID = 2;
var transportRoomId = -1;
var nextGameId = 1001;
var waitingUsers = [];

function init() {
    trace("Lobby extension initialized");
    transportRoomId = resolveTransportRoomId();
    trace("Resolved transport room id: " + transportRoomId);
}

function handleInternalEvent(evt)
{
    if (evt == null || evt.name == null) {
        return;
    }

    if (evt.name == "userExit" || evt.name == "userLost" || evt.name == "logout") {
        removeWaitingUser(evt.user);
    }
}

function dumpUser(user)
{
    trace("=== USER DUMP ===");
    trace("ID: " + user.getUserId());
    trace("Name: " + user.getName());
    trace("IP: " + user.getIpAddress());
    trace("Login Time: " + user.getLoginTime());
    trace("Last Msg Time: " + user.getLastMessageTime());
    trace("Moderator: " + user.isModerator());
    trace("Spectator: " + user.isSpectator());
}

function handleRequest(cmd, params, user, fromRoom)
{
    trace("handleRequest called");

    if (cmd == "rlj") {
        trace("Lobby Join Request Made");

        var responseObj = [];
        var room = ensureTransportRoom();
        var ok = room != null;

        trace("curr room" + fromRoom);
        dumpUser(user);

        if (ok) {
            trace("LOBBY Join Success!");
        }
        else {
            trace("LOBBY JOIN failed");
        }

        responseObj.push("_ljs");
        responseObj.push(CLIENT_LOBBY_ID);
        sendProtocol(responseObj, [user]);
        trace("sent _ljs");
    }

    if (cmd == "rlp") {
        trace("Lobby Ping Request Made");
        sendProtocol(["_slp"], [user]);
        trace("sent _slp");
    }

    if (cmd == "rgf") {
        trace("----GAME MATCHING----");
        if (params != null && params.length > 0) {
            trace("RoundTrip: " + params[0]);
        }
        queueForMatch(user);
    }
}

function destroy() {}

function ensureTransportRoom()
{
    var zone = _server.getCurrentZone();
    var room = null;

    if (zone == null) {
        return null;
    }

    room = findZoneRoomByName(zone, "Lobby");
    if (room == null) {
        room = findZoneRoomByName(zone, "Lobby0");
    }
    if (room == null && transportRoomId >= 0) {
        room = zone.getRoom(transportRoomId);
    }

    if (room == null) {
        trace("Unable to find configured transport room");
        return null;
    }

    transportRoomId = getRoomNumericId(room, transportRoomId);
    return room;
}

function findZoneRoomByName(zone, roomName)
{
    var rooms = zone.getRoomList();
    var i = 0;

    if (rooms == null) {
        return null;
    }

    for (i = 0; i < rooms.size(); i++) {
        var room = rooms.get(i);
        if (room != null && room.getName != null && room.getName() == roomName) {
            return room;
        }
    }

    return null;
}

function queueForMatch(user)
{
    if (user == null) {
        return;
    }

    removeWaitingUser(user);
    waitingUsers.push(user);
    trace("Queued user " + user.getName() + " for match. Queue size: " + waitingUsers.length);

    if (waitingUsers.length < 2) {
        return;
    }

    var user1 = waitingUsers.shift();
    var user2 = waitingUsers.shift();

    if (user1 == null || user2 == null) {
        return;
    }

    startMatch(user1, user2);
}

function startMatch(user1, user2)
{
    var gameId = nextGameId;
    var clientRoomId = nextGameId;
    nextGameId++;

    trace("Starting logical game " + gameId + " in client room " + clientRoomId);

    sendProtocol(["_gjs", clientRoomId, 1], [user1]);
    sendProtocol(["_gjs", clientRoomId, 2], [user2]);

    sendProtocol(["_oj", 2, user2.getName().toUpperCase()], [user1]);
    sendProtocol(["_oj", 1, user1.getName().toUpperCase()], [user2]);

    sendProtocol(["_strt"], [user1]);
    sendProtocol(["_strt"], [user2]);
}

function sendProtocol(payload, users)
{
    ensureTransportRoom();
    _server.sendResponse(payload, transportRoomId, null, users, _server.PROTOCOL_STR);
}

function removeWaitingUser(user)
{
    if (user == null) {
        return;
    }

    for (var i = waitingUsers.length - 1; i >= 0; i--) {
        if (waitingUsers[i] != null && waitingUsers[i].getUserId() == user.getUserId()) {
            waitingUsers.splice(i, 1);
        }
    }
}

function getRoomNumericId(roomLike, fallback)
{
    if (roomLike == null) {
        return fallback;
    }

    if (typeof(roomLike) == "number") {
        return roomLike;
    }

    if (roomLike.getId != null) {
        return roomLike.getId();
    }

    if (roomLike.getRoomId != null) {
        return roomLike.getRoomId();
    }

    if (roomLike.id != null) {
        return roomLike.id;
    }

    if (roomLike.roomId != null) {
        return roomLike.roomId;
    }

    if (roomLike._id != null) {
        return roomLike._id;
    }

    return fallback;
}

function resolveTransportRoomId()
{
    var room = ensureTransportRoom();

    if (room != null) {
        return getRoomNumericId(room, transportRoomId);
    }

    return 1;
}


