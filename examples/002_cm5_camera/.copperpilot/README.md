# CM5 Nano Camera Carrier Project Index

## Project Summary
High-density nano camera carrier board for the Raspberry Pi Compute Module 5 (CM5). Integrates an onboard Sony IMX219-D160 image sensor, high-speed display/video outputs (Mini-HDMI, 15-pin DSI FPC), dual-role USB routing with FSUSB42UMX switch, power management, Micro SD storage, and a 40-pin GPIO header into an ultra-compact 55.0 x 40.0 mm footprint.

## Key Specifications
- **Dimensions**: 55.0 x 40.0 mm with 3.0 mm corner radii and 4x M2.5 mounting holes.
- **Layer Stackup**: 6-layer controlled impedance stackup (100 ohm differential pairs for HDMI, CSI, DSI; 90 ohm differential pairs for USB).
- **Compute Module Interface**: Dual 100-pin Hirose DF40 mezzanine connectors H1A/H1B for CM5 (5V in, 3.3V/1.8V out, GPIO, I2C/SPI/UART, MIPI CSI/DSI, HDMI, nRPIBOOT, RUN_PG, GLOBAL_EN).
- **Onboard Camera**: IMX219-D160 with triple LDOs:
  - RT9193-28GB (VA2V8, 2.8V analog)
  - RT9193-18GB (VO1V8, 1.8V I/O)
  - RT9013-12 (VD1V2, 1.2V digital core)
  - 24.000 MHz crystal oscillator through 22 ohm damping resistor to MCLK
  - DMG1012T I2C level shift (3.3V to 1.8V)
  - MCF1210BF2-900T01 common-mode noise filters on CAM0_D0, CAM0_D1, and CAM0_CLK
- **Power Architecture**:
  - USB-C power input into CM5_5V with 5.1 kOhm CC pulldown resistors
  - MP1658GTF-Z synchronous step-down converter for PWR_3V3
  - DIO7003HCST5 load switches for HDMI_5V and SD_3.3V
  - STMPS2171STR power switch for USB_5V
- **USB Subsystem**:
  - FSUSB42UMX high-speed USB switch multiplexing USB0 between USB-C (rpiboot provisioning) and vertical USB-A (host peripheral), selected by USB_SEL jumper
- **Peripherals & Indicators**:
  - Mini-HDMI video output
  - 15-pin 1.0 mm pitch DSI FPC connector
  - Micro SD card slot (TF1)
  - 40-pin standard GPIO header (PI1)
  - User push-button Key1 connected to GPIO21
  - Power (PWR) and Activity (ACT) LEDs

## References
- `cm5-minima-rev3`: Cloned and upgraded in-place to KiCad 10.0 under `.copperpilot/references/cm5-minima-rev3/`.
