# -*- coding: utf-8 -*-
"""

    Copyright (C) 2014-2016 bromix (plugin.video.youtube)
    Copyright (C) 2016-2025 plugin.video.youtube

    SPDX-License-Identifier: GPL-2.0-only
    See LICENSES/GPL-2.0-only for more information.
"""

from __future__ import absolute_import, division, unicode_literals

import json

from .. import logging
from ..compatibility import xbmcgui
from ..constants import (
    ADDON_ID,
    PATHS,
    PLAY_FORCE_AUDIO,
    PLAY_PROMPT_QUALITY,
    PLAY_PROMPT_SUBTITLES,
    SEEK,
    VIDEO_ID,
)
from .methods import jsonrpc


__all__ = (
    'PROVIDER',
    'URL_TEMPLATE',
    'claim',
    'in_group',
    'open_menu',
    'propose',
    'register',
    'session_state',
    'tempo_route',
)


log = logging.getLogger(__name__)


# The SyncPlay provider contract, v1 (plan G2 + G3.6), published as
# docs/syncplay-provider-contract.md by plugin.video.kofin. The kofin service
# hosts the one SyncPlay engine per Kodi; a provider is anything that owns
# content and tells the engine what is playing (Claim), how a follower starts
# it (Register), and optionally asks for it as the group queue (Propose).
#
# Everything the engine does with that - pause/seek/resume choreography, ready
# reports, barriers, fine-sync pulses - runs against the global Kodi player and
# needs nothing from this add-on. So this module is the whole integration.
#
# The bus is deliberately unguarded: nothing irreversible crosses it, and a
# hostile local add-on could already write any window property.
VERSION = 1

PROVIDER = 'youtube'

REGISTER = 'SyncProvider.Register'
CLAIM = 'SyncProvider.Claim'
PROPOSE = 'SyncSession.Propose'
MENU = 'SyncSession.Menu'
# Service to everyone, and the reason this add-on listens at all: a ping that
# the session state changed. Also the kofin service's start-up announce, which
# is what tells a provider to register again.
STATE = 'SyncSession.State'

# Kodi delivers a NotifyAll message as 'Other.<message>'.
STATE_METHOD = '.'.join(('Other', STATE))

# The state mirror, read never trusted from a payload - a payload can be
# overtaken between send and handling, the property cannot. Not kofin-prefixed
# on purpose: the name is part of the contract and survives a future
# re-hosting of the engine.
STATE_PROPERTY = 'syncsession.state'
# The fine-sync route, published only while kofin is in a group with fine sync
# armed. This one *is* kofin's own name, and the contract names it literally.
TEMPO_PROPERTY = 'kofin.syncplay.tempo'

# How a follower starts a YouTube video. The engine treats this as opaque text
# and token-replaces {key} (URL-quoted) and {position_s} (whole seconds); it
# never str.formats it, so other braces would be left alone.
#
# The three false params matter: without them a follower start can raise the
# quality or subtitle prompt and sit there while the group waits. They are the
# same params _play_stream's own fallback URI sets.
URL_TEMPLATE = (
    'plugin://{addon_id}{path}/'
    '?{video_id}={{key}}'
    '&{seek}={{position_s}}'
    '&{ask_quality}=false'
    '&{prompt_subtitles}=false'
    '&{force_audio}=false'
).format(
    addon_id=ADDON_ID,
    path=PATHS.PLAY,
    video_id=VIDEO_ID,
    seek=SEEK,
    ask_quality=PLAY_PROMPT_QUALITY,
    prompt_subtitles=PLAY_PROMPT_SUBTITLES,
    force_audio=PLAY_FORCE_AUDIO,
)

# Jellyfin ticks are 100ns units; the engine speaks them everywhere a position
# or a runtime crosses the wire.
TICKS_PER_SECOND = 10000000

# The engine truncates a descriptor name at 256 itself. Doing it here keeps the
# payload honest about what will be shown.
MAX_NAME_LENGTH = 256


def _send(message, data):
    """One contract notification, from this add-on's own id.

    No 'no_response' here, deliberately. It looks like the right economy for
    a notification nobody reads a reply to, but it drops the request 'id' -
    and Kodi discards an id-less JSONRPC.NotifyAll without executing it and
    without logging anything. executeJSONRPC still returns, the caller still
    sees success, and the message simply never reaches the bus. Measured:
    of two otherwise identical Registers, only the one carrying an id
    reached the engine. The rest of this add-on's send_notification helpers
    keep the id for the same reason.

    Never raises: this is called from the play route and from the
    notification thread, and a bus that is not listening must not be able to
    break either.
    """
    try:
        jsonrpc(method='JSONRPC.NotifyAll',
                params={
                    'sender': ADDON_ID,
                    'message': message,
                    'data': dict(data, v=VERSION),
                })
    except Exception:
        log.exception('SyncPlay {message} failed', message=message)
        return False
    log.debug('SyncPlay sent {message}', message=message)
    return True


