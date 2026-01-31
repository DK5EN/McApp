import sys

VERSION = "v0.61.0"

# Response chunking constants
MAX_RESPONSE_LENGTH = 140  # Maximum characters per message chunk
MAX_CHUNKS = 3  # Maximum number of response chunks
MSG_DELAY = 12

DEFAULT_THROTTLE_TIMEOUT = 5 * 60  # 5 minutes default

COMMAND_THROTTLING = {
    "dice": 5,  # 5 seconds for dice games
    "time": 5,  # 5 seconds for time requests
    "group": 5,
    "kb": 5,
    "topic": 5,
    # All other commands use default 5 minutes
}

has_console = sys.stdout.isatty()
