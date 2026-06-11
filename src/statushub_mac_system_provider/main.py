from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import time


DEFAULT_CONFIG = {
    "refreshIntervalSeconds": 3,
    "enabledMetrics": ["cpu", "memory", "network", "battery", "disk", "temperature"],
    "thresholds": {
        "cpuAttentionPercent": 85,
        "memoryAttentionPercent": 85,
        "memoryFreeAttentionPercent": 10,
        "diskFreeAttentionPercent": 5,
        "diskFreeAttentionGB": 10,
        "batteryLowPercent": 20,
        "temperatureAttentionCelsius": 85,
        "temperatureCriticalCelsius": 95,
    },
    "network": {"interface": "auto"},
    "temperature": {
        "source": "auto",
        "allowPowermetricsFallback": False,
    },
}

ROOT_DIR = Path(__file__).resolve().parents[2]
TEMPERATURE_HELPER_SOURCE = ROOT_DIR / "src" / "MacTemperatureHelper" / "main.swift"
TEMPERATURE_HELPER_BINARY = ROOT_DIR / ".build" / "mac-temperature-helper"

SEVERITY = {
    "idle": 0,
    "unknown": 0,
    "success": 1,
    "running": 3,
    "attention": 4,
    "failed": 5,
}


@dataclass
class NetworkSample:
    timestamp: float
    rx_bytes: int
    tx_bytes: int


class CollectorState:
    def __init__(self) -> None:
        self.network: NetworkSample | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Status Hub Mac system provider")
    parser.add_argument("--once", action="store_true", help="write status once and exit")
    parser.add_argument("--interval", type=float, default=None, help="poll interval in seconds")
    args = parser.parse_args(argv)

    status_file = Path(os.environ.get("STATUS_HUB_STATUS_FILE", "runtime/status.json")).expanduser()
    config_file = os.environ.get("STATUS_HUB_CONFIG_FILE")
    config = load_config(Path(config_file).expanduser() if config_file else None)
    interval = args.interval or float(config.get("refreshIntervalSeconds", 3))

    stopped = False
    state = CollectorState()

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopped:
        snapshot = build_snapshot(config, state)
        write_json(status_file, snapshot)
        if args.once:
            return 0
        time.sleep(max(interval, 1.0))

    return 0


def load_config(config_file: Path | None) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if not config_file or not config_file.exists():
        return config
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config
    return deep_merge(config, data)


def deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def build_snapshot(config: dict, state: CollectorState) -> dict:
    enabled = set(config.get("enabledMetrics", DEFAULT_CONFIG["enabledMetrics"]))
    thresholds = config.get("thresholds", {})
    items = []

    collectors = [
        ("cpu", collect_cpu),
        ("memory", collect_memory),
        ("network", lambda c, t, s: collect_network(c, t, s)),
        ("battery", collect_battery),
        ("disk", collect_disk),
        ("temperature", collect_temperature),
    ]

    for metric, collector in collectors:
        if metric not in enabled:
            continue
        try:
            items.append(collector(config, thresholds, state))
        except Exception as exc:  # keep one metric from breaking the provider
            items.append(
                {
                    "id": metric,
                    "title": title_for(metric),
                    "subtitle": f"collector error: {exc}",
                    "status": "unknown",
                    "value": "--",
                }
            )

    status = max((item.get("status", "unknown") for item in items), key=lambda s: SEVERITY.get(s, 2), default="idle")
    summary = build_summary(items)
    return {
        "status": status,
        "summary": summary,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": items,
    }


def collect_cpu(_config: dict, thresholds: dict, _state: CollectorState) -> dict:
    output = run(["/usr/bin/top", "-l", "1", "-n", "0"], timeout=5)
    match = re.search(r"CPU usage:\s+([\d.]+)% user,\s+([\d.]+)% sys,\s+([\d.]+)% idle", output)
    user = float(match.group(1)) if match else 0.0
    system = float(match.group(2)) if match else 0.0
    idle = float(match.group(3)) if match else 100.0
    usage = max(0.0, min(100.0, user + system if match else 100.0 - idle))
    load = os.getloadavg()
    status = "attention" if usage >= float(thresholds.get("cpuAttentionPercent", 85)) else "success"
    return {
        "id": "cpu",
        "title": "CPU",
        "subtitle": f"{usage:.0f}% · load {load[0]:.2f}",
        "status": status,
        "value": f"{usage:.0f}%",
        "detail": {
            "userPercent": f"{user:.1f}",
            "systemPercent": f"{system:.1f}",
            "idlePercent": f"{idle:.1f}",
            "load1": f"{load[0]:.2f}",
        },
    }


