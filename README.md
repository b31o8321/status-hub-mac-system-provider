# status-hub-mac-system-provider

Status Hub market provider for macOS system health.

This plugin is the first general-purpose Status Hub market provider. It is not
business-specific, and it should be installable by any Status Hub user.

## Product Scope

The provider reports current Mac health as a compact Status Hub status source.
It does not try to replace full menu-bar monitors like Stats. Status Hub owns
the hub UI, while this provider owns collection, thresholds, and system-health
status mapping.

## Core Metrics

Temperature is part of the first core version, not a later enhancement.

| Metric | First version data | Status rule |
| --- | --- | --- |
| CPU | total usage, load average | `attention` when sustained usage exceeds threshold |
| Memory | used percent, available memory, swap | `attention` when pressure or swap exceeds threshold |
| Network | active interface, upload/download rate | `attention` when selected interface is disconnected |
| Battery / Energy | battery percent, charging state, low-power mode, thermal state | `attention` on low battery or serious thermal state |
| Disk | main volume used/free space | `attention` when free space is below threshold |
| Temperature | CPU/package temperature if available, max sensor temperature, source used | `attention` when temperature exceeds threshold, `unknown` when unsupported |

## Temperature Strategy

Temperature support must be explicit because macOS exposes it differently across
Intel, Apple Silicon, OS versions, and permission contexts.

Collection priority:

1. **SMC / IOKit sensor read**: preferred path for regular provider operation.
   It should not require sudo. The implementation should read known temperature
   keys and report the best available CPU/package value plus the max sensor.
2. **`powermetrics` fallback**: optional path when SMC access is unavailable.
   This may require elevated privileges and should not block the provider. If it
   is not permitted, report temperature as unavailable instead of failing the
   whole provider.
3. **Unsupported state**: emit the temperature item with `unknown` status and a
   clear subtitle, while the rest of the system metrics continue to work.

Temperature item requirements:

```json
{
  "id": "temperature",
  "title": "Temperature",
  "subtitle": "CPU 61 C · max 64 C · SMC",
  "status": "success",
  "value": "61 C",
  "detail": {
    "source": "smc",
    "cpuCelsius": "61",
    "maxCelsius": "64"
  }
}
```

If unavailable:

```json
{
  "id": "temperature",
  "title": "Temperature",
  "subtitle": "Temperature sensors unavailable on this Mac",
  "status": "unknown",
  "value": "--"
}
```

Temperature should affect the provider overall status only when it is
successfully collected and crosses a configured threshold. An unavailable
temperature reading should not make CPU, memory, network, battery, or disk look
failed.

## Status Hub Output

The first executable version is included in this repository:

```bash
STATUS_HUB_STATUS_FILE=/tmp/mac-system-status.json bin/mac-system-provider --once
cat /tmp/mac-system-status.json
```

Provider snapshot:

```json
{
  "status": "success",
  "summary": "CPU 18% · Mem 62% · Net 2.3 MB/s down · Battery 81% · Temp 61 C",
  "updatedAt": "2026-06-11T14:00:00+08:00",
  "items": [
    {
      "id": "cpu",
      "title": "CPU",
      "subtitle": "18% · load 2.1",
      "status": "success",
      "value": "18%"
    },
    {
      "id": "temperature",
      "title": "Temperature",
      "subtitle": "CPU 61 C · max 64 C · SMC",
      "status": "success",
      "value": "61 C"
    }
  ]
}
```

The provider should continue to use the Status Hub file protocol:

- `STATUS_HUB_PROVIDER_ID`
- `STATUS_HUB_STATUS_FILE`
- `STATUS_HUB_CONFIG_FILE`
- `STATUS_HUB_DATA_DIR`

`STATUS_HUB_CONFIG_FILE` and `STATUS_HUB_DATA_DIR` require matching Status Hub
support before marketplace release.

## Configuration

Default config:

```json
{
  "refreshIntervalSeconds": 3,
  "enabledMetrics": ["cpu", "memory", "network", "battery", "disk", "temperature"],
  "thresholds": {
    "cpuAttentionPercent": 85,
    "memoryAttentionPercent": 85,
    "diskFreeAttentionPercent": 10,
    "batteryLowPercent": 20,
    "temperatureAttentionCelsius": 85,
    "temperatureCriticalCelsius": 95
  },
  "network": {
    "interface": "auto"
  },
  "temperature": {
    "source": "auto",
    "allowPowermetricsFallback": false
  }
}
```

Temperature config:

- `source=auto`: try SMC first, then allowed fallbacks.
- `source=smc`: only use SMC/IOKit.
- `source=powermetrics`: use `powermetrics` only if the user explicitly enables
  it and permissions allow it.
- `allowPowermetricsFallback=false` by default, because the provider should not
  request elevated privileges implicitly.

## Status Mapping

Overall provider status is the highest-severity status from enabled metrics:

1. `failed`: provider cannot collect essential metrics or config is invalid.
2. `attention`: at least one enabled metric crosses an attention threshold.
3. `running`: reserved for long-running maintenance actions, normally unused.
4. `unknown`: optional metric unavailable and no higher status exists.
5. `success`: all enabled metrics are healthy.
6. `idle`: provider has not collected a sample yet.

Temperature-specific mapping:

- `< temperatureAttentionCelsius`: `success`
- `>= temperatureAttentionCelsius`: `attention`
- `>= temperatureCriticalCelsius`: `attention` with stronger subtitle wording
- unavailable sensor: `unknown`
- collector error affecting only temperature: temperature item `unknown`, not
  provider `failed`

## Status Hub Requirements

Status Hub needs these generic capabilities for this market plugin:

- Marketplace tab with general-purpose plugins only. GitLab Monitor and Intelli
  Integration Provider must stay custom GitHub plugins, not fixed marketplace
  entries.
- Provider config file support via `STATUS_HUB_CONFIG_FILE`.
- Provider data directory support via `STATUS_HUB_DATA_DIR`.
- Plugin lifecycle controls: install, update, restart, stop, uninstall.
- Richer item rendering: support optional `value` and `detail` fields.
- Clear handling for optional/unsupported metrics such as temperature.

## Implementation Notes

Recommended provider implementation language: Swift.

Reasoning:

- CPU, memory, network, battery, disk, thermal state, and SMC/IOKit are native
  macOS APIs.
- A Swift command-line provider avoids shipping Python dependencies.
- The Status Hub plugin protocol remains process/file based, so the Swift CLI
  stays decoupled from the Status Hub app.
