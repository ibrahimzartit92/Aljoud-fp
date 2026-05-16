from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class NetCheckResult:
    ok: bool
    avg_ms: float | None
    rating: str
    raw: str


def ping_avg_ms(ip: str, count: int = 3, timeout_s: int = 1) -> tuple[bool, float | None, str]:
    """
    Uses system ping (icmp). Works for LAN/ZeroTier typically.
    Returns (ok, avg_ms, raw_output).
    """
    try:
        # -c count, -W timeout (seconds) on Linux
        p = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout_s), ip],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        if p.returncode != 0:
            return (False, None, out)

        # Try parse avg from "rtt min/avg/max/mdev = 0.123/0.456/..."
        m = re.search(r"=\s*([\d\.]+)/([\d\.]+)/([\d\.]+)/", out)
        if m:
            avg = float(m.group(2))
            return (True, avg, out)

        # Fallback: find all "time=XX ms"
        times = [float(x) for x in re.findall(r"time=([\d\.]+)\s*ms", out)]
        if times:
            avg = sum(times) / len(times)
            return (True, avg, out)

        return (True, None, out)
    except Exception as e:
        return (False, None, str(e))


def rate(avg_ms: float | None, ok: bool) -> str:
    if not ok:
        return "غير متصل"
    if avg_ms is None:
        return "متصل"
    # thresholds tuned for LAN/ZeroTier
    if avg_ms <= 30:
        return "ممتاز"
    if avg_ms <= 80:
        return "جيد جدًا"
    if avg_ms <= 150:
        return "جيد"
    return "ضعيف"
