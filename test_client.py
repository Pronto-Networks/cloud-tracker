#!/usr/bin/env python3
"""
Test utility for the cloud tracker service.

Publishes sample detection messages and subscribes to tracked results.
Useful for local testing without a real edge device.
"""

import argparse
import json
import time
from typing import Optional

import paho.mqtt.client as mqtt


def create_sample_detection(camera_id: int, frame_count: int, num_detections: int = 2) -> dict:
    """Generate a sample detection message."""
    detections = [
        {
            "class": "person",
            "class_id": 1,
            "confidence": 0.85 + (i * 0.05),
            "bbox": [0.1 + (i * 0.3), 0.2, 0.4 + (i * 0.3), 0.8],
            "roi-id": "",
        }
        for i in range(num_detections)
    ]

    return {
        "camera_id": camera_id,
        "frame_count": frame_count,
        "pts": frame_count * 33333,  # ~30fps
        "timestamp": int(time.time() * 1000),
        "inference_time_ms": 20.5,
        "image_width": 640,
        "image_height": 480,
        "detection_count": len(detections),
        "detections": detections,
        "image_base64": "",
    }


class TestClient:
    """Test MQTT client for tracker service."""

    def __init__(
        self,
        device_id: str,
        broker_host: str,
        broker_port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.device_id = device_id
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        if username and password:
            self.client.username_pw_set(username, password)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"✓ Connected to {self.broker_host}:{self.broker_port}")
            # Subscribe to tracked results for all cameras
            topic = f"edge/{self.device_id}/data/tracked_detections/+"
            print(f"✓ Subscribing to: {topic}")
            client.subscribe(topic, qos=0)
        else:
            print(f"✗ Connection failed with code {rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            print(f"\n📬 Tracked detections received on: {msg.topic}")
            print(f"   Frame: {payload.get('frame_count')}, "
                  f"Detections: {payload.get('detection_count')}")

            for idx, det in enumerate(payload.get("detections", [])):
                track_id = det.get("track-id", "?")
                cls_name = det.get("class", "?")
                conf = det.get("confidence", 0)
                print(f"     [{idx}] track_id={track_id} class={cls_name} conf={conf:.2f}")
        except Exception as e:
            print(f"✗ Error parsing message: {e}")

    def connect(self):
        """Connect to MQTT broker."""
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            print(f"✗ Failed to connect: {e}")
            raise

    def publish_detection(self, camera_id: int, frame_count: int, num_detections: int = 2):
        """Publish a sample detection message."""
        topic = f"edge/{self.device_id}/data/detections/{camera_id}"
        payload = create_sample_detection(camera_id, frame_count, num_detections)

        try:
            result = self.client.publish(topic, json.dumps(payload), qos=0)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                print(f"✓ Published detection to {topic} "
                      f"(frame={frame_count}, detections={num_detections})")
            else:
                print(f"✗ Publish failed: {mqtt.error_string(result.rc)}")
        except Exception as e:
            print(f"✗ Error publishing: {e}")

    def disconnect(self):
        """Disconnect from MQTT broker."""
        self.client.loop_stop()
        self.client.disconnect()
        print("✓ Disconnected")


def main():
    parser = argparse.ArgumentParser(
        description="Test utility for cloud tracker service"
    )
    parser.add_argument(
        "--device-id",
        default="edge-test",
        help="Device ID (default: edge-test)",
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
        help="MQTT username (optional)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="MQTT password (optional)",
    )
    parser.add_argument(
        "--camera-ids",
        type=int,
        nargs="+",
        default=[1, 2],
        help="Camera IDs to test (default: 1 2)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=10,
        help="Number of frames to simulate (default: 10)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Interval between frames in seconds (default: 1.0)",
    )

    args = parser.parse_args()

    print("🚀 Cloud Tracker Test Client")
    print(f"   Device ID: {args.device_id}")
    print(f"   Broker: {args.broker_host}:{args.broker_port}")
    print(f"   Cameras: {args.camera_ids}")
    print(f"   Frames per camera: {args.frames}")
    print()

    try:
        client = TestClient(
            device_id=args.device_id,
            broker_host=args.broker_host,
            broker_port=args.broker_port,
            username=args.username,
            password=args.password,
        )
        client.connect()

        # Give tracker time to start
        print("⏳ Waiting for tracker to connect...")
        time.sleep(2)

        # Simulate frames
        for frame_count in range(1, args.frames + 1):
            for camera_id in args.camera_ids:
                # Vary number of detections to test tracker behavior
                num_dets = 1 if frame_count % 3 == 0 else 2
                client.publish_detection(camera_id, frame_count, num_dets)

            if frame_count < args.frames:
                time.sleep(args.interval)

        print("\n✓ All frames published")
        print("⏳ Waiting for tracked results...")
        time.sleep(2)

        print("\n✓ Test complete")
        client.disconnect()

    except KeyboardInterrupt:
        print("\n⏸ Test interrupted")
        client.disconnect()
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        exit(1)


if __name__ == "__main__":
    main()
