"""Publish simulated IMU telemetry directly to AWS IoT Core over mTLS."""

import argparse
import json
import math
import random
import time

from app.core.aws_iot_client import AWSIoTClient
from app.core.config import load_settings


def build_sample(elapsed: float, anomaly: bool) -> dict[str, float]:
    """Build one normal walking or anomalous IMU sample."""
    base_z = 1.0 + math.sin(elapsed * 3) * 0.4
    if anomaly:
        ax = random.uniform(-1.5, 1.5)
        ay = random.uniform(-1.5, 1.5)
        az = base_z + random.uniform(1.2, 3.5)
    else:
        ax = math.sin(elapsed) * 0.2
        ay = math.cos(elapsed) * 0.2
        az = base_z
    return {
        "ax": round(ax, 2),
        "ay": round(ay, 2),
        "az": round(az, 2),
        "gx": round(random.uniform(-10, 10), 1),
        "gy": round(random.uniform(-10, 10), 1),
        "gz": round(random.uniform(-10, 10), 1),
        "timestamp": time.time(),
    }


def main() -> None:
    """Connect with mTLS and publish samples until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anomaly-interval", type=float, default=15.0)
    parser.add_argument("--sample-interval", type=float, default=0.05)
    args = parser.parse_args()

    config = load_settings()
    client = AWSIoTClient(config)
    client.start()
    print("AWS IoT IMU simulator started. Press Ctrl+C to stop.")
    started_at = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started_at
            anomaly = int(elapsed / args.anomaly_interval) % 2 == 1
            client.publish(
                config.aws_iot_telemetry_topic,
                json.dumps(build_sample(elapsed, anomaly)),
            )
            print(
                f"{'ANOMALY' if anomaly else 'NORMAL'} telemetry",
                end="\r",
                flush=True,
            )
            time.sleep(args.sample_interval)
    except KeyboardInterrupt:
        print("\nAWS IoT simulator stopped.")
    finally:
        client.stop()


if __name__ == "__main__":
    main()
