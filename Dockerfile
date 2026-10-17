# --- Stage 1: build the SoapySDR module for Airspy HF+ (not packaged in Debian)
FROM debian:trixie-slim AS airspyhf
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates libsoapysdr-dev libairspyhf-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch soapy-airspyhf-0.2.1 \
      https://github.com/pothosware/SoapyAirspyHF.git /src \
 && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
 && cmake --build /build -j2 \
 && DESTDIR=/out cmake --install /build

# --- Stage 2: runtime
FROM debian:trixie-slim
# Only the RTL-SDR and Airspy HF+ drivers: other SoapySDR modules (UHD etc.)
# are not needed and have been seen to crash the process on a Pi.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-scipy python3-aiohttp python3-aiortc \
      python3-soapysdr soapysdr0.8-module-rtlsdr libairspyhf1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=airspyhf /out/ /

WORKDIR /opt/sdr-scanner
COPY app ./app
COPY web ./web

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python3", "-m", "app.server"]
