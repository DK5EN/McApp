"""CommandHandler assembly and COMMANDS registry."""

from .admin_commands import AdminCommandsMixin
from .constants import has_console
from .ctcping import CTCPingMixin
from .data_commands import DataCommandsMixin
from .dedup import DedupMixin
from .response import ResponseMixin
from .routing import RoutingMixin
from .simple_commands import SimpleCommandsMixin
from .topic_beacon import TopicBeaconMixin
from .weather_command import WeatherCommandMixin

# Command registry with handler functions and metadata
COMMANDS = {
    "search": {
        "handler": "handle_search",
        "args": ["call", "days"],
        "format": "!search call:CALL days:N",
        "description": "Search messages by user and timeframe",
    },
    "s": {
        "handler": "handle_search",
        "args": ["call", "days"],
        "format": "!search call:CALL days:N",
        "description": "Search messages by user and timeframe",
    },
    "stats": {
        "handler": "handle_stats",
        "args": ["hours"],
        "format": "!stats hours:N",
        "description": "Show message statistics for last N hours",
    },
    "mheard": {
        "handler": "handle_mheard",
        "args": ["limit"],
        "format": "!mheard type:all|msg|pos limit:N",
        "description": "Show recently heard stations",
    },
    "mh": {
        "handler": "handle_mheard",
        "args": ["limit"],
        "format": "!mheard type:all|msg|pos limit:N",
        "description": "Show recently heard stations",
    },
    "pos": {
        "handler": "handle_position",
        "args": ["call", "days"],
        "format": "!pos call:CALL days:N",
        "description": "Show position data for callsign",
    },
    "dice": {
        "handler": "handle_dice",
        "args": [],
        "format": "!dice",
        "description": "Roll two dice with Mäxchen rules",
    },
    "time": {
        "handler": "handle_time",
        "args": [],
        "format": "!time",
        "description": "Show nodes time and date",
    },
    "wx": {
        "handler": "handle_weather",
        "args": [],
        "format": "!wx",
        "description": "Show nodes current weather",
    },
    "weather": {
        "handler": "handle_weather",
        "args": [],
        "format": "!weather",
        "description": "Show nodes current weather",
    },
    "group": {
        "handler": "handle_group_control",
        "args": ["state"],
        "format": "!group on|off",
        "description": "Control group response mode (admin only)",
    },
    "userinfo": {
        "handler": "handle_userinfo",
        "args": [],
        "format": "!userinfo",
        "description": "Show user information",
    },
    "kb": {
        "handler": "handle_kickban",
        "args": ["callsign", "action"],
        "format": "!kb [callsign] [del|list|delall]",
        "description": "Manage blocked callsigns (admin only)",
    },
    "topic": {
        "handler": "handle_topic",
        "args": ["group", "text", "interval"],
        "format": "!topic [group] [text] [interval:minutes] | !topic | !topic delete group",
        "description": "Manage group beacon messages (admin only)",
    },
    "ctcping": {
        "handler": "handle_ctcping",
        "args": ["call", "payload", "repeat"],
        "format": "!ctcping call:Ping-Target payload:25 repeat:3 [target:Remote-Node]",
        "description": "Ping test with roundtrip time measurement",
    },
    "help": {
        "handler": "handle_help",
        "args": [],
        "format": "!help",
        "description": "Show available commands",
    },
}


class CommandHandler(
    RoutingMixin,
    DedupMixin,
    ResponseMixin,
    SimpleCommandsMixin,
    DataCommandsMixin,
    WeatherCommandMixin,
    AdminCommandsMixin,
    CTCPingMixin,
    TopicBeaconMixin,
):
    def __init__(
        self,
        message_router=None,
        storage_handler=None,
        my_callsign="DK0XXX",
        lat=None,
        lon=None,
        stat_name="",
        user_info_text=None,
    ):
        self.blocked_callsigns = set()

        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = my_callsign.upper()
        self.admin_callsign_base = my_callsign.split("-")[0]
        self.lat = lat
        self.lon = lon
        self.stat_name = stat_name
        self.user_info_text = (
            user_info_text or f"{my_callsign} Node | No additional info configured"
        )
        self.group_responses_enabled = False

        # Initialize subsystems
        self._init_topic_beacon()
        self._init_ctcping()
        self._init_dedup()
        self._init_weather()

        # GPS caching is handled centrally in main.py via _cache_gps

        # Subscribe to message types that might contain commands
        if message_router:
            message_router.subscribe("mesh_message", self._message_handler)
            message_router.subscribe("ble_notification", self._message_handler)

        if has_console:
            print(f"CommandHandler: Initialized with {len(COMMANDS)} commands")
            print(f"🐛 CommandHandler: Listening for commands to '{self.my_callsign}'")
            print(f"🐛 CommandHandler: Weather service initialized for {self.lat}/{self.lon}")

    async def run_all_tests(self):
        """Run complete test suite for CommandHandler"""
        from .tests import run_all_tests

        return await run_all_tests(self)


def create_command_handler(
    message_router,
    storage_handler,
    call_sign,
    lat=None,
    lon=None,
    stat_name="",
    user_info_text=None,
):
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(
        message_router, storage_handler, call_sign, lat, lon, stat_name, user_info_text
    )
