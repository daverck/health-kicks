"""Local ML training script for Edge activity classifier (HealthKicks).

This script connects to project databases (Aurora PostgreSQL and DynamoDB) to
retrieve validated Studio recording sessions, maintains an incremental local
cache to minimize network calls, extracts biomechanical and temporal features
from the IMU across sliding windows (duration: --window-size, default 3.0s;
step/overlap: --window-step, default 0.5s), benchmarks several Edge-adapted
classifiers via cross-validation (Stratified 5-Fold), and serializes the
champion model as a joblib artifact.

Windowing parameters:
    - window-size: duration of raw signal accumulation for feature extraction (3.0s).
    - window-step: temporal advance / stride between successive sliding windows (0.5s).

By default, candidate models are strictly constrained to m2cgen-compatible
estimators (RandomForest, ExtraTrees, LogisticRegression) with depths tailored
for ESP32-S3 Flash memory footprint.

Prerequisites:
    1. Data science dependencies are isolated in the optional 'ml' group:
       $ uv sync --group ml

    2. AWS CLI authentication for access to real DynamoDB telemetry data:
       $ aws sso login    # (or 'aws login' / 'aws configure')
       Note: '--synthetic' flag allows skipping AWS for offline training.

Usage examples:
    # Standard training with m2cgen-compatible models by default:
    $ uv run python -m scripts.train_detector

    # Include non-C transpilable models (e.g., HistGradientBoosting) for research:
    $ uv run python -m scripts.train_detector --include-unsupported-c

    # Batch download with custom concurrency (e.g., 12 workers):
    $ uv run python -m scripts.train_detector --batch-size 12

    # Force complete resynchronization from PostgreSQL & DynamoDB:
    $ uv run python -m scripts.train_detector --force-refresh

    # Development / offline demo mode with synthetic dataset (no AWS access needed):
    $ uv run python -m scripts.train_detector --synthetic
"""
from pathlib import Path

from dotenv import load_dotenv


