#!/usr/bin/env python3
"""AirPlay 2 to Sonos bridge, built on shairport-sync.

One shairport-sync instance runs per Sonos room and handles AirPlay 2. Speakers
selected together on a sender form a session: the first one plays the stream, the
others join its Sonos group, so Sonos does the room-to-room synchronisation.
"""
import argparse
import base64
import collections
import hashlib
import html
import http.server
import os
import queue
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
ZONE_PLAYER = "urn:schemas-upnp-org:device:ZonePlayer:1"
NO_INFO = "No track information"

START_WAIT = 2.0        # senders give title and cover just after starting, while nothing plays
TRACK_SETTLE = 2.0      # a track change is a burst of items, and zapping several bursts
STOP_GRACE = 2.5        # a track change is a short pend then prsm, not a stop
WATCH_GRACE = 10.0      # how long Sonos may take to load a stream
LOST_POLLS = 5          # polls, one per second, before believing Sonos moved on
ERROR_RETRIES = 3       # reloads of a stream Sonos failed to play
DELIVER_RETRIES = 10    # attempts to reach Sonos, two seconds apart
UNREACHABLE_POLLS = 15  # failed polls before giving up on a session
STALL_TIMEOUT = 20.0    # "playing" without audio for this long means a stuck instance
DISCOVERY_INTERVAL = 60.0
ICY_INTERVAL = 16000
CLIENT_QUEUE = 1024     # 4 KiB chunks, about 100 s of MP3: past that a client is stuck


CODECS = {
    "mp3": {
        "ext": "mp3", "type": "audio/mpeg", "scheme": "x-rincon-mp3radio://", "icy": True,
        "protocol": "x-rincon-mp3radio:*:*:*",
        "ffmpeg": ["-c:a", "libmp3lame", "-b:a", "320k", "-reservoir", "0", "-id3v2_version", "0",
                   "-write_xing", "0", "-f", "mp3"],
    },
    "flac": {
        "ext": "flac", "type": "audio/flac", "scheme": "", "icy": False,
        "protocol": "http-get:*:audio/flac:*",
        "ffmpeg": ["-af", "aresample=out_sample_fmt=s16:dither_method=triangular", "-c:a", "flac",
                   "-compression_level", "5", "-f", "flac"],
    },
}


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


# ---------------------------------------------------------------- pure helpers
def sonos_volume(db, limit):
    """AirPlay's slider is -30..0 dB (-144 when muted); all of it maps to 0..limit."""
    return round(limit * min(1, max(0, (db + 30) / 30)))


def parse_items(buf):
    """Returns the complete (code, data) metadata items of buf, and what is left to read."""
    items, end = [], 0
    pattern = (rb'<item><type>\w+</type><code>(\w+)</code><length>\d+</length>'
               rb'(?:\s*<data encoding="base64">\s*(.*?)</data>)?\s*</item>')
    for m in re.finditer(pattern, buf, re.S):
        items.append((bytes.fromhex(m.group(1).decode()).decode("latin1"), base64.b64decode(m.group(2) or b"")))
        end = m.end()
    return items, buf[end:]


def display(title, artist, art, source):
    """(main line, second line) for Sonos. A track is shown only with both title and cover."""
    if title and art:
        return title, artist or ""
    main = f"AirPlay - {source}" if source else "AirPlay"
    if artist and title:
        return main, f"{artist} - {title}"
    return main, title or artist or NO_INFO


def didl(uri, protocol, main, second, art_url):
    art = f"<upnp:albumArtURI>{escape(art_url)}</upnp:albumArtURI>" if art_url else ""
    return ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="1" parentID="0" restricted="1">'
            f"<dc:title>{escape(main)}</dc:title><dc:creator>{escape(second)}</dc:creator>"
            f"<upnp:artist>{escape(second)}</upnp:artist>{art}"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f'<res protocolInfo="{escape(protocol)}">{escape(uri)}</res></item></DIDL-Lite>')


