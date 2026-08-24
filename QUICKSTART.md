# Quick Start Guide

## 5-Minute Local Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start MQTT Broker (Mosquitto)

```bash
# Ubuntu/Debian
sudo apt-get install mosquitto
mosquitto -p 1883

# Or use Docker
docker run -p 1883:1883 eclipse-mosquitto
```

### 3. Start Tracker Service (Terminal 1)

```bash
python tracker_service.py --device-id edge-test --broker-host localhost --log-level INFO
```

Expected output:
```
2024-03-14 10:30:45 - root - INFO - Starting cloud tracker service
2024-03-14 10:30:45 - root - INFO - Configuration: device_id=edge-test, broker=localhost:1883
2024-03-14 10:30:45 - root - INFO - Connecting to MQTT broker at localhost:1883
2024-03-14 10:30:45 - root - INFO - Connected to MQTT broker at localhost:1883
2024-03-14 10:30:45 - root - INFO - Subscribing to topic: edge/edge-test/data/detections/+
```

### 4. Run Test Client (Terminal 2)

```bash
python test_client.py --device-id edge-test --broker-host localhost
```

Expected output:
```
🚀 Cloud Tracker Test Client
   Device ID: edge-test
   Broker: localhost:1883
   Cameras: [1, 2]
   Frames per camera: 10
   Interval: 1.0s

✓ Connected to localhost:1883
✓ Subscribing to: edge/edge-test/data/tracked_detections/+
⏳ Waiting for tracker to connect...
✓ Published detection to edge/edge-test/data/detections/1 (frame=1, detections=2)
✓ Published detection to edge/edge-test/data/detections/2 (frame=1, detections=2)
...

📬 Tracked detections received on: edge/edge-test/data/tracked_detections/1
   Frame: 1, Detections: 2
     [0] track_id=1 class=person conf=0.85
     [1] track_id=2 class=person conf=0.90
...

✓ Test complete
✓ Disconnected
```

## Production Deployment

### Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  mqtt:
    image: eclipse-mosquitto:2.0
    ports:
      - "1883:1883"
    volumes:
      - ./mosquitto.conf:/mosquitto/config/mosquitto.conf
      - mosquitto_data:/mosquitto/data

  tracker:
    build: .
    depends_on:
      - mqtt
    environment:
      - DEVICE_ID=edge-AABBCCDDEEFF
      - MQTT_HOST=mqtt
      - MQTT_PORT=1883
    command:
      - --device-id
      - edge-AABBCCDDEEFF
      - --broker-host
      - mqtt
      - --broker-port
      - "1883"
      - --log-level
      - INFO

volumes:
  mosquitto_data:
```

```bash
docker-compose up
```

### Kubernetes

Create `tracker-deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloud-tracker
  labels:
    app: cloud-tracker
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cloud-tracker
  template:
    metadata:
      labels:
        app: cloud-tracker
    spec:
      containers:
      - name: tracker
        image: cloud-tracker:latest
        args:
          - --device-id
          - edge-AABBCCDDEEFF
          - --broker-host
          - mqtt-service
          - --broker-port
          - "1883"
        resources:
          requests:
            memory: "256Mi"
            cpu: "500m"
          limits:
            memory: "512Mi"
            cpu: "1000m"
```

```bash
kubectl apply -f tracker-deployment.yaml
```

## Monitoring

### Check Service Logs

```bash
# Docker
docker logs -f cloud-tracker

# Systemd
journalctl -u cloud-tracker -f

# Direct logs
python tracker_service.py --device-id edge-test --log-level DEBUG 2>&1 | tee tracker.log
```

### Verify Message Flow

Monitor published topics:

```bash
# Terminal: Watch for detection messages
mosquitto_sub -h localhost -t "edge/edge-test/data/detections/+" -v

# Terminal: Watch for tracked results
mosquitto_sub -h localhost -t "edge/edge-test/data/tracked_detections/+" -v
```

## Common Issues

**"Connection refused" on startup:**
- Ensure MQTT broker is running: `netstat -an | grep 1883`
- Check broker host/port configuration

**No tracked detections received:**
- Verify tracker service is subscribing: Look for "Subscribing to topic" in logs
- Check device_id matches in publisher: `edge/<device_id>/data/detections/...`
- Try with `--log-level DEBUG` for detailed output

**Track IDs resetting frequently:**
- Increase `--track-buffer` to keep tracks alive longer
- Reduce `--track-thresh` if detections are being filtered
- Check confidence values in incoming detections

**High CPU/Memory usage:**
- Reduce `--track-buffer` (fewer frames kept in memory)
- Lower `--cleanup-timeout` to recycle idle trackers faster
- Reduce number of concurrent cameras

## Next Steps

1. **Replace device_id:** Update `--device-id` to match your actual device
2. **Configure MQTT broker:** Set host/port for your production broker
3. **Tune tracker parameters:** Adjust thresholds based on your detection quality
4. **Set up logging:** Configure `--log-level` for production (typically INFO)
5. **Deploy:** Use Docker or systemd for continuous operation

See README.md for detailed documentation.
