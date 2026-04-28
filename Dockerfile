FROM python:3.12-alpine

# System dependencies
RUN apk add --no-cache \
    iproute2 \
    tcpdump \
    iputils \
    busybox-extras \
    && rm -rf /var/cache/apk/*

# Python dependencies
RUN pip install --no-cache-dir \
    pyroute2 \
    pytest \
    pytest-asyncio

# Copy entire project and install as a package
WORKDIR /app
COPY sbsp/       /app/sbsp/
COPY setup.py    /app/setup.py
COPY pytest.ini  /app/pytest.ini

# Install sbsp package so `python -m sbsp.daemon.main` works from anywhere
RUN pip install --no-cache-dir -e .

# Entry-point script reads env vars SBSP_ROUTER_ID, SBSP_AREA, SBSP_LOG_LEVEL
COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
