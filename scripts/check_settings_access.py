#!/usr/bin/env python3
"""
check_settings_access.py — fail when a store-managed key is read outside the
settings accessor.

    python3 scripts/check_settings_access.py          # from the repo root
    docker exec franklin-automation python3 /app/scripts/check_settings_access.py

Flags direct os.getenv / os.environ.get / os.environ[...] reads of any name
the registry classes as setting, detected, derived or retired, including the
dynamic SOLAR_ARRAY_<ID>_<FIELD> reads and legacy single-array aliases.
Secrets, infra and dev-tool names may be read directly.

Non-literal reads (os.getenv(some_var)) are listed as warnings to eyeball.
Exit 1 on any violation.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import settings  # noqa: E402

# Files allowed to read env directly: the accessor itself, the tools that
# compare/import env by design, and the standalone dev tools.
EXEMPT = {
    'settings.py', 'check_settings_access.py', 'dump_effective_config.py',
    'config_admin.py', 'migrate_v46.py', 'config_review.py',
    'modbus_collect.py', 'modbus_cloud_compare.py', 'modbus_register_logger.py',
    'modbus_discovery.py', 'probe_reserve.py',
}

READ_RE = re.compile(
    r"os\.(?:getenv|environ\.get)\(\s*(f?)(['\"])(.*?)\2"
    r"|os\.environ\[\s*(f?)(['\"])(.*?)\5\s*\](?!\s*=[^=])")
NONLITERAL_RE = re.compile(r"os\.(?:getenv|environ\.get)\(\s*(?!f?['\"])([A-Za-z_][\w.]*)")

ENV_ONLY_FIELDS = tuple(f for f, (cls, _, _) in settings.ARRAY_FIELDS.items()
                        if cls not in settings.STORE_CLASSES)


def check_name(name: str, is_f: bool):
    if is_f or '{' in name:
        if 'SOLAR_ARRAY' in name or name.startswith('{prefix}') or name.startswith('{'):
            tail = re.sub(r'\{[^}]*\}', '', name)
            if tail.endswith(ENV_ONLY_FIELDS):
                return None
            if 'SOLAR_ARRAY' in name or tail.endswith(tuple(settings.ARRAY_FIELDS)):
                return 'dynamic per-array read'
        return None
    if settings.is_store_managed_env(name):
        return 'store-managed'
    return None


def main():
    violations, warnings = [], []
    for path in sorted(HERE.glob('*.py')):
        if path.name in EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
            code = line.split('#', 1)[0]
            for m in READ_RE.finditer(code):
                is_f = bool(m.group(1) or m.group(4))
                name = m.group(3) if m.group(3) is not None else m.group(6)
                why = check_name(name, is_f)
                if why:
                    violations.append(f'{path.name}:{lineno}: {why} {name!r}: {line.strip()}')
            for m in NONLITERAL_RE.finditer(code):
                warnings.append(f'{path.name}:{lineno}: non-literal read ({m.group(1)}): '
                                f'{line.strip()}')
    for w in warnings:
        print(f'WARN  {w}')
    for v in violations:
        print(f'FAIL  {v}')
    print(f'{len(violations)} violation(s), {len(warnings)} warning(s)')
    return 1 if violations else 0


if __name__ == '__main__':
    sys.exit(main())
