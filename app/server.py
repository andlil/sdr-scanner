"""Web server: serves the UI and does WebRTC signalling.

Everything after the initial offer/answer travels over WebRTC: audio on an
Opus track, status and commands as JSON on a data channel named "control".
"""

import asyncio
import json
import logging
import os
from pathlib import Path

# The crc32c module (used by aiortc's SCTP data channel) picks a hardware path
# that executes instructions the Pi 3's Cortex-A53 lacks: SIGILL on any buffer
# over 1 KiB. Must be set before aiortc is imported.
os.environ.setdefault("CRC32C_SW_MODE", "force")

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from .channels import ChannelError, ChannelStore
from .rtc import AudioBroadcaster, ScannerAudioTrack
from .scanner import Scanner
from .sources import make_source

log = logging.getLogger("sdr-scanner")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def env(name, default):
    return os.environ.get(name, default)


class App:
    def __init__(self):
        self.store = ChannelStore(env("DATA_DIR", "/data"))
        self.audio = AudioBroadcaster()
        self.peers = set()
        self.channels = set()       # open data channels
        self.loop = None
        self.status = {"type": "status", "state": "stopped"}
        stun = env("STUN_SERVER", "")
        self.rtc_config = RTCConfiguration(iceServers=[RTCIceServer(stun)] if stun else [])

        def source():
            return make_source(env("SDR_DEVICE", ""), env("SDR_GAIN", "auto"),
                               float(env("SDR_PPM", "0")),
                               lambda: [c["freq"] for c in self.store.all() if c["enabled"]])

        self.scanner = Scanner(
            self.store, source,
            on_audio=lambda a: self.loop.call_soon_threadsafe(self.audio.push, a),
            on_status=lambda s: self.loop.call_soon_threadsafe(self._on_status, s),
            hang_s=float(env("HANG_TIME", "2.0")),
        )

    # ---- lifecycle -------------------------------------------------------------

    async def startup(self, _app):
        self.loop = asyncio.get_running_loop()
        self.scanner.begin()
        if env("AUTOSTART", "1") == "1":
            self.scanner.command("start")

    async def shutdown(self, _app):
        await asyncio.gather(*(pc.close() for pc in list(self.peers)), return_exceptions=True)
        await asyncio.to_thread(self.scanner.shutdown)

    # ---- outgoing --------------------------------------------------------------

    def _on_status(self, status):
        self.status = status
        self._broadcast(status)

    def _broadcast(self, msg):
        data = json.dumps(msg)
        for ch in list(self.channels):
            if ch.readyState == "open":
                ch.send(data)

    def _channels_msg(self):
        return {"type": "channels", "channels": self.store.all()}

    # ---- incoming --------------------------------------------------------------

    def _on_message(self, ch, raw):
        try:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "command" and msg.get("action") in ("start", "pause", "stop"):
                self.scanner.command(msg["action"])
            elif kind == "hold":
                self.scanner.command("hold", id=msg.get("id"))
            elif kind in ("channel.add", "channel.update", "channel.delete"):
                if kind == "channel.add":
                    self.store.add(msg.get("channel") or {})
                elif kind == "channel.update":
                    self.store.update(msg.get("id"), msg.get("changes") or {})
                else:
                    self.store.delete(msg.get("id"))
                self.scanner.command("channels_changed")
                self._broadcast(self._channels_msg())
            else:
                raise ChannelError(f"unknown message type {kind!r}")
        except (ChannelError, ValueError, TypeError) as e:
            ch.send(json.dumps({"type": "error", "message": str(e)}))

    async def offer(self, request):
        params = await request.json()
        pc = RTCPeerConnection(self.rtc_config)
        self.peers.add(pc)
        track = ScannerAudioTrack()
        self.audio.tracks.add(track)

        @pc.on("datachannel")
        def on_datachannel(ch):
            self.channels.add(ch)

            @ch.on("open")
            def _open():
                ch.send(json.dumps(self._channels_msg()))
                ch.send(json.dumps(self.status))

            @ch.on("message")
            def _message(raw):
                self._on_message(ch, raw)

            @ch.on("close")
            def _close():
                self.channels.discard(ch)

            if ch.readyState == "open":
                _open()

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("peer %s: %s", request.remote, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                self.audio.tracks.discard(track)
                self.peers.discard(pc)
                await pc.close()

        await pc.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type=params["type"]))
        pc.addTrack(track)
        await pc.setLocalDescription(await pc.createAnswer())
        return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def index(_request):
    return web.FileResponse(WEB_DIR / "index.html")


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aioice").setLevel(logging.WARNING)
    app = App()
    web_app = web.Application()
    web_app.on_startup.append(app.startup)
    web_app.on_shutdown.append(app.shutdown)
    web_app.router.add_get("/", index)
    web_app.router.add_post("/api/offer", app.offer)
    web_app.router.add_static("/static/", WEB_DIR)
    web.run_app(web_app, host=env("HOST", "0.0.0.0"), port=int(env("PORT", "8080")),
                access_log=None)


if __name__ == "__main__":
    main()
