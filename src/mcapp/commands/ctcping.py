"""CTCPingMixin: all ping/ack/echo methods."""

import asyncio
import re
import time
from typing import Optional

from .constants import has_console


class CTCPingMixin:
    """Mixin providing CTC ping test functionality."""

    def _init_ctcping(self):
        """Initialize CTC ping state. Called from CommandHandler.__init__."""
        self.active_pings = {}  # {ping_id: PingTest}
        self.ping_tests = {}
        self.ping_timeout = 30.0  # 30 seconds per ping

    def _is_ack_message(self, msg: str) -> bool:
        """Check if message is an ACK with :ackXXX pattern"""
        if not msg:
            return False
        pattern = r"\s+:ack\d{3}$"
        result = bool(re.search(pattern, msg))
        return result

    def _is_echo_message(self, msg: str) -> bool:
        """Check if message is an echo with {xxx} suffix"""
        if not msg:
            return False
        pattern = r"\{\d{3}$"  # Exactly 3 digits after {
        result = bool(re.search(pattern, msg))
        return result

    def _is_ping_message(self, msg: str) -> bool:
        """Check if message looks like a ping test message (not the 'started' message)"""
        if not msg:
            return False

        msg_lower = msg.lower()

        has_sequence = bool(re.search(r"ping test \d+/\d+", msg_lower))
        has_measurement = any(
            term in msg_lower
            for term in [
                "mea",
                "measure",
                "roundtrip",
            ]
        )

        return has_sequence and has_measurement

    def _extract_sequence_info(self, msg: str) -> Optional[str]:
        """Extract sequence info from ping message"""
        match = re.search(r"ping test (\d+)/(\d+)", msg.lower())
        if match:
            current = match.group(1)
            total = match.group(2)
            return f"{current}/{total}"
        return None

    def _find_test_id_for_target(self, target: str) -> Optional[str]:
        """Find active test ID for target"""
        if has_console:
            print(f"🔍 Looking for test with target='{target}'")
            print(f"🔍 Available tests: {list(self.ping_tests.keys())}")
            for tid, info in self.ping_tests.items():
                print(f"🔍   Test {tid}: target='{info['target']}', status='{info['status']}'")

        for test_id, test_info in self.ping_tests.items():
            if test_info["target"] == target and test_info["status"] == "running":
                return test_id

        if has_console:
            print(f"🔍 No matching test found for target '{target}'")
        return None

    def get_active_pings_info(self) -> str:
        """Get info about currently active pings (for debugging)"""
        if not self.active_pings:
            return "No active pings"

        ping_info = []
        for msg_id, info in self.active_pings.items():
            target = info["target"]
            status = info["status"]
            elapsed = time.time() - info["sent_time"]
            seq_info = info.get("sequence_info", "")

            seq_text = f" {seq_info}" if seq_info else ""
            ping_info.append(f"ID:{msg_id}{seq_text} → {target} ({status}, {elapsed:.1f}s)")

        return f"Active pings: {' | '.join(ping_info)}"

    async def _handle_echo_message(self, message_data: dict):
        """Handle echo message and start tracking for ACK"""
        try:
            src = message_data.get("src", "").upper()
            dst = message_data.get("dst", "").upper()
            msg = message_data.get("msg", "")

            if has_console:
                print(f"🔍 Echo processing: src={src}, dst={dst}, msg='{msg[:30]}...'")

            match = re.search(r"\{(\d{3})$", msg)
            if not match:
                if has_console:
                    print("🔍 No message ID found in echo")
                return

            message_id = match.group(1)
            original_msg = msg[:-4]

            if has_console:
                print(f"🔍 Echo ID: {message_id}, Original: '{original_msg}'")

            if src != self.my_callsign:
                if has_console:
                    print(f"🔍 Echo not from us ({src} != {self.my_callsign})")
                return

            is_ping = self._is_ping_message(original_msg)
            if has_console:
                print(f"🔍 Is ping message: {is_ping}")
            if not is_ping:
                return

            sequence_info = self._extract_sequence_info(original_msg)
            test_id = self._find_test_id_for_target(dst)

            if has_console:
                print(f"🔍 Sequence: {sequence_info}, Test ID: {test_id}")
                print(
                    "🔍 Available tests:"
                    f" {list(self.ping_tests.keys()) if hasattr(self, 'ping_tests') else 'None'}"
                )

            # Use actual send time if available, fall back to echo receipt time
            sent_time = time.time()
            if test_id and test_id in self.ping_tests:
                send_times = self.ping_tests[test_id].get("send_times", {})
                if sequence_info and sequence_info in send_times:
                    sent_time = send_times[sequence_info]

            if message_id in self.active_pings:
                if has_console:
                    print(f"🔍 Echo {message_id} already tracked, ignoring duplicate")
                return

            ping_info = {
                "target": dst,
                "original_msg": original_msg,
                "sent_time": sent_time,
                "requester": src,
                "status": "waiting_ack",
                "sequence_info": sequence_info,
                "test_id": test_id,
            }

            self.active_pings[message_id] = ping_info

            if has_console:
                print(f"🏓 Echo tracked: ID={message_id}, target={dst}, test_id={test_id}")
                print(f"🔍 Active pings now: {list(self.active_pings.keys())}")

            asyncio.create_task(self._ping_timeout_task(message_id))

        except Exception as e:
            if has_console:
                print(f"❌ Error handling echo message: {e}")

    async def _handle_ack_message(self, message_data: dict):
        """Handle ACK message and calculate RTT with idempotent processing"""
        try:
            src_raw = message_data.get("src", "").upper()
            dst = message_data.get("dst", "").upper()
            msg = message_data.get("msg", "")

            src = src_raw.split(",")[0].strip() if "," in src_raw else src_raw.strip()

            if has_console:
                if "," in src_raw:
                    print(f"🏓 ACK path processing: '{src_raw}' → originator: '{src}'")

            match = re.search(r"\s+:ack(\d{3})$", msg)
            if not match:
                return

            ack_id = match.group(1)

            if ack_id not in self.active_pings:
                if has_console:
                    print(f"🏓 Received ACK {ack_id} from {src}, but no matching ping found")
                return

            ping_info = self.active_pings[ack_id]

            if ping_info.get("ack_processed", False):
                if has_console:
                    print(f"🏓 ACK {ack_id} already processed, ignoring duplicate")
                return

            if src != ping_info["target"] or dst != self.my_callsign:
                if has_console:
                    print(
                        f"🏓 ACK {ack_id} verification"
                        f" failed: src={src},"
                        f" expected={ping_info['target']}"
                    )
                return

            ping_info["ack_processed"] = True

            receive_time = time.time()
            sent_time = ping_info["sent_time"]
            rtt = receive_time - sent_time

            result = {
                "sequence": ping_info.get("sequence_info") or "",
                "rtt": rtt,
                "status": "success",
                "timestamp": receive_time,
            }

            test_id = ping_info.get("test_id")

            if test_id and test_id in self.ping_tests:
                test_summary = self.ping_tests[test_id]

                if test_summary["status"] == "running":
                    sequence = ping_info.get("sequence_info") or ""
                    completed_seqs = test_summary.get("completed_sequences", set())
                    if sequence and sequence in completed_seqs:
                        if has_console:
                            print(f"🏓 Sequence {sequence} already completed, ignoring duplicate ACK {ack_id}")
                        del self.active_pings[ack_id]
                        return

                    if sequence:
                        completed_seqs.add(sequence)

                    test_summary["results"].append(result)
                    test_summary["completed"] += 1

                    total_completed = test_summary["completed"] + test_summary["timeouts"]
                    test_completed = total_completed >= test_summary["total_pings"]

                    rtt_ms = rtt * 1000
                    result_msg = (
                        f"🏓 Ping {result['sequence']}"
                        f" to {ping_info['target']}:"
                        f" RTT = {rtt_ms:.1f}ms"
                    )
                    await self._send_ping_result(ping_info["requester"], result_msg, ping_info["target"])

                    if has_console:
                        print(
                            f"🏓 ACK processed:"
                            f" ID={ack_id},"
                            f" RTT={rtt * 1000:.1f}ms,"
                            f" Test complete:"
                            f" {test_completed}"
                        )

                    if test_completed:
                        completion_event_key = f"completion_{test_id}"
                        if not hasattr(self, "_completion_events"):
                            self._completion_events = {}

                        if completion_event_key not in self._completion_events:
                            self._completion_events[completion_event_key] = asyncio.Event()
                            self._completion_events[completion_event_key].set()

                            asyncio.create_task(
                                self._complete_test_with_cleanup(test_id, completion_event_key)
                            )
                else:
                    if has_console:
                        print(
                            f"🏓 ACK {ack_id} received"
                            f" but test {test_id} no"
                            f" longer running (status:"
                            f" {test_summary['status']})"
                        )

            del self.active_pings[ack_id]

        except Exception as e:
            if has_console:
                print(f"❌ Error handling ACK message: {e}")

    async def _complete_test(self, test_id: str):
        """Complete a test: cancel monitor, send summary,
        cleanup (idempotent with event coordination)"""
        try:
            if test_id not in self.ping_tests:
                if has_console:
                    print(f"🧹 Test {test_id} already completed and cleaned up")
                return

            test_summary = self.ping_tests[test_id]

            if test_summary["status"] != "running":
                if has_console:
                    print(f"🧹 Test {test_id} already in status '{test_summary['status']}'")
                return

            test_summary["status"] = "completing"

            monitor_task = test_summary.get("monitor_task")
            if monitor_task and not monitor_task.done():
                if has_console:
                    print(f"🧹 Cancelling monitor task for {test_id}")
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

            test_summary["status"] = "completed"
            test_summary["end_time"] = time.time()

            await self._send_test_summary(test_id)

        except Exception as e:
            if has_console:
                print(f"❌ Error completing test {test_id}: {e}")

    async def _complete_test_with_cleanup(self, test_id: str, completion_event_key: str):
        """Complete test and cleanup completion event"""
        try:
            await self._complete_test(test_id)
        finally:
            if (
                hasattr(self, "_completion_events")
                and completion_event_key in self._completion_events
            ):
                del self._completion_events[completion_event_key]

    async def _record_ping_result(self, test_id: str, result: dict) -> bool:
        """Record ping result and check for test completion (updated for idempotent design)"""
        if test_id not in self.ping_tests:
            return False

        test_summary = self.ping_tests[test_id]

        if test_summary["status"] != "running":
            if has_console:
                print(f"🔍 Test {test_id} no longer running, ignoring result")
            return False

        test_summary["results"].append(result)

        if result["status"] == "success":
            if test_summary["completed"] < test_summary["total_pings"]:
                test_summary["completed"] += 1
        elif result["status"] == "timeout":
            if test_summary["timeouts"] < test_summary["total_pings"]:
                test_summary["timeouts"] += 1

        test_completed = self._check_test_completion(test_id)

        if test_completed:
            if has_console:
                print(f"🏓 Test {test_id} completed via {result['status']}")

            completion_event_key = f"completion_{test_id}"
            if not hasattr(self, "_completion_events"):
                self._completion_events = {}

            if completion_event_key not in self._completion_events:
                self._completion_events[completion_event_key] = asyncio.Event()
                self._completion_events[completion_event_key].set()

                asyncio.create_task(
                    self._complete_test_with_cleanup(test_id, completion_event_key)
                )

            return True

        return False

    def _check_test_completion(self, test_id: str) -> bool:
        """Check if test is complete with validation (idempotent)"""
        if test_id not in self.ping_tests:
            return False

        test_summary = self.ping_tests[test_id]

        if test_summary["status"] != "running":
            return False

        total_completed = test_summary["completed"] + test_summary["timeouts"]
        expected_total = test_summary["total_pings"]

        if total_completed > expected_total:
            if has_console:
                print(
                    f"⚠️ Test {test_id} over-completion detected:"
                    f" {total_completed}/{expected_total}"
                )
            excess = total_completed - expected_total
            if test_summary["completed"] >= excess:
                test_summary["completed"] -= excess
            else:
                test_summary["timeouts"] -= excess
            total_completed = expected_total

        is_complete = total_completed >= expected_total

        if has_console and is_complete:
            print(
                f"🔍 Test {test_id} completion"
                f" detected:"
                f" {test_summary['completed']}"
                f" success +"
                f" {test_summary['timeouts']}"
                f" timeouts ="
                f" {total_completed}/{expected_total}"
            )

        return is_complete

    async def _ping_timeout_task(self, message_id: str):
        """Handle ping timeout after 30 seconds"""
        try:
            await asyncio.sleep(self.ping_timeout)

            if message_id not in self.active_pings:
                return

            ping_info = self.active_pings[message_id]

            if ping_info["status"] != "waiting_ack":
                return

            timeout_result = {
                "sequence": ping_info.get("sequence_info") or "",
                "rtt": None,
                "status": "timeout",
                "timestamp": time.time(),
            }

            test_id = ping_info.get("test_id")

            test_completed = (
                await self._record_ping_result(test_id, timeout_result) if test_id else False
            )

            del self.active_pings[message_id]

            if test_id and test_id in self.ping_tests:
                timeout_msg = (
                    f"🏓 Ping"
                    f" {timeout_result['sequence']}"
                    f" to {ping_info['target']}:"
                    f" timeout (no ACK after 30s)"
                )
                await self._send_ping_result(ping_info["requester"], timeout_msg, ping_info["target"])

            if has_console:
                print(f"⏰ Timeout processed: ID={message_id}, Test complete: {test_completed}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if has_console:
                print(f"❌ Error in ping timeout task: {e}")

    async def handle_ctcping(self, kwargs, requester):
        """Handle CTC ping test with roundtrip time measurement"""
        ping_target = kwargs.get("call", "").upper()
        payload_size = kwargs.get("payload", 25)
        repeat_count = kwargs.get("repeat", 1)

        if not ping_target:
            return "❌ Target callsign required (call:TARGET)"

        if not re.match(r"^[A-Z]{1,2}[0-9][A-Z]{1,3}(-\d{1,2})?$", ping_target):
            return "❌ Invalid target callsign format"

        if ping_target == self.my_callsign:
            return "❌ Cannot ping yourself"

        if hasattr(self, "blocked_callsigns") and ping_target in self.blocked_callsigns:
            return f"❌ Target {ping_target} is blocked"

        try:
            payload_size = int(payload_size)
            if payload_size < 25 or payload_size > 140:
                return "❌ Payload size must be between 25 and 140 bytes"
        except (ValueError, TypeError):
            return "❌ Invalid payload size"

        try:
            repeat_count = int(repeat_count)
            if repeat_count < 1 or repeat_count > 5:
                return "❌ Repeat count must be between 1 and 5"
        except (ValueError, TypeError):
            return "❌ Invalid repeat count"

        asyncio.create_task(self._start_ping_test(ping_target, payload_size, repeat_count, requester))

        return (
            f"🏓 Ping test to {ping_target}"
            f" started: {repeat_count} ping(s)"
            f" with {payload_size} bytes payload..."
        )

    async def _start_ping_test(
        self, target: str, payload_size: int, repeat_count: int, requester: str
    ):
        """Start the ping test sequence"""
        test_id = f"{target}_{int(time.time())}"

        test_summary = {
            "test_id": test_id,
            "target": target,
            "requester": requester,
            "total_pings": repeat_count,
            "payload_size": payload_size,
            "start_time": time.time(),
            "results": [],
            "completed": 0,
            "timeouts": 0,
            "status": "running",
            "monitor_task": None,
            "completed_sequences": set(),
        }

        self.ping_tests[test_id] = test_summary

        try:
            for sequence in range(1, repeat_count + 1):
                if test_summary["status"] != "running":
                    break

                base_msg = f"Ping test {sequence}/{repeat_count} to measure roundtrip"

                if len(base_msg) > payload_size:
                    ping_message = base_msg[:payload_size]
                elif len(base_msg) < payload_size:
                    padding = "." * (payload_size - len(base_msg))
                    ping_message = base_msg + padding
                else:
                    ping_message = base_msg

                await self._send_ping_message(
                    target, ping_message, sequence, repeat_count, requester, test_id
                )

                if sequence < repeat_count:
                    await asyncio.sleep(20.0)

            monitor_task = asyncio.create_task(self._monitor_test_completion(test_id))
            test_summary["monitor_task"] = monitor_task

        except Exception as e:
            if has_console:
                print(f"❌ Ping test error: {e}")

            test_summary["status"] = "error"
            await self._send_ping_result(requester, f"🏓 Ping test error: {str(e)[:50]}", target)

    async def _send_ping_message(
        self, target: str, message: str, sequence: int, total: int, requester: str, test_id: str
    ):
        """Send a single ping message and track it"""
        try:
            if self.message_router:
                send_time = time.time()
                if test_id in self.ping_tests:
                    self.ping_tests[test_id].setdefault("send_times", {})
                    self.ping_tests[test_id]["send_times"][f"{sequence}/{total}"] = send_time

                message_data = {"dst": target, "msg": message, "src_type": "ctcping", "type": "msg"}

                await self.message_router.publish("ctcping", "udp_message", message_data)

                if has_console:
                    print(f"🏓 Sent ping {sequence}/{total} to {target}: '{message[:30]}...'")
                    print("🏓 Waiting for echo and ACK...")

        except Exception as e:
            if has_console:
                print(f"❌ Failed to send ping to {target}: {e}")

    async def _monitor_test_completion(self, test_id: str):
        """Monitor test completion and send summary when done"""
        try:
            start_time = time.time()
            max_wait = 300

            while (time.time() - start_time) < max_wait:
                if test_id not in self.ping_tests:
                    return

                test_summary = self.ping_tests[test_id]

                total_completed = test_summary["completed"] + test_summary["timeouts"]

                if total_completed >= test_summary["total_pings"]:
                    test_summary["status"] = "completed"
                    test_summary["end_time"] = time.time()
                    await self._send_test_summary(test_id)
                    return

                await asyncio.sleep(1.0)

            if test_id in self.ping_tests:
                test_summary = self.ping_tests[test_id]
                test_summary["status"] = "timeout"
                test_summary["end_time"] = time.time()
                await self._send_test_summary(test_id, "Test timeout after 5 minutes")

        except Exception as e:
            if has_console:
                print(f"❌ Error monitoring test completion: {e}")

    async def _send_test_summary(self, test_id: str, error_msg: str = None):
        """Send complete test summary to requester"""
        try:
            if test_id not in self.ping_tests:
                return

            test_summary = self.ping_tests[test_id]

            if error_msg:
                await self._send_ping_result(test_summary["requester"], f"🏓 {error_msg}", test_summary["target"])
            else:
                results = test_summary["results"]
                total_pings = test_summary["total_pings"]

                successful_from_results = len([r for r in results if r["rtt"] is not None])
                timeouts_from_results = len([r for r in results if r["rtt"] is None])

                successful = test_summary["completed"]
                timeouts = test_summary["timeouts"]

                if successful != successful_from_results or timeouts != timeouts_from_results:
                    if has_console:
                        print("⚠️ Ping summary inconsistency detected!")
                        print(
                            f"   Results:"
                            f" {successful_from_results}"
                            f" success,"
                            f" {timeouts_from_results}"
                            f" timeouts"
                        )
                        print(f"   Tracked: {successful} success, {timeouts} timeouts")

                loss_percent = int((timeouts / total_pings) * 100)

                target = test_summary["target"]
                payload_size = test_summary["payload_size"]

                if successful > 0:
                    results = test_summary["results"]
                    successful_rtts = [r["rtt"] for r in results if r["rtt"] is not None]

                    if successful_rtts:
                        min_rtt = min(successful_rtts) * 1000
                        max_rtt = max(successful_rtts) * 1000
                        avg_rtt = (sum(successful_rtts) / len(successful_rtts)) * 1000

                        summary_msg = (
                            f"🏓 Ping summary to"
                            f" {target}:"
                            f" {successful}/{total_pings}"
                            f" replies,"
                            f" {loss_percent}% loss,"
                            f" {payload_size}B payload."
                            f" RTT min/avg/max ="
                            f" {min_rtt:.1f}/"
                            f"{avg_rtt:.1f}/"
                            f"{max_rtt:.1f}ms"
                        )

                else:
                    summary_msg = (
                        f"🏓 Ping summary to"
                        f" {target}:"
                        f" {loss_percent}% packet loss"
                        f" ({successful}/{total_pings}),"
                        f" {payload_size}B payload"
                    )

                await self._send_ping_result(test_summary["requester"], summary_msg, test_summary["target"])

            del self.ping_tests[test_id]

            if has_console:
                print(f"📊 Test summary sent for {test_id}")

        except Exception as e:
            if has_console:
                print(f"❌ Error sending test summary: {e}")
                import traceback

                traceback.print_exc()

    async def _send_ping_result(self, requester: str, result_message: str, target: str = ""):
        """Send ping result to requester"""
        try:
            if self.message_router:
                if requester == self.my_callsign:
                    now_ms = int(time.time() * 1000)
                    result_data = {
                        "src": self.my_callsign,
                        "dst": target or requester,
                        "msg": result_message,
                        "msg_id": now_ms,
                        "src_type": "node",
                        "type": "msg",
                        "timestamp": now_ms,
                    }
                    await self.message_router.publish("ctcping", "websocket_message", result_data)
                else:
                    result_data = {
                        "dst": requester,
                        "msg": result_message,
                        "src_type": "ctcping_result",
                        "type": "msg",
                    }
                    await self.message_router.publish("ctcping", "udp_message", result_data)

        except Exception as e:
            if has_console:
                print(f"❌ Failed to send ping result: {e}")

    async def cleanup_ping_tests(self):
        """Clean up all active ping tests"""
        if has_console:
            print(f"🧹 Cleaning up {len(self.active_pings)} active pings...")

        self.active_pings.clear()
        self.ping_tests.clear()

        if has_console:
            print("✅ All ping tests cleaned up")
