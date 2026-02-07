"""DedupMixin: deduplication, throttling, and abuse protection."""

import hashlib
import time

from .constants import COMMAND_THROTTLING, DEFAULT_THROTTLE_TIMEOUT, has_console


class DedupMixin:
    """Mixin providing dedup/throttle/abuse protection methods."""

    def _init_dedup(self):
        """Initialize dedup/throttle state. Called from CommandHandler.__init__."""
        # Primary deduplication (msg_id based)
        self.processed_msg_ids = {}  # {msg_id: timestamp}
        self.msg_id_timeout = 5 * 60  # 5 minutes

        # Secondary throttling (content hash based)
        self.command_throttle = {}  # {content_hash: timestamp}
        self.throttle_timeout = DEFAULT_THROTTLE_TIMEOUT

        # Abuse protection
        self.failed_attempts = {}  # {src: [timestamp, timestamp, ...]}
        self.max_failed_attempts = 3
        self.failed_attempt_window = DEFAULT_THROTTLE_TIMEOUT
        self.block_duration = 5 * DEFAULT_THROTTLE_TIMEOUT
        self.blocked_users = {}  # {src: block_timestamp}
        self.block_notifications_sent = set()

    def _get_content_hash(self, src, msg_text, dst=None):
        """Create hash from source + command (without arguments for command-specific throttling)"""
        # Extract command for specific throttling
        if msg_text.startswith("!"):
            parts = msg_text[1:].split()
            if parts:
                command = parts[0].lower()
                # For commands with specific throttling, use command-only hash
                if command in COMMAND_THROTTLING:
                    if dst:
                        content = f"{src}:{dst}:!{command}"
                    else:
                        content = f"{src}:!{command}"
                else:
                    if dst:
                        content = f"{src}:{dst}:{msg_text}"
                    else:
                        content = f"{src}:{msg_text}"  # Full command + args for others
            else:
                content = f"{src}:{msg_text}"
        else:
            content = f"{src}:{msg_text}"

        hash_value = hashlib.md5(content.encode()).hexdigest()[:8]
        if has_console:
            print(f"🔍 Hash generation: '{content}' -> {hash_value}")

        return hash_value

    def _is_duplicate_msg_id(self, msg_id):
        """Check msg_id cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_msg_id_cache(current_time)
        return msg_id in self.processed_msg_ids

    def _is_throttled(self, content_hash, command=None):
        """Check throttle cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_throttle_cache(current_time)
        return content_hash in self.command_throttle

    def _is_user_blocked(self, src):
        """Check if user is blocked and cleanup expired blocks"""
        current_time = time.time()
        self._cleanup_blocked_users(current_time)
        return src in self.blocked_users

    def _mark_msg_id_processed(self, msg_id):
        """Mark msg_id as processed"""
        self.processed_msg_ids[msg_id] = time.time()

    def _mark_content_processed(self, content_hash, command=None):
        """Mark content hash as processed with command-aware timestamp"""
        self.command_throttle[content_hash] = {"timestamp": time.time(), "command": command}

    def _track_failed_attempt(self, src):
        """Track failed command attempt and block if necessary"""
        current_time = time.time()

        # Initialize or get existing attempts
        if src not in self.failed_attempts:
            self.failed_attempts[src] = []

        # Add current attempt
        self.failed_attempts[src].append(current_time)

        # Clean old attempts outside the window
        cutoff = current_time - self.failed_attempt_window
        self.failed_attempts[src] = [
            timestamp for timestamp in self.failed_attempts[src] if timestamp > cutoff
        ]

        # Check if user should be blocked
        if len(self.failed_attempts[src]) >= self.max_failed_attempts:
            self.blocked_users[src] = current_time
            if has_console:
                print(
                    f"🚫 CommandHandler: BLOCKED"
                    f" user {src} for"
                    f" {self.block_duration / 60}"
                    f" minutes due to"
                    f" {len(self.failed_attempts[src])}"
                    f" failed attempts"
                )

    def _cleanup_msg_id_cache(self, current_time):
        """Remove old entries from msg_id cache"""
        cutoff = current_time - self.msg_id_timeout
        expired = [mid for mid, timestamp in self.processed_msg_ids.items() if timestamp < cutoff]
        for mid in expired:
            del self.processed_msg_ids[mid]

    def _cleanup_blocked_users(self, current_time):
        """Remove old entries from blocked users"""
        cutoff = current_time - self.block_duration
        expired = [src for src, timestamp in self.blocked_users.items() if timestamp < cutoff]
        for src in expired:
            del self.blocked_users[src]
            self.block_notifications_sent.discard(src)

            if has_console:
                print(f"🔓 CommandHandler: UNBLOCKED user {src}")

    def _cleanup_throttle_cache(self, current_time, timeout=None):
        """Remove old entries from throttle cache with specific timeout"""
        if has_console:
            print(f"🔍 Cleanup throttle cache at {current_time}")

        expired = []

        for chash, data in self.command_throttle.items():
            if isinstance(data, dict):
                timestamp = data["timestamp"]
                cmd = data.get("command")
            else:
                # Backward compatibility für alte float timestamps
                timestamp = data
                cmd = None

            # Determine timeout for this entry
            if cmd and cmd in COMMAND_THROTTLING:
                entry_timeout = COMMAND_THROTTLING[cmd]
            else:
                entry_timeout = DEFAULT_THROTTLE_TIMEOUT

            age = current_time - timestamp

            if has_console:
                print(
                    f"🔍   Entry hash:{chash}"
                    f" cmd:{cmd}"
                    f" age:{age:.1f}s"
                    f" timeout:{entry_timeout}s"
                    f" -> {'EXPIRED' if age > entry_timeout else 'VALID'}"
                )

            if age > entry_timeout:
                expired.append(chash)

        for chash in expired:
            del self.command_throttle[chash]
            if has_console:
                print(f"🔍   Removed expired hash:{chash}")
