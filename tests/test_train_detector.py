"""Tests unitaires et d'intégration pour le script scripts/train_detector.py."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

# Skip gracefully if optional ML dependencies are not installed
joblib = pytest.importorskip("joblib")
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")
pytest.importorskip("sklearn")

from scripts.train_detector import (
    build_dataset_from_sessions,
    compute_window_features,
    generate_synthetic_sessions,
    is_benign_activity,
    is_fall_activity,
    load_session_npz,
    main,
    migrate_json_to_npz,
    parse_args,
    save_session_npz,
    sync_sessions_cache,
    train_and_benchmark,
)


# -----------------------------------------------------------------------------
# Helpers communs aux tests
# -----------------------------------------------------------------------------
def _make_session(session_id: str, device_id: str = "dev-01", label: str = "walk") -> dict:
    """Crée un dictionnaire de session IMU minimal pour les tests."""
    return {
        "session_id": session_id,
        "device_id": device_id,
        "label": label,
        "duration_sec": 5.0,
        "sample_count": 3,
        "readings": [
            {"timestamp": i * 1000, "ax": 0.0, "ay": 9.8, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": float(i)}
            for i in range(3)
        ],
    }


def _make_db_mock(session_id: str, device_id: str, label: str) -> MagicMock:
    """Crée un mock de session PostgreSQL (StudioSession)."""
    m = MagicMock()
    m.id = session_id
    m.device_id = device_id
    m.label = label
    m.duration_sec = 5.0
    return m


# -----------------------------------------------------------------------------
# 1. Tests de compute_window_features
# -----------------------------------------------------------------------------
def test_compute_window_features_keys_and_values():
    """Vérifie que compute_window_features extrait l'ensemble des indicateurs biomécaniques."""
    # Simulation d'un capteur stationnaire orienté sur l'axe Y
    n_samples = 50
    df = pd.DataFrame(
        {
            "ax": [0.0] * n_samples,
            "ay": [9.8] * n_samples,
            "az": [0.0] * n_samples,
            "gx": [0.0] * n_samples,
            "gy": [0.0] * n_samples,
            "gz": [0.0] * n_samples,
        }
    )

    feats = compute_window_features(df)

    expected_keys = {
        "acc_mag_max",
        "acc_mag_min",
        "acc_mag_mean",
        "acc_mag_std",
        "acc_mag_peak_to_peak",
        "gyro_mag_max",
        "gyro_mag_mean",
        "gyro_mag_std",
        "ax_std",
        "ay_std",
        "az_std",
        "gx_std",
        "gy_std",
        "gz_std",
        "acc_energy",
        "gyro_energy",
    }
    assert expected_keys.issubset(feats.keys())
    assert feats["acc_mag_max"] == pytest.approx(9.8, rel=1e-3)
    assert feats["acc_mag_min"] == pytest.approx(9.8, rel=1e-3)
    assert feats["acc_mag_mean"] == pytest.approx(9.8, rel=1e-3)
    assert feats["acc_mag_std"] == pytest.approx(0.0, abs=1e-5)
    assert feats["acc_mag_peak_to_peak"] == pytest.approx(0.0, abs=1e-5)
    assert feats["acc_energy"] == pytest.approx(9.8**2, rel=1e-3)
    assert feats["gyro_mag_mean"] == pytest.approx(0.0, abs=1e-5)


# -----------------------------------------------------------------------------
# 2. Tests de build_dataset_from_sessions
# -----------------------------------------------------------------------------
def test_build_dataset_from_sessions_temporal_windowing():
    """Vérifie le découpage temporel basé sur les timestamps en microsecondes."""
    # 5 secondes à 50 Hz = 250 points (dt = 20_000 µs)
    base_us = 1_700_000_000_000_000
    readings = []
    for i in range(250):
        readings.append(
            {
                "timestamp": base_us + i * 20_000,
                "ax": 0.1 * np.sin(i / 10),
                "ay": 9.8 + 0.2 * np.cos(i / 10),
                "az": 0.05,
                "gx": 0.01,
                "gy": 0.01,
                "gz": 0.01,
            }
        )

    sessions = [
        {
            "session_id": "sess-01",
            "label": "walk",
            "duration_sec": 5.0,
            "readings": readings,
        }
    ]

    # Avec window=2.0s et step=0.5s sur 5.0s :
    # Départs possibles : 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0 (7 fenêtres)
    X, y = build_dataset_from_sessions(sessions, window_size_sec=2.0, step_sec=0.5)

    assert isinstance(X, pd.DataFrame)
    assert len(X) >= 6
    assert len(y) == len(X)
    assert (y == "walk").all()
    assert "acc_mag_max" in X.columns


