# Machine Learning Activity Classifier Training & Debugging Guide (`scripts/train_detector.py`)

This document details the architecture, CLI execution, and step-by-step debugging in Visual Studio Code for the machine learning activity classification & fall detection training pipeline targeted at the embedded Edge service.

---

## 1. Script Overview & Objectives

The [`scripts/train_detector.py`](file:///f:/Programmation/health-kicks/scripts/train_detector.py) script accomplishes five key tasks:
1. **Dataset Consolidation**: Connects to Aurora PostgreSQL and Amazon DynamoDB to fetch labeled and validated Studio sessions.
2. **Incremental Local Cache**: Queries DynamoDB only for newly captured sessions, minimizing network latency, bandwidth, and AWS read costs.
3. **Biomechanical Feature Extraction**: Applies a rolling time-window to compute 16 orientation-invariant physical and biomechanical indicators (accelerometer and gyroscope magnitudes, jerk, variance, min/max).
4. **Model Benchmarking via Stratified Cross-Validation**: Evaluates `RandomForest`, `ExtraTrees`, `HistGradientBoosting`, and `LogisticRegression` using a Stratified 5-Fold Cross-Validation optimizing for `macro F1-score`.
5. **Production Serialization**: Exports the champion estimator alongside feature extraction metadata into a production-ready `joblib` artifact used by the Edge service.

---

## 2. Prerequisites & Setup

Heavy data science dependencies (`scikit-learn`, `pandas`, `numpy`, `joblib`) are isolated in the optional `ml` dependency group in [`pyproject.toml`](file:///f:/Programmation/health-kicks/pyproject.toml) to keep the production Docker container lightweight.

To install or synchronize your local virtual environment:
```bash
uv sync --group ml
```

---

## 3. Command-Line Interface (CLI) Execution

Run the script via `uv run python -m scripts.train_detector`.

### A. Standard Execution (Incremental Caching)
Queries PostgreSQL for new sessions, downloads missing telemetry frames from DynamoDB, and reuses existing cached sessions:
```bash
uv run python -m scripts.train_detector
```

### B. Offline Synthetic / Demo Mode (No Database or AWS Required)
Generates a representative synthetic dataset covering 5 key biomechanical movement and posture classes (`walk`, `idle`, `fall_forward`, `stairs`, `stumble_recover`), ideal for pipeline verification or offline work:
```bash
uv run python -m scripts.train_detector --synthetic
```

### C. Force Full Refresh
Bypasses the local cache file and re-downloads all telemetry frames directly from DynamoDB:
```bash
uv run python -m scripts.train_detector --force-refresh
```

### D. Advanced Customization (Window Sizing & Custom Paths)
```bash
uv run python -m scripts.train_detector \
  --window-size 2.0 \
  --window-step 0.5 \
  --cache-path scripts/data/sessions_cache.json \
  --output-model scripts/models/activity_classifier.joblib
```

### CLI Options Reference:

| Option | Default | Description |
|---|---|---|
| `--synthetic` | `False` | Generate synthetic biomechanical data for offline testing without database/AWS. |
| `--force-refresh` | `False` | Bypass local session cache and force a complete redownload from DynamoDB. |
| `--cache-path` | `scripts/data/sessions_cache.json` | Path to the JSON file storing the incremental session cache. |
| `--output-model` | `scripts/models/activity_classifier.joblib` | Destination filepath for the serialized champion model artifact. |
| `--window-size` | `2.0` | Duration of the rolling analysis window in seconds. |
| `--window-step` | `0.5` | Step / stride between consecutive windows in seconds (determines overlap). |

---

## 4. Step-by-Step Debugging in Visual Studio Code

The repository includes a preconfigured [`.vscode/launch.json`](file:///f:/Programmation/health-kicks/.vscode/launch.json) file with ready-to-use debugging targets.

### Step 1: Verify the Active Python Interpreter
1. Open VS Code in the root folder (`health-kicks`).
2. Open the Command Palette (`Ctrl+Shift+P` or `F1`).
3. Search and select: **`Python: Select Interpreter`**.
4. Choose the project virtual environment: **`Python (.venv)`** (`.\.venv\Scripts\python.exe`).

### Step 2: Set Breakpoints
1. Open [`scripts/train_detector.py`](file:///f:/Programmation/health-kicks/scripts/train_detector.py).
2. Click in the margin to the left of the line number where you want execution to pause:
   - For example around line **652** (`sessions_data = sync_sessions_cache(...)`) to inspect cache synchronization.
   - Around line **664** (`X, y = build_dataset_from_sessions(...)`) to inspect extracted feature windows.
   - Around line **510** (`for name, clf in models.items():`) to inspect individual classifier scores.
3. A red circle will appear on each selected line.

### Step 3: Launch the Debugger
1. Click the **Run and Debug** icon on the left sidebar (shortcut `Ctrl+Shift+D`).
2. In the dropdown at the top left, select one of the preconfigured targets:
   - **`Python: Entraîner Classifieur d'Activité (Synthétique)`** *(recommended for a fast run without network dependencies)*
   - **`Python: Entraîner Classifieur d'Activité (PostgreSQL & DynamoDB)`** *(against live databases)*
   - **`Python: Entraîner Classifieur d'Activité (Force Refresh)`** *(full resync)*
3. Press **`F5`** (or click the green Play ▶️ button).

### Step 4: Stepping Controls
Once execution pauses at your breakpoint, the floating debug toolbar will appear:

| Shortcut | Action | Description |
|---|---|---|
| **`F10`** | **Step Over** | Execute the current line and advance to the next line without stepping into sub-functions. |
| **`F11`** | **Step Into** | Step inside the called function to examine its logic line by line. |
| **`Shift+F11`** | **Step Out** | Complete execution of the current function and return to the caller. |
| **`F5`** | **Continue** | Resume normal execution until the next breakpoint is hit. |
| **`Ctrl+Shift+F5`** | **Restart** | Restart the script execution from the beginning. |
| **`Shift+F5`** | **Stop** | Terminate the debugging session. |

### Step 5: Inspect Variables & DataFrames
- **Variables Pane (Left sidebar)**: Live inspection of dictionary contents, numpy arrays (`y`), pandas DataFrames (`X`), model hyper-parameters, etc.
- **Hover**: Hover your mouse over any variable in the code editor to view its current value.
- **Debug Console**: Click the *Debug Console* tab at the bottom of VS Code to execute interactive Python code at the exact execution state (e.g. `X.head()`, `y.shape`, `df_window.describe()`).

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


