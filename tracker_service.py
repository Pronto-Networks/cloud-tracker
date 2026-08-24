#!/usr/bin/env python3
"""
Cloud-based tracker service for edge-inference detection streams.

Subscribes to MQTT detection topics, runs per-camera ByteTrack instances,
and publishes tracked detections with track IDs.
"""

import argparse
import json
import logging
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
        device_id: str,
        broker_host: str,
        broker_port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        track_thresh: float = 0.5,
        match_thresh: float = 0.8,
        track_buffer: int = 30,
        tracker_cleanup_timeout_s: float = 300.0,
    ):
        self.device_id = device_id
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password

        # ByteTrack configuration
        self.track_thresh = track_thresh
        self.match_thresh = match_thresh
        self.track_buffer = track_buffer

        # Per-camera tracker instances and last-seen timestamps
        self.trackers: Dict[int, ByteTrack] = {}
        self.tracker_last_seen: Dict[int, float] = {}
        self.tracker_cleanup_timeout = tracker_cleanup_timeout_s

        # MQTT client
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

    def _get_or_create_tracker(self, camera_id: int) -> ByteTrack:
        """Get existing tracker for camera or create new one."""
        if camera_id not in self.trackers:
            logger.info(
                f"Creating new tracker for camera {camera_id} "
                f"(track_thresh={self.track_thresh}, match_thresh={self.match_thresh}, "
                f"track_buffer={self.track_buffer})"
            )
            self.trackers[camera_id] = ByteTrack(
                track_thresh=self.track_thresh,
                match_thresh=self.match_thresh,
                track_buffer=self.track_buffer,
            )
        return self.trackers[camera_id]

    def _cleanup_stale_trackers(self) -> None:
        """Remove trackers that haven't been used recently."""
        now = time.time()
        stale_cameras = [
            camera_id
            for camera_id, last_seen in self.tracker_last_seen.items()
            if now - last_seen > self.tracker_cleanup_timeout
        ]

        for camera_id in stale_cameras:
            logger.info(
                f"Cleaning up tracker for camera {camera_id} "
                f"(idle for {now - self.tracker_last_seen[camera_id]:.1f}s)"
            )
            del self.trackers[camera_id]
            del self.tracker_last_seen[camera_id]

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
            # Subscribe to all detections for this device
            topic = f"edge/{self.device_id}/data/detections/+"
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
            # Parse topic to extract camera_id
            topic_parts = msg.topic.split("/")
            if len(topic_parts) < 5:
                logger.warning(f"Invalid topic format: {msg.topic}")
                return

            try:
                camera_id = int(topic_parts[4])
            except (ValueError, IndexError):
                logger.warning(f"Could not parse camera_id from topic: {msg.topic}")
                return

            # Parse detection message
            try:
                dets, payload = self._mqtt_message_to_detections(msg.payload.decode())
            except (ValueError, UnicodeDecodeError) as e:
                logger.error(f"Failed to parse detection message: {e}")
                return

            # Get or create tracker for this camera
            tracker = self._get_or_create_tracker(camera_id)
            self.tracker_last_seen[camera_id] = time.time()

            # Create frame for BoxMOT (pixel content not used by ByteTrack)
            img_h = payload.get("image_height", 480)
            img_w = payload.get("image_width", 640)
            frame = np.zeros((img_h, img_w, 3), dtype=np.uint8)

            # Update tracker and get tracks
            tracks = tracker.update(dets, frame)

            # Add track IDs to detections in payload
            tracked_payload = self._update_detections_with_tracks(payload, tracks)

            # Publish tracked detections
            publish_topic = f"edge/{self.device_id}/data/tracked_detections/{camera_id}"
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
            self.client.loop_forever()
        except Exception as e:
            logger.error(f"Failed to start tracker service: {e}", exc_info=True)
            sys.exit(1)

    def stop(self):
        """Disconnect from MQTT broker."""
        logger.info("Stopping tracker service")
        self.client.disconnect()
        self.client.loop_stop()


def main():
    parser = argparse.ArgumentParser(
        description="Cloud-based tracker service for edge-inference detection streams"
    )
    parser.add_argument(
        "--device-id",
        required=True,
        help="Device ID to subscribe to (e.g., edge-AABBCCDDEEFF)",
    )
    parser.add_argument(
        "--broker-host",
        default="localhost",
        help="MQTT broker hostname (default: localhost)",
    )
    parser.add_argument(
        "--broker-port",
        type=int,
        default=1883,
        help="MQTT broker port (default: 1883)",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="MQTT broker username (optional)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="MQTT broker password (optional)",
    )
    parser.add_argument(
        "--track-thresh",
        type=float,
        default=0.5,
        help="ByteTrack confidence threshold for new tracks (default: 0.5)",
    )
    parser.add_argument(
        "--match-thresh",
        type=float,
        default=0.8,
        help="ByteTrack IoU threshold for matching (default: 0.8)",
    )
    parser.add_argument(
        "--track-buffer",
        type=int,
        default=30,
        help="ByteTrack frames to keep lost tracks alive (default: 30)",
    )
    parser.add_argument(
        "--cleanup-timeout",
        type=float,
        default=300.0,
        help="Seconds before removing idle tracker (default: 300s)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting cloud tracker service")
    logger.info(f"Configuration: device_id={args.device_id}, "
                f"broker={args.broker_host}:{args.broker_port}")

    service = TrackerService(
        device_id=args.device_id,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        username=args.username,
        password=args.password,
        track_thresh=args.track_thresh,
        match_thresh=args.match_thresh,
        track_buffer=args.track_buffer,
        tracker_cleanup_timeout_s=args.cleanup_timeout,
    )

    try:
        service.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
        service.stop()


if __name__ == "__main__":
    main()
