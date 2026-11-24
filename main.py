"""
VideoGrab - paste a link, grab the video.
Kivy + yt-dlp.  Runs on Android (APK) and on desktop for quick testing.

Features
  * Paste a link (or share one from any app straight into VideoGrab)
  * Clipboard auto-detect
  * Link analyzer with thumbnail, title, uploader, duration
  * Quality picker: Best / 1080p / 720p / 480p / 360p / Audio only
  * Download queue with live progress, speed, ETA, cancel and retry
  * Library with search, play, share and delete
  * Saves into Downloads/VideoGrab (visible in your Gallery / Files app)
  * One-tap "Update engine" so sites keep working without a new APK
"""
import os
import re
import sys
import json
import time
import glob
import uuid
import shutil
import threading
import mimetypes
import webbrowser
from urllib.parse import unquote

from kivy.app import App
from kivy.clock import Clock
from kivy.animation import Animation
from kivy.lang import Builder
from kivy.core.window import Window
from kivy.core.clipboard import Clipboard
from kivy.metrics import dp
from kivy.properties import (StringProperty, NumericProperty,
                             BooleanProperty, ListProperty, ObjectProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.button import Button
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen
from kivy.uix.widget import Widget
from kivy.uix.videoplayer import VideoPlayer
from kivy.utils import platform

try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    os.environ.setdefault('REQUESTS_CA_BUNDLE', certifi.where())
except Exception:
    certifi = None

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
ENGINE_URL = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp'
FORMATS = {
    'best': 'b[ext=mp4]/b',
    '1080': 'b[height<=1080][ext=mp4]/b[height<=1080]/b',
    '720': 'b[height<=720][ext=mp4]/b[height<=720]/b',
    '480': 'b[height<=480][ext=mp4]/b[height<=480]/b',
    '360': 'b[height<=360][ext=mp4]/b[height<=360]/b',
    'audio': 'ba[ext=m4a]/ba/b',
}
QUALITY_LABEL = {'best': 'Best', '1080': '1080p', '720': '720p',
                 '480': '480p', '360': '360p', 'audio': 'Audio'}
MIME_FIX = {
    '.mp4': 'video/mp4', '.m4a': 'audio/mp4', '.webm': 'video/webm',
    '.mkv': 'video/x-matroska', '.opus': 'audio/ogg', '.ts': 'video/mp2t',
    '.mp3': 'audio/mpeg', '.3gp': 'video/3gpp', '.mov': 'video/quicktime',
}
ACTIVE = ('queued', 'fetching', 'downloading', 'saving')
ANSI = re.compile(r'\x1b\[[0-9;]*m')

DATA_DIR = ''
ENGINE_ZIP = ''
CFG_FILE = ''
HIST_FILE = ''


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def fmt_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.0f B' % n if unit == 'B' else '%.1f %s' % (n, unit)
        n /= 1024.0


def fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return ''
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


def clean_url(text):
    text = (text or '').strip()
    m = re.search(r'https?://\S+', text)
    if m:
        return m.group(0).rstrip('.,;)')
    if text and ' ' not in text and '.' in text:
        return 'https://' + text
    return ''


def friendly_error(e):
    msg = ANSI.sub('', str(e)).replace('ERROR: ', '').strip()
    low = msg.lower()
    if 'unsupported url' in low:
        return 'This link is not supported (or not a video page).'
    if 'private' in low or 'login' in low or 'sign in' in low:
        return 'This video is private or needs a login.'
    if 'unable to download' in low or 'urlopen' in low or 'getaddrinfo' in low:
        return 'Network problem. Check your internet and try again.'
    if len(msg) > 200:
        msg = msg[:200] + '...'
    return msg or 'Something went wrong.'


def pick_thumb(info):
    thumbs = info.get('thumbnails') or []
    for t in reversed(thumbs):
        u = t.get('url') or ''
        if u and not u.lower().split('?')[0].endswith('.webp'):
            return u
    u = info.get('thumbnail') or ''
    if 'vi_webp' in u:
        u = u.replace('vi_webp', 'vi').replace('.webp', '.jpg')
    return u


def guess_mime(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_FIX:
        return MIME_FIX[ext]
    return mimetypes.guess_type(path)[0] or 'video/*'


# ----------------------------------------------------------------------------
# yt-dlp engine loading (bundled copy, or a newer one downloaded by the user)
# ----------------------------------------------------------------------------
def setup_engine_path():
    if ENGINE_ZIP and os.path.exists(ENGINE_ZIP) and ENGINE_ZIP not in sys.path:
        sys.path.insert(0, ENGINE_ZIP)


def purge_ytdlp():
    for k in list(sys.modules):
        if k == 'yt_dlp' or k.startswith('yt_dlp.') or k.startswith('yt_dlp_plugins'):
            del sys.modules[k]


def load_ytdlp():
    import yt_dlp
    return yt_dlp


def engine_version():
    try:
        load_ytdlp()
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return 'unavailable'


# ----------------------------------------------------------------------------
# Android helpers (all wrapped, they never crash the app)
# ----------------------------------------------------------------------------
def _activity():
    from jnius import autoclass
    return autoclass('org.kivy.android.PythonActivity').mActivity


def android_sdk():
    from jnius import autoclass
    return autoclass('android.os.Build$VERSION').SDK_INT


def save_public(path, name, mime):
    """Copy a finished download to the public Downloads/VideoGrab folder.
    Returns a uri string (android) or a file path (desktop)."""
    if platform != 'android':
        dest_dir = os.path.join(os.path.expanduser('~'), 'Downloads', 'VideoGrab')
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(dest):
            dest = '%s (%d)%s' % (base, i, ext)
            i += 1
        shutil.copy2(path, dest)
        return dest

    from jnius import autoclass
    activity = _activity()
    sdk = android_sdk()
    if sdk >= 29:
        ContentValues = autoclass('android.content.ContentValues')
        Downloads = autoclass('android.provider.MediaStore$Downloads')
        Cols = autoclass('android.provider.MediaStore$MediaColumns')
        values = ContentValues()
        values.put(Cols.DISPLAY_NAME, name)
        values.put(Cols.MIME_TYPE, mime)
        values.put(Cols.RELATIVE_PATH, 'Download/VideoGrab')
        resolver = activity.getContentResolver()
        uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            raise RuntimeError('Could not create file in Downloads')
        out = resolver.openOutputStream(uri)
        copied = False
        try:
            FileInputStream = autoclass('java.io.FileInputStream')
            FileUtils = autoclass('android.os.FileUtils')
            ins = FileInputStream(path)
            FileUtils.copy(ins, out)
            ins.close()
            copied = True
        except Exception:
            copied = False
        if not copied:
            out.close()
            out = resolver.openOutputStream(uri, 'wt')
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(bytearray(chunk))
        out.flush()
        out.close()
        return uri.toString()

    # Android 7 - 9: plain file access with storage permission
    Environment = autoclass('android.os.Environment')
    pub = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath()
    dest_dir = os.path.join(pub, 'VideoGrab')
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(dest):
        dest = '%s (%d)%s' % (base, i, ext)
        i += 1
    shutil.copy2(path, dest)
    try:
        Scanner = autoclass('android.media.MediaScannerConnection')
        Scanner.scanFile(activity, [dest], None, None)
    except Exception:
        pass
    return 'file://' + dest


def _uri_for(uri_str):
    from jnius import autoclass
    Uri = autoclass('android.net.Uri')
    if uri_str.startswith('/'):
        uri_str = 'file://' + uri_str
    return Uri.parse(uri_str)


def open_media(uri_str, mime):
    if platform != 'android':
        try:
            webbrowser.open('file://' + uri_str)
        except Exception:
            pass
        return True
    try:
        from jnius import autoclass
        StrictMode = autoclass('android.os.StrictMode')
        VmPolicy = autoclass('android.os.StrictMode$VmPolicy$Builder')
        StrictMode.setVmPolicy(VmPolicy().build())
    except Exception:
        pass
    try:
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(_uri_for(uri_str), mime)
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        _activity().startActivity(intent)
        return True
    except Exception:
        return False


def share_media(uri_str, mime, url=''):
    if platform != 'android':
        return False
    try:
        from jnius import autoclass, cast
        Intent = autoclass('android.content.Intent')
        intent = Intent(Intent.ACTION_SEND)
        intent.setType(mime)
        parcel = cast('android.os.Parcelable', _uri_for(uri_str))
        intent.putExtra(Intent.EXTRA_STREAM, parcel)
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        chooser = Intent.createChooser(intent, 'Share video')
        _activity().startActivity(chooser)
        return True
    except Exception:
        return False


def delete_media(uri_str):
    try:
        if platform == 'android' and uri_str.startswith('content://'):
            _activity().getContentResolver().delete(_uri_for(uri_str), None, None)
        else:
            p = uri_str[7:] if uri_str.startswith('file://') else uri_str
            if os.path.exists(p):
                os.remove(p)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Custom widgets
# ----------------------------------------------------------------------------
class Root(FloatLayout):
    pass


class Toast(Label):
    pass


class Card(BoxLayout):
    bg = ListProperty([0.075, 0.075, 0.075, 1])


class RBtn(Button):
    bg = ListProperty([1, 1, 1, 1])


class Chip(ToggleButton):
    key = StringProperty('best')


class NavBtn(ToggleButton):
    pass


class Bar(Widget):
    value = NumericProperty(0)


class JobRow(Card):
    job_id = StringProperty('')
    title = StringProperty('')
    status_text = StringProperty('')
    status_color = ListProperty([1, 1, 1, 0.65])
    progress = NumericProperty(0)
    action_text = StringProperty('Cancel')

    def on_action(self):
        App.get_running_app().job_action(self.job_id)


class HistRow(Card):
    entry = ObjectProperty(None)
    title = StringProperty('')
    meta = StringProperty('')
    thumb = StringProperty('')


class HomeScreen(Screen):
    has_info = BooleanProperty(False)
    busy = BooleanProperty(False)
    p_title = StringProperty('')
    p_meta = StringProperty('')
    p_thumb = StringProperty('')


class QueueScreen(Screen):
    pass


class LibraryScreen(Screen):
    pass


class MoreScreen(Screen):
    pass


class PlayerScreen(Screen):
    pass


# ----------------------------------------------------------------------------
# UI definition
# ----------------------------------------------------------------------------
KV = r'''
#:import FadeTransition kivy.uix.screenmanager.FadeTransition

<Card>:
    canvas.before:
        Color:
            rgba: 0, 0, 0, .78
        RoundedRectangle:
            pos: self.x, self.y - dp(4)
            size: self.width, self.height + dp(1)
            radius: [dp(18)]
        Color:
            rgba: 1, 1, 1, .18
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(18)]
        Color:
            rgba: self.bg
        RoundedRectangle:
            pos: self.x + dp(1), self.y + dp(1)
            size: self.width - dp(2), self.height - dp(2)
            radius: [dp(18)]
        Color:
            rgba: 1, 1, 1, .07
        RoundedRectangle:
            pos: self.x + dp(5), self.top - dp(4)
            size: self.width - dp(10), dp(1)
            radius: [dp(1)]
        Color:
            rgba: 0, 0, 0, .22
        RoundedRectangle:
            pos: self.x + dp(5), self.y + dp(2)
            size: self.width - dp(10), dp(1)
            radius: [dp(1)]

<RBtn>:
    background_normal: ''
    background_down: ''
    background_color: 0, 0, 0, 0
    color: (0.05, 0.05, 0.05, 1) if not self.disabled else (.35, .35, .35, 1)
    bold: True
    font_size: sp(15)
    canvas.before:
        Color:
            rgba: 0, 0, 0, .82
        RoundedRectangle:
            pos: self.x, self.y - dp(4)
            size: self.width, self.height + dp(1)
            radius: [dp(14)]
        Color:
            rgba: .16, .16, .16, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]
        Color:
            rgba: (self.bg[0] * (.72 if self.state == 'down' else 1), self.bg[1] * (.72 if self.state == 'down' else 1), self.bg[2] * (.72 if self.state == 'down' else 1), self.bg[3] * (.45 if self.disabled else 1))
        RoundedRectangle:
            pos: self.x + dp(1), self.y + dp(1)
            size: self.width - dp(2), self.height - dp(3)
            radius: [dp(13)]
        Color:
            rgba: 1, 1, 1, .26 if not self.disabled else .12
        RoundedRectangle:
            pos: self.x + dp(5), self.top - dp(7)
            size: self.width - dp(10), dp(2)
            radius: [dp(2)]
        Color:
            rgba: 0, 0, 0, .12 if self.state == 'down' else .08
        RoundedRectangle:
            pos: self.x + dp(5), self.y + dp(2)
            size: self.width - dp(10), dp(2)
            radius: [dp(2)]

<Chip>:
    group: 'quality'
    allow_no_selection: False
    background_normal: ''
    background_down: ''
    background_color: 0, 0, 0, 0
    bold: True
    font_size: sp(14)
    color: (0.05, 0.05, 0.05, 1) if self.state == 'down' else (1, 1, 1, .75)
    canvas.before:
        Color:
            rgba: (1, 1, 1, 1) if self.state == 'down' else (1, 1, 1, 0.08)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<NavBtn>:
    group: 'nav'
    allow_no_selection: False
    background_normal: ''
    background_down: ''
    background_color: 0, 0, 0, 0
    bold: True
    font_size: sp(14)
    color: (1, 1, 1, 1) if self.state == 'down' else (1, 1, 1, .55)
    canvas.after:
        Color:
            rgba: (1, 1, 1, 1) if self.state == 'down' else (0, 0, 0, 0)
        RoundedRectangle:
            pos: self.x + self.width * .3, self.y + dp(2)
            size: self.width * .4, dp(3)
            radius: [dp(2)]

<Bar>:
    canvas:
        Color:
            rgba: 1, 1, 1, .09
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.height / 2.]
        Color:
            rgba: 1, 1, 1, 1
        RoundedRectangle:
            pos: self.pos
            size: (self.width * max(0.03, min(self.value, 1.0)), self.height)
            radius: [self.height / 2.]

<Toast>:
    size_hint: None, None
    width: dp(320)
    text_size: self.width - dp(32), None
    height: self.texture_size[1] + dp(26)
    pos_hint: {'center_x': .5}
    y: dp(96)
    opacity: 0
    halign: 'center'
    valign: 'middle'
    canvas.before:
        Color:
            rgba: 0.045, 0.045, 0.045, .98
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(18)]

<JobRow>:
    orientation: 'vertical'
    size_hint_y: None
    height: dp(136)
    padding: dp(14)
    spacing: dp(6)
    Label:
        text: root.title
        bold: True
        font_size: sp(15)
        size_hint_y: None
        height: dp(24)
        text_size: self.size
        halign: 'left'
        valign: 'middle'
        shorten: True
        shorten_from: 'right'
    Label:
        text: root.status_text
        color: root.status_color
        font_size: sp(13)
        size_hint_y: None
        height: dp(20)
        text_size: self.size
        halign: 'left'
        valign: 'middle'
        shorten: True
    Bar:
        value: root.progress
        size_hint_y: None
        height: dp(8)
    BoxLayout:
        size_hint_y: None
        height: dp(40)
        Widget:
        RBtn:
            text: root.action_text
            size_hint_x: None
            width: dp(110)
            on_release: root.on_action()

<HistRow>:
    size_hint_y: None
    height: dp(124)
    padding: dp(10)
    spacing: dp(12)
    AsyncImage:
        source: root.thumb
        size_hint_x: None
        width: dp(100)
        fit_mode: 'cover'
        canvas.before:
            Color:
                rgba: 1, 1, 1, .06
            RoundedRectangle:
                pos: self.pos
                size: self.size
                radius: [dp(12)]
    BoxLayout:
        orientation: 'vertical'
        spacing: dp(4)
        Label:
            text: root.title
            bold: True
            font_size: sp(14)
            text_size: self.size
            halign: 'left'
            valign: 'top'
            max_lines: 2
            shorten: True
            shorten_from: 'right'
        Label:
            text: root.meta
            color: 1, 1, 1, .55
            font_size: sp(12)
            size_hint_y: None
            height: dp(18)
            text_size: self.size
            halign: 'left'
            valign: 'middle'
            shorten: True
        BoxLayout:
            size_hint_y: None
            height: dp(36)
            spacing: dp(6)
            RBtn:
                text: 'Play'
                font_size: sp(13)
                on_release: app.play(root.entry)
            RBtn:
                text: 'Share'
                font_size: sp(13)
                on_release: app.share(root.entry)
            RBtn:
                text: 'Delete'
                font_size: sp(13)
                on_release: app.delete_entry(root.entry)

<Root>:
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.020, 0.020, 0.020, 1
            Rectangle:
                pos: self.pos
                size: self.size
        ScreenManager:
            id: sm
            transition: FadeTransition(duration=.12)
            HomeScreen:
            QueueScreen:
            LibraryScreen:
            MoreScreen:
            PlayerScreen:
        BoxLayout:
            id: nav_bar
            size_hint_y: None
            height: dp(60)
            canvas.before:
                Color:
                    rgba: 0.055, 0.055, 0.055, 1
                Rectangle:
                    pos: self.pos
                    size: self.size
            NavBtn:
                id: nav_home
                text: 'Home'
                state: 'down'
                on_release: app.go('home')
            NavBtn:
                id: nav_queue
                text: app.queue_label
                on_release: app.go('queue')
            NavBtn:
                id: nav_library
                text: 'Library'
                on_release: app.go('library')
            NavBtn:
                id: nav_more
                text: 'More'
                on_release: app.go('more')
    Toast:
        id: toast

<PlayerScreen>:
    name: 'player'
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.020, 0.020, 0.020, 1
            Rectangle:
                pos: self.pos
                size: self.size
        BoxLayout:
            size_hint_y: None
            height: dp(64)
            padding: dp(12), dp(8)
            spacing: dp(10)
            RBtn:
                text: 'Close'
                size_hint_x: None
                width: dp(90)
                on_release: app.close_player()
            Label:
                id: player_title
                text: 'Now playing'
                bold: True
                font_size: sp(17)
                text_size: self.size
                halign: 'left'
                valign: 'middle'
                shorten: True
                shorten_from: 'right'
        VideoPlayer:
            id: video
            state: 'play'
            options: {'allow_stretch': True, 'eos': 'stop'}
            canvas.before:
                Color:
                    rgba: 0, 0, 0, 1
                Rectangle:
                    pos: self.pos
                    size: self.size

<HomeScreen>:
    name: 'home'
    ScrollView:
        do_scroll_x: False
        bar_width: 0
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16), dp(18), dp(16), dp(24)
            spacing: dp(14)
            Label:
                text: 'VideoGrab'
                font_size: sp(32)
                bold: True
                color: 1, 1, 1, 1
                size_hint_y: None
                height: dp(44)
                text_size: self.size
                halign: 'left'
                valign: 'middle'
            Label:
                text: 'Paste a link. Grab the video.'
                color: 1, 1, 1, .6
                font_size: sp(15)
                size_hint_y: None
                height: dp(22)
                text_size: self.size
                halign: 'left'
                valign: 'middle'
            Card:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(14)
                spacing: dp(10)
                TextInput:
                    id: url_input
                    hint_text: 'Paste video link here'
                    multiline: False
                    input_type: 'url'
                    size_hint_y: None
                    height: dp(52)
                    padding: dp(14), dp(15), dp(14), dp(12)
                    background_normal: ''
                    background_active: ''
                    background_color: 1, 1, 1, .08
                    foreground_color: 1, 1, 1, 1
                    hint_text_color: 1, 1, 1, .4
                    cursor_color: 1, 1, 1, 1
                    font_size: sp(15)
                    on_text_validate: app.analyze()
                BoxLayout:
                    size_hint_y: None
                    height: dp(44)
                    spacing: dp(10)
                    RBtn:
                        text: 'Paste'
                        on_release: app.paste_link()
                    RBtn:
                        text: 'Clear'
                        on_release: app.clear_link()
                Label:
                    text: 'Quality'
                    color: 1, 1, 1, .6
                    font_size: sp(13)
                    size_hint_y: None
                    height: dp(20)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
                GridLayout:
                    id: chips
                    cols: 3
                    size_hint_y: None
                    height: dp(96)
                    spacing: dp(8)
                    Chip:
                        text: 'Best MP4'
                        key: 'best'
                        state: 'down'
                        on_state: app.set_quality(self.key) if self.state == 'down' else None
                    Chip:
                        text: '1080p'
                        key: '1080'
                        on_state: app.set_quality(self.key) if self.state == 'down' else None
                    Chip:
                        text: '720p'
                        key: '720'
                        on_state: app.set_quality(self.key) if self.state == 'down' else None
                    Chip:
                        text: '480p'
                        key: '480'
                        on_state: app.set_quality(self.key) if self.state == 'down' else None
                    Chip:
                        text: '360p'
                        key: '360'
                        on_state: app.set_quality(self.key) if self.state == 'down' else None
                    Chip:
                        text: 'Audio M4A'
                        key: 'audio'
                        on_state: app.set_quality(self.key) if self.state == 'down' else None
            RBtn:
                text: 'Checking...' if root.busy else 'Analyze link'
                disabled: root.busy
                size_hint_y: None
                height: dp(50)
                on_release: app.analyze()
            Card:
                size_hint_y: None
                height: dp(112) if root.has_info else 0
                opacity: 1 if root.has_info else 0
                disabled: not root.has_info
                padding: dp(10)
                spacing: dp(12)
                AsyncImage:
                    source: root.p_thumb
                    size_hint_x: None
                    width: dp(120)
                    fit_mode: 'cover'
                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, .06
                        RoundedRectangle:
                            pos: self.pos
                            size: self.size
                            radius: [dp(12)]
                BoxLayout:
                    orientation: 'vertical'
                    spacing: dp(4)
                    Label:
                        text: root.p_title
                        bold: True
                        font_size: sp(15)
                        text_size: self.size
                        halign: 'left'
                        valign: 'top'
                        max_lines: 3
                        shorten: True
                        shorten_from: 'right'
                    Label:
                        text: root.p_meta
                        color: 1, 1, 1, .6
                        font_size: sp(12)
                        size_hint_y: None
                        height: dp(34)
                        text_size: self.size
                        halign: 'left'
                        valign: 'top'
                        max_lines: 2
            RBtn:
                text: 'Download'
                font_size: sp(17)
                size_hint_y: None
                height: dp(56)
                on_release: app.queue_download()
            Label:
                text: 'Works with YouTube, TikTok, Instagram, Facebook, X, Vimeo, Reddit and 1000+ more sites.'
                color: 1, 1, 1, .4
                font_size: sp(12)
                size_hint_y: None
                height: dp(40)
                text_size: self.width, None
                halign: 'center'
                valign: 'middle'

<QueueScreen>:
    name: 'queue'
    BoxLayout:
        orientation: 'vertical'
        padding: dp(16)
        spacing: dp(10)
        BoxLayout:
            size_hint_y: None
            height: dp(46)
            Label:
                text: 'Queue'
                font_size: sp(26)
                bold: True
                text_size: self.size
                halign: 'left'
                valign: 'middle'
            RBtn:
                text: 'Clear finished'
                font_size: sp(13)
                size_hint_x: None
                width: dp(130)
                on_release: app.clear_finished()
        ScrollView:
            do_scroll_x: False
            bar_width: 0
            BoxLayout:
                id: box
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(10)
                Label:
                    id: empty
                    text: 'Nothing here yet.\nPaste a link on Home and tap Download.'
                    color: 1, 1, 1, .45
                    halign: 'center'
                    size_hint_y: None
                    height: dp(120)

<LibraryScreen>:
    name: 'library'
    BoxLayout:
        orientation: 'vertical'
        padding: dp(16)
        spacing: dp(10)
        Label:
            text: 'Library'
            font_size: sp(26)
            bold: True
            size_hint_y: None
            height: dp(46)
            text_size: self.size
            halign: 'left'
            valign: 'middle'
        TextInput:
            id: search
            hint_text: 'Search your downloads'
            multiline: False
            size_hint_y: None
            height: dp(46)
            padding: dp(14), dp(13), dp(14), dp(10)
            background_normal: ''
            background_active: ''
            background_color: 1, 1, 1, .08
            foreground_color: 1, 1, 1, 1
            hint_text_color: 1, 1, 1, .4
            cursor_color: 1, 1, 1, 1
            on_text: app.refresh_history(self.text)
        ScrollView:
            do_scroll_x: False
            bar_width: 0
            BoxLayout:
                id: box
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(10)
                Label:
                    id: empty
                    text: 'Your finished downloads show up here.'
                    color: 1, 1, 1, .45
                    halign: 'center'
                    size_hint_y: None
                    height: dp(120)

<MoreScreen>:
    name: 'more'
    ScrollView:
        do_scroll_x: False
        bar_width: 0
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(14)
            Label:
                text: 'More'
                font_size: sp(26)
                bold: True
                size_hint_y: None
                height: dp(46)
                text_size: self.size
                halign: 'left'
                valign: 'middle'
            Card:
                orientation: 'vertical'
                size_hint_y: None
                height: dp(120)
                padding: dp(16)
                spacing: dp(6)
                Label:
                    text: 'Stats'
                    bold: True
                    color: 1, 1, 1, 1
                    size_hint_y: None
                    height: dp(22)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
                Label:
                    text: app.stats_text
                    text_size: self.size
                    halign: 'left'
                    valign: 'top'
                Label:
                    text: app.save_text
                    color: 1, 1, 1, .55
                    font_size: sp(12)
                    size_hint_y: None
                    height: dp(20)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
            Card:
                orientation: 'vertical'
                size_hint_y: None
                height: dp(118)
                padding: dp(16)
                spacing: dp(8)
                BoxLayout:
                    size_hint_y: None
                    height: dp(44)
                    Label:
                        text: 'Auto-detect links in clipboard'
                        text_size: self.size
                        halign: 'left'
                        valign: 'middle'
                    Switch:
                        size_hint_x: None
                        width: dp(90)
                        active: app.cfg.get('auto_paste', True)
                        on_active: app.set_cfg('auto_paste', self.active)
                Label:
                    text: 'Copy a video link in any app, then open VideoGrab. You can also use Share > VideoGrab from other apps.'
                    color: 1, 1, 1, .5
                    font_size: sp(12)
                    text_size: self.width, None
                    halign: 'left'
                    valign: 'top'
            Card:
                orientation: 'vertical'
                size_hint_y: None
                height: dp(170)
                padding: dp(16)
                spacing: dp(8)
                Label:
                    text: 'Download engine'
                    bold: True
                    color: 1, 1, 1, 1
                    size_hint_y: None
                    height: dp(22)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
                Label:
                    text: app.engine_text
                    size_hint_y: None
                    height: dp(22)
                    text_size: self.size
                    halign: 'left'
                    valign: 'middle'
                Label:
                    text: 'Websites change often. If a site stops working, update the engine. No new APK needed.'
                    color: 1, 1, 1, .5
                    font_size: sp(12)
                    text_size: self.width, None
                    halign: 'left'
                    valign: 'top'
                RBtn:
                    text: 'Update engine'
                    size_hint_y: None
                    height: dp(44)
                    on_release: app.update_engine()
            RBtn:
                text: 'Clear library list'
                size_hint_y: None
                height: dp(46)
                on_release: app.clear_history()
            Label:
                text: 'Only download videos you have the right to save.'
                color: 1, 1, 1, .4
                font_size: sp(12)
                size_hint_y: None
                height: dp(30)
'''


# ----------------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------------
class VideoGrabApp(App):
    title = 'VideoGrab'
    quality = StringProperty('best')
    queue_label = StringProperty('Queue')
    stats_text = StringProperty('No downloads yet.')
    engine_text = StringProperty('Engine: loading...')
    save_text = StringProperty('Saved to: Downloads/VideoGrab')

    # ---- lifecycle ---------------------------------------------------------
    def build(self):
        global DATA_DIR, ENGINE_ZIP, CFG_FILE, HIST_FILE
        DATA_DIR = self.user_data_dir
        os.makedirs(DATA_DIR, exist_ok=True)
        ENGINE_ZIP = os.path.join(DATA_DIR, 'yt-dlp-engine.zip')
        CFG_FILE = os.path.join(DATA_DIR, 'settings.json')
        HIST_FILE = os.path.join(DATA_DIR, 'history.json')
        setup_engine_path()

        self.cfg = load_json(CFG_FILE, {})
        self.cfg.setdefault('auto_paste', True)
        self.cfg.setdefault('quality', 'best')
        self.history = load_json(HIST_FILE, [])
        self.jobs = []
        self.rows = {}
        self.info = None
        self.running = False
        self.current = 'home'
        self.auto_pasted = ''
        self.last_clip = ''
        self.quality = self.cfg.get('quality', 'best')
        self.ready = False

        Window.softinput_mode = 'below_target'
        Window.bind(on_keyboard=self.on_key)
        Builder.load_string(KV)
        return Root()

    def on_start(self):
        sm = self.root.ids.sm
        self.home = sm.get_screen('home')
        self.queue_screen = sm.get_screen('queue')
        self.library = sm.get_screen('library')

        # restore chosen quality chip
        for chip in self.home.ids.chips.children:
            if chip.key == self.quality:
                chip.state = 'down'
        self.ready = True
        self.home.ids.url_input.bind(text=self._on_url_text)

        if platform == 'android':
            self._ask_permissions()
            try:
                from android import activity
                activity.bind(on_new_intent=self.on_new_intent)
            except Exception:
                pass
            Clock.schedule_once(lambda dt: self.handle_android_intent(), 0.6)
            self.save_text = 'Saved to: Downloads/VideoGrab'
        else:
            self.save_text = 'Saved to: ~/Downloads/VideoGrab'

        Clock.schedule_interval(self.tick, 0.4)
        Clock.schedule_once(lambda dt: self.check_clipboard(), 0.9)
        self.refresh_history()
        threading.Thread(target=self._warm_engine, daemon=True).start()

    def on_pause(self):
        return True

    def on_resume(self):
        Clock.schedule_once(lambda dt: self.check_clipboard(), 0.4)

    def on_key(self, window, key, *args):
        if key == 27:  # Android back / Esc
            if self.current == 'player':
                self.close_player()
                return True
            if self.current != 'home':
                self.go('home')
                return True
            if platform == 'android':
                try:
                    _activity().moveTaskToBack(True)
                    return True
                except Exception:
                    return False
        return False

    def _ask_permissions(self):
        try:
            if android_sdk() < 29:
                from android.permissions import request_permissions, Permission
                request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                                     Permission.READ_EXTERNAL_STORAGE])
        except Exception:
            pass

    def _warm_engine(self):
        v = engine_version()
        Clock.schedule_once(lambda dt: setattr(self, 'engine_text', 'Engine: yt-dlp ' + v))

    # ---- navigation / toast ------------------------------------------------
    def go(self, name):
        self.current = name
        self.root.ids.sm.current = name
        nav = self.root.ids.nav_bar
        if name == 'player':
            nav.height = 0
            nav.opacity = 0
        else:
            nav.height = dp(60)
            nav.opacity = 1
            nav_id = self.root.ids.get('nav_' + name)
            if nav_id is not None:
                nav_id.state = 'down'

    def toast(self, msg, secs=2.4):
        t = self.root.ids.toast
        t.text = msg
        Animation.cancel_all(t)
        t.opacity = 0
        anim = (Animation(opacity=1, d=0.15) + Animation(opacity=1, d=secs)
                + Animation(opacity=0, d=0.3))
        anim.start(t)

    def set_cfg(self, key, value):
        self.cfg[key] = value
        save_json(CFG_FILE, self.cfg)

    def set_quality(self, key):
        if not getattr(self, 'ready', False):
            return
        self.quality = key
        self.set_cfg('quality', key)

    # ---- link input --------------------------------------------------------
    def _on_url_text(self, inst, text):
        if self.info and clean_url(text) != self.info.get('input_url'):
            self.info = None
            self.home.has_info = False

    def paste_link(self):
        txt = Clipboard.paste() or ''
        url = clean_url(txt)
        if not url:
            self.toast('No link found in the clipboard')
            return
        self.home.ids.url_input.text = url
        self.toast('Link pasted')

    def clear_link(self):
        self.home.ids.url_input.text = ''
        self.info = None
        self.home.has_info = False

    def check_clipboard(self):
        if not self.cfg.get('auto_paste', True):
            return
        try:
            txt = Clipboard.paste() or ''
        except Exception:
            return
        url = clean_url(txt)
        if not url or not txt.strip().lower().startswith('http'):
            return
        if txt == self.last_clip:
            return
        ti = self.home.ids.url_input
        if ti.text.strip() == '' or ti.text == self.auto_pasted:
            self.last_clip = txt
            self.auto_pasted = url
            ti.text = url
            self.toast('Link detected from clipboard')

    def set_link(self, text, auto_analyze=True):
        url = clean_url(text)
        if not url:
            return
        self.go('home')
        self.home.ids.url_input.text = url
        self.auto_pasted = url
        self.toast('Link received')
        if auto_analyze:
            Clock.schedule_once(lambda dt: self.analyze(), 0.3)

    # ---- android share-in --------------------------------------------------
    def on_new_intent(self, intent):
        Clock.schedule_once(lambda dt: self.handle_android_intent(intent), 0)

    def handle_android_intent(self, intent=None):
        try:
            from jnius import autoclass
            it = intent or _activity().getIntent()
            if it is None:
                return
            Intent = autoclass('android.content.Intent')
            if it.getAction() == Intent.ACTION_SEND:
                text = it.getStringExtra(Intent.EXTRA_TEXT)
                it.setAction(Intent.ACTION_MAIN)
                if text:
                    self.set_link(text)
        except Exception:
            pass

    # ---- analyze -----------------------------------------------------------
    def analyze(self):
        url = clean_url(self.home.ids.url_input.text)
        if not url:
            self.toast('Paste a video link first')
            return
        if self.home.busy:
            return
        self.home.busy = True
        threading.Thread(target=self._analyze_thread, args=(url,), daemon=True).start()

    def _analyze_thread(self, url):
        try:
            yt = load_ytdlp()
            opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
                    'skip_download': True, 'socket_timeout': 25, 'cachedir': False}
            with yt.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and info.get('entries'):
                entries = [e for e in info['entries'] if e]
                if entries:
                    info = entries[0]
            heights = sorted({f.get('height') for f in (info.get('formats') or [])
                              if f.get('height')})
            small = {
                'input_url': url,
                'url': info.get('webpage_url') or url,
                'title': info.get('title') or 'Untitled video',
                'uploader': info.get('uploader') or info.get('channel') or '',
                'duration': info.get('duration'),
                'thumb': pick_thumb(info),
                'site': info.get('extractor_key') or '',
                'max_h': heights[-1] if heights else 0,
            }
            Clock.schedule_once(lambda dt: self._show_info(small))
        except Exception as e:
            msg = friendly_error(e)
            Clock.schedule_once(lambda dt: self._analyze_failed(msg))

    def _show_info(self, small):
        self.home.busy = False
        self.info = small
        self.home.p_title = small['title']
        bits = [b for b in (small['uploader'], fmt_dur(small['duration']),
                            ('up to %dp' % small['max_h']) if small['max_h'] else '') if b]
        self.home.p_meta = '  |  '.join(bits)
        self.home.p_thumb = small['thumb']
        self.home.has_info = True

    def _analyze_failed(self, msg):
        self.home.busy = False
        self.toast(msg, 4)

    # ---- queue -------------------------------------------------------------
    def queue_download(self):
        url = clean_url(self.home.ids.url_input.text)
        if not url:
            self.toast('Paste a video link first')
            return
        info = self.info if (self.info and self.info.get('input_url') == url) else None
        job = {
            'id': uuid.uuid4().hex[:8],
            'url': url,
            'title': info['title'] if info else url,
            'thumb': info['thumb'] if info else '',
            'q': self.quality,
            'status': 'queued',
            'progress': 0.0,
            'speed': None, 'eta': None,
            'error': '', 'cancel': False,
            'path': '', 'uri': '', 'size': 0,
        }
        self.jobs.append(job)
        row = JobRow()
        row.job_id = job['id']
        self.rows[job['id']] = row
        self.queue_screen.ids.box.add_widget(row)
        self.update_row(row, job)
        self._sync_empty()
        self.toast('Added to queue')
        self._pump()

    def _sync_empty(self):
        e = self.queue_screen.ids.empty
        has = bool(self.jobs)
        e.height = 0 if has else dp(120)
        e.opacity = 0 if has else 1

    def _pump(self):
        if self.running:
            return
        job = next((j for j in self.jobs if j['status'] == 'queued'), None)
        if not job:
            return
        self.running = True
        job['status'] = 'fetching'
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _job_finished(self):
        self.running = False
        self._pump()

    def job_action(self, job_id):
        job = next((j for j in self.jobs if j['id'] == job_id), None)
        if not job:
            return
        st = job['status']
        if st == 'queued':
            self._remove_job(job)
        elif st in ('fetching', 'downloading'):
            job['cancel'] = True
        elif st == 'saving':
            self.toast('Almost done...')
        elif st in ('error', 'cancelled'):
            job.update(status='queued', progress=0.0, error='', cancel=False,
                       speed=None, eta=None, logged=False, notified=False)
            self._pump()
        elif st == 'done':
            self._remove_job(job)

    def _remove_job(self, job):
        row = self.rows.pop(job['id'], None)
        if row:
            self.queue_screen.ids.box.remove_widget(row)
        if job in self.jobs:
            self.jobs.remove(job)
        self._sync_empty()

    def clear_finished(self):
        for j in list(self.jobs):
            if j['status'] in ('done', 'error', 'cancelled'):
                self._remove_job(j)

    def update_row(self, row, j):
        st = j['status']
        row.title = j['title']
        row.progress = j['progress'] if st != 'done' else 1.0
        row.status_color = [1, 1, 1, 0.65]
        q = QUALITY_LABEL.get(j['q'], '')
        if st == 'queued':
            row.status_text = 'Waiting in queue  |  ' + q
            row.action_text = 'Cancel'
        elif st == 'fetching':
            row.status_text = 'Fetching video info...'
            row.action_text = 'Cancel'
        elif st == 'downloading':
            parts = ['%d%%' % int(j['progress'] * 100)]
            if j['speed']:
                parts.append(fmt_size(j['speed']) + '/s')
            if j['eta'] is not None:
                parts.append('ETA ' + fmt_dur(j['eta']))
            row.status_text = '  |  '.join(parts)
            row.action_text = 'Cancel'
        elif st == 'saving':
            row.status_text = 'Saving to Downloads...'
            row.action_text = 'Cancel'
        elif st == 'done':
            row.status_text = 'Done  |  ' + fmt_size(j['size'])
            row.status_color = [1, 1, 1, 1]
            row.action_text = 'Remove'
        elif st == 'error':
            row.status_text = j['error'] or 'Failed'
            row.status_color = [1, 1, 1, .85]
            row.action_text = 'Retry'
        elif st == 'cancelled':
            row.status_text = 'Cancelled'
            row.action_text = 'Retry'

    def tick(self, dt):
        for j in list(self.jobs):
            row = self.rows.get(j['id'])
            if row:
                self.update_row(row, j)
            if j['status'] == 'done' and not j.get('logged'):
                j['logged'] = True
                self.add_history(j)
                self.toast('Saved: ' + j['title'][:40])
            elif j['status'] == 'error' and not j.get('notified'):
                j['notified'] = True
                self.toast(j['error'] or 'Download failed', 4)
        active = sum(1 for j in self.jobs if j['status'] in ACTIVE)
        self.queue_label = 'Queue (%d)' % active if active else 'Queue'

    # ---- the actual download (runs in a worker thread) ---------------------
    def _run_job(self, job):
        tmp = os.path.join(DATA_DIR, 'tmp')
        os.makedirs(tmp, exist_ok=True)
        last = [0.0]

        def hook(d):
            if job['cancel']:
                raise Cancelled()
            now = time.time()
            if d.get('status') == 'downloading':
                if now - last[0] < 0.25:
                    return
                last[0] = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                done = d.get('downloaded_bytes') or 0
                job['status'] = 'downloading'
                if total:
                    job['progress'] = min(done / float(total), 1.0)
                job['speed'] = d.get('speed')
                job['eta'] = d.get('eta')
            elif d.get('status') == 'finished':
                job['progress'] = 1.0

        try:
            yt = load_ytdlp()
            opts = {
                'format': FORMATS.get(job['q'], FORMATS['best']),
                'outtmpl': os.path.join(tmp, job['id'] + '_%(title).90s.%(ext)s'),
                'noplaylist': True, 'quiet': True, 'no_warnings': True,
                'noprogress': True, 'progress_hooks': [hook],
                'socket_timeout': 25, 'retries': 5, 'fragment_retries': 5,
                'concurrent_fragment_downloads': 4, 'windowsfilenames': True,
                'cachedir': False, 'overwrites': True,
            }
            with yt.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job['url'], download=True)
                if info and info.get('entries'):
                    entries = [e for e in info['entries'] if e]
                    info = entries[0] if entries else info
                if info:
                    job['title'] = info.get('title') or job['title']
                    if not job['thumb']:
                        job['thumb'] = pick_thumb(info)
                path = None
                rd = (info or {}).get('requested_downloads') or []
                if rd:
                    path = rd[0].get('filepath')
                if not path or not os.path.exists(path):
                    try:
                        path = ydl.prepare_filename(info)
                    except Exception:
                        path = None
                if not path or not os.path.exists(path):
                    cands = [p for p in glob.glob(os.path.join(tmp, job['id'] + '_*'))
                             if not p.endswith(('.part', '.ytdl'))]
                    path = max(cands, key=os.path.getsize) if cands else None
            if not path or not os.path.exists(path):
                raise RuntimeError('Download finished but the file was not found.')
            if job['cancel']:
                raise Cancelled()

            job['status'] = 'saving'
            job['progress'] = 1.0
            name = os.path.basename(path)
            if name.startswith(job['id'] + '_'):
                name = name[len(job['id']) + 1:]
            mime = guess_mime(path)
            job['size'] = os.path.getsize(path)
            job['uri'] = save_public(path, name, mime)
            job['name'] = name
            job['mime'] = mime
            job['status'] = 'done'
        except Cancelled:
            job['status'] = 'cancelled'
        except Exception as e:
            if job['cancel']:
                job['status'] = 'cancelled'
            else:
                job['status'] = 'error'
                job['error'] = friendly_error(e)
        finally:
            for p in glob.glob(os.path.join(tmp, job['id'] + '_*')):
                try:
                    os.remove(p)
                except Exception:
                    pass
            Clock.schedule_once(lambda dt: self._job_finished())

    # ---- library -----------------------------------------------------------
    def add_history(self, j):
        entry = {
            'id': j['id'], 'title': j['title'], 'url': j['url'],
            'thumb': j.get('thumb', ''), 'uri': j['uri'],
            'mime': j.get('mime', 'video/*'), 'name': j.get('name', ''),
            'size': j['size'], 'q': j['q'],
            'time': time.strftime('%d %b %Y, %H:%M'),
        }
        self.history.insert(0, entry)
        save_json(HIST_FILE, self.history)
        self.refresh_history()

    def refresh_history(self, query=None):
        if not hasattr(self, 'library'):
            return
        if query is None:
            query = self.library.ids.search.text
        q = (query or '').strip().lower()
        box = self.library.ids.box
        for w in list(box.children):
            if isinstance(w, HistRow):
                box.remove_widget(w)
        shown = 0
        for e in self.history:
            if q and q not in (e.get('title', '') + ' ' + e.get('name', '')).lower():
                continue
            row = HistRow()
            row.entry = e
            row.title = e.get('title', '')
            row.meta = '%s  |  %s  |  %s' % (QUALITY_LABEL.get(e.get('q'), ''),
                                             fmt_size(e.get('size')), e.get('time', ''))
            row.thumb = e.get('thumb', '')
            box.add_widget(row)
            shown += 1
        empty = self.library.ids.empty
        empty.height = 0 if shown else dp(120)
        empty.opacity = 0 if shown else 1
        empty.text = ('No matches.' if (q and self.history)
                      else 'Your finished downloads show up here.')
        self.refresh_stats()

    def refresh_stats(self):
        n = len(self.history)
        total = sum(e.get('size', 0) for e in self.history)
        if n:
            self.stats_text = '%d download%s  |  %s saved' % (n, '' if n == 1 else 's', fmt_size(total))
        else:
            self.stats_text = 'No downloads yet.'

    def _playback_path(self, uri):
        if not uri:
            return ''
        if platform != 'android':
            if uri.startswith('file://'):
                uri = unquote(uri[7:])
                if os.name == 'nt' and re.match(r'^/[A-Za-z]:', uri):
                    uri = uri[1:]
            return uri if os.path.exists(uri) else ''
        if uri.startswith('file://'):
            path = uri[7:]
            return path if os.path.exists(path) else ''
        if not uri.startswith('content://'):
            return uri if os.path.exists(uri) else ''

        try:
            from jnius import autoclass
            Uri = autoclass('android.net.Uri')
            ins = _activity().getContentResolver().openInputStream(Uri.parse(uri))
            if ins is None:
                return ''
            os.makedirs(os.path.join(DATA_DIR, 'playback'), exist_ok=True)
            path = os.path.join(DATA_DIR, 'playback', 'current.mp4')
            buf = bytearray(256 * 1024)
            with open(path, 'wb') as out:
                while True:
                    n = ins.read(buf)
                    if n <= 0:
                        break
                    out.write(buf[:n])
            ins.close()
            return path
        except Exception:
            return ''

    def play(self, e):
        uri = e.get('uri', '')
        if platform == 'android':
            if not open_media(uri, e.get('mime', 'video/*')):
                self.toast('No compatible video player could open this file.', 3.5)
            return

        path = self._playback_path(uri)
        if not path:
            self.toast('Could not open the file. It may have been moved or deleted.', 3.5)
            return
        player = self.root.ids.sm.get_screen('player')
        player.ids.player_title.text = e.get('title', 'Now playing')
        video = player.ids.video
        video.state = 'stop'
        video.source = ''
        video.source = path
        video.state = 'play'
        self.go('player')

    def close_player(self):
        player = self.root.ids.sm.get_screen('player')
        video = player.ids.video
        video.state = 'stop'
        video.source = ''
        self.go('library')

    def share(self, e):
        if not share_media(e.get('uri', ''), e.get('mime', 'video/*'), e.get('url', '')):
            self.toast('Sharing is not available here')

    def delete_entry(self, e):
        delete_media(e.get('uri', ''))
        if e in self.history:
            self.history.remove(e)
        save_json(HIST_FILE, self.history)
        self.refresh_history()
        self.toast('Deleted')

    def clear_history(self):
        self.history = []
        save_json(HIST_FILE, self.history)
        self.refresh_history()
        self.toast('Library list cleared (files are kept)')

    # ---- engine update -----------------------------------------------------
    def update_engine(self):
        self.toast('Downloading latest engine...', 3)
        threading.Thread(target=self._update_engine, daemon=True).start()

    def _update_engine(self):
        import ssl
        import zipimport
        import zipfile
        import urllib.request
        try:
            try:
                ctx = ssl.create_default_context(cafile=certifi.where()) if certifi else None
            except Exception:
                ctx = None
            req = urllib.request.Request(ENGINE_URL, headers={'User-Agent': 'VideoGrab'})
            tmp = ENGINE_ZIP + '.new'
            with urllib.request.urlopen(req, timeout=90, context=ctx) as r, open(tmp, 'wb') as f:
                shutil.copyfileobj(r, f)
            if not zipfile.is_zipfile(tmp):
                raise RuntimeError('Bad download')
            os.replace(tmp, ENGINE_ZIP)
            try:
                zipimport._zip_directory_cache.pop(ENGINE_ZIP, None)
            except Exception:
                pass
            sys.path_importer_cache.pop(ENGINE_ZIP, None)
            purge_ytdlp()
            if ENGINE_ZIP in sys.path:
                sys.path.remove(ENGINE_ZIP)
            sys.path.insert(0, ENGINE_ZIP)
            v = engine_version()
            if v == 'unavailable':
                raise RuntimeError('New engine failed to load')
            Clock.schedule_once(lambda dt: setattr(self, 'engine_text', 'Engine: yt-dlp ' + v))
            Clock.schedule_once(lambda dt: self.toast('Engine updated to ' + v, 3))
        except Exception as e:
            try:
                if os.path.exists(ENGINE_ZIP + '.new'):
                    os.remove(ENGINE_ZIP + '.new')
            except Exception:
                pass
            msg = 'Update failed: ' + friendly_error(e)
            Clock.schedule_once(lambda dt: self.toast(msg, 4))


if __name__ == '__main__':
    VideoGrabApp().run()