def collect_memory(_config: dict, thresholds: dict, _state: CollectorState) -> dict:
    page_size = int(run(["/usr/sbin/sysctl", "-n", "hw.pagesize"], timeout=3).strip() or "4096")
    total = int(run(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=3).strip())
    vm = run(["/usr/bin/vm_stat"], timeout=3)
    values = {}
    for line in vm.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        number = re.sub(r"[^0-9]", "", raw)
        if number:
            values[key.strip()] = int(number)
    free_pages = values.get("Pages free", 0) + values.get("Pages speculative", 0)
    compressed_pages = values.get("Pages occupied by compressor", 0)
    used = max(0, total - free_pages * page_size)
    used_percent = used / total * 100 if total else 0
    pressure_free_percent = read_memory_pressure_free_percent()
    attention_free = float(thresholds.get("memoryFreeAttentionPercent", 10))
    attention_used = float(thresholds.get("memoryAttentionPercent", 85))
    if pressure_free_percent is not None:
        status = "attention" if pressure_free_percent <= attention_free else "success"
        value = f"{pressure_free_percent:.0f}% free"
        subtitle = f"pressure ok · {pressure_free_percent:.0f}% free · {format_bytes(compressed_pages * page_size)} compressed"
    else:
        status = "attention" if used_percent >= attention_used else "success"
        value = f"{used_percent:.0f}%"
        subtitle = f"{used_percent:.0f}% used · {format_bytes(free_pages * page_size)} free"
    return {
        "id": "memory",
        "title": "Memory",
        "subtitle": subtitle,
        "status": status,
        "value": value,
        "detail": {
            "total": format_bytes(total),
            "used": format_bytes(used),
            "free": format_bytes(free_pages * page_size),
            "compressed": format_bytes(compressed_pages * page_size),
            "pressureFreePercent": f"{pressure_free_percent:.0f}" if pressure_free_percent is not None else "unknown",
        },
    }


def read_memory_pressure_free_percent() -> float | None:
    output = run(["/usr/bin/memory_pressure"], timeout=5)
    match = re.search(r"System-wide memory free percentage:\s+([\d.]+)%", output)
    return float(match.group(1)) if match else None


def collect_network(config: dict, _thresholds: dict, state: CollectorState) -> dict:
    interface_setting = config.get("network", {}).get("interface", "auto")
    sample = read_network_sample(interface_setting)
    previous = state.network
    state.network = sample
    down_rate = 0.0
    up_rate = 0.0
    if previous and sample.timestamp > previous.timestamp:
        elapsed = sample.timestamp - previous.timestamp
        down_rate = max(0.0, (sample.rx_bytes - previous.rx_bytes) / elapsed)
        up_rate = max(0.0, (sample.tx_bytes - previous.tx_bytes) / elapsed)
    return {
        "id": "network",
        "title": "Network",
        "subtitle": f"down {format_bytes(down_rate)}/s · up {format_bytes(up_rate)}/s",
        "status": "success",
        "value": f"{format_bytes(down_rate)}/s",
        "detail": {
            "downloadPerSecond": format_bytes(down_rate),
            "uploadPerSecond": format_bytes(up_rate),
            "rxBytes": str(sample.rx_bytes),
            "txBytes": str(sample.tx_bytes),
        },
    }


def read_network_sample(interface_setting: str) -> NetworkSample:
    output = run(["/usr/sbin/netstat", "-ibn"], timeout=5)
    totals = defaultdict(lambda: [0, 0])
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10:
            continue
        name = parts[0]
        if interface_setting != "auto" and name != interface_setting:
            continue
        if interface_setting == "auto" and not (name.startswith("en") or name.startswith("utun")):
            continue
        try:
            ibytes = int(parts[6])
            obytes = int(parts[9])
        except ValueError:
            continue
        totals[name][0] = max(totals[name][0], ibytes)
        totals[name][1] = max(totals[name][1], obytes)
    rx = sum(value[0] for value in totals.values())
    tx = sum(value[1] for value in totals.values())
    return NetworkSample(time.time(), rx, tx)


def collect_battery(_config: dict, thresholds: dict, _state: CollectorState) -> dict:
    output = run(["/usr/bin/pmset", "-g", "batt"], timeout=5)
    percent_match = re.search(r"(\d+)%", output)
    percent = int(percent_match.group(1)) if percent_match else None
    charging = "AC Power" in output or "charging" in output
    status = "unknown"
    value = "--"
    subtitle = "battery unavailable"
    if percent is not None:
        low = percent <= int(thresholds.get("batteryLowPercent", 20)) and not charging
        status = "attention" if low else "success"
        value = f"{percent}%"
        subtitle = f"{percent}% · {'charging' if charging else 'battery'}"
    thermal = run(["/usr/bin/pmset", "-g", "therm"], timeout=5, check=False)
    return {
        "id": "battery",
        "title": "Battery",
        "subtitle": subtitle,
        "status": status,
        "value": value,
        "detail": {
            "charging": str(charging).lower(),
            "thermal": " ".join(thermal.split())[:160],
        },
    }


