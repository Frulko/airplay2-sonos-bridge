"""Run with: python3 -m unittest discover tests"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bridge as b


class FakeProc:
    def __init__(self):
        self.code = None
        self.stdin = self
        self.stdout = self

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    kill = terminate

    def close(self):
        pass


class FakeBridge(b.Bridge):
    """The real logic, without processes or a network: Sonos is a dict per IP."""

    def __init__(self, **options):
        super().__init__(b.parse_args(["--nqptp", ""]))
        self.calls = []
        self.spawned = []
        self.sonos = {}
        self.reachable = True
        for key, value in options.items():
            setattr(self.args, key, value)

    def add(self, room, ip, current=""):
        rincon = f"RINCON_{room}"
        self.speakers[rincon] = b.Speaker(rincon, ip, room, f"{room} AP2", 7000 + len(self.speakers), "/nonexistent")
        self.sonos[ip] = {"uri": current, "state": "STOPPED", "status": "OK"}
        return self.speakers[rincon]

    def spawn(self, sp):
        sp.proc = FakeProc()
        self.spawned.append(sp.room)

    def start_encoder(self, session):
        session.encoder = FakeProc()

    def soap(self, ip, service, action, args=""):
        if not self.reachable:
            return None
        self.calls.append((ip, action))
        sonos = self.sonos[ip]
        if action == "SetAVTransportURI":
            sonos["uri"] = b.html.unescape(b.field(args, "CurrentURI"))
            sonos["meta"] = b.html.unescape(b.field(args, "CurrentURIMetaData"))
        elif action == "Play":
            sonos["state"] = "PLAYING"
        elif action == "Stop":
            sonos["state"] = "STOPPED"
        elif action == "BecomeCoordinatorOfStandaloneGroup":
            sonos["uri"] = ""
        elif action == "SetVolume":
            sonos["volume"] = int(b.field(args, "DesiredVolume"))
        elif action == "GetMediaInfo":
            return f"<CurrentURI>{b.escape(sonos['uri'])}</CurrentURI>"
        elif action == "GetTransportInfo":
            return (f"<CurrentTransportState>{sonos['state']}</CurrentTransportState>"
                    f"<CurrentTransportStatus>{sonos['status']}</CurrentTransportStatus>")
        return "<ok/>"

    def actions(self, ip):
        return [a for i, a in self.calls if i == ip and not a.startswith("Get")]

    def start_playing(self, sp, now, client="phone", title="Tape", artist="Parcels", art=b"jpg"):
        self.on_item(sp, "clip", client.encode(), now)
        self.on_item(sp, "pbeg", b"", now)
        if title:
            self.on_item(sp, "minm", title.encode(), now)
            self.on_item(sp, "asar", artist.encode(), now)
        if art:
            self.on_item(sp, "PICT", art, now)
        sp.proc = sp.proc or FakeProc()

    def run_for(self, start, seconds, step=1.0):
        now = start
        while now < start + seconds:
            for sp in self.speakers.values():
                if sp.playing:
                    sp.last_audio = now
            self.tick(now)
            now += step
        return now


class Pure(unittest.TestCase):
    def test_volume_uses_the_whole_slider(self):
        self.assertEqual(b.sonos_volume(-144, 60), 0)
        self.assertEqual(b.sonos_volume(0, 60), 60)
        self.assertEqual(b.sonos_volume(-15, 60), 30)

    def test_partial_metadata_items_wait_for_the_rest(self):
        item = (b'<item><type>636f7265</type><code>6d696e6d</code><length>4</length>\n'
                b'<data encoding="base64">\nVGFwZQ==</data></item>')
        self.assertEqual(b.parse_items(item + b"<item><ty"), ([("minm", b"Tape")], b"<item><ty"))

    def test_display(self):
        self.assertEqual(b.display("Tape", "Parcels", b"x", "iPhone"), ("Tape", "Parcels"))
        self.assertEqual(b.display("Tape", "Parcels", None, "iPhone"), ("AirPlay - iPhone", "Parcels - Tape"))
        self.assertEqual(b.display(None, None, None, None), ("AirPlay", b.NO_INFO))

    def test_didl_escapes_what_the_sender_gives(self):
        meta = b.didl("http://x/a", "p", "R&B <1>", "", None)
        self.assertIn("R&amp;B &lt;1&gt;", meta)
        self.assertNotIn("albumArtURI", meta)

    def test_icy_block(self):
        self.assertEqual(b.icy_block(""), b"\0")
        self.assertEqual(len(b.icy_block("abc")), 33)
        self.assertIn("’".encode(), b.icy_block("l'homme"))

    def test_flac_header_end(self):
        block = bytes([0x80, 0, 0, 2]) + b"xx"
        self.assertEqual(b.flac_header_end(b"fLaC" + block + b"frames"), 10)
        self.assertIsNone(b.flac_header_end(b"fLaC" + block[:-1]))
        self.assertIsNone(b.flac_header_end(b"fL"))

    def test_flac_frame_start_ignores_a_sync_inside_audio(self):
        header = bytes([0xff, 0xf8, 0xc9, 0x18, 0x00])
        frame = header + bytes([b.flac_crc8(header)]) + b"\0" * 16
        self.assertEqual(b.flac_frame_start(b"\xff\xf8\xc9\x18\x00\x00garbage" + frame), 13)
        self.assertEqual(b.flac_frame_start(b"no frame here" + b"\0" * 16), -1)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.a = self.bridge.add("A", "1.1.1.1")
        self.c = self.bridge.add("C", "1.1.1.3")
        self.ip_patch = b.local_ip
        b.local_ip = lambda peer: "9.9.9.9"
        self.addCleanup(lambda: setattr(b, "local_ip", self.ip_patch))

    def test_start_waits_for_track_info_then_shows_it_once(self):
        br = self.bridge
        br.start_playing(self.a, 100.0, title=None, art=None)
        br.tick(100.5)
        self.assertEqual(br.actions("1.1.1.1"), [])
        br.on_item(self.a, "minm", b"Tape", 100.8)
        br.on_item(self.a, "asar", b"Parcels", 100.8)
        br.on_item(self.a, "PICT", b"jpg", 100.9)
        br.run_for(101.0, 10)
        self.assertEqual(br.actions("1.1.1.1").count("SetAVTransportURI"), 1)
        self.assertIn("<dc:title>Tape</dc:title>", br.sonos["1.1.1.1"]["meta"])
        self.assertIn("albumArtURI", br.sonos["1.1.1.1"]["meta"])

    def test_no_info_shows_the_source_after_the_wait(self):
        self.a.source = "iPhone"
        self.bridge.start_playing(self.a, 100.0, title=None, art=None)
        self.bridge.run_for(100.0, 4)
        self.assertIn("AirPlay - iPhone", self.bridge.sonos["1.1.1.1"]["meta"])

    def test_track_change_restarts_once_after_settling(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 3)
        br.calls.clear()
        for i in range(3):      # zapping
            br.on_item(self.a, "minm", f"Song {i}".encode(), now)
            now = br.run_for(now, 1)
        br.run_for(now, 5)
        self.assertEqual(br.actions("1.1.1.1").count("SetAVTransportURI"), 1)
        self.assertIn("Song 2", br.sonos["1.1.1.1"]["meta"])

    def test_takes_over_from_another_source(self):
        br = self.bridge
        br.sonos["1.1.1.1"].update(uri="x-sonos-vli:spotify", state="PLAYING")
        br.start_playing(self.a, 100.0)
        br.run_for(100.0, 2)
        self.assertIn("/ap2-", br.sonos["1.1.1.1"]["uri"])

    def test_takes_over_a_speaker_in_someone_elses_group(self):
        br = self.bridge
        br.sonos["1.1.1.1"].update(uri="x-rincon:RINCON_OTHER")
        br.start_playing(self.a, 100.0)
        br.run_for(100.0, 2)
        actions = br.actions("1.1.1.1")
        self.assertLess(actions.index("BecomeCoordinatorOfStandaloneGroup"), actions.index("SetAVTransportURI"))

    def test_other_source_disconnects_the_sender_and_leaves_sonos_alone(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 15)
        br.sonos["1.1.1.1"]["uri"] = "x-sonos-vli:spotify"
        br.calls.clear()
        proc = self.a.proc
        br.run_for(now, 8)
        self.assertEqual(br.sessions, {})
        self.assertEqual(proc.code, 0)
        self.assertNotIn("Stop", br.actions("1.1.1.1"))
        self.assertEqual(br.sonos["1.1.1.1"]["uri"], "x-sonos-vli:spotify")
        self.assertIn("A", br.spawned)

    def test_a_short_glitch_is_not_another_source(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 15)
        uri = br.sonos["1.1.1.1"]["uri"]
        br.sonos["1.1.1.1"]["uri"] = ""
        now = br.run_for(now, 3)
        br.sonos["1.1.1.1"]["uri"] = uri
        br.run_for(now, 10)
        self.assertEqual(len(br.sessions), 1)

    def test_stop_from_the_sonos_app_disconnects_the_sender(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 15)
        br.sonos["1.1.1.1"]["state"] = "STOPPED"
        br.run_for(now, 8)
        self.assertEqual(br.sessions, {})

    def test_sonos_error_reloads_then_gives_up(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 15)
        br.calls.clear()
        br.sonos["1.1.1.1"]["status"] = "ERROR_LOST_CONNECTION"
        br.run_for(now, 120)
        self.assertGreaterEqual(br.actions("1.1.1.1").count("SetAVTransportURI"), 2)
        self.assertEqual(br.sessions, {})

    def test_pause_on_the_sender_stops_then_resume_restarts(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 5)
        br.on_item(self.a, "paus", b"", now)
        now = br.run_for(now, 5)
        self.assertEqual(br.sonos["1.1.1.1"]["state"], "STOPPED")
        self.assertEqual(br.sessions, {})
        br.on_item(self.a, "prsm", b"", now)
        br.run_for(now, 1)
        self.assertEqual(br.sonos["1.1.1.1"]["state"], "PLAYING")

    def test_track_change_gap_keeps_the_session(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 5)
        session = next(iter(br.sessions.values()))
        br.on_item(self.a, "pend", b"", now)
        now = br.run_for(now, 1)
        br.on_item(self.a, "prsm", b"", now)
        br.run_for(now, 3)
        self.assertIs(next(iter(br.sessions.values())), session)

    def test_second_speaker_joins_and_leaves(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 3)
        br.start_playing(self.c, now)
        now = br.run_for(now, 3)
        self.assertEqual(br.sonos["1.1.1.3"]["uri"], "x-rincon:RINCON_A")
        self.assertEqual(len(br.sessions), 1)
        br.on_item(self.c, "pend", b"", now)
        br.run_for(now, 5)
        self.assertIn("BecomeCoordinatorOfStandaloneGroup", br.actions("1.1.1.3"))
        self.assertEqual(len(br.sessions), 1)

    def test_member_taken_elsewhere_is_disconnected_alone(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        br.start_playing(self.c, 100.0)
        now = br.run_for(100.0, 15)
        primary_proc, member_proc = self.a.proc, self.c.proc
        br.sonos["1.1.1.3"]["uri"] = "x-sonos-vli:spotify"
        br.run_for(now, 8)
        self.assertEqual(member_proc.code, 0)
        self.assertIsNone(primary_proc.code)
        self.assertEqual(len(br.sessions), 1)

    def test_primary_unticked_moves_the_stream(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        br.start_playing(self.c, 100.0)
        now = br.run_for(100.0, 5)
        br.on_item(self.a, "pend", b"", now)
        br.run_for(now, 6)
        self.assertIs(next(iter(br.sessions.values())).primary, self.c)
        self.assertIn("/ap2-", br.sonos["1.1.1.3"]["uri"])

    def test_two_senders_get_two_sessions(self):
        br = self.bridge
        br.start_playing(self.a, 100.0, client="phone")
        br.start_playing(self.c, 100.0, client="laptop")
        br.run_for(100.0, 3)
        self.assertEqual(len(br.sessions), 2)
        self.assertIn("/ap2-", br.sonos["1.1.1.1"]["uri"])
        self.assertIn("/ap2-", br.sonos["1.1.1.3"]["uri"])
        self.assertNotEqual(br.sonos["1.1.1.1"]["uri"], br.sonos["1.1.1.3"]["uri"])

    def test_sonos_unreachable_retries_then_releases(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 15)
        br.reachable = False
        now = br.run_for(now, 5)
        self.assertEqual(len(br.sessions), 1)
        self.assertTrue(br.discover_now.is_set())
        br.run_for(now, 15)
        self.assertEqual(br.sessions, {})

    def test_a_start_that_fails_is_retried(self):
        br = self.bridge
        br.reachable = False
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 6)
        br.reachable = True
        br.run_for(now, 3)
        self.assertEqual(br.sonos["1.1.1.1"]["state"], "PLAYING")


class Resilience(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.a = self.bridge.add("A", "1.1.1.1")
        self.ip_patch = b.local_ip
        b.local_ip = lambda peer: "9.9.9.9"
        self.addCleanup(lambda: setattr(b, "local_ip", self.ip_patch))

    def test_crash_loop_backs_off(self):
        br = self.bridge
        now = 1000.0
        times = []
        for _ in range(240):
            br.supervise(now)
            if br.spawned:
                times.append(now)
                br.spawned.clear()
                self.a.proc.code = 1    # dies at once
            now += 0.25
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreater(gaps[-1], gaps[0])

    def test_crash_during_playback_stops_sonos_and_comes_back(self):
        br = self.bridge
        br.supervise(0.0)
        br.spawned.clear()
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 5)
        self.a.proc.code = -11
        br.run_for(now, 10)
        self.assertEqual(br.sessions, {})
        self.assertEqual(br.sonos["1.1.1.1"]["state"], "STOPPED")
        self.assertEqual(br.spawned, ["A"])

    def test_stuck_instance_is_restarted(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 5)
        proc = self.a.proc
        self.a.last_audio = now
        for t in range(int(b.STALL_TIMEOUT) + 5):
            br.tick(now + t)        # no audio any more
        self.assertEqual(proc.code, 0)
        self.assertIn("A", br.spawned)

    def test_encoder_crash_reloads_the_stream(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        now = br.run_for(100.0, 3)
        session = next(iter(br.sessions.values()))
        br.calls.clear()
        session.encoder.code = 1
        br.run_for(now, 3)
        self.assertEqual(br.actions("1.1.1.1").count("SetAVTransportURI"), 1)
        self.assertIsNone(session.encoder.code)

    def test_idle_restart_happens_once_after_a_session(self):
        br = self.bridge
        br.args.idle_restart = 1
        br.tick(0.0)
        br.spawned.clear()
        br.run_for(2.0, 120)
        self.assertEqual(br.spawned, [], "nothing to clean without a session")
        br.start_playing(self.a, 200.0)
        now = br.run_for(200.0, 3)
        br.on_item(self.a, "pend", b"", now)
        now = br.run_for(now, 5)
        br.spawned.clear()
        br.run_for(now, 200)
        self.assertEqual(br.spawned, ["A"])

    def test_volume_keeps_the_last_of_a_burst_and_limits_the_start(self):
        br = self.bridge
        br.start_playing(self.a, 100.0)
        br.on_item(self.a, "pvol", b"0.00,0,0,0", 100.0)
        br.send_volumes()
        self.assertEqual(br.sonos["1.1.1.1"]["volume"], 50)
        br.calls.clear()
        for db in (b"-3.00,0,0,0", b"-2.00,0,0,0", b"-1.00,0,0,0"):
            br.on_item(self.a, "pvol", db, 101.0)
        br.send_volumes()
        self.assertEqual(br.actions("1.1.1.1"), ["SetVolume"])
        self.assertEqual(br.sonos["1.1.1.1"]["volume"], 97)

    def test_address_change_is_followed(self):
        br = self.bridge
        br.add_speaker("1.1.1.9", "RINCON_A", "A")
        self.assertEqual(self.a.ip, "1.1.1.9")
        self.assertEqual(len(br.speakers), 1)

    def test_stereo_pair_is_offered_once(self):
        br = self.bridge
        br.add_speaker("1.1.1.9", "RINCON_A2", "A")
        self.assertEqual(len(br.speakers), 1)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RealStream(unittest.TestCase):
    """The real encoder and HTTP server, fed with noise."""

    def stream(self, codec):
        self.port = 17099 if codec == "mp3" else 17098
        args = b.parse_args(["--codec", codec, "--nqptp", "", "--http-port", str(self.port)])
        br = b.Bridge(args)
        br.running = True
        sp = b.Speaker("R", "127.0.0.1", "R", "R", 1, "/nonexistent")
        session = b.Session("k", sp)
        br.sessions["k"] = session
        br.start_encoder(session)
        session.icy, session.icy_serial = "Parcels", 1
        return br, session

    def feed(self, session, seconds=3):
        noise = os.urandom(48000 * 8)
        for _ in range(seconds * 10):
            session.encoder.stdin.write(noise[:38400])
            time.sleep(0.02)

    def get(self, br, session, ext, headers, feeder, size=40000):
        import threading
        threading.Thread(target=br.serve, daemon=True).start()
        time.sleep(0.3)
        threading.Thread(target=feeder, daemon=True).start()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/ap2-{session.id}.{ext}", headers=headers)
        with urllib.request.urlopen(request, timeout=10) as r:
            return r.headers, r.read(size)

    def test_mp3_with_icy(self):
        br, session = self.stream("mp3")
        self.addCleanup(br.stop_encoder, session)
        headers, data = self.get(br, session, "mp3", {"Icy-MetaData": "1"}, lambda: self.feed(session))
        self.assertEqual(headers["icy-metaint"], str(b.ICY_INTERVAL))
        self.assertEqual(data[b.ICY_INTERVAL:b.ICY_INTERVAL + 1 + 32], b.icy_block("Parcels"))

    def test_flac_decodes_when_joining_mid_stream(self):
        br, session = self.stream("flac")
        self.addCleanup(br.stop_encoder, session)
        # let the stream run first, as when Sonos reconnects on a track change
        self.feed(session, 1)
        # the read cuts the last frame, so the decoder below stops well before it
        headers, data = self.get(br, session, "flac", {}, lambda: self.feed(session), size=400000)
        self.assertEqual(headers["Content-Type"], "audio/flac")
        self.assertTrue(data.startswith(b"fLaC"))
        with tempfile.NamedTemporaryFile(suffix=".flac") as f:
            f.write(data)
            f.flush()
            out = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", f.name, "-t", "0.3", "-f", "null", "-"],
                                 capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
