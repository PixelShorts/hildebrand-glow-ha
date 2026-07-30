# Hildebrand Glow (Bright App) Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Home Assistant custom integration for UK SMETS2 smart meters using the Hildebrand Glow / Bright app API.

## About this fork

This is a fork of [McDon22/hildebrand-glow-ha](https://github.com/McDon22/hildebrand-glow-ha) with three fixes to how the integration reports data to the Energy dashboard (also submitted upstream as [PR #12](https://github.com/McDon22/hildebrand-glow-ha/pull/12)):

1. **"No data" / permanently zero sensors** -- the integration only ever queried "today", which Glowmarkt's API never has real data for (it has a documented ~24-48h processing delay). Now walks back to the most recent day that actually has data.
2. **Energy dashboard silently undercounting usage** -- the consumption sensors used `state_class: total_increasing` against a value that isn't a real running total, which could under-report multi-day totals. Now backed by a genuine persisted cumulative counter.
3. **Each day shown as one artificial spike instead of its real hourly shape** -- the dashboard's hourly chart now gets real hour-by-hour usage (Glowmarkt already provides 30-minute interval data), including a one-time backfill of the last 7 days on first install.

If you just want the stock integration, use the [upstream repo](https://github.com/McDon22/hildebrand-glow-ha) instead.

## Features

- **Easy Setup**: Configure through the Home Assistant UI - no YAML required
- **8 Sensors**: Electricity consumption, gas consumption, API costs, calculated daily costs with standing charges
- **Tariff Configuration**: Set your own electricity and gas rates including standing charges
- **Energy Dashboard Compatible**: Works with Home Assistant's Energy Dashboard
- **Auto Updates**: Data refreshes every 5 minutes

## Sensors Created

| Sensor | Description |
|--------|-------------|
| Electricity Consumption | Daily electricity usage (kWh) |
| Gas Consumption | Daily gas usage (kWh) |
| Electricity Cost (API) | Cost from Glowmarkt API |
| Gas Cost (API) | Cost from Glowmarkt API |
| Electricity Daily Cost | Calculated: (usage × rate) + standing charge |
| Gas Daily Cost | Calculated: (usage × rate) + standing charge |
| Total Daily Energy Cost | Combined electricity + gas costs |
| Daily Standing Charges | Total standing charges |

## Prerequisites

1. A UK SMETS2 smart meter
2. A [Hildebrand Bright app](https://www.hildebrand.co.uk/bright/) account linked to your smart meter
3. Home Assistant 2024.1.0 or newer

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add `https://github.com/PixelShorts/hildebrand-glow-ha` as an **Integration**
4. Search for "Hildebrand Glow" and click **Download**
5. Restart Home Assistant

### Manual Installation

1. Download this repository as a zip from [GitHub](https://github.com/PixelShorts/hildebrand-glow-ha)
2. Extract and copy the `custom_components/hildebrand_glow` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services**
2. Click **+ Add Integration**
3. Search for **"Hildebrand Glow"**
4. Enter your Bright app credentials (email and password)
5. Configure your tariff rates:
   - Electricity rate (£/kWh)
   - Electricity standing charge (£/day)
   - Gas rate (£/kWh)
   - Gas standing charge (£/day)

### Updating Tariff Rates

To update your tariff rates without reconfiguring:
1. Go to **Settings → Devices & Services**
2. Find the Hildebrand Glow integration
3. Click **Configure**
4. Update your rates

## Energy Dashboard Setup

To use with the Energy Dashboard:

1. Go to **Settings → Dashboards → Energy**
2. Add **Electricity grid consumption**: `sensor.smart_meter_electricity_consumption`
3. Add **Gas consumption**: `sensor.smart_meter_gas_consumption`
4. Set your tariff rates

## Data Availability

**Important**: Smart meter data from the Glowmarkt API typically has a 24-48 hour delay, so the sensors and Energy dashboard will always be a day or two behind, not real-time. Once data arrives, this fork imports its real hour-by-hour breakdown (plus a one-time backfill of the last 7 days on first install), so the historical shape on the Energy dashboard is accurate even though it's not live.

For near real-time data, consider using a Glow CAD/IHD device with local MQTT.

## Troubleshooting

### No resources found
- Ensure your Bright app account is properly linked to your smart meter
- Check that you can see data in the Bright mobile app

### Invalid credentials
- Verify your email and password work in the Bright mobile app
- Passwords are case-sensitive

### Sensors showing unavailable
- Check your internet connection
- The Glowmarkt API may be temporarily unavailable
- Check Home Assistant logs for specific error messages

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Hildebrand Technology](https://www.hildebrand.co.uk/) for the Glowmarkt API
- The Home Assistant community

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
