"""Web server: the UI, a WebSocket for control and status, and audio.

The browser keeps one WebSocket open (/api/ws, JSON text messages) for
channel lists, status and commands; it works through any reverse proxy.
Audio is sent by WebRTC (Opus) when the browser can reach this machine
directly, and otherwise over the same WebSocket as IMA ADPCM chunks
(binary messages), so listening works without opening any ports.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

# The crc32c module (used by aiortc's SCTP transport) picks a hardware path
# that executes instructions the Pi 3's Cortex-A53 lacks: SIGILL on any buffer
# over 1 KiB. Must be set before aiortc is imported.
os.environ.setdefault("CRC32C_SW_MODE", "force")

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from .adpcm import AdpcmEncoder
from .channels import ChannelError, ChannelStore
from .rtc import AudioBroadcaster, ScannerAudioTrack, limit_ice_interfaces
from .scanner import Scanner
from .sources import make_source

log = logging.getLogger("sdr-scanner")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


CONNECT_TIMEOUT_S = 20


def _candidates(sdp):
    """'udp 192.168.1.5:50000 host, ...' from the a=candidate lines of an SDP."""
    out = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            f = line.split()
            if len(f) >= 8:
                out.append(f"{f[2].lower()} {f[4]}:{f[5]} {f[7]}")
    return ", ".join(out) or "none"


def env(name, default):
    return os.environ.get(name, default)


def client_address(request):
    # Behind a reverse proxy request.remote is the proxy; the browser's own
    # address is in X-Forwarded-For.
    return request.headers.get("X-Forwarded-For", request.remote).split(",")[0].strip()


class WsClient:
    """One browser's WebSocket, with a send queue so a slow link can't stall us."""

    MAX_QUEUED_AUDIO = 25           # ~0.6 s; beyond that drop audio, never JSON

    def __init__(self, ws, address):
        self.ws = ws
        self.address = address
        self.wants_audio = False
        self._queue = asyncio.Queue()
        self._audio_queued = 0
        self._task = asyncio.ensure_future(self._sender())

    def send_text(self, text):
        self._queue.put_nowait((False, text))

    def send_audio(self, chunk):
        if self._audio_queued < self.MAX_QUEUED_AUDIO:
            self._audio_queued += 1
            self._queue.put_nowait((True, chunk))

    async def _sender(self):
        while True:
            is_audio, data = await self._queue.get()
            try:
                if is_audio:
                    self._audio_queued -= 1
                    await self.ws.send_bytes(data)
                else:
                    await self.ws.send_str(data)
            except (ConnectionError, RuntimeError):
                return                  # closed; the handler cleans up

    def close(self):
        self._task.cancel()


class App:
    def __init__(self):
        self.store = ChannelStore(env("DATA_DIR", "/data"))
        self.audio = AudioBroadcaster()
        self.peers = set()
        self.clients = set()        # open WebSockets (WsClient)
        self.encoder = AdpcmEncoder()
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
            on_audio=lambda a: self.loop.call_soon_threadsafe(self._on_audio, a),
            on_status=lambda s: self.loop.call_soon_threadsafe(self._on_status, s),
            hang_s=float(env("HANG_TIME", "2.0")),
        )

    # ---- lifecycle -------------------------------------------------------------

    async def startup(self, _app):
        self.loop = asyncio.get_running_loop()
        limit_ice_interfaces()
        self.scanner.begin()
        if env("AUTOSTART", "1") == "1":
            self.scanner.command("start")

    async def shutdown(self, _app):
        await asyncio.gather(*(pc.close() for pc in list(self.peers)), return_exceptions=True)
        await asyncio.gather(*(c.ws.close() for c in list(self.clients)), return_exceptions=True)
        await asyncio.to_thread(self.scanner.shutdown)

    # ---- outgoing --------------------------------------------------------------

    def _on_status(self, status):
        self.status = status
        self._broadcast(status)

    def _on_audio(self, audio):
        self.audio.push(audio)                      # WebRTC listeners
        listeners = [c for c in self.clients if c.wants_audio]
        if listeners:                               # WebSocket listeners
            chunk = self.encoder.encode(audio)
            for c in listeners:
                c.send_audio(chunk)

    def _broadcast(self, msg):
        data = json.dumps(msg)
        for c in list(self.clients):
            c.send_text(data)

    def _channels_msg(self):
        return {"type": "channels", "channels": self.store.all()}

    # ---- incoming --------------------------------------------------------------

    def _on_message(self, client, raw):
        try:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "command" and msg.get("action") in ("start", "pause", "stop"):
                self.scanner.command(msg["action"])
            elif kind == "audio":
                # "ws": send audio over this WebSocket (no WebRTC path)
                client.wants_audio = msg.get("transport") == "ws"
                log.info("%s: audio over %s", client.address,
                         "WebSocket" if client.wants_audio else "WebRTC")
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
            client.send_text(json.dumps({"type": "error", "message": str(e)}))

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=20)    # keeps proxies from timing out
        await ws.prepare(request)
        client = WsClient(ws, client_address(request))
        self.clients.add(client)
        log.info("%s: connected", client.address)
        client.send_text(json.dumps(self._channels_msg()))
        client.send_text(json.dumps(self.status))
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    self._on_message(client, msg.data)
        finally:
            self.clients.discard(client)
            client.close()
            log.info("%s: disconnected", client.address)
        return ws

    async def offer(self, request):
        params = await request.json()
        client = client_address(request)
        log.info("offer from %s, browser candidates: %s", client, _candidates(params.get("sdp", "")))
        pc = RTCPeerConnection(self.rtc_config)
        self.peers.add(pc)
        track = ScannerAudioTrack()
        self.audio.tracks.add(track)

        async def drop():
            self.audio.tracks.discard(track)
            self.peers.discard(pc)
            await pc.close()

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("peer %s: %s", client, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await drop()

        async def give_up_if_not_connected(answer):
            # A peer whose ICE never succeeds can sit in "connecting" forever;
            # the browser retries with a new connection, so drop this one.
            await asyncio.sleep(CONNECT_TIMEOUT_S)
            if pc.connectionState not in ("connected", "closed"):
                log.info("peer %s: no WebRTC path after %d s (browser not on the LAN?), "
                         "dropping it; audio goes over the WebSocket. Offered: %s",
                            client, CONNECT_TIMEOUT_S, _candidates(answer))
                await drop()

        await pc.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type=params["type"]))
        pc.addTrack(track)
        await pc.setLocalDescription(await pc.createAnswer())
        answer = pc.localDescription.sdp
        asyncio.ensure_future(give_up_if_not_connected(answer))
        return web.json_response({"sdp": answer, "type": pc.localDescription.type})


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
    web_app.router.add_get("/api/ws", app.websocket)
    web_app.router.add_static("/static/", WEB_DIR)
    web.run_app(web_app, host=env("HOST", "0.0.0.0"), port=int(env("PORT", "8080")),
                access_log=None)


if __name__ == "__main__":
    main()
