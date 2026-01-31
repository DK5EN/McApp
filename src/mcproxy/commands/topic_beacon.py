"""TopicBeaconMixin: topic command and beacon lifecycle management."""

import asyncio
from datetime import datetime

from .constants import has_console


class TopicBeaconMixin:
    """Mixin providing topic/beacon management."""

    def _init_topic_beacon(self):
        """Initialize topic/beacon state. Called from CommandHandler.__init__."""
        self.active_topics = {}  # {group: {'text': str, 'interval': int, 'task': asyncio.Task}}
        self.topic_tasks = set()

    async def handle_topic(self, kwargs, requester):
        """Manage group beacon messages"""
        if not self._is_admin(requester):
            return "❌ Admin access required"

        # !topic (show all active topics)
        if not kwargs:
            if not self.active_topics:
                return "📡 No active beacon topics"

            topics_info = []
            for group, info in self.active_topics.items():
                interval = info["interval"]
                text_preview = info["text"][:30] + ("..." if len(info["text"]) > 30 else "")
                topics_info.append(f"Group {group}: '{text_preview}' every {interval}min")

            return f"📡 Active beacons: {' | '.join(topics_info)}"

        # !topic delete GROUP
        if kwargs.get("action") == "delete":
            group = kwargs.get("group")
            if not group:
                return "❌ Group required for delete"

            if not self.is_group(group):
                return "❌ Invalid group format"

            if group not in self.active_topics:
                return f"ℹ️ No beacon active for group {group}"

            await self._stop_topic_beacon(group)
            return f"✅ Beacon stopped for group {group}"

        # !topic GROUP TEXT [interval]
        group = kwargs.get("group")
        text = kwargs.get("text", "")
        interval = kwargs.get("interval", 30)

        if not group:
            return "❌ Group required"

        if not self.is_group(group):
            return "❌ Invalid group format (use digits 1-99999 or TEST)"

        if not text:
            return "❌ Beacon text required"

        if len(text) > 120:
            return "❌ Beacon text too long (max 120 chars)"

        try:
            interval = int(interval)
            if interval < 1 or interval > 1440:
                return "❌ Interval must be between 1 and 1440 minutes"
        except (ValueError, TypeError):
            return "❌ Invalid interval format"

        if group in self.active_topics:
            await self._stop_topic_beacon(group)

        success = await self._start_topic_beacon(group, text, interval)

        if success:
            return (
                f"✅ Beacon started for group"
                f" {group}:"
                f" '{text[:50]}"
                f"{'...' if len(text) > 50 else ''}'"
                f" every {interval}min"
            )
        else:
            return "❌ Failed to start beacon"

    async def _start_topic_beacon(self, group, text, interval_minutes):
        """Start a beacon task for a group"""
        try:
            interval_seconds = (interval_minutes * 60) - 10
            if interval_seconds < 10:
                interval_seconds = 10

            task = asyncio.create_task(self._beacon_loop(group, text, interval_seconds))

            self.active_topics[group] = {
                "text": text,
                "interval": interval_minutes,
                "task": task,
                "started": datetime.now(),
            }

            self.topic_tasks.add(task)

            task.add_done_callback(self.topic_tasks.discard)

            if has_console:
                print(f"📡 Started beacon for group {group}: interval {interval_seconds}s")

            return True

        except Exception as e:
            if has_console:
                print(f"❌ Failed to start beacon for group {group}: {e}")
            return False

    async def _stop_topic_beacon(self, group):
        """Stop a beacon task for a group"""
        if group not in self.active_topics:
            return False

        try:
            topic_info = self.active_topics[group]
            task = topic_info["task"]

            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            del self.active_topics[group]

            if has_console:
                print(f"📡 Stopped beacon for group {group}")

            return True

        except Exception as e:
            if has_console:
                print(f"❌ Failed to stop beacon for group {group}: {e}")
            return False

    async def _beacon_loop(self, group, text, interval_seconds):
        """Beacon loop - sends periodic messages to a group"""
        try:
            while True:
                await asyncio.sleep(interval_seconds)

                if group not in self.active_topics:
                    break

                await self._send_beacon_message(group, text)

                if has_console:
                    print(f"📡 Sent beacon to group {group}: '{text[:30]}...'")

        except asyncio.CancelledError:
            if has_console:
                print(f"📡 Beacon loop cancelled for group {group}")
            raise

        except Exception as e:
            if has_console:
                print(f"❌ Beacon loop error for group {group}: {e}")

            if group in self.active_topics:
                del self.active_topics[group]

    async def _send_beacon_message(self, group, text):
        """Send a beacon message to a group"""
        try:
            if self.message_router:
                beacon_message = {
                    "dst": group,
                    "msg": f"📡 {text}",
                    "src_type": "beacon",
                    "type": "msg",
                }

                await self.message_router.publish("beacon", "udp_message", beacon_message)

        except Exception as e:
            if has_console:
                print(f"❌ Failed to send beacon message to group {group}: {e}")

    async def cleanup_topic_beacons(self):
        """Clean up all running beacon tasks"""
        if has_console:
            print(f"🧹 Cleaning up {len(self.active_topics)} beacon tasks...")

        groups_to_stop = list(self.active_topics.keys())
        for group in groups_to_stop:
            await self._stop_topic_beacon(group)

        remaining_tasks = [task for task in self.topic_tasks if not task.done()]
        if remaining_tasks:
            for task in remaining_tasks:
                task.cancel()

            try:
                await asyncio.gather(*remaining_tasks, return_exceptions=True)
            except Exception:
                pass

        self.topic_tasks.clear()

        if has_console:
            print("✅ All beacon tasks cleaned up")
