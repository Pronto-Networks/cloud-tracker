# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone Python service (`tracker_service.py`) that bridges an edge-inference detection stream to a tracked-detection stream over MQTT. It subscribes to raw per-frame detections for *all* devices via a wildcard topic, runs ByteTrack (via `boxmot`) per device/camera, and republishes the same payload with a `track-id` added to each detection. There is no build step, web framework, or database — it's a single long-running MQTT consumer/producer process.

## Commands

```bash
pip install -r requirements.txt

# Run the service against a broker (config is env-vars only, no CLI args)
MQTT_BROKER_HOST=localhost LOG_LEVEL=DEBUG python tracker_service.py

# Exercise it locally (requires a running MQTT broker, e.g. `mosquitto -p 1883`)
python test_client.py --device-id edge-test --broker-host localhost
```

There is no automated test suite (no pytest/unittest files). `test_client.py` is the verification tool: it publishes synthetic detection frames to `cloud/<device_id>/detections/<camera_id>` and prints whatever comes back on `cloud/<device_id>/tracked_detections/<camera_id>`, so "testing" a change means running the service and `test_client.py` against each other via a local broker and reading the printed track IDs. See the "Testing" section of [README.md](README.md) for the manual `mosquitto_pub`/`mosquitto_sub` walkthrough. Note `test_client.py` itself is unaffected by the env-var change — it still simulates one device via `--device-id`.

Docker: `docker build -t cloud-tracker .` then `docker run --env MQTT_BROKER_HOST=... cloud-tracker` (see [Dockerfile](Dockerfile)), or `docker compose up --build` to run it alongside a Mosquitto broker (see [docker-compose.yml](docker-compose.yml)).

## Architecture

Everything lives in one class, `TrackerService` in [tracker_service.py](tracker_service.py). The flow through it, in order, matters more than any individual method:

1. **One MQTT connection, fully wildcard subscription.** On connect, it subscribes once to `cloud/+/detections/+` — the first `+` is the device ID, the second is the camera ID, so a single service instance handles all cameras for all devices. There is no device-id setting anywhere; the service is inherently multi-device.
2. **Per-device, per-camera tracker state (`_get_or_create_tracker`).** `self.trackers: Dict[(device_id, camera_id), ByteTrack]` lazily creates one ByteTrack instance per (device, camera) pair on first message. Track IDs are only unique *within* one tracker, not across cameras, across devices, or across a tracker recycle.
3. **Message handling (`_on_message`) is the core pipeline**, run synchronously on the MQTT network thread for every message:
   - Extract `device_id` and `camera_id` from the topic path (`cloud/<device_id>/detections/<camera_id>` — indices 1 and 3 after split on `/`).
   - `_mqtt_message_to_detections` parses the JSON payload and converts each detection's normalized `bbox` (`[x_min, y_min, x_max, y_max]` in 0.0–1.0) into pixel coordinates using `image_width`/`image_height` from the payload, producing an `(N, 6)` numpy array `[x1, y1, x2, y2, confidence, class_id]` that boxmot expects. Malformed JSON or missing dimensions raises `ValueError`, which is caught and logged — the message is dropped, not retried.
   - The tracker is updated with a zero-filled dummy frame (`np.zeros((h, w, 3))`) since ByteTrack needs a frame arg but doesn't use its pixel content for this tracker type.
   - `_update_detections_with_tracks` maps boxmot's output rows back onto the *original* detections: `det_ind` (column 7 of the tracker output) is a row of the array passed to the tracker, and the `source_indices` list returned by `_mqtt_message_to_detections` converts that row to the index in `payload["detections"]` (invalid detections are skipped, so the two differ). It deep-copies the original payload so the response mirrors the input schema exactly, plus `track-id` per detection (`-1` if boxmot dropped that detection, e.g. below `track_thresh`).
   - Result is published to `cloud/<device_id>/tracked_detections/<camera_id>` — same device, same camera (both taken from the inbound topic), different path segment.
4. **Stale tracker cleanup** runs opportunistically inside `_on_message` via a `time.time() % 60 < 1` check (not a separate thread/timer) — so cleanup only happens while messages are still arriving, and only approximately every 60s.

### Payload contract

The service never changes the shape of the input JSON — it always deep-copies the incoming payload and adds `track-id` to each object in `detections`. Any new field added to the input schema flows through to the output automatically. See "Input/Output Payload Format" in [README.md](README.md) for the exact JSON shape.

### Key parameters (all env vars, no CLI args, no config file)

All configuration — `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_USERNAME`/`MQTT_PASSWORD`, `TRACK_THRESH`, `MATCH_THRESH`, `TRACK_BUFFER`, `CLEANUP_TIMEOUT`, `LOG_LEVEL` — is read once at startup in `main()`'s `_load_config_from_env()` ([tracker_service.py](tracker_service.py)); there is no argparse and no config file. `TRACK_THRESH` / `MATCH_THRESH` / `TRACK_BUFFER` are passed straight through to `boxmot.ByteTrack` per tracker instance — they're fixed for the lifetime of a tracker but apply identically to every device/camera pair (there's no per-device or per-camera override). `CLEANUP_TIMEOUT` controls only the idle-eviction check described above, independent of the ByteTrack params. An invalid or unparseable env var makes the service print a config error and `sys.exit(1)` immediately, rather than silently falling back to a default.