def icy_block(text):
    """An ICY metadata block: a length byte, then text padded to 16-byte blocks."""
    data = b""
    if text:
        data = f"StreamTitle='{text.replace(chr(39), chr(8217))}';".encode()[:255 * 16]
    blocks = (len(data) + 15) // 16
    return bytes([blocks]) + data.ljust(blocks * 16, b"\0")


def flac_header_end(data):
    """Offset of the first audio frame, or None if data doesn't hold the whole header yet."""
    if len(data) < 4:
        return None
    pos = 4
    while pos + 4 <= len(data):
        last, size = data[pos] & 0x80, int.from_bytes(data[pos + 1:pos + 4], "big")
        pos += 4 + size
        if last:
            return pos if pos <= len(data) else None
    return None


def flac_frame_start(data):
    """Offset of the first real frame in data. Its 2-byte sync also occurs inside audio, so
    the whole frame header is checked: stereo 16-bit as we encode it, and its CRC-8."""
    pos = data.find(b"\xff\xf8")
    while pos >= 0:
        size = flac_frame_header_size(data[pos:pos + 16])
        if size and flac_crc8(data[pos:pos + size - 1]) == data[pos + size - 1]:
            return pos
        pos = data.find(b"\xff\xf8", pos + 1)
    return -1


def flac_frame_header_size(h):
    if len(h) < 16 or h[3] & 0x0f != 0x08 or h[3] >> 4 not in (0x1, 0x8, 0x9, 0xa):
        return None
    leading = 0
    while leading < 8 and h[4] & (0x80 >> leading):
        leading += 1
    number = 1 if leading == 0 else leading
    if leading == 1 or number > 6:
        return None
    blocksize = {6: 1, 7: 2}.get(h[2] >> 4, 0)
    rate = {12: 1, 13: 2, 14: 2}.get(h[2] & 0x0f, 0)
    return 4 + number + blocksize + rate + 1


def flac_crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc << 1 ^ 0x07 if crc & 0x80 else crc << 1) & 0xff
    return crc


def field(xml, name):
    m = re.search(f"<{name}>(.*?)</{name}>", xml or "", re.S)
    return html.unescape(m.group(1)) if m else ""


def sd_notify(message):
    """systemd's notify protocol, so that a hung bridge gets restarted."""
    path = os.environ.get("NOTIFY_SOCKET")
    if path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode(), "\0" + path[1:] if path.startswith("@") else path)


def local_ip(peer):
    """The address of the interface that reaches peer: it can change with the network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((peer, 1400))
        return s.getsockname()[0]


def describe(ip):
    """(rincon, room) of a Sonos, or None for one that plays no audio or can't be reached."""
    try:
        xml = urllib.request.urlopen(f"http://{ip}:1400/xml/device_description.xml", timeout=4).read().decode()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if re.search("Boost|Bridge", field(xml, "modelName")):
        return None
    rincon, room = field(xml, "UDN").replace("uuid:", ""), field(xml, "roomName")
    return (rincon, room) if rincon and room else None


def ssdp_search(timeout=3.0):
    """IPs of the Sonos answering on the network."""
    request = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 2\r\nST: {ZONE_PLAYER}\r\n\r\n").encode()
    found = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        s.sendto(request, ("239.255.255.250", 1900))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            if ZONE_PLAYER.encode() in data:
                found.add(addr[0])
    return found


# ---------------------------------------------------------------- state
class Speaker:
    def __init__(self, rincon, ip, room, name, port, pipe):
        self.rincon, self.ip, self.room, self.name, self.port, self.pipe = rincon, ip, room, name, port, pipe
        self.proc = None
        self.started_at = 0.0
        self.respawn_at = 0.0
        self.kill_at = 0.0
        self.backoff = 1.0
        self.log_tail = collections.deque(maxlen=40)
        self.reset()

    def reset(self):
        self.playing = False
        self.changed_at = 0.0
        self.last_audio = 0.0
        self.client = None
        self.source = None
        self.title = self.artist = self.art = None
        self.info_at = 0.0
        self.volume_db = None       # last volume from the sender
        self.volume_due = None      # to be sent to Sonos
        self.joined = False
        self.lost = 0


