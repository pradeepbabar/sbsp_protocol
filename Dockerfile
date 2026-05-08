FROM python:3.12-alpine

RUN apk add --no-cache iproute2 tcpdump tini && rm -rf /var/cache/apk/*

RUN pip install --no-cache-dir pyroute2

WORKDIR /app

# Copy the sbsp package
COPY sbsp/ /app/sbsp/

# Show exactly what was copied - makes missing files obvious in build log
RUN echo "=== Copied files ===" && find /app/sbsp -type f | sort

# Ensure all __init__.py files exist (safety net if they were missing on host)
#RUN touch /app/sbsp/__init__.py \
#    && touch /app/sbsp/algo/__init__.py \
#    && touch /app/sbsp/daemon/__init__.py \
#    && touch /app/sbsp/cli/__init__.py

# Set Python path so sbsp package is found
ENV PYTHONPATH=/app

# Verify import - build fails here if any source file is missing
#RUN python -c "from sbsp.daemon.main import main; print('OK: import verified')"

COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

CMD ["/sbin/tini", "--", "sleep", "infinity"]