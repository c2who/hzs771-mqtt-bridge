# HZS 771 MQTT Bridge

Bridges a Sigmatek HZS 771 heating control panel to [Home Assistant](https://www.home-assistant.io/) via MQTT. The panel is only accessible through VNC, so this service captures screenshots, OCRs the temperature values, and publishes them as JSON over MQTT. It also accepts commands to set the target temperature.

The HZS 771 hardware is made by Sigmatek, but the UI running on it is specific to Kohlbacher (the property developer). The VNC interaction (click coordinates, crop regions, etc.) is tailored to that Kohlbacher interface. The general approach -- VNC screenshot, OCR, MQTT publish -- might be useful as a starting point for similar projects.

## How it works

1. Connects to the heating panel via VNC (`vncdotool`)
2. Captures a screenshot and crops individual value regions
3. Cleans up the images and runs OCR (`tesseract`)
4. Publishes the readings as JSON to MQTT
5. Listens for MQTT commands to adjust the target temperature via simulated VNC key presses

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Tesseract with the German language pack is also required:

```bash
sudo apt install tesseract-ocr tesseract-ocr-deu
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp heating-ocr.env.example heating-ocr.env
```

See `heating-ocr.env.example` for the available variables. All connection details (MQTT host/credentials, VNC host/password) are required.

## Running

Directly:

```bash
source venv/bin/activate
source heating-ocr.env
python3 heating_mqtt_bridge.py
```

As a systemd service (see `heating-ocr.service`):

```bash
sudo cp heating-ocr.service /etc/systemd/system/
sudo systemctl enable --now heating-ocr
```

## MQTT topics

| Topic | Direction | Description |
|---|---|---|
| `heating/request_read` | in | Trigger a temperature reading |
| `heating/set_temp` | in | Set target temperature (payload: number) |
| `consumption/heating` | out | JSON with current readings |

## License

MIT
