# The match runs this with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13-slim
WORKDIR /bot
COPY bot.py mechfight.py ./
COPY arena/ ./arena/
# Build the walk grid once at image build time so the bot is ready in well under a second.
RUN python3 bot.py --precompute /bot/arena /bot/nav.bin && chmod 644 /bot/nav.bin
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
