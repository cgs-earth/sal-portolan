FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Set the working directory inside the container
WORKDIR /app

# rasterio/GDAL wheels link against libexpat at runtime; the slim base doesn't ship it.
# Force the https mirror: some networks block apt's default plain-http mirror.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/*.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so they're cached independently of source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

# Copy the script into the container
COPY main.py .

# Configure the script to be the main executable entrypoint
ENTRYPOINT ["uv", "run", "--no-sync", "main.py"]

# Default to showing the help menu if no subcommands are provided
CMD ["--help"]
