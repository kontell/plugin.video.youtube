# -*- coding: utf-8 -*-
"""

    Copyright (C) 2026 plugin.video.youtube

    SPDX-License-Identifier: GPL-2.0-only
    See LICENSES/GPL-2.0-only for more information.
"""

from __future__ import absolute_import, division, unicode_literals

from re import compile as re_compile


_ATTRIBUTE_RE = re_compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')

EVENT_TAG = '#EXT-X-PLAYLIST-TYPE:EVENT'


def _attributes(line):
    return {
        name: value
        for name, value in _ATTRIBUTE_RE.findall(line.partition(':')[2])
    }


def _unquote(value):
    if value and value[0] == '"' == value[-1]:
        return value[1:-1]
    return value


def _tag(name, attributes):
    return ''.join((
        name,
        ':',
        ','.join(['='.join(item) for item in attributes.items()]),
    ))


def _video_details(attributes):
    """The nominal height, frame rate, codec family and HDR state of a
    variant, in the terms of the add-on's stream features."""
    width, _, height = attributes.get('RESOLUTION', '0x0').partition('x')
    try:
        # Portrait video is rated by its short side, as the quality setting
        # means 1080p for a 1080x1920 Short as much as for 1920x1080.
        height = min(int(width), int(height)) or int(height)
    except ValueError:
        height = 0

    try:
        fps = float(attributes.get('FRAME-RATE', 0))
    except ValueError:
        fps = 0

    codecs = _unquote(attributes.get('CODECS', ''))
    codec = None
    for codec_id in codecs.split(','):
        codec_id = codec_id.strip()
        if codec_id.startswith(('avc1', 'avc3')):
            codec = 'avc1'
        elif codec_id.startswith('vp09'):
            codec = 'vp9'
        elif codec_id.startswith('av01'):
            codec = 'av01'
        else:
            continue
        break

    hdr = attributes.get('VIDEO-RANGE', 'SDR') not in ('SDR', '')
    return height, fps, codec, hdr


