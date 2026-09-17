# CopperPilot Project Index: ESP32 Dev Board

## Project Description
Design of an ESP32 development board targeting KiCad 10.0.3.
The board features an Espressif ESP32-S3 module with native USB-C CDC/JTAG communication, 3.3V LDO power regulation, tactile reset and boot controls, status LEDs, and dual breadboard-compatible 20-pin breakout headers.

## Architecture Summary
- **MCU Module**: ESP32-S3-WROOM-1 (Xtensa dual-core LX7 @ 240MHz, 2.4GHz Wi-Fi + BLE 5.0).
- **USB Interface**: USB Type-C Receptacle (USB 2.0 16-pin) with dual 5.1kΩ CC pull-down resistors; direct D+/D- connection to ESP32-S3 USB OTG pins (GPIO19 / GPIO20).
- **Power Architecture**: 5V VBUS from USB-C regulated to 3.3V via 800mA+ LDO (SOT-223 package) with 10µF input and 22µF output ceramic filtering.
- **User Interface**: Reset (EN) pushbutton with RC debounce (10kΩ + 1µF/100nF), Boot (GPIO0) pushbutton, 3.3V Power LED (Red), User GPIO LED (Blue).
- **Expansion**: Dual 1x20 2.54mm pitch headers on 0.1" pitch, spaced for standard breadboards.

## References Ingested & Upgraded
1. `basic-esp32s3-dev-board` (atomic14, MIT License) — Validated KiCad reference for ESP32-S3 USB-C dev board architecture.
2. `esp32-c3-devboard` (21km43, Permissive) — Validated reference for USB-C and RF antenna layout considerations.
