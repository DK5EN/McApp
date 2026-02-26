"""RoutingMixin: message handling, command parsing, execution routing."""

import re

from ..logging_setup import get_logger
from .constants import (
    COMMAND_THROTTLING,
    DEFAULT_THROTTLE_TIMEOUT,
    has_console,
)
from .parsing import extract_target_callsign, is_group, parse_command_v2
from .shadow import compare_parse_command, normalize_unified

logger = get_logger(__name__)


class RoutingMixin:
    """Mixin providing message routing, command parsing, and execution logic."""

    async def _message_handler(self, routed_message):
        """Handle incoming messages and check for commands"""
        message_data = routed_message["data"]
        src_type = message_data.get("src_type")

        logger.debug(
            "CommandHandler._message_handler: source=%s type=%s "
            "src_type=%r src=%s dst=%s msg=%.30s",
            routed_message.get('source'), routed_message.get('type'),
            src_type, message_data.get('src'), message_data.get('dst'),
            message_data.get('msg', ''),
        )

        if "msg" not in message_data:
            return

        msg_text = message_data.get("msg", "")

        if self._is_echo_message(msg_text):
            await self._handle_echo_message(message_data)
            return

        if self._is_ack_message(msg_text):
            await self._handle_ack_message(message_data)
            return

        if not msg_text or not msg_text.startswith("!"):
            return

        msg_id = message_data.get("msg_id")
        if self._is_duplicate_msg_id(msg_id):
            if has_console:
                print(f"🔄 CommandHandler: Duplicate msg_id {msg_id}, ignoring silently")
            return

        # EARLY NORMALIZATION using the same pattern as MessageRouter
        normalized = self.normalize_command_data(message_data)
        src = normalized["src"]
        dst = normalized["dst"]
        msg_text = normalized["msg"]

        # Skip our own messages echoed back from the mesh
        # (these arrive as mesh_message from UDP with our callsign as src)
        if src == self.my_callsign and routed_message.get('source') == 'udp':
            if has_console:
                print(f"📋 CommandHandler: Skipping own echo from mesh: {msg_text[:30]}")
            return

        if has_console:
            print(f"📋 CommandHandler: Checking command '{msg_text}' from {src} to {dst}")

        should_execute, target_type = self._should_execute_command(src, dst, msg_text)

        if not should_execute:
            if has_console:
                print("📋 CommandHandler: Command execution denied")
            return

        if has_console:
            admin_status = " (ADMIN)" if self._is_admin(src) else ""
            group_status = " [Groups: ON]" if self.group_responses_enabled else " [Groups: OFF]"
            print(f"📋 CommandHandler: Executing {target_type} command{admin_status}{group_status}")

        if target_type == "direct":
            if src == self.my_callsign:
                # Outgoing: Antwort an Chat-Partner
                response_target = dst
            else:
                # Incoming: Antwort an Sender
                response_target = src
        else:
            # Group: Antwort an Gruppe
            response_target = dst

        if has_console:
            print(f"📋 CommandHandler: Response will be sent to {response_target} ({target_type})")

        # Check if user is blocked
        if self._is_user_blocked(src):
            if has_console:
                print(f"🔴 CommandHandler: User {src} is blocked due to abuse")
            if src not in self.block_notifications_sent:
                self.block_notifications_sent.add(src)
                await self.send_response(
                    "🚫 Temporarily in timeout due to repeated invalid commands",
                    response_target,
                    src_type,
                )
            return

        # Check throttling
        content_hash = self._get_content_hash(src, msg_text, dst)
        if self._is_throttled(content_hash):
            if has_console:
                print(f"⏳ CommandHandler: THROTTLED - {src} command '{msg_text}'")
            await self.send_response(
                "⏳ Command throttled. Same command allowed once per 5min",
                response_target,
                src_type,
            )
            return

        # Parse and execute command
        try:
            cmd_result = self.parse_command(msg_text)
            if cmd_result:
                cmd, kwargs = cmd_result

                if self._is_throttled(content_hash, cmd):
                    timeout_text = (
                        f"{COMMAND_THROTTLING.get(cmd, DEFAULT_THROTTLE_TIMEOUT // 60)}min"
                    )
                    await self.send_response(
                        f"⏳ !{cmd} throttled. Try again in {timeout_text}",
                        response_target,
                        src_type,
                    )
                    return

                response = await self.execute_command(cmd, kwargs, src)

                self._mark_msg_id_processed(msg_id)
                self._mark_content_processed(content_hash, cmd)

                await self.send_response(response, response_target, src_type)

            else:
                self._mark_msg_id_processed(msg_id)
                if has_console:
                    print(f"📋 CommandHandler: Unknown command '{msg_text}' from {src} (discarded)")

        except Exception as e:
            error_type = type(e).__name__
            if has_console:
                print(f"CommandHandler ERROR ({error_type}): {e}")

            self._track_failed_attempt(src)
            self._mark_msg_id_processed(msg_id)

            if "timeout" in str(e).lower():
                await self.send_response(
                    "❌ Command timeout. Try again later", response_target, src_type
                )
            elif "weather" in str(e).lower():
                await self.send_response(
                    "❌ Weather service temporarily unavailable", response_target, src_type
                )
            else:
                await self.send_response(
                    f"❌ Command failed: {str(e)[:50]}", response_target, src_type
                )

    def normalize_command_data(self, message_data):
        """Normalize command data with uppercase conversion."""
        return normalize_unified(message_data, context="command")

    def _should_execute_command(self, src, dst, msg):
        """Flat routing logic with early returns."""
        src = src.upper()
        dst = dst.upper()
        msg = msg.upper()
        target = self.extract_target_callsign(msg)
        is_own = src == self.my_callsign

        def _target_type(dst_val):
            """Return 'group' for group destinations, 'direct' otherwise."""
            return "group" if self.is_group(dst_val) else "direct"

        # --- Broadcast destinations ---
        if dst in ("*", "ALL", ""):
            if is_own:
                return True, "group"
            return False, None

        # --- Our own commands ---
        if is_own:
            # Remote intent: target is someone else
            if target and target != self.my_callsign:
                return False, None
            # Local intent: no target or target is us
            return True, _target_type(dst)

        # --- Incoming: direct P2P to us ---
        if dst == self.my_callsign:
            if target and target != self.my_callsign:
                return False, None
            return True, "direct"

        # --- Incoming: group message ---
        if self.is_group(dst):
            if target != self.my_callsign:
                return False, None
            if self.group_responses_enabled or self._is_admin(src):
                return True, "group"
            return False, None

        # --- No match ---
        return False, None

    def extract_target_callsign(self, msg):
        """Delegate to shared pure function."""
        return extract_target_callsign(msg)

    def is_group(self, dst):
        """Delegate to shared pure function."""
        return is_group(dst)

    def _is_admin(self, callsign):
        """Check if callsign is admin (DK5EN with any SID)"""
        if not callsign:
            return False
        base_call = callsign.split("-")[0] if "-" in callsign else callsign
        return base_call.upper() == self.admin_callsign_base.upper()

    def parse_command(self, msg_text):
        """Parse command — shadow mode: runs v1 + v2, compares, returns v1."""
        v1_result = self._parse_command_v1(msg_text)
        v2_result = parse_command_v2(msg_text)
        compare_parse_command(v1_result, v2_result, msg_text)
        return v1_result

    def _parse_command_v1(self, msg_text):
        """Original parse_command (v1) — kept for shadow comparison."""
        from .handler import COMMANDS

        if not msg_text.startswith("!"):
            return None

        parts = msg_text[1:].split()
        if not parts:
            return None

        cmd = parts[0].lower()

        if cmd not in COMMANDS:
            return None

        # Parse key:value pairs
        kwargs = {}

        # Special handling for wx/weather: TEXT: captures everything after it
        if cmd in ["wx", "weather"]:
            remaining = msg_text[len(parts[0]):].strip()
            if remaining:
                text_match = re.search(r'TEXT:(.*)', remaining, re.IGNORECASE)
                if text_match:
                    kwargs["text"] = text_match.group(1).strip()
            return cmd, kwargs

        for part in parts[1:]:
            if ":" in part:
                key, value = part.split(":", 1)
                kwargs[key.lower()] = value
            else:
                # Handle positional arguments for simple commands
                if cmd in ["s", "search"] and not kwargs:
                    kwargs["call"] = part

                elif cmd == "pos" and not kwargs:
                    kwargs["call"] = part

                elif cmd == "stats" and not kwargs:
                    try:
                        kwargs["hours"] = int(part)
                    except ValueError:
                        pass

                elif cmd in ["mh", "mheard"] and not kwargs:
                    try:
                        kwargs["limit"] = int(part)
                    except ValueError:
                        if part.lower() in ["msg", "pos", "all"]:
                            kwargs["type"] = part.lower()
                        else:
                            pass

                elif cmd == "group" and not kwargs:
                    kwargs["state"] = part

                elif cmd == "ctcping" and not kwargs:
                    for part in parts[1:]:
                        if ":" in part:
                            key, value = part.split(":", 1)
                            key = key.lower()
                            if key == "call":
                                kwargs["call"] = value.upper()
                            elif key == "payload":
                                kwargs["payload"] = value
                            elif key == "repeat":
                                kwargs["repeat"] = value

                elif cmd == "topic" and not kwargs:
                    if len(parts) >= 2:
                        if parts[1].upper() == "DELETE" and len(parts) >= 3:
                            kwargs["action"] = "delete"
                            kwargs["group"] = parts[2].upper()
                        else:
                            kwargs["group"] = parts[1].upper()

                            if len(parts) >= 3:
                                text_parts = []
                                interval_part = None

                                for i, part in enumerate(parts[2:], 2):
                                    if ":" in part and part.startswith("interval:"):
                                        interval_part = part
                                        break
                                    else:
                                        text_parts.append(parts[i])

                                if text_parts:
                                    kwargs["text"] = " ".join(text_parts)

                                if interval_part:
                                    try:
                                        interval_value = int(
                                            interval_part.split(":", 1)[1]
                                        )
                                        kwargs["interval"] = interval_value
                                    except (ValueError, IndexError):
                                        pass
                                elif len(parts) >= 4 and parts[-1].isdigit():
                                    try:
                                        kwargs["interval"] = int(parts[-1])
                                        if (
                                            text_parts
                                            and text_parts[-1] == parts[-1]
                                        ):
                                            text_parts = text_parts[:-1]
                                            kwargs["text"] = (
                                                " ".join(text_parts)
                                                if text_parts
                                                else kwargs.get("text", "")
                                            )
                                    except ValueError:
                                        pass

                elif cmd == "kb" and not kwargs:
                    if len(parts) >= 2:
                        first_arg = parts[1].upper()

                        if first_arg in ["LIST", "DELALL"]:
                            kwargs["callsign"] = first_arg.lower()
                        else:
                            kwargs["callsign"] = first_arg

                            if len(parts) >= 3:
                                second_arg = parts[2].upper()
                                if second_arg == "DEL":
                                    kwargs["action"] = "del"

        return cmd, kwargs

    async def execute_command(self, cmd, kwargs, requester):
        """Execute a command and return response"""
        from .handler import COMMANDS

        if cmd not in COMMANDS:
            return "❌ Unknown command"

        handler_name = COMMANDS[cmd]["handler"]
        handler = getattr(self, handler_name, None)

        if not handler:
            return f"❌ Handler {handler_name} not implemented"

        try:
            return await handler(kwargs, requester)
        except Exception as e:
            return f"❌ Command error: {str(e)[:50]}"
