#!/usr/bin/env python3
"""Heating MQTT VNC Bridge — Python rewrite of heating_mqtt_listener.sh.

Connects to a heating terminal via VNC, captures screenshots, OCRs them,
and publishes temperature data to MQTT. Accepts MQTT commands to set target temperature.
"""

import io
import json
import logging
import os
import re
import signal
import sys
import threading
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt
import pytesseract
from PIL import Image
from vncdotool import api as vnc_api

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------

def _run_with_timeout(func, timeout_seconds, *args, **kwargs):
    """Run func in a thread with a timeout enforced by a watchdog."""
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            error[0] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        raise TimeoutError(f"VNC operation timed out after {timeout_seconds}s")
    if error[0]:
        raise error[0]
    return result[0]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_pass: str
    vnc_host: str
    vnc_pass: str
    vncdo_cmd: str  # kept for env compat; not used (vncdotool API used directly)
    vnc_timeout: int = 60
    lock_timeout: int = 30
    listen_topic: str = "heating/#"
    response_topic: str = "consumption/heating"
    response_topic_read: str = "consumption/heating/read_result"
    response_topic_settemp: str = "consumption/heating/settemp_result"
    set_temp_topic: str = "heating/set_temp"

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        missing = []
        def req(name: str) -> str:
            val = os.environ.get(name)
            if not val:
                missing.append(name)
                return ""
            return val

        mqtt_port_str = os.environ.get("MQTT_PORT")
        if not mqtt_port_str:
            missing.append("MQTT_PORT")
            mqtt_port = 0
        else:
            try:
                mqtt_port = int(mqtt_port_str)
            except ValueError:
                raise RuntimeError(f"MQTT_PORT must be a number, got: {mqtt_port_str}")

        cfg = cls(
            mqtt_host=req("MQTT_HOST"),
            mqtt_port=mqtt_port,
            mqtt_user=req("MQTT_USER"),
            mqtt_pass=req("MQTT_PASS"),
            vnc_host=req("VNC_HOST"),
            vnc_pass=req("VNC_PASS"),
            vncdo_cmd=os.environ.get("VNCDO_CMD", ""),
            vnc_timeout=int(os.environ.get("VNC_TIMEOUT", "60")),
            lock_timeout=int(os.environ.get("LOCK_TIMEOUT", "30")),
            listen_topic=os.environ.get("LISTEN_TOPIC", "heating/#"),
            response_topic=os.environ.get("RESPONSE_TOPIC", "consumption/heating"),
            response_topic_read=os.environ.get("RESPONSE_TOPIC_READ", "consumption/heating/read_result"),
            response_topic_settemp=os.environ.get("RESPONSE_TOPIC_SETTEMP", "consumption/heating/settemp_result"),
            set_temp_topic=os.environ.get("SET_TEMP_TOPIC", "heating/set_temp"),
        )

        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Set them via /etc/heating-ocr.env or export them."
            )
        return cfg

# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

# Per-value crop regions: (x1, y1, x2, y2) on the raw 800x480 VNC screenshot.
# Each region contains just the number box for one sensor value.
# This avoids masking and full-text OCR — tesseract only sees digits.
VALUE_X1, VALUE_X2 = 425, 510

VALUE_REGIONS = {
    "heating": {
        "aussentemp":      (680, 18, 760, 50),  # outdoor temp, top-right corner
        "vorlauf":         (VALUE_X1, 70, VALUE_X2, 100),
        "ruecklauf":       (VALUE_X1, 105, VALUE_X2, 135),
        "vorlauf_fbh":     (VALUE_X1, 143, VALUE_X2, 170),
        "raumtemp":        (VALUE_X1, 178, VALUE_X2, 205),
        "heizkurveplus":   (VALUE_X1, 218, VALUE_X2, 245),
        "heizkurveminus":  (VALUE_X1, 253, VALUE_X2, 280),
        "raumtemp_soll":   (VALUE_X1, 288, VALUE_X2, 318),
        "absenktemp_soll": (VALUE_X1, 325, VALUE_X2, 355),
    },
    "water": {
        "speicher":        (VALUE_X1, 159, VALUE_X2, 189),
        "speicher_max":    (VALUE_X1, 195, VALUE_X2, 225),
    },
    "solar": {
        "kollektor":       (VALUE_X1, 100, VALUE_X2, 130),
        "speicher_unten":  (VALUE_X1, 138, VALUE_X2, 168),
        "solar_vorlauf":   (VALUE_X1, 175, VALUE_X2, 205),
        "solar_ruecklauf": (VALUE_X1, 213, VALUE_X2, 243),
    },
}


def ocr_value(img: Image.Image, region: tuple) -> str:
    """Crop a single value box, threshold, upscale, and OCR as digits only."""
    box = img.crop(region)
    box = box.convert("L")
    box = box.point(lambda x: 255 if x > 150 else 0)
    w, h = box.size
    box = box.resize((w * 3, h * 3), Image.LANCZOS)
    text = pytesseract.image_to_string(
        box, config="--psm 7 -c tessedit_char_whitelist=0123456789-"
    ).strip()
    return text


