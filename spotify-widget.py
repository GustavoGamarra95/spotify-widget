#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
gi.require_version('Gst', '1.0')
gi.require_foreign('cairo')
from gi.repository import Gtk, GLib, GdkPixbuf, Gdk, Pango, PangoCairo, Gst
import cairo
import subprocess
import urllib.request
import urllib.parse
import os
import json
import threading
import math
import time
import re
import hmac
import hashlib
import struct

Gst.init(None)

# ── Config ────────────────────────────────────────────────────────────────────
WAL_COLORS   = os.path.expanduser('~/.cache/wal/colors.json')
SPOTIFY_CONF = os.path.expanduser('~/.config/spotify-widget/config.json')
CACHE_DIR    = os.path.expanduser('~/.cache/spotify-widget')
WIN_W, WIN_H = 300, 560
INFO_H       = 220   # altura aproximada de la sección info (badge+título+slider+controles+vol)
PADDING      = 44    # widget-box padding top+bottom + spacing entre secciones
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Spotipy (Web API) ─────────────────────────────────────────────────────────
sp = None
def init_spotipy():
    global sp
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
        with open(SPOTIFY_CONF) as f:
            cfg = json.load(f)
        scope = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
        auth = SpotifyOAuth(
            client_id     = cfg['client_id'],
            client_secret = cfg['client_secret'],
            redirect_uri  = cfg['redirect_uri'],
            scope         = scope,
            cache_path    = os.path.join(CACHE_DIR, '.spotify_token'),
            open_browser  = True,
        )
        sp = spotipy.Spotify(auth_manager=auth)
        sp.current_playback()
        print("Spotipy: Web API OK")
    except Exception as e:
        print(f"Spotipy init error: {e}")
        sp = None

def _get_sp_token():
    """Return current spotipy access token, refreshing if needed."""
    if not sp or not sp.auth_manager:
        return None
    try:
        info = sp.auth_manager.get_cached_token()
        if not info:
            return None
        if sp.auth_manager.is_token_expired(info):
            info = sp.auth_manager.refresh_access_token(info['refresh_token'])
        return info.get('access_token')
    except:
        return None

# ── Pywal colors ──────────────────────────────────────────────────────────────
def load_wal():
    try:
        with open(WAL_COLORS) as f:
            d = json.load(f)
        return {'bg': d['special']['background'],
                'fg': d['special']['foreground'],
                'c1': d['colors']['color6']}
    except:
        return {'bg':'#0d0d1a','fg':'#c2c4c7','c1':'#3064A3'}

def hex_to_rgb_f(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))

def fmt_time(ms):
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"

def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout.strip()
    except:
        return ''

# ── Protobuf helpers (Canvas API) ─────────────────────────────────────────────
def _pb_varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)

def _pb_string(field, s):
    data = s.encode('utf-8')
    return _pb_varint((field << 3) | 2) + _pb_varint(len(data)) + data

def _pb_message(field, content):
    return _pb_varint((field << 3) | 2) + _pb_varint(len(content)) + content

def _pb_encode_canvas_request(track_uri):
    """Encode EntityCanvazRequest { tracks: [{ track_uri }] }"""
    inner = _pb_string(1, track_uri)
    return _pb_message(1, inner)

def _pb_read_varint(data, pos):
    result = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos

def _pb_parse(data):
    """Parse raw protobuf bytes → {field_num: [bytes|int, ...]}"""
    fields = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _pb_read_varint(data, pos)
            field, wire = tag >> 3, tag & 7
            if wire == 0:
                val, pos = _pb_read_varint(data, pos)
            elif wire == 1:
                val = data[pos:pos+8]; pos += 8
            elif wire == 2:
                length, pos = _pb_read_varint(data, pos)
                val = data[pos:pos+length]; pos += length
            elif wire == 5:
                val = data[pos:pos+4]; pos += 4
            else:
                break
            fields.setdefault(field, []).append(val)
        except:
            break
    return fields

def _pb_decode_canvas_url(response_bytes):
    """Extract first canvas video URL from EntityCanvazResponse."""
    top = _pb_parse(response_bytes)
    for canvas_bytes in top.get(1, []):       # field 1 = canvases[]
        canvas = _pb_parse(canvas_bytes)
        for url_bytes in canvas.get(2, []):    # field 2 = url
            try:
                url = url_bytes.decode('utf-8')
                if url.startswith('http'):
                    return url
            except:
                pass
    return None

# ── Canvas Token (SP_DC → internal access token via TOTP) ────────────────────
_canvas_token_cache = {'token': None, 'expires': 0}

