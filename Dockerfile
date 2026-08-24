FROM python:3.11-slim

LABEL maintainer="Edge Inference Team"
LABEL description="Cloud-based object tracker for edge-inference detection streams"

WORKDIR /app

# Install system dependencies (none needed for this service)
RUN apt-get update && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY tracker_service.py .

# Health check: Try connecting to the configured MQTT broker
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import os, paho.mqtt.client as mqtt; c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION1); c.connect(os.environ.get('MQTT_BROKER_HOST', 'localhost'), int(os.environ.get('MQTT_BROKER_PORT', '1883')), 1); print('OK')" || exit 1

# Configuration is entirely via environment variables (see README) — no CLI args
ENTRYPOINT ["python3", "tracker_service.py"]
