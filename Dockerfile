# syntax=docker/dockerfile:1

FROM python:3.11-slim

# Avoid interactive prompts and keep Python output unbuffered.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git is needed for the git-state features; build tools for any C extensions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package. setup.py maps this directory to the top-level `mote`
# package, so copy the whole tree then install in editable mode.
COPY . /app
RUN pip install --upgrade pip && pip install -e .

# Config lives at ~/.mote/config.yaml at runtime (mount it in).
ENTRYPOINT ["python", "-m", "mote.cli"]
