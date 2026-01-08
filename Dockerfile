# Multi-stage build for Franklin WH Battery Automation
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Final stage - minimal runtime image
FROM python:3.11-slim

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create app directory and logs directory
WORKDIR /app
RUN mkdir -p /app/logs

# Copy application files
COPY scripts/*.py /app/
COPY scripts/*.sh /app/

# Make scripts executable
RUN chmod +x /app/*.py /app/*.sh

# Create non-root user for security
RUN useradd -m -u 1000 franklin && \
    chown -R franklin:franklin /app
USER franklin

# Default command (can be overridden)
CMD ["python", "/app/smart_decision.py"]