def filter_master(manifest,
                  max_height=None,
                  stream_features=None,
                  languages=None,
                  default_language=None,
                  rewrite_uri=None):
    """Trim a YouTube HLS master playlist to what this add-on would choose,
    for playback by Kodi's own FFmpeg demuxer.

    FFmpeg opens and probes every rendition in a master playlist before it
    plays anything, and Kodi abandons the open after 30 seconds. YouTube's
    on-demand masters list up to 26 video variants in three codec families,
    two copies of every audio track (HE-AAC and AAC-LC), every dubbed
    language and every subtitle, which takes longer than that to probe on a
    dubbed video.

    :param manifest: the master playlist text
    :param max_height: the tallest variant to keep, by its short side
    :param stream_features: the add-on's stream features: a codec family
                            ('avc1', 'vp9', 'av01'), 'hdr' or 'hfr' missing
                            from it drops the variants that need it
    :param languages: audio languages (ISO 639-1) to keep besides the
                      original track; None keeps every language
    :param default_language: the audio language to flag as the default
                             rendition; the original track when not offered
    :param rewrite_uri: called with each kept media playlist URI, returns the
                        URI to use instead
    :return: the trimmed master playlist text, or None when nothing in it is
             playable within the constraints
    """
    lines = manifest.splitlines()

    header = []
    variants = []
    media = []
    pending = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if pending is not None:
            if line.startswith('#'):
                continue
            variants.append((pending, line))
            pending = None
        elif line.startswith('#EXT-X-STREAM-INF:'):
            pending = _attributes(line)
        elif line.startswith('#EXT-X-MEDIA:'):
            media.append(_attributes(line))
        elif line.startswith('#EXT-X-I-FRAME-STREAM-INF:'):
            continue
        elif not variants and not media:
            header.append(line)

    if stream_features is None:
        stream_features = frozenset(('avc1', 'vp9', 'av01', 'hdr', 'hfr'))

    def _allowed(_variants, codecs_matter):
        allowed = []
        for attributes, uri in _variants:
            height, fps, codec, hdr = _video_details(attributes)
            if max_height and height > max_height:
                continue
            if hdr and 'hdr' not in stream_features:
                continue
            if fps > 30 and 'hfr' not in stream_features:
                continue
            if (codecs_matter
                    and codec
                    and codec not in stream_features):
                continue
            allowed.append((attributes, uri))
        return allowed

    # A codec family left out of the stream features is a preference, as it
    # is for MPD playback: fall back to it rather than play nothing.
    kept = _allowed(variants, True) or _allowed(variants, False)
    if not kept:
        return None

    # Kodi plays the variant with the highest bandwidth, so the audio group
    # it references is the one worth keeping. The other group is the same
    # tracks as HE-AAC, referenced only by low resolution duplicates.
    def _bandwidth(_variant):
        try:
            return int(_variant[0].get('BANDWIDTH', 0))
        except ValueError:
            return 0

    kept.sort(key=_bandwidth, reverse=True)
    audio_group = kept[0][0].get('AUDIO')
    if audio_group:
        kept = [variant for variant in kept
                if variant[0].get('AUDIO') == audio_group]

    # Kodi opens a master twice and probes every rendition in it each time,
    # so each variant costs startup time. Keep the ladder of the codec and
    # range Kodi will play, the best variant's, with one variant per height
    # and frame rate: YouTube sometimes lists two encodes of the same one.
    family = _video_details(kept[0][0])[2:]
    unique = {}
    for variant in kept:
        key = _video_details(variant[0])
        if key[2:] == family and key not in unique:
            unique[key] = variant
    # Best first. FFmpeg reports an EVENT playlist's growing duration from
    # the first variant only, and only the variant playing is reloaded.
    kept = sorted(unique.values(), key=_bandwidth, reverse=True)

    audio = []
    if audio_group:
        audio = [attributes for attributes in media
                 if attributes.get('TYPE') == 'AUDIO'
                 and attributes.get('GROUP-ID') == audio_group]

    if audio and len(audio) > 1:
        def _language(attributes):
            return _unquote(attributes.get('LANGUAGE', '')).split('-')[0]

        def _is_original(attributes):
            return 'original' in _unquote(attributes.get('NAME', '')).lower()

        original = [attributes for attributes in audio
                    if _is_original(attributes)]
        if languages is not None:
            languages = frozenset(languages)
            audio = [attributes for attributes in audio
                     if attributes in original
                     or _language(attributes) in languages] or audio

        default = None
        if default_language:
            default = next((attributes for attributes in audio
                            if _language(attributes) == default_language),
                           None)
        if default is None and original:
            default = original[0]
        if default is not None:
            for attributes in audio:
                attributes['DEFAULT'] = 'NO'
                attributes.pop('AUTOSELECT', None)
            default['DEFAULT'] = 'YES'
            default['AUTOSELECT'] = 'YES'
            audio.remove(default)
            audio.insert(0, default)

    output = header[:]
    if not output or output[0] != '#EXTM3U':
        output.insert(0, '#EXTM3U')

    for attributes in audio:
        if rewrite_uri and 'URI' in attributes:
            attributes['URI'] = '"%s"' % rewrite_uri(
                _unquote(attributes['URI'])
            )
        output.append(_tag('#EXT-X-MEDIA', attributes))

    for attributes, uri in kept:
        # Subtitles are left to the add-on, which fetches them itself, so the
        # demuxer has no rendition group to probe for them.
        attributes.pop('SUBTITLES', None)
        attributes.pop('CLOSED-CAPTIONS', None)
        output.append(_tag('#EXT-X-STREAM-INF', attributes))
        output.append(rewrite_uri(uri) if rewrite_uri else uri)

    output.append('')
    return '\n'.join(output)


_PLAYLIST_TAGS = (
    '#EXTM3U',
    '#EXT-X-VERSION',
    '#EXT-X-TARGETDURATION',
    '#EXT-X-MEDIA-SEQUENCE',
    '#EXT-X-DISCONTINUITY-SEQUENCE',
    '#EXT-X-PLAYLIST-TYPE',
    '#EXT-X-INDEPENDENT-SEGMENTS',
    '#EXT-X-START',
)


