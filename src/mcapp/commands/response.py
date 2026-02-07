"""ResponseMixin: sending responses and chunking logic."""

import asyncio
import time

from .constants import MAX_CHUNKS, MAX_RESPONSE_LENGTH, has_console


class ResponseMixin:
    """Mixin providing response sending and chunking methods."""

    async def send_response(self, response, recipient, src_type="udp"):
        """Send response back to requester, chunking if necessary"""
        if not response:
            return

        if has_console:
            print(
                f"🐛 send_response:"
                f" recipient='{recipient}',"
                f" my_callsign='{self.my_callsign}',"
                f" equal="
                f"{recipient.upper() == self.my_callsign}"
            )

        # Split response into chunks if too long
        chunks = self._chunk_response(response)

        for i, chunk in enumerate(chunks[:MAX_CHUNKS]):
            if len(chunks) > 1:
                chunk_header = f"({i + 1}/{min(len(chunks), MAX_CHUNKS)}) "
                chunk = chunk_header + chunk

            if recipient.upper() == self.my_callsign:
                if has_console:
                    print("🔄 CommandHandler: Self-response, sending directly to WebSocket")

                # Send directly via WebSocket, bypass BLE routing
                if self.message_router:
                    websocket_message = {
                        "src": self.my_callsign,
                        "dst": recipient,
                        "msg": chunk,
                        "src_type": "ble",
                        "type": "msg",
                        "timestamp": int(time.time() * 1000),
                    }
                    await self.message_router.publish(
                        "command", "websocket_message", websocket_message
                    )

            else:
                # Send via message router
                if self.message_router:
                    message_data = {
                        "dst": recipient,
                        "msg": chunk,
                        "src_type": "command_response",
                        "type": "msg",
                    }

                    # Route to appropriate protocol (BLE or UDP)
                    if has_console:
                        print("command handler: src_type", src_type)

                    try:
                        if src_type in ("ble", "ble_remote"):
                            await self.message_router.publish(
                                "command", "ble_message", message_data
                            )
                            if has_console:
                                print(
                                    f"📋 CommandHandler: Sent chunk {i + 1} via BLE to {recipient}"
                                )
                        elif src_type in ["udp", "node", "lora"]:
                            # Update message data for UDP transport
                            message_data["src_type"] = "command_response_udp"
                            await self.message_router.publish(
                                "command", "udp_message", message_data
                            )
                            if has_console:
                                print(
                                    f"📋 CommandHandler: Sent chunk {i + 1} via UDP to {recipient}"
                                )
                        else:
                            print("TransportUnavailableError BLE and UDP not available", src_type)
                    except Exception as ble_error:
                        if has_console:
                            print(f"⚠️  CommandHandler: send failed to {recipient}: {ble_error}")
                            continue

            # Small delay between chunks
            if i < len(chunks) - 1:
                await asyncio.sleep(12)

            if has_console:
                print(f"📋 CommandHandler: Sent response chunk {i + 1} to {recipient}")

    def _chunk_response(self, response):
        """Split response into chunks - simple and robust"""
        max_bytes = MAX_RESPONSE_LENGTH

        # Single chunk fits?
        if len(response.encode("utf-8")) <= max_bytes:
            return [response]

        chunks = []

        # Split on padding separator first (for our two-line responses)
        if ", " in response and len(response.split(", ")) == 2:
            chunks = response.split(", ")
        else:
            # Split long single responses on station boundaries
            if " | " in response:
                parts = response.split(" | ")
                current = ""

                for part in parts:
                    test = current + (" | " if current else "") + part
                    if len(test.encode("utf-8")) <= max_bytes:
                        current = test
                    else:
                        if current:
                            chunks.append(current)
                        current = part

                if current:
                    chunks.append(current)
            else:
                # Fallback: character-wise split
                chunks = [response[i : i + max_bytes] for i in range(0, len(response), max_bytes)]

        return chunks[:MAX_CHUNKS]

    def _pad_for_chunk_break(self, text, target_length=MAX_RESPONSE_LENGTH - 2):
        """Pad text to force clean chunk boundary using byte-aware calculation"""
        text_bytes = text.encode("utf-8")

        if len(text_bytes) < target_length:
            # Calculate padding needed in bytes
            padding_needed = target_length - len(text_bytes)
            # Use spaces for padding (1 byte each)
            padded_text = text + " " * padding_needed + ", "
        else:
            # Text is already at or over target, just add separator
            padded_text = text + ", "

        if has_console:
            original_bytes = len(text.encode("utf-8"))
            padded_bytes = len(padded_text.encode("utf-8"))
            print(f"🔍 Padding: '{text[:30]}...' {original_bytes}→{padded_bytes} bytes")

        return padded_text
