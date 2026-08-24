# -*- coding: utf-8 -*-
"""

    Copyright (C) 2014-2016 bromix (plugin.video.youtube)
    Copyright (C) 2016-2025 plugin.video.youtube

    SPDX-License-Identifier: GPL-2.0-only
    See LICENSES/GPL-2.0-only for more information.
"""

from __future__ import absolute_import, division, unicode_literals

import json
import os
import re

from ..compatibility import xbmcvfs
from .methods import jsonrpc


OWNER = 'plugin.video.youtube'

__all__ = (
    'ACTIVE_FILE',
    'CONFIG_FILE',
    'OWNER',
    'TEMPO_FILE',
    'TEMPO_MAX',
    'TEMPO_MIN',
    'TEMPO_STEP',
    'arm_speed_keys',
    'disarm_speed_keys',
    'set_tempo',
    'tempo_supports_video',
)


# inputstream.tempo polls TEMPO_FILE every 250ms for a new rate, and its
# Page Up/Page Down/=/s keymap reads the step and range from CONFIG_FILE.
# Both are per-add-on: KoShelf drives the same add-on, and on the shared
# paths an audiobook at 2.0x and a video at 1.5x overwrite each other's
# rate. The sentinel points the keymap at whichever set belongs to the item
# that is playing.
TEMPO_FILE = xbmcvfs.translatePath(
    'special://temp/inputstream_tempo.' + OWNER
)
CONFIG_FILE = xbmcvfs.translatePath(
    'special://temp/inputstream_tempo_config.' + OWNER
)
# The sentinel itself stays shared - it is the single "the keys are live"
# flag. The keymap does nothing at all unless it exists, and it binds
# FullscreenVideo as well as the music windows, so one left behind after
# playback would hijack those keys during ordinary playback.
ACTIVE_FILE = xbmcvfs.translatePath('special://temp/inputstream_tempo_active')

# The range the playback speed settings offer, and the step the keymap moves
# in. Keep in step with the slider constraints in resources/settings.xml.
TEMPO_MIN = 0.5
TEMPO_MAX = 5.0
TEMPO_STEP = 0.10

# Rate-shifting video arrived in inputstream.tempo v21.4.0 and v22.4.0: the
# major version tracks the Kodi release, and the rest moves in step across
# both channels, so the check is on everything after the major. Older builds
# accept the same properties but shift the audio alone, which plays video out
# of sync rather than refusing it.
VIDEO_MIN_VERSION = (4, 0)


def tempo_supports_video(addon_id='inputstream.tempo'):
    """Whether an installed and enabled inputstream.tempo can shift video."""
    try:
        addon = jsonrpc(
            method='Addons.GetAddonDetails',
            params={
                'addonid': addon_id,
                'properties': ['enabled', 'version'],
            },
        )['result']['addon']
        if addon['enabled'] is not True:
            return False
        # Everything after the major, padded so a two-part '21.4' compares
        # as 21.4.0 rather than falling short of the minimum.
        parts = re.findall(r'\d+', addon['version'])[1:3]
        if not parts:
            return False
        version = tuple(int(part) for part in parts)
        version += (0,) * (len(VIDEO_MIN_VERSION) - len(version))
    except (KeyError, TypeError, ValueError):
        return False
    return version >= VIDEO_MIN_VERSION


def set_tempo(tempo):
    """Write the rate the add-on polls for, atomically as it expects."""
    temp_path = TEMPO_FILE + '.tmp'
    try:
        with open(temp_path, 'w') as tempo_file:
            tempo_file.write(str(tempo))
        os.rename(temp_path, TEMPO_FILE)
    except (IOError, OSError):
        return False
    return True


def arm_speed_keys(tempo=1.0,
                   minimum=TEMPO_MIN,
                   maximum=TEMPO_MAX,
                   step=TEMPO_STEP):
    """Offer the tempo keymap a step and range, and enable it.

    The rate file is seeded with the rate this item actually starts at. The
    keymap steps from whatever it finds there, and the add-on polls it from
    the moment it opens - so a file left at another rate by the last item
    would both misplace the first key press and pull this one off its
    configured speed a quarter-second in.
    """
    try:
        with open(CONFIG_FILE, 'w') as config_file:
            json.dump({
                'step': step,
                'min': minimum,
                'max': maximum,
            }, config_file)
        with open(ACTIVE_FILE, 'w') as active_file:
            active_file.write(
                'addon={owner}\ntempo_file={tempo}\nconfig_file={config}\n'
                .format(owner=OWNER, tempo=TEMPO_FILE, config=CONFIG_FILE)
            )
    except (IOError, OSError, TypeError, ValueError):
        return False
    return set_tempo(tempo)


def _sentinel_owner(content):
    """The add-on named in a sentinel, or None for one this add-on did not
    write. Matches how inputstream.tempo's speed.py parses it."""
    for line in content.splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() == 'addon':
            return value.strip()
    return None


def disarm_speed_keys(owner=OWNER):
    """Make the tempo keymap inert again, if this add-on armed it.

    The sentinel is a single file shared by every add-on that drives
    inputstream.tempo, and PlayerMonitor sees every player event on the box
    rather than only this add-on's - so an unconditional remove would take
    the speed keys away from a KoShelf audiobook whenever any other
    playback ended.
    """
    try:
        with open(ACTIVE_FILE) as active_file:
            content = active_file.read()
        if _sentinel_owner(content) != owner:
            return False
        os.remove(ACTIVE_FILE)
    except (IOError, OSError):
        return False
    return True
