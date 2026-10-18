# SDR Scanner

A radio scanner you listen to in the browser. An RTL-SDR or Airspy HF+ on a
Raspberry Pi steps through your list of narrowband FM channels, stops on
any channel with traffic, and streams the audio to the browser over WebRTC.

- Name your channels and see which one is playing.
- Automatic squelch per channel: no level to set.
- Scan, pause (hold) and stop from the browser; volume per listener.
- Audio (Opus) and channel information both travel over one WebRTC connection.

## Run it

Requirements: Docker with the compose plugin, a 64-bit OS (arm64), and the
SDR plugged in.

Use the pre-built image. GitHub Actions builds it on every push
(see `.github/workflows/docker.yml`):

```sh
docker compose pull
docker compose up -d
```

To update later, run the same two commands again.

Alternatively, build the image on the Pi itself:

```sh
docker compose up -d --build
```

This takes a while on a Pi 3, because the Airspy HF+ driver is compiled
from source. If the Pi runs out of memory, build stage by stage:
`docker build --target airspyhf .`, then `docker build --target pydeps .`,
then `docker compose up -d --build`.

Open `http://<pi-address>:8080` in a browser.

Published image tags at `ghcr.io/andlil/sdr-scanner`: `latest` (main
branch), `<branch-name>`, `sha-<commit>`, and `<version>` for `v*` git tags.

Channels are saved in `./data/channels.json`. On first start the list holds
marine VHF channels 9, 11, 16, 71 and 73 and the eight PMR446 channels.

### Host preparation (once per Pi)

Stop the Linux TV-tuner driver from claiming the RTL-SDR:

```sh
printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' \
  | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
```

Then unplug and replug the dongle, or reboot.

Use a proper power supply (5.1 V, 2.5 A for a Pi 3). An RTL-SDR draws around
300 mA, and an undersized supply causes USB dropouts.

## Settings

Set these under `environment:` in `docker-compose.yml`:

| Variable      | Default | Meaning |
|---------------|---------|---------|
| `SDR_DEVICE`  | `""`    | SoapySDR device string. Empty means the first SDR found. Examples: `driver=rtlsdr`, `driver=airspyhf`, `driver=rtlsdr,serial=00000001`, `driver=sim` (simulator, no hardware). |
| `SDR_GAIN`    | `auto`  | `auto` for the receiver's AGC, or a fixed gain in dB such as `35`. |
| `SDR_PPM`     | `0`     | Frequency correction for the RTL-SDR, in ppm. |
| `HANG_TIME`   | `2.0`   | Seconds the scanner stays on a channel after the signal ends, so replies are heard. |
| `AUTOSTART`   | `1`     | Start scanning when the container starts. |
| `PORT`        | `8080`  | HTTP port. |
| `STUN_SERVER` | unset   | e.g. `stun:stun.l.google.com:19302`. Only needed when listening from outside the LAN. |

### Choosing a receiver

With more than one receiver connected, set `SDR_DEVICE` to choose one by
serial number. To list the connected receivers and the exact value to use:

```sh
docker compose stop          # free the receivers that the scanner holds
docker compose run --rm scanner python3 -m app.devices
```

```
Airspy HF+   AirSpy HF+ [dd52978026ba4686]
    SDR_DEVICE: "driver=airspyhf,serial=dd52978026ba4686"
```

If no connected receiver has that serial, the scanner refuses to start and
shows the receivers it found. It never falls back to another receiver.

RTL-SDR dongles often leave the factory with the same serial (`00000001`).
To give each one its own serial, plug in one dongle at a time and run:

```sh
docker compose run --rm scanner rtl_eeprom -s 00000002
```

Then unplug and replug the dongle.

Receiver coverage:

| Receiver   | Range                             | Sample rate used |
|------------|-----------------------------------|------------------|
| RTL-SDR    | about 24–1766 MHz                 | 1.024 MS/s       |
| Airspy HF+ | 9 kHz–31 MHz and 60–260 MHz       | 768 kS/s         |

Channels outside the receiver's range are marked in the UI and skipped.

## How it works

```
SDR ──SoapySDR──▶ scanner thread ──audio──▶ WebRTC Opus track ──▶ browser
                    │      ▲
                    │      └── commands ◀── data channel "control" ◀── browser
                    └── status ──────────▶ data channel "control" ──▶ browser
```

- **Tuning windows.** Channels close enough together to fit in one SDR
  bandwidth (about 0.8 × sample rate) share a tuning window, so they are
  monitored at the same time without retuning. The window centre is placed
  so that no channel lands on the receiver's DC spike.
- **Channelizer.** Every block of samples is run through one FFT. The same
  spectrum gives the power of every channel in the window, and the
  overlap-save filtered and decimated baseband of the channel being heard.
- **Auto squelch.** A channel's SNR is its power compared with the median
  of the surrounding 200 kHz, so it adapts at once to gain and noise
  changes. Each channel also learns its idle baseline: the lowest SNR seen
  in the last 5 minutes. A busy channel still drops back to pure noise
  between transmissions, so frequent weak signals cannot push its threshold
  up. A spur or interferer that never goes away does raise it, so it doesn't
  hold the scanner. The squelch opens at `max(6 dB, baseline + 6 dB)`, with
  3 dB of hysteresis, and the scanner re-measures once before stopping on a
  channel.
- **Demodulation.** FM discriminator, 300–3400 Hz voice band-pass (removes
  CTCSS tones), 300 Hz de-emphasis, and a slow AGC so all channels play at
  a similar loudness. Audio is 16 kHz and is Opus-encoded by aiortc.
- **States.**
  - *scanning:* hop between windows. When a squelch opens, stay until the
    channel has been quiet for `HANG_TIME`.
  - *paused:* hold on the current channel, or the one chosen with
    **Listen**.
  - *stopped:* the SDR is released.

## Development without Docker

```sh
sudo apt install python3-numpy python3-scipy python3-aiohttp python3-aiortc \
                 python3-soapysdr soapysdr0.8-module-rtlsdr
DATA_DIR=./data SDR_DEVICE=driver=sim python3 -m app.server
```

Install only the SoapySDR modules you need. With Debian's
`soapysdr-module-all` (pulled in as a recommendation), the UHD module was
seen to crash the process on a Pi. Install with `--no-install-recommends`.

On a Raspberry Pi 3, the `crc32c` module that aiortc uses for the data
channel crashes with "Illegal instruction" in its hardware mode. The server
sets `CRC32C_SW_MODE=force` itself. Set it as well for any other aiortc
program you run on the Pi, such as test clients.
