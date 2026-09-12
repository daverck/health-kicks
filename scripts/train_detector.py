"""Script d'entraînement ML local pour le classifieur d'activité Edge (HealthKicks).

Ce script se connecte aux bases du projet (Aurora PostgreSQL et DynamoDB) pour
récupérer les sessions d'enregistrement Studio validées, maintient un cache local
incrémental afin de minimiser les appels réseau, extrait les features biomécaniques
et temporelles issues de la centrale inertielle (IMU), compare plusieurs classifieurs
adaptés aux contraintes Edge (Raspberry Pi / microcontrôleur) via validation croisée
(Stratified 5-Fold), et sérialise le modèle champion sous forme d'artefact joblib.

Prérequis :
    1. Les dépendances de data science sont isolées dans le groupe optionnel 'ml' :
       $ uv sync --group ml

    2. Authentification AWS CLI pour l'accès aux données réelles de télémétrie DynamoDB :
       $ aws sso login    # (ou 'aws login' / 'aws configure')
       Note : l'option '--synthetic' permet d'ignorer AWS pour un entraînement hors-ligne.

Exemples d'utilisation :
    # Entraînement standard avec cache local incrémental (téléchargement batch) :
    $ uv run python -m scripts.train_detector

    # Téléchargement batch avec réglage de la concurrence (ex: 12 workers) :
    $ uv run python -m scripts.train_detector --batch-size 12

    # Forcer la resynchronisation complète depuis PostgreSQL & DynamoDB :
    $ uv run python -m scripts.train_detector --force-refresh

    # Mode développement / démo hors-ligne avec dataset synthétique (aucun accès AWS requis) :
    $ uv run python -m scripts.train_detector --synthetic

    # Personnalisation des fenêtres et des chemins de sortie :
    $ uv run python -m scripts.train_detector --window-size 2.0 --window-step 0.5 --output-model scripts/models/activity_classifier.joblib
"""
from pathlib import Path
from dotenv import load_dotenv


