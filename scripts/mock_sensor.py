"""Publish normal and anomalous IMU data to the MQTT broker."""

import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt


def build_sample(elapsed: float, anomaly: bool) -> dict[str, float]:
    """Build one synthetic walking or heavy-impact IMU sample."""
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
    """Run the IMU simulator until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--anomaly-interval", type=float, default=15.0)
    parser.add_argument("--sample-interval", type=float, default=0.05)
    args = parser.parse_args()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="Mock_Arduino_IMU",
    )
    client.connect_async(args.broker, args.port, 60)
    client.loop_start()
    print("Démarrage du simulateur d'IMU. Ctrl+C pour arrêter.")

    started_at = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started_at
            anomaly = int(elapsed / args.anomaly_interval) % 2 == 1
            data = build_sample(elapsed, anomaly)
            client.publish("chaussure/imu/telemetry", json.dumps(data))
            print(
                f"{'ANOMALIE' if anomaly else 'MARCHE NORMALE'}: {data}",
                end="\r",
                flush=True,
            )
            time.sleep(args.sample_interval)
    except KeyboardInterrupt:
        print("\nArrêt du simulateur.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
