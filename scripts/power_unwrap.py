#!/usr/bin/env python3
"""
power_unwrap.py — undo int16 wraparound on Modbus power registers.

The aGate reports grid power (SunSpec model 701 offset 8) and battery power
(register 1048) as signed 16-bit watts, so a real value beyond +/-32,767 W
wraps by 65,536: 34 kW of import reads as -31.5 kW (export). Small sites never
get there; large sites (three or more aPowers, EV charging, big generators) do.

Two ways to recover the true value, in order of preference:
  1. A simultaneous reference that has no 16-bit limit (the local connection's
     grid reading). The candidate closest to it wins.
  2. The previous sample of the same register. Only used when that sample was
     already near the edge (|prev| >= EDGE_W), so ordinary load swings far
     from the limit are never touched.

Without either, the plain signed value is returned unchanged.
Idea credited to don4of4's Home Assistant fork (_resolve_grid_wraparound).
"""

from typing import Optional

WRAP_W = 65536
EDGE_W = 20000


def int16(raw: int) -> int:
    """uint16 register value to signed int."""
    return raw - 0x10000 if raw >= 0x8000 else raw


def unwrap_w(raw: int, ref_w: Optional[float] = None,
             prev_w: Optional[float] = None) -> int:
    """Signed watts from a uint16 register, unwrapped when a reference allows.

    raw    -- the register value as read (0..65535)
    ref_w  -- simultaneous reading from a source without the 16-bit limit
    prev_w -- previous sample of this register (already unwrapped), watts
    """
    s = int16(raw)
    if ref_w is not None:
        anchor = ref_w
    elif prev_w is not None and abs(prev_w) >= EDGE_W:
        anchor = prev_w
    else:
        return s
    return min((s, s + WRAP_W, s - WRAP_W), key=lambda c: abs(c - anchor))


def previous_modbus_kw(column: str, max_age_min: int = 15,
                       device_id: str = 'agate_main') -> Optional[float]:
    """Last Modbus-sourced value of a system_readings column, if recent.

    Collectors and the engine run as separate short-lived processes, so the
    previous sample comes from the database. Returns kW or None.
    """
    if column not in ('grid_kw', 'battery_kw_direct'):
        return None
    try:
        import db
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(minutes=max_age_min)).strftime('%Y-%m-%d %H:%M:%S')
        rows = db.query(
            f"SELECT {column} AS v FROM system_readings "
            "WHERE source = 'modbus' AND device_id = ? AND timestamp >= ? "
            f"AND {column} IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
            (device_id, cutoff))
        return float(rows[0]['v']) if rows else None
    except Exception:
        return None


if __name__ == '__main__':
    # Self-check: python3 power_unwrap.py
    t = [
        ((34000 - WRAP_W) & 0xFFFF, None, 30000, 34000),   # import past the edge
        ((-34000) & 0xFFFF, None, -30000, -34000),         # export past the edge
        (10000, None, 32000, 10000),                       # back down from the edge
        ((-5000) & 0xFFFF, None, 4000, -5000),             # normal swing, prev small
        ((40000 - WRAP_W) & 0xFFFF, 39800, None, 40000),   # local reference wins
        ((-1500) & 0xFFFF, -1450, None, -1500),            # reference, no wrap
        (1234, None, None, 1234),                          # nothing to compare
    ]
    bad = 0
    for raw, ref, prev, want in t:
        got = unwrap_w(raw, ref, prev)
        bad += got != want
        print('ok ' if got == want else 'BAD', raw, ref, prev, '->', got, 'want', want)
    print('all ok' if not bad else f'{bad} failed')