def collect_disk(_config: dict, thresholds: dict, _state: CollectorState) -> dict:
    usage = read_user_visible_storage_usage()
    if usage is None:
        raw = shutil.disk_usage("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/")
        usage = {
            "total": raw.total,
            "used": raw.used,
            "available": raw.free,
            "containerFree": raw.free,
            "volume": "Data volume",
        }
    used_percent = usage["used"] / usage["total"] * 100 if usage["total"] else 0
    free_percent = usage["available"] / usage["total"] * 100 if usage["total"] else 0
    free_attention_percent = float(thresholds.get("diskFreeAttentionPercent", 5))
    free_attention_bytes = float(thresholds.get("diskFreeAttentionGB", 10)) * 1024 * 1024 * 1024
    status = "attention" if free_percent <= free_attention_percent or usage["available"] <= free_attention_bytes else "success"
    return {
        "id": "disk",
        "title": "Disk",
        "subtitle": f"{usage['volume']} · {format_storage_bytes(usage['used'])} used of {format_storage_bytes(usage['total'])}",
        "status": status,
        "value": f"{used_percent:.0f}%",
        "detail": {
            "volume": usage["volume"],
            "total": format_storage_bytes(usage["total"]),
            "userVisibleUsed": format_storage_bytes(usage["used"]),
            "userVisibleAvailable": format_storage_bytes(usage["available"]),
            "containerFree": format_storage_bytes(usage["containerFree"]),
        },
    }


def read_user_visible_storage_usage() -> dict | None:
    output = run(["/usr/sbin/diskutil", "apfs", "list", "-plist"], timeout=8)
    if not output:
        return None
    try:
        data = plistlib.loads(output.encode("utf-8"))
    except Exception:
        return None

    containers = data.get("Containers", [])
    if not containers:
        return None
    container = max(containers, key=lambda item: int(item.get("CapacityCeiling", 0)))
    total = int(container.get("CapacityCeiling", 0))
    container_free = int(container.get("CapacityFree", 0))
    if total <= 0:
        return None

    included_roles = {"Data", "System", "Recovery"}
    used = 0
    for volume in container.get("Volumes", []):
        roles = set(volume.get("Roles", []))
        if roles & included_roles:
            used += int(volume.get("CapacityInUse", 0))
    if used <= 0:
        return None
    return {
        "total": total,
        "used": used,
        "available": max(0, total - used),
        "containerFree": container_free,
        "volume": "Macintosh HD",
    }


def collect_temperature(config: dict, thresholds: dict, _state: CollectorState) -> dict:
    temp_config = config.get("temperature", {})
    source = temp_config.get("source", "auto")
    allow_powermetrics = bool(temp_config.get("allowPowermetricsFallback", False))
    thermal_pressure = read_thermal_pressure()
    reading = None
    native_reading = None
    source_used = "unavailable"

    if source in {"auto", "iohid", "native"}:
        native_reading = read_native_temperature()
        if native_reading is not None:
            reading = float(native_reading.get("cpuCelsius") or native_reading.get("maxCelsius"))
            source_used = str(native_reading.get("source", "iohid"))

    if source in {"auto", "powermetrics"} and (source == "powermetrics" or allow_powermetrics):
        powermetrics_reading = read_powermetrics_temperature()
        if reading is None and powermetrics_reading is not None:
            reading = powermetrics_reading
            source_used = "powermetrics"

    if reading is None:
        if thermal_pressure is not None:
            thermal_status = "attention" if thermal_pressure["warning"] else "success"
            return {
                "id": "temperature",
                "title": "Thermal",
                "subtitle": thermal_pressure["summary"],
                "status": thermal_status,
                "value": thermal_pressure["value"],
                "detail": thermal_pressure["detail"],
            }
        return {
            "id": "temperature",
            "title": "Thermal",
            "subtitle": "thermal state unavailable without a native sensor collector",
            "status": "unknown",
            "value": "--",
            "detail": {
                "source": source_used,
                "hint": "CPU temperature needs a native SMC/IOKit helper on Apple Silicon",
            },
        }

    attention = float(thresholds.get("temperatureAttentionCelsius", 85))
    critical = float(thresholds.get("temperatureCriticalCelsius", 95))
    status = "attention" if reading >= attention else "success"
    label = "critical" if reading >= critical else "high" if reading >= attention else "normal"
    max_celsius = float(native_reading.get("maxCelsius")) if native_reading is not None else reading
    sensor_count = int(native_reading.get("sensorCount", 0)) if native_reading is not None else 0
    subtitle = f"CPU {reading:.0f} C · max {max_celsius:.0f} C · {label} · {source_used}"
    detail = {
        "source": source_used,
        "cpuCelsius": f"{reading:.1f}",
        "maxCelsius": f"{max_celsius:.1f}",
        "sensorCount": str(sensor_count) if sensor_count else "unknown",
        "attentionCelsius": f"{attention:.1f}",
        "criticalCelsius": f"{critical:.1f}",
    }
    if native_reading is not None:
        sensors = native_reading.get("sensors") or []
        if sensors:
            detail["topSensors"] = ", ".join(
                f"{sensor.get('name', 'sensor')} {float(sensor.get('celsius', 0)):.0f} C"
                for sensor in sensors[:5]
            )
    return {
        "id": "temperature",
        "title": "Temperature",
        "subtitle": subtitle,
        "status": status,
        "value": f"{reading:.0f} C",
        "detail": detail,
    }


