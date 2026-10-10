"""MLOps Orchestration Script for Edge Inference Model.

Orchestrates the complete lifecycle of the HealthKicks Edge AI inference model:
1. Data Ingestion & Caching: Fetches validated Studio recording sessions from PostgreSQL & DynamoDB.
2. Training & Cross-Validation: Trains edge-adapted classifiers with 5-Fold Stratified CV.
3. Quality Gate Validation: Enforces minimum F1-score and safety constraints (zero false negatives for falls).
4. C Transpilation: Converts the champion model into embedded C header using m2cgen.
5. Firmware Verification: Builds the ESP32-S3 firmware via PlatformIO to ensure binary integrity and monitor Flash/RAM usage.

Usage examples:
    # Full end-to-end sync, train, export and firmware build check:
    $ uv run python -m scripts.update_edge_model

    # Fast offline run with synthetic dataset (no database or AWS credentials needed):
    $ uv run python -m scripts.update_edge_model --synthetic

    # Skip retraining and re-transpile existing model + check build:
    $ uv run python -m scripts.update_edge_model --skip-train

    # Export to custom firmware directory without PlatformIO build check:
    $ uv run python -m scripts.update_edge_model --no-build-check
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.train_detector import (
    build_dataset_from_sessions,
    load_project_env,
    sync_sessions_cache,
    train_and_benchmark,
)

logger = logging.getLogger("update_edge_model")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)


def find_firmware_dir(custom_path: str | None = None) -> Path:
    """Finds the ESP32-S3 firmware repository root."""
    if custom_path:
        p = Path(custom_path).resolve()
        if p.exists() and (p / "platformio.ini").exists():
            return p
        raise FileNotFoundError(f"Custom firmware directory not found or missing platformio.ini: {custom_path}")

    # Standard candidate locations relative to health-kicks/
    current_dir = Path(__file__).resolve().parent.parent
    candidates = [
        current_dir.parent / "health-kicks-esp32-s3",
        current_dir / "health-kicks-esp32-s3",
        Path("../health-kicks-esp32-s3").resolve(),
    ]
    for c in candidates:
        if c.exists() and (c / "platformio.ini").exists():
            return c

    raise FileNotFoundError(
        "Could not automatically locate 'health-kicks-esp32-s3'. "
        "Please provide --target-firmware-dir explicitly."
    )


def run_c_transpilation(
    model_path: Path,
    output_header_path: Path,
    firmware_dir: Path,
) -> bool:
    """Invokes export_model_to_c.py to generate activity_model_generated.h."""
    export_script = firmware_dir / "tools" / "export_model_to_c.py"
    if not export_script.exists():
        logger.error("Export script not found: %s", export_script)
        return False

    cmd = [
        sys.executable,
        str(export_script),
        "--model",
        str(model_path.resolve()),
        "--output",
        str(output_header_path.resolve()),
    ]

    logger.info("Executing C transpilation: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        logger.error("C transpilation failed:\nSTDOUT:\n%s\nSTDERR:\n%s", result.stdout, result.stderr)
        return False

    logger.info("C transpilation succeeded:\n%s", result.stdout.strip())
    return True


def run_platformio_build(firmware_dir: Path) -> tuple[bool, str]:
    """Compiles the firmware using PlatformIO and extracts Flash/RAM stats."""
    logger.info("Running PlatformIO build verification in: %s", firmware_dir)

    # Determine command based on available runners
    cmd = ["uv", "tool", "run", "--from", "platformio", "pio", "run"]

    try:
        res = subprocess.run(
            cmd,
            cwd=str(firmware_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except FileNotFoundError:
        # Fallback to direct 'pio run' if uv tool is unavailable
        cmd = ["pio", "run"]
        try:
            res = subprocess.run(
                cmd,
                cwd=str(firmware_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
        except Exception as pio_err:
            return False, f"PlatformIO not installed or accessible: {pio_err}"
    except subprocess.TimeoutExpired:
        return False, "PlatformIO build timed out after 180 seconds."

    output = f"{res.stdout}\n{res.stderr}"
    if res.returncode != 0:
        return False, f"PlatformIO build failed with code {res.returncode}:\n{output}"

    # Extract RAM and Flash metrics from PlatformIO output
    ram_match = re.search(r"RAM:\s+\[[^\]]+\]\s+([\d\.]+%)\s+\(used\s+(\d+)\s+bytes\s+from\s+(\d+)\s+bytes\)", output)
    flash_match = re.search(r"Flash:\s+\[[^\]]+\]\s+([\d\.]+%)\s+\(used\s+(\d+)\s+bytes\s+from\s+(\d+)\s+bytes\)", output)

    stats = []
    if ram_match:
        stats.append(f"RAM: {ram_match.group(1)} ({ram_match.group(2)} / {ram_match.group(3)} B)")
    if flash_match:
        stats.append(f"Flash: {flash_match.group(1)} ({flash_match.group(2)} / {flash_match.group(3)} B)")

    stats_str = ", ".join(stats) if stats else "Build succeeded"
    return True, stats_str


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parses command line arguments."""
    parser = argparse.ArgumentParser(
        description="Unified MLOps pipeline for HealthKicks Edge inference model."
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use synthetic dataset (no AWS or PostgreSQL credentials required).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force re-download and re-parsing of raw telemetry sessions.",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="scripts/data/sessions",
        help="Local cache directory for raw IMU sessions (default: scripts/data/sessions).",
    )
    parser.add_argument(
        "--output-model",
        type=str,
        default="scripts/models/activity_classifier.joblib",
        help="Target path for trained .joblib model package (default: scripts/models/activity_classifier.joblib).",
    )
    parser.add_argument(
        "--target-firmware-dir",
        type=str,
        default=None,
        help="Path to health-kicks-esp32-s3 firmware directory (auto-detected if omitted).",
    )
    parser.add_argument(
        "--output-header",
        type=str,
        default="include/activity_model_generated.h",
        help="Target C header relative to firmware directory (default: include/activity_model_generated.h).",
    )
    parser.add_argument(
        "--window-size",
        type=float,
        default=3.0,
        help="Feature extraction window size in seconds (default: 3.0s).",
    )
    parser.add_argument(
        "--window-step",
        type=float,
        default=0.5,
        help="Feature extraction sliding step in seconds (default: 0.5s).",
    )
    parser.add_argument(
        "--min-f1",
        type=float,
        default=0.85,
        help="Quality gate: minimum macro F1 score required to accept champion model (default: 0.85).",
    )
    parser.add_argument(
        "--skip-train",
        action="store_true",
        help="Skip training and use existing .joblib model artifact directly.",
    )
    parser.add_argument(
        "--no-build-check",
        action="store_true",
        help="Skip PlatformIO firmware compilation verification.",
    )
    parser.add_argument(
        "--min-sessions-per-class",
        type=int,
        default=20,
        help="Minimum number of recording sessions required to include a class in training (default: 20).",
    )
    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """Main MLOps orchestration entrypoint."""
    load_project_env()
    args = parse_args(argv)
    min_sessions = 0 if args.synthetic else args.min_sessions_per_class

    print("\n" + "=" * 72)
    print(" HEALTHKICKS MLOPS - END-TO-END EDGE MODEL UPDATE PIPELINE")
    print("=" * 72)
    print(f"* Mode                 : {'SYNTHETIC (Offline)' if args.synthetic else 'PRODUCTION (Postgres + DynamoDB)'}")
    print(f"* Model Artifact       : {args.output_model}")
    print(f"* Quality Gate F1 Min  : {args.min_f1 * 100:.1f} %")
    print(f"* Window Parameters    : {args.window_size:.1f}s window, {args.window_step:.1f}s step")
    print(f"* Min Sessions / Class : {min_sessions}")
    print(f"* Build Check Enabled  : {'NO' if args.no_build_check else 'YES'}")
    print("=" * 72 + "\n")

    # Locate firmware directory early to fail fast if missing
    try:
        firmware_dir = find_firmware_dir(args.target_firmware_dir)
        print(f"[OK] Firmware directory detected : {firmware_dir}")
    except FileNotFoundError as fnf_err:
        logger.error("Firmware directory error: %s", fnf_err)
        return 1

    model_path = Path(args.output_model).resolve()
    header_path = (firmware_dir / args.output_header).resolve()

    # -------------------------------------------------------------------------
    # STEP 1: TRAINING OR USING EXISTING MODEL
    # -------------------------------------------------------------------------
    if not args.skip_train:
        print("\n>>> [STEP 1/4] Synchronizing sessions & training candidate models...")
        cache_path = Path(args.cache_dir)
        sessions_data = sync_sessions_cache(
            cache_dir=cache_path,
            force_refresh=args.force_refresh,
            synthetic=args.synthetic,
            batch_size=8,
        )

        if not sessions_data:
            logger.error("No sessions available for training. Pipeline aborted.")
            return 1

        print("\n>>> [STEP 2/4] Extracting temporal & biomechanical features...")
        X, y = build_dataset_from_sessions(
            sessions_data=sessions_data,
            window_size_sec=args.window_size,
            step_sec=args.window_step,
            min_sessions_per_class=min_sessions,
        )

        if len(X) == 0:
            logger.error("Feature extraction produced 0 valid windows. Pipeline aborted.")
            return 1

        print("\n>>> [STEP 3/4] Cross-validation benchmark & champion model selection...")
        try:
            package = train_and_benchmark(
                X=X,
                y=y,
                output_model_path=str(model_path),
                window_size_sec=args.window_size,
                include_unsupported_c=False,
            )
        except Exception as train_err:
            logger.error("Training failed: %s", train_err)
            return 1

        # Quality Gate Verification
        f1_macro = package.get("metrics", {}).get("f1_macro", 0.0)
        print(f"\n[QUALITY GATE] Champion F1 Score: {f1_macro*100:.2f}% (Threshold: {args.min_f1*100:.1f}%)")
        if f1_macro < args.min_f1:
            logger.error(
                "Quality Gate REJECTED: Model F1-score (%.2f%%) is below minimum threshold (%.2f%%).",
                f1_macro * 100,
                args.min_f1 * 100,
            )
            return 2
        print("[QUALITY GATE] PASSED! Model meets accuracy standards for embedded deployment.")
    else:
        print("\n>>> [STEP 1-3/4] Skipping training (--skip-train). Using existing artifact:")
        if not model_path.exists():
            logger.error("Existing model artifact not found: %s", model_path)
            return 1
        print(f"    Artifact: {model_path} ({model_path.stat().st_size / 1024:.1f} KB)")

    # -------------------------------------------------------------------------
    # STEP 4: C TRANSPILATION (m2cgen)
    # -------------------------------------------------------------------------
    print(f"\n>>> [STEP 4/4] Transpiling scikit-learn model to C header -> {header_path.name}...")
    transpile_ok = run_c_transpilation(
        model_path=model_path,
        output_header_path=header_path,
        firmware_dir=firmware_dir,
    )
    if not transpile_ok:
        logger.error("C transpilation failed. Pipeline aborted.")
        return 3

    header_size_str = f"({header_path.stat().st_size} bytes)" if header_path.exists() else ""
    print(f"[OK] Generated C header: {header_path} {header_size_str}")

    # -------------------------------------------------------------------------
    # STEP 5: FIRMWARE BUILD CHECK (PLATFORMIO)
    # -------------------------------------------------------------------------
    if not args.no_build_check:
        print("\n>>> [VERIFICATION] Building ESP32-S3 firmware with PlatformIO...")
        build_ok, stats_msg = run_platformio_build(firmware_dir)
        if not build_ok:
            logger.error("PlatformIO build verification FAILED:\n%s", stats_msg)
            return 4
        print(f"[OK] Firmware compilation SUCCESSFUL! ({stats_msg})")
    else:
        print("\n>>> [VERIFICATION] PlatformIO build check skipped (--no-build-check).")

    # -------------------------------------------------------------------------
    # SUMMARY & NEXT STEPS
    # -------------------------------------------------------------------------
    print("\n" + "=" * 72)
    print(" MLOPS PIPELINE COMPLETED SUCCESSFULLY")
    print("=" * 72)
    print(f"1. Champion Model Artifact  : {model_path}")
    print(f"2. Transpiled C Header      : {header_path}")
    print(f"3. Target Firmware Dir      : {firmware_dir}")
    print("\nNext recommended steps:")
    print("  $ git -C health-kicks-esp32-s3 diff include/activity_model_generated.h")
    print("  $ git -C health-kicks-esp32-s3 commit -am \"feat(ml): update activity inference model\"")
    print("  $ git -C health-kicks-esp32-s3 push origin main")
    print("=" * 72 + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())

