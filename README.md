# Cloud Tracker Service

A cloud-based multi-camera object tracking service that consumes MQTT detection streams from edge-inference devices and publishes tracked detections with track IDs.

## Overview

The tracker service:
- Subscribes to `edge/<device_id>/data/detections/<camera_id>` MQTT topics
- Maintains a separate ByteTrack instance per camera
- Adds a `track-id` field to each detected object
- Publishes results to `edge/<device_id>/data/tracked_detections/<camera_id>`
- Handles connection failures and stale tracker cleanup automatically

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Basic Example

```bash
python tracker_service.py \
  --device-id edge-AABBCCDDEEFF \
  --broker-host mqtt.example.com \
  --broker-port 1883
```

### With Authentication

```bash
python tracker_service.py \
  --device-id edge-AABBCCDDEEFF \
  --broker-host mqtt.example.com \
  --broker-port 8883 \
  --username tracking_user \
  --password secret_password
```

### Tuning ByteTrack Parameters

```bash
python tracker_service.py \
  --device-id edge-AABBCCDDEEFF \
  --broker-host localhost \
  --track-thresh 0.6 \
  --match-thresh 0.7 \
  --track-buffer 50 \
  --cleanup-timeout 600
```

## Command-Line Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--device-id` | str | **required** | Device ID to track (e.g., `edge-AABBCCDDEEFF`) |
| `--broker-host` | str | `localhost` | MQTT broker hostname |
| `--broker-port` | int | `1883` | MQTT broker port |
| `--username` | str | - | MQTT username (optional) |
| `--password` | str | - | MQTT password (optional) |
| `--track-thresh` | float | `0.5` | ByteTrack confidence threshold for initiating new tracks |
| `--match-thresh` | float | `0.8` | ByteTrack IoU threshold for matching detections to existing tracks |
| `--track-buffer` | int | `30` | Number of frames to keep lost tracks alive (for occlusion tolerance) |
| `--cleanup-timeout` | float | `300.0` | Seconds before removing idle tracker instances |
| `--log-level` | str | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Input Payload Format

Detection messages arrive on `edge/<device_id>/data/detections/<camera_id>`:

```json
{
  "camera_id": 1,
  "frame_count": 42,
  "pts": 7914670924,
  "timestamp": 1710500001123,
  "inference_time_ms": 20.5,
  "image_width": 640,
  "image_height": 480,
  "detection_count": 2,
  "detections": [
    {
      "class": "person",
      "class_id": 1,
      "confidence": 0.87,
      "bbox": [0.1, 0.2, 0.5, 0.8],
      "roi-id": "roi-1"
    }
  ],
  "image_base64": "..."
}
```

## Output Payload Format

Tracked detections are published to `edge/<device_id>/data/tracked_detections/<camera_id>` with the original payload plus a `track-id` field on each detection:

```json
{
  "camera_id": 1,
  "frame_count": 42,
  "pts": 7914670924,
  "timestamp": 1710500001123,
  "inference_time_ms": 20.5,
  "image_width": 640,
  "image_height": 480,
  "detection_count": 2,
  "detections": [
    {
      "class": "person",
      "class_id": 1,
      "confidence": 0.87,
      "bbox": [0.1, 0.2, 0.5, 0.8],
      "roi-id": "roi-1",
      "track-id": 5
    }
  ],
  "image_base64": "..."
}
```

## Architecture

### Per-Camera Tracker Instances

Each camera ID gets its own ByteTrack instance maintained in memory. Track IDs are stable within a camera's tracker but may restart if the tracker is recycled.

### Automatic Cleanup

Trackers idle for longer than `--cleanup-timeout` (default 300s) are automatically removed to free memory. This is useful for multi-camera systems where not all cameras are always active.

### Error Handling

- **Malformed JSON**: Logged and skipped; service continues
- **Missing dimensions**: Detection skipped but frame still processed
- **MQTT disconnection**: Automatic reconnection with exponential backoff
- **Publish failures**: Logged; detection still tracked locally

## Logging

The service uses Python's standard logging. Use `--log-level DEBUG` for detailed trace output, useful for diagnosing tracker behavior.

```bash
python tracker_service.py --device-id edge-demo --log-level DEBUG
```

## Performance Considerations

- **Memory**: ~10-50 MB per active tracker (depends on track_buffer size)
- **CPU**: ByteTrack is CPU-efficient; one service can handle 5-10 concurrent cameras on a modest machine
- **Latency**: Sub-millisecond added per detection (tracker update is very fast)

## Testing

To test with a local MQTT broker (Mosquitto):

```bash
# Terminal 1: Start MQTT broker
mosquitto -p 1883

# Terminal 2: Start tracker service
python tracker_service.py --device-id edge-test --broker-host localhost

# Terminal 3: Send test detection message
mosquitto_pub -h localhost -t "edge/edge-test/data/detections/1" -m '
{
  "camera_id": 1,
  "frame_count": 1,
  "pts": 1000,
  "timestamp": 1710500001000,
  "inference_time_ms": 20.5,
  "image_width": 640,
  "image_height": 480,
  "detection_count": 1,
  "detections": [
    {
      "class": "person",
      "class_id": 1,
      "confidence": 0.95,
      "bbox": [0.1, 0.2, 0.5, 0.8],
      "roi-id": ""
    }
  ],
  "image_base64": ""
}
'

# Terminal 2: Should show "Published tracked detections..."

# Terminal 3: Verify the output
mosquitto_sub -h localhost -t "edge/edge-test/data/tracked_detections/1"
```

## Deployment

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tracker_service.py .

ENTRYPOINT ["python", "tracker_service.py"]
```

```bash
docker build -t cloud-tracker .
docker run --rm \
  --env MQTT_HOST=mqtt.example.com \
  --env MQTT_PORT=1883 \
  cloud-tracker \
  --device-id edge-AABBCCDDEEFF \
  --broker-host $MQTT_HOST \
  --broker-port $MQTT_PORT
```

### Systemd Service

Create `/etc/systemd/system/cloud-tracker.service`:

```ini
[Unit]
Description=Cloud Tracker Service
After=network.target

[Service]
Type=simple
User=tracker
WorkingDirectory=/opt/cloud-tracker
ExecStart=/usr/bin/python3 /opt/cloud-tracker/tracker_service.py \
  --device-id edge-AABBCCDDEEFF \
  --broker-host mqtt.example.com \
  --broker-port 1883 \
  --log-level INFO

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable cloud-tracker
sudo systemctl start cloud-tracker
sudo journalctl -u cloud-tracker -f
```

## Troubleshooting

**Service won't connect to MQTT broker:**
- Check broker host/port
- Verify firewall rules
- Try with `--log-level DEBUG`

**Track IDs jumping unexpectedly:**
- Reduce `--track-buffer` if tracker is coasting too long
- Increase `--match-thresh` for stricter matching

**High memory usage:**
- Reduce `--track-buffer`
- Lower `--cleanup-timeout` to recycle idle trackers more aggressively

**Missing track IDs on some detections:**
- Check confidence thresholds; low-confidence detections may not be tracked
- Verify `--track-thresh` is not filtering out valid detections

## License

This service is part of the edge-inference project.
