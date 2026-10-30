# Machine Learning Activity Classifier Training Guide (`scripts/train_detector.py`)

This document details the architecture, CLI execution, and inference integration for the machine learning activity classification & fall detection training pipeline targeted at the embedded Edge service.

---

## 1. Script Overview & Objectives

The [`scripts/train_detector.py`](../scripts/train_detector.py) script accomplishes five key tasks:
1. **Dataset Consolidation**: Connects to Aurora PostgreSQL and Amazon DynamoDB to fetch labeled and validated Studio sessions.
2. **Incremental Local Cache**: Queries DynamoDB only for newly captured sessions, minimizing network latency, bandwidth, and AWS read costs.
3. **Biomechanical Feature Extraction**: Applies a rolling time-window to compute 16 orientation-invariant physical and biomechanical indicators (accelerometer and gyroscope magnitudes, jerk, variance, min/max).
4. **Model Benchmarking via Stratified Cross-Validation**: Evaluates `RandomForest`, `ExtraTrees`, `HistGradientBoosting`, and `LogisticRegression` using a Stratified 5-Fold Cross-Validation optimizing for `macro F1-score`.
5. **Production Serialization**: Exports the champion estimator alongside feature extraction metadata into a production-ready `joblib` artifact used by the Edge service.

---

## 2. Prerequisites & Setup

### A. Python Dependencies
The data science dependencies (`scikit-learn`, `pandas`, `numpy`, `joblib`, `m2cgen`) are declared in the main `dependencies` of `pyproject.toml`. There is no separate `ml` dependency group.

To install or synchronize your local virtual environment (including the ML dependencies):
```bash
uv sync
```

### B. AWS CLI Authentication (`aws login` / `aws sso login`)
When training on live telemetry data recorded in Amazon DynamoDB (`healthkicks_telemetry`), the script requires active AWS credentials.

1. **AWS IAM Identity Center (SSO)** (recommended):
   ```bash
   aws sso login
   # Or with an AWS CLI login alias:
   aws login
   ```
   If using a dedicated AWS CLI profile, define the environment variable:
   ```bash
   # Linux / macOS / Git Bash
   export AWS_PROFILE=your-profile-name

   # Windows PowerShell
   $env:AWS_PROFILE="your-profile-name"
   ```

2. **Standard IAM User / Access Keys**:
   ```bash
   aws configure
   ```

3. **Offline / Synthetic Alternative**:
   If AWS credentials are not available or if you want to test the ML pipeline locally without network access, run with `--synthetic` (no AWS or PostgreSQL connection needed):
   ```bash
   uv run python -m scripts.train_detector --synthetic
   ```

---

## 3. Command-Line Interface (CLI) Execution

Run the script via `uv run python -m scripts.train_detector`.

### A. Standard Execution (Incremental Caching & Concurrent Batch Download)
Queries PostgreSQL for new sessions, downloads missing telemetry frames from DynamoDB in parallel worker batches (default 8 simultaneous workers), and updates the local cache:
```bash
uv run python -m scripts.train_detector
```

### B. Custom Concurrency / Batch Size
Tune the number of simultaneous DynamoDB session downloads according to your network bandwidth and DynamoDB read capacity:
```bash
uv run python -m scripts.train_detector --batch-size 16
```

### C. Offline Synthetic / Demo Mode (No Database or AWS Required)
Generates a representative synthetic dataset covering 6 key biomechanical movement and posture classes (`walk`, `idle`, `fall_forward`, `stairs_up`, `stairs_down`, `fall_recovery`), ideal for pipeline verification or offline work:
```bash
uv run python -m scripts.train_detector --synthetic
```

### D. Force Full Refresh
Bypasses the local cache file and re-downloads all telemetry frames directly from DynamoDB in concurrent batches:
```bash
uv run python -m scripts.train_detector --force-refresh
```

### E. Advanced Customization (Window Sizing & Custom Paths)
```bash
uv run python -m scripts.train_detector \
  --window-size 2.0 \
  --window-step 0.5 \
  --batch-size 12 \
  --cache-path scripts/data/sessions_cache.json \
  --output-model scripts/models/activity_classifier.joblib
```

### CLI Options Reference:

| Option | Default | Description |
|---|---|---|
| `--batch-size` | `8` | Number of simultaneous session downloads from DynamoDB via a thread pool. |
| `--synthetic` | `False` | Generate synthetic biomechanical data for offline testing without database/AWS. |
| `--force-refresh` | `False` | Bypass local session cache and force a complete redownload from DynamoDB. |
| `--cache-path` | `scripts/data/sessions_cache.json` | Path to the JSON file storing the incremental session cache. |
| `--output-model` | `scripts/models/activity_classifier.joblib` | Destination filepath for the serialized champion model artifact. |
| `--window-size` | `2.0` | Duration of the rolling analysis window in seconds. |
| `--window-step` | `0.5` | Step / stride between consecutive windows in seconds (determines overlap). |

---

## 4. End-to-End MLOps Pipeline (`scripts/update_edge_model.py`)

While `scripts/train_detector.py` serves for model exploration and hyperparameter benchmarking, the production orchestration script [`scripts/update_edge_model.py`](../scripts/update_edge_model.py) binds the entire machine learning lifecycle to the embedded ESP32-S3 firmware:

