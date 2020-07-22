"""Creating and managing WebTiles websocket connections."""

import asyncio
if hasattr(asyncio, 'ensure_future'):
  ensure_future = asyncio.ensure_future
else:  # use of async keyword has been Deprecated since Python 3.4.4
  ensure_future =  getattr(asyncio, "async")

import logging
import os
import re
import signal
import sys
import time
import traceback
import webtiles
from websockets.exceptions import ConnectionClosed

from .chat import ChatWatcher, BotCommandException, bot_help_command
from .chat import pluralize_name
from .version import version as Version

_log = logging.getLogger()

# How long to wait in seconds before reattempting a WebSocket connection.
_retry_connection_wait = 5
# How many seconds to wait after sending a login or watch request before we
# timeout.
_request_timeout = 10
# How many seconds to wait after a game ends before attempting to watch the
# game again.
_rewatch_wait = 5

class ConnectionHandler():
    """This class provides some basic support to continuous read/respond tasks.
    This code is common to both the lobby connection and game connections, but
    isn't general enough to be in the webtiles package itself."""

    def __init__(self, manager, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.manager = manager
        self.task = None
        self.ping_task = None

    @asyncio.coroutine
    def start_ping(self):
        while True:
            if not self.connected():
                return

            try:
                yield from self.websocket.ping()

            except asyncio.CancelledError:
                return

            except Exception:
                self.log_exception("unable to send ping")
                yield from self.manager.stop_connection(self)
                return

            yield from asyncio.sleep(60)

    @asyncio.coroutine
    def handle_pre_read(self):
        pass

    @asyncio.coroutine
    def start(self):
        if not self.connected():
            try:
                yield from self.connect()

            except Exception:
                self.log_exception("unable to connect")
                yield from asyncio.sleep(_retry_connection_wait)
                ensure_future(self.manager.stop_connection(self))
                return

        self.ping_task = ensure_future(self.start_ping())

        while True:
            yield from self.handle_pre_read()

            messages = None
            try:
                messages = yield from self.read()

            except asyncio.CancelledError:
                return

            except Exception:
                self.log_exception("unable to read WebSocket")
                ensure_future(self.manager.stop_connection(self))
                return

            if not messages:
                continue

            for message in messages:
                try:
                    yield from self.handle_message(message)

                except asyncio.CancelledError:
                    return

                except Exception:
                    self.log_exception("unable to handle WebSocket message")
                    ensure_future(self.manager.stop_connection(self))
                    return


class LobbyConnection(webtiles.WebTilesConnection, ConnectionHandler):
    """Lobby connection. Only needed due to different connection arguments and
    formatting on error messages."""

    def __init__(self, manager, *args, **kwargs):
        super().__init__(manager, *args, **kwargs)

    def connect(self):
        yield from super().connect(
            websocket_url=self.manager.conf["server_url"],
            protocol_version=self.manager.conf["protocol_version"])

    def log_exception(self, error_msg):
        exc_type, exc_value, exc_tb = sys.exc_info()
        _log.error("WebTiles: In lobby connection, %s: ", error_msg)
        _log.error("".join(traceback.format_exception(
            exc_type, exc_value, exc_tb)))


class GameConnection(webtiles.WebTilesGameConnection, ConnectionHandler,
                     ChatWatcher):
    """A game websocket connection that watches chat and responds to
    commands."""

    def __init__(self, manager, player, game_id, *args, **kwargs):
        super().__init__(manager, *args, **kwargs)

        self.player = player
        self.game_id = game_id
        self.source_type_desc = "chat"

        self.time_since_request = None

        self.need_greeting = False
        if manager.conf.get("greeting_text"):
            user_data = manager.bot_db.get_user_data(player)
            if user_data and user_data["subscription"] > 0:
                self.need_greeting = False
            else:
                self.need_greeting = True
        # Last time we either send the watch command or had watched a game,
        # used so we can reuse connections, but end them after being idle for
        # too long.
        self.last_reminder_time = None

    @property
    def user(self):
        """The user of this chat source is the player."""
        return self.player

    def describe(self):
        if not self.player:
            return "{} (undefined player)"

        return "{} {}".format(pluralize_name(self.player),
                self.source_type_desc)

    def connect(self):
        yield from super().connect(self.manager.conf["server_url"],
                                   self.manager.conf["username"],
                                   self.manager.conf["password"],
                                   self.manager.conf["protocol_version"])

    def get_source_ident(self):
        """Get a unique identifier dict of the game for this connection.
        Identifies this game connection as a source for chat watching. This is
        used to map DCSS queries to their results as they're received."""

        return {"service" : self.manager.service,
                "player" : self.player,
                "game_id" : self.game_id}

    @asyncio.coroutine
    def handle_pre_read(self):
        """For a game connection, we check timeouts on login and watch
        requests, and greet the user if we're autowatching them."""

        if (self.time_since_request
            and time.time() - self.time_since_request >= _request_timeout):
            ensure_future(self.manager.stop_connection(self))
            return

        if (self.logged_in
            and self.player
            and not self.watching
            and not self.time_since_request):
            yield from self.send_watch_game(self.player,
                                            self.game_id)
            self.time_since_request = time.time()

        if not self.watching or not self.need_greeting:
            return

        greeting = self.manager.conf["greeting_text"].replace("\n", " ")
        greeting = greeting.replace("%n", self.login_user)
        yield from self.send_chat(greeting)
        self.need_greeting = False

    def get_chat_dcss_nicks(self, sender):
        nicks = set()
        for username in self.spectators:
            if self.is_allowed_user(username):
                nicks.add(username)
        return nicks

    def is_allowed_user(self, user):
        """Return True if the user is allowed to execute bot commands."""

        if self.manager.user_is_admin(user):
            return True

        if self.manager.user_is_ignored(user):
            return False

        user_data = self.manager.bot_db.get_user_data(self.player)
        if user_data and user_data["player_only"]:
            return user.lower() == self.player.lower()

        return True

    @asyncio.coroutine
    def send_chat(self, message, message_type="normal"):
        """Send a WebTiles chat message. We currently shut down the game
        connection if an error occurs and log the event, but don't raise to the
        caller, since we don't care to take any action."""

        if message_type == "action":
            message = "*{}* {}".format(self.login_user, message)
        # In case any other beem bot happens to watch in the same
        # channel, don't cause a feedback loop by relaying Sequell output.
        elif self.message_needs_escape(message):
            message = "]" + message

        try:
            yield from self.send({"msg" : "chat_msg", "text" : message})

        except Exception as e:
            self.log_exception("unable to send chat message {}".format(
                message))
            ensure_future(self.manager.stop_connection(self))
            return

    @asyncio.coroutine
    def handle_message(self, message):
        if message["msg"] == "login_success":
            self.time_since_request = None

        elif message["msg"] == "login_fail":
            _log.critical("WebTiles: Login to %s failed, shutting down "
                          "server.", self.manager.conf["server_url"])
            os.kill(os.getpid(), signal.SIGTERM)

        elif message["msg"] == "watching_started":
            self.time_since_request = None
            _log.info("WebTiles: Watching user %s", self.player)

        elif message["msg"] == "game_ended" and self.watching:
            _log.info("WebTiles: Game ended for user %s", self.player)
            ensure_future(self.manager.stop_connection(self))
            return

        elif ((message["msg"] == "go_lobby"
               or message["msg"] == "go" and message["path"] == "/")
              and self.watching):
            # The game we were watching stopped for some reason.
            _log.warning("WebTiles: Told to go to lobby while watching user "
                         "%s.", self.player)
            ensure_future(self.manager.stop_connection(self))
            return

        elif self.logged_in and message["msg"] == "chat":
            user, chat_message = self.parse_chat_message(message)
            yield from self.read_chat(user, chat_message)

        yield from super().handle_message(message)


class WebTilesManager():
    def __init__(self, conf, bot_db, dcss_manager):
        self.conf = conf
        self.bot_db = bot_db
        self.dcss_manager = dcss_manager
        self.bot_commands = bot_commands

        self.service = "WebTiles"
        dcss_manager.managers["WebTiles"] = self
        self.single_user = conf.get("watch_player") is not None

        self.lobby = None
        self.autowatch_candidate = None
        self.autowatch = None
        self.watch_queue = []
        self.connections = set()

    def get_connection(self, username, game_id):
        """Get any existing connection for the given game."""

        if (self.autowatch
            and self.autowatch.player
            and self.autowatch.player == username
            and self.autowatch.game_id == game_id):
            return self.autowatch

        for conn in self.connections:
            if (conn.player
                and conn.player == username
                and conn.game_id == game_id):
                return conn

        return

    def get_source_by_ident(self, ident):
        return self.get_connection(ident["player"], ident["game_id"])

    @asyncio.coroutine
    def stop_connection(self, conn):
        """Shut down a WebTiles connection. If the connection is a game
        connection, it has its game connection entry removed (including
        autowatch).

        Note: This cancels the connection's start() tasks, so any coroutine
        that might call this through start() should use asyncio.ensure_future()
        to schedule instead of yield, otherwise that call to stop_connection()
        itself can be cancelled."""

        if conn.task and not conn.task.done():
            conn.task.cancel()

        if conn.ping_task and not conn.ping_task.done():
            conn.ping_task.cancel()

        if conn is self.autowatch:
            self.autowatch = None
        elif conn in self.connections:
            if conn.watching:
                self.set_watch_end(conn)
            self.connections.remove(conn)

        try:
            yield from conn.disconnect()
        except Exception:
            conn.log_exception("error attempting disconnect")

    @asyncio.coroutine
    def try_new_connection(self, player, game_id):
        """Try to make a new subscriber connection."""

        if len(self.connections) >= self.conf["max_watched_subscribers"]:
            return

        conn = GameConnection(self, player, game_id)
        conn.task = ensure_future(conn.start())
        self.connections.add(conn)

    @asyncio.coroutine
    def disconnect(self):
        if self.lobby:
            yield from self.stop_connection(self.lobby)

        if self.autowatch:
            yield from self.stop_connection(self.autowatch)

        for conn in list(self.connections):
            yield from self.stop_connection(conn)

        self.watch_queue = []

    @asyncio.coroutine
    def start(self):
        """Start the WebTiles service manager."""

        _log.info("WebTiles: Starting manager")

        if not self.lobby:
            self.lobby = LobbyConnection(self)

        while True:
            if not self.lobby.task or self.lobby.task.done():
                self.lobby.task = ensure_future(self.lobby.start())

            autowatch_game = None
            if self.conf["protocol_version"] >= 2 or self.lobby.lobby_complete:
                autowatch_game = self.process_lobby()
            if autowatch_game:
                yield from self.do_autowatch_game(autowatch_game)
            else:
                yield from self.check_current_autowatch()

            yield from self.process_queue()
            yield from asyncio.sleep(0.5)

    def add_queue(self, player, game_id, pos=None):
        """Add a game to the watch queue. It will be watched when a watching
        slot is available."""

        entry = {"username" : player,
                 "game_id"  : game_id,
                 "time_end" : None}
        if pos is None:
            pos = len(self.watch_queue)

        self.watch_queue.insert(pos, entry)
        return

    def get_queue_entry(self, player, game_id):
        for entry in self.watch_queue:
            ### XXX For now we ignore game_id, since webtiles can't make unique
            ### watch URLs by game for the same user.
            if entry["username"] == player:
                return entry

        return

    @asyncio.coroutine
    def do_autowatch_game(self, game):
        player, game_id = game
        if (self.autowatch
                and self.autowatch.player == player
                and self.autowatch.game_id == game_id):
            return

        _log.info("WebTiles: Found new autowatch user %s", player)
        if self.autowatch and self.autowatch.watching:
            _log.info("WebTiles: Stopping autowatch for user %s: new "
                      "autowatch game found", self.autowatch.player)

        if not self.autowatch:
            self.autowatch = GameConnection(self, player, game_id)
            self.autowatch.task = ensure_future(self.autowatch.start())
        else:
            try:
                yield from self.autowatch.send_watch_game(player, game_id)

            except Exception:
                yield from self.stop_connection(self.autowatch)


    @asyncio.coroutine
    def check_current_autowatch(self):
        """When we don't find a new autowatch candidate, check that we're still
        able to watch our present autowatch game."""

        if not self.autowatch:
            return

        lobby_entry = None
        for entry in self.lobby.lobby_entries:
            if (entry["username"] == self.autowatch.player
                and entry["game_id"] == self.autowatch.game_id):
                lobby_entry = entry
                break

        # Game no longer has a lobby entry, but let the connection itself
        # handle any stop watching event from the server.
        if not lobby_entry:
            return

        # See if this game is no longer eligable for autowatch. We don't
        # require a min. spectator count after the initial autowatch, since
        # doing so just leads to a lot of fluctations in autowatching.
        idle_time = (lobby_entry["idle_time"] +
                     time.time() - lobby_entry["time_last_update"])
        game_allowed = self.is_game_allowed(self.autowatch.player,
                                            self.autowatch.game_id)
        end_reason = None
        if not game_allowed:
            end_reason = "Game disallowed"
        elif not self.dcss_manager.ready():
            end_reason = "DCSS not ready"
        elif idle_time >= self.conf["max_game_idle"]:
            end_reason = "Game idle"
        else:
            return

        _log.info("WebTiles: Stopping autowatch for user %s: %s",
                  self.autowatch.player, end_reason)
        yield from self.stop_connection(self.autowatch)

    def process_lobby(self):
        """Process lobby entries, adding games to the watch queue and return an
        autowatch candidate if one is found."""

        autowatch_spectators = -1
        min_spectators = self.conf["min_autowatch_spectators"]
        current_time = time.time()
        autowatch_game = None
        max_subscribers = self.conf["max_watched_subscribers"]
        for entry in self.lobby.lobby_entries:
            subscribed = self.user_is_subscribed(entry["username"])
            queue_entry = self.get_queue_entry(entry["username"],
                                               entry["game_id"])
            idle_time = (entry["idle_time"] +
                         current_time - entry["time_last_update"])
            if (not self.is_game_allowed(entry["username"], entry["game_id"])
                or idle_time >= self.conf["max_game_idle"]):
                continue

            if subscribed and not queue_entry:
                self.add_queue(entry["username"], entry["game_id"])

            conn = self.get_connection(entry["username"], entry["game_id"])
            # Only subscribers who don't have subscriber slots are valid
            # autowatch candidates.
            no_free_slot = (not conn in self.connections
                            and len(self.connections) >= max_subscribers)
            # Find an autowatch candidate
            if (self.conf.get("autowatch_enabled")
                    and self.dcss_manager.ready()
                    and entry["spectator_count"] >= min_spectators
                    and (not subscribed or no_free_slot)
                    and (conn
                        and conn is self.autowatch
                        # If there's a tie, favor a game we're already
                        # autowatching instead of letting the order of
                        # iteration decide.
                        and entry["spectator_count"] == autowatch_spectators
                        # This is chosen because it has more spectators.
                        or entry["spectator_count"] > autowatch_spectators)):
                autowatch_spectators = entry["spectator_count"]
                autowatch_game = (entry["username"], entry["game_id"])

        return autowatch_game

    @asyncio.coroutine
    def process_queue(self):
        """Update the subscriber watch queue, watching any games that we
        can."""

        timeout = self.conf["game_rewatch_timeout"]
        max_subscribers = self.conf["max_watched_subscribers"]
        for entry in list(self.watch_queue):
            lobby = self.lobby.get_lobby_entry(entry["username"],
                                               entry["game_id"])
            idle_time = 0
            if lobby:
                idle_time = (lobby["idle_time"] +
                             time.time() - lobby["time_last_update"])
            idle = idle_time >= self.conf["max_game_idle"]

            allowed = self.is_game_allowed(entry["username"], entry["game_id"])
            wait = (entry["time_end"]
                    and time.time() - entry["time_end"] < _rewatch_wait)
            expired = (not entry["time_end"]
                       or time.time() - entry["time_end"] >= timeout)
            conn = self.get_connection(entry["username"], entry["game_id"])
            if conn:
                end_reason = None
                if not allowed:
                    end_reason = "Game disallowed"
                if not self.dcss_manager.ready():
                    end_reason = "DCSS not ready"
                elif idle:
                    end_reason = "Game idle"
                if end_reason:
                    _log.info("WebTiles: Stopping watching of user %s: %s",
                              entry["username"], end_reason)
                    yield from self.stop_connection(conn)
                # An autowatched subscriber without a subscriber slot now has
                # one.
                elif (conn is self.autowatch
                      and len(self.connections) < max_subscribers):
                    self.connections.add(conn)
                    self.autowatch = None
                    continue

            # The queue entry is no longer valid.
            if not allowed or idle or not lobby and expired:
                self.watch_queue.remove(entry)
                continue

            # We can't watch yet or they already have a subscriber slot.
            if not self.dcss_manager.ready() or not lobby or wait or conn:
                continue

            # Try to give the game a subscriber slot. If this fails, the entry
            # will remain in the queue for subsequent attempts.
            yield from self.try_new_connection(entry["username"],
                                               entry["game_id"])

    def set_watch_end(self, conn):
        queue = self.get_queue_entry(conn.player, conn.game_id)
        if not queue:
            return

        queue["time_end"] = time.time()

    def user_is_admin(self, user):
        """Return True if the user is a bot admin."""

        if not self.conf.get('admins'):
            return False

        lname = user.lower()
        for u in self.conf['admins']:
            if u.lower() == lname:
                return True

        return False

    def user_is_ignored(self, user):
        if not self.conf.get('ignored_users'):
            return False

        lname = user.lower()
        for u in self.conf['ignored_users']:
            if u.lower() == lname:
                return True

        return False

    def can_watch_user(self, username):
        """Can we ever watch this user? If in single user mode, we only return
        true for the watch user. Otherwise, return true if the user is not
        ignored and is not unsubscribed."""

        if self.conf.get("watch_player"):
            return username == self.conf["watch_player"]

        if self.user_is_ignored(username):
            return False

        user_data = self.bot_db.get_user_data(username)
        if user_data and user_data["subscription"] < 0:
            return False

        return True

    def is_game_allowed(self, username, game_id):
        """Can this game ever be watched? A game is disallowed if the user is
        not allowed or the game is of too old a version."""

        if not self.can_watch_user(username):
            return False

        # Check for old, untested versions.
        match = re.search(r"([.0-9]+)", game_id)
        if match:
            try:
                version = float(match.group(1))
            except ValueError:
                return True

            if version < 0.10:
                return False

        return True

    def user_is_subscribed(self, username):
        user_data = self.bot_db.get_user_data(username)
        return user_data and user_data["subscription"] > 0


@asyncio.coroutine
def bot_subscribe_command(source, username):
    """!subscribe chat command"""

    bot_db = source.manager.bot_db
    user_data = bot_db.get_user_data(username)
    if not user_data:
        user_data = bot_db.register_user(username)

    if user_data["subscription"] == 1:
        raise BotCommandException("User {} is already subscribed".format(
            username))

    bot_db.set_user_field(username, "subscription", 1)
    yield from source.send_chat(
        "Subscribed. I will now watch all games of user {}".format(username))

@asyncio.coroutine
def bot_unsubscribe_command(source, username):
    """!unsubscribe chat command"""

    bot_db = source.manager.bot_db
    user_data = bot_db.get_user_data(username)
    if not user_data:
        user_data = bot_db.register_user(username)

    if user_data["subscription"] == -1:
        raise BotCommandException("User {} is already unsubscribed".format(
            username))

    bot_db.set_user_field(username, "subscription", -1)
    msg = "Unsubscribed. I will no longer watch games of user {}.".format(
        username)
    # We'll be leaving the chat of this source.
    if source.player == username:
        msg += " Bye!"
    yield from source.send_chat(msg)

@asyncio.coroutine
def bot_status_command(source, *args):
    """!status chat command"""

    mgr = source.manager
    report = "Version {}".format(Version)

    if mgr.autowatch and mgr.autowatch.watching:
        num_specs = len(mgr.autowatch.spectators)

        if mgr.autowatch.player in mgr.autowatch.spectators:
            num_specs -= 1

        report += "; Autowatching user {} with {} spec(s)".format(
            mgr.autowatch.player, num_specs)

    if mgr.connections:
        names = sorted(
            [conn.player.lower() for conn in mgr.connections])
        report += "; Watching {} subscriber(s): {}".format(
                len(mgr.connections), ", ".join(names))

    yield from source.send_chat(report)

@asyncio.coroutine
def bot_player_only_command(source, username, state=None):
    """!player-only chat command"""

    mgr = source.manager
    user_data = mgr.bot_db.get_user_data(username)

    if not user_data:
        user_data = mgr.bot_db.register_user(username)

    if state is None:
        state_desc = "on" if user_data["player_only"] else "off"
        yield from source.send_chat(
            "Player-only responses to bot commands for user {} are {}".format(
                username, state_desc))
        return

    state_val = 1 if state == "on" else 0

    if int(user_data["player_only"]) == state_val:
        raise BotCommandException("Player-only responses for user {} already "
                "set to {}".format(username, state))

    mgr.bot_db.set_user_field(username, "player_only", state_val)

    yield from source.send_chat(
        "Player-only responses to bot commands for user {} set to {}".format(
            username, state))

# Fields names and default values in the WebTiles DB.
db_tables = {
        "webtiles_users" : [
            {"name" : "username",
             "type" : "text",
             "primary" : True,
            },
            {"name" : "subscription",
             "type" : "integer",
             "default" : 0
            },
            {"name" : "player_only",
             "type" : "integer",
             "default" : 0
            },
        ],
}

# WebTiles bot commands
bot_commands = {
    "bothelp" : {
        "unlogged" : True,
        "function" : bot_help_command,
    },
    "status" : {
        "require_admin" : True,
        "function" : bot_status_command,
    },
    "subscribe" : {
        "disallow_single_user_mode" : True,
        "function" : bot_subscribe_command,
    },
    "unsubscribe" : {
        "disallow_single_user_mode" : True,
        "function" : bot_unsubscribe_command,
    },
    "player-only" : {
        "require_source_user" : True,
        "args" : [
            {
                "pattern" : r"(on|off)$",
                "description" : "on|off",
                "required" : False
            } ],
        "function" : bot_player_only_command,
    },
}
