# --- Stage 1: build the SoapySDR module for Airspy HF+ (not packaged in Debian)
FROM debian:trixie-slim AS airspyhf
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates libsoapysdr-dev libairspyhf-dev \
 && rm -rf /var/lib/apt/lists/*
# The driver queues only 8 USB transfers of 2048 samples (~21 ms at 768 kS/s),
# which overflows on any hiccup on a Pi. Queue 256 (~0.7 s) instead, and size
# each slot for one transfer rather than 512 KB so that stays ~4 MB of RAM.
RUN git clone --depth 1 --branch soapy-airspyhf-0.2.1 \
      https://github.com/pothosware/SoapyAirspyHF.git /src \
 && sed -i 's/#define DEFAULT_NUM_BUFFERS 8/#define DEFAULT_NUM_BUFFERS 256/' /src/SoapyAirspyHF.hpp \
 && sed -i 's/buff\.\(reserve\|resize\)(bufferLength\*bytesPerSample)/buff.\1(4096*bytesPerSample)/' /src/Streaming.cpp \
 && grep -q "DEFAULT_NUM_BUFFERS 256" /src/SoapyAirspyHF.hpp \
 && grep -q "buff.reserve(4096\*bytesPerSample)" /src/Streaming.cpp \
 && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
 && cmake --build /build -j1 \
 && DESTDIR=/out cmake --install /build

# --- Stage 2: WebRTC libraries from PyPI wheels. Debian's python3-av drags in
# the full FFmpeg (LLVM, Mesa, video codecs: ~350 MB); the wheel bundles a
# minimal FFmpeg instead.
FROM debian:trixie-slim AS pydeps
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --break-system-packages --only-binary=:all: \
      --target /opt/pydeps -r /tmp/requirements.txt

# --- Stage 3: runtime
FROM debian:trixie-slim
# Only the RTL-SDR and Airspy HF+ drivers: other SoapySDR modules (UHD etc.)
# are not needed and have been seen to crash the process on a Pi.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-scipy python3-aiohttp \
      python3-soapysdr soapysdr0.8-module-rtlsdr rtl-sdr libairspyhf1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=airspyhf /out/ /
COPY --from=pydeps /opt/pydeps /opt/pydeps

WORKDIR /opt/sdr-scanner
COPY app ./app
COPY web ./web

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/pydeps \
    DATA_DIR=/data \
    PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python3", "-m", "app.server"]