def _read_property(name):
    """A foreign window property, parsed, or None.

    Read raw: these names belong to the contract, not to this add-on, so they
    must not pick up the '<addon id>-' prefix that XbmcContextUI adds.
    """
    try:
        value = xbmcgui.Window(10000).getProperty(name)
        if not value:
            return None
        value = json.loads(value)
    except (TypeError, ValueError):
        log.debug('SyncPlay property {name!r} unparseable', name=name)
        return None
    return value if isinstance(value, dict) else None


def session_state():
    """The published session state, or an empty dict when no engine is here.

    An empty dict is the answer for 'kofin is not installed', 'its service is
    not running' and 'it stopped and cleared the property' alike - all three
    mean the same thing to this add-on.
    """
    state = _read_property(STATE_PROPERTY)
    if not state or state.get('v') != VERSION:
        return {}
    return state


def in_group(state=None):
    if state is None:
        state = session_state()
    return bool(state.get('in_group'))


def tempo_route():
    """kofin's fine-sync route, or None.

    Published only while the session has fine sync armed. The tempo file named
    here is the one the engine pulses; a ListItem routed through any other file
    gets command-only sync, which is always safe but is not why we are here.
    """
    route = _read_property(TEMPO_PROPERTY)
    if not route or not route.get('file'):
        return None
    return route


def register():
    """Offer this add-on as a provider the engine can start content on.

    Sent on service start and again on every SyncSession.State: registrations
    live only as long as a kofin service generation and are deliberately not
    persisted, so a provider that only registered once would silently stop
    being startable the first time that service restarted.
    """
    return _send(REGISTER, {
        'provider': PROVIDER,
        'play': {
            'url_template': URL_TEMPLATE,
            # Video playlist. The add-on can play audio-only, but a follower
            # start is a group watching a video together.
            'audio': False,
        },
    })


def claim(video_id, name=None, duration=None, tempo=None):
    """Tell the engine what this add-on just put on screen.

    Without a claim the engine cannot tell a member's own playback from the
    group's, so it demotes them to spectator - the right default for an add-on
    that has not opted in, and the thing this call exists to stop.

    'name' and 'duration' are what a later Propose needs to build its
    external-content descriptor; unknown fields are ignored by design, so they
    are harmless against an engine that predates that.
    """
    data = {
        'provider': PROVIDER,
        'key': video_id,
        # YouTube hands out direct HTTP streams; nothing here is remuxed or
        # transcoded on our behalf.
        'play_method': 'DirectPlay',
    }

    if name:
        data['name'] = name[:MAX_NAME_LENGTH]

    ticks = _runtime_ticks(duration)
    if ticks:
        data['runtime_ticks'] = ticks

    if tempo:
        data['tempo'] = tempo

    return _send(CLAIM, data)


def propose(video_id, name=None, duration=None, position=0):
    """Ask for this video as the group's queue.

    The programmatic form of the engine's hold-and-propose. A non-jellyfin key
    goes out as an external-content descriptor, which needs the server to have
    negotiated the ExternalContent capability - without it the engine refuses
    with a log line rather than downgrading to a key the group cannot resolve.
    Ignored outside a group.
    """
    data = {
        'provider': PROVIDER,
        'key': video_id,
        'position_ticks': _runtime_ticks(position) or 0,
    }

    if name:
        data['name'] = name[:MAX_NAME_LENGTH]

    ticks = _runtime_ticks(duration)
    if ticks:
        data['runtime_ticks'] = ticks

    return _send(PROPOSE, data)


def open_menu():
    """Open the engine's group menu - how a provider UI offers 'watch
    together' without knowing anything about groups itself."""
    return _send(MENU, {})


def _runtime_ticks(seconds):
    """Seconds as engine ticks, or 0 for anything unusable.

    A zero runtime is not a failure on the wire: the server reads it as
    'unknown' and leaves positions on the item unbounded rather than clamping
    them, which is the correct answer for a live stream.
    """
    try:
        ticks = int(float(seconds or 0) * TICKS_PER_SECOND)
    except (TypeError, ValueError):
        return 0
    return ticks if ticks > 0 else 0