def load_project_env() -> None:
    """Charge le fichier .env du projet sans écraser les variables déjà définies."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

import argparse
from datetime import datetime, timezone
import json
import logging
import math
import os
import sys
from typing import Any

import joblib
import numpy as np
import pandas as pd
import warnings

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

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("train_detector")


# -----------------------------------------------------------------------------
# 1. EXTRACTION DES FEATURES BIOMÉCANIQUES IMU
# -----------------------------------------------------------------------------
def compute_window_features(df_window: pd.DataFrame) -> dict[str, float]:
    """Calcule les indicateurs statistiques et physiques sur une fenêtre IMU.

    Colonnes attendues : ax, ay, az, gx, gy, gz.
    Les accélérations sont en m/s² ou g, les vitesses angulaires en rad/s ou deg/s.
    """
    ax = df_window["ax"].to_numpy(dtype=float)
    ay = df_window["ay"].to_numpy(dtype=float)
    az = df_window["az"].to_numpy(dtype=float)
    gx = df_window["gx"].to_numpy(dtype=float)
    gy = df_window["gy"].to_numpy(dtype=float)
    gz = df_window["gz"].to_numpy(dtype=float)

    # Normes euclidiennes (invariantes à l'orientation spatiale de la chaussure)
    acc_mag = np.sqrt(ax**2 + ay**2 + az**2)
    gyro_mag = np.sqrt(gx**2 + gy**2 + gz**2)

    n_samples = max(len(acc_mag), 1)

    features: dict[str, float] = {
        # Accélération - Magnitude (Signature de chute : creux d'apesanteur + pic d'impact)
        "acc_mag_max": float(np.max(acc_mag)),
        "acc_mag_min": float(np.min(acc_mag)),
        "acc_mag_mean": float(np.mean(acc_mag)),
        "acc_mag_std": float(np.std(acc_mag)),
        "acc_mag_peak_to_peak": float(np.max(acc_mag) - np.min(acc_mag)),
        # Gyroscope - Magnitude (Rotation brutale du segment corporel)
        "gyro_mag_max": float(np.max(gyro_mag)),
        "gyro_mag_mean": float(np.mean(gyro_mag)),
        "gyro_mag_std": float(np.std(gyro_mag)),
        # Écarts-types par axe individuel (dispersion tri-axiale)
        "ax_std": float(np.std(ax)),
        "ay_std": float(np.std(ay)),
        "az_std": float(np.std(az)),
        "gx_std": float(np.std(gx)),
        "gy_std": float(np.std(gy)),
        "gz_std": float(np.std(gz)),
        # Énergie cinétique / dynamique approchée
        "acc_energy": float(np.sum(acc_mag**2) / n_samples),
        "gyro_energy": float(np.sum(gyro_mag**2) / n_samples),
    }
    return features


def build_dataset_from_sessions(
    sessions_data: list[dict[str, Any]],
    window_size_sec: float = 2.0,
    step_sec: float = 0.5,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Découpe chaque session en fenêtres glissantes et extrait les features.

    Gère le fenêtrage temporel basé sur les timestamps d'échantillonnage
    (microsecondes DynamoDB ou millisecondes), avec repli par nombre d'échantillons
    si les horodatages ne sont pas exploitables.
    """
    X_rows: list[dict[str, float]] = []
    y_labels: list[str] = []

    for session in sessions_data:
        label = session.get("label")
        readings = session.get("readings", [])
        if not label or len(readings) < 5:
            continue

        df = pd.DataFrame(readings)

        # Vérification des colonnes IMU indispensables
        required_cols = {"ax", "ay", "az", "gx", "gy", "gz"}
        if not required_cols.issubset(df.columns):
            continue

        # Tri chronologique si timestamp disponible
        ts_col = None
        for candidate in ["timestamp", "timestamp_epoch_us", "ts"]:
            if candidate in df.columns:
                ts_col = candidate
                break

        if ts_col:
            df = df.sort_values(ts_col).reset_index(drop=True)
            ts_vals = df[ts_col].to_numpy(dtype=float)
            delta_ts = ts_vals[-1] - ts_vals[0]

            # Détection de l'échelle d'horodatage (époque Unix ou horodatage relatif)
            t0 = ts_vals[0]
            if t0 > 1e14 or delta_ts > 100_000:
                scale = 1e6  # Microsecondes (format DynamoDB standard)
            elif t0 > 1e11 or delta_ts > 100:
                scale = 1e3  # Millisecondes
            elif delta_ts > 0:
                scale = 1.0  # Secondes
            else:
                scale = None
        else:
            scale = None

        total_samples = len(df)
        windows_extracted = 0

        # Fenêtrage temporel strict si horodatages valides
        if scale and scale > 0 and (delta_ts / scale) >= window_size_sec:
            t_sec = (ts_vals - ts_vals[0]) / scale
            max_t = t_sec[-1]
            curr_t = 0.0

            while curr_t + window_size_sec <= max_t + 1e-5:
                window_mask = (t_sec >= curr_t) & (t_sec <= curr_t + window_size_sec)
                window_df = df.loc[window_mask]
                if len(window_df) >= 5:
                    feat = compute_window_features(window_df)
                    X_rows.append(feat)
                    y_labels.append(label)
                    windows_extracted += 1
                curr_t += step_sec
        else:
            # Repli sur découpage par taux d'échantillonnage estimé
            duration_hint = float(session.get("duration_sec", 5.0) or 5.0)
            samples_per_sec = max(total_samples / max(duration_hint, 0.5), 10.0)
            window_samples = max(int(window_size_sec * samples_per_sec), 5)
            step_samples = max(int(step_sec * samples_per_sec), 1)

            for start in range(0, total_samples - window_samples + 1, step_samples):
                window_df = df.iloc[start : start + window_samples]
                feat = compute_window_features(window_df)
                X_rows.append(feat)
                y_labels.append(label)
                windows_extracted += 1

        # Si la session était un peu courte mais contient assez de points, extraire au moins 1 fenêtre
        if windows_extracted == 0 and total_samples >= 5:
            feat = compute_window_features(df)
            X_rows.append(feat)
            y_labels.append(label)

    if not X_rows:
        return pd.DataFrame(), np.array([])

    return pd.DataFrame(X_rows), np.array(y_labels)