def _split_media_playlist(playlist):
    """The playlist tags, the media sequence and discontinuity sequence
    numbers, and each segment as the block of lines that ends with its URI.
    """
    header = []
    segments = []
    block = []
    sequence = 0
    discontinuity = 0
    ended = False
    for line in playlist.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_PLAYLIST_TAGS):
            if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                sequence = int(line.partition(':')[2])
            elif line.startswith('#EXT-X-DISCONTINUITY-SEQUENCE:'):
                discontinuity = int(line.partition(':')[2])
            elif not line.startswith('#EXT-X-PLAYLIST-TYPE'):
                header.append(line)
        elif line.startswith('#EXT-X-ENDLIST'):
            ended = True
        elif line.startswith('#'):
            block.append(line)
        else:
            block.append(line)
            segments.append('\n'.join(block))
            block = []
    return header, sequence, discontinuity, segments, ended


class EventPlaylist(object):
    """A live media playlist kept append-only for Kodi's FFmpeg demuxer.

    FFmpeg's HLS demuxer seeks only in a playlist that is finished or of type
    EVENT, and YouTube's live playlists carry no type. Declaring one an EVENT
    is not enough on its own: YouTube slides the window, and FFmpeg maps time
    0 to the first segment it was given while Kodi keeps that start for the
    whole playback, so once the window has moved by its own length the clock
    sticks at the end and the end is no longer live.

    YouTube keeps serving segments after they leave the playlist, so this
    keeps every segment seen since playback started and serves the playlist
    as a real EVENT playlist, one that only grows.
    """

    # 12 hours of 5 second segments, YouTube's longest DVR window
    MAX_SEGMENTS = 8640

    def __init__(self):
        self._first = None
        self._discontinuity = 0
        self._segments = []

    def update(self, playlist):
        if '#EXT-X-MAP' in playlist or not playlist.startswith('#EXTM3U'):
            return mark_as_event(playlist)

        (header, sequence, discontinuity,
         segments, ended) = _split_media_playlist(playlist)

        known_end = (self._first + len(self._segments)
                     if self._first is not None else
                     None)
        if (known_end is None
                or sequence < self._first
                or sequence > known_end):
            # The first load, or a window that moved past everything kept
            # (a pause longer than the window): start over from this one.
            self._first = sequence
            self._discontinuity = discontinuity
            self._segments = segments
        else:
            offset = sequence - self._first
            self._segments[offset:] = segments

        excess = len(self._segments) - self.MAX_SEGMENTS
        if excess > 0:
            discontinuities = sum(
                1
                for segment in self._segments[:excess]
                for line in segment.split('\n')
                if line == '#EXT-X-DISCONTINUITY'
            )
            del self._segments[:excess]
            self._first += excess
            self._discontinuity += discontinuities

        output = [header[0] if header else '#EXTM3U', EVENT_TAG]
        output.extend(header[1:])
        output.append('#EXT-X-MEDIA-SEQUENCE:%d' % self._first)
        output.append('#EXT-X-DISCONTINUITY-SEQUENCE:%d' % self._discontinuity)
        output.extend(self._segments)
        if ended:
            output.append('#EXT-X-ENDLIST')
        output.append('')
        return '\n'.join(output)


def mark_as_event(playlist):
    """Declare a live media playlist an EVENT playlist.

    FFmpeg's HLS demuxer seeks only in a playlist that is finished or of type
    EVENT. YouTube's live playlists carry no type, so Kodi can neither pause
    nor skip in them, although each one holds the stream's DVR window. As an
    EVENT, FFmpeg maps the playlist's first segment to time 0 and reports the
    window as the duration, and follows the window's start as YouTube drops
    old segments from it.
    """
    if ('#EXT-X-PLAYLIST-TYPE' in playlist
            or '#EXT-X-ENDLIST' in playlist
            or not playlist.startswith('#EXTM3U')):
        return playlist
    first, _, rest = playlist.partition('\n')
    return '\n'.join((first, EVENT_TAG, rest))