def extract_values(img: Image.Image, screen: str) -> dict:
    """Extract all sensor values from a screenshot by OCR-ing each value box."""
    regions = VALUE_REGIONS[screen]
    values = {}
    for name, region in regions.items():
        values[name] = ocr_value(img, region)
    return values


def build_result(
    heating_vals: dict,
    water_vals: dict,
    solar_vals: dict,
    status: str = "OK",
    operation: str = "read",
) -> dict:
    """Combine extracted values into the JSON result dict."""
    data = {"operation": operation}
    data.update(heating_vals)
    data.update(water_vals)
    data.update(solar_vals)
    data["status"] = status
    # Filter out empty values (match bash jq behaviour — keep 0 and "0")
    return {k: v for k, v in data.items() if v != "" and v is not None}

# ---------------------------------------------------------------------------
# VNC operations
# ---------------------------------------------------------------------------

def capture_screenshots(config: Config) -> tuple[Image.Image, Image.Image, Image.Image]:
    """Connect to VNC, navigate UI, capture 3 screenshots."""
    # TODO: vncdotool's api.connect() does not expose a nocursor option;
    # the factory's nocursor attribute is set up internally and cannot be
    # overridden without patching. Mouse cursor may appear in screenshots.
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="heating")
    try:
        heating_path = os.path.join(tmpdir, "heating.png")
        water_path = os.path.join(tmpdir, "water.png")
        solar_path = os.path.join(tmpdir, "solar.png")

        log.info("Connecting to VNC at %s", config.vnc_host)
        with vnc_api.connect(config.vnc_host, password=config.vnc_pass, timeout=config.vnc_timeout) as client:
            log.info("VNC connected, navigating to heating screen")
            client.mouseMove(780, 10); client.mousePress(1); client.pause(1)
            client.mouseMove(780, 200); client.mousePress(1); client.pause(1)

            # Heating
            client.mouseMove(200, 200); client.mousePress(1); client.pause(1)
            client.captureScreen(heating_path)
            log.info("Captured heating screenshot")

            # Water
            client.mouseMove(780, 200); client.mousePress(1); client.pause(1)
            client.mouseMove(300, 200); client.mousePress(1); client.pause(1)
            client.captureScreen(water_path)
            log.info("Captured water screenshot")

            # Solar
            client.mouseMove(780, 200); client.mousePress(1); client.pause(1)
            client.mouseMove(400, 200); client.mousePress(1); client.pause(1)
            client.captureScreen(solar_path)
            log.info("Captured solar screenshot")

            # Close menus
            client.mouseMove(780, 470); client.mousePress(1); client.mousePress(1)
            log.info("VNC navigation complete, disconnecting")

        heating_img = Image.open(heating_path).copy()
        water_img = Image.open(water_path).copy()
        solar_img = Image.open(solar_path).copy()
        return (heating_img, water_img, solar_img)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def set_vnc_temperature(config: Config, target_temp: str) -> None:
    """Connect to VNC and set the target temperature."""
    log.info("Connecting to VNC at %s to set temperature", config.vnc_host)
    with vnc_api.connect(config.vnc_host, password=config.vnc_pass, timeout=config.vnc_timeout) as client:
        log.info("VNC connected, navigating to temperature input")
        client.mouseMove(780, 10); client.mousePress(1); client.pause(1)
        client.mouseMove(780, 200); client.mousePress(1); client.pause(1)
        client.mouseMove(200, 200); client.mousePress(1); client.pause(1)
        client.mouseMove(450, 305); client.mousePress(1); client.pause(1)
        log.info("Typing temperature: %s", target_temp)
        for char in target_temp:
            client.keyPress(char)
        client.keyPress("enter")
        client.pause(1)
        client.mouseMove(780, 10); client.mousePress(1); client.pause(1)
        client.mouseMove(780, 470); client.mousePress(1); client.mousePress(1)
        log.info("Temperature input complete, disconnecting")

# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------

_operation_lock = threading.Lock()


def publish_json(mqtt_client, topic: str, data: dict):
    payload = json.dumps(data)
    log.info("Publishing to %s: %s", topic, payload)
    mqtt_client.publish(topic, payload, retain=True)


def run_reading(config: Config, mqtt_client):
    """Full read cycle: VNC capture -> process -> OCR -> publish."""
    log.info("=== Starting read cycle ===")
    publish_json(mqtt_client, config.response_topic_read,
                 build_result({}, {}, {}, "processing", "read"))

    try:
        heating_img, water_img, solar_img = _run_with_timeout(
            capture_screenshots, config.vnc_timeout, config)
    except TimeoutError as e:
        log.error(str(e))
        publish_json(mqtt_client, config.response_topic_read,
                     build_result({}, {}, {}, f"ERROR: {e}", "read"))
        return
    except Exception as e:
        log.error("VNC capture failed: %s", e)
        publish_json(mqtt_client, config.response_topic_read,
                     build_result({}, {}, {}, f"ERROR: VNC capture failed: {e}", "read"))
        return

    try:
        log.info("Extracting values from screenshots (per-value OCR)")
        heating_vals = extract_values(heating_img, "heating")
        water_vals = extract_values(water_img, "water")
        solar_vals = extract_values(solar_img, "solar")

        log.info("Heating: %s", heating_vals)
        log.info("Water: %s", water_vals)
        log.info("Solar: %s", solar_vals)
    except Exception as e:
        log.error("OCR failed: %s", e)
        publish_json(mqtt_client, config.response_topic_read,
                     build_result({}, {}, {}, f"ERROR: OCR failed: {e}", "read"))
        return

    result = build_result(heating_vals, water_vals, solar_vals)
    publish_json(mqtt_client, config.response_topic_read, result)
    log.info("=== Read cycle complete, %d values extracted ===", len(result) - 2)