def test_build_dataset_from_sessions_fallback_no_timestamps():
    """Vérifie le découpage de repli lorsque les timestamps ne sont pas renseignés."""
    readings = [
        {"ax": 0.1, "ay": 9.8, "az": 0.1, "gx": 0.0, "gy": 0.0, "gz": 0.0}
        for _ in range(100)
    ]
    sessions = [
        {
            "session_id": "sess-02",
            "label": "fall_forward",
            "duration_sec": 5.0,
            "readings": readings,
        }
    ]

    X, y = build_dataset_from_sessions(sessions, window_size_sec=2.0, step_sec=0.5)
    assert len(X) > 0
    assert len(y) == len(X)
    assert (y == "fall_forward").all()


def test_build_dataset_from_sessions_short_session():
    """Vérifie qu'une session courte avec >= 5 points produit au moins une fenêtre."""
    readings = [
        {"ax": 0.1, "ay": 9.8, "az": 0.1, "gx": 0.0, "gy": 0.0, "gz": 0.0}
        for _ in range(8)
    ]
    sessions = [{"session_id": "sess-short", "label": "stairs", "readings": readings}]

    X, y = build_dataset_from_sessions(sessions, window_size_sec=2.0, step_sec=0.5)
    assert len(X) == 1
    assert y[0] == "stairs"


def test_build_dataset_from_sessions_empty_or_invalid():
    """Vérifie la robustesse face aux données manquantes ou invalides."""
    sessions = [
        {"session_id": "sess-empty", "label": "walk", "readings": []},
        {"session_id": "sess-no-label", "readings": [{"ax": 1, "ay": 2}]},
        {"session_id": "sess-missing-cols", "label": "walk", "readings": [{"ax": 1} for _ in range(20)]},
    ]
    X, y = build_dataset_from_sessions(sessions)
    assert len(X) == 0
    assert len(y) == 0


# -----------------------------------------------------------------------------
# 3. Tests de generate_synthetic_sessions
# -----------------------------------------------------------------------------
def test_generate_synthetic_sessions():
    """Vérifie la cohérence du générateur de sessions synthétiques."""
    sessions = generate_synthetic_sessions(n_per_class=5)
    assert len(sessions) == 25  # 5 classes * 5

    labels = {s["label"] for s in sessions}
    assert labels == {"walk", "idle", "fall_forward", "stairs", "stumble_recover"}

    sample_sess = sessions[0]
    assert "session_id" in sample_sess
    assert "readings" in sample_sess
    assert len(sample_sess["readings"]) == 250
    assert "timestamp" in sample_sess["readings"][0]


def test_fall_and_benign_activity_helpers():
    """Vérifie le classement correct des activités bénignes (dont idle) vs chutes critiques."""
    assert is_benign_activity("idle")
    assert not is_fall_activity("idle")

    assert is_benign_activity("walk")
    assert is_benign_activity("stairs")
    assert is_benign_activity("stumble_recover")

    assert is_fall_activity("fall_forward")
    assert is_fall_activity("fall_backward")
    assert is_fall_activity("fall_lateral")
    assert not is_benign_activity("fall_forward")


# -----------------------------------------------------------------------------
# 4. Tests de save_session_npz / load_session_npz
# -----------------------------------------------------------------------------
def test_save_load_session_npz_roundtrip(tmp_path: Path):
    """Vérifie que sauvegarder puis charger un .npz préserve toutes les données de la session."""
    sess_id = str(uuid4())
    session = _make_session(sess_id, device_id="HK-1", label="fall_forward")

    save_session_npz(tmp_path, session)
    npz_file = tmp_path / f"{sess_id}.npz"
    assert npz_file.exists()

    loaded = load_session_npz(npz_file)
    assert loaded is not None
    assert loaded["session_id"] == sess_id
    assert loaded["device_id"] == "HK-1"
    assert loaded["label"] == "fall_forward"
    assert abs(loaded["duration_sec"] - 5.0) < 1e-6
    assert loaded["sample_count"] == 3
    assert len(loaded["readings"]) == 3

    # Vérification des valeurs IMU
    for i, r in enumerate(loaded["readings"]):
        assert abs(r["gz"] - float(i)) < 1e-4
        assert abs(r["ay"] - 9.8) < 1e-3