def _totp_generate(secret_bytes, timestamp_ms):
    counter = struct.pack('>Q', int(timestamp_ms // 1000 // 30))
    h = hmac.new(secret_bytes, counter, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack('>I', h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)

def _fetch_totp_secret():
    url = ('https://raw.githubusercontent.com/xyloflake/spot-secrets-go'
           '/refs/heads/main/secrets/secretDict.json')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        secrets = json.loads(r.read().decode())
    version = str(max(int(k) for k in secrets))
    data = secrets[version]
    mapped = [v ^ ((i % 33) + 9) for i, v in enumerate(data)]
    hex_data = ''.join(str(x) for x in mapped).encode('utf-8').hex()
    return bytes.fromhex(hex_data), version

def _get_canvas_token(sp_dc):
    global _canvas_token_cache
    now = time.time()
    if _canvas_token_cache['token'] and now < _canvas_token_cache['expires']:
        return _canvas_token_cache['token']
    try:
        secret_bytes, version = _fetch_totp_secret()
        local_ms = int(now * 1000)

        ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        hdrs = {'User-Agent': ua, 'Origin': 'https://open.spotify.com/',
                'Referer': 'https://open.spotify.com/', 'Cookie': f'sp_dc={sp_dc}'}

        req = urllib.request.Request('https://open.spotify.com/api/server-time', headers=hdrs)
        with urllib.request.urlopen(req, timeout=8) as r:
            server_sec = int(json.loads(r.read().decode())['serverTime'])
        server_ms = server_sec * 1000

        totp_local  = _totp_generate(secret_bytes, local_ms)
        totp_server = _totp_generate(secret_bytes, server_ms)

        params = urllib.parse.urlencode({
            'reason': 'init', 'productType': 'mobile-web-player',
            'totp': totp_local, 'totpVer': version, 'totpServer': totp_server,
        })
        req = urllib.request.Request(
            f'https://open.spotify.com/api/token?{params}', headers=hdrs)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        token = data.get('accessToken')
        exp   = data.get('accessTokenExpirationTimestampMs', 0)
        if token:
            _canvas_token_cache = {'token': token, 'expires': exp / 1000 - 60}
            print('[CanvasToken] OK')
        return token
    except Exception as e:
        print(f'[CanvasToken] {e}')
        return None


# ── Canvas Client ─────────────────────────────────────────────────────────────
class CanvasClient:
    _API = 'https://spclient.wg.spotify.com/canvaz-cache/v0/canvases'

    def get_canvas(self, track_id, sp_dc, callback):
        threading.Thread(
            target=self._fetch,
            args=(track_id, sp_dc, callback),
            daemon=True
        ).start()

    def _fetch(self, track_id, sp_dc, callback):
        url = self._get(track_id, sp_dc)
        GLib.idle_add(callback, url)

    def _get(self, track_id, sp_dc):
        try:
            token = _get_canvas_token(sp_dc)
            if not token:
                return None
            body = _pb_encode_canvas_request(f'spotify:track:{track_id}')
            req  = urllib.request.Request(
                self._API, data=body,
                headers={
                    'Authorization':   f'Bearer {token}',
                    'Content-Type':    'application/x-protobuf',
                    'Accept':          'application/protobuf',
                    'Accept-Language': 'en',
                    'User-Agent':      'Spotify/9.0.34.593 iOS/18.4 (iPhone15,3)',
                }
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status == 200:
                    return _pb_decode_canvas_url(r.read())
        except Exception as e:
            print(f'[Canvas] {e}')
        return None


# ── Video Player (GStreamer gtksink) ──────────────────────────────────────────
class VideoPlayer:
    def __init__(self, width, height):
        self._uri      = None
        self._pipeline = Gst.ElementFactory.make('playbin', None)

        sink_bin   = Gst.Bin.new('video-sink-bin')
        videoscale = Gst.ElementFactory.make('videoscale', None)
        capsfilter = Gst.ElementFactory.make('capsfilter', None)
        self._sink = Gst.ElementFactory.make('gtksink', None)

        self._capsfilter = capsfilter
        self._capsfilter.set_property('caps',
                                      Gst.Caps.from_string(f'video/x-raw,width={width},height={height}'))
        self._sink.set_property('force-aspect-ratio', False)

        for el in (videoscale, self._capsfilter, self._sink):
            sink_bin.add(el)
        videoscale.link(self._capsfilter)
        self._capsfilter.link(self._sink)
        sink_bin.add_pad(Gst.GhostPad.new('sink', videoscale.get_static_pad('sink')))

        self._pipeline.set_property('volume', 0.0)
        self._pipeline.set_property('mute', True)
        self._pipeline.set_property('video-sink', sink_bin)

        self.widget = self._sink.props.widget
        self.widget.set_size_request(width, height)
        self.widget.set_halign(Gtk.Align.FILL)
        self.widget.set_valign(Gtk.Align.FILL)
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_message)

    def update_size(self, width, height):
        self._capsfilter.set_property('caps',
                                      Gst.Caps.from_string(f'video/x-raw,width={width},height={height}'))
        self.widget.set_size_request(width, height)

    def play(self, uri):
        self._uri = uri
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline.set_property('uri', uri)
        self._pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self._pipeline.set_state(Gst.State.NULL)
        self._uri = None

    def destroy(self):
        self.stop()

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.EOS and self._uri:
            self._pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0
            )
        elif msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            print(f'[GStreamer] {err}')


# ── Wave Slider ───────────────────────────────────────────────────────────────
WAVE_AMPLITUDE = 4.0
WAVE_FREQUENCY = 0.08
SIDE_PADDING   = 15
SLIDER_HEIGHT  = 28

class WaveSlider(Gtk.DrawingArea):
    def __init__(self, on_seek_cb, elapsed_label):
        super().__init__()
        self.set_size_request(-1, SLIDER_HEIGHT)
        self.get_style_context().add_class('wave-slider')
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK   |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK
        )
        self._on_seek     = on_seek_cb
        self._elapsed     = elapsed_label
        self._position    = 0.0
        self._duration    = 1.0
        self._is_playing  = False
        self._is_dragging = False
        self._phase       = 0.0
        self._tick_id     = None
        self._last_frame  = time.monotonic()
        self._track_hash  = None

        self.connect('draw',                 self._on_draw)
        self.connect('button-press-event',   self._on_press)
        self.connect('button-release-event', self._on_release)
        self.connect('motion-notify-event',  self._on_motion)
        self.connect('destroy',              lambda *_: self._stop_animation())

    def update_metadata(self, duration_ms, is_playing, pos_ms, track_hash=None):
        if self._is_dragging:
            return
        self._duration = max(1.0, duration_ms / 1000.0)

        if track_hash and track_hash != self._track_hash:
            self._track_hash = track_hash
            self._position   = pos_ms / 1000.0
            self._phase      = 0.0
        elif not self._is_dragging:
            if abs(pos_ms / 1000.0 - self._position) > 3.0:
                self._position = pos_ms / 1000.0

        if self._is_playing != is_playing:
            self._is_playing = is_playing
            if is_playing:
                self._last_frame = time.monotonic()
                self._start_animation()
            else:
                self._stop_animation()
                self.queue_draw()
        elif is_playing and not self._tick_id:
            self._start_animation()

    def get_position_ms(self):
        return self._position * 1000.0

    def reset(self):
        self._position = 0.0
        self._phase    = 0.0
        if self._elapsed:
            self._elapsed.set_text('0:00')
        self.queue_draw()

    def _start_animation(self):
        if self._tick_id:
            return
        self._last_frame = time.monotonic()
        self._tick_id = GLib.timeout_add(16, self._tick)

    def _stop_animation(self):
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _tick(self):
        now = time.monotonic()
        dt  = now - self._last_frame
        self._last_frame = now
        if self._is_playing and not self._is_dragging:
            self._position = min(self._position + dt, self._duration)
            self._phase   += 0.04
            if self._elapsed:
                self._elapsed.set_text(fmt_time(self._position * 1000))
        self.queue_draw()
        return True

    def _ratio_from_x(self, x):
        return max(0.0, min(1.0, (x - SIDE_PADDING) / max(1, self.get_allocated_width() - SIDE_PADDING * 2)))

    def _on_press(self, w, e):
        if e.button == 1:
            self._is_dragging = True
            self._position    = self._ratio_from_x(e.x) * self._duration
            self.queue_draw()
        return True

    def _on_motion(self, w, e):
        if self._is_dragging:
            self._position = self._ratio_from_x(e.x) * self._duration
            if self._elapsed:
                self._elapsed.set_text(fmt_time(self._position * 1000))
            self.queue_draw()
        return True

    def _on_release(self, w, e):
        if self._is_dragging and e.button == 1:
            self._is_dragging = False
            self._position    = self._ratio_from_x(e.x) * self._duration
            if self._on_seek:
                self._on_seek(self._position * 1000.0)
            self.queue_draw()
        return True

    def _on_draw(self, widget, cr):
        w         = self.get_allocated_width()
        h         = self.get_allocated_height()
        center_y  = h / 2.0
        ratio     = min(1.0, max(0.0, self._position / self._duration))
        current_x = SIDE_PADDING + (w - SIDE_PADDING * 2) * ratio
        thickness = 3.5

        cr.set_source_rgba(1, 1, 1, 0.25)
        cr.set_line_width(thickness)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(current_x, center_y)
        cr.line_to(w - SIDE_PADDING, center_y)
        cr.stroke()

        dist = current_x - SIDE_PADDING
        if dist > 0:
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.set_line_width(thickness)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(SIDE_PADDING, center_y)
            for i in range(int(dist) + 1):
                x = SIDE_PADDING + i
                if self._is_playing and not self._is_dragging:
                    y = center_y + math.sin(i * WAVE_FREQUENCY - self._phase) * WAVE_AMPLITUDE * min(1.0, i / 15.0)
                else:
                    y = center_y
                cr.line_to(x, y)
            cr.stroke()

        cr.set_source_rgba(1, 1, 1, 1.0)
        cr.arc(current_x, center_y, 5.5, 0, 2 * math.pi)
        cr.fill()
        return False


# ── Lyrics Client ─────────────────────────────────────────────────────────────
class LyricsClient:
    _LRC_RE = re.compile(r'\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)')

    def get_lyrics(self, title, artist, album, duration_sec, callback):
        threading.Thread(
            target=self._fetch,
            args=(title, artist, album, duration_sec, callback),
            daemon=True
        ).start()

    def _fetch(self, title, artist, album, duration_sec, callback):
        GLib.idle_add(callback, self._get(title, artist, album, duration_sec))

    def _request(self, url):
        req = urllib.request.Request(url, headers={'User-Agent': 'spotify-widget/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())

    def _get(self, title, artist, album, duration_sec):
        try:
            params = urllib.parse.urlencode({
                'track_name': title, 'artist_name': artist,
                'album_name': album or '', 'duration': int(duration_sec),
            })
            data = self._request(f'https://lrclib.net/api/get?{params}')
            if data.get('syncedLyrics'):
                return self._parse_lrc(data['syncedLyrics'])
        except Exception as e:
            print(f'[Lyrics] get: {e}')
        return self._search(title, artist, duration_sec)

    def _search(self, title, artist, duration_sec):
        try:
            q    = urllib.parse.urlencode({'q': f'{title} {artist}'})
            data = self._request(f'https://lrclib.net/api/search?{q}')
            for item in data:
                if abs(item.get('duration', 0) - duration_sec) < 3 and item.get('syncedLyrics'):
                    return self._parse_lrc(item['syncedLyrics'])
        except Exception as e:
            print(f'[Lyrics] search: {e}')
        return None

    def _parse_lrc(self, text):
        lines = []
        for line in text.splitlines():
            m = self._LRC_RE.match(line)
            if m:
                ms   = int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + round(float('0.' + m.group(3)) * 1000)
                part = m.group(4).strip()
                if part:
                    lines.append({'time': ms, 'text': part})
        return lines or None


# ── Lyrics View ───────────────────────────────────────────────────────────────
class LyricsView(Gtk.DrawingArea):
    PADDING_X     = 20
    ACTIVE_SIZE   = 17
    NEIGHBOR_SIZE = 12
    INACTIVE_SIZE = 10
    LINE_SPACING  = 10
    LERP          = 0.06

    def __init__(self, width, height):
        super().__init__()
        self.set_size_request(width, height)
        self.get_style_context().add_class('lyrics-view')
        self._state   = 'loading'
        self._lyrics  = []
        self._active  = -1
        self._scroll  = 0.0
        self._target  = 0.0
        self._tick_id = None
        self._geoms   = []
        self.connect('draw',    self._on_draw)
        self.connect('destroy', lambda *_: self._stop_tick())

    def show_loading(self):
        self._state = 'loading'; self._lyrics = []; self._geoms = []; self.queue_draw()

    def show_empty(self):
        self._state = 'empty'; self._lyrics = []; self._geoms = []; self.queue_draw()

    def set_lyrics(self, lines):
        if not lines:
            self.show_empty(); return
        self._state  = 'lyrics'; self._lyrics = lines
        self._active = -1; self._scroll = 0.0; self._target = 0.0; self._geoms = []
        self.queue_draw()

    def update_position(self, time_ms):
        if self._state != 'lyrics': return
        new_idx = -1
        for i, line in enumerate(self._lyrics):
            if line['time'] <= time_ms: new_idx = i
            else: break
        if new_idx != self._active:
            self._active = new_idx; self._start_tick(); self.queue_draw()

    def _start_tick(self):
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(16, self._tick)

    def _stop_tick(self):
        if self._tick_id:
            GLib.source_remove(self._tick_id); self._tick_id = None

    def _tick(self):
        diff = self._target - self._scroll
        if abs(diff) < 0.5:
            self._scroll = self._target; self._tick_id = None; self.queue_draw(); return False
        self._scroll += diff * self.LERP; self.queue_draw(); return True

    def _on_draw(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        layout = PangoCairo.create_layout(cr)
        layout.set_width((w - self.PADDING_X * 2) * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_alignment(Pango.Alignment.CENTER)

        if self._state != 'lyrics':
            msg = 'Buscando letra…' if self._state == 'loading' else 'Letra no disponible'
            fd  = Pango.FontDescription.from_string(f'Sans Bold {self.ACTIVE_SIZE}')
            layout.set_font_description(fd); layout.set_text(msg, -1)
            _, log = layout.get_extents()
            tw = log.width / Pango.SCALE; th = log.height / Pango.SCALE
            cr.set_source_rgba(1, 1, 1, 0.75)
            cr.move_to((w - tw) / 2, (h - th) / 2); PangoCairo.show_layout(cr, layout)
            return False

        self._geoms = []; cursor_y = 0.0
        for i, line in enumerate(self._lyrics):
            active   = (i == self._active)
            neighbor = (abs(i - self._active) == 1)
            size     = self.ACTIVE_SIZE if active else (self.NEIGHBOR_SIZE if neighbor else self.INACTIVE_SIZE)
            fd       = Pango.FontDescription.from_string(f'Sans Bold {size}')
            layout.set_font_description(fd); layout.set_text(line['text'], -1)
            _, log = layout.get_extents(); lh = log.height / Pango.SCALE
            self._geoms.append({'y': cursor_y, 'h': lh, 'text': line['text'],
                                'fd': fd, 'active': active, 'neighbor': neighbor})
            cursor_y += lh + self.LINE_SPACING

        total_h = max(0.0, cursor_y - self.LINE_SPACING)
        if 0 <= self._active < len(self._geoms):
            geo = self._geoms[self._active]; max_s = max(0.0, total_h - h)
            tl  = geo['h'] * 2.5; bl = total_h - geo['h'] * 2.5
            tgt = 0.0 if geo['y'] < tl else (max_s if geo['y'] > bl
                                             else geo['y'] + geo['h'] / 2.0 - h / 2.0)
            self._target = min(max(tgt, 0.0), max_s)
            if not self._tick_id and abs(self._target - self._scroll) > 0.5:
                self._start_tick()

        for geo in self._geoms:
            y = geo['y'] - self._scroll
            if y + geo['h'] < -30 or y > h + 30: continue
            layout.set_font_description(geo['fd']); layout.set_text(geo['text'], -1)
            if geo['active']:       cr.set_source_rgba(1, 1, 1, 1.0)
            elif geo['neighbor']:   cr.set_source_rgba(1, 1, 1, 0.6)
            else:                   cr.set_source_rgba(1, 1, 1, 0.25)
            cr.move_to(self.PADDING_X, y); PangoCairo.show_layout(cr, layout)
        return False


# ── Main widget ───────────────────────────────────────────────────────────────
class SpotifyWidget(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.c               = load_wal()
        self.current_art_url = None
        self.vol_updating    = False
        self.dragging        = False
        self.drag_x = self.drag_y = self.win_x = self.win_y = 0
        self.use_api         = False
        self.bg_pixbuf       = None
        self.ambient_rgb     = None
        self.is_playing      = False
        self.shuffle_state   = False
        self.repeat_state    = 'off'

        # Layout state
        self._layout_mode     = 'vertical'   # 'vertical' | 'horizontal'
        self._relayout_tid    = None

        # View state: 'art' | 'lyrics'
        self._view_mode      = 'art'
        self._canvas_url     = None
        self._canvas_client  = CanvasClient()
        self._video_player   = None
        self._canvas_track   = None

        self._lyrics_track  = None
        self._last_info     = None           # (title, artist, album, dur_sec)
        self._lyrics_tick_id = None
        self._lyrics_client = LyricsClient()

        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), self._css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._apply_css()

        self.setup_window()
        self.build_ui()

        threading.Thread(target=self.init_api, daemon=True).start()
        self.update_info()
        GLib.timeout_add(1500, self.update_info)

    def init_api(self):
        init_spotipy()
        self.use_api = sp is not None
        print(f"Mode: {'Web API' if self.use_api else 'playerctl only'}")

    # ── Window setup ──────────────────────────────────────────────────────────
    def setup_window(self):
        self.set_title("Spotify Widget")
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.SPLASHSCREEN)
        self.set_keep_below(False)
        self.set_resizable(True)
        self.set_default_size(WIN_W, WIN_H)
        screen = self.get_screen()
        if v := screen.get_rgba_visual():
            self.set_visual(v)
        self.set_app_paintable(True)
        display = Gdk.Display.get_default()
        max_x, right_geo = -1, None
        for i in range(display.get_n_monitors()):
            geo = display.get_monitor(i).get_geometry()
            if geo.x + geo.width > max_x:
                max_x = geo.x + geo.width; right_geo = geo
        if right_geo:
            self.move(right_geo.x + right_geo.width - WIN_W - 20, 520)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect('button-press-event',   self.on_press)
        self.connect('button-release-event', lambda w, e: setattr(self, 'dragging', False))
        self.connect('motion-notify-event',  self.on_motion)
        self.connect('draw', self.on_draw)
        self.connect('configure-event', self._on_configure)
        self.connect('destroy', self._on_destroy)

    def _on_destroy(self, *_):
        if self._video_player:
            self._video_player.destroy()
            self._video_player = None

    def on_press(self, w, e):
        if e.button == 1:
            self.dragging = True
            self.drag_x, self.drag_y = e.x_root, e.y_root
            self.win_x, self.win_y   = self.get_position()
        return False  # propagar evento para que los botones hijos reciban 'clicked'

    def on_motion(self, w, e):
        if self.dragging:
            self.move(int(self.win_x + e.x_root - self.drag_x),
                      int(self.win_y + e.y_root - self.drag_y))

    # ── Cairo background ──────────────────────────────────────────────────────
    def on_draw(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        radius = 14
        cr.move_to(radius, 0)
        cr.arc(w - radius, radius,     radius, -math.pi / 2, 0)
        cr.arc(w - radius, h - radius, radius,  0,           math.pi / 2)
        cr.arc(radius,     h - radius, radius,  math.pi / 2, math.pi)
        cr.arc(radius,     radius,     radius,  math.pi,     3 * math.pi / 2)
        cr.close_path(); cr.clip()

        if self._canvas_url:
            cr.set_source_rgba(0, 0, 0, 0); cr.paint()
            return False

        if self.bg_pixbuf:
            pb_w = self.bg_pixbuf.get_width(); pb_h = self.bg_pixbuf.get_height()
            scale = max(w / pb_w, h / pb_h)
            scaled = self.bg_pixbuf.scale_simple(int(pb_w * scale), int(pb_h * scale),
                                                 GdkPixbuf.InterpType.BILINEAR)
            Gdk.cairo_set_source_pixbuf(cr, scaled, (w - int(pb_w*scale))//2, (h - int(pb_h*scale))//2)
            cr.paint()
            ar, ag, ab = self.ambient_rgb if self.ambient_rgb else (0, 0, 0)
            pat = cairo.LinearGradient(0, 0, 0, h)
            pat.add_color_stop_rgba(0.0, ar, ag, ab, 0.72)
            pat.add_color_stop_rgba(0.5, 0, 0, 0, 0.80)
            pat.add_color_stop_rgba(1.0, 0, 0, 0, 0.93)
            cr.set_source(pat); cr.paint()
        else:
            r, g, b = hex_to_rgb_f(self.c['bg'])
            cr.set_source_rgba(r, g, b, 0.549); cr.paint()
        return False

    # ── CSS ───────────────────────────────────────────────────────────────────
    def _apply_css(self):
        css = b"""
        window { background-color: transparent; border-radius: 14px; }
        .widget-box { padding: 14px; padding-top: 16px; background: transparent; }
        .song-title { font-family: 'Bebas Neue', sans-serif; font-size: 20px; color: #ffffff; }
        .artist { font-family: 'Abel', sans-serif; font-size: 12px; color: rgba(255,255,255,0.8); }
        .ctrl-btn {
            background: transparent; border: none; border-radius: 22px; color: #ffffff;
            min-width: 44px; min-height: 44px; padding: 0; box-shadow: none;
            transition: background 150ms ease-in-out;
        }
        .ctrl-btn:hover  { background: rgba(255,255,255,0.15); }
        .ctrl-btn:active { background: rgba(255,255,255,0.28); }
        .play-btn { background: #ffffff; border-radius: 29px; min-width: 58px; min-height: 58px; box-shadow: none; }
        .play-btn:hover  { background: #eeeeee; }
        .play-btn:active { background: #dddddd; }
        .play-icon { color: #000000; }
        .vol-scale trough { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 3px; box-shadow: none; }
        .vol-scale highlight { background: rgba(255,255,255,0.7); border-radius: 3px; box-shadow: none; }
        .vol-scale slider { background: #ffffff; border-radius: 50%; min-width: 10px; min-height: 10px; border: none; box-shadow: none; }
        .time-lbl  { font-family: 'Abel'; font-size: 10px; color: rgba(255,255,255,0.7); }
        .lbl-small { font-family: 'Abel'; font-size: 10px; color: rgba(255,255,255,0.7); }
        .view-hint { font-family: 'Abel'; font-size: 9px; color: rgba(255,255,255,0.45); }
        .wave-slider { background: transparent; background-color: transparent; }
        .lyrics-view { background: transparent; background-color: transparent; }
        .transparent-bg { background: transparent; background-color: transparent; }
        .canvas-badge {
            font-family: 'Abel'; font-size: 9px;
            color: rgba(255,255,255,0.9);
            background: rgba(255,255,255,0.18);
            border-radius: 8px; padding: 2px 8px;
        }
        image { box-shadow: none; }
        """
        self._css_provider.load_from_data(css)

    # ── UI ────────────────────────────────────────────────────────────────────
    def build_ui(self):
        self._video_player = VideoPlayer(WIN_W, WIN_H)
        self._video_player.widget.set_halign(Gtk.Align.FILL)
        self._video_player.widget.set_valign(Gtk.Align.FILL)
        self._video_player.widget.set_hexpand(True)
        self._video_player.widget.set_vexpand(True)

        bg_placeholder = Gtk.DrawingArea()
        bg_placeholder.set_size_request(1, 1)
        bg_placeholder.get_style_context().add_class('transparent-bg')

        self._bg_stack = Gtk.Stack()
        self._bg_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._bg_stack.set_transition_duration(400)
        self._bg_stack.set_halign(Gtk.Align.FILL)
        self._bg_stack.set_valign(Gtk.Align.FILL)
        self._bg_stack.set_hexpand(True)
        self._bg_stack.set_vexpand(True)
        self._bg_stack.add_named(bg_placeholder,               'none')
        self._bg_stack.add_named(self._video_player.widget,    'video')

        overlay = Gtk.Overlay()
        overlay.add(self._bg_stack)

        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._content_box.get_style_context().add_class('widget-box')
        overlay.add_overlay(self._content_box)
        overlay.set_overlay_pass_through(self._content_box, False)

        # ── Art section ───────────────────────────────────────────────────────
        self._art_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._art_section.set_valign(Gtk.Align.CENTER)

        art_event = Gtk.EventBox()
        art_event.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        art_event.connect('button-press-event', self._on_view_clicked)

        self._art_stack = Gtk.Stack()
        self._art_stack.set_size_request(268, 268)
        self._art_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._art_stack.set_transition_duration(350)

        self.art_image = Gtk.Image()
        self.art_image.set_size_request(268, 268)
        self._set_placeholder_art()

        self.lyrics_view = LyricsView(268, 268)
        self._art_stack.add_named(self.art_image,   'art')
        self._art_stack.add_named(self.lyrics_view, 'lyrics')

        art_event.add(self._art_stack)
        self._art_box = Gtk.Box(); self._art_box.set_halign(Gtk.Align.CENTER)
        self._art_box.pack_start(art_event, False, False, 0)
        self._art_section.pack_start(self._art_box, False, False, 0)

        # ── Info section ──────────────────────────────────────────────────────
        # Estructura fija — solo cambian alineaciones y visibilidad según modo.
        # Layout horizontal:
        #   [_badge_row  align=END]
        #   [_top_spacer vexpand]
        #   [title_lbl   align=START]
        #   [artist_lbl  align=START]
        #   [_bot_spacer vexpand]
        #   [wave_slider]
        #   [_time_row  ]
        #   [_ctrl_row  ]
        #   (_vol_row hidden)
        # Layout vertical:
        #   [_badge_row  align=CENTER]
        #   (spacers hidden/size=0)
        #   [title_lbl   align=CENTER]
        #   [artist_lbl  align=CENTER]
        #   (spacers hidden/size=0)
        #   [wave_slider]
        #   [_time_row  ]
        #   [_ctrl_row  ]
        #   [_vol_row   ]

        self._info_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Badge row
        self._badge_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._badge_row.set_halign(Gtk.Align.CENTER)
        self._canvas_badge = Gtk.Label(label='▶ Canvas')
        self._canvas_badge.get_style_context().add_class('canvas-badge')
        self._canvas_badge.set_no_show_all(True)
        self._canvas_badge.hide()
        self._badge_row.pack_start(self._canvas_badge, False, False, 0)
        self._view_hint = Gtk.Label(label='♪ toca para ver letra')
        self._view_hint.get_style_context().add_class('view-hint')
        self._view_hint.set_no_show_all(True)
        self._badge_row.pack_start(self._view_hint, False, False, 0)

        # Layout toggle button
        self._layout_lbl = Gtk.Label(label='⇔')
        self._layout_lbl.get_style_context().add_class('view-hint')
        layout_btn = Gtk.Button()
        layout_btn.add(self._layout_lbl)
        layout_btn.get_style_context().add_class('ctrl-btn')
        layout_btn.set_relief(Gtk.ReliefStyle.NONE)
        layout_btn.connect('clicked', self._on_layout_toggle)
        self._badge_row.pack_end(layout_btn, False, False, 0)
        self._info_section.pack_start(self._badge_row, False, False, 0)

        # Spacer superior (solo visible en horizontal)
        self._top_spacer = Gtk.Box()
        self._top_spacer.set_vexpand(False)
        self._top_spacer.set_no_show_all(True)
        self._info_section.pack_start(self._top_spacer, True, True, 0)

        # Title
        self.title_lbl = Gtk.Label(label='No media playing')
        self.title_lbl.get_style_context().add_class('song-title')
        self.title_lbl.set_line_wrap(True)
        self.title_lbl.set_max_width_chars(24)
        self.title_lbl.set_halign(Gtk.Align.CENTER)
        self.title_lbl.set_justify(Gtk.Justification.CENTER)
        self._info_section.pack_start(self.title_lbl, False, False, 0)

        # Artist
        self.artist_lbl = Gtk.Label(label='')
        self.artist_lbl.get_style_context().add_class('artist')
        self.artist_lbl.set_halign(Gtk.Align.CENTER)
        self.artist_lbl.set_justify(Gtk.Justification.CENTER)
        self._info_section.pack_start(self.artist_lbl, False, False, 0)

        # Spacer inferior (solo visible en horizontal)
        self._bot_spacer = Gtk.Box()
        self._bot_spacer.set_vexpand(False)
        self._bot_spacer.set_no_show_all(True)
        self._info_section.pack_start(self._bot_spacer, True, True, 0)

        # Wave slider + time
        self.elapsed_lbl = Gtk.Label(label='0:00')
        self.elapsed_lbl.get_style_context().add_class('time-lbl')
        self.total_lbl = Gtk.Label(label='0:00')
        self.total_lbl.get_style_context().add_class('time-lbl')

        self.wave_slider = WaveSlider(self._do_seek, self.elapsed_lbl)
        self.wave_slider.set_margin_start(4); self.wave_slider.set_margin_end(4)
        self._info_section.pack_start(self.wave_slider, False, False, 0)

        self._time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._time_row.set_margin_start(6); self._time_row.set_margin_end(6); self._time_row.set_margin_bottom(-4)
        spacer = Gtk.Label(); spacer.set_hexpand(True)
        self._time_row.pack_start(self.elapsed_lbl, False, False, 0)
        self._time_row.pack_start(spacer, True, True, 0)
        self._time_row.pack_start(self.total_lbl, False, False, 0)
        self._info_section.pack_start(self._time_row, False, False, 0)

        # Controls
        self._ctrl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._ctrl_row.set_halign(Gtk.Align.CENTER); self._ctrl_row.set_margin_top(0)

        def mkbtn(icon, cb, size=24):
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            img.set_pixel_size(size)
            b = Gtk.Button(); b.add(img)
            b.get_style_context().add_class('ctrl-btn')
            b.set_relief(Gtk.ReliefStyle.NONE); b.connect('clicked', cb)
            return b, img

        self.shuffle_btn, self.shuffle_img = mkbtn('media-playlist-shuffle-symbolic', self.on_shuffle, 22)
        self._ctrl_row.pack_start(self.shuffle_btn, False, False, 0)

        _, prev_img = mkbtn('media-skip-backward-symbolic', lambda b: self.ctrl('previous'), 28)
        self._ctrl_row.pack_start(prev_img.get_parent(), False, False, 0)

        self.play_img = Gtk.Image.new_from_icon_name('media-playback-start-symbolic', Gtk.IconSize.BUTTON)
        self.play_img.set_pixel_size(32); self.play_img.get_style_context().add_class('play-icon')
        self.play_btn = Gtk.Button(); self.play_btn.add(self.play_img)
        self.play_btn.get_style_context().add_class('ctrl-btn')
        self.play_btn.get_style_context().add_class('play-btn')
        self.play_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.play_btn.connect('clicked', lambda b: self.ctrl('play-pause'))
        self._ctrl_row.pack_start(self.play_btn, False, False, 0)

        _, next_img = mkbtn('media-skip-forward-symbolic', lambda b: self.ctrl('next'), 28)
        self._ctrl_row.pack_start(next_img.get_parent(), False, False, 0)

        self.repeat_btn, self.repeat_img = mkbtn('media-playlist-repeat-symbolic', self.on_repeat, 22)
        self._ctrl_row.pack_start(self.repeat_btn, False, False, 0)
        self._info_section.pack_start(self._ctrl_row, False, False, 0)

        # Volume row
        self._vol_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._vol_row.set_halign(Gtk.Align.CENTER)
        self._vol_row.set_margin_top(4)
        self._vol_row.set_margin_bottom(28)
        self._vol_row.set_no_show_all(True)
        l1 = Gtk.Label(label='🔈'); l1.get_style_context().add_class('lbl-small')
        self._vol_row.pack_start(l1, False, False, 0)
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol_scale.get_style_context().add_class('vol-scale')
        self.vol_scale.set_size_request(200, -1); self.vol_scale.set_draw_value(False)
        self.vol_scale.connect('value-changed', self.on_volume)
        self._vol_row.pack_start(self.vol_scale, True, True, 0)
        l2 = Gtk.Label(label='🔊'); l2.get_style_context().add_class('lbl-small')
        self._vol_row.pack_start(l2, False, False, 0)
        self._info_section.pack_start(self._vol_row, False, False, 0)

        # Inicial layout vertical
        self._content_box.pack_start(self._art_section,  False, False, 0)
        self._content_box.pack_start(self._info_section, False, False, 0)

        self.add(overlay)
        self.show_all()

        # Estado inicial: vertical — ocultar spacers y elementos solo-horizontal
        self._top_spacer.hide()
        self._bot_spacer.hide()
        self._vol_row.show()
        self._view_hint.show()

    # ── Responsive layout ─────────────────────────────────────────────────────
    def _resize_art(self, size):
        size = max(80, size)
        self._art_stack.set_size_request(size, size)
        self.art_image.set_size_request(size, size)
        self.lyrics_view.set_size_request(size, size)

    def _on_layout_toggle(self, btn):
        if self._layout_mode == 'vertical':   # vertical → horizontal compacto
            self._layout_lbl.set_text('⇕')
            self.resize(560, 160)
        else:                                  # horizontal → vertical
            self._layout_lbl.set_text('⇔')
            self.resize(300, 560)

    def _on_configure(self, widget, event):
        if self._relayout_tid:
            GLib.source_remove(self._relayout_tid)
        self._relayout_tid = GLib.timeout_add(80, self._relayout, event.width, event.height)
        return False

    def _relayout(self, w, h):
        self._relayout_tid = None
        mode = 'horizontal' if w > h else 'vertical'

        # ── Art size ──────────────────────────────────────────────────────────
        if mode == 'vertical':
            art_size = min(268, w - 28, h - INFO_H - PADDING)
        else:
            art_size = max(80, min(h - 28, 160))
        self._resize_art(art_size)

        # ── Layout switch ─────────────────────────────────────────────────────
        if mode != self._layout_mode:
            self._layout_mode = mode

            if mode == 'horizontal':
                # content_box: arte | info lado a lado
                self._content_box.set_orientation(Gtk.Orientation.HORIZONTAL)
                self._content_box.set_spacing(12)
                # art_section: centrado verticalmente
                self._art_section.set_valign(Gtk.Align.CENTER)
                self._art_box.set_halign(Gtk.Align.FILL)
                # info_section: ocupa el resto, fill vertical
                self._info_section.set_valign(Gtk.Align.FILL)
                self._info_section.set_vexpand(True)
                # Quitar y re-añadir con expand correcto
                self._content_box.set_child_packing(self._art_section,  False, False, 0, Gtk.PackType.START)
                self._content_box.set_child_packing(self._info_section, True,  True,  0, Gtk.PackType.START)
                # Spacers activos → push título al centro vertical
                self._top_spacer.set_vexpand(True);  self._top_spacer.show()
                self._bot_spacer.set_vexpand(True);  self._bot_spacer.show()
                # Título/artista alineados a la izquierda
                self.title_lbl.set_halign(Gtk.Align.START)
                self.title_lbl.set_justify(Gtk.Justification.LEFT)
                self.title_lbl.set_max_width_chars(32)
                self.artist_lbl.set_halign(Gtk.Align.START)
                self.artist_lbl.set_justify(Gtk.Justification.LEFT)
                # badge_row alineado a la derecha (solo botón toggle)
                self._badge_row.set_halign(Gtk.Align.END)
                # Ocultar vol y hint
                self._vol_row.hide()
                self._view_hint.hide()
                # Icono del toggle
                self._layout_lbl.set_text('⇕')

            else:  # vertical
                # content_box: apilado vertical
                self._content_box.set_orientation(Gtk.Orientation.VERTICAL)
                self._content_box.set_spacing(6)
                self._art_section.set_valign(Gtk.Align.FILL)
                self._art_box.set_halign(Gtk.Align.CENTER)
                self._info_section.set_valign(Gtk.Align.FILL)
                self._info_section.set_vexpand(False)
                self._content_box.set_child_packing(self._art_section,  False, False, 0, Gtk.PackType.START)
                self._content_box.set_child_packing(self._info_section, False, False, 0, Gtk.PackType.START)
                # Spacers desactivados
                self._top_spacer.set_vexpand(False); self._top_spacer.hide()
                self._bot_spacer.set_vexpand(False); self._bot_spacer.hide()
                # Título/artista centrados
                self.title_lbl.set_halign(Gtk.Align.CENTER)
                self.title_lbl.set_justify(Gtk.Justification.CENTER)
                self.title_lbl.set_max_width_chars(24)
                self.artist_lbl.set_halign(Gtk.Align.CENTER)
                self.artist_lbl.set_justify(Gtk.Justification.CENTER)
                # badge_row centrado
                self._badge_row.set_halign(Gtk.Align.CENTER)
                # Restaurar vol y hint
                self._vol_row.show()
                self._view_hint.show()
                # Icono del toggle
                self._layout_lbl.set_text('⇔')

            # Canvas badge: respetar estado actual
            if self._canvas_url:
                self._canvas_badge.show()
            else:
                self._canvas_badge.hide()

        # ── Video size ────────────────────────────────────────────────────────
        if self._canvas_url and self._video_player:
            self._video_player.update_size(w, h)
        return False

    # ── View mode management ──────────────────────────────────────────────────
    def _set_view(self, mode):
        self._view_mode = mode
        self._art_stack.set_visible_child_name(mode)
        if self._layout_mode == 'vertical':
            if mode == 'lyrics':
                self._view_hint.set_text('♪ toca para ver portada')
            else:
                self._view_hint.set_text('♪ toca para ver letra')

    def _on_view_clicked(self, widget, event):
        if event.button != 1:
            return False
        if self._view_mode == 'lyrics':
            self._set_view('art')
            self._stop_lyrics_tick()
        else:
            self._set_view('lyrics')
            if self._last_info:
                self._fetch_lyrics(*self._last_info)
            self._start_lyrics_tick()
        return True

    # ── Canvas ────────────────────────────────────────────────────────────────
    def _check_canvas(self, track_id):
        if not track_id or track_id == self._canvas_track:
            return
        self._canvas_track = track_id
        try:
            with open(SPOTIFY_CONF) as f:
                sp_dc = json.load(f).get('sp_dc', '')
        except Exception:
            sp_dc = ''
        if sp_dc:
            self._canvas_client.get_canvas(track_id, sp_dc, self._on_canvas_ready)

    def _on_canvas_ready(self, url):
        self._canvas_url = url
        if url:
            print(f'[Canvas] ✓ {url[:60]}…')
            self._canvas_badge.show()
            self._video_player.play(url)
            self._bg_stack.set_visible_child_name('video')
            self.art_image.hide()
        else:
            print('[Canvas] ✗ no canvas for this track')
            self._canvas_badge.hide()
            self._video_player.stop()
            self._bg_stack.set_visible_child_name('none')
            self.art_image.show()
        self.queue_draw()
        return False

    # ── Lyrics ────────────────────────────────────────────────────────────────
    def _fetch_lyrics(self, title, artist, album, dur_sec):
        track_hash = title + artist
        if track_hash == self._lyrics_track and self.lyrics_view._state == 'lyrics':
            return
        self._lyrics_track = track_hash
        self.lyrics_view.show_loading()
        self._lyrics_client.get_lyrics(title, artist, album, dur_sec, self._on_lyrics_ready)

    def _on_lyrics_ready(self, lines):
        if lines: self.lyrics_view.set_lyrics(lines)
        else:     self.lyrics_view.show_empty()
        return False

    def _start_lyrics_tick(self):
        if not self._lyrics_tick_id:
            self._lyrics_tick_id = GLib.timeout_add(100, self._lyrics_tick)

    def _stop_lyrics_tick(self):
        if self._lyrics_tick_id:
            GLib.source_remove(self._lyrics_tick_id); self._lyrics_tick_id = None

    def _lyrics_tick(self):
        if self._view_mode == 'lyrics' and self.is_playing:
            self.lyrics_view.update_position(self.wave_slider.get_position_ms())
        return True

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _set_placeholder_art(self):
        try:
            bg = self.c['bg'].lstrip('#')
            r, g, b = [int(bg[i:i+2], 16) for i in (0, 2, 4)]
            pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 268, 268)
            pb.fill((r << 24) | (g << 16) | (b << 8) | 60)
            self.art_image.set_from_pixbuf(pb)
        except:
            pass

    def _extract_ambient(self, pixbuf):
        try:
            px = pixbuf.scale_simple(1, 1, GdkPixbuf.InterpType.TILES).get_pixels()
            return (px[0] / 255.0, px[1] / 255.0, px[2] / 255.0)
        except:
            return None

    # ── Controls ──────────────────────────────────────────────────────────────
    def _spotify_running(self):
        try:
            return subprocess.run(['pgrep', '-x', 'spotify'], capture_output=True).returncode == 0
        except:
            return False

    def _launch_spotify(self):
        if not self._spotify_running():
            subprocess.Popen(['spotify', '--minimized'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                time.sleep(0.5)
                if run(['playerctl', '-p', 'spotify', 'status']): break

    def ctrl(self, action):
        if not self._spotify_running():
            threading.Thread(target=self._launch_and_ctrl, args=(action,), daemon=True).start()
            return
        self._do_ctrl(action)

    def _launch_and_ctrl(self, action):
        self._launch_spotify()
        if self.use_api and sp:
            for _ in range(20):
                time.sleep(0.5)
                try:
                    if sp.devices().get('devices', []): break
                except: pass
        GLib.idle_add(self._do_ctrl, action)

    def _get_device_id(self):
        try:
            devices = sp.devices().get('devices', [])
            return devices[0]['id'] if devices else None
        except:
            return None

    def _do_ctrl(self, action):
        if self.use_api and sp:
            try:
                if action == 'play-pause':
                    pb = sp.current_playback()
                    if pb and pb.get('is_playing'):
                        sp.pause_playback()
                    else:
                        active_id = (pb or {}).get('device', {}).get('id')
                        if active_id:
                            sp.start_playback(device_id=active_id)
                        else:
                            device_id = self._get_device_id()
                            if device_id:
                                sp.transfer_playback(device_id, force_play=True)
                            else:
                                run(['playerctl', '-p', 'spotify', action]); return
                elif action == 'next':     sp.next_track()
                elif action == 'previous': sp.previous_track()
                return
            except Exception as e:
                print(f"API ctrl error: {e}")
        run(['playerctl', '-p', 'spotify', action])
        run(['playerctl', action])

    def on_shuffle(self, btn):
        new_state = not self.shuffle_state
        if self.use_api and sp:
            try: sp.shuffle(new_state); return
            except Exception as e: print(f"Shuffle error: {e}")
        run(['playerctl', '-p', 'spotify', 'shuffle', 'Toggle'])

    def on_repeat(self, btn):
        new_state = {'off': 'context', 'context': 'track', 'track': 'off'}.get(self.repeat_state, 'off')
        if self.use_api and sp:
            try: sp.repeat(new_state); return
            except Exception as e: print(f"Repeat error: {e}")
        pc = {'off': 'None', 'context': 'Playlist', 'track': 'Track'}
        run(['playerctl', '-p', 'spotify', 'loop', pc.get(new_state, 'None')])

    def _do_seek(self, pos_ms):
        def _seek():
            if self.use_api and sp:
                try: sp.seek_track(int(pos_ms)); return
                except: pass
            run(['playerctl', '-p', 'spotify', 'position', str(pos_ms / 1000)])
        threading.Thread(target=_seek, daemon=True).start()

    def on_volume(self, scale):
        if self.vol_updating: return
        vol = int(scale.get_value())
        def _set():
            if self.use_api and sp:
                try: sp.volume(vol); return
                except: pass
            run(['playerctl', '-p', 'spotify', 'volume', str(vol / 100)])
        threading.Thread(target=_set, daemon=True).start()

    # ── Data update ───────────────────────────────────────────────────────────
    def update_info(self):
        threading.Thread(target=self._fetch, daemon=True).start()
        return True

    def _fetch(self):
        title = artist = art_url = album = track_id = ''
        status  = ''
        vol_pct = None
        pos_ms  = dur_ms = 0
        shuffle = False
        repeat  = 'off'

        if self.use_api and sp:
            try:
                pb = sp.current_playback()
                if pb:
                    status   = 'Playing' if pb.get('is_playing') else 'Paused'
                    item     = pb.get('item') or {}
                    title    = item.get('name', '')
                    artist   = ', '.join(a['name'] for a in item.get('artists', []))
                    album    = item.get('album', {}).get('name', '')
                    track_id = item.get('id', '')
                    images   = item.get('album', {}).get('images', [])
                    art_url  = images[0]['url'] if images else ''
                    vol_pct  = pb.get('device', {}).get('volume_percent')
                    pos_ms   = pb.get('progress_ms') or 0
                    dur_ms   = item.get('duration_ms') or 0
                    shuffle  = pb.get('shuffle_state', False)
                    repeat   = pb.get('repeat_state', 'off')
            except Exception as e:
                print(f"API fetch error: {e}"); self.use_api = False

        if not title:
            status   = run(['playerctl', '-p', 'spotify', 'status']) or run(['playerctl', 'status'])
            title    = run(['playerctl', '-p', 'spotify', 'metadata', '--format', '{{xesam:title}}']) or \
                       run(['playerctl', 'metadata', '--format', '{{xesam:title}}'])
            artist   = run(['playerctl', '-p', 'spotify', 'metadata', '--format', '{{xesam:artist}}']) or \
                       run(['playerctl', 'metadata', '--format', '{{xesam:artist}}'])
            album    = run(['playerctl', '-p', 'spotify', 'metadata', '--format', '{{xesam:album}}']) or ''
            art_url  = run(['playerctl', '-p', 'spotify', 'metadata', '--format', '{{mpris:artUrl}}']) or \
                       run(['playerctl', 'metadata', '--format', '{{mpris:artUrl}}'])
            track_uri = run(['playerctl', '-p', 'spotify', 'metadata', 'mpris:trackid']) or ''
            if track_uri.startswith('spotify:track:'):
                track_id = track_uri.split(':')[-1]
            try:
                v = run(['playerctl', '-p', 'spotify', 'volume'])
                if v: vol_pct = int(float(v) * 100)
                p = run(['playerctl', '-p', 'spotify', 'position'])
                l = run(['playerctl', '-p', 'spotify', 'metadata', 'mpris:length'])
                if p: pos_ms = int(float(p) * 1000)
                if l: dur_ms = int(int(l) / 1000)
                sh = run(['playerctl', '-p', 'spotify', 'shuffle'])
                lp = run(['playerctl', '-p', 'spotify', 'loop'])
                shuffle = (sh == 'On')
                repeat  = {'None': 'off', 'Playlist': 'context', 'Track': 'track'}.get(lp, 'off')
            except: pass

        GLib.idle_add(self._update_ui, status, title, artist, album, track_id,
                      art_url, vol_pct, pos_ms, dur_ms, shuffle, repeat)

    def _update_ui(self, status, title, artist, album, track_id,
                   art_url, vol_pct, pos_ms, dur_ms, shuffle, repeat):
        is_playing      = (status == 'Playing')
        self.is_playing = is_playing
        self.play_img.set_from_icon_name(
            'media-playback-pause-symbolic' if is_playing else 'media-playback-start-symbolic',
            Gtk.IconSize.BUTTON)

        # Truncar título según modo de layout
        max_chars = 32 if self._layout_mode == 'horizontal' else 27
        t = (title[:max_chars] + '…' if len(title) > max_chars else title) if title else 'No media playing'
        self.title_lbl.set_text(t)
        self.artist_lbl.set_text(artist or '')

        # Track change detection
        if title:
            dur_sec  = dur_ms / 1000.0
            new_info = (title, artist, album, dur_sec)
            if new_info != self._last_info:
                self._last_info   = new_info
                self._canvas_url  = None
                self._canvas_badge.hide()
                self._video_player.stop()
                self._bg_stack.set_visible_child_name('none')
                self.art_image.show()
                self.queue_draw()
                if self._view_mode != 'lyrics':
                    self._set_view('art')
                if track_id:
                    self._check_canvas(track_id)
                if self._view_mode == 'lyrics':
                    self._fetch_lyrics(*new_info)
                else:
                    self._lyrics_track = None

        # Wave slider
        if dur_ms > 0:
            self.wave_slider.update_metadata(dur_ms, is_playing, pos_ms, title + artist)
            self.total_lbl.set_text(fmt_time(dur_ms))

        # Shuffle / repeat
        self.shuffle_state = shuffle
        self.shuffle_btn.set_opacity(1.0 if shuffle else 0.45)
        self.repeat_state = repeat
        if repeat == 'track':
            self.repeat_img.set_from_icon_name('media-playlist-repeat-song-symbolic', Gtk.IconSize.BUTTON)
            self.repeat_btn.set_opacity(1.0)
        elif repeat == 'context':
            self.repeat_img.set_from_icon_name('media-playlist-repeat-symbolic', Gtk.IconSize.BUTTON)
            self.repeat_btn.set_opacity(1.0)
        else:
            self.repeat_img.set_from_icon_name('media-playlist-repeat-symbolic', Gtk.IconSize.BUTTON)
            self.repeat_btn.set_opacity(0.45)

        # Art
        if art_url and art_url != self.current_art_url:
            self.current_art_url = art_url
            threading.Thread(target=self.load_art, args=(art_url,), daemon=True).start()
        elif not art_url:
            self.bg_pixbuf = None; self.ambient_rgb = None
            self.queue_draw(); self._set_placeholder_art()

        # Volume
        if vol_pct is not None:
            self.vol_updating = True
            self.vol_scale.set_value(vol_pct)
            self.vol_updating = False
        return False

    def load_art(self, url):
        try:
            if url.startswith('file://'):
                src_file = url[7:]
            else:
                src_file = os.path.join(CACHE_DIR, url.split('/')[-1])
                if not os.path.exists(src_file):
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        data = r.read()
                    with open(src_file, 'wb') as f:
                        f.write(data)
            pb_full = GdkPixbuf.Pixbuf.new_from_file(src_file)
            ambient = self._extract_ambient(pb_full)
            pb_art  = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_file, 268, 268, True)
            def _apply():
                self.bg_pixbuf = pb_full; self.ambient_rgb = ambient
                self.art_image.set_from_pixbuf(pb_art); self.queue_draw()
                return False
            GLib.idle_add(_apply)
        except Exception as e:
            print(f"Art error: {e}")


if __name__ == '__main__':
    w = SpotifyWidget()
    w.connect('destroy', Gtk.main_quit)
    Gtk.main()