def load_project_env() -> None:
    """Loads project .env file without overriding already defined variables."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

import argparse
import json
import logging
import sys
import warnings
from datetime import UTC, datetime
from typing import Any

import joblib
import numpy as np
import pandas as pd
from app.schemas.studio import StudioSessionDatasetRecord
from sklearn.ensemble import (
    ExtraTreesClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.exceptions import UndefinedMetricWarning
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_validate
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("train_detector")


# -----------------------------------------------------------------------------
# 1. BIOMECHANICAL IMU FEATURE EXTRACTION
# -----------------------------------------------------------------------------
def compute_window_features(df_window: pd.DataFrame) -> dict[str, float]:
    """Computes statistical and physical metrics over an IMU window.

    Expected columns: ax, ay, az, gx, gy, gz.
    Accelerations in m/s² or g, angular velocities in rad/s or deg/s.
    """
    ax = df_window["ax"].to_numpy(dtype=float)
    ay = df_window["ay"].to_numpy(dtype=float)
    az = df_window["az"].to_numpy(dtype=float)
    gx = df_window["gx"].to_numpy(dtype=float)
    gy = df_window["gy"].to_numpy(dtype=float)
    gz = df_window["gz"].to_numpy(dtype=float)

    # Euclidean norms (invariant to spatial shoe orientation)
    acc_mag = np.sqrt(ax**2 + ay**2 + az**2)
    gyro_mag = np.sqrt(gx**2 + gy**2 + gz**2)

    n_samples = max(len(acc_mag), 1)

    features: dict[str, float] = {
        # Acceleration - Magnitude (Fall signature: weightlessness trough + impact peak)
        "acc_mag_max": float(np.max(acc_mag)),
        "acc_mag_min": float(np.min(acc_mag)),
        "acc_mag_mean": float(np.mean(acc_mag)),
        "acc_mag_std": float(np.std(acc_mag)),
        "acc_mag_peak_to_peak": float(np.max(acc_mag) - np.min(acc_mag)),
        # Gyroscope - Magnitude (Sudden limb segment rotation)
        "gyro_mag_max": float(np.max(gyro_mag)),
        "gyro_mag_mean": float(np.mean(gyro_mag)),
        "gyro_mag_std": float(np.std(gyro_mag)),
        # Individual axis standard deviations (tri-axial dispersion)
        "ax_std": float(np.std(ax)),
        "ay_std": float(np.std(ay)),
        "az_std": float(np.std(az)),
        "gx_std": float(np.std(gx)),
        "gy_std": float(np.std(gy)),
        "gz_std": float(np.std(gz)),
        # Approximate kinetic / dynamic energy
        "acc_energy": float(np.sum(acc_mag**2) / n_samples),
        "gyro_energy": float(np.sum(gyro_mag**2) / n_samples),
        # Individual axis means (static posture discrimination: idle vs lying_supine vs lying_prone)
        "ax_mean": float(np.mean(ax)),
        "ay_mean": float(np.mean(ay)),
        "az_mean": float(np.mean(az)),
    }
    return features


DEFAULT_EXCLUDED_LABELS: set[str] = {"stairs"}
PUNCTUAL_EVENT_LABELS: set[str] = {"stumble_recover", "sit_down", "stand_up", "jump"}


def is_punctual_event(label: str) -> bool:
    """Returns True if the label represents a discrete, punctual transition event (falls, stumbles)."""
    return label.startswith("fall_") or label in ("fall", "stumble_recover") or label in PUNCTUAL_EVENT_LABELS


def locate_dynamic_event_peak(df: pd.DataFrame) -> int | None:
    """Locates the sample index of the dynamic impact/transition in a punctual event session.

    Uses combined acceleration deviation from gravity and angular velocity magnitude.
    Returns None if the signal is uniform/stationary without a distinct dynamic peak.
    """
    if len(df) < 5:
        return None
    ax = df["ax"].to_numpy(dtype=float)
    ay = df["ay"].to_numpy(dtype=float)
    az = df["az"].to_numpy(dtype=float)
    gx = df["gx"].to_numpy(dtype=float)
    gy = df["gy"].to_numpy(dtype=float)
    gz = df["gz"].to_numpy(dtype=float)

    acc_mag = np.sqrt(ax**2 + ay**2 + az**2)
    gyro_mag = np.sqrt(gx**2 + gy**2 + gz**2)

    g_ref = 9.81 if float(np.median(acc_mag)) > 5.0 else 1.0
    gyro_norm = 100.0 if float(np.median(gyro_mag)) > 10.0 else 2.0
    metric = np.abs(acc_mag - g_ref) + (gyro_mag / gyro_norm)

    if (np.max(metric) - np.min(metric)) < 0.15:
        return None

    return int(np.argmax(metric))


def build_dataset_from_sessions(
    sessions_data: list[dict[str, Any]],
    window_size_sec: float = 2.0,
    step_sec: float = 0.5,
    min_sessions_per_class: int = 0,
    max_sessions_per_class: int | None = None,
    excluded_labels: set[str] | None = None,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Splits each session into sliding windows and extracts features.

    Filters out sessions with excluded labels (e.g. legacy 'stairs') and classes
    with fewer than min_sessions_per_class sessions. For punctual events (falls,
    stumble_recover), uses event-centric windowing to reject idle post-fall tails.
    Supports capping to the N most recent sessions per class via max_sessions_per_class.
    """
    if excluded_labels is None:
        excluded_labels = DEFAULT_EXCLUDED_LABELS

    X_rows: list[dict[str, float]] = []
    y_labels: list[str] = []

    # Pre-filter sessions by class count if required
    class_counts: dict[str, int] = {}
    for s in sessions_data:
        lbl = s.get("label")
        if lbl and lbl not in excluded_labels:
            class_counts[lbl] = class_counts.get(lbl, 0) + 1

    candidate_sessions: list[dict[str, Any]] = []
    for session in sessions_data:
        label = session.get("label")
        if not label or label in excluded_labels:
            continue
        if min_sessions_per_class > 0 and class_counts.get(label, 0) < min_sessions_per_class:
            continue
        readings = session.get("readings", [])
        if len(readings) < 5:
            continue
        candidate_sessions.append(session)

    # Optional recency filtering: limit to N most recent sessions per class
    valid_sessions: list[dict[str, Any]] = []
    if max_sessions_per_class is not None and max_sessions_per_class > 0:
        by_class: dict[str, list[dict[str, Any]]] = {}
        for s in candidate_sessions:
            by_class.setdefault(str(s["label"]), []).append(s)

        for lbl, class_sess in by_class.items():
            def _get_ts(sess: dict[str, Any]) -> float:
                rds = sess.get("readings", [])
                if rds:
                    return float(rds[-1].get("timestamp", 0) or rds[0].get("timestamp", 0))
                return 0.0

            class_sess.sort(key=_get_ts, reverse=True)
            kept = class_sess[:max_sessions_per_class]
            if len(class_sess) > max_sessions_per_class:
                logger.info(
                    "Capped class '%s' from %d to %d most recent sessions.",
                    lbl,
                    len(class_sess),
                    max_sessions_per_class,
                )
            valid_sessions.extend(kept)
    else:
        valid_sessions = candidate_sessions

    for session in valid_sessions:
        label = session.get("label")
        readings = session.get("readings", [])

        df = pd.DataFrame(readings)

        # Check required IMU columns
        required_cols = {"ax", "ay", "az", "gx", "gy", "gz"}
        if not required_cols.issubset(df.columns):
            continue

        # Chronological sort if timestamp available
        ts_col = None
        for candidate in ["timestamp", "timestamp_epoch_us", "ts"]:
            if candidate in df.columns:
                ts_col = candidate
                break

        if ts_col:
            df = df.sort_values(ts_col).reset_index(drop=True)
            ts_vals = df[ts_col].to_numpy(dtype=float)
            delta_ts = ts_vals[-1] - ts_vals[0]

            t0 = ts_vals[0]
            if t0 > 1e14 or delta_ts > 100_000:
                scale = 1e6  # Microseconds
            elif t0 > 1e11 or delta_ts > 100:
                scale = 1e3  # Milliseconds
            elif delta_ts > 0:
                scale = 1.0  # Seconds
            else:
                scale = None
        else:
            scale = None

        total_samples = len(df)
        windows_extracted = 0

        # Detect dynamic event peak for punctual events (falls, stumble_recover)
        punctual = is_punctual_event(label)
        peak_idx = locate_dynamic_event_peak(df) if punctual else None

        if scale and scale > 0 and (delta_ts / scale) >= window_size_sec:
            t_sec = (ts_vals - ts_vals[0]) / scale
            max_t = t_sec[-1]
            curr_t = 0.0
            t_peak = float(t_sec[peak_idx]) if peak_idx is not None else None
            last_extracted_start = -1.0

            while curr_t + window_size_sec <= max_t + 1e-5:
                # Event-centric windowing: reject windows that don't cover the dynamic event
                if t_peak is not None:
                    is_event_window = (curr_t <= t_peak <= curr_t + window_size_sec)
                    if not is_event_window:
                        curr_t += step_sec
                        continue

                window_mask = (t_sec >= curr_t) & (t_sec <= curr_t + window_size_sec)
                window_df = df.loc[window_mask]
                if len(window_df) >= 5:
                    feat = compute_window_features(window_df)
                    X_rows.append(feat)
                    y_labels.append(label)
                    windows_extracted += 1
                    last_extracted_start = curr_t
                curr_t += step_sec

            # Tail-window alignment: capture final window ending at max_t if not covered
            tail_start_t = max_t - window_size_sec
            if tail_start_t > 0 and (last_extracted_start < 0 or (tail_start_t - last_extracted_start) >= 0.1):
                if t_peak is None or (tail_start_t <= t_peak <= max_t):
                    tail_mask = (t_sec >= tail_start_t) & (t_sec <= max_t)
                    tail_df = df.loc[tail_mask]
                    if len(tail_df) >= 5:
                        feat = compute_window_features(tail_df)
                        X_rows.append(feat)
                        y_labels.append(label)
                        windows_extracted += 1
        else:
            duration_hint = float(session.get("duration_sec", 5.0) or 5.0)
            samples_per_sec = max(total_samples / max(duration_hint, 0.5), 10.0)
            window_samples = max(int(window_size_sec * samples_per_sec), 5)
            step_samples = max(int(step_sec * samples_per_sec), 1)
            last_extracted_start_idx = -1

            for start in range(0, total_samples - window_samples + 1, step_samples):
                if peak_idx is not None:
                    is_event_window = (start <= peak_idx <= start + window_samples)
                    if not is_event_window:
                        continue

                window_df = df.iloc[start : start + window_samples]
                feat = compute_window_features(window_df)
                X_rows.append(feat)
                y_labels.append(label)
                windows_extracted += 1
                last_extracted_start_idx = start

            # Tail-window alignment (index-based)
            if total_samples >= window_samples:
                tail_start_idx = total_samples - window_samples
                min_diff_samples = max(int(0.1 * samples_per_sec), 1)
                if tail_start_idx > 0 and (last_extracted_start_idx < 0 or (tail_start_idx - last_extracted_start_idx) >= min_diff_samples):
                    if peak_idx is None or (tail_start_idx <= peak_idx <= total_samples):
                        window_df = df.iloc[tail_start_idx : total_samples]
                        feat = compute_window_features(window_df)
                        X_rows.append(feat)
                        y_labels.append(label)
                        windows_extracted += 1

        if windows_extracted == 0 and total_samples >= 5:
            feat = compute_window_features(df)
            X_rows.append(feat)
            y_labels.append(label)

    if not X_rows:
        return pd.DataFrame(), np.array([])

    return pd.DataFrame(X_rows), np.array(y_labels)


