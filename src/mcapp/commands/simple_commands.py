"""SimpleCommandsMixin: dice, time, help, userinfo, position commands."""

import json
import random
import time
from datetime import datetime


class SimpleCommandsMixin:
    """Mixin providing simple command handlers."""

    async def handle_dice(self, kwargs, requester):
        """Roll two dice with Mäxchen rules"""
        die1 = random.randint(1, 6)
        die2 = random.randint(1, 6)

        sorted_value, description = self._calculate_maexchen_value(die1, die2)

        return f"🎲 {requester}: [{die1}][{die2}] → {sorted_value} {description}"

    def _calculate_maexchen_value(self, die1, die2):
        """Calculate Mäxchen value and description according to rules"""
        dice = sorted([die1, die2], reverse=True)
        higher, lower = dice[0], dice[1]

        # Special case: Mäxchen (2,1)
        if set([die1, die2]) == {2, 1}:
            return "21", "(Mäxchen! 🏆)"

        # Double values (Pasch)
        if die1 == die2:
            pasch_names = {
                6: "Sechser-Pasch",
                5: "Fünfer-Pasch",
                4: "Vierer-Pasch",
                3: "Dreier-Pasch",
                2: "Zweier-Pasch",
                1: "Einser-Pasch",
            }
            return f"{die1}{die2}", f"({pasch_names[die1]})"

        # Regular values (higher die first)
        value = f"{higher}{lower}"
        return value, ""

    async def handle_time(self, kwargs, requester):
        """Show current time and date"""
        now = datetime.now()

        date_str = now.strftime("%d.%m.%Y")
        time_str = now.strftime("%H:%M:%S")
        weekday = now.strftime("%A")

        weekday_german = {
            "Monday": "Montag",
            "Tuesday": "Dienstag",
            "Wednesday": "Mittwoch",
            "Thursday": "Donnerstag",
            "Friday": "Freitag",
            "Saturday": "Samstag",
            "Sunday": "Sonntag",
        }

        weekday_de = weekday_german.get(weekday, weekday)

        return f"🕐 {time_str} Uhr, {weekday_de}, {date_str}"

    async def handle_help(self, kwargs, requester):
        """Show available commands"""
        response = "📋 Available commands: "

        search_cmds = ["!search user:CALL days:7", "!pos call:CALL"]
        stats_cmds = ["!stats 24", "!mheard 5"]
        weather_cmds = ["!wx"]
        fun_cmds = ["!dice", "!time"]

        response += "Search: " + ", ".join(search_cmds) + " | "
        response += "Stats: " + ", ".join(stats_cmds) + " | "
        response += "Weather: " + ", ".join(weather_cmds) + " | "
        response += "Fun: " + ", ".join(fun_cmds)

        return response

    async def handle_userinfo(self, kwargs, requester):
        """Show user information from config"""
        try:
            user_info = getattr(self, "user_info_text", None)

            if not user_info:
                return "❌ User info not configured"

            return f"{user_info}"

        except Exception as e:
            return f"❌ Error retrieving user info: {str(e)[:30]}"

    async def handle_position(self, kwargs, requester):
        """Show position data for callsign"""
        callsign = kwargs.get("call", "").upper()
        days = int(kwargs.get("days", 7))

        if not callsign:
            return "❌ Callsign required (call:CALLSIGN)"

        if not self.storage_handler:
            return "❌ Message storage not available"

        if hasattr(self.storage_handler, 'get_positions'):
            positions = await self.storage_handler.get_positions(callsign, days)
        else:
            cutoff_time = time.time() - (days * 24 * 60 * 60)

            positions = []
            for item in reversed(list(self.storage_handler.message_store)):
                try:
                    raw_data = json.loads(item["raw"])
                    timestamp = raw_data.get("timestamp", 0)

                    if timestamp < cutoff_time * 1000:
                        continue

                    if raw_data.get("type") != "pos":
                        continue

                    src = raw_data.get("src", "")
                    if callsign not in src.upper():
                        continue

                    lat = raw_data.get("lat")
                    lon = raw_data.get("long")

                    if lat and lon:
                        time_str = time.strftime("%H:%M", time.localtime(timestamp / 1000))
                        positions.append(
                            {"lat": lat, "lon": lon, "time": time_str, "timestamp": timestamp}
                        )

                except (json.JSONDecodeError, KeyError):
                    continue

        if not positions:
            return f"🔍 No position data for {callsign} in last {days} day(s)"

        latest = max(positions, key=lambda x: x["timestamp"])

        return (
            f"🔍 {callsign} position:"
            f" {latest['lat']:.4f},"
            f"{latest['lon']:.4f}"
            f" (last seen {latest['time']})"
        )