def read_native_temperature() -> dict | None:
    helper = ensure_temperature_helper()
    if helper is None:
        return None
    output = run([str(helper)], timeout=5, check=False)
    if not output.strip():
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    max_celsius = data.get("maxCelsius")
    if not isinstance(max_celsius, (int, float)) or not 0 < float(max_celsius) < 130:
        return None
    cpu_celsius = data.get("cpuCelsius")
    if cpu_celsius is not None and not isinstance(cpu_celsius, (int, float)):
        data["cpuCelsius"] = None
    return data


def ensure_temperature_helper() -> Path | None:
    if (
        TEMPERATURE_HELPER_BINARY.exists()
        and TEMPERATURE_HELPER_SOURCE.exists()
        and TEMPERATURE_HELPER_BINARY.stat().st_mtime >= TEMPERATURE_HELPER_SOURCE.stat().st_mtime
    ):
        return TEMPERATURE_HELPER_BINARY
    swiftc = shutil.which("swiftc")
    if not swiftc or not TEMPERATURE_HELPER_SOURCE.exists():
        return None
    TEMPERATURE_HELPER_BINARY.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            swiftc,
            str(TEMPERATURE_HELPER_SOURCE),
            "-framework",
            "IOKit",
            "-framework",
            "CoreFoundation",
            "-o",
            str(TEMPERATURE_HELPER_BINARY),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return TEMPERATURE_HELPER_BINARY


def read_thermal_pressure() -> dict | None:
    output = run(["/usr/bin/pmset", "-g", "therm"], timeout=5, check=False)
    if not output.strip():
        return None
    normalized = " ".join(output.split())
    has_warning = "warning" in normalized.lower() and "no thermal warning" not in normalized.lower()
    has_performance_warning = (
        "performance warning" in normalized.lower()
        and "no performance warning" not in normalized.lower()
    )
    warning = has_warning or has_performance_warning
    value = "warning" if warning else "ok"
    summary = "thermal warning" if warning else "thermal ok"
    return {
        "value": value,
        "summary": summary,
        "warning": warning,
        "detail": {
            "source": "pmset therm",
            "raw": normalized[:180],
            "cpuTemperature": "native helper unavailable",
        },
    }


def read_powermetrics_temperature() -> float | None:
    output = run(
        ["/usr/bin/powermetrics", "--samplers", "smc", "--sample-count", "1", "--sample-rate", "1000"],
        timeout=4,
        check=False,
    )
    values = []
    for match in re.finditer(r"(-?\d+(?:\.\d+)?)\s*C", output):
        value = float(match.group(1))
        if 0 < value < 130:
            values.append(value)
    return max(values) if values else None


def build_summary(items: list[dict]) -> str:
    values = []
    labels = {
        "cpu": "CPU",
        "memory": "Mem",
        "network": "Net",
        "battery": "Battery",
        "disk": "Disk",
        "temperature": "Temp",
    }
    for item in items:
        value = item.get("value")
        if value and value != "--":
            values.append(f"{labels.get(item['id'], item['title'])} {value}")
    return " · ".join(values) if values else "Mac system provider is running"


def title_for(metric: str) -> str:
    return {
        "cpu": "CPU",
        "memory": "Memory",
        "network": "Network",
        "battery": "Battery",
        "disk": "Disk",
        "temperature": "Temperature",
    }.get(metric, metric.title())


def run(command: list[str], timeout: float, check: bool = True) -> str:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if check and completed.returncode != 0:
        return ""
    return completed.stdout or ""


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_storage_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1000 or unit == units[-1]:
            if unit == "B":
                return f"{size:.0f} {unit}"
            if size >= 100:
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=f".{path.name}.",
    ) as handle:
        handle.write(data)
        handle.write("\n")
        tmp_path = Path(handle.name)
    tmp_path.replace(path)