# -----------------------------------------------------------------------------
# 2. CACHE LOCAL INCRÉMENTAL (PostgreSQL + DynamoDB)
# -----------------------------------------------------------------------------
def _fetch_single_session(
    sess: Any,
    telemetry_service: Any,
) -> tuple[str, dict[str, Any] | None, Exception | None]:
    """Télécharge les trames IMU d'une session depuis DynamoDB (exécuté dans un thread worker)."""
    sess_id = str(sess.id)
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
            session_dict = {
                "session_id": sess_id,
                "device_id": sess.device_id,
                "label": sess.label,
                "duration_sec": sess.duration_sec,
                "sample_count": len(raw_readings),
                "readings": raw_readings,
            }
            return sess_id, session_dict, None
        return sess_id, None, None
    except Exception as exc:
        return sess_id, None, exc


def sync_sessions_cache(
    cache_path: Path,
    force_refresh: bool = False,
    synthetic: bool = False,
    batch_size: int = 8,
) -> list[dict[str, Any]]:
    """Synchronise le cache local avec PostgreSQL et DynamoDB de manière incrémentale et par lots.

    1. Charge les sessions déjà archivées localement dans `cache_path`.
    2. Interroge la table PostgreSQL `StudioSession` pour identifier les nouvelles captures.
    3. Télécharge en batch concurrent (pool de threads de taille `batch_size`) les trames
       DynamoDB des sessions manquantes.
    4. Réécrit le fichier JSON mis à jour de manière incrémentale à chaque lot téléchargé.
    """
    if synthetic:
        logger.info("Mode synthétique activé : génération de données artificielles.")
        return generate_synthetic_sessions()

    cached_sessions: dict[str, dict[str, Any]] = {}

    if not force_refresh and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "sessions" in data:
                    cached_sessions = data["sessions"]
                elif isinstance(data, list):
                    cached_sessions = {s["session_id"]: s for s in data if "session_id" in s}
            logger.info(
                "Cache local chargé : %d sessions trouvées dans %s",
                len(cached_sessions),
                cache_path,
            )
        except Exception as err:
            logger.warning("Échec de lecture du cache existant (%s) : %s", cache_path, err)
            cached_sessions = {}

    # Connexion à PostgreSQL pour récupérer la liste des StudioSessions
    db_sessions = []
    try:
        from app.db.database import SessionLocal
        from app.db.models import StudioSession

        with SessionLocal() as db:
            db_sessions = db.query(StudioSession).all()
        logger.info("Interrogation PostgreSQL : %d sessions répertoriées", len(db_sessions))
    except Exception as db_err:
        err_msg = str(db_err)
        if isinstance(db_err, UnicodeDecodeError) or "codec can't decode byte" in err_msg:
            logger.warning(
                "Connexion PostgreSQL impossible : échec d'authentification sur le serveur local. "
                "(Le serveur PostgreSQL a renvoyé un message en encodage Windows-1252 indiquant que "
                "le rôle ou la base 'healthkicks' n'existe pas, ou mot de passe incorrect). "
                "Vérifiez DATABASE_URL dans votre fichier .env ou démarrez le conteneur Docker PostgreSQL."
            )
        else:
            logger.warning("Connexion PostgreSQL impossible : %s", db_err)
        if cached_sessions:
            logger.info("Utilisation exclusive des %d sessions en cache local.", len(cached_sessions))
            return list(cached_sessions.values())
        logger.error("Aucune session en cache et base inaccessible.")
        logger.info("Pour tester l'entraînement sans base active, relancez avec : --synthetic")
        return []

    if not db_sessions:
        logger.warning("Aucune session trouvée dans la base PostgreSQL.")
        if cached_sessions:
            return list(cached_sessions.values())
        logger.info("Pour générer un jeu d'entraînement d'exemple, utilisez : --synthetic")
        return []

    def _save_cache_to_disk() -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_payload = {
            "version": 1,
            "last_sync": datetime.now(timezone.utc).isoformat(),
            "sessions": cached_sessions,
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_payload, f, indent=2)

    # 1. Détection et purge automatique des sessions supprimées de PostgreSQL
    active_db_session_ids = {str(sess.id).lower() for sess in db_sessions}
    deleted_session_ids = [
        sess_id
        for sess_id in list(cached_sessions.keys())
        if str(sess_id).lower() not in active_db_session_ids
    ]
    cache_modified = False
    if deleted_session_ids:
        logger.info(
            "Purge du dataset local : %d session(s) supprimée(s) de PostgreSQL retirée(s) du cache : %s",
            len(deleted_session_ids),
            deleted_session_ids,
        )
        for sess_id in deleted_session_ids:
            del cached_sessions[sess_id]
        cache_modified = True
        try:
            _save_cache_to_disk()
            logger.info("Fichier de cache %s synchronisé sur disque après suppression de %d session(s).", cache_path, len(deleted_session_ids))
        except Exception as write_err:
            logger.error("Impossible d'écrire le fichier de cache %s après purge : %s", cache_path, write_err)

    # 2. Filtrage des sessions à télécharger ou dont le label a été modifié
    sessions_to_download = []
    labels_updated = 0
    for sess in db_sessions:
        sess_id = str(sess.id)
        if not force_refresh and sess_id in cached_sessions:
            if cached_sessions[sess_id].get("label") != sess.label:
                cached_sessions[sess_id]["label"] = sess.label
                labels_updated += 1
                cache_modified = True
        else:
            sessions_to_download.append(sess)

    # Si toutes les sessions sont déjà en cache
    if not sessions_to_download:
        logger.info("Toutes les %d sessions sont déjà présentes dans le cache local.", len(cached_sessions))
        if labels_updated > 0:
            try:
                _save_cache_to_disk()
                logger.info("Labels mis à jour pour %d session(s) dans le cache.", labels_updated)
            except Exception as write_err:
                logger.error("Impossible d'écrire le fichier de cache %s : %s", cache_path, write_err)
        return list(cached_sessions.values())

    # Import du service de télémétrie DynamoDB
    try:
        from app.services.telemetry_service import TelemetryService

        telemetry_service = TelemetryService()
    except Exception as init_err:
        logger.warning("Impossible d'initialiser TelemetryService : %s", init_err)
        telemetry_service = None

    if telemetry_service is None:
        logger.error("TelemetryService non disponible. Impossible de télécharger les trames DynamoDB.")
        if cache_modified:
            try:
                _save_cache_to_disk()
            except Exception as write_err:
                logger.error("Impossible d'écrire le cache %s : %s", cache_path, write_err)
        return list(cached_sessions.values())

    from concurrent.futures import ThreadPoolExecutor, as_completed

    effective_workers = max(1, min(batch_size, len(sessions_to_download)))
    total_to_download = len(sessions_to_download)
    total_batches = (total_to_download + batch_size - 1) // batch_size

    logger.info(
        "Téléchargement batch DynamoDB : %d session(s) à récupérer en %d lot(s) (concurrence max: %d workers)",
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
            "--> Lot %d/%d : téléchargement de %d session(s) en parallèle...",
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
                sess_ref = future_to_sess[future]
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
                        logger.error(
                            "\n" + "=" * 70 + "\n"
                            "⚠️  AUTHENTIFICATION AWS REQUISE POUR DYNAMODB\n"
                            "Les identifiants AWS sont introuvables, invalides ou expirés.\n"
                            "Pour utiliser les sessions DynamoDB réelles, connectez-vous via l'AWS CLI :\n"
                            "    $ aws sso login    (ou 'aws login' / 'aws configure')\n\n"
                            "Astuce : pour vous entraîner hors-ligne sans connexion AWS, utilisez :\n"
                            "    $ uv run python -m scripts.train_detector --synthetic\n"
                            + "=" * 70
                        )
                        aws_auth_error_notified = True
                    logger.error(
                        "Erreur DynamoDB pour session %s (device: %s) : %s",
                        sess_id,
                        sess_ref.device_id,
                        exc,
                    )
                elif session_dict is not None:
                    cached_sessions[sess_id] = session_dict
                    downloaded_count += 1
                else:
                    logger.warning(
                        "Aucune trame IMU dans DynamoDB pour la session %s (device: %s)",
                        sess_id,
                        sess_ref.device_id,
                    )

        # Sauvegarde incrémentale à la fin de chaque lot
        try:
            _save_cache_to_disk()
        except Exception as write_err:
            logger.error("Impossible d'écrire le cache intermédiaire %s : %s", cache_path, write_err)

    logger.info(
        "Fin du téléchargement batch : %d/%d session(s) enregistrée(s) dans %s (%d sessions totales en cache)",
        downloaded_count,
        total_to_download,
        cache_path,
        len(cached_sessions),
    )

    return list(cached_sessions.values())


def is_fall_activity(label: str) -> bool:
    """Indique si un label correspond à une chute critique (famille 'fall_*')."""
    return str(label).lower().startswith("fall_")


def is_benign_activity(label: str) -> bool:
    """Indique si un label correspond à une activité normale ou au repos ('idle', 'walk', etc.)."""
    return not is_fall_activity(label)


# -----------------------------------------------------------------------------
# 3. GÉNÉRATEUR DE SESSIONS SYNTHÉTIQUES (Tests & Démo)
# -----------------------------------------------------------------------------
def generate_synthetic_sessions(n_per_class: int = 25) -> list[dict[str, Any]]:
    """Génère un dataset biomécanique IMU synthétique avec signatures physiques typiques.

    Classes simulées :
    - 'walk' : oscillations harmoniques à 1.8 Hz, magnitude ~9.8 m/s² ± 2.5 m/s².
    - 'idle' : état de repos / immobile (accélération statique 1g ~9.8 m/s², bruit minimal).
    - 'fall_forward' : phase d'apesanteur (norme proche de 0), pic d'impact (> 25 m/s²),
      forte vélocité angulaire (> 6 rad/s).
    - 'stairs' : pas cadencés plus amples et asymétriques.
    - 'stumble_recover' : à-coup brusque suivi d'une stabilisation sans pic de chute critique.
    """
    np.random.seed(42)
    sessions: list[dict[str, Any]] = []
    sampling_freq = 50.0  # 50 Hz
    duration = 5.0
    n_points = int(duration * sampling_freq)
    t = np.linspace(0, duration, n_points)

    classes = ["walk", "idle", "fall_forward", "stairs", "stumble_recover"]

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
                # État stationnaire / repos (gravité statique 1g sur l'axe Y, accélérations et rotations nulles)
                ax = np.random.normal(0, 0.04, n_points)
                ay = 9.8 + np.random.normal(0, 0.04, n_points)
                az = np.random.normal(0, 0.04, n_points)
                gx = np.random.normal(0, 0.01, n_points)
                gy = np.random.normal(0, 0.01, n_points)
                gz = np.random.normal(0, 0.01, n_points)

            elif label == "fall_forward":
                # Chute survenant vers t = 2.5s
                fall_start = int(2.2 * sampling_freq)
                impact_idx = int(2.6 * sampling_freq)
                rest_idx = int(3.0 * sampling_freq)

                ax = np.random.normal(0, 0.2, n_points)
                ay = np.random.normal(9.8, 0.3, n_points)
                az = np.random.normal(0, 0.2, n_points)
                gx = np.random.normal(0, 0.1, n_points)
                gy = np.random.normal(0, 0.1, n_points)
                gz = np.random.normal(0, 0.1, n_points)

                # Apesanteur (free-fall)
                ay[fall_start:impact_idx] *= 0.1
                ax[fall_start:impact_idx] *= 0.1
                az[fall_start:impact_idx] *= 0.1

                # Pic d'impact
                ay[impact_idx : impact_idx + 4] = 28.0 + np.random.normal(0, 2.0, 4)
                ax[impact_idx : impact_idx + 4] = 15.0 + np.random.normal(0, 1.5, 4)
                gx[impact_idx - 5 : impact_idx + 5] = 7.5 + np.random.normal(0, 0.5, 10)

                # Immobilisation post-chute au sol
                ay[rest_idx:] = 0.2 + np.random.normal(0, 0.05, n_points - rest_idx)
                az[rest_idx:] = 9.7 + np.random.normal(0, 0.05, n_points - rest_idx)
                gx[rest_idx:] = np.random.normal(0, 0.02, n_points - rest_idx)

            elif label == "stairs":
                ax = 1.2 * np.sin(2 * np.pi * 1.4 * t) + np.random.normal(0, 0.3, n_points)
                ay = 9.8 + 3.5 * np.sin(2 * np.pi * 1.4 * t) + np.random.normal(0, 0.4, n_points)
                az = 1.0 * np.cos(2 * np.pi * 1.4 * t) + np.random.normal(0, 0.3, n_points)
                gx = 0.6 * np.cos(2 * np.pi * 1.4 * t) + np.random.normal(0, 0.1, n_points)
                gy = 0.3 * np.sin(2 * np.pi * 1.4 * t) + np.random.normal(0, 0.1, n_points)
                gz = 0.5 * np.cos(2 * np.pi * 1.4 * t) + np.random.normal(0, 0.1, n_points)

            else:  # stumble_recover
                ax = 0.8 * np.sin(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.2, n_points)
                ay = 9.8 + 2.0 * np.cos(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.3, n_points)
                az = 0.5 * np.sin(2 * np.pi * 1.8 * t + 0.5) + np.random.normal(0, 0.2, n_points)
                gx = 0.3 * np.cos(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.05, n_points)
                gy = 0.2 * np.sin(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.05, n_points)
                gz = 0.4 * np.cos(2 * np.pi * 1.8 * t) + np.random.normal(0, 0.05, n_points)

                # Trébuchement vers t=2.0s
                jerk_idx = int(2.0 * sampling_freq)
                ay[jerk_idx : jerk_idx + 6] = 16.0 + np.random.normal(0, 1.0, 6)
                gx[jerk_idx : jerk_idx + 6] = 3.5 + np.random.normal(0, 0.5, 6)

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
# 4. BENCHMARK MULTI-MODÈLES & ENTRAÎNEMENT FINAL
# -----------------------------------------------------------------------------
def train_and_benchmark(
    X: pd.DataFrame,
    y: np.ndarray,
    output_model_path: str = "scripts/models/activity_classifier.joblib",
    window_size_sec: float = 2.0,
) -> dict[str, Any]:
    """Compare plusieurs classifieurs ML adaptés à l'Edge par 5-Fold Stratified CV,

    sélectionne le meilleur modèle d'après le F1-Score macro, l'entraîne sur 100%
    des données et exporte l'artefact sous forme de fichier joblib.
    """
    if len(X) == 0 or len(y) == 0:
        raise ValueError("Le dataset de fenêtres est vide. Impossible de démarrer l'entraînement.")

    print("\n" + "=" * 68)
    print("DATASET CONSTITUE : EXTRACTION DES FENETRES TEMPORELLES")
    print("=" * 68)
    print(f"Total fenetres extraites : {len(X)}")
    print(f"Dimensions matrice X     : {X.shape[0]} lignes x {X.shape[1]} features")
    print("Distribution des classes :")
    for lbl, count in pd.Series(y).value_counts().items():
        pct = (count / len(y)) * 100
        cat_tag = "CHUTE CRITIQUE" if is_fall_activity(lbl) else "BENIN"
        print(f"  * {lbl:<18} : {count:4d} fenetres ({pct:5.1f} %) [{cat_tag}]")
    print("=" * 68 + "\n")

    # Définition des classifieurs candidats pour l'Edge
    models = {
        "RandomForest": RandomForestClassifier(
            n_estimators=100, max_depth=8, random_state=42, n_jobs=-1
        ),
        "ExtraTrees": ExtraTreesClassifier(
            n_estimators=100, max_depth=8, random_state=42, n_jobs=-1
        ),
        "HistGradientBoosting": HistGradientBoostingClassifier(
            max_iter=100, max_depth=5, random_state=42
        ),
        "LogisticRegression": Pipeline(
            [
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(max_iter=1000, random_state=42)),
            ]
        ),
    }

    # Détermination du nombre de folds adapté selon la classe la moins représentée
    min_class_samples = pd.Series(y).value_counts().min()
    n_splits = max(min(5, min_class_samples), 2)

    scoring = ["accuracy", "precision_macro", "recall_macro", "f1_macro"]
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)

    print(f"[BENCHMARK] Validation croisee ({n_splits} folds stratifies) :")
    header = f"{'Modele':<24} | {'Accuracy':<10} | {'Precision':<12} | {'Rappel':<10} | {'F1-Macro':<10}"
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
            logger.warning("Échec de l'évaluation CV pour %s : %s", name, cv_err)

    if not scores_summary:
        raise RuntimeError("Aucun modèle n'a pu être évalué avec succès en cross-validation.")

    print("-" * len(header))

    # Sélection du meilleur modèle selon le F1-Score Macro (crucial pour le déséquilibre de classes)
    best_name = max(scores_summary, key=lambda k: scores_summary[k]["f1_macro"])
    best_info = scores_summary[best_name]
    best_clf = best_info["estimator"]

    print(f"\n[CHAMPION] Modele selectionne : {best_name}")
    print(f"   Score F1 (macro)     : {best_info['f1_macro']*100:.2f} %")
    print(f"   Precision (macro)    : {best_info['precision_macro']*100:.2f} %")
    print(f"   Rappel (macro)       : {best_info['recall_macro']*100:.2f} %")
    print(f"   Accuracy globale     : {best_info['accuracy']*100:.2f} %")

    # Entraînement final du modèle sélectionné sur 100% des données disponibles
    print("\n[ENTRAINEMENT] Re-entrainement du champion sur 100% des donnees...")
    best_clf.fit(X, y)

    # Préparation et sauvegarde de l'artefact joblib
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
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "f1_macro": best_info["f1_macro"],
            "accuracy": best_info["accuracy"],
            "precision_macro": best_info["precision_macro"],
            "recall_macro": best_info["recall_macro"],
        },
    }

    joblib.dump(package, out_path)
    file_size_kb = out_path.stat().st_size / 1024.0
    print(f"[OK] Artefact exporte avec succes : {out_path} ({file_size_kb:.1f} KB)")
    print("=" * 68 + "\n")

    return package


