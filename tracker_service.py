#!/usr/bin/env python3
"""
Cloud-based tracker service for edge-inference detection streams.

Subscribes to MQTT detection topics, runs per-camera ByteTrack instances,
and publishes tracked detections with track IDs.
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Dict, Optional

import numpy as np
import paho.mqtt.client as mqtt
from boxmot import ByteTrack

logger = logging.getLogger(__name__)


class TrackerService:
    """Manages per-camera ByteTrack instances and MQTT communication."""

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        track_thresh: float = 0.5,
        match_thresh: float = 0.8,
        track_buffer: int = 30,
        tracker_cleanup_timeout_s: float = 300.0,
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password

        # ByteTrack configuration
        self.track_thresh = track_thresh
        self.match_thresh = match_thresh
        self.track_buffer = track_buffer

        # Per-(device, camera) tracker instances and last-seen timestamps.
        # Keyed by (device_id, camera_id) since a single service instance now
        # handles all devices, and camera IDs are only unique within a device.
        self.trackers: Dict[tuple[str, int], ByteTrack] = {}
        self.tracker_last_seen: Dict[tuple[str, int], float] = {}
        self.tracker_cleanup_timeout = tracker_cleanup_timeout_s

        # MQTT client
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
            logger.debug(f"MQTT authentication configured for user '{self.username}'")
        elif self.username or self.password:
            logger.warning(
                "Only one of MQTT_USERNAME/MQTT_PASSWORD was provided; MQTT authentication "
                "requires both, so connecting without authentication"
            )

    def _get_or_create_tracker(self, device_id: str, camera_id: int) -> ByteTrack:
        """Get existing tracker for (device, camera) or create new one."""
        key = (device_id, camera_id)
        if key not in self.trackers:
            logger.info(
                f"Creating new tracker for device {device_id}, camera {camera_id} "
                f"(track_thresh={self.track_thresh}, match_thresh={self.match_thresh}, "
                f"track_buffer={self.track_buffer})"
            )
            self.trackers[key] = ByteTrack(
                track_thresh=self.track_thresh,
                match_thresh=self.match_thresh,
                track_buffer=self.track_buffer,
            )
        return self.trackers[key]

    def _cleanup_stale_trackers(self) -> None:
        """Remove trackers that haven't been used recently."""
        now = time.time()
        stale_keys = [
            key
            for key, last_seen in self.tracker_last_seen.items()
            if now - last_seen > self.tracker_cleanup_timeout
        ]

        if not stale_keys:
            logger.debug(
                f"Stale tracker cleanup ran, nothing to evict "
                f"({len(self.trackers)} active tracker(s))"
            )
            return

        for key in stale_keys:
            device_id, camera_id = key
            logger.info(
                f"Cleaning up tracker for device {device_id}, camera {camera_id} "
                f"(idle for {now - self.tracker_last_seen[key]:.1f}s)"
            )
            del self.trackers[key]
            del self.tracker_last_seen[key]

    def _mqtt_message_to_detections(
        self, mqtt_payload: str,
    ) -> tuple[np.ndarray, dict]:
        """Parse MQTT detection message into numpy array and return payload dict.

        Returns:
            (detections_array, payload_dict) where detections_array has shape (N, 6)
            with columns [x1, y1, x2, y2, confidence, class_id] in pixel coordinates.
        """
        try:
            payload = json.loads(mqtt_payload)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse MQTT message: {e}")
            raise ValueError(f"Malformed JSON payload: {e}")

        detections = payload.get("detections", [])

        if not detections:
            logger.debug(
                f"Frame {payload.get('frame_count')} (camera {payload.get('camera_id')}) "
                f"has no detections"
            )
            return np.empty((0, 6), dtype=np.float32), payload

        img_w = payload.get("image_width")
        img_h = payload.get("image_height")

        if img_w is None or img_h is None:
            logger.error("Missing image_width or image_height in MQTT message")
            raise ValueError("Missing image dimensions")

        rows = []
        for det in detections:
            try:
                bbox = det.get("bbox", [])
                if len(bbox) != 4:
                    logger.warning(f"Invalid bbox format: {bbox}")
                    continue

                x_min, y_min, x_max, y_max = bbox
                rows.append(
                    [
                        x_min * img_w,
                        y_min * img_h,
                        x_max * img_w,
                        y_max * img_h,
                        float(det.get("confidence", 0.0)),
                        float(det.get("class_id", 0)),
                    ]
                )
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to process detection: {det}, error: {e}")
                continue

        return np.array(rows, dtype=np.float32), payload

    def _update_detections_with_tracks(
        self, payload: dict, tracks: np.ndarray
    ) -> dict:
        """Add track_id to each detection in the original payload.

        Args:
            payload: Original MQTT detection payload
            tracks: Array with shape (M, 8) from tracker.update():
                    [x1, y1, x2, y2, track_id, confidence, class_id, det_ind]

        Returns:
            Modified payload with track_id added to each detection
        """
        # Build mapping from detection index to track_id
        track_id_map = {}
        for track in tracks:
            det_ind = int(track[7])  # detection index
            track_id = int(track[4])
            track_id_map[det_ind] = track_id

        # Add track_id to each detection using its index
        modified_payload = json.loads(json.dumps(payload))  # Deep copy
        for det_idx, detection in enumerate(modified_payload.get("detections", [])):
            if det_idx in track_id_map:
                detection["track-id"] = track_id_map[det_idx]
            else:
                logger.warning(
                    f"Detection at index {det_idx} not found in tracker output"
                )
                detection["track-id"] = -1  # Untracked

        return modified_payload

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """MQTT callback: on connect."""
        if rc == 0:
            logger.info(f"Connected to MQTT broker at {self.broker_host}:{self.broker_port}")
            # Subscribe to detections for all devices and cameras
            topic = "cloud/+/detections/+"
            logger.info(f"Subscribing to topic: {topic}")
            client.subscribe(topic, qos=0)
        else:
            logger.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """MQTT callback: on disconnect."""
        if rc == 0:
            logger.info("Disconnected from MQTT broker")
        else:
            logger.warning(f"Unexpected disconnection with code {rc}, will auto-reconnect")

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        """MQTT callback: on subscribe."""
        logger.info(f"Subscription confirmed with QoS: {granted_qos}")

    def _on_message(self, client, userdata, msg):
        """MQTT callback: on message received."""
        try:
            logger.debug(
                f"Received message on topic '{msg.topic}' ({len(msg.payload)} bytes)"
            )

            # Parse topic to extract device_id and camera_id
            # Expected format: cloud/<device_id>/detections/<camera_id>
            topic_parts = msg.topic.split("/")
            if len(topic_parts) < 4:
                logger.warning(f"Invalid topic format: {msg.topic}")
                return

            device_id = topic_parts[1]
            try:
                camera_id = int(topic_parts[3])
            except (ValueError, IndexError):
                logger.warning(f"Could not parse camera_id from topic: {msg.topic}")
                return

            # Parse detection message
            try:
                dets, payload = self._mqtt_message_to_detections(msg.payload.decode())
            except (ValueError, UnicodeDecodeError) as e:
                logger.error(f"Failed to parse detection message: {e}")
                return

            # Get or create tracker for this device/camera
            tracker = self._get_or_create_tracker(device_id, camera_id)
            self.tracker_last_seen[(device_id, camera_id)] = time.time()

            # Create frame for BoxMOT (pixel content not used by ByteTrack)
            if payload.get("image_height") is None or payload.get("image_width") is None:
                logger.warning(
                    f"Device {device_id}, camera {camera_id}: image_width/image_height "
                    f"missing from payload, defaulting to 640x480 for the dummy tracker frame"
                )
            img_h = payload.get("image_height", 480)
            img_w = payload.get("image_width", 640)
            frame = np.zeros((img_h, img_w, 3), dtype=np.uint8)

            # Update tracker and get tracks
            tracks = tracker.update(dets, frame)
            logger.debug(
                f"Device {device_id}, camera {camera_id}: tracker.update() got "
                f"{len(dets)} detection(s), returned {len(tracks)} track(s)"
            )

            # Add track IDs to detections in payload
            tracked_payload = self._update_detections_with_tracks(payload, tracks)

            # Publish tracked detections
            publish_topic = f"cloud/{device_id}/tracked_detections/{camera_id}"
            try:
                result = client.publish(
                    publish_topic,
                    json.dumps(tracked_payload),
                    qos=0,
                    retain=False,
                )
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    logger.error(
                        f"Failed to publish to {publish_topic}: {mqtt.error_string(result.rc)}"
                    )
                else:
                    logger.debug(
                        f"Published tracked detections to {publish_topic} "
                        f"(frame={payload.get('frame_count')}, detections={payload.get('detection_count')})"
                    )
            except Exception as e:
                logger.error(f"Error publishing to {publish_topic}: {e}")

            # Periodically cleanup stale trackers
            if time.time() % 60 < 1:  # Approximately every 60s
                self._cleanup_stale_trackers()

        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}", exc_info=True)

    def start(self):
        """Connect to MQTT broker and start listening."""
        try:
            logger.info(
                f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}"
            )
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            logger.debug("Entering MQTT network loop (loop_forever)")
            self.client.loop_forever()
            logger.info("MQTT loop exited")
        except Exception as e:
            logger.error(f"Failed to start tracker service: {e}", exc_info=True)
            sys.exit(1)

    def stop(self):
        """Disconnect from MQTT broker."""
        logger.info("Stopping tracker service")
        self.client.disconnect()
        self.client.loop_stop()
        logger.info(f"Tracker service stopped ({len(self.trackers)} tracker(s) discarded)")