def test_load_session_npz_missing_file(tmp_path: Path):
    """load_session_npz doit retourner None si le fichier n'existe pas."""
    result = load_session_npz(tmp_path / "nonexistent.npz")
    assert result is None


def test_save_session_npz_empty_readings(tmp_path: Path):
    """save_session_npz doit gérer une session sans lectures (N=0) sans lever d'exception."""
    sess_id = str(uuid4())
    session = {
        "session_id": sess_id,
        "device_id": "HK-0",
        "label": "idle",
        "duration_sec": 0.0,
        "sample_count": 0,
        "readings": [],
    }
    save_session_npz(tmp_path, session)
    npz_file = tmp_path / f"{sess_id}.npz"
    assert npz_file.exists()

    loaded = load_session_npz(npz_file)
    assert loaded is not None
    assert loaded["sample_count"] == 0
    assert loaded["readings"] == []


# -----------------------------------------------------------------------------
# 5. Tests de migrate_json_to_npz
# -----------------------------------------------------------------------------
def test_migrate_json_to_npz(tmp_path: Path):
    """Vérifie la migration automatique d'un cache JSON monolithique vers des fichiers .npz."""
    sess_1_id = str(uuid4())
    sess_2_id = str(uuid4())

    cache_dir = tmp_path / "sessions"
    json_path = tmp_path / "sessions_cache.json"

    legacy_data = {
        "version": 1,
        "sessions": {
            sess_1_id: _make_session(sess_1_id, label="walk"),
            sess_2_id: _make_session(sess_2_id, label="fall_forward"),
        },
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f)

    migrated = migrate_json_to_npz(json_path, cache_dir)
    assert migrated == 2

    # Les deux fichiers .npz doivent exister
    assert (cache_dir / f"{sess_1_id}.npz").exists()
    assert (cache_dir / f"{sess_2_id}.npz").exists()

    # Le JSON original doit avoir été renommé en .bak
    assert (tmp_path / "sessions_cache.json.bak").exists()
    assert not json_path.exists()

    # Intégrité des données après migration
    loaded = load_session_npz(cache_dir / f"{sess_1_id}.npz")
    assert loaded is not None
    assert loaded["label"] == "walk"


def test_migrate_json_to_npz_missing_file(tmp_path: Path):
    """migrate_json_to_npz doit retourner 0 si le JSON source est absent."""
    cache_dir = tmp_path / "sessions"
    result = migrate_json_to_npz(tmp_path / "nonexistent.json", cache_dir)
    assert result == 0


# -----------------------------------------------------------------------------
# 6. Tests de sync_sessions_cache (Cache local .npz incrémental)
# -----------------------------------------------------------------------------
def test_sync_sessions_cache_synthetic():
    """Le mode synthetic doit retourner directement des sessions générées sans appeler DB/AWS."""
    sessions = sync_sessions_cache(cache_dir=Path("dummy_dir"), synthetic=True)
    assert len(sessions) > 0


def test_sync_sessions_cache_delta_logic(tmp_path: Path):
    """Vérifie la stratégie de delta sync : seules les nouvelles sessions sont téléchargées."""
    cache_dir = tmp_path / "sessions"
    sess_1_id = str(uuid4())
    sess_2_id = str(uuid4())

    # 1. Cache initial avec sess_1 pré-enregistrée en .npz
    save_session_npz(cache_dir, _make_session(sess_1_id, label="walk"))

    # 2. PostgreSQL retourne sess_1 et sess_2
    mock_s1 = _make_db_mock(sess_1_id, "dev-01", "walk")
    mock_s2 = _make_db_mock(sess_2_id, "dev-02", "fall_forward")

    # 3. Mock TelemetryService
    mock_reading = MagicMock()
    mock_reading.timestamp_epoch_us = 1000
    mock_reading.ax = 1.0
    mock_reading.ay = 28.0
    mock_reading.az = 0.5
    mock_reading.gx = 5.0
    mock_reading.gy = 1.0
    mock_reading.gz = 0.5

    mock_resp = MagicMock()
    mock_resp.readings = [mock_reading]
    mock_telemetry_svc = MagicMock()
    mock_telemetry_svc.get_session_readings.return_value = mock_resp

    with patch("app.db.database.SessionLocal") as mock_session_maker, \
         patch("app.services.telemetry_service.TelemetryService", return_value=mock_telemetry_svc):
        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = [mock_s1, mock_s2]
        mock_session_maker.return_value.__enter__.return_value = mock_db

        result_sessions = sync_sessions_cache(cache_dir, force_refresh=False)

    # Seul sess_2 devait être téléchargé depuis DynamoDB
    mock_telemetry_svc.get_session_readings.assert_called_once_with(
        device_id="dev-02",
        session_id=sess_2_id,
    )

    assert len(result_sessions) == 2
    session_ids = {s["session_id"] for s in result_sessions}
    assert session_ids == {sess_1_id, sess_2_id}

    # sess_2 doit avoir été sauvegardé en .npz sur disque
    assert (cache_dir / f"{sess_2_id}.npz").exists()


