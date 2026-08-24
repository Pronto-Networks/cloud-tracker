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
MQTT_BROKER_HOST=localhost LOG_LEVEL=INFO python tracker_service.py
```

Expected output:
```
2024-03-14 10:30:45 - root - INFO - Starting cloud tracker service
2024-03-14 10:30:45 - root - INFO - Configuration: broker=localhost:1883
2024-03-14 10:30:45 - root - INFO - Connecting to MQTT broker at localhost:1883
2024-03-14 10:30:45 - root - INFO - Connected to MQTT broker at localhost:1883
2024-03-14 10:30:45 - root - INFO - Subscribing to topic: cloud/+/detections/+
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
✓ Subscribing to: cloud/edge-test/tracked_detections/+
⏳ Waiting for tracker to connect...
✓ Published detection to cloud/edge-test/detections/1 (frame=1, detections=2)
✓ Published detection to cloud/edge-test/detections/2 (frame=1, detections=2)
...

📬 Tracked detections received on: cloud/edge-test/tracked_detections/1
   Frame: 1, Detections: 2
     [0] track_id=1 class=person conf=0.85
     [1] track_id=2 class=person conf=0.90
...

✓ Test complete
✓ Disconnected
```

## Production Deployment

### Docker Compose

The repo already ships [docker-compose.yml](docker-compose.yml) (a Mosquitto broker + the tracker service, both configured entirely through environment variables — no CLI args) and the [mosquitto.conf](mosquitto.conf) it mounts:

```bash
docker compose up --build
```

To change a setting, edit the `tracker.environment` block in `docker-compose.yml` (see the [Environment Variables](README.md#environment-variables) table in README.md for the full list) — for example:

```yaml
    environment:
      MQTT_BROKER_HOST: mqtt
      MQTT_BROKER_PORT: "1883"
      LOG_LEVEL: DEBUG
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
        env:
          - name: MQTT_BROKER_HOST
            value: mqtt-service
          - name: MQTT_BROKER_PORT
            value: "1883"
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
LOG_LEVEL=DEBUG python tracker_service.py 2>&1 | tee tracker.log
```

### Verify Message Flow

Monitor published topics:

```bash
# Terminal: Watch for detection messages (all devices/cameras)
mosquitto_sub -h localhost -t "cloud/+/detections/+" -v

# Terminal: Watch for tracked results (all devices/cameras)
mosquitto_sub -h localhost -t "cloud/+/tracked_detections/+" -v
```

## Common Issues

**"Connection refused" on startup:**
- Ensure MQTT broker is running: `netstat -an | grep 1883`
- Check broker host/port configuration

**No tracked detections received:**
- Verify tracker service is subscribing: Look for "Subscribing to topic" in logs
- Check the publisher's topic matches the expected shape: `cloud/<device_id>/detections/<camera_id>`
- Try with `LOG_LEVEL=DEBUG` for detailed output

**Track IDs resetting frequently:**
- Increase `TRACK_BUFFER` to keep tracks alive longer
- Reduce `TRACK_THRESH` if detections are being filtered
- Check confidence values in incoming detections

**High CPU/Memory usage:**
- Reduce `TRACK_BUFFER` (fewer frames kept in memory)
- Lower `CLEANUP_TIMEOUT` to recycle idle trackers faster
- Reduce number of concurrent cameras

## Next Steps

1. **Configure MQTT broker:** Set `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT` for your production broker
2. **Tune tracker parameters:** Adjust `TRACK_THRESH`/`MATCH_THRESH`/`TRACK_BUFFER` based on your detection quality
3. **Set up logging:** Set `LOG_LEVEL` for production (typically `INFO`)
4. **Deploy:** Use `docker compose up` (or systemd) for continuous operation — one instance now serves every device publishing under `cloud/+/detections/+`

See README.md for detailed documentation.
