#!/usr/bin/env python3
"""
Franklin cloud client with saved-token reuse (v4.7).

Every cloud user used to build a fresh TokenFetcher + Client, which means a
fresh account login on every call. When Franklin's login endpoint is down
(Oct 2026) that fails even though the data endpoints still work for an
existing session. This keeps the last good session token in
data/franklin_token.json and pre-seeds new clients with it; the library only
logs in again when the server answers 401.

    from cloud_token import make_client
    client = make_client()          # None if no credentials configured
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

import channel_stats

try:
    from config import config
except Exception:  # pragma: no cover
    config = None


def _token_path() -> Path:
    return channel_stats._data_dir() / 'franklin_token.json'


def _creds():
    if config is not None:
        return (config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD, config.FRANKLIN_GATEWAY_ID)
    return (os.getenv('FRANKLIN_USERNAME', ''), os.getenv('FRANKLIN_PASSWORD', ''),
            os.getenv('FRANKLIN_GATEWAY_ID', ''))


def load_token() -> Optional[str]:
    try:
        with open(_token_path()) as f:
            d = json.load(f)
        user = _creds()[0]
        if d.get('user') and user and d['user'] != user:
            return None
        return d.get('token') or None
    except Exception:
        return None


def save_token(token: str):
    try:
        p = _token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'token': token, 'user': _creds()[0],
                       'saved_at': time.strftime('%Y-%m-%dT%H:%M:%S')}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        pass


def make_client(username: str = None, password: str = None, gateway_id: str = None):
    """Return a franklinwh Client pre-seeded with the saved token, or None."""
    from franklinwh import Client, TokenFetcher
    u, p, g = _creds()
    username, password, gateway_id = username or u, password or p, gateway_id or g
    if not (username and password and gateway_id):
        return None

    fetcher = TokenFetcher(username, password)
    orig = fetcher.get_token

    async def get_token_and_save():
        t0 = time.time()
        try:
            tok = await orig()
        except Exception as e:
            channel_stats.record('cloud', 'login', False, (time.time() - t0) * 1000, str(e))
            channel_stats.record_token('login_fail')
            raise
        channel_stats.record('cloud', 'login', True, (time.time() - t0) * 1000)
        channel_stats.record_token('login_ok')
        save_token(tok)
        return tok

    fetcher.get_token = get_token_and_save
    client = Client(fetcher, gateway_id)
    saved = load_token()
    if saved:
        client.token = saved
        client._seeded_token = saved
    return client


def note_success(client):
    """Call after a successful cloud request: counts a reuse when the saved
    token carried the request without a fresh login."""
    try:
        seeded = getattr(client, '_seeded_token', None)
        if seeded and client.token == seeded:
            channel_stats.record_token('reused')
            client._seeded_token = None   # count once per client
    except Exception:
        pass