def test_sync_sessions_cache_purges_deleted_db_sessions(tmp_path: Path):
    """Vérifie que les sessions supprimées de PostgreSQL ont leur .npz supprimé du disque."""
    cache_dir = tmp_path / "sessions"
    sess_1_id = str(uuid4())
    sess_2_deleted_id = str(uuid4())
    sess_3_id = str(uuid4())

    # Cache initial avec 3 sessions
    for sid, label in [(sess_1_id, "walk"), (sess_2_deleted_id, "fall_forward"), (sess_3_id, "idle")]:
        save_session_npz(cache_dir, _make_session(sid, label=label))

    assert (cache_dir / f"{sess_2_deleted_id}.npz").exists()

    # PostgreSQL ne retourne plus que sess_1 et sess_3 (sess_2 supprimé)
    mock_s1 = _make_db_mock(sess_1_id, "dev-01", "walk")
    mock_s3 = _make_db_mock(sess_3_id, "dev-02", "idle")

    with patch("app.db.database.SessionLocal") as mock_session_maker:
        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = [mock_s1, mock_s3]
        mock_session_maker.return_value.__enter__.return_value = mock_db

        result_sessions = sync_sessions_cache(cache_dir, force_refresh=False)

    # Résultat en mémoire : sess_1 et sess_3 uniquement
    assert len(result_sessions) == 2
    res_ids = {s["session_id"] for s in result_sessions}
    assert res_ids == {sess_1_id, sess_3_id}
    assert sess_2_deleted_id not in res_ids

    # Vérification sur disque : le .npz de sess_2 doit avoir été supprimé
    assert not (cache_dir / f"{sess_2_deleted_id}.npz").exists()
    assert (cache_dir / f"{sess_1_id}.npz").exists()
    assert (cache_dir / f"{sess_3_id}.npz").exists()
    assert len(list(cache_dir.glob("*.npz"))) == 2


def test_sync_sessions_cache_batch_download(tmp_path: Path):
    """Vérifie que le téléchargement batch concurrent récupère plusieurs sessions en .npz."""
    cache_dir = tmp_path / "sessions"

    db_mocks = [_make_db_mock(f"sess-{i}", f"dev-{i}", "walk") for i in range(4)]

    mock_reading = MagicMock()
    mock_reading.timestamp_epoch_us = 1000
    mock_reading.ax = 0.0
    mock_reading.ay = 9.8
    mock_reading.az = 0.0
    mock_reading.gx = 0.0
    mock_reading.gy = 0.0
    mock_reading.gz = 0.0

    mock_resp = MagicMock()
    mock_resp.readings = [mock_reading]
    mock_telemetry_svc = MagicMock()
    mock_telemetry_svc.get_session_readings.return_value = mock_resp

    with patch("app.db.database.SessionLocal") as mock_session_maker, \
         patch("app.services.telemetry_service.TelemetryService", return_value=mock_telemetry_svc):
        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = db_mocks
        mock_session_maker.return_value.__enter__.return_value = mock_db

        result_sessions = sync_sessions_cache(cache_dir, force_refresh=True, batch_size=2)

    assert len(result_sessions) == 4
    assert mock_telemetry_svc.get_session_readings.call_count == 4

    # Tous les fichiers .npz doivent être présents sur disque
    assert cache_dir.exists()
    npz_files = list(cache_dir.glob("*.npz"))
    assert len(npz_files) == 4


