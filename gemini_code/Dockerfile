# Dockerfile
FROM python:3.12-alpine
RUN apk add --no-cache iproute2 tcpdump
RUN pip install pyroute2 asyncio
COPY sbsp/ /app/sbsp/
WORKDIR /app
CMD ["python", "-m", "sbsp.daemon.main"]