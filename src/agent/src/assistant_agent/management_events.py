"""Bounded invalidations with explicit resynchronization after gaps or restarts."""
import asyncio
from collections import deque
import json
import secrets


class EventBuffer:
    """Keep only non-sensitive invalidations, never conversation bodies or credentials."""

    def __init__(self, limit=256):
        self.epoch = secrets.token_hex(8)
        self.sequence = 0
        self.events = deque(maxlen=limit)
        self.changed = asyncio.Condition()

    @property
    def cursor(self):
        return f"{self.epoch}:{self.sequence}"

    async def publish(self, event):
        async with self.changed:
            self.sequence += 1
            self.events.append((self.sequence, event))
            self.changed.notify_all()

    def since(self, cursor):
        try:
            epoch, sequence = cursor.split(":")
            sequence = int(sequence)
        except (AttributeError, ValueError):
            return [(self.cursor, "resync")]
        oldest = self.events[0][0] if self.events else self.sequence + 1
        if epoch != self.epoch or sequence < oldest - 1 or sequence > self.sequence:
            return [(self.cursor, "resync")]
        return [(f"{self.epoch}:{number}", event) for number, event in self.events if number > sequence]

    async def stream(self, cursor, disconnected):
        while not await disconnected():
            async with self.changed:
                pending = self.since(cursor)
                if not pending:
                    try:
                        await asyncio.wait_for(self.changed.wait(), 15)
                    except asyncio.TimeoutError:
                        pass
                    pending = self.since(cursor)
            if not pending:
                yield ": keepalive\n\n"
            for cursor, event in pending:
                yield f"id: {cursor}\nevent: {event}\ndata: {json.dumps({'kind': event})}\n\n"
