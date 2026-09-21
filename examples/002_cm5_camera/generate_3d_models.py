"""Generate detailed STEP models for the CM5 camera carrier (cadquery)."""

from __future__ import annotations

from pathlib import Path

import cadquery as cq
from cadquery import exporters

ROOT = Path(__file__).resolve().parent
CAM_DIR = ROOT / "cm5_camera" / "3dmodels"
ROOT_3D = ROOT / "3dmodels"


def _export(solid: cq.Workplane, *paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        exporters.export(solid, str(path))


def camera_imx219() -> cq.Workplane:
    """IMX219-D160-style module in the vendor STEP frame (mm).

    Connector at the origin, board extending -X, lens along +Z. Footprint
    model rotation 0° matches Waveshare NANO-C (body west of J1).
    """

    pcb = cq.Workplane("XY").box(18.0, 11.4, 1.0).translate((-7.2, 0.0, 0.5))
    for x, y in ((-1.5, -4.2), (-1.5, 4.2), (-13.5, -4.2), (-13.5, 4.2)):
        pcb = pcb.cut(cq.Workplane("XY").circle(0.7).extrude(2.0).translate((x, y, -0.2)))
    stiffener = cq.Workplane("XY").box(16.5, 10.2, 0.4).translate((-7.2, 0.0, 1.1))
    holder = cq.Workplane("XY").box(10.5, 10.5, 2.2).translate((-8.5, 0.0, 2.3))
    barrel = cq.Workplane("XY").circle(4.8).extrude(5.4).translate((-8.5, 0.0, 3.4))
    ring = cq.Workplane("XY").circle(5.2).circle(4.4).extrude(0.6).translate((-8.5, 0.0, 8.5))
    glass = cq.Workplane("XY").circle(3.4).extrude(0.45).translate((-8.5, 0.0, 8.7))
    iris = cq.Workplane("XY").circle(2.3).extrude(0.25).translate((-8.5, 0.0, 9.05))
    sensor = cq.Workplane("XY").box(5.0, 5.0, 0.6).translate((-8.5, 0.0, 1.5))
    connector = cq.Workplane("XY").box(4.2, 8.0, 1.1).translate((0.0, 0.0, 1.05))
    flex = cq.Workplane("XY").box(5.5, 6.5, 0.12).translate((-3.0, 0.0, 0.18))
    return (
        pcb.union(stiffener)
        .union(holder)
        .union(barrel)
        .union(ring)
        .union(glass)
        .union(iris)
        .union(sensor)
        .union(connector)
        .union(flex)
    )


def cm5_module() -> cq.Workplane:
    """CM5 body in the vendor CM4.step frame.

    PCB x 0..55, y 16..56 so offset (52.25, -52, 2) + Rz(-90) lands on the
    DF40 pair after the Module1 footprint Y is restored.
    """

    pcb = cq.Workplane("XY").box(55.0, 40.0, 1.2).translate((27.5, 36.0, 2.2))
    soc = cq.Workplane("XY").box(15.0, 15.0, 1.15).translate((18.0, 34.0, 3.35))
    dram = cq.Workplane("XY").box(12.0, 14.0, 0.95).translate((36.0, 34.0, 3.25))
    pmics = cq.Workplane("XY").box(8.0, 8.0, 0.85).translate((18.0, 48.0, 3.2))
    eeprom = cq.Workplane("XY").box(4.0, 3.2, 0.7).translate((10.0, 22.0, 3.15))
    can = cq.Workplane("XY").box(11.0, 11.0, 1.7).translate((42.0, 48.0, 3.65))
    df40_a = cq.Workplane("XY").box(8.0, 21.0, 1.5).translate((4.0, 36.0, 3.5))
    df40_b = cq.Workplane("XY").box(8.0, 21.0, 1.5).translate((51.0, 36.0, 3.5))
    sd = cq.Workplane("XY").box(14.0, 15.0, 0.55).translate((27.5, 22.0, 1.65))
    label = cq.Workplane("XY").box(20.0, 8.0, 0.2).translate((27.5, 36.0, 1.55))
    return (
        pcb.union(soc)
        .union(dram)
        .union(pmics)
        .union(eeprom)
        .union(can)
        .union(df40_a)
        .union(df40_b)
        .union(sd)
        .union(label)
    )


def usb_a_vertical() -> cq.Workplane:
    """Vertical USB-A shell with a front socket, not a through-hole."""

    shell = (
        cq.Workplane("XY")
        .box(13.1, 5.8, 14.5)
        .faces(">Y")
        .workplane(centerOption="CenterOfMass")
        .rect(11.0, 5.0)
        .cutBlind(-4.2)
    )
    insulator = cq.Workplane("XY").box(10.5, 1.2, 8.0).translate((0.0, -0.6, 1.5))
    tongue = cq.Workplane("XY").box(8.5, 0.4, 7.0).translate((0.0, 0.4, 1.8))
    return shell.union(insulator).union(tongue).translate((3.5, 0.0, 7.25))


def main() -> None:
    cam = camera_imx219()
    _export(cam, CAM_DIR / "Camera_IMX219-D160.step", ROOT_3D / "Camera_IMX219-D160.step")
    module = cm5_module()
    _export(
        module,
        CAM_DIR / "CM4.step",
        CAM_DIR / "CM5.step",
        ROOT_3D / "CM4.step",
        ROOT_3D / "CM5.step",
    )
    usb = usb_a_vertical()
    _export(usb, CAM_DIR / "USB_A_vertical.step")
    print("wrote camera, CM5, and USB-A STEP models")


if __name__ == "__main__":
    main()
