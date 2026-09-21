---
name: CM5 Nano Camera Carrier
overview: Reverse-engineer the Waveshare CM4-NANO-C nano camera carrier into a Raspberry Pi Compute Module 5 (CM5) carrier on an exact 55x40 mm 6-layer board with dual Hirose DF40 connectors. Preserves mechanical dimensions, on-board IMX219-D160 camera interface, Mini-HDMI, 15-pin DSI, USB, MicroSD, and 40-pin GPIO while retaining published NANO-C net names.
todos:
  - id: prepare-libraries
    content: Set up symbol, footprint, and 3D model libraries for CM5 DF40 and all discrete/peripheral components.
    status: completed
  - id: schematic-capture
    content: Complete schematic capture according to the netlist contract and BOM using exact net names.
    status: completed
  - id: run-erc
    content: Run electrical rules check (ERC) on schematic and resolve any reported errors.
    status: open
  - id: board-outline-and-placement
    content: Establish 55x40mm Edge.Cuts with 3mm radii, M2.5 mounting holes, and lock all 90 footprints at specified coordinates.
    status: open
  - id: stackup-netclasses-and-zones
    content: Configure 6-layer SIG-GND-SIG-PWR-GND-SIG stackup, impedance netclasses (100R diff, 90R USB, power widths), and power/ground copper zones.
    status: open
  - id: staged-pcb-routing
    content: Execute staged routing prioritizing high-speed differential pairs (HDMI0, CSI, CAM0, DSI1, USB) followed by power distribution and 40-pin GPIO.
    status: open
  - id: run-drc
    content: Run design rules check (DRC) and verify zero unrouted nets, clearance, or manufacturing rule violations.
    status: open
  - id: model-assignment-and-3d-render
    content: Audit and verify 3D model alignments, orientations, and render full board assembly.
    status: open
  - id: 3d-pose-audit
    content: Audit 3D poses for camera, CM4, DF40, Mini-HDMI, Micro SD against the pose table.
    status: open
overall_status: open
---

# CM5 Nano Camera Carrier Implementation Plan

## Mission
Reverse-engineer the Waveshare **CM4-NANO-C** nano camera carrier as a CM5 board. The product to clone is https://www.waveshare.com/wiki/CM4-NANO-C (also https://www.waveshare.com/product/cm4-nano-c.htm ). Download the published schematic, mechanical drawing, and 3D render from the wiki Resources section and derive pin functions, connector choice, and outline from those. Retarget the mezzanine to **CM5** dual Hirose DF40 (H1A/H1B). Keep the published NANO-C net names (`CM4_5V`, `CM4_3V3`, `CM4_1V8`, `1V8`, `2V8`, `1V2`, `CSI_*`, `CAM0_*`, `HDMI0_*`, `DSI1_*`) so the schematic netlist matches the NANO-C carrier.