# -----------------------------------------------------------------------------
# 2. INCREMENTAL LOCAL CACHE — .NPZ STORAGE PER SESSION
# -----------------------------------------------------------------------------
_SIGNAL_COLS = ["ax", "ay", "az", "gx", "gy", "gz"]


def save_session_npz(cache_dir: Path, session_dict: dict[str, Any]) -> Path:
    """Serializes an IMU session into a compressed NumPy file (.npz)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    sess_id = session_dict["session_id"]
    readings = session_dict.get("readings", [])

    n = len(readings)
    signals = np.zeros((n, 6), dtype=np.float32)
    timestamps = np.zeros(n, dtype=np.int64)
    for i, r in enumerate(readings):
        signals[i, 0] = float(r.get("ax", 0.0))
        signals[i, 1] = float(r.get("ay", 0.0))
        signals[i, 2] = float(r.get("az", 0.0))
        signals[i, 3] = float(r.get("gx", 0.0))
        signals[i, 4] = float(r.get("gy", 0.0))
        signals[i, 5] = float(r.get("gz", 0.0))
        timestamps[i] = int(r.get("timestamp", 0))

    filepath = cache_dir / f"{sess_id}.npz"
    np.savez_compressed(
        filepath,
        signals=signals,
        timestamps=timestamps,
        session_id=np.array(sess_id),
        device_id=np.array(session_dict.get("device_id", "")),
        label=np.array(session_dict.get("label", "")),
        duration_sec=np.array(float(session_dict.get("duration_sec") or 0.0)),
        sample_count=np.array(int(session_dict.get("sample_count", n))),
        source_duration_sec=np.array(
            float(session_dict.get("source_duration_sec"))
            if session_dict.get("source_duration_sec") is not None
            else np.nan
        ),
        source_sample_count=np.array(
            int(session_dict.get("source_sample_count"))
            if session_dict.get("source_sample_count") is not None
            else -1
        ),
    )
    return filepath


def load_session_npz(filepath: Path) -> dict[str, Any] | None:
    """Loads an IMU session .npz file and converts it into an internal dictionary."""
    if not filepath.exists():
        return None
    try:
        data = np.load(filepath, allow_pickle=False)
        signals: np.ndarray = data["signals"]
        timestamps: np.ndarray = data["timestamps"]
        n = len(timestamps)

        readings = []
        for i in range(n):
            readings.append({
                "timestamp": int(timestamps[i]),
                "ax": float(signals[i, 0]),
                "ay": float(signals[i, 1]),
                "az": float(signals[i, 2]),
                "gx": float(signals[i, 3]),
                "gy": float(signals[i, 4]),
                "gz": float(signals[i, 5]),
            })

        source_duration_sec = (
            float(data["source_duration_sec"])
            if "source_duration_sec" in data.files
            and np.isfinite(float(data["source_duration_sec"]))
            else None
        )
        source_sample_count = (
            int(data["source_sample_count"])
            if "source_sample_count" in data.files and int(data["source_sample_count"]) >= 0
            else None
        )
        record = StudioSessionDatasetRecord(
            session_id=str(data["session_id"]),
            device_id=str(data["device_id"]),
            label=str(data["label"]),
            duration_sec=float(data["duration_sec"]),
            sample_count=int(data["sample_count"]),
            source_duration_sec=source_duration_sec,
            source_sample_count=source_sample_count,
            readings=readings,
        )
        return record.model_dump()
    except Exception as err:
        logger.warning("Corrupted or unreadable .npz file (%s): %s", filepath, err)
        return None


def migrate_json_to_npz(json_path: Path, cache_dir: Path) -> int:
    """Migrates legacy monolithic JSON cache to per-session .npz files."""
    if not json_path.exists():
        return 0

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as err:
        logger.warning("JSON->NPZ Migration: failed to read %s: %s", json_path, err)
        return 0

    if isinstance(data, dict) and "sessions" in data:
        sessions_raw = data["sessions"].values()
    elif isinstance(data, list):
        sessions_raw = data
    else:
        logger.warning("JSON->NPZ Migration: unrecognized JSON format in %s", json_path)
        return 0

    migrated = 0
    for sess in sessions_raw:
        if not isinstance(sess, dict) or "session_id" not in sess:
            continue
        try:
            save_session_npz(cache_dir, sess)
            migrated += 1
        except Exception as err:
            logger.warning(
                "JSON->NPZ Migration: failed for session %s: %s",
                sess.get("session_id"),
                err,
            )

    if migrated > 0:
        bak_path = json_path.with_suffix(".json.bak")
        try:
            json_path.rename(bak_path)
            logger.info(
                "Migration completed: %d session(s) converted to .npz. Old JSON archived under: %s",
                migrated,
                bak_path,
            )
        except Exception:
            logger.info("Migration completed: %d session(s) converted to .npz.", migrated)

    return migrated


def _get_sess_id(sess: Any) -> str:
    """Extracts session ID from a StudioSession model or mock."""
    val_uuid = getattr(sess, "uuid", None)
    val_id = getattr(sess, "id", None)
    # If uuid is a MagicMock but id is a string/int, prefer id
    if val_uuid is not None and type(val_uuid).__name__ != "MagicMock":
        return str(val_uuid)
    if val_id is not None and type(val_id).__name__ != "MagicMock":
        return str(val_id)
    if val_uuid is not None:
        return str(val_uuid)
    return str(val_id or "")

def _fetch_single_session(
    sess: Any,
    telemetry_service: Any,
) -> tuple[str, dict[str, Any] | None, Exception | None]:
    """Downloads IMU frames of a session from DynamoDB."""
    sess_id = _get_sess_id(sess)
    try:
        readings_resp = telemetry_service.get_session_readings(
            device_id=sess.device_id,
            session_id=sess_id,
        )
        if readings_resp and readings_resp.readings:
            raw_readings = [
                {
                    "timestamp": r.timestamp_epoch_us,
                    "ax": r.ax,
                    "ay": r.ay,
                    "az": r.az,
                    "gx": r.gx,
                    "gy": r.gy,
                    "gz": r.gz,
                }
                for r in readings_resp.readings
            ]
            record = StudioSessionDatasetRecord(
                session_id=sess_id,
                device_id=str(getattr(sess, "device_id", "HK-1") or "HK-1"),
                label=str(getattr(sess, "label", "unknown") or "unknown"),
                duration_sec=float(getattr(sess, "duration_sec", 5.0) or 5.0),
                sample_count=len(raw_readings),
                source_duration_sec=float(getattr(sess, "duration_sec", 0.0) or 0.0),
                source_sample_count=int(getattr(sess, "sample_count", len(raw_readings)) or 0),
                readings=raw_readings,
            )
            return sess_id, record.model_dump(), None
        return sess_id, None, None
    except Exception as exc:
        return sess_id, None, exc


def sync_sessions_cache(
    cache_dir: Path,
    force_refresh: bool = False,
    synthetic: bool = False,
    batch_size: int = 8,
) -> list[dict[str, Any]]:
    """Synchronizes local .npz cache with PostgreSQL and DynamoDB incrementally."""
    if synthetic:
        logger.info("Synthetic mode enabled: generating artificial dataset.")
        return generate_synthetic_sessions()

    cached_sessions: dict[str, dict[str, Any]] = {}

    if not force_refresh:
        npz_files = list(cache_dir.glob("*.npz")) if cache_dir.exists() else []

        legacy_json = cache_dir.parent / "sessions_cache.json"
        if not npz_files and legacy_json.exists():
            logger.info(
                "Empty .npz cache detected. Automatic migration from legacy JSON: %s",
                legacy_json,
            )
            migrated = migrate_json_to_npz(legacy_json, cache_dir)
            if migrated > 0:
                npz_files = list(cache_dir.glob("*.npz"))

        for npz_file in npz_files:
            sess = load_session_npz(npz_file)
            if sess is not None:
                cached_sessions[sess["session_id"]] = sess

        if cached_sessions:
            logger.info(
                ".npz cache loaded: %d session(s) found in %s",
                len(cached_sessions),
                cache_dir,
            )

    db_sessions = []
    try:
        from app.db.database import SessionLocal
        from app.db.models import StudioSession

        with SessionLocal() as db:
            all_db_sessions = db.query(StudioSession).all()
        db_sessions = [sess for sess in all_db_sessions if sess.is_validated]
        logger.info(
            "PostgreSQL query: %d session(s) listed, %d confirmed/validated",
            len(all_db_sessions),
            len(db_sessions),
        )
    except Exception as db_err:
        err_msg = str(db_err)
        if isinstance(db_err, UnicodeDecodeError) or "codec can't decode byte" in err_msg:
            logger.warning("PostgreSQL connection failed: authentication error on local server.")
        else:
            logger.warning("PostgreSQL connection failed: %s", db_err)
        if cached_sessions:
            logger.info("Exclusively using %d sessions from local cache.", len(cached_sessions))
            return list(cached_sessions.values())
        logger.error("No sessions in cache and database unreachable.")
        return []

    if not db_sessions:
        logger.warning("No validated sessions found in PostgreSQL database.")
        for sess_id in list(cached_sessions):
            npz_file = cache_dir / f"{sess_id}.npz"
            try:
                npz_file.unlink(missing_ok=True)
            except Exception as del_err:
                logger.warning("Unable to delete %s: %s", npz_file, del_err)
        return []

    active_db_session_ids = {_get_sess_id(sess).lower() for sess in db_sessions}
    deleted_session_ids = [
        sess_id
        for sess_id in list(cached_sessions.keys())
        if str(sess_id).lower() not in active_db_session_ids
    ]
    if deleted_session_ids:
        logger.info(
            "Purging local dataset: %d session(s) deleted from PostgreSQL",
            len(deleted_session_ids),
        )
        for sess_id in deleted_session_ids:
            npz_file = cache_dir / f"{sess_id}.npz"
            try:
                npz_file.unlink(missing_ok=True)
            except Exception as del_err:
                logger.warning("Unable to delete %s: %s", npz_file, del_err)
            del cached_sessions[sess_id]

    sessions_to_download = []
    labels_updated = 0
    modified_sessions = 0
    for sess in db_sessions:
        sess_id = _get_sess_id(sess)
        if not force_refresh and sess_id in cached_sessions:
            cached_session = cached_sessions[sess_id]
            cached_source_sample_count = cached_session.get("source_sample_count")
            if cached_source_sample_count is None:
                cached_source_sample_count = cached_session.get("sample_count", 0)
            cached_source_duration_sec = cached_session.get("source_duration_sec")
            if cached_source_duration_sec is None:
                cached_source_duration_sec = cached_session.get("duration_sec", 0.0)
            if (
                int(cached_source_sample_count or 0) != int(sess.sample_count or 0)
                or abs(
                    float(cached_source_duration_sec or 0.0)
                    - float(sess.duration_sec or 0.0)
                )
                > 1e-6
            ):
                sessions_to_download.append(sess)
                modified_sessions += 1
                cached_sessions.pop(sess_id)
                cached_file = cache_dir / f"{sess_id}.npz"
                try:
                    cached_file.unlink(missing_ok=True)
                except Exception as del_err:
                    logger.warning(
                        "Unable to remove stale cache file %s: %s",
                        cached_file,
                        del_err,
                    )
            elif cached_session.get("label") != sess.label:
                cached_session["label"] = sess.label
                try:
                    save_session_npz(cache_dir, cached_session)
                    labels_updated += 1
                except Exception as write_err:
                    logger.warning("Unable to update .npz label for %s: %s", sess_id, write_err)
        else:
            sessions_to_download.append(sess)

    if not sessions_to_download:
        logger.info("All %d sessions are already present in local cache.", len(cached_sessions))
        if labels_updated > 0:
            logger.info("Labels updated for %d session(s).", labels_updated)
        return list(cached_sessions.values())

    try:
        from app.services.telemetry_service import TelemetryService

        telemetry_service = TelemetryService()
    except Exception as init_err:
        logger.warning("Unable to initialize TelemetryService: %s", init_err)
        telemetry_service = None

    if telemetry_service is None:
        logger.error("TelemetryService unavailable. Cannot download DynamoDB frames.")
        return list(cached_sessions.values())

    from concurrent.futures import ThreadPoolExecutor, as_completed

    effective_workers = max(1, min(batch_size, len(sessions_to_download)))
    total_to_download = len(sessions_to_download)
    total_batches = (total_to_download + batch_size - 1) // batch_size

    logger.info(
        "Batch DynamoDB download: %d session(s) to fetch in %d batch(es) (max concurrency: %d workers)",
        total_to_download,
        total_batches,
        effective_workers,
    )

    downloaded_count = 0
    aws_auth_error_notified = False

    for batch_idx in range(0, total_to_download, batch_size):
        batch_chunk = sessions_to_download[batch_idx : batch_idx + batch_size]
        batch_num = (batch_idx // batch_size) + 1
        logger.info(
            "--> Batch %d/%d: downloading %d session(s) in parallel...",
            batch_num,
            total_batches,
            len(batch_chunk),
        )

        with ThreadPoolExecutor(max_workers=min(batch_size, len(batch_chunk))) as executor:
            future_to_sess = {
                executor.submit(_fetch_single_session, sess, telemetry_service): sess
                for sess in batch_chunk
            }
            for future in as_completed(future_to_sess):
                sess_id, session_dict, exc = future.result()
                if exc is not None:
                    exc_type = type(exc).__name__
                    exc_str = str(exc)
                    if not aws_auth_error_notified and (
                        "NoCredentialsError" in exc_type
                        or "PartialCredentialsError" in exc_type
                        or "ExpiredToken" in exc_str
                        or "UnrecognizedClientException" in exc_str
                        or "AccessDenied" in exc_str
                    ):
                        logger.error("AWS credentials missing, invalid or expired.")
                        aws_auth_error_notified = True
                    logger.error("DynamoDB error for session %s: %s", sess_id, exc)
                elif session_dict is not None:
                    try:
                        save_session_npz(cache_dir, session_dict)
                        cached_sessions[sess_id] = session_dict
                        downloaded_count += 1
                    except Exception as write_err:
                        logger.error("Unable to save session %s to .npz: %s", sess_id, write_err)

    logger.info(
        "Batch download complete: %d/%d session(s) saved (%d total in cache)",
        downloaded_count,
        total_to_download,
        len(cached_sessions),
    )
    if modified_sessions > 0:
        logger.info(
            "Detected metadata changes for %d session(s); refreshed their telemetry.",
            modified_sessions,
        )

    return list(cached_sessions.values())


def is_fall_activity(label: str) -> bool:
    """Returns True if label corresponds to a critical fall (family 'fall_*')."""
    lbl = str(label).lower()
    return lbl.startswith("fall_") and lbl != "fall_recovery"


def is_benign_activity(label: str) -> bool:
    """Returns True if label corresponds to normal activity or resting."""
    return not is_fall_activity(label)


# -----------------------------------------------------------------------------
# 3. SYNTHETIC SESSION GENERATOR (Tests & Demo)
# -----------------------------------------------------------------------------
def generate_synthetic_sessions(n_per_class: int = 25) -> list[dict[str, Any]]:
    """Generates a synthetic IMU biomechanical dataset."""
    np.random.seed(42)
    sessions: list[dict[str, Any]] = []
    sampling_freq = 50.0
    duration = 5.0
    n_points = int(duration * sampling_freq)
    t = np.linspace(0, duration, n_points)

    classes = ["walk", "idle", "fall_forward", "stairs_up", "stairs_down", "fall_recovery"]

    for label in classes:
        for idx in range(n_per_class):
            sess_id = f"synth_{label}_{idx:03d}"
            base_time = 1720000000_000_000 + idx * 10_000_000

            if label == "walk":
                ax = 0.8 * np.sin(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.2, n_points)
                ay = 9.8 + 2.0 * np.cos(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.3, n_points)
                az = 0.5 * np.sin(2 * np.pi * 1.8 * t + 0.5) + np.random.normal(0, 0.2, n_points)
                gx = 0.3 * np.cos(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.05, n_points)
                gy = 0.2 * np.sin(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.05, n_points)
                gz = 0.4 * np.cos(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.05, n_points)

            elif label == "idle":
                ax = np.random.normal(0, 0.04, n_points)
                ay = 9.8 + np.random.normal(0, 0.04, n_points)
                az = np.random.normal(0, 0.04, n_points)
                gx = np.random.normal(0, 0.01, n_points)
                gy = np.random.normal(0, 0.01, n_points)
                gz = np.random.normal(0, 0.01, n_points)

            elif label == "fall_forward":
                fall_start = int(2.2 * sampling_freq)
                impact_idx = int(2.6 * sampling_freq)
                rest_idx = int(3.0 * sampling_freq)

                ax = np.random.normal(0, 0.2, n_points)
                ay = np.random.normal(9.8, 0.3, n_points)
                az = np.random.normal(0, 0.2, n_points)
                gx = np.random.normal(0, 0.1, n_points)
                gy = np.random.normal(0, 0.1, n_points)
                gz = np.random.normal(0, 0.1, n_points)

                ay[fall_start:impact_idx] *= 0.1
                ax[fall_start:impact_idx] *= 0.1
                az[fall_start:impact_idx] *= 0.1

                ay[impact_idx : impact_idx + 4] = 28.0 + np.random.normal(0, 2.0, 4)
                ax[impact_idx : impact_idx + 4] = 15.0 + np.random.normal(0, 1.5, 4)
                gx[impact_idx - 5 : impact_idx + 5] = 7.5 + np.random.normal(0, 0.5, 10)

                ay[rest_idx:] = 0.2 + np.random.normal(0, 0.05, n_points - rest_idx)
                az[rest_idx:] = 9.7 + np.random.normal(0, 0.05, n_points - rest_idx)
                gx[rest_idx:] = np.random.normal(0, 0.02, n_points - rest_idx)

            elif label == "stairs_up":
                ax = 1.2 * np.sin(2 * np.pi * 1.3 * t) + np.random.normal(0, 0.3, n_points)
                ay = 9.8 + 4.2 * np.sin(2 * np.pi * 1.3 * t) + np.random.normal(0, 0.4, n_points)
                az = 1.1 * np.cos(2 * np.pi * 1.3 * t) + np.random.normal(0, 0.3, n_points)
                gx = 0.7 * np.cos(2 * np.pi * 1.3 * t) + np.random.normal(0, 0.1, n_points)
                gy = 0.4 * np.sin(2 * np.pi * 1.3 * t) + np.random.normal(0, 0.1, n_points)
                gz = 0.5 * np.cos(2 * np.pi * 1.3 * t) + np.random.normal(0, 0.1, n_points)

            elif label == "stairs_down":
                ax = 1.0 * np.sin(2 * np.pi * 1.5 * t) + np.random.normal(0, 0.3, n_points)
                ay = 9.8 + 3.0 * np.sin(2 * np.pi * 1.5 * t) + np.random.normal(0, 0.4, n_points)
                az = 0.9 * np.cos(2 * np.pi * 1.5 * t) + np.random.normal(0, 0.3, n_points)
                gx = 0.5 * np.cos(2 * np.pi * 1.5 * t) + np.random.normal(0, 0.1, n_points)
                gy = 0.3 * np.sin(2 * np.pi * 1.5 * t) + np.random.normal(0, 0.1, n_points)
                gz = 0.6 * np.cos(2 * np.pi * 1.5 * t) + np.random.normal(0, 0.1, n_points)

            elif label == "fall_recovery":
                # Fall impact at 1.0s, brief stillness on floor, then stand-up motion from 2.5s to 4.2s
                ax = np.random.normal(0, 0.1, n_points)
                ay = np.random.normal(9.8, 0.2, n_points)
                az = np.random.normal(0, 0.1, n_points)
                gx = np.random.normal(0, 0.05, n_points)
                gy = np.random.normal(0, 0.05, n_points)
                gz = np.random.normal(0, 0.05, n_points)

                impact_idx = int(1.0 * sampling_freq)
                standup_idx = int(2.5 * sampling_freq)
                ay[impact_idx:standup_idx] = 0.5 + np.random.normal(0, 0.05, standup_idx - impact_idx)
                az[impact_idx:standup_idx] = 9.6 + np.random.normal(0, 0.05, standup_idx - impact_idx)

                rec_len = int(1.5 * sampling_freq)
                t_rec = np.linspace(0, 1, rec_len)
                ay[standup_idx : standup_idx + rec_len] = (0.5 + 9.3 * t_rec) + 1.5 * np.sin(np.pi * t_rec)
                az[standup_idx : standup_idx + rec_len] = 9.6 * (1.0 - t_rec)
                gx[standup_idx : standup_idx + rec_len] = 2.0 * np.sin(np.pi * t_rec) + np.random.normal(0, 0.1, rec_len)

            readings = [
                {
                    "timestamp": int(base_time + i * (1_000_000 / sampling_freq)),
                    "ax": float(ax[i]),
                    "ay": float(ay[i]),
                    "az": float(az[i]),
                    "gx": float(gx[i]),
                    "gy": float(gy[i]),
                    "gz": float(gz[i]),
                }
                for i in range(n_points)
            ]

            sessions.append(
                {
                    "session_id": sess_id,
                    "device_id": f"dev_synth_{idx%5:02d}",
                    "label": label,
                    "duration_sec": duration,
                    "sample_count": len(readings),
                    "readings": readings,
                }
            )

    return sessions


# -----------------------------------------------------------------------------
# 4. MULTI-MODEL BENCHMARK & FINAL TRAINING
# -----------------------------------------------------------------------------
def train_and_benchmark(
    X: pd.DataFrame,
    y: np.ndarray,
    output_model_path: str = "scripts/models/activity_classifier.joblib",
    window_size_sec: float = 3.0,
    include_unsupported_c: bool = False,
) -> dict[str, Any]:
    """Compares candidate classifiers using 5-Fold Stratified CV.

    By default, only m2cgen-compatible models configured for ESP32 constraints
    are evaluated. If include_unsupported_c is True, HistGradientBoosting is added.
    """
    if len(X) == 0 or len(y) == 0:
        raise ValueError("Window dataset is empty. Cannot start training.")

    print("\n" + "=" * 68)
    print("DATASET READY: TEMPORAL WINDOW EXTRACTION")
    print("=" * 68)
    print(f"Total extracted windows : {len(X)}")
    print(f"Matrix X dimensions     : {X.shape[0]} rows x {X.shape[1]} features")
    print(f"Target profile          : {'RESEARCH (All Models)' if include_unsupported_c else 'EDGE EMBEDDED (m2cgen C compatible)'}")
    print("Class distribution :")
    for lbl, count in pd.Series(y).value_counts().items():
        pct = (count / len(y)) * 100
        cat_tag = "CRITICAL FALL" if is_fall_activity(lbl) else "BENIGN"
        print(f"  * {lbl:<18} : {count:4d} windows ({pct:5.1f} %) [{cat_tag}]")
    print("=" * 68 + "\n")

    # Base candidate models: 100% compatible m2cgen & optimized for ESP32 flash memory
    models: dict[str, Any] = {
        "RandomForest_Standard": RandomForestClassifier(
            n_estimators=40, max_depth=8, class_weight="balanced", random_state=42, n_jobs=-1
        ),
        "RandomForest_Light": RandomForestClassifier(
            n_estimators=30, max_depth=6, class_weight="balanced", random_state=42, n_jobs=-1
        ),
        "ExtraTrees_Standard": ExtraTreesClassifier(
            n_estimators=40, max_depth=8, class_weight="balanced", random_state=42, n_jobs=-1
        ),
    }

    # Include non-transpilable / experimental models only when explicitly requested
    if include_unsupported_c:
        models["HistGradientBoosting"] = HistGradientBoostingClassifier(
            max_iter=100, max_depth=5, random_state=42
        )
        models["LogisticRegression"] = Pipeline(
            [
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(max_iter=1000, random_state=42)),
            ]
        )

    min_class_samples = pd.Series(y).value_counts().min()
    n_splits = max(min(5, min_class_samples), 2)

    scoring = ["accuracy", "precision_macro", "recall_macro", "f1_macro"]
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)

    print(f"[BENCHMARK] Cross validation ({n_splits} stratified folds):")
    header = f"{'Model':<24} | {'Accuracy':<10} | {'Precision':<12} | {'Recall':<10} | {'F1-Macro':<10}"
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    scores_summary: dict[str, dict[str, Any]] = {}
    warnings.filterwarnings("ignore", category=UndefinedMetricWarning)

    for name, clf in models.items():
        try:
            cv_res = cross_validate(clf, X, y, cv=cv, scoring=scoring, n_jobs=-1)
            mean_acc = float(np.mean(cv_res["test_accuracy"]))
            mean_prec = float(np.mean(cv_res["test_precision_macro"]))
            mean_rec = float(np.mean(cv_res["test_recall_macro"]))
            mean_f1 = float(np.mean(cv_res["test_f1_macro"]))

            scores_summary[name] = {
                "accuracy": mean_acc,
                "precision_macro": mean_prec,
                "recall_macro": mean_rec,
                "f1_macro": mean_f1,
                "estimator": clf,
            }

            print(
                f"{name:<24} | {mean_acc*100:8.2f} % | {mean_prec*100:10.2f} % | {mean_rec*100:8.2f} % | {mean_f1*100:8.2f} %"
            )
        except Exception as cv_err:
            logger.warning("CV evaluation failed for %s: %s", name, cv_err)

    if not scores_summary:
        raise RuntimeError("No model could be successfully evaluated in cross-validation.")

    print("-" * len(header))

    best_name = max(scores_summary, key=lambda k: scores_summary[k]["f1_macro"])
    best_info = scores_summary[best_name]
    best_clf = best_info["estimator"]

    print(f"\n[CHAMPION] Selected model: {best_name}")
    print(f"   F1 Score (macro)     : {best_info['f1_macro']*100:.2f} %")
    print(f"   Precision (macro)    : {best_info['precision_macro']*100:.2f} %")
    print(f"   Recall (macro)       : {best_info['recall_macro']*100:.2f} %")
    print(f"   Global Accuracy      : {best_info['accuracy']*100:.2f} %")

    print("\n[TRAINING] Retraining champion model on 100% of dataset...")
    best_clf.fit(X, y)

    out_path = Path(output_model_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    unique_classes = [str(c) for c in np.unique(y)]
    fall_classes = [c for c in unique_classes if is_fall_activity(c)]
    benign_classes = [c for c in unique_classes if is_benign_activity(c)]

    package = {
        "model_name": best_name,
        "estimator": best_clf,
        "feature_names": list(X.columns),
        "classes": unique_classes,
        "fall_classes": fall_classes,
        "benign_classes": benign_classes,
        "window_size_sec": float(window_size_sec),
        "trained_at_utc": datetime.now(UTC).isoformat(),
        "is_m2cgen_compatible": (best_name != "HistGradientBoosting"),
        "metrics": {
            "f1_macro": best_info["f1_macro"],
            "accuracy": best_info["accuracy"],
            "precision_macro": best_info["precision_macro"],
            "recall_macro": best_info["recall_macro"],
        },
    }

    joblib.dump(package, out_path)
    file_size_kb = out_path.stat().st_size / 1024.0
    print(f"[OK] Artifact exported successfully: {out_path} ({file_size_kb:.1f} KB)")
    print("=" * 68 + "\n")

    return package


# -----------------------------------------------------------------------------
# 5. CLI ENTRYPOINT
# -----------------------------------------------------------------------------
def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parses command line arguments."""
    parser = argparse.ArgumentParser(
        description="Trains the Edge fall detection model from Studio sessions."
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignores local cache and re-downloads all frames from DynamoDB.",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="scripts/data/sessions",
        help="Per-session .npz cache directory (default: scripts/data/sessions).",
    )
    parser.add_argument(
        "--output-model",
        type=str,
        default="scripts/models/activity_classifier.joblib",
        help="Output path for serialized model artifact (default: scripts/models/activity_classifier.joblib).",
    )
    parser.add_argument(
        "--window-size",
        type=float,
        default=3.0,
        help="Duration of raw signal accumulation in seconds used to extract temporal & biomechanical features (default: 3.0s).",
    )
    parser.add_argument(
        "--window-step",
        type=float,
        default=0.5,
        help="Sliding step / temporal shift between successive extracted windows in seconds, defining the overlap (default: 0.5s).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Number of concurrent DynamoDB downloads in thread pool (default: 8).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Generates an example synthetic dataset (useful without active AWS or DB).",
    )
    parser.add_argument(
        "--include-unsupported-c",
        action="store_true",
        help="Include non-m2cgen models (HistGradientBoosting) in CV benchmark.",
    )
    parser.add_argument(
        "--min-sessions-per-class",
        type=int,
        default=20,
        help="Minimum number of recording sessions required to include a class in training (default: 20).",
    )
    parser.add_argument(
        "--max-sessions-per-class",
        type=int,
        default=None,
        help="Maximum number of most recent recording sessions to keep per class (default: None, keep all).",
    )
    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """Main entrypoint for the training script."""
    load_project_env()
    args = parse_args(argv)
    cache_file = Path(args.cache_dir)
    min_sessions = 0 if args.synthetic else args.min_sessions_per_class

    print("=" * 68)
    print("HEALTHKICKS EDGE ML - ACTIVITY CLASSIFIER TRAINING PIPELINE")
    print("=" * 68)
    print(f"* .npz Cache dir       : {cache_file}")
    print(f"* Output model         : {args.output_model}")
    print(f"* Window size          : {args.window_size:.1f} s (step: {args.window_step:.1f} s)")
    print(f"* Batch concurrency    : {args.batch_size} concurrent sessions")
    print(f"* Force refresh        : {'YES' if args.force_refresh else 'NO'}")
    print(f"* Synthetic data       : {'YES' if args.synthetic else 'NO'}")
    print(f"* Min sessions / class : {min_sessions}")
    print(f"* Max sessions / class : {args.max_sessions_per_class if args.max_sessions_per_class else 'ALL (unlimited)'}")
    print(f"* Only m2cgen models   : {'NO (all models)' if args.include_unsupported_c else 'YES (default)'}")
    print("=" * 68 + "\n")

    # 1. Incremental cache synchronization
    sessions_data = sync_sessions_cache(
        cache_dir=cache_file,
        force_refresh=args.force_refresh,
        synthetic=args.synthetic,
        batch_size=args.batch_size,
    )

    if not sessions_data:
        logger.error("No session data available for training. Exiting.")
        return 1

    # 2. Featurization and windowing
    logger.info("Extracting features and sliding windows (min_sessions=%d)...", min_sessions)
    X, y = build_dataset_from_sessions(
        sessions_data=sessions_data,
        window_size_sec=args.window_size,
        step_sec=args.window_step,
        min_sessions_per_class=min_sessions,
        max_sessions_per_class=args.max_sessions_per_class,
    )

    if len(X) == 0:
        logger.error("No time windows could be extracted from sessions. Exiting.")
        return 1

    # 3. Benchmark and export champion model
    try:
        train_and_benchmark(
            X=X,
            y=y,
            output_model_path=args.output_model,
            window_size_sec=args.window_size,
            include_unsupported_c=args.include_unsupported_c,
        )
    except Exception as exc:
        logger.error("Error during training and benchmark: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
