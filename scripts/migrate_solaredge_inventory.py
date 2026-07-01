#!/usr/bin/env python3
"""
Normalize SolarEdge device_inventory rows to a single representation.

Two collection eras left the table inconsistent:
  - Portal era:  serial '731ED2B5-18', firmware '4.24.16'
  - Modbus era:  serial '731ED2B5',    firmware '0004.0024.0025'

This makes every solaredge row use:
  - Bare SunSpec serial   (strip a trailing '-<digits>' suffix)
  - Human firmware form    ('0004.0024.0025' -> '4.24.25')

so each inverter has one continuous timeline regardless of collection method.

Idempotent: re-running after apply changes nothing. Defaults to DRY-RUN;
pass --apply to write.

Usage:
    python3 migrate_solaredge_inventory.py            # dry-run (shows changes)
    python3 migrate_solaredge_inventory.py --apply     # write changes
"""

import argparse
import re
import sqlite3
import sys

DB_PATH = '/app/data/franklin.db'


def normalize_serial(serial):
    """Strip a trailing portal suffix like '-18' / '-90'."""
    if not serial:
        return serial
    return re.sub(r'-\d+$', '', serial)


def normalize_firmware(v):
    """Strip per-segment zero padding: '0004.0024.0025' -> '4.24.25'."""
    if not v:
        return v
    parts = v.split('.')
    if parts and all(p.isdigit() for p in parts):
        return '.'.join(str(int(p)) for p in parts)
    return v


def main():
    parser = argparse.ArgumentParser(description="Normalize SolarEdge device_inventory rows")
    parser.add_argument('--apply', action='store_true', help='Write changes (default: dry-run)')
    parser.add_argument('--db', default=DB_PATH, help=f'DB path (default {DB_PATH})')
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, timestamp, serial_number, firmware "
        "FROM device_inventory WHERE system = 'solaredge' ORDER BY id"
    ).fetchall()

    if not rows:
        print("No solaredge device_inventory rows found.")
        conn.close()
        return

    changes = []
    for r in rows:
        old_sn = r['serial_number']
        old_fw = r['firmware']
        new_sn = normalize_serial(old_sn)
        new_fw = normalize_firmware(old_fw)
        if new_sn != old_sn or new_fw != old_fw:
            changes.append((r['id'], r['timestamp'], old_sn, new_sn, old_fw, new_fw))

    print(f"Examined {len(rows)} solaredge rows; {len(changes)} need normalization.\n")
    for _id, ts, osn, nsn, ofw, nfw in changes:
        sn_part = f"serial {osn} -> {nsn}" if osn != nsn else f"serial {osn} (unchanged)"
        fw_part = f"fw {ofw} -> {nfw}" if ofw != nfw else f"fw {ofw} (unchanged)"
        print(f"  id={_id} [{ts}]  {sn_part}  |  {fw_part}")

    if not changes:
        print("\nNothing to do — already normalized.")
        conn.close()
        return

    if not args.apply:
        print(f"\nDRY-RUN: {len(changes)} row(s) would be updated. Re-run with --apply to write.")
        conn.close()
        return

    for _id, _ts, _osn, nsn, _ofw, nfw in changes:
        conn.execute(
            "UPDATE device_inventory SET serial_number = ?, firmware = ? WHERE id = ?",
            (nsn, nfw, _id),
        )
    conn.commit()
    print(f"\nAPPLIED: {len(changes)} row(s) updated.")
    conn.close()


if __name__ == '__main__':
    main()
