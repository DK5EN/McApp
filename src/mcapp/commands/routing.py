"""RoutingMixin: message handling, command parsing, execution routing."""

import re

from ..logging_setup import get_logger
from .constants import (
    CALLSIGN_TARGET_PATTERN,
    COMMAND_THROTTLING,
    DEFAULT_THROTTLE_TIMEOUT,
    has_console,
)
from .shadow import compare_normalize, compare_routing, normalize_unified

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

        # NEW: Use simplified reception logic
        should_execute, target_type = self._should_execute_command(src, dst, msg_text)

        # Shadow: compare with v2 routing logic
        shadow_result = self._should_execute_command_v2(src, dst, msg_text)
        compare_routing(
            (should_execute, target_type), shadow_result, src, dst, msg_text
        )

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
        """Normalize command data with uppercase conversion"""
        src_raw = message_data.get("src", "UNKNOWN")
        src = src_raw.split(",")[0].strip().upper() if "," in src_raw else src_raw.strip().upper()

        dst = message_data.get("dst", "").strip().upper()
        msg = message_data.get("msg", "").strip()

        # Strip MeshCom message ID suffix ({NNN) before any routing decisions
        msg = re.sub(r"\{\d+$", "", msg).strip()

        result = {"src": src, "dst": dst, "msg": msg, "original": message_data}
        # Shadow: compare with unified normalizer
        shadow_result = normalize_unified(message_data, context="command")
        compare_normalize(result, shadow_result, "command", message_data)
        return result

    def _should_execute_command(self, src, dst, msg):
        """Simplified reception logic with P2P support"""
        src = src.upper()
        dst = dst.upper()
        msg = msg.upper()

        if has_console:
            print(f"🔍 Command execution check: src='{src}', dst='{dst}', msg='{msg[:20]}...'")

        if dst in ["*", "ALL", ""]:
            # Nur eigene Befehle an Broadcast-Destinationen ausführen
            if src == self.my_callsign:
                if has_console:
                    print(f"🔍 → Own broadcast command '{dst}' - EXECUTE")
                return True, "group"
            else:
                if has_console:
                    print(f"🔍 → Remote broadcast command '{dst}' from {src} - NO EXECUTION")
                return False, None

        target = self.extract_target_callsign(msg)

        if src == self.my_callsign:
            # Our own commands - existing logic remains the same
            if not target:
                if has_console:
                    print("🔍 → Our command without target - EXECUTE (local intent)")
                if dst == self.my_callsign:
                    return True, "direct"
                elif self.is_group(dst):
                    return True, "group"
                else:
                    return True, "direct"
            elif target == self.my_callsign:
                if has_console:
                    print("🔍 → Our command with our target - EXECUTE (local execution)")
                if dst == self.my_callsign:
                    return True, "direct"
                elif self.is_group(dst):
                    return True, "group"
                else:
                    return True, "direct"
            else:
                if has_console:
                    print(
                        f"🔍 → Our command with remote"
                        f" target '{target}' - NO EXECUTION"
                        f" (remote intent)"
                    )
                return False, None

        # === INCOMING COMMANDS ===

        # Direct P2P message to us
        if dst == self.my_callsign:
            if not target:
                # Personal message without target → execute (P2P intent)
                if has_console:
                    print("🔍 → P2P message without target - EXECUTE (personal chat)")
                return True, "direct"
            elif target == self.my_callsign:
                # Personal message with our target → execute
                if has_console:
                    print("🔍 → P2P message with our target - EXECUTE")
                return True, "direct"
            else:
                # Personal message with other target → don't execute
                if has_console:
                    print(f"🔍 → P2P message with other target '{target}' - NO EXECUTION")
                return False, None

        # Group message → requires our callsign as target
        if self.is_group(dst):
            if target != self.my_callsign:
                if has_console:
                    print("🔍 → Group message without our target - NO EXECUTION")
                return False, None

            # Group message with our target → check permissions
            execute = self.group_responses_enabled or self._is_admin(src)
            reason = (
                "Groups ON"
                if self.group_responses_enabled
                else "Admin override"
                if self._is_admin(src)
                else "Groups OFF"
            )
            if has_console:
                print(
                    f"🔍 → Group '{dst}' with our"
                    f" target - "
                    f"{'EXECUTE' if execute else 'NO EXECUTION'}"
                    f" ({reason})"
                )

            if execute:
                return True, "group"
            else:
                return False, None

        if has_console:
            print("🔍 → No match - NO EXECUTION")
        return False, None

    def _should_execute_command_v2(self, src, dst, msg):
        """Flat routing logic with early returns (shadow candidate for v1 replacement)."""
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
        """Extract target callsign from command message.

        Priority:
        1. Explicit target: parameter (scanned anywhere in message)
        2. Fallback: first standalone callsign (right-to-left, skip key:value)

        Commands that never have targets: GROUP, KB, TOPIC
        """
        if not msg or not msg.startswith("!"):
            return None

        msg_upper = msg.upper().strip()
        parts = msg_upper.split()

        if len(parts) < 2:
            return None

        command = parts[0][1:]  # Remove ! prefix

        # Commands that NEVER have targets (admin-only, local state)
        if command in ["GROUP", "KB", "TOPIC"]:
            return None

        # Priority 1: Explicit target:CALLSIGN parameter (scanned anywhere)
        for part in parts[1:]:
            if part.startswith("TARGET:"):
                potential = part[7:]  # Remove 'TARGET:' prefix
                if potential in ["LOCAL", ""]:
                    return None  # Explicit local execution
                if re.match(CALLSIGN_TARGET_PATTERN, potential):
                    return potential
                return None  # Invalid target format

        # Priority 2: Positional fallback (right-to-left, skip key:value pairs)
        for part in reversed(parts[1:]):
            if ":" in part:
                continue  # Skip key:value arguments
            potential = part.strip()
            if re.match(CALLSIGN_TARGET_PATTERN, potential):
                return potential

        return None

    def is_group(self, dst):
        """Check if destination is a group"""
        if not dst:
            return False

        # Special group 'TEST'
        if dst.upper() == "TEST":
            return True

        # Numeric groups: 1-99999
        if dst.isdigit():
            try:
                group_num = int(dst)
                return 1 <= group_num <= 99999
            except ValueError:
                return False

        return False

    def _is_admin(self, callsign):
        """Check if callsign is admin (DK5EN with any SID)"""
        if not callsign:
            return False
        base_call = callsign.split("-")[0] if "-" in callsign else callsign
        return base_call.upper() == self.admin_callsign_base.upper()

    def _is_valid_target(self, dst, src):
        """Check if message is for us (callsign) or valid group (1-5 digits or 'TEST')"""
        if has_console:
            print(f"🔍 valid_target dubug {dst}, {src}")

        # Always allow direct messages to our callsign
        if dst.upper() == self.my_callsign.upper():
            if has_console:
                print("🔍 valid_target Ture, callsign")
            return True, "callsign"

        # Check if dst is a valid group format
        is_valid_group = dst == "TEST" or (dst and dst.isdigit() and 1 <= len(dst) <= 5)
        if not is_valid_group:
            if has_console:
                print("🔍 valid_target False, None")
            return False, None

        # Admin always allowed for groups
        if self._is_admin(src):
            if has_console:
                print("🔍 valid_target admin override, True, group")
            return True, "group"

        # Non-admin only allowed if group responses are enabled
        if self.group_responses_enabled:
            if has_console:
                print("🔍 valid_target group responses enabled, True, group")
            return True, "group"

        if has_console:
            print("🔍 valid_target no match, False, None")
        return False, None

    def parse_command(self, msg_text):
        """Parse command text into command and arguments"""
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
                            # target: is handled by extract_target_callsign routing
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
                                        interval_value = int(interval_part.split(":", 1)[1])
                                        kwargs["interval"] = interval_value
                                    except (ValueError, IndexError):
                                        pass
                                elif len(parts) >= 4 and parts[-1].isdigit():
                                    try:
                                        kwargs["interval"] = int(parts[-1])
                                        if text_parts and text_parts[-1] == parts[-1]:
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