def run_set_temperature(config: Config, mqtt_client, target_temp: str):
    """Set temperature via VNC, then do a reading."""
    publish_json(mqtt_client, config.response_topic_settemp,
                 build_result({}, {}, {}, "processing", "settemp"))

    try:
        _run_with_timeout(set_vnc_temperature, config.vnc_timeout, config, target_temp)
    except TimeoutError as e:
        log.error(str(e))
        publish_json(mqtt_client, config.response_topic_settemp,
                     build_result({}, {}, {}, f"ERROR: {e}", "settemp"))
        return
    except Exception as e:
        log.error("Temperature setting failed: %s", e)
        publish_json(mqtt_client, config.response_topic_settemp,
                     build_result({}, {}, {}, f"ERROR: {e}", "settemp"))
        return

    publish_json(mqtt_client, config.response_topic_settemp,
                 build_result({}, {}, {}, "OK", "settemp"))
    log.info("=== Temperature set to %s°C ===", target_temp)

    # After setting, do a reading
    log.info("Triggering read after temperature change")
    run_reading(config, mqtt_client)


def with_lock(func, config, mqtt_client, *args, error_topic=None, operation="read"):
    """Acquire lock with timeout, run function, release."""
    log.info("Acquiring VNC lock (timeout %ds)", config.lock_timeout)
    acquired = _operation_lock.acquire(timeout=config.lock_timeout)
    if not acquired:
        log.warning("VNC operation already in progress, rejecting %s request", operation)
        if error_topic:
            publish_json(mqtt_client, error_topic,
                         build_result({}, {}, {}, "ERROR: VNC operation already in progress", operation))
        return
    log.info("Lock acquired for %s", operation)
    try:
        func(config, mqtt_client, *args)
    finally:
        _operation_lock.release()
        log.info("Lock released")

# ---------------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties=None):
    config = userdata
    log.info("Connected to MQTT broker, subscribing to %s", config.listen_topic)
    client.subscribe(config.listen_topic)


def on_message(client, userdata, msg):
    config = userdata
    topic = msg.topic
    payload = msg.payload.decode().strip()

    if topic == "heating/request_read":
        if payload in ("read", "trigger", "update"):
            threading.Thread(
                target=with_lock,
                args=(run_reading, config, client),
                kwargs={"error_topic": config.response_topic_read, "operation": "read"},
                daemon=True,
            ).start()
        elif payload in ("exit", "quit", "stop"):
            log.info("Exit signal received. Stopping.")
            client.disconnect()
        else:
            log.warning("Unknown command on request_read: %s", payload)
    elif topic == config.set_temp_topic:
        if not payload or not re.match(r"^\d+(\.\d+)?$", payload):
            log.error("Invalid temperature value: %s", payload)
            publish_json(client, config.response_topic_settemp,
                         build_result({}, {}, {}, "ERROR: Invalid temperature value", "settemp"))
            return
        temp_int = int(float(payload))
        if temp_int < 5 or temp_int > 30:
            log.error("Temperature out of range: %s", payload)
            publish_json(client, config.response_topic_settemp,
                         build_result({}, {}, {}, "ERROR: Temperature out of range (5-30)", "settemp"))
            return
        log.info("Received temperature setting request: %s°C", payload)
        threading.Thread(
            target=with_lock,
            args=(run_set_temperature, config, client, payload),
            kwargs={"error_topic": config.response_topic_settemp, "operation": "settemp"},
            daemon=True,
        ).start()
    else:
        log.warning("Message on unknown topic: %s", topic)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )

    # Suppress noisy vncdotool/twisted internal logging
    logging.getLogger("vncdotool").setLevel(logging.WARNING)
    logging.getLogger("twisted").setLevel(logging.WARNING)

    config = Config.from_env()
    log.info("Configuration loaded: VNC=%s, MQTT=%s:%d", config.vnc_host, config.mqtt_host, config.mqtt_port)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=config)
    client.username_pw_set(config.mqtt_user, config.mqtt_pass)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(1, 60)

    def shutdown(signum, frame):
        log.info("Signal %s received, shutting down", signum)
        client.disconnect()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("Connecting to MQTT broker at %s:%d", config.mqtt_host, config.mqtt_port)
    client.connect(config.mqtt_host, config.mqtt_port)
    client.loop_forever()

    try:
        vnc_api.shutdown()
    except Exception:
        log.debug("vnc_api shutdown error (ignored)", exc_info=True)

    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
