"""Shape test for RM1 remote-admin frames (commands and replies).

RM1 frames are machine chatter between McApp's Node Admin and a managed node:

    command  RM1 <ctr> <cmd>[ <args>] <tag16>
    reply    RM1 <ctr> ok|err <result> <tag16>

They are stored (the Node Admin reply hook needs the row) but must never be
shown as chat, counted as unread, or pushed. Pure and dependency-free so the
storage layer, the SSE verdict and the push gate can all import it.

The Extern-UDP copy may carry the firmware ack-request suffix ``{NNN`` (strict,
no closing brace); the BLE copy does not. Both shapes are accepted.

`RM1_HIDDEN_GLOBS` is the SQLite GLOB form of the same predicate. GLOB has no
repetition operator, so it cannot say "ctr is a number without leading zero" or
"the middle is a command or ok/err reply"; it is therefore a deliberate
SUPERSET of `is_rm1_frame`: ``RM1 <digit>...`` ending in exactly 16 lower-hex
characters, optionally followed by ``{<digits>``. It still anchors the whole
payload and requires the 16-hex tag at the end, so a human message that merely
starts with "RM1" stays visible. Every frame `is_rm1_frame` accepts matches a
glob (pinned by the suite); the glob may additionally hide an odd text that
only looks like a frame, which is the safe direction for a hide rule.
"""

from __future__ import annotations

import re
from typing import Final

_RM1_RE: Final = re.compile(
    r"RM1 (?:0|[1-9][0-9]{0,9}) [^ \n][^\n]* [0-9a-f]{16}(?:\{[0-9]+)?",
)

_HEX16: Final = "[0-9a-f]" * 16

RM1_HIDDEN_GLOBS: Final[tuple[str, ...]] = (
    f"RM1 [0-9]* {_HEX16}",
    f"RM1 [0-9]* {_HEX16}[{{][0-9]*",
)


def is_rm1_frame(text: object) -> bool:
    """True when ``text`` has the shape of an RM1 command or reply.

    Shape only, not authenticated. Non-str input is never a frame.
    """
    return isinstance(text, str) and _RM1_RE.fullmatch(text) is not None
