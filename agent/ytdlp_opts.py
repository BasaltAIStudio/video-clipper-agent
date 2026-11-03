'''yt-dlp option helpers: YouTube cookie support (v4).

Accepts Netscape or JSON cookie exports via YOUTUBE_COOKIES_B64_1..N,
YOUTUBE_COOKIES_B64 or YOUTUBE_COOKIES. Keeps #HttpOnly_ cookies (the Google
auth cookies SID/PSID/LOGIN_INFO are HttpOnly) and validates the result with
yt-dlp's own cookie jar. Logs only counts and cookie NAMES, never values.
'''
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

log = logging.getLogger('agent.ytdlp_opts')

_COOKIE_FILE = Path(os.getenv('AGENT_DATA_DIR', '/tmp/video_agent_data')) / 'yt_cookies.txt'
_CHUNK_PREFIX = 'YOUTUBE_COOKIES_B64_'
_HTTP = '#HttpOnly_'
_WS = chr(65279) + ' ' + chr(9) + chr(10) + chr(13)
_AUTH_NAMES = ('SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-1PSID', '__Secure-3PSID', 'LOGIN_INFO')
log.warning('ytdlp_opts loaded: v4 (HttpOnly cookies kept, yt-dlp validation)')


def _decode_b64(value: str) -> str:
    compact = ''.join(value.split())
    compact += '=' * (-len(compact) % 4)
    return base64.b64decode(compact).decode('utf-8', 'replace')


def _load_cookie_text() -> str:
    chunks = []
    for key, value in os.environ.items():
        suffix = key[len(_CHUNK_PREFIX):]
        if key.startswith(_CHUNK_PREFIX) and suffix.isdigit() and value.strip():
            chunks.append((int(suffix), value))
    if chunks:
        chunks.sort()
        return _decode_b64(''.join(v for _, v in chunks))
    single = os.getenv('YOUTUBE_COOKIES_B64', '').strip()
    if single:
        return _decode_b64(single)
    raw = os.getenv('YOUTUBE_COOKIES', '').strip()
    escaped_nl = chr(92) + 'n'
    if escaped_nl in raw and chr(10) not in raw:
        raw = raw.replace(escaped_nl, chr(10))
    return raw


def _clean(value) -> str:
    return str(value).replace(chr(9), ' ').replace(chr(10), ' ').replace(chr(13), ' ')


def _json_to_netscape(text: str) -> str:
    '''Convert a JSON cookie export (list, or {cookies: [...]}) to Netscape text.'''
    data = json.loads(text)
    items = data if isinstance(data, list) else data.get('cookies', [])
    rows = ['# Netscape HTTP Cookie File']
    for c in items:
        if not isinstance(c, dict) or not c.get('name') or not c.get('domain'):
            continue
        domain = _clean(c['domain'])
        prefix = _HTTP if c.get('httpOnly') else ''
        sub = 'TRUE' if domain.startswith('.') else 'FALSE'
        try:
            exp = int(float(c.get('expirationDate') or c.get('expires') or 0))
        except (TypeError, ValueError):
            exp = 0
        if c.get('session') or exp < 0:
            exp = 0
        secure = 'TRUE' if c.get('secure') else 'FALSE'
        path = _clean(c.get('path') or '/')
        rows.append(chr(9).join([prefix + domain, sub, path, secure, str(exp), _clean(c['name']), _clean(c.get('value', ''))]))
    return chr(10).join(rows) + chr(10)


def apply_cookies(ydl_opts: dict) -> dict:
    '''Return ydl_opts with a validated cookiefile when cookie env vars are set.'''
    try:
        text = _load_cookie_text().strip()
        text = text.lstrip(_WS)
        if text[:1] == chr(34):
            try:
                inner = json.loads(text)
                if isinstance(inner, str):
                    text = inner.strip().lstrip(_WS)
            except ValueError:
                pass
        log.warning('ytdlp_opts: cookie text %d chars, first char code %d', len(text), ord(text[:1] or ' '))
        if text[:1] == '<':
            log.warning('YouTube cookies env var contains HTML markup, ignoring invalid cookie data.')
            return ydl_opts
        if text[:1] in ('[', '{'):
            try:
                text = _json_to_netscape(text)
                log.warning('Converted JSON cookie export to Netscape format.')
            except Exception as e:
                log.warning('Failed to convert JSON cookies: %s', e)
                return ydl_opts
    except Exception as exc:
        log.warning('Could not decode YouTube cookies from env: %s', exc)
        return ydl_opts

    if not text:
        log.info('No YouTube cookies configured (set YOUTUBE_COOKIES_B64_1..N).')
        return ydl_opts

    lines = ['# Netscape HTTP Cookie File']
    count = 0
    httponly = 0
    names = set()
    for line in text.splitlines():
        line = line.rstrip(chr(13) + chr(10))
        if not line.strip():
            continue
        is_http = line.startswith(_HTTP)
        if line.startswith('#') and not is_http:
            continue
        parts = line.split(chr(9))
        if len(parts) < 7:
            continue
        parts = parts[:7]
        if not parts[4].strip().lstrip('-').isdigit():
            continue
        lines.append(chr(9).join(parts))
        count += 1
        httponly += 1 if is_http else 0
        names.add(parts[5])

    if count == 0:
        log.warning('No valid Netscape cookie lines found in YouTube cookies env var.')
        return ydl_opts

    try:
        _COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIE_FILE.write_text(chr(10).join(lines) + chr(10), encoding='utf-8')
        _COOKIE_FILE.chmod(0o600)
    except OSError as exc:
        log.warning('Could not write YouTube cookies file: %s', exc)
        return ydl_opts

    try:
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(str(_COOKIE_FILE))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        log.warning('Cookie file failed yt-dlp validation: %s', exc)
        return ydl_opts

    ydl_opts['cookiefile'] = str(_COOKIE_FILE)
    log.warning('YouTube cookies loaded: %d cookies (%d HttpOnly); auth cookies present: %s', count, httponly, sorted(n for n in names if n in _AUTH_NAMES))
    return ydl_opts