```text
[ PostgreSQL + DynamoDB ]
         │ (Incremental caching)
         ▼
[ Feature Extraction ] (19 canonical indicators, 3.0s window / 0.5s stride)
         │
         ▼
[ Stratified 5-Fold CV ] (RandomForest, ExtraTrees, etc.)
         │
         ▼
[ Quality Gate Check ] (Macro F1 >= 75%)
         │
         ▼
[ C Transpilation via m2cgen ] ──► health-kicks-esp32-s3/include/activity_model_generated.h
         │
         ▼
[ PlatformIO Firmware Build ] ──► Flash / RAM validation (pio run)
```

### Key Capabilities & Workflow:
1. **Automated Data Sync**: Downloads missing raw telemetry sessions into `scripts/data/sessions/` via multithreaded workers.
2. **Class Filtering**: Automatically selects classes with sufficient training sessions (`--min-sessions-per-class 20`).
3. **Quality Gate**: Prevents degraded models from reaching firmware by asserting `macro F1 >= min_f1` (default 75%).
4. **Zero-Configuration C Transpilation**: Generates pure C decision trees compatible with embedded C++17 (`activity_model_generated.h`).
5. **Firmware Integrity Validation**: Runs PlatformIO (`pio run`) directly in `health-kicks-esp32-s3/` to guarantee RAM/Flash bounds before any git commit.
6. **Excluded Classes**: The legacy `stairs` label is excluded from training (superseded by `stairs_up` / `stairs_down`), and classes with fewer than `--min-sessions-per-class` sessions (e.g. `stumble_recover`) are skipped automatically. Neither is part of the `edge_ai` classification filter of the Studio API.

> **Model size limit**: m2cgen emits the whole forest as a single C++ function. If the generated header grows too large (roughly beyond 1.2 MB), the Xtensa linker fails with `dangerous relocation: l32r: literal target out of range`. Reduce the model size with `--max-sessions-per-class` (e.g. `30`) and re-run.

### CLI Usage:

```bash
# Standard production update (Sync + Train + Quality Gate + C Transpile + Build Check)
uv run python -m scripts.update_edge_model

# Fast offline demo using synthetic data (no AWS credentials or DB needed)
uv run python -m scripts.update_edge_model --synthetic

# Skip retraining and re-transpile existing model artifact + verify firmware build
uv run python -m scripts.update_edge_model --skip-train

# Custom Quality Gate threshold (e.g. 90% F1 score)
uv run python -m scripts.update_edge_model --min-f1 0.90

# Cap training data per class to keep the generated C header compilable
uv run python -m scripts.update_edge_model --max-sessions-per-class 30

# Skip PlatformIO firmware compilation check
uv run python -m scripts.update_edge_model --no-build-check
```

### CLI Options Reference (`scripts.update_edge_model`):

| Option | Default | Description |
|---|---|---|
| `--synthetic` | `False` | Generate synthetic dataset for offline pipeline validation without DB/AWS. |
| `--force-refresh` | `False` | Re-download all raw telemetry sessions from DynamoDB even if cached. |
| `--cache-dir` | `scripts/data/sessions` | Directory path where individual session JSON files are stored. |
| `--output-model` | `scripts/models/activity_classifier.joblib` | Path where champion `.joblib` model artifact is saved. |
| `--target-firmware-dir` | Auto-detected | Path to `health-kicks-esp32-s3` (detected automatically in workspace). |
| `--output-header` | `include/activity_model_generated.h` | Target C header relative to firmware directory. |
| `--window-size` | `3.0` | Rolling analysis window size in seconds (default: 3.0s). |
| `--window-step` | `0.5` | Sliding window step in seconds (default: 0.5s). |
| `--min-f1` | `0.75` | Quality gate: minimum macro F1 score required to accept champion model. |
| `--min-sessions-per-class` | `20` | Minimum recording sessions required to include a class in training. |
| `--max-sessions-per-class` | `None` | Maximum number of most recent sessions to include per class. |
| `--skip-train` | `False` | Skip training phase and re-transpile existing model artifact directly. |
| `--no-build-check` | `False` | Skip PlatformIO firmware build verification step. |

---

## 5. Running Inference with the Production Artifact

The exported artifact file (`scripts/models/activity_classifier.joblib`) packages the trained estimator alongside all feature engineering metadata. Here is an example showing how to run inference in a Python service or Edge script:

```python
import joblib
import pandas as pd
from scripts.train_detector import compute_window_features

# 1. Load the model artifact
artifact = joblib.load("scripts/models/activity_classifier.joblib")
model = artifact["estimator"]
feature_names = artifact["feature_names"]
model_name = artifact["model_name"]
classes = artifact["classes"]

print(f"Loaded model: {model_name} (Classes: {classes})")
fall_classes = artifact.get("fall_classes", [c for c in classes if str(c).startswith("fall_")])

# 2. Run inference on a real-time IMU window
# df_window contains the latest 50-100 real-time IMU frames (ax, ay, az, gx, gy, gz)
features_dict = compute_window_features(df_window)
X_new = pd.DataFrame([features_dict])[feature_names]

prediction = model.predict(X_new)[0]
probabilities = model.predict_proba(X_new)[0] if hasattr(model, "predict_proba") else None

print(f"Predicted activity: {prediction}")
if prediction in fall_classes or str(prediction).startswith("fall_"):
    print(f"ALERT: Critical fall detected ({prediction})! Triggering haptic stimulation.")
elif prediction == "idle":
    print("Subject is stationary / resting (benign state, no intervention needed).")
else:
    print(f"Normal daily movement ({prediction}).")
```


