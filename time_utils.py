from __future__ import annotations

import re
from datetime import datetime


def parse_timestamp(raw: str, now: datetime | None = None) -> datetime | None:
    raw = raw.strip()
    now = now or datetime.now()
    formats = (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d %H:%M:%S",
        "%H:%M:%S",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%m/%d %H:%M:%S":
                parsed = parsed.replace(year=now.year)
            elif fmt == "%H:%M:%S":
                parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            return parsed
        except ValueError:
            pass

    match = re.search(r"(?:(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+)?(\d{1,2}):(\d{2}):(\d{2})", raw)
    if not match:
        return None
    year = int(match.group(1) or now.year)
    month = int(match.group(2) or now.month)
    day = int(match.group(3) or now.day)
    try:
        return datetime(year, month, day, int(match.group(4)), int(match.group(5)), int(match.group(6)))
    except ValueError:
        return None
