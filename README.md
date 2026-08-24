# Cloud Tracker Service

A cloud-based multi-camera object tracking service that consumes MQTT detection streams from edge-inference devices and publishes tracked detections with track IDs.

## Overview

The tracker service:
- Subscribes to `cloud/+/detections/+` (device ID and camera ID both wildcarded — one instance handles all devices)
- Maintains a separate ByteTrack instance per (device, camera) pair
- Adds a `track-id` field to each detected object
- Publishes results to `cloud/<device_id>/tracked_detections/<camera_id>`
- Handles connection failures and stale tracker cleanup automatically

## Installation

```bash
pip install -r requirements.txt
```

## Usage

The service takes no CLI arguments — everything is configured via environment variables so it drops straight into Docker/Kubernetes/systemd without a custom entrypoint. The service subscribes to `cloud/+/detections/+` and handles every device and camera it sees; there is no device-id setting, since the device ID is read per-message from the topic.

### Basic Example

```bash
MQTT_BROKER_HOST=mqtt.example.com \
MQTT_BROKER_PORT=1883 \
python tracker_service.py
```

### With Authentication

```bash
MQTT_BROKER_HOST=mqtt.example.com \
MQTT_BROKER_PORT=8883 \
MQTT_USERNAME=tracking_user \
MQTT_PASSWORD=secret_password \
python tracker_service.py
```

### Tuning ByteTrack Parameters

```bash
MQTT_BROKER_HOST=localhost \
TRACK_THRESH=0.6 \
MATCH_THRESH=0.7 \
TRACK_BUFFER=50 \
CLEANUP_TIMEOUT=600 \
python tracker_service.py
```

## Environment Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MQTT_BROKER_HOST` | str | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | int | `1883` | MQTT broker port |
| `MQTT_USERNAME` | str | - | MQTT username (optional; leave unset/empty for no auth) |
| `MQTT_PASSWORD` | str | - | MQTT password (optional; leave unset/empty for no auth) |
| `TRACK_THRESH` | float | `0.5` | ByteTrack confidence threshold for initiating new tracks |
| `MATCH_THRESH` | float | `0.8` | ByteTrack IoU threshold for matching detections to existing tracks |
| `TRACK_BUFFER` | int | `30` | Number of frames to keep lost tracks alive (for occlusion tolerance) |
| `CLEANUP_TIMEOUT` | float | `300.0` | Seconds before removing idle tracker instances |
| `LOG_LEVEL` | str | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

An invalid value (unparseable number, unrecognized `LOG_LEVEL`) makes the service print a `Configuration error:` message and exit immediately rather than falling back to a default.

## Input Payload Format

Detection messages arrive on `cloud/<device_id>/detections/<camera_id>`:

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

Tracked detections are published to `cloud/<device_id>/tracked_detections/<camera_id>` with the original payload plus a `track-id` field on each detection:

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

### Per-Device, Per-Camera Tracker Instances

Each `(device_id, camera_id)` pair gets its own ByteTrack instance maintained in memory. Track IDs are stable within a tracker but may restart if the tracker is recycled, and are not comparable across devices or cameras.

### Automatic Cleanup

Trackers idle for longer than `CLEANUP_TIMEOUT` (default 300s) are automatically removed to free memory. This is useful for multi-camera systems where not all cameras are always active.

### Error Handling

- **Malformed JSON**: Logged and skipped; service continues
- **Missing dimensions**: Detection skipped but frame still processed
- **MQTT disconnection**: Automatic reconnection with exponential backoff
- **Publish failures**: Logged; detection still tracked locally

## Logging

The service uses Python's standard logging. Use `LOG_LEVEL=DEBUG` for detailed trace output, useful for diagnosing tracker behavior.

```bash
LOG_LEVEL=DEBUG python tracker_service.py
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
MQTT_BROKER_HOST=localhost python tracker_service.py

# Terminal 3: Send test detection message
mosquitto_pub -h localhost -t "cloud/edge-test/detections/1" -m '
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
mosquitto_sub -h localhost -t "cloud/edge-test/tracked_detections/1"
```

## Deployment

### Docker Compose

The bundled [docker-compose.yml](docker-compose.yml) runs a Mosquitto broker and the tracker service together, wired up via environment variables — no CLI args anywhere:

```bash
docker compose up --build
```

Override any setting from the [Environment Variables](#environment-variables) table by editing the `tracker.environment` block in `docker-compose.yml`, or with an `.env` file / `docker compose run -e VAR=value`.

### Docker (standalone)

```bash
docker build -t cloud-tracker .
docker run --rm \
  --env MQTT_BROKER_HOST=mqtt.example.com \
  --env MQTT_BROKER_PORT=1883 \
  cloud-tracker
```

## Troubleshooting

**Service won't connect to MQTT broker:**
- Check `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT`
- Verify firewall rules
- Try with `LOG_LEVEL=DEBUG`

**Track IDs jumping unexpectedly:**
- Reduce `TRACK_BUFFER` if tracker is coasting too long
- Increase `MATCH_THRESH` for stricter matching

**High memory usage:**
- Reduce `TRACK_BUFFER`
- Lower `CLEANUP_TIMEOUT` to recycle idle trackers more aggressively

**Missing track IDs on some detections:**
- Check confidence thresholds; low-confidence detections may not be tracked
- Verify `TRACK_THRESH` is not filtering out valid detections
