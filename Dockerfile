# NetAudit Web — Kali base so the full aircrack-ng suite installs cleanly.
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# Wireless tooling + Python runtime.
#  - aircrack-ng: airmon-ng, airodump-ng, aireplay-ng, aircrack-ng
#  - iw / wireless-tools: interface enumeration & mode control
#  - pciutils/usbutils + firmware: adapter detection inside the container
RUN apt-get update && apt-get install -y --no-install-recommends \
        aircrack-ng \
        iw \
        wireless-tools \
        pciutils \
        usbutils \
        firmware-linux-free \
        python3 \
        python3-pip \
        procps \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# Volume mount points for persisted artifacts.
RUN mkdir -p /app/captures /app/wordlists
VOLUME ["/app/captures", "/app/wordlists"]

# Runs as root inside the privileged container so airmon-ng/airodump can touch
# the host Wi-Fi hardware. Sudo is therefore unnecessary (CRYFI_USE_SUDO=0).
# --proxy-headers: trust X-Forwarded-* from a TLS reverse proxy (e.g. for
# https://wifi.malik.casa) so the scheme/secure-cookie logic is correct.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
