"""Helper to load ML benchmark metrics from committed JSON artifact or fallback joblib."""

import json
from pathlib import Path
from typing import Any


def load_champion_model_metrics() -> dict[str, Any]:
    """Retrieves serialized champion metrics (macro & per-class) from committed JSON or joblib."""
    # 1. Primary: committed lightweight JSON artifact (bundled inside Docker image)
    json_path = Path(__file__).resolve().parent / "model_metrics.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if "metrics" in data:
                return data["metrics"]
            return data
        except Exception:
            pass

    # 2. Fallback: local development joblib file
    try:
        import joblib

        model_path = Path("scripts/models/activity_classifier.joblib")
        if not model_path.exists():
            model_path = Path(__file__).resolve().parents[2] / "scripts" / "models" / "activity_classifier.joblib"

        if model_path.exists():
            pkg = joblib.load(model_path)
            return pkg.get("metrics", {})
    except Exception:
        pass

    return {}