## Public references (study these DURING planning; cite URLs in the plan)
You MUST find and list multiple CM5/CM4 carrier references before freezing symbols or coordinates. Start here, then search for more:
1. PRIMARY product: Waveshare CM4-NANO-C wiki + product page. This is the board we are electrically and mechanically cloning (55 x 40 mm, IMX219-D160, Mini-HDMI, 15-pin DSI, USB-A, USB-C power+program, Micro SD, 40-pin GPIO, GPIO21 key, BOOT, ACT/PWR LEDs). There is NO Ethernet, M.2, or PoE on this nano camera board.
2. CLOSEST open KiCad CM4/CM5 DF40 carrier: https://github.com/rapidanalysis/xerxes — use this for Hirose DF40C-100DS footprints, CM5 pinout, and KiCad library practice. Do NOT copy Xerxes features (Gigabit Ethernet, M.2, PoE+, Qwiic, fan) onto this 55 x 40 mm camera carrier.
3. KiCad demo `cm5_minima` (KiCad source demos + https://github.com/piecol/CM5_MINIMA_REV3 ) — 6-layer CM5 carrier, USB-C PD, HDMI, CSI/DSI. Size is 54 x 57 mm, NOT our outline. Use for CM5 stackup/pinout only.
4. Raspberry Pi Compute Module 5 datasheet and CM5IO board: confirm DF40 H1A/H1B pin compatibility with the CM4 nets used here (CSI0, DSI1, HDMI0, USB2.0, SD, GPIO 2-27, 5V/3V3/1V8).
5. Additional public CM5/CM4 carriers to cite in the plan (already selected; do not block on live search). If any conflicts with Waveshare CM4-NANO-C, Waveshare wins for this clone:
   - Raspberry Pi Compute Module 5 IO Board + CM5 datasheet (https://www.raspberrypi.com/documentation/computers/compute-module.html)
   - Makerforge CM5 carrier design guide (https://www.makerforge.tech/posts/cm5-carrier-basics/)
   - ShawnHymel CM4 carrier KiCad template (https://github.com/ShawnHymel/rpi-cm4-carrier-template)
   - Sentinel Core CM5 mini-ITX (https://github.com/abg-research/sentinel-core) for CM5 DF40 pinout only — not the 55 x 40 mm outline.

## Envelope (must be exact; do not invent a different shape)
Board-local origin = south-west corner of Edge.Cuts.
- Outline: 55.0 x 40.0 mm rectangle, 3.0 mm corner radii, Edge.Cuts width 0.1 mm.
- 4x M2.5 mounting holes at 3.5,3.5 / 3.5,36.5 / 51.5,3.5 / 51.5,36.5.
- Silk `CM5 CAMERA` on F.SilkS.
- Dual DF40 keepout on B.Cu covering the CM5 module courtyard.
- Visual: Mini-HDMI and 15-pin DSI along the NORTH edge; vertical USB-A on the NORTH-EAST; IMX219-D160 camera module east-of-center on F.Cu (J1 at 37.21, 18.00, NOT board center); Micro SD on the WEST; USB-C on the SOUTH-WEST; 40-pin GPIO along the WEST; GPIO21 key SOUTH-EAST; CM5 DF40 Module1 on B.Cu at 51.30, 36.42, 90 deg covering most of the back. If any of those orientations is unclear after the wiki photos and Xerxes, write it under Clarifications instead of guessing.

## Stackup SIG-GND-SIG-PWR-GND-SIG
F.Cu / In1.Cu GND / In2.Cu / In3.Cu PWR / In4.Cu GND / B.Cu, 1.6 mm, 35 um copper, dielectrics 0.10 then 0.51 then 0.14 then 0.51 then 0.10 mm. ENIG.
Netclasses: 100Ohm_Diff width 0.15 mm gap 0.18 mm on F.Cu (never In1.Cu, In3.Cu, In4.Cu) for HDMI0/CSI/CAM0/DSI1; 90Ohm_Diff width 0.18 mm gap 0.20 mm for USB; Power_5V 0.80 mm; Power_3V3 0.60 mm. Ignore-net-class GND, Power_5V, Power_3V3, Power_Low while autorouting. Lock HDMI/CSI/DSI/USB tracks after the diff stage.

## BOM / symbols / footprints / 3D / placement (copy verbatim)
91 schematic symbols, 90 PCB footprints. Use these exact footprint names. Assign the listed 3D models (KiCad 10 official where given; project files `DF40C-100DS.stp`, `JTHDA-19F08.STEP`, `5033981892.stp`, `SRP5030CC.stp`, `Camera_IMX219-D160.step`, `CM4.step` / CM5 equivalent, USB-A wrl).
| REF | VALUE | SYMBOL | FOOTPRINT | 3D MODEL | LAYER | X | Y | ROT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | 2.2uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 22.00 | 22.50 | 90 |
| C10 | 100nF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 19.50 | 14.50 | 90 |
| C11 | 10uF | C | C_0805_2012Metric | C_0805_2012Metric.step | B.Cu | 46.00 | 36.00 | 90 |
| C12 | 2.2uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 27.00 | 22.50 | 90 |
| C14 | 100nF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 29.50 | 14.50 | 90 |
| C15 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 27.00 | 14.50 | 90 |
| C16 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 17.50 | 12.00 | 90 |
| C17 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 30.20 | 7.00 | 90 |
| C18 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 23.00 | 7.00 | 90 |
| C19 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 20.50 | 8.00 | 90 |
| C2 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 22.00 | 14.50 | 90 |
| C20 | 10pF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 41.50 | 13.48 | 90 |
| C21 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 43.50 | 12.48 | 90 |
| C22 | 10uF | C | C_0805_2012Metric | C_0805_2012Metric.step | F.Cu | 46.50 | 10.00 | -90 |
| C23 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 34.50 | 38.00 | 90 |
| C24 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 43.50 | 10.00 | 90 |
| C25 | 22uF | C | C_0805_2012Metric | C_0805_2012Metric.step | F.Cu | 17.50 | 16.00 | 90 |
| C26 | 22uF | C | C_0805_2012Metric | C_0805_2012Metric.step | F.Cu | 17.50 | 22.00 | 90 |
| C27 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 21.50 | 8.00 | 90 |
| C28 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 8.00 | 6.50 | 90 |
| C29 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 38.50 | 12.50 | 90 |
| C3 | 100nF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 24.50 | 14.50 | 90 |
| C30 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 36.00 | 10.50 | 90 |
| C31 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 33.00 | 10.50 | 90 |
| C32 | 0.1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 35.00 | 21.50 | 90 |
| C4 | 10uF | C | C_0805_2012Metric | C_0805_2012Metric.step | B.Cu | 49.50 | 36.00 | 90 |
| C5 | 2.2uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 32.00 | 22.50 | 90 |
| C6 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 16.50 | 12.00 | 90 |
| C7 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 26.50 | 9.00 | 90 |
| C8 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | F.Cu | 18.50 | 12.00 | 90 |
| C9 | 1uF | C | C_0402_1005Metric | C_0402_1005Metric.step | B.Cu | 17.00 | 14.50 | 90 |
| DSI1 | DSI_15P | DSI_15P | Molex_200528-0150_1x15-1MP_P1.00mm_Horizontal | TE_1-84952-5_1x15-1MP_P1.0mm_Horizontal.step | F.Cu | 27.00 | 4.39 | 180 |
| FB1 | 120R | L | R_0603_1608Metric | R_0603_1608Metric.step | B.Cu | 17.00 | 22.50 | 90 |
| H3 | Conn_01x03 | Conn_01x03 | PinHeader_1x03_P2.54mm_Vertical | PinHeader_1x03_P2.54mm_Vertical.step | B.Cu | 45.50 | 24.54 | 180 |
| H4 | Conn_01x03 | Conn_01x03 | PinHeader_1x03_P2.54mm_Vertical | PinHeader_1x03_P2.54mm_Vertical.step | F.Cu | 49.00 | 19.38 | 0 |
| H5 | Conn_01x03 | Conn_01x03 | PinHeader_1x03_P2.54mm_Vertical | PinHeader_1x03_P2.54mm_Vertical.step | F.Cu | 53.00 | 24.50 | 180 |
| HDMI1 | HDMI_Mini_C | HDMI_Mini_C | HDMI_Mini-C_Molex_47151-0001 | JTHDA-19F08.STEP | F.Cu | 8.00 | 5.52 | 180 |
| J1 | Camera_Module_IMX219-D160 | IMX219_30P | Camera_Module_IMX219-D160 | Camera_IMX219-D160.step | F.Cu | 37.21 | 18.00 | 0 |
| J2 | USB_A_Vertical | USB_A_Vertical | MOLEX_USB_67298-4090 | USB_A_Molex_105057-0001_Vertical.wrl | F.Cu | 43.50 | 6.50 | 0 |
| Key1 | SW_Push_SPST | SW_Push_SPST | SW_SPST_PTS645Sx43SMTR92 | SW_SPST_PTS645Sx43SMTR92.step | F.Cu | 45.52 | 36.25 | 0 |
| L1 | MCF1210BF2-900T01 | MCF1210BF2-900T01 | L_CommonModeChoke_Coilank_ACM1210 | L_CommonModeChoke_Coilank_ACM1210.step | F.Cu | 34.50 | 8.00 | 0 |
| L2 | MCF1210BF2-900T01 | MCF1210BF2-900T01 | L_CommonModeChoke_Coilank_ACM1210 | L_CommonModeChoke_Coilank_ACM1210.step | F.Cu | 28.50 | 8.00 | 0 |
| L3 | MCF1210BF2-900T01 | MCF1210BF2-900T01 | L_CommonModeChoke_Coilank_ACM1210 | L_CommonModeChoke_Coilank_ACM1210.step | F.Cu | 31.50 | 8.00 | 0 |
| L5 | LED Red | LED | LED_0603_1608Metric | LED_0603_1608Metric.step | B.Cu | 2.00 | 21.50 | 90 |
| L6 | LED Green | LED | LED_0603_1608Metric | LED_0603_1608Metric.step | B.Cu | 6.00 | 21.50 | 90 |
| L7 | 4.7uH | L | L_Bourns_SRP5030CC | SRP5030CC.stp | F.Cu | 51.00 | 12.00 | -90 |
| Module1 | ComputeModule4-CM4 | ComputeModule4-CM4 | Raspberry-Pi-4-Compute-Module | DF40C-100DS.stp, DF40C-100DS.stp, CM4.step | B.Cu | 51.30 | 36.42 | 90 |
| PI1 | Raspberry_Pi_40Pin_GPIO | Raspberry_Pi_40Pin_GPIO | PinHeader_2x20_P2.54mm_Vertical | PinHeader_2x20_P2.54mm_Vertical.step | F.Cu | 3.50 | 30.77 | 90 |
| Q1 | DMG1012T | DMG1012T | Texas_DRT-3 | Texas_DRT-3.step | B.Cu | 14.00 | 18.50 | 0 |
| Q2 | DMG1012T | DMG1012T | Texas_DRT-3 | Texas_DRT-3.step | B.Cu | 11.00 | 18.50 | 0 |
| R1 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 8.50 | 13.00 | 90 |
| R10 | 22R | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 41.50 | 16.01 | 90 |
| R11 | 10k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 10.00 | 21.50 | 90 |
| R12 | 1k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 4.00 | 21.50 | 90 |
| R13 | 1k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 8.00 | 21.50 | 90 |
| R14 | 10k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 32.00 | 7.00 | 90 |
| R15 | 100k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 32.00 | 14.50 | 90 |
| R16 | 10R | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 44.50 | 12.45 | 90 |
| R17 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 34.50 | 14.50 | 90 |
| R19 | 100k | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 47.50 | 15.00 | 90 |
| R2 | 5.1k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 43.50 | 33.00 | 90 |
| R20 | 100k | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 44.50 | 10.00 | 90 |
| R21 | 20k | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 41.50 | 11.00 | 90 |
| R22 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 5.00 | 7.00 | 90 |
| R24 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 5.00 | 9.00 | 90 |
| R28 | 47k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 5.00 | 11.00 | 90 |
| R29 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 11.50 | 13.00 | 90 |
| R3 | 5.1k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 43.00 | 36.00 | 90 |
| R30 | 10k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 35.00 | 18.50 | 90 |
| R31 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 13.50 | 13.00 | 90 |
| R32 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 15.50 | 13.00 | 90 |
| R33 | 0R | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 5.00 | 13.00 | 90 |
| R34 | 10k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 39.00 | 10.50 | 90 |
| R35 | 4.7k | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 23.50 | 8.00 | 90 |
| R36 | 4.7k | R | R_0402_1005Metric | R_0402_1005Metric.step | F.Cu | 22.50 | 8.00 | 90 |
| R4 | 4.7k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 14.00 | 16.00 | 90 |
| R5 | 2.2k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 14.00 | 21.00 | 90 |
| R7 | 4.7k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 11.00 | 16.00 | 90 |
| R8 | 2.2k | R | R_0402_1005Metric | R_0402_1005Metric.step | B.Cu | 11.00 | 21.00 | 90 |
| TF1 | Micro_SD_Card_Det | Micro_SD_Card_Det | SDCARD_MOLEX_503398-1892 | 5033981892.stp | F.Cu | 7.50 | 18.50 | -90 |
| Type_C1 | USB_C_Receptacle_USB2.0_16P | USB_C_Receptacle_USB2.0_16P | USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal | USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step | F.Cu | 9.50 | 37.50 | 0 |
| U1 | RT9193-28GB | RT9193 | SOT-23-5 | SOT-23-5.step | B.Cu | 22.00 | 18.50 | 0 |
| U2 | RT9193-18GB | RT9193 | SOT-23-5 | SOT-23-5.step | B.Cu | 17.00 | 18.50 | 0 |
| U3 | RT9013-12 | RT9013-12 | SOT-23-5 | SOT-23-5.step | B.Cu | 27.00 | 18.50 | 0 |
| U4 | DIO7003HCST5 | DIO7003HCST5 | SOT-23-5 | SOT-23-5.step | B.Cu | 27.00 | 7.00 | 0 |
| U5 | MP1658GTF-Z | MP1658GTF-Z | TSOT-23-6 | TSOT-23-6.step | F.Cu | 44.64 | 15.50 | 0 |
| U6 | FSUSB42MX | FSUSB42MX | UQFN-10_1.4x1.8mm_P0.4mm | UQFN-10_1.4x1.8mm_P0.4mm.step | B.Cu | 8.00 | 10.00 | 0 |
| U7 | DIO7003HCST5 | DIO7003HCST5 | SOT-23-5 | SOT-23-5.step | B.Cu | 32.00 | 18.50 | 0 |
| U8 | STMPS2171STR | STMPS2171STR | SOT-23-5 | SOT-23-5.step | B.Cu | 37.00 | 7.00 | 0 |
| X1 | OSC_24MHz | OSC_24MHz | Crystal_SMD_3225-4Pin_3.2x2.5mm | Crystal_SMD_3225-4Pin_3.2x2.5mm.step | F.Cu | 41.65 | 19.90 | 90 |

## Unique PCB footprints (list must match)
- C_0402_1005Metric
- C_0805_2012Metric
- Camera_Module_IMX219-D160
- Crystal_SMD_3225-4Pin_3.2x2.5mm
- HDMI_Mini-C_Molex_47151-0001
- LED_0603_1608Metric
- L_Bourns_SRP5030CC
- L_CommonModeChoke_Coilank_ACM1210
- MOLEX_USB_67298-4090
- Molex_200528-0150_1x15-1MP_P1.00mm_Horizontal
- PinHeader_1x03_P2.54mm_Vertical
- PinHeader_2x20_P2.54mm_Vertical
- R_0402_1005Metric
- R_0603_1608Metric
- Raspberry-Pi-4-Compute-Module
- SDCARD_MOLEX_503398-1892
- SOT-23-5
- SW_SPST_PTS645Sx43SMTR92
- TSOT-23-6
- Texas_DRT-3
- UQFN-10_1.4x1.8mm_P0.4mm
- USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal

## Unique 3D models (list must match)
- SW_SPST_PTS645Sx43SMTR92.step  (`${KICAD10_3DMODEL_DIR}/Button_Switch_SMD.3dshapes/SW_SPST_PTS645Sx43SMTR92.step`)
- C_0402_1005Metric.step  (`${KICAD10_3DMODEL_DIR}/Capacitor_SMD.3dshapes/C_0402_1005Metric.step`)
- C_0805_2012Metric.step  (`${KICAD10_3DMODEL_DIR}/Capacitor_SMD.3dshapes/C_0805_2012Metric.step`)
- TE_1-84952-5_1x15-1MP_P1.0mm_Horizontal.step  (`${KICAD10_3DMODEL_DIR}/Connector_FFC-FPC.3dshapes/TE_1-84952-5_1x15-1MP_P1.0mm_Horizontal.step`)
- PinHeader_2x20_P2.54mm_Vertical.step  (`${KICAD10_3DMODEL_DIR}/Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x20_P2.54mm_Vertical.step`)
- Crystal_SMD_3225-4Pin_3.2x2.5mm.step  (`${KICAD10_3DMODEL_DIR}/Crystal.3dshapes/Crystal_SMD_3225-4Pin_3.2x2.5mm.step`)
- L_CommonModeChoke_Coilank_ACM1210.step  (`${KICAD10_3DMODEL_DIR}/Inductor_SMD.3dshapes/L_CommonModeChoke_Coilank_ACM1210.step`)
- LED_0603_1608Metric.step  (`${KICAD10_3DMODEL_DIR}/LED_SMD.3dshapes/LED_0603_1608Metric.step`)
- UQFN-10_1.4x1.8mm_P0.4mm.step  (`${KICAD10_3DMODEL_DIR}/Package_DFN_QFN.3dshapes/UQFN-10_1.4x1.8mm_P0.4mm.step`)
- SOT-23-5.step  (`${KICAD10_3DMODEL_DIR}/Package_TO_SOT_SMD.3dshapes/SOT-23-5.step`)
- TSOT-23-6.step  (`${KICAD10_3DMODEL_DIR}/Package_TO_SOT_SMD.3dshapes/TSOT-23-6.step`)
- Texas_DRT-3.step  (`${KICAD10_3DMODEL_DIR}/Package_TO_SOT_SMD.3dshapes/Texas_DRT-3.step`)
- R_0402_1005Metric.step  (`${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.step`)
- PinHeader_1x03_P2.54mm_Vertical.step  (`${KICAD9_3DMODEL_DIR}/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x03_P2.54mm_Vertical.step`)
- USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step  (`${KICAD9_3DMODEL_DIR}/Connector_USB.3dshapes/USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal.step`)
- R_0603_1608Metric.step  (`${KICAD9_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step`)
- USB_A_Molex_105057-0001_Vertical.wrl  (`${KIPRJMOD}/3dmodels/USB_A_Molex_105057-0001_Vertical.wrl`)
- CM4.step  (`./3dmodels/CM4.step`)
- Camera_IMX219-D160.step  (`./3dmodels/Camera_IMX219-D160.step`)
- 5033981892.stp  (`./CM5.3dshapes/5033981892.stp`)
- DF40C-100DS.stp  (`./CM5.3dshapes/DF40C-100DS.stp`)
- JTHDA-19F08.STEP  (`./CM5.3dshapes/JTHDA-19F08.STEP`)
- SRP5030CC.stp  (`./CM5.3dshapes/SRP5030CC.stp`)

## Placement table (board-local mm from SW of Edge.Cuts)
C1: 22.00, 22.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C10: 19.50, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C11: 46.00, 36.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C12: 27.00, 22.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C14: 29.50, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C15: 27.00, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C16: 17.50, 12.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C17: 30.20, 7.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C18: 23.00, 7.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C19: 20.50, 8.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C2: 22.00, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C20: 41.50, 13.48 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C21: 43.50, 12.48 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C22: 46.50, 10.00 mm, -90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C23: 34.50, 38.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C24: 43.50, 10.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C25: 17.50, 16.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C26: 17.50, 22.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C27: 21.50, 8.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C28: 8.00, 6.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C29: 38.50, 12.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C3: 24.50, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C30: 36.00, 10.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C31: 33.00, 10.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C32: 35.00, 21.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C4: 49.50, 36.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C5: 32.00, 22.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C6: 16.50, 12.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C7: 26.50, 9.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C8: 18.50, 12.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
C9: 17.00, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
DSI1: 27.00, 4.39 mm, 180 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
FB1: 17.00, 22.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
H3: 45.50, 24.54 mm, 180 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
H4: 49.00, 19.38 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
H5: 53.00, 24.50 mm, 180 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
HDMI1: 8.00, 5.52 mm, 180 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
J1: 37.21, 18.00 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
J2: 43.50, 6.50 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
Key1: 45.52, 36.25 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
L1: 34.50, 8.00 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
L2: 28.50, 8.00 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
L3: 31.50, 8.00 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
L5: 2.00, 21.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
L6: 6.00, 21.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
L7: 51.00, 12.00 mm, -90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
Module1: 51.30, 36.42 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
PI1: 3.50, 30.77 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
Q1: 14.00, 18.50 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
Q2: 11.00, 18.50 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
R1: 8.50, 13.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R10: 41.50, 16.01 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R11: 10.00, 21.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R12: 4.00, 21.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R13: 8.00, 21.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R14: 32.00, 7.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R15: 32.00, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R16: 44.50, 12.45 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R17: 34.50, 14.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R19: 47.50, 15.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R2: 43.50, 33.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R20: 44.50, 10.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R21: 41.50, 11.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R22: 5.00, 7.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R24: 5.00, 9.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R28: 5.00, 11.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R29: 11.50, 13.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R3: 43.00, 36.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R30: 35.00, 18.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R31: 13.50, 13.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R32: 15.50, 13.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R33: 5.00, 13.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R34: 39.00, 10.50 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R35: 23.50, 8.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R36: 22.50, 8.00 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R4: 14.00, 16.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R5: 14.00, 21.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R7: 11.00, 16.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
R8: 11.00, 21.00 mm, 90 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±2.0 mm)
TF1: 7.50, 18.50 mm, -90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
Type_C1: 9.50, 37.50 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.0 mm)
U1: 22.00, 18.50 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U2: 17.00, 18.50 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U3: 27.00, 18.50 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U4: 27.00, 7.00 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U5: 44.64, 15.50 mm, 0 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U6: 8.00, 10.00 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U7: 32.00, 18.50 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
U8: 37.00, 7.00 mm, 0 deg, B.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)
X1: 41.65, 19.90 mm, 90 deg, F.Cu (board-local from SW of Edge.Cuts, tol ±1.5 mm)

## Schematic netlist contract (copy verbatim; these net names are required)
- 1V2: J1, U3
- 1V8: J1, Q1, Q2, U2
- 2V8: J1, U1
- BT_EN: Module1
- CAM0_CLK_N: L3, Module1
- CAM0_CLK_P: L3, Module1
- CAM0_D0_N: L1, Module1
- CAM0_D0_P: L1, Module1
- CAM0_D1_N: L2, Module1
- CAM0_D1_P: L2, Module1
- CAM_FB1_OUT: FB1, U1
- CAM_GPIO: Module1
- CAM_GPIO': J1, U1, U2, U3
- CC1: Type_C1
- CC2: Type_C1
- CM4_1V8: H5, Module1
- CM4_3V3: H5, Module1, U6
- CM4_5V: Module1, PI1, Type_C1, U4
- CSI_CLK_N: J1, L3
- CSI_CLK_P: J1, L3
- CSI_D0_N: J1, L1
- CSI_D0_P: J1, L1
- CSI_D1_N: J1, L2
- CSI_D1_P: J1, L2
- DSI1_CLK_N: DSI1, Module1
- DSI1_CLK_P: DSI1, Module1
- DSI1_D0_N: DSI1, Module1
- DSI1_D0_P: DSI1, Module1
- DSI1_D1_N: DSI1, Module1
- DSI1_D1_P: DSI1, Module1
- EEPROM_WP: Module1
- GPIO10: Module1, PI1
- GPIO11: Module1, PI1
- GPIO12: Module1, PI1
- GPIO13: Module1, PI1
- GPIO14: Module1, PI1
- GPIO15: Module1, PI1
- GPIO16: Module1, PI1
- GPIO17: Module1, PI1
- GPIO18: Module1, PI1
- GPIO19: Module1, PI1
- GPIO2: Module1, PI1
- GPIO20: Module1, PI1
- GPIO21: Key1, Module1, PI1
- GPIO22: Module1, PI1
- GPIO23: Module1, PI1
- GPIO24: Module1, PI1
- GPIO25: Module1, PI1
- GPIO26: Module1, PI1
- GPIO27: Module1, PI1
- GPIO3: Module1, PI1
- GPIO4: Module1, PI1
- GPIO5: Module1, PI1
- GPIO6: Module1, PI1
- GPIO7: Module1, PI1
- GPIO8: Module1, PI1
- GPIO9: Module1, PI1
- GPIO_VREF: H5, Module1
- HDMI0_CEC: HDMI1, Module1
- HDMI0_CLK_N: HDMI1, Module1
- HDMI0_CLK_P: HDMI1, Module1
- HDMI0_HOTPLUG: HDMI1, Module1
- HDMI0_SCL: HDMI1, Module1
- HDMI0_SDA: HDMI1, Module1
- HDMI0_TX0_N: HDMI1, Module1
- HDMI0_TX0_P: HDMI1, Module1
- HDMI0_TX1_N: HDMI1, Module1
- HDMI0_TX1_P: HDMI1, Module1
- HDMI0_TX2_N: HDMI1, Module1
- HDMI0_TX2_P: HDMI1, Module1
- HDMI_5V: HDMI1, U4
- HDMI_5V_EN: U4
- ID_SC: Module1, PI1
- ID_SD: Module1, PI1
- L5_K: L5
- L6_K: L6
- LED_G: Module1
- MCLK: J1
- MP1658_BST: U5
- MP1658_BST_R: C21, R16
- MP1658_EN: U5
- MP1658_FB: U5
- MP1658_SW: L7, U5
- PI_BOOT: H4, Module1
- PI_GLOBAL_EN: H3, Module1
- PI_RUN: H3, Module1
- PWR_3V3: DSI1, FB1, L5, L6
- SCL0: DSI1, Module1, Q1
- SCL_1V8: J1, Q1
- SDA0: DSI1, Module1, Q2
- SDA_1V8: J1, Q2
- SD_3.3V: TF1, U7
- SD_CLK: Module1, TF1
- SD_CMD: Module1, TF1
- SD_DAT0: Module1, TF1
- SD_DAT1: Module1, TF1
- SD_DAT2: Module1, TF1
- SD_DAT3: Module1, TF1
- SD_PWREN: Module1, U7
- SD_VDD_Override: Module1
- U1_BP: U1
- U2_BP: U2
- U3_BP: U3
- USB0_B_N: U6
- USB0_B_P: U6
- USB0_ID: Module1
- USB0_N: Module1
- USB0_P: Module1
- USBB_N: Type_C1, U6
- USBB_P: Type_C1, U6
- USBD0_N: J2, U6
- USBD0_P: J2, U6
- USB_5V: J2, U8
- USB_FAULT: U8
- USB_SEL: H4, U6
- WIFI_EN: Module1
- X1_OUT: X1
- GND: HDMI1, Type_C1, Module1

## Electrical summary
Dual 100-pin Hirose DF40 H1A/H1B for CM5 (`Raspberry-Pi-4-Compute-Module` / DF40C-100DS). IMX219-D160 (J1) with RT9193-28GB -> 2V8, RT9193-18GB -> 1V8, RT9013-12 -> 1V2, 24.000 MHz XO through 22 ohm to MCLK, DMG1012T level shifters on SDA/SCL, MCF1210 chokes on CAM0_D0 / CAM0_D1 / CAM0_CLK. USB-C into CM4_5V, MP1658GTF-Z (L7 SRP5030CC 4.7uH) to PWR_3V3, DIO7003 to HDMI_5V and SD_3.3V, STMPS2171 to USB_5V, FSUSB42UMX USB0 mux between Type-C and USB-A, Mini-HDMI (Molex 47151 / JTHDA-19F08), 15-pin DSI (Molex 200528-0150), Micro SD (Molex 503398-1892), 40-pin GPIO, Key1 PTS645 on GPIO21, H3 RUN/GLOBAL_EN, H4 BOOT/USB_SEL, H5 3V3/VREF/1V8 test header, ACT/PWR LEDs L5/L6.

## Clarifications
1. **Mezzanine Footprint & 3D Model Identity**:
   - Footprint identifier is specified as `Raspberry-Pi-4-Compute-Module` with dual Hirose DF40C-100DS sockets. On CM5, the Hirose DF40 receptacle pinout H1A/H1B aligns pin-for-pin with the CM4 standard interfaces utilized in this design (5V input, 3V3/1V8 outputs, CSI0, DSI1, HDMI0, USB2.0 OTG, SD card interface, and GPIO 2-27).
   - The Module1 placement is B.Cu at (51.30, 36.42 mm), rotation 90 deg. This matches the standard mezzanine orientation where the module overhang sits across the carrier board back.
2. **Connector Pin 1 Orientations**:
   - DSI1 (15-pin FPC Molex 200528-0150): Top edge F.Cu at (27.00, 4.39 mm), 180 deg orientation faces connector opening northward. Pin 1 aligns with standard Raspberry Pi 15-pin display pinout (GND on pin 1/4/7/10/13).
   - HDMI1 (Mini-HDMI Type C Molex 47151-0001): Top edge F.Cu at (8.00, 5.52 mm), 180 deg orientation faces receptacle outward to north. Pin 1 (TX2+) starts at standard left/right orientation per Molex specification.
   - J1 (IMX219-D160 Camera 30-pin): F.Cu at (37.21, 18.00 mm), 0 deg. Mating connector orientation matches Waveshare NANO-C layout where camera ribbon routes towards center/east.
   - Type_C1 (GCT USB4105 16-pin): F.Cu at (9.50, 37.50 mm), 0 deg. Receptacle mouth faces southward past the PCB edge for cable insertion.
   - J2 (USB-A Vertical Molex 67298-4090): F.Cu at (43.50, 6.50 mm), 0 deg. Sits vertically with opening facing upward from front side.
   - TF1 (Micro SD Molex 503398-1892): F.Cu at (7.50, 18.50 mm), -90 deg. Card slot entrance faces westward off the board edge.
   - PI1 (40-Pin GPIO Header 2x20 2.54mm): F.Cu at (3.50, 30.77 mm), 90 deg. Pins 1 & 2 orient toward south edge consistent with standard Raspberry Pi 40-pin layout.
   - H3, H4, H5 (1x03 2.54mm headers):
     - H3 on B.Cu at (45.50, 24.54 mm), 180 deg (PI_RUN, PI_GLOBAL_EN, GND).
     - H4 on F.Cu at (49.00, 19.38 mm), 0 deg (PI_BOOT, USB_SEL, GND).
     - H5 on F.Cu at (53.00, 24.50 mm), 180 deg (CM4_3V3, GPIO_VREF, CM4_1V8).
3. **3D Model Sourcing & Paths**:
   - Official KiCad libraries are mapped using `${KICAD10_3DMODEL_DIR}` and `${KICAD9_3DMODEL_DIR}` as specified.
   - Custom and project-specific 3D models (`DF40C-100DS.stp`, `JTHDA-19F08.STEP`, `5033981892.stp`, `SRP5030CC.stp`, `Camera_IMX219-D160.step`, `CM4.step`, `USB_A_Molex_105057-0001_Vertical.wrl`) will be resolved from project `./CM5.3dshapes/` and `${KIPRJMOD}/3dmodels/`. If local files are not yet populated, models will be staged into these relative directories prior to final 3D rendering.

## 3D pose table
Audit each assigned model against these offsets (mm) and rotations (deg) after the 3D-model phase. Re-render top and bottom.
| File | Offset xyz | Rotate xyz |
| --- | --- | --- |
| Camera_IMX219-D160.step | 0 0 0 | 0 0 0 |
| JTHDA-19F08.STEP | 0 -6.8 0 | -90 0 0 |
| 5033981892.stp | -135.95 -16.5 154.5 | 0 -180 -180 |
| CM4.step | 52.25 -52 0 | 0 0 -90 |
| DF40C-100DS.stp (H1A) | -0.46 -28.385 0 | -90 0 0 |
| DF40C-100DS.stp (H1B) | 33.46 -28.385 0 | -90 0 0 |
Camera STEP origin is the mezzanine; body extends -X (not a 90° brick covering USB-A). USB-A vertical STEP is a front pocket, not a through-hole. H3 on B.Cu points away from the compute module keepout.
4. **Stackup and Differential Impedance Strategy**:
   - 6-layer stackup: Layer 1 (F.Cu) Signal -> Layer 2 (In1.Cu) Solid GND -> Layer 3 (In2.Cu) Signal -> Layer 4 (In3.Cu) Mixed PWR/Signal -> Layer 5 (In4.Cu) Solid GND -> Layer 6 (B.Cu) Signal.
   - All critical high-speed pairs (HDMI0 differential signals, CSI camera lanes, DSI display lanes, and USB D+/D-) will be strictly routed on external layers (primarily F.Cu) directly over unbroken ground reference on In1.Cu/In4.Cu.
   - 100 Ohm differential impedance targets 0.15 mm track width and 0.18 mm spacing. 90 Ohm differential USB targets 0.18 mm track width and 0.20 mm spacing.