# Environment variable names and defaults for all service configuration.
# CLI flags were removed in favor of env vars so the service is easy to
# configure via Docker/Kubernetes/systemd without touching an entrypoint.
_ENV_DEFAULTS = {
    "MQTT_BROKER_HOST": "localhost",
    "MQTT_BROKER_PORT": "1883",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
    "TRACK_THRESH": "0.5",
    "MATCH_THRESH": "0.8",
    "TRACK_BUFFER": "30",
    "CLEANUP_TIMEOUT": "300.0",
    "LOG_LEVEL": "INFO",
}

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _load_config_from_env() -> dict:
    """Read and validate service configuration from environment variables."""
    env = {key: os.environ.get(key, default) for key, default in _ENV_DEFAULTS.items()}

    log_level = env["LOG_LEVEL"].upper()
    if log_level not in _LOG_LEVELS:
        raise ValueError(
            f"Invalid LOG_LEVEL={env['LOG_LEVEL']!r}; must be one of {_LOG_LEVELS}"
        )

    try:
        broker_port = int(env["MQTT_BROKER_PORT"])
        track_thresh = float(env["TRACK_THRESH"])
        match_thresh = float(env["MATCH_THRESH"])
        track_buffer = int(env["TRACK_BUFFER"])
        cleanup_timeout = float(env["CLEANUP_TIMEOUT"])
    except ValueError as e:
        raise ValueError(f"Invalid numeric environment variable: {e}")

    return {
        "broker_host": env["MQTT_BROKER_HOST"],
        "broker_port": broker_port,
        "username": env["MQTT_USERNAME"] or None,
        "password": env["MQTT_PASSWORD"] or None,
        "track_thresh": track_thresh,
        "match_thresh": match_thresh,
        "track_buffer": track_buffer,
        "cleanup_timeout": cleanup_timeout,
        "log_level": log_level,
    }


def main():
    try:
        config = _load_config_from_env()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, config["log_level"]),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting cloud tracker service")
    logger.info(f"Configuration: broker={config['broker_host']}:{config['broker_port']}")

    service = TrackerService(
        broker_host=config["broker_host"],
        broker_port=config["broker_port"],
        username=config["username"],
        password=config["password"],
        track_thresh=config["track_thresh"],
        match_thresh=config["match_thresh"],
        track_buffer=config["track_buffer"],
        tracker_cleanup_timeout_s=config["cleanup_timeout"],
    )

    try:
        service.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
        service.stop()


if __name__ == "__main__":
    main()
