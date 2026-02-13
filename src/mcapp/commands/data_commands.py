"""DataCommandsMixin: search, stats, mheard commands."""

import time

from mcapp import __version__


class DataCommandsMixin:
    """Mixin providing data query command handlers."""

    async def handle_search(self, kwargs, requester):
        """Search messages by user and timeframe - show
        summary with counts, last seen, and destinations"""
        user = kwargs.get("call", "*")
        days = int(kwargs.get("days", 1))

        if not self.storage_handler:
            return "❌ Message storage not available"

        # Determine search pattern
        if user != "*" and "-" not in user:
            search_pattern = user.upper() + "-"
            search_type = "prefix"
            display_call = user.upper() + "-*"
        elif user != "*":
            search_pattern = user.upper()
            search_type = "exact"
            display_call = user.upper()
        else:
            search_pattern = "*"
            search_type = "all"
            display_call = "*"

        msg_count = 0
        pos_count = 0
        last_msg_time = None
        last_pos_time = None
        destinations = set()
        sids_activity = {}

        raw_messages = await self.storage_handler.search_messages(
            user, days, search_type,
        )

        for raw_data in raw_messages:
            try:
                timestamp = raw_data.get("timestamp", 0)
                src = raw_data.get("src", "")
                msg_type = raw_data.get("type", "")
                dst = raw_data.get("dst", "")

                matched_callsigns = []
                if search_type == "all":
                    matched_callsigns = [src.split(",")[0]]
                elif search_type == "prefix":
                    src_calls = [call.strip().upper() for call in src.split(",")]
                    matched_callsigns = [
                        call for call in src_calls if call.startswith(search_pattern)
                    ]
                    if not matched_callsigns:
                        continue

                elif search_type == "exact":
                    if search_pattern not in src.upper():
                        continue
                    matched_callsigns = [search_pattern]
                if search_type == "prefix":
                    for callsign in matched_callsigns:
                        if "-" in callsign:
                            sid = callsign.split("-")[1]
                            if sid not in sids_activity or timestamp > sids_activity[sid]:
                                sids_activity[sid] = timestamp

                if msg_type == "msg":
                    msg_count += 1
                    if last_msg_time is None or timestamp > last_msg_time:
                        last_msg_time = timestamp

                    if dst and dst.isdigit():
                        destinations.add(dst)

                elif msg_type == "pos":
                    pos_count += 1
                    if last_pos_time is None or timestamp > last_pos_time:
                        last_pos_time = timestamp

            except (KeyError, TypeError):
                continue

        if msg_count == 0 and pos_count == 0:
            return f"🔍 No activity for {display_call} in last {days} day(s)"

        response = f"🔍 {display_call} ({days}d): "

        if msg_count > 0:
            last_msg_str = time.strftime("%H:%M", time.localtime(last_msg_time / 1000))
            response += f"{msg_count} msg (last {last_msg_str})"

        if msg_count > 0 and pos_count > 0:
            response += " / "

        if pos_count > 0:
            last_pos_str = time.strftime("%H:%M", time.localtime(last_pos_time / 1000))
            response += f"{pos_count} pos (last {last_pos_str})"

        if search_type == "prefix" and sids_activity:
            sorted_sids = sorted(sids_activity.items(), key=lambda x: x[1], reverse=True)
            sid_info = []
            for sid, timestamp in sorted_sids:
                last_time = time.strftime("%H:%M", time.localtime(timestamp / 1000))
                sid_info.append(f"-{sid} @{last_time}")
            response += f" / SIDs: {', '.join(sid_info)}"

        if destinations:
            sorted_destinations = sorted(destinations, key=int)
            response += f" / Groups: {','.join(sorted_destinations)}"

        return response

    async def handle_stats(self, kwargs, requester):
        """Show message statistics"""
        hours = int(kwargs.get("hours", 24))

        if not self.storage_handler:
            return "❌ Message storage not available"

        stats = await self.storage_handler.get_stats(hours)
        msg_count = stats["msg_count"]
        pos_count = stats["pos_count"]
        users = stats["users"]

        total = msg_count + pos_count
        avg_per_hour = round(total / max(hours, 1), 1)

        response = f"📊 Stats (last {hours}h): "
        response += f"Messages: {msg_count}, "
        response += f"Positions: {pos_count}, "
        response += f"Total: {total} ({avg_per_hour}/h), "
        response += f"Active stations: {len(users)}, "
        response += f"McApp v{__version__}"

        return response

    async def handle_mheard(self, kwargs, requester):
        """Show recently heard stations with optional type filtering"""
        limit = int(kwargs.get("limit", 5))
        msg_type = kwargs.get("type", "all").lower()

        if not self.storage_handler:
            return "❌ Message storage not available"

        stations = await self.storage_handler.get_mheard_stations(limit, msg_type)

        lines = []

        if msg_type in ["all", "msg"]:
            msg_stations = [
                (call, data["msg_count"], data["last_msg"])
                for call, data in stations.items()
                if data["msg_count"] > 0
            ]
            if msg_stations:
                msg_stations.sort(key=lambda x: x[2], reverse=True)
                msg_entries = [
                    f"{call} @{time.strftime('%H:%M', time.localtime(ts / 1000))} ({count})"
                    for call, count, ts in msg_stations[:limit]
                ]
                lines.append("📻 MH: 💬 " + " | ".join(msg_entries))

        if msg_type in ["all", "pos"]:
            pos_stations = [
                (call, data["pos_count"], data["last_pos"])
                for call, data in stations.items()
                if data["pos_count"] > 0
            ]
            if pos_stations:
                pos_stations.sort(key=lambda x: x[2], reverse=True)
                pos_entries = [
                    f"{call} @{time.strftime('%H:%M', time.localtime(ts / 1000))} ({count})"
                    for call, count, ts in pos_stations[:limit]
                ]
                lines.append("      📍 " + " | ".join(pos_entries))

        if not lines:
            return "📻 No activity found"

        if len(lines) == 1:
            return lines[0]
        else:
            line1 = lines[0]
            padding_needed = max(0, 138 - len(line1.encode("utf-8")))
            return line1 + " " * padding_needed + ", " + lines[1]
