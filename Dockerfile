# ==============================================================================
# FranklinWH Battery Automation - Docker Image
# ==============================================================================
#
# Multi-stage build for minimal image size
#
# Build: docker compose build
# Run:   docker compose up -d
#
# ==============================================================================

# Base image pinned (T051): the v4.7.0-rc.6 production image is Python 3.11.17.
FROM python:3.11.17-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ==============================================================================
# Final stage - minimal runtime image
# ==============================================================================
FROM python:3.11.17-slim

# Install runtime dependencies (for matplotlib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 \
    libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Create directories for data persistence
RUN mkdir -p /app/scripts /app/logs /app/data /app/web

# Copy application files
COPY scripts/*.py /app/scripts/
COPY VERSION /app/VERSION

# Make scripts executable
RUN chmod +x /app/scripts/*.py

# Create non-root user for security

# Switch to non-root user

# Set Python to run unbuffered for real-time logs
ENV PYTHONUNBUFFERED=1

# Health check - verify the scheduler's API server is answering (pgrep is not in python:slim)
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/version' % os.getenv('API_PORT','8101'), timeout=10)" || exit 1

# Default command - run the scheduler
CMD ["python", "/app/scripts/scheduler.py"]
