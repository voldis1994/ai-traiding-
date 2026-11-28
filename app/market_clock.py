"""UTC quote event/receipt clocks. Never silently substitute local time for market time."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math


def utc(value):
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('Timezone required')
    return t.astimezone(timezone.utc)


@dataclass(frozen=True)
class Quote:
    instrument: str
    event_time: datetime
    received_at: datetime
    bid: float
    ask: float
    wire_received_at: datetime | None = None

    def __post_init__(self):
        object.__setattr__(self, 'event_time', utc(self.event_time))
        object.__setattr__(self, 'received_at', utc(self.received_at))
        if self.wire_received_at is not None:
            object.__setattr__(self, 'wire_received_at', utc(self.wire_received_at))
            if self.wire_received_at > self.received_at: raise ValueError('Wire receipt after availability')
        if not self.instrument or not all(math.isfinite(x) for x in (self.bid, self.ask)) or not 0 < self.bid <= self.ask:
            raise ValueError('Invalid quote')

    @property
    def mid(self): return (self.bid + self.ask) / 2
    @property
    def spread(self): return self.ask - self.bid
    @property
    def latency(self): return (self.received_at - self.event_time).total_seconds()


def minute(t): return utc(t).replace(second=0, microsecond=0)