# -----------------------------------------------------------------------------
# 5. CLI ENTRYPOINT
# -----------------------------------------------------------------------------
def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse les arguments de la ligne de commande."""
    parser = argparse.ArgumentParser(
        description="Entraîne le modèle de détection de chute Edge à partir des sessions Studio."
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore le cache local et retélécharge toutes les trames depuis DynamoDB.",
    )
    parser.add_argument(
        "--cache-path",
        type=str,
        default="scripts/data/sessions_cache.json",
        help="Chemin du fichier JSON de cache local (défaut: scripts/data/sessions_cache.json).",
    )
    parser.add_argument(
        "--output-model",
        type=str,
        default="scripts/models/activity_classifier.joblib",
        help="Chemin de sortie pour l'artefact de modèle sérialisé (défaut: scripts/models/activity_classifier.joblib).",
    )
    parser.add_argument(
        "--window-size",
        type=float,
        default=2.0,
        help="Durée de la fenêtre temporelle glissante en secondes (défaut: 2.0s).",
    )
    parser.add_argument(
        "--window-step",
        type=float,
        default=0.5,
        help="Pas de déplacement de la fenêtre en secondes (défaut: 0.5s).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Nombre de téléchargements simultanés en batch depuis DynamoDB via un pool de threads (défaut: 8).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Génère un dataset synthétique d'exemple (utile sans connexion AWS ou DB).",
    )
    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée principal du script d'entraînement."""
    load_project_env()
    args = parse_args(argv)
    cache_file = Path(args.cache_path)

    print("=" * 68)
    print("HEALTHKICKS EDGE ML - PIPELINE D'ENTRAINEMENT DU CLASSIFIEUR D'ACTIVITE")
    print("=" * 68)
    print(f"* Cache local          : {cache_file}")
    print(f"* Modele de sortie     : {args.output_model}")
    print(f"* Taille de fenetre    : {args.window_size:.1f} s (pas: {args.window_step:.1f} s)")
    print(f"* Concurrence batch    : {args.batch_size} sessions simultanees")
    print(f"* Forcer le refresh    : {'OUI' if args.force_refresh else 'NON'}")
    print(f"* Donnees synthetiques : {'OUI' if args.synthetic else 'NON'}")
    print("=" * 68 + "\n")

    # 1. Synchronisation incrémentale du cache (par lots concurrents)
    sessions_data = sync_sessions_cache(
        cache_path=cache_file,
        force_refresh=args.force_refresh,
        synthetic=args.synthetic,
        batch_size=args.batch_size,
    )

    if not sessions_data:
        logger.error("Aucune donnée de session disponible pour l'entraînement. Arrêt.")
        return 1

    # 2. Featurisation et fenêtrage
    logger.info("Extraction des features et fenêtrage glissant en cours...")
    X, y = build_dataset_from_sessions(
        sessions_data=sessions_data,
        window_size_sec=args.window_size,
        step_sec=args.window_step,
    )

    if len(X) == 0:
        logger.error("Aucune fenêtre temporelle n'a pu être extraite des sessions. Arrêt.")
        return 1

    # 3. Benchmark et exportation du modèle champion
    try:
        train_and_benchmark(
            X=X,
            y=y,
            output_model_path=args.output_model,
            window_size_sec=args.window_size,
        )
    except Exception as exc:
        logger.error("Erreur lors de l'entraînement et du benchmark : %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
