"""Local browser application for interactive city and base-station generation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import threading
import webbrowser
from dataclasses import asdict
from functools import lru_cache, partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import BaseStationConfig, config_from_mapping, load_config
from .pipeline import generate
from .profiles import CITY_PROFILES
from .runtime import RadioMapLookup

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = Path(__file__).resolve().parent / "web"
MAX_REQUEST_BYTES = 1_000_000
DOWNLOAD_FILES = {
    "scene.json",
    "base_station_manifest.json",
    "scene.obj",
    "preview.svg",
    "config.json",
    "manifest.json",
    "radio_manifest.json",
    "radio_volume.npz",
}

RADIO_QUALITY_PRESETS = {
    "quick": {"cell_size_m": 40.0, "samples_per_logical_sector": 4096, "max_depth": 4},
    "balanced": {"cell_size_m": 20.0, "samples_per_logical_sector": 65536, "max_depth": 8},
    "detailed": {"cell_size_m": 10.0, "samples_per_logical_sector": 1048576, "max_depth": 12},
}


def default_visualizer_output_root() -> Path:
    """Return a user-writable output directory for the local application."""
    override = os.environ.get("WCF_VISUALIZER_OUTPUT")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "WirelessCityFactory" / "outputs"
    return Path.home() / ".wireless_city_factory" / "outputs"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def preset_catalog(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    city_presets = {
        name: {
            "label": {
                "dense_highrise_grid": "密集高层网格",
                "irregular_midrise": "不规则中层城区",
                "compact_radial_lowrise": "紧凑放射低层",
                "sparse_suburban": "稀疏低层郊区",
            }[name],
            **_jsonable(profile),
        }
        for name, profile in CITY_PROFILES.items()
    }
    base_station_presets: dict[str, Any] = {}
    for key, label, filename, description in (
        (
            "low_band_wide_area",
            "低频广域通信站",
            "low_band_wide_area_1km.yaml",
            "700 MHz 广域通信工程参考；频段类别有政策与产业依据，具体射频和部署数值是仿真代理。",
        ),
        (
            "sub6_low_altitude_macro",
            "Sub-6 GHz 低空宏站",
            "sub6_low_altitude_macro_1km.yaml",
            "3.5 GHz 公网低空增强参考；采用 3GPP 宏站几何，功率、站高和密度是可配置仿真代理。",
        ),
        (
            "isac_49_experimental",
            "4.9 GHz 通感一体实验站",
            "isac_49_experimental_1km.yaml",
            "国内公开试点支持的实验类型；4.9 GHz 有案例依据，功率、阵列、站高和密度仍需标定。",
        ),
        (
            "mmwave_isac_experimental",
            "毫米波通感实验站",
            "mmwave_isac_experimental_1km.yaml",
            "面向重点区域的探索性通感预设；遮挡敏感，所有数值均按仿真代理处理。",
        ),
        (
            "uma_reference",
            "3GPP UMa 校准参考",
            "uma_reference_1km.yaml",
            "用于传播模型校准的 UMa 几何参考，不是国内低空基站产品标准。",
        ),
        (
            "umi_reference",
            "3GPP UMi 街谷校准参考",
            "umi_reference_1km.yaml",
            "用于传播模型校准的 UMi 街谷几何参考，不是国内低空基站产品标准。",
        ),
    ):
        config = load_config(project_root / "configs" / "base_station_profiles" / filename)
        base_station_presets[key] = {
            "label": label,
            "description": description,
            "values": _jsonable(asdict(config.city.base_stations)),
        }
    return {
        "city": city_presets,
        "base_station": base_station_presets,
        "custom_base_station_defaults": _jsonable(asdict(BaseStationConfig())),
        "capabilities": {
            "sionna_rt_installed": importlib.util.find_spec("sionna") is not None
            and importlib.util.find_spec("mitsuba") is not None,
            "radio_quality": RADIO_QUALITY_PRESETS,
        },
    }


def config_from_visualizer_payload(
    payload: dict[str, Any],
    *,
    output: Path | str = "examples/outputs/visualizer",
):
    if not isinstance(payload, dict) or not isinstance(payload.get("city"), dict):
        raise TypeError("Request must contain a city object.")
    seed = payload.get("seed", 20260821)
    city = payload["city"]
    size = city.get("size_m", [])
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("city.size_m must contain X and Y dimensions.")
    if any(float(value) > 2_000.0 for value in size):
        raise ValueError("The visualizer limits each city dimension to 2,000 m.")
    building = city.get("building", {})
    stations = city.get("base_stations", {})
    if float(building.get("density_per_km2", 0.0)) > 1_000.0:
        raise ValueError("Building density exceeds the visualizer limit of 1,000/km².")
    if float(stations.get("density_per_km2", 0.0)) > 100.0:
        raise ValueError("Base-station density exceeds the visualizer limit of 100/km².")
    radio = payload.get("radio", {})
    if not isinstance(radio, dict):
        raise TypeError("radio must be an object.")
    radio_enabled = bool(radio.get("enabled", False))
    quality = str(radio.get("quality", "quick"))
    if quality not in RADIO_QUALITY_PRESETS:
        raise ValueError(f"Unknown radio quality preset: {quality}")
    quality_values = RADIO_QUALITY_PRESETS[quality]
    carrier_frequency_hz = float(stations.get("carrier_frequency_hz", 4.9e9))
    bandwidth_hz = float(stations.get("bandwidth_hz", 100e6))
    measurement_height_m = float(radio.get("height_m", city.get("task", {}).get("altitude_m", 65.0)))
    if not 35.0 <= measurement_height_m <= 115.0:
        raise ValueError("Radio measurement height must be between 35 and 115 m.")
    raw = {
        "global": {
            "seed": seed,
            "name": str(payload.get("name", "interactive_city")),
            "output": str(output),
        },
        "city": city,
        "radio": {
            "enabled": radio_enabled,
            "preset": "smoke",
            "frequency_hz": carrier_frequency_hz,
            "bandwidth_hz": bandwidth_hz,
            "cell_size_m": quality_values["cell_size_m"],
            "heights_m": [measurement_height_m],
            "samples_per_logical_sector": quality_values["samples_per_logical_sector"],
            "max_depth": quality_values["max_depth"],
            "antenna_model": str(payload.get("antenna_model", "sector3-tr38901-8x8-dualpol")),
            "tx_power_dbm": stations.get("power_dbm", 44.0),
            "downtilt_deg": stations.get("downtilt_deg", 0.0),
            "sector_azimuths_deg": stations.get("sector_azimuths_deg", [0.0, 120.0, 240.0]),
        },
    }
    return config_from_mapping(raw)


def generate_visualizer_run(
    payload: dict[str, Any],
    project_root: Path = PROJECT_ROOT,
    *,
    output_root: Path | None = None,
) -> dict[str, Any]:
    config = config_from_visualizer_payload(
        payload,
        output=output_root if output_root is not None else "examples/outputs/visualizer",
    )
    result = generate(config, project_root=project_root)
    run_dir = Path(result["output_dir"])
    scene = json.loads((run_dir / "scene.json").read_text(encoding="utf-8"))
    stations = json.loads((run_dir / "base_station_manifest.json").read_text(encoding="utf-8"))
    downloads = {
        name: f"/api/runs/{result['run_id']}/{name}"
        for name in sorted(DOWNLOAD_FILES)
        if (run_dir / name).is_file()
    }
    response = {
        "status": "complete",
        "run_id": result["run_id"],
        "reused": bool(result.get("reused", False)),
        "scene": scene,
        "base_station_manifest": stations,
        "downloads": downloads,
    }
    if (run_dir / "radio_volume.npz").is_file():
        response["radio"] = _radio_layer(run_dir)
    else:
        response["radio"] = None
    return response


def _radio_layer(run_dir: Path) -> dict[str, Any]:
    """Return one compact, browser-ready horizontal slice from a radio volume."""
    import numpy as np

    with np.load(run_dir / "radio_volume.npz", allow_pickle=False) as volume:
        x_m = np.asarray(volume["x_m"], dtype=float)
        y_m = np.asarray(volume["y_m"], dtype=float)
        z_m = np.asarray(volume["z_m"], dtype=float)
        power_key = "rss_dbm" if "rss_dbm" in volume.files else "rsrp_dbm"
        rss = np.asarray(volume[power_key], dtype=float)
        sinr = np.asarray(volume["sinr_db"], dtype=float)
        transmitter_ids = [str(value) for value in volume["base_station_ids"].tolist()]
    z_index = 0
    rss_slice = rss[:, z_index, :, :]
    serving_indices = np.argmax(rss_slice, axis=0)
    best_rss = np.take_along_axis(rss_slice, serving_indices[None, :, :], axis=0)[0]
    serving_sinr = np.take_along_axis(
        sinr[:, z_index, :, :], serving_indices[None, :, :], axis=0
    )[0]
    serving_ids = np.asarray(transmitter_ids, dtype=object)[serving_indices]

    detection_threshold_dbm = -125.0
    valid_mask = np.isfinite(best_rss) & (best_rss >= detection_threshold_dbm)

    def summary(values: Any) -> dict[str, float | None]:
        finite = np.asarray(values, dtype=float)[valid_mask]
        finite = finite[np.isfinite(finite)]
        if not finite.size:
            return {key: None for key in ("minimum", "p05", "median", "p95", "maximum")}
        return {
            "minimum": float(np.min(finite)),
            "p05": float(np.percentile(finite, 5)),
            "median": float(np.median(finite)),
            "p95": float(np.percentile(finite, 95)),
            "maximum": float(np.max(finite)),
        }

    radio_manifest = json.loads((run_dir / "radio_manifest.json").read_text(encoding="utf-8"))
    radio_config = radio_manifest["metadata"]["config"]
    quality = next(
        (
            name
            for name, values in RADIO_QUALITY_PRESETS.items()
            if float(radio_config.get("cell_size_m", -1.0)) == values["cell_size_m"]
            and int(radio_config.get("samples_per_logical_sector", -1))
            == values["samples_per_logical_sector"]
            and int(radio_config.get("max_depth", -1)) == values["max_depth"]
        ),
        "custom",
    )
    return {
        "status": "complete",
        "backend": "sionna-rt-2.x",
        "height_m": float(z_m[z_index]),
        "frequency_hz": float(radio_config["frequency_hz"]),
        "bandwidth_hz": float(radio_config["bandwidth_hz"]),
        "cell_size_m": float(radio_config.get("cell_size_m", 0.0)),
        "quality": quality,
        "power_metric": "wideband_rss_dbm",
        "sinr_model": "full-buffer-reuse-1-all-sectors",
        "detection_threshold_dbm": detection_threshold_dbm,
        "x_m": x_m.tolist(),
        "y_m": y_m.tolist(),
        "best_rss_dbm": best_rss.ravel().tolist(),
        "best_rsrp_dbm": best_rss.ravel().tolist(),
        "serving_sinr_db": serving_sinr.ravel().tolist(),
        "serving_cell": np.where(valid_mask, serving_ids, None).ravel().tolist(),
        "valid_mask": valid_mask.ravel().tolist(),
        "coverage": {
            "valid_cells": int(np.count_nonzero(valid_mask)),
            "total_cells": int(valid_mask.size),
            "fraction": float(np.mean(valid_mask)),
        },
        "summary": {
            "rss": summary(best_rss),
            "rsrp": summary(best_rss),
            "sinr": summary(serving_sinr),
        },
    }


def _run_dir(output_root: Path, run_id: str) -> Path:
    if Path(run_id).name != run_id:
        raise ValueError("Invalid run ID.")
    run_dir = output_root / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Unknown run: {run_id}")
    return run_dir


@lru_cache(maxsize=2)
def _load_radio_lookup(run_dir: str) -> RadioMapLookup:
    return RadioMapLookup.load(run_dir)


def query_visualizer_radio(output_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("Request must be a JSON object.")
    run_dir = _run_dir(output_root, str(payload.get("run_id", "")))
    position = payload.get("position_m")
    if not isinstance(position, list) or len(position) != 3:
        raise ValueError("position_m must contain x, y and z.")
    return _load_radio_lookup(str(run_dir)).query(position).to_dict()


class VisualizerHandler(SimpleHTTPRequestHandler):
    server_version = "WirelessCityVisualizer/1.0"

    def __init__(self, *args: Any, project_root: Path, output_root: Path, **kwargs: Any) -> None:
        self.project_root = project_root
        self.output_root = output_root
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/presets":
            self._send_json(preset_catalog(self.project_root))
            return
        if path.startswith("/api/runs/"):
            self._serve_run_file(path)
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/generate", "/api/radio/query"}:
            self._send_json({"error": "Unknown API endpoint."}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("Request body is empty or too large.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/generate":
                result = generate_visualizer_run(
                    payload,
                    self.project_root,
                    output_root=self.output_root,
                )
            else:
                result = query_visualizer_radio(self.output_root, payload)
            self._send_json(result)
        except (json.JSONDecodeError, TypeError, ValueError, RuntimeError, OSError) as exc:
            self._send_json(
                {"error": str(exc), "error_type": type(exc).__name__},
                HTTPStatus.BAD_REQUEST,
            )

    def _serve_run_file(self, path: str) -> None:
        parts = [unquote(item) for item in path.split("/") if item]
        if len(parts) != 4 or parts[:2] != ["api", "runs"]:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        run_id, filename = parts[2], parts[3]
        if Path(run_id).name != run_id or filename not in DOWNLOAD_FILES:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        target = self.output_root / run_id / filename
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        content_type = {
            ".json": "application/json; charset=utf-8",
            ".obj": "text/plain; charset=ascii",
            ".svg": "image/svg+xml",
        }.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[visualizer] {self.address_string()} {format % args}")


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Prevent multiple Windows processes from sharing the same listening port."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def _server(
    host: str,
    port: int,
    project_root: Path,
    output_root: Path,
) -> tuple[ThreadingHTTPServer, int]:
    handler = partial(
        VisualizerHandler,
        project_root=project_root,
        output_root=output_root,
    )
    for candidate in range(port, port + 20):
        try:
            return ExclusiveThreadingHTTPServer((host, candidate), handler), candidate
        except OSError as exc:
            if exc.errno not in {48, 98, 10048} and getattr(exc, "winerror", None) != 10048:
                raise
    raise OSError(f"No available port between {port} and {port + 19}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AeroCity-RT local 3D visualizer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not WEB_ROOT.is_dir():
        raise FileNotFoundError(f"Visualizer assets are missing: {WEB_ROOT}")
    output_root = default_visualizer_output_root()
    server, port = _server(args.host, args.port, PROJECT_ROOT, output_root)
    url = f"http://{args.host}:{port}/"
    print(f"AeroCity-RT visualizer: {url}")
    print(f"Generated files: {output_root}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