def test_sync_sessions_cache_migrates_old_json(tmp_path: Path):
    """Vérifie que sync_sessions_cache migre automatiquement un ancien cache JSON."""
    cache_dir = tmp_path / "sessions"
    legacy_json = tmp_path / "sessions_cache.json"

    sess_id = str(uuid4())
    legacy_data = {
        "version": 1,
        "sessions": {sess_id: _make_session(sess_id, label="idle")},
    }
    with open(legacy_json, "w", encoding="utf-8") as f:
        json.dump(legacy_data, f)

    # PostgreSQL retourne la même session (déjà migrée)
    mock_s = _make_db_mock(sess_id, "dev-01", "idle")

    with patch("app.db.database.SessionLocal") as mock_session_maker:
        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = [mock_s]
        mock_session_maker.return_value.__enter__.return_value = mock_db

        result_sessions = sync_sessions_cache(cache_dir, force_refresh=False)

    assert len(result_sessions) == 1
    assert result_sessions[0]["session_id"] == sess_id
    assert result_sessions[0]["label"] == "idle"

    # Le fichier .npz doit avoir été créé
    assert (cache_dir / f"{sess_id}.npz").exists()

    # Le JSON original doit avoir été archivé en .bak
    assert (tmp_path / "sessions_cache.json.bak").exists()


# -----------------------------------------------------------------------------
# 7. Tests de train_and_benchmark & Exportation joblib
# -----------------------------------------------------------------------------
def test_train_and_benchmark_synthetic(tmp_path: Path):
    """Vérifie l'entraînement complet sur un mini-dataset et la sérialisation joblib."""
    sessions = generate_synthetic_sessions(n_per_class=6)
    X, y = build_dataset_from_sessions(sessions, window_size_sec=2.0, step_sec=1.0)

    model_out = tmp_path / "models" / "activity_classifier.joblib"

    package = train_and_benchmark(
        X=X,
        y=y,
        output_model_path=str(model_out),
        window_size_sec=2.0,
    )

    # Vérification du dictionnaire de package
    assert "model_name" in package
    assert "estimator" in package
    assert "feature_names" in package
    assert "classes" in package
    assert "fall_classes" in package
    assert "benign_classes" in package
    assert "idle" in package["benign_classes"]
    assert "fall_forward" in package["fall_classes"]
    assert "idle" not in package["fall_classes"]
    assert "metrics" in package
    assert package["window_size_sec"] == 2.0

    # Vérification du fichier sur disque
    assert model_out.exists()
    loaded_pkg = joblib.load(model_out)
    assert loaded_pkg["model_name"] == package["model_name"]
    assert "idle" in loaded_pkg["benign_classes"]

    # Test d'inférence avec l'estimateur rechargé
    preds = loaded_pkg["estimator"].predict(X.iloc[:5])
    assert len(preds) == 5
    for p in preds:
        assert p in package["classes"]


# -----------------------------------------------------------------------------
# 8. Tests CLI
# -----------------------------------------------------------------------------
def test_parse_args_defaults():
    """Vérifie les valeurs par défaut des options de la ligne de commande."""
    args = parse_args([])
    assert args.cache_dir == "scripts/data/sessions"
    assert args.output_model == "scripts/models/activity_classifier.joblib"
    assert args.window_size == 2.0
    assert args.window_step == 0.5
    assert args.batch_size == 8
    assert not args.force_refresh
    assert not args.synthetic


def test_parse_args_custom_batch_size():
    """Vérifie la personnalisation de la taille de lot concurrent."""
    args = parse_args(["--batch-size", "16"])
    assert args.batch_size == 16


def test_main_synthetic_execution(tmp_path: Path):
    """Vérifie l'exécution complète du CLI en mode synthétique avec le répertoire .npz."""
    cache_dir = tmp_path / "sessions"
    model_path = tmp_path / "model.joblib"

    ret_code = main(
        [
            "--synthetic",
            "--cache-dir",
            str(cache_dir),
            "--output-model",
            str(model_path),
            "--window-size",
            "2.0",
            "--window-step",
            "1.0",
            "--batch-size",
            "4",
        ]
    )
    assert ret_code == 0
    assert model_path.exists()
