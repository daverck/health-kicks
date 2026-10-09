"""Unit tests for scripts/update_edge_model.py MLOps pipeline."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.update_edge_model import (
    find_firmware_dir,
    main,
    parse_args,
    run_c_transpilation,
    run_platformio_build,
)


def test_parse_args_defaults() -> None:
    """Verifies default command line argument values."""
    args = parse_args([])
    assert args.synthetic is False
    assert args.force_refresh is False
    assert args.min_f1 == 0.85
    assert args.window_size == 3.0
    assert args.window_step == 0.5
    assert args.skip_train is False
    assert args.no_build_check is False


def test_parse_args_custom() -> None:
    """Verifies custom flags and options parsing."""
    args = parse_args([
        "--synthetic",
        "--min-f1", "0.92",
        "--window-size", "4.0",
        "--skip-train",
        "--no-build-check",
    ])
    assert args.synthetic is True
    assert args.min_f1 == 0.92
    assert args.window_size == 4.0
    assert args.skip_train is True
    assert args.no_build_check is True


def test_find_firmware_dir_custom_valid(tmp_path: Path) -> None:
    """Verifies finding firmware directory when valid custom path is provided."""
    ini_file = tmp_path / "platformio.ini"
    ini_file.write_text("[env:esp32s3]\n", encoding="utf-8")
    found = find_firmware_dir(str(tmp_path))
    assert found == tmp_path


def test_find_firmware_dir_custom_invalid(tmp_path: Path) -> None:
    """Raises FileNotFoundError when provided path lacks platformio.ini."""
    with pytest.raises(FileNotFoundError, match="platformio.ini"):
        find_firmware_dir(str(tmp_path))


def test_run_c_transpilation_missing_script(tmp_path: Path) -> None:
    """Returns False when export script does not exist."""
    fake_model = tmp_path / "model.joblib"
    fake_header = tmp_path / "header.h"
    res = run_c_transpilation(fake_model, fake_header, tmp_path)
    assert res is False


@patch("scripts.update_edge_model.subprocess.run")
def test_run_c_transpilation_success(mock_run: MagicMock, tmp_path: Path) -> None:
    """Returns True when C transpilation script exits with 0."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    script = tools_dir / "export_model_to_c.py"
    script.write_text("#!/usr/bin/env python\n", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=0, stdout="Success", stderr="")
    res = run_c_transpilation(tmp_path / "model.joblib", tmp_path / "out.h", tmp_path)
    assert res is True


@patch("scripts.update_edge_model.subprocess.run")
def test_run_c_transpilation_failure(mock_run: MagicMock, tmp_path: Path) -> None:
    """Returns False when C transpilation script exits with non-zero code."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    script = tools_dir / "export_model_to_c.py"
    script.write_text("#!/usr/bin/env python\n", encoding="utf-8")

    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error syntax")
    res = run_c_transpilation(tmp_path / "model.joblib", tmp_path / "out.h", tmp_path)
    assert res is False


@patch("scripts.update_edge_model.subprocess.run")
def test_run_platformio_build_success(mock_run: MagicMock, tmp_path: Path) -> None:
    """Parses RAM and Flash metrics from PlatformIO output."""
    sample_output = """
    Building in release mode
    RAM:   [=         ]  11.6% (used 37972 bytes from 327680 bytes)
    Flash: [===       ]  25.7% (used 808701 bytes from 3145728 bytes)
    ========================= [SUCCESS] Took 21.36 seconds =========================
    """
    mock_run.return_value = MagicMock(returncode=0, stdout=sample_output, stderr="")
    ok, stats = run_platformio_build(tmp_path)
    assert ok is True
    assert "RAM: 11.6%" in stats
    assert "Flash: 25.7%" in stats


@patch("scripts.update_edge_model.subprocess.run")
def test_run_platformio_build_failure(mock_run: MagicMock, tmp_path: Path) -> None:
    """Returns False with error log when PlatformIO build fails."""
    mock_run.return_value = MagicMock(returncode=1, stdout="Fatal compile error", stderr="")
    ok, stats = run_platformio_build(tmp_path)
    assert ok is False
    assert "failed with code 1" in stats


@patch("scripts.update_edge_model.find_firmware_dir")
@patch("scripts.update_edge_model.run_c_transpilation")
def test_main_skip_train_and_no_build_check(
    mock_transpile: MagicMock,
    mock_find_fw: MagicMock,
    tmp_path: Path,
) -> None:
    """Verifies main flow when training and build check are bypassed."""
    mock_find_fw.return_value = tmp_path
    mock_transpile.return_value = True

    model_file = tmp_path / "activity_classifier.joblib"
    model_file.write_bytes(b"dummy_model_bytes")

    ret = main([
        "--skip-train",
        "--no-build-check",
        "--output-model", str(model_file),
        "--target-firmware-dir", str(tmp_path),
    ])
    assert ret == 0
    mock_transpile.assert_called_once()

