# STORYFLEET Docker Image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p data/sessions data/media data/logs

# Create non-root user
RUN useradd -m -u 1000 storyfleet && \
    chown -R storyfleet:storyfleet /app
USER storyfleet

# Expose ports
EXPOSE 5000

# Default command
CMD ["python", "main.py", "dashboard"]