class Session:
    """What is played from one sender: one encoder, one stream, one Sonos group."""

    def __init__(self, key, primary):
        self.key, self.primary = key, primary
        self.id = secrets.token_hex(4)
        self.encoder = None
        self.encoder_restarts = 0
        self.uri = None
        self.protocol = None
        self.delivered = False      # Sonos was told to play and shows the current track
        self.shown = None
        self.attempts = 0
        self.retry_at = 0.0
        self.watch_after = 0.0
        self.poll_at = 0.0
        self.lost = self.stopped = self.unreachable = self.errors = 0
        self.icy = ""
        self.icy_serial = 0
        self.clients = []
        self.header = b""           # FLAC: every new connection needs it
        self.header_done = False

    def key_of_track(self):
        sp = self.primary
        return sp.title, sp.artist, bool(sp.art)


# ---------------------------------------------------------------- the bridge
class Bridge:
    def __init__(self, args):
        self.args = args
        self.codec = CODECS[args.codec]
        self.speakers = {}          # by rincon
        self.sessions = {}          # by sender
        self.next_port = args.first_port
        self.arts = collections.OrderedDict()
        self.nqptp = None
        self.discover_now = threading.Event()
        self.threads = {}
        self.tick_failures = 0
        self.last_session_at = None # set when a session ended and nothing cleaned up since
        self.running = True

    # ------------------------------------------------------------ Sonos calls
    def soap(self, ip, service, action, args=""):
        """The body of the answer, or None if the speaker couldn't be reached or refused."""
        path = "AVTransport" if service == AVT else "RenderingControl"
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}"><InstanceID>0</InstanceID>{args}</u:{action}>'
                "</s:Body></s:Envelope>")
        request = urllib.request.Request(f"http://{ip}:1400/MediaRenderer/{path}/Control", body.encode(), {
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
        try:
            return urllib.request.urlopen(request, timeout=3).read().decode()
        except urllib.error.HTTPError as e:
            log(f"Sonos {ip}: {action} refused: {e.code}")
        except (urllib.error.URLError, OSError) as e:
            log(f"Sonos {ip}: {action} failed: {e}")
        return None

    def play(self, sp, uri, metadata=""):
        return (self.soap(sp.ip, AVT, "SetAVTransportURI", f"<CurrentURI>{escape(uri)}</CurrentURI>"
                          f"<CurrentURIMetaData>{escape(metadata)}</CurrentURIMetaData>") is not None
                and self.soap(sp.ip, AVT, "Play", "<Speed>1</Speed>") is not None)

    def leave_group(self, sp):
        sp.joined = False
        log(f"{sp.room}: leaves the group")
        self.soap(sp.ip, AVT, "BecomeCoordinatorOfStandaloneGroup")

    # ------------------------------------------------------------ processes
    def spawn_nqptp(self):
        if self.args.nqptp:
            self.nqptp = subprocess.Popen([self.args.nqptp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def spawn(self, sp):
        cmd = [self.args.shairport, "-c", self.args.config, "-a", sp.name, f"--port={sp.port}", "-o", "stdout",
               f"--metadata-pipename={sp.pipe}", "-vv"]
        sp.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for target, stream in ((self.read_audio, sp.proc.stdout), (self.read_log, sp.proc.stderr)):
            threading.Thread(target=target, args=(sp, stream), daemon=True).start()
        if sp.rincon not in self.threads:
            self.threads[sp.rincon] = threading.Thread(target=self.read_meta, args=(sp,), daemon=True)
            self.threads[sp.rincon].start()

    def terminate(self, sp, now):
        if sp.proc and sp.proc.poll() is None:
            sp.proc.terminate()
            sp.kill_at = now + 3.0

    def restart(self, sp, now, why):
        """Closes the sender's connection to this speaker, then offers it again."""
        log(f"{sp.name}: restarting, {why}")
        self.terminate(sp, now)
        sp.reset()
        sp.backoff = 1.0
        sp.respawn_at = now + 0.5

    def supervise(self, now):
        if self.args.nqptp and self.nqptp and self.nqptp.poll() is not None:
            log("nqptp died, restarting everything that relies on it")
            self.spawn_nqptp()
            for sp in list(self.speakers.values()):
                self.restart(sp, now, "nqptp restarted")
        for sp in list(self.speakers.values()):
            if sp.proc is None or sp.proc.poll() is not None:
                if sp.proc is not None and sp.respawn_at == 0.0:
                    # not asked for: back off, a crash loop must not burn the machine
                    tail = " | ".join(list(sp.log_tail)[-3:])
                    sp.backoff = 1.0 if now - sp.started_at > 30 else min(30.0, sp.backoff * 2)
                    log(f"{sp.name}: instance exited, restarting in {sp.backoff:.0f} s. Last output: {tail[:300]}")
                    sp.reset()
                    sp.respawn_at = now + sp.backoff
                elif now >= sp.respawn_at:
                    self.spawn(sp)
                    sp.started_at, sp.respawn_at, sp.kill_at = now, 0.0, 0.0
                    log(f"{sp.name}: instance started on port {sp.port}")
            elif sp.kill_at and now >= sp.kill_at:
                sp.proc.kill()
                sp.kill_at = 0.0

    def read_audio(self, sp, out):
        while True:
            data = out.read1(65536)
            if not data:
                return
            sp.last_audio = time.monotonic()
            session = self.sessions.get(sp.client or sp.rincon)
            if session and session.primary is sp and session.encoder:
                try:
                    session.encoder.stdin.write(data)
                except (OSError, ValueError):
                    pass

    def read_log(self, sp, err):
        source = re.compile(rb'Source: "([^"]*)"')
        for line in err:
            sp.log_tail.append(line.decode("utf8", "replace").strip())
            m = source.search(line)
            if m:
                sp.source = m.group(1).decode("utf8", "replace")

    def read_meta(self, sp):
        while self.running:
            if not os.path.exists(sp.pipe):
                time.sleep(0.5)
                continue
            buf = b""
            with open(sp.pipe, "rb", buffering=0) as f:
                while True:
                    data = f.read(1 << 16)
                    if not data:
                        break
                    items, buf = parse_items(buf + data)
                    for code, value in items:
                        self.on_item(sp, code, value, time.monotonic())

    def on_item(self, sp, code, value, now):
        if code == "clip":
            sp.client = value.decode("utf8", "replace") or None
        elif code == "pbeg":
            sp.title = sp.artist = sp.art = sp.volume_db = None
        if code in ("pbeg", "prsm"):
            if not sp.playing:
                sp.playing, sp.changed_at, sp.last_audio = True, now, now
                log(f"{sp.name}: playing")
        elif code in ("pend", "paus", "aend"):
            if sp.playing:
                sp.playing, sp.changed_at = False, now
                log(f"{sp.name}: {code}")
        elif code in ("minm", "asar"):
            text = value.decode("utf8", "replace") or None
            attribute = "title" if code == "minm" else "artist"
            if getattr(sp, attribute) != text:
                setattr(sp, attribute, text)
                sp.info_at = now
        elif code == "PICT" and value != sp.art:
            sp.art, sp.info_at = value or None, now
        elif code == "pvol":
            db = float(value.decode().split(",")[0])
            if db != sp.volume_db:
                volume = sonos_volume(db, self.args.max_volume)
                # the slider may have been left high: a session must not start at full blast
                sp.volume_due = min(volume, self.args.start_volume) if sp.volume_db is None else volume
                sp.volume_db = db

    # ------------------------------------------------------------ discovery
    def add_speaker(self, ip, rincon, room):
        known = self.speakers.get(rincon)
        if known:
            if known.ip != ip:
                log(f"{room}: moved from {known.ip} to {ip}")
                known.ip = ip
            return
        # the two sides of a stereo pair share a room name, and only one must be offered
        if any(s.room == room for s in self.speakers.values()):
            return
        port, self.next_port = self.next_port, self.next_port + 1
        pipe = os.path.join(self.args.runtime_dir, f"meta-{port}")
        self.speakers[rincon] = Speaker(rincon, ip, room, self.args.name.format(room=room), port, pipe)
        log(f"{room}: found at {ip}")
        # what a previous run of this bridge left: Sonos would keep trying to reach it
        if "/ap2-" in field(self.soap(ip, AVT, "GetMediaInfo"), "CurrentURI"):
            log(f"{room}: stopping a stream left by a previous run")
            self.soap(ip, AVT, "Stop")

    def discover(self):
        found = set(self.args.sonos)
        try:
            found |= ssdp_search()
        except OSError as e:
            log(f"discovery: {e}")
        for ip in found:
            info = describe(ip)
            if info:
                self.add_speaker(ip, *info)

    def discovery_loop(self):
        while self.running:
            self.discover()
            self.discover_now.wait(DISCOVERY_INTERVAL)
            self.discover_now.clear()

    # ------------------------------------------------------------ encoder
    def start_encoder(self, session):
        session.header, session.header_done = b"", False
        session.encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s32le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
             *self.codec["ffmpeg"], "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self.read_encoder, args=(session, session.encoder), daemon=True).start()

    def stop_encoder(self, session):
        encoder, session.encoder = session.encoder, None
        if encoder:
            encoder.stdin.close()
            encoder.terminate()

    def read_encoder(self, session, encoder):
        pending = b""
        while True:
            data = encoder.stdout.read(4096)
            if not data:
                return
            if self.codec is CODECS["flac"] and not session.header_done:
                pending += data
                end = flac_header_end(pending)
                if end is None:
                    continue
                session.header, session.header_done = pending[:end], True
                data = pending[end:]
            for q in list(session.clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    # ------------------------------------------------------------ sessions
    def art_url(self, host, art):
        if not art:
            return None
        key = hashlib.sha1(art).hexdigest()[:16]
        self.arts[key] = art
        while len(self.arts) > 8:
            self.arts.popitem(last=False)
        return f"http://{host}:{self.args.http_port}/art/{key}.jpg"

    def open_session(self, key, primary, now):
        session = Session(key, primary)
        self.sessions[key] = session
        self.start_encoder(session)
        log(f"{primary.room}: session opened")
        return session

    def close_session(self, session, stop, now):
        self.sessions.pop(session.key, None)
        self.stop_encoder(session)
        self.last_session_at = now
        for sp in self.speakers.values():
            if sp is not session.primary and sp.joined and (sp.client or sp.rincon) == session.key:
                self.leave_group(sp)
        if stop:
            log(f"{session.primary.room}: stop")
            self.soap(session.primary.ip, AVT, "Stop")

    def release(self, session, now, why):
        """Sonos was given something else: let it play and disconnect the sender."""
        log(f"{session.primary.room}: {why}, disconnecting the sender")
        members = [s for s in self.speakers.values() if s.joined and (s.client or s.rincon) == session.key]
        self.sessions.pop(session.key, None)
        self.stop_encoder(session)
        self.last_session_at = now
        for sp in [session.primary, *members]:
            self.restart(sp, now, "the speaker moved to another source")

    def deliver(self, session, now):
        """Sonos reads title and cover only when told what to play, which restarts its stream."""
        sp = session.primary
        host = local_ip(sp.ip)
        stream = f"{self.codec['scheme']}http://{host}:{self.args.http_port}/ap2-{session.id}.{self.codec['ext']}"
        main, second = display(sp.title, sp.artist, sp.art, sp.source)
        session.icy, session.icy_serial = second, session.icy_serial + 1
        session.uri = stream
        art = self.art_url(host, sp.art) if sp.title else None
        # a group member would only follow its coordinator
        current = field(self.soap(sp.ip, AVT, "GetMediaInfo"), "CurrentURI")
        if current.startswith("x-rincon:"):
            self.leave_group(sp)
        log(f"{sp.room}: playing '{main}' / '{second}'" + (" with cover" if art else ""))
        if self.play(sp, stream, didl(stream, self.codec["protocol"], main, second, art)):
            session.delivered, session.shown = True, session.key_of_track()
            session.attempts, session.lost, session.stopped = 0, 0, 0
            session.watch_after = now + WATCH_GRACE
            return
        session.delivered = False
        session.attempts += 1
        session.retry_at = now + 2.0
        self.discover_now.set()
        if session.attempts >= DELIVER_RETRIES:
            log(f"{sp.room}: can't be reached, giving up")
            self.release(session, now, "unreachable")

    def watch(self, session, now):
        """Polls what the primary plays: Sonos doesn't tell us when another source takes over."""
        if now < session.poll_at or now < session.watch_after:
            return
        session.poll_at = now + 1.0
        sp = session.primary
        info = self.soap(sp.ip, AVT, "GetTransportInfo")
        media = self.soap(sp.ip, AVT, "GetMediaInfo")
        if info is None or media is None:
            session.unreachable += 1
            if session.unreachable == 3:
                self.discover_now.set()     # its address may have changed
            if session.unreachable >= UNREACHABLE_POLLS:
                self.release(session, now, "unreachable")
            return
        session.unreachable = 0
        uri = field(media, "CurrentURI")
        session.lost = 0 if f"/ap2-{session.id}." in uri else session.lost + 1
        if session.lost >= LOST_POLLS:
            self.release(session, now, f"moved to {uri[:50] or 'nothing'}")
            return
        state, status = field(info, "CurrentTransportState"), field(info, "CurrentTransportStatus")
        if status.startswith("ERROR"):
            session.errors += 1
            log(f"{sp.room}: reports {status} ({session.errors}/{ERROR_RETRIES})")
            if session.errors > ERROR_RETRIES:
                self.release(session, now, "keeps failing")
            else:
                session.delivered, session.retry_at = False, now + 2.0 * session.errors
            return
        # stopped or paused on the Sonos side: the sender can't know unless we disconnect it
        session.stopped = session.stopped + 1 if state in ("STOPPED", "PAUSED_PLAYBACK") else 0
        if state == "PLAYING":
            session.errors = 0
        if session.stopped >= LOST_POLLS:
            self.release(session, now, "stopped from the Sonos side")

    def watch_member(self, session, sp, now):
        media = self.soap(sp.ip, AVT, "GetMediaInfo")
        if media is None:
            return
        sp.lost = 0 if field(media, "CurrentURI") == f"x-rincon:{session.primary.rincon}" else sp.lost + 1
        if sp.lost >= LOST_POLLS:
            sp.joined = False
            self.restart(sp, now, "it left the group")

    def step(self, session, now):
        sp = session.primary
        if session.encoder and session.encoder.poll() is not None:
            session.encoder_restarts += 1
            log(f"{sp.room}: encoder died ({session.encoder_restarts})")
            if session.encoder_restarts > 5:
                self.release(session, now, "encoder keeps dying")
                return
            self.start_encoder(session)
            session.delivered = False
        if not session.delivered:
            if now >= session.retry_at:
                self.deliver(session, now)
            return
        if sp.playing and session.key_of_track() != session.shown and now - sp.info_at >= TRACK_SETTLE:
            self.deliver(session, now)
            return
        self.watch(session, now)
        if session.key not in self.sessions:
            return
        polling = session.poll_at == now + 1.0
        for other in self.speakers.values():
            if other is sp or (other.client or other.rincon) != session.key:
                continue
            if other.playing and not other.joined:
                log(f"{other.room}: joins {sp.room}")
                # uses play() and not a bare SetAVTransportURI: a Sonos that was stopped stays stopped
                other.joined = self.play(other, f"x-rincon:{sp.rincon}")
                other.lost = 0
            elif other.joined and not other.playing and now - other.changed_at >= STOP_GRACE:
                self.leave_group(other)
            elif other.joined and polling:
                self.watch_member(session, other, now)

    def reconcile(self, now):
        for session in list(self.sessions.values()):
            sp = session.primary
            if not sp.playing and now - sp.changed_at >= STOP_GRACE:
                self.close_session(session, stop=True, now=now)
        groups = collections.defaultdict(list)
        for sp in self.speakers.values():
            if sp.playing:
                groups[sp.client or sp.rincon].append(sp)
        for key, members in groups.items():
            if key not in self.sessions:
                primary = members[0]
                if (primary.title and primary.art) or now - primary.changed_at >= START_WAIT:
                    self.open_session(key, primary, now)
        for session in list(self.sessions.values()):
            self.step(session, now)

    def send_volumes(self):
        for sp in self.speakers.values():
            if sp.volume_due is not None:
                volume, sp.volume_due = sp.volume_due, None
                log(f"{sp.name}: volume {volume}")
                self.soap(sp.ip, RC, "SetVolume", f"<Channel>Master</Channel><DesiredVolume>{volume}</DesiredVolume>")

    def housekeeping(self, now):
        for sp in self.speakers.values():
            if sp.playing and now - max(sp.changed_at, sp.last_audio) > STALL_TIMEOUT:
                self.restart(sp, now, f"no audio for {STALL_TIMEOUT:.0f} s")
        # a clean start after a burst of activity, once nobody can notice it
        idle = self.args.idle_restart * 60
        if (idle and self.last_session_at is not None and now - self.last_session_at >= idle
                and not self.sessions and not any(s.playing for s in self.speakers.values())):
            self.last_session_at = None
            for sp in self.speakers.values():
                self.restart(sp, now, f"idle for {self.args.idle_restart} min")

    def tick(self, now):
        self.supervise(now)
        self.reconcile(now)
        self.send_volumes()
        self.housekeeping(now)

    # ------------------------------------------------------------ main loop
    def healthy(self):
        return all(t.is_alive() for t in (self.threads.get("http"), self.threads.get("discovery")) if t)

    def run(self):
        os.makedirs(self.args.runtime_dir, exist_ok=True)
        # a bridge killed outright leaves its children holding the ports
        for path in filter(None, (self.args.shairport, self.args.nqptp)):
            subprocess.run(["pkill", "-f", path], check=False)
        self.spawn_nqptp()
        time.sleep(1)
        self.threads["http"] = threading.Thread(target=self.serve, daemon=True)
        self.threads["http"].start()
        self.threads["discovery"] = threading.Thread(target=self.discovery_loop, daemon=True)
        self.threads["discovery"].start()
        sd_notify("READY=1")
        while self.running:
            try:
                self.tick(time.monotonic())
                self.tick_failures = 0
            except Exception as e:
                self.tick_failures += 1
                log(f"tick failed ({self.tick_failures}): {e!r}")
                if self.tick_failures >= 20:
                    log("too many failures, exiting so that the service manager restarts us")
                    self.shutdown(code=1)
            if self.healthy():
                sd_notify("WATCHDOG=1")
            time.sleep(0.25)

    def shutdown(self, *_, code=0):
        self.running = False
        now = time.monotonic()
        for session in list(self.sessions.values()):
            self.close_session(session, stop=True, now=now)
        for sp in self.speakers.values():
            self.terminate(sp, now)
        if self.nqptp:
            self.nqptp.terminate()
        os._exit(code)

    # ------------------------------------------------------------ HTTP
    def find_session(self, session_id):
        return next((s for s in self.sessions.values() if s.id == session_id), None)

    def serve(self):
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            timeout = 15    # a Sonos that stopped reading must not hold a thread

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/art/"):
                    art = bridge.arts.get(self.path[5:].split(".")[0])
                    if not art:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(art)))
                    self.end_headers()
                    self.wfile.write(art)
                    return
                m = re.match(r"/ap2-(\w+)\.\w+$", self.path)
                session = bridge.find_session(m.group(1)) if m else None
                if not session:
                    self.send_error(404)
                    return
                self.stream(session)

            def stream(self, session):
                icy = bridge.codec["icy"] and self.headers.get("Icy-MetaData") == "1"
                self.send_response(200)
                self.send_header("Content-Type", bridge.codec["type"])
                self.send_header("Connection", "close")
                if icy:
                    self.send_header("icy-metaint", str(ICY_INTERVAL))
                self.end_headers()
                q = queue.Queue(CLIENT_QUEUE)
                session.clients.append(q)
                log(f"stream: {self.client_address[0]} connected")
                flac = bridge.codec is CODECS["flac"]
                aligned = not flac
                remain, serial = ICY_INTERVAL, -1
                try:
                    while bridge.running and session.key in bridge.sessions:
                        try:
                            data = q.get(timeout=5)
                        except queue.Empty:
                            continue
                        if not aligned:
                            # a connection must start with the header, then on a frame boundary
                            if not session.header_done:
                                continue
                            start = flac_frame_start(data)
                            if start < 0:
                                continue
                            self.wfile.write(session.header)
                            data, aligned = data[start:], True
                        while icy and len(data) >= remain:
                            self.wfile.write(data[:remain])
                            data = data[remain:]
                            block = b"\0"
                            if serial != session.icy_serial:
                                serial = session.icy_serial
                                block = icy_block(session.icy)
                            self.wfile.write(block)
                            remain = ICY_INTERVAL
                        self.wfile.write(data)
                        if icy:
                            remain -= len(data)
                except OSError:
                    pass
                finally:
                    session.clients.remove(q)
                    log(f"stream: {self.client_address[0]} gone")

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        Server(("", self.args.http_port), Handler).serve_forever()


def parse_args(argv=None):
    deps = os.path.join(HERE, ".deps", "bin")
    ap = argparse.ArgumentParser(description="AirPlay 2 to Sonos bridge, built on shairport-sync.")
    ap.add_argument("sonos", nargs="*", help="Sonos IPs, for networks where discovery is blocked")
    ap.add_argument("--codec", choices=CODECS, default="mp3", help="format sent to Sonos (default: %(default)s)")
    ap.add_argument("--name", default="{room} AP2", help="speaker name in the AirPlay menu (default: %(default)s)")
    ap.add_argument("--max-volume", type=int, default=100, help="Sonos volume with the slider at full (default: %(default)s)")
    ap.add_argument("--start-volume", type=int, default=50, help="highest volume applied when a session starts (default: %(default)s)")
    ap.add_argument("--idle-restart", type=int, default=30, metavar="MIN", help="restart idle instances this long after the last session, 0 to disable (default: %(default)s)")
    ap.add_argument("--http-port", type=int, default=7099)
    ap.add_argument("--first-port", type=int, default=7021, help="AirPlay port of the first speaker (default: %(default)s)")
    ap.add_argument("--shairport", default=os.path.join(deps, "shairport-sync"))
    ap.add_argument("--nqptp", default=os.path.join(deps, "nqptp"), help="empty if a system service runs it")
    ap.add_argument("--config", default=os.path.join(HERE, "shairport-sync.conf"))
    ap.add_argument("--runtime-dir", default=os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "airplay2-sonos-bridge"))
    return ap.parse_args(argv)


def main():
    bridge = Bridge(parse_args())
    signal.signal(signal.SIGTERM, bridge.shutdown)
    signal.signal(signal.SIGINT, bridge.shutdown)
    bridge.run()


if __name__ == "__main__":
    main()
