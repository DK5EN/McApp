"""AdminCommandsMixin: group control and kickban commands."""

import re

from .constants import has_console


class AdminCommandsMixin:
    """Mixin providing admin command handlers."""

    async def handle_group_control(self, kwargs, requester):
        """Control group response mode (admin only)"""
        if has_console:
            print(f"🔍 handle_group_control called with kwargs={kwargs}, requester='{requester}'")

        if not self._is_admin(requester):
            if has_console:
                print(f"🔍 Admin check failed for '{requester}'")
            return "❌ Admin access required"

        state = kwargs.get("state", "").lower()
        if has_console:
            print(f"🔍 Extracted state: '{state}'")

        if state == "on":
            self.group_responses_enabled = True
            if has_console:
                print("🔍 Set group_responses_enabled = True")
            return "✅ Group responses ENABLED"
        elif state == "off":
            self.group_responses_enabled = False
            if has_console:
                print("🔍 Set group_responses_enabled = False")
            return "✅ Group responses DISABLED"
        else:
            current = "ON" if self.group_responses_enabled else "OFF"
            if has_console:
                print(f"🔍 No valid state, current setting: {current}")
            return f"🔧 Group responses: {current}. Use !group on|off"

    async def handle_kickban(self, kwargs, requester):
        """Manage blocked callsigns"""
        if not self._is_admin(requester):
            return "❌ Admin access required"

        # !kb oder !kb list
        if not kwargs or kwargs.get("callsign") == "list":
            if not self.blocked_callsigns:
                return "📋 Blocklist is empty"
            blocked_list = ", ".join(sorted(self.blocked_callsigns))
            return f"🚫 Blocked: {blocked_list}"

        # !kb delall
        if kwargs.get("callsign") == "delall":
            count = len(self.blocked_callsigns)
            self.blocked_callsigns.clear()
            return f"✅ Cleared {count} blocked callsign(s)"

        callsign = kwargs.get("callsign", "").upper()
        action = kwargs.get("action", "").lower()

        # Validate callsign
        if not re.match(r"^[A-Z]{1,2}[0-9][A-Z]{1,3}(-\d{1,2})?$", callsign):
            return "❌ Invalid callsign format"

        # Prevent self-blocking
        if callsign.split("-")[0] == self.admin_callsign_base:
            return "❌ Cannot block own callsign"

        # !kb CALL del
        if action == "del":
            if callsign in self.blocked_callsigns:
                self.blocked_callsigns.remove(callsign)
                return f"✅ {callsign} unblocked"
            else:
                return f"ℹ️ {callsign} was not blocked"

        # !kb CALL (add to blocklist)
        if callsign in self.blocked_callsigns:
            return f"ℹ️ {callsign} already blocked"

        self.blocked_callsigns.add(callsign)
        return f"🚫 {callsign} blocked"
