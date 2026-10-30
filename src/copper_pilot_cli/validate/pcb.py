"""Minimal `.kicad_pcb` reader for deterministic geometry checks.

Only what the builtin checks need: footprints (reference, library id, position,
rotation, side, pads, graphic outlines per layer, 3D models) and the board outline
on Edge.Cuts. Coordinates follow KiCad: millimetres, Y grows downward, and a
positive rotation turns a footprint counter-clockwise on screen.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Sexp = list[Any]
Point = tuple[float, float]
Segment = tuple[Point, Point]

_TOKEN_RE = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.DOTALL)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}


class Quoted(str):
    """A string that was quoted in the file (so `"yes"` and `yes` stay distinguishable)."""

    __slots__ = ()


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), text)


def parse_sexp(text: str) -> Sexp:
    """Parse one KiCad s-expression document into nested lists of strings."""
    stack: list[Sexp] = [[]]
    position = 0
    length = len(text)
    while position < length:
        match = _TOKEN_RE.match(text, position)
        if match is None:
            if text[position:].strip():
                raise ValueError(f"unexpected text at offset {position}")
            break
        position = match.end()
        opening, closing, quoted, atom = match.groups()
        if opening:
            node: Sexp = []
            stack[-1].append(node)
            stack.append(node)
        elif closing:
            if len(stack) == 1:
                raise ValueError(f"unbalanced ')' at offset {position}")
            stack.pop()
        elif quoted is not None:
            stack[-1].append(Quoted(_unescape(quoted)))
        elif atom is not None:
            stack[-1].append(atom)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' at end of file")
    root = stack[0]
    if len(root) != 1 or not isinstance(root[0], list):
        raise ValueError("expected a single top-level expression")
    return root[0]


def _head(node: Any) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def children(node: Sexp, name: str) -> list[Sexp]:
    return [child for child in node[1:] if _head(child) == name]


def child(node: Sexp, name: str) -> Sexp | None:
    for item in node[1:]:
        if _head(item) == name:
            return item
    return None


def _value(node: Sexp, name: str, default: str = "") -> str:
    found = child(node, name)
    if found is None or len(found) < 2 or isinstance(found[1], list):
        return default
    return str(found[1])


def _floats(node: Sexp | None, count: int) -> list[float] | None:
    if node is None or len(node) < count + 1:
        return None
    try:
        return [float(item) for item in node[1 : count + 1]]
    except (TypeError, ValueError):
        return None


def _xy(node: Sexp, name: str) -> Point | None:
    values = _floats(child(node, name), 2)
    return (values[0], values[1]) if values else None


def _flag(node: Sexp, name: str) -> bool:
    """`(hide yes)` / `(hide)` / bare `hide` atoms, across KiCad 7-10 syntax."""
    if name in node[1:]:
        return True
    found = child(node, name)
    if found is None:
        return False
    return len(found) == 1 or str(found[1]).lower() in {"yes", "true"}


# ------------------------------------------------------------------ geometry


def rotate(point: Point, degrees: float) -> Point:
    """Rotate a footprint-local vector into board orientation (KiCad, Y down)."""
    if not degrees:
        return point
    radians = math.radians(degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    x, y = point
    return (x * cos + y * sin, -x * sin + y * cos)


def _arc_points(start: Point, mid: Point, end: Point, steps: int = 8) -> list[Point]:
    """Points along a three-point arc; falls back to the chord when collinear."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    denominator = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(denominator) < 1e-9:
        return [start, mid, end]
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / denominator
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / denominator
    radius = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)

    def sweep(frm: float, to: float) -> float:
        return (to - frm) % (2 * math.pi)

    total = sweep(a0, a2)
    if sweep(a0, a1) > total:  # the arc runs the other way round
        total -= 2 * math.pi
    return [
        (
            ux + radius * math.cos(a0 + total * i / steps),
            uy + radius * math.sin(a0 + total * i / steps),
        )
        for i in range(steps + 1)
    ]


def _circle_points(center: Point, edge: Point, steps: int = 16) -> list[Point]:
    radius = math.hypot(edge[0] - center[0], edge[1] - center[1])
    return [
        (
            center[0] + radius * math.cos(2 * math.pi * i / steps),
            center[1] + radius * math.sin(2 * math.pi * i / steps),
        )
        for i in range(steps + 1)
    ]


def _shape_paths(node: Sexp) -> list[list[Point]]:
    """Polylines for one graphic item (fp_/gr_ line, rect, arc, circle, poly)."""
    kind = (_head(node) or "").split("_", 1)[-1]
    if kind == "line":
        start, end = _xy(node, "start"), _xy(node, "end")
        return [[start, end]] if start and end else []
    if kind == "rect":
        start, end = _xy(node, "start"), _xy(node, "end")
        if not (start and end):
            return []
        (x0, y0), (x1, y1) = start, end
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
    if kind == "arc":
        start, mid, end = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
        return [_arc_points(start, mid, end)] if start and mid and end else []
    if kind == "circle":
        center, end = _xy(node, "center"), _xy(node, "end")
        return [_circle_points(center, end)] if center and end else []
    if kind in {"poly", "curve"}:
        pts = child(node, "pts")
        points = [
            (float(item[1]), float(item[2]))
            for item in (pts or [])[1:]
            if _head(item) == "xy" and len(item) >= 3
        ]
        if kind == "poly" and len(points) > 2:
            points.append(points[0])
        return [points] if len(points) > 1 else []
    return []


@dataclass(slots=True)
class Pad:
    number: str
    kind: str  # smd, thru_hole, np_thru_hole, connect
    x: float  # footprint-local, unrotated
    y: float


@dataclass(slots=True)
class Model:
    path: str
    hidden: bool = False


@dataclass(slots=True)
class Footprint:
    ref: str
    value: str
    lib_id: str
    x: float
    y: float
    rotation: float
    layer: str
    description: str = ""
    tags: str = ""
    attrs: set[str] = field(default_factory=set)
    properties: dict[str, str] = field(default_factory=dict)
    pads: list[Pad] = field(default_factory=list)
    # footprint-local polylines keyed by layer name
    graphics: dict[str, list[list[Point]]] = field(default_factory=dict)
    # footprint-local text items keyed by layer name: (text, x, y)
    texts: dict[str, list[tuple[str, float, float]]] = field(default_factory=dict)
    models: list[Model] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Footprint name without the library nickname."""
        return self.lib_id.split(":", 1)[-1]

    @property
    def back(self) -> bool:
        return self.layer.startswith("B.")

    def to_board(self, point: Point) -> Point:
        dx, dy = rotate(point, self.rotation)
        return (self.x + dx, self.y + dy)

    def local_points(self, *layers: str) -> list[Point]:
        return [
            point for layer in layers for path in self.graphics.get(layer, []) for point in path
        ]

    @property
    def side_prefix(self) -> str:
        return "B." if self.back else "F."


@dataclass(slots=True)
class Board:
    footprints: list[Footprint]
    outline: list[Segment]
    version: str = ""

    def outline_bbox(self) -> tuple[float, float, float, float] | None:
        if not self.outline:
            return None
        xs = [p[0] for segment in self.outline for p in segment]
        ys = [p[1] for segment in self.outline for p in segment]
        return min(xs), min(ys), max(xs), max(ys)


def _segments(paths: list[list[Point]]) -> list[Segment]:
    return [(path[i], path[i + 1]) for path in paths for i in range(len(path) - 1)]


def _parse_footprint(node: Sexp) -> tuple[Footprint, list[Segment]]:
    lib_id = str(node[1]) if len(node) > 1 and isinstance(node[1], str) else ""
    at = child(node, "at") or ["at", "0", "0"]
    numbers = []
    for item in at[1:4]:
        try:
            numbers.append(float(item))
        except (TypeError, ValueError):
            break
    x, y = (numbers + [0.0, 0.0])[:2]
    rotation = numbers[2] if len(numbers) > 2 else 0.0
    properties: dict[str, str] = {}
    for prop in children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
            properties[str(prop[1])] = str(prop[2])
    texts: dict[str, list[tuple[str, float, float]]] = {}
    for text in children(node, "fp_text"):
        # (fp_text reference|value|user "text" (at x y) (layer ...))
        if len(text) < 3:
            continue
        kind, body = str(text[1]), str(text[2])
        if kind == "reference":
            properties.setdefault("Reference", body)
        elif kind == "value":
            properties.setdefault("Value", body)
        position = _xy(text, "at") or (0.0, 0.0)
        texts.setdefault(_value(text, "layer"), []).append((body, *position))
    attr = child(node, "attr")
    footprint = Footprint(
        ref=properties.get("Reference", ""),
        value=properties.get("Value", ""),
        lib_id=lib_id,
        x=x,
        y=y,
        rotation=rotation,
        layer=_value(node, "layer", "F.Cu"),
        description=_value(node, "descr") or properties.get("Description", ""),
        tags=_value(node, "tags"),
        attrs={str(item) for item in (attr or [])[1:] if isinstance(item, str)},
        properties=properties,
    )
    edge: list[Segment] = []
    for item in node[1:]:
        head = _head(item)
        if head is None:
            continue
        if head == "pad" and len(item) >= 3:
            position = _xy(item, "at") or (0.0, 0.0)
            footprint.pads.append(Pad(str(item[1]), str(item[2]), position[0], position[1]))
        elif head == "model" and len(item) >= 2:
            footprint.models.append(Model(str(item[1]), hidden=_flag(item, "hide")))
        elif head.startswith("fp_") and head != "fp_text":
            layer = _value(item, "layer")
            paths = _shape_paths(item)
            if not paths:
                continue
            footprint.graphics.setdefault(layer, []).extend(paths)
            if layer == "Edge.Cuts":
                edge.extend(
                    (footprint.to_board(a), footprint.to_board(b)) for a, b in _segments(paths)
                )
    return footprint, edge


def parse_board(text: str) -> Board:
    root = parse_sexp(text)
    if _head(root) != "kicad_pcb":
        raise ValueError("not a kicad_pcb file")
    footprints: list[Footprint] = []
    outline: list[Segment] = []
    for item in root[1:]:
        head = _head(item)
        if head == "footprint" or head == "module":
            footprint, edge = _parse_footprint(item)
            footprints.append(footprint)
            outline.extend(edge)
        elif head and head.startswith("gr_") and _value(item, "layer") == "Edge.Cuts":
            outline.extend(_segments(_shape_paths(item)))
    return Board(footprints=footprints, outline=outline, version=_value(root, "version"))


def ray_exit(origin: Point, direction: Point, outline: list[Segment]) -> float | None:
    """Distance along `direction` (unit) from `origin` to the nearest outline crossing."""
    ox, oy = origin
    dx, dy = direction
    best: float | None = None
    for (ax, ay), (bx, by) in outline:
        ex, ey = bx - ax, by - ay
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-12:
            continue
        t = ((ax - ox) * ey - (ay - oy) * ex) / denominator
        u = ((ax - ox) * dy - (ay - oy) * dx) / denominator
        if -1e-9 <= u <= 1 + 1e-9 and t >= -1e-6 and (best is None or t < best):
            best = t
    return best


def point_inside(point: Point, outline: list[Segment]) -> bool:
    """Even-odd test against the outline segments."""
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in outline:
        if (ay > y) != (by > y):
            cross = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < cross:
                inside = not inside
    return inside


# ------------------------------------------------------- external connectors

# Connector families that must be reachable from outside the enclosure. Matched
# against the footprint name first, then value, keywords and description.
_EXTERNAL_RE = re.compile(
    r"usb|rj-?45|rj-?11|rj-?12|8p8c|magjack|ethernet|hdmi|displayport|\bdvi\b"
    r"|barrel|dc[ _-]?jack|power[ _-]?jack|\bpj-?0\d\d|audio[ _-]?jack|jack[ _-]?3\.5"
    r"|phone[ _-]?jack|d-?sub|\bdb-?(?:9|15|25)\b|\bde-?9\b",
    re.IGNORECASE,
)
# Internal, vertical or ambiguous parts: pin headers and sockets, jumpers, battery
# holders, board-to-board/mezzanine sockets, flex and wire-to-board connectors,
# card slots, and protection or interface ICs that mention USB/HDMI in the name.
_INTERNAL_RE = re.compile(
    r"pin_?header|pin_?socket|header|jumper|battery|\bbat\b|cr\d{4}|df40|hirose_df|mezzanine"
    r"|board[ _-]?to[ _-]?board|\bb2b\b|ffc|fpc|\bjst\b|jst_|picoblade|molex_pico|terminal"
    r"|micro_?sd|\bsd[ _-]?card|tf[ _-]?card|\bsim\b|vertical|compute[ _-]?module"
    r"|^conn_\d|testpoint|tag-?connect",
    re.IGNORECASE,
)
_SKIPPED_PROPERTIES = {"Reference", "Value", "Datasheet", "Footprint", "ki_fp_filters"}
_CONNECTOR_REF_RE = re.compile(
    r"^(?:J|P|CN|CON|X|XS|XJ|USB|RJ|HDMI|ETH|LAN)(?:\d|[_-])", re.IGNORECASE
)


def external_connector_kind(footprint: Footprint) -> str | None:
    """Return the connector family when this footprint is an external, edge connector."""
    if not _CONNECTOR_REF_RE.match(footprint.ref):
        return None
    name = footprint.name
    if _INTERNAL_RE.search(name):
        return None
    match = _EXTERNAL_RE.search(name)
    if match is None:
        if _INTERNAL_RE.search(footprint.value) or _INTERNAL_RE.search(footprint.tags):
            return None
        extra = [
            value
            for key, value in footprint.properties.items()
            if key not in _SKIPPED_PROPERTIES and "://" not in value
        ]
        text = " ".join((footprint.value, footprint.tags, footprint.description, *extra))
        match = _EXTERNAL_RE.search(text)
    if match is None:
        return None
    word = match.group(0).lower()
    for label, keys in (
        ("USB", ("usb",)),
        ("RJ45/Ethernet", ("rj", "8p8c", "magjack", "ethernet")),
        ("HDMI", ("hdmi",)),
        ("DisplayPort", ("displayport",)),
        ("DVI", ("dvi",)),
        ("audio jack", ("audio", "jack", "phone")),
        ("D-Sub", ("sub", "db", "de")),
    ):
        if any(word.startswith(key) or key in word for key in keys):
            return label
    return "power jack"


CARDINALS: dict[Point, str] = {
    (1.0, 0.0): "+X (right)",
    (-1.0, 0.0): "-X (left)",
    (0.0, 1.0): "+Y (down)",
    (0.0, -1.0): "-Y (up)",
}


def _bbox(points: list[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _area(box: tuple[float, float, float, float]) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def body_points(footprint: Footprint) -> list[Point]:
    """Footprint-local outline of the part body: Fab, else courtyard.

    Silkscreen is not used: it often carries arrows, labels and keep-out marks that
    would move the apparent body centre.
    """
    pads = [(pad.x, pad.y) for pad in footprint.pads]
    pad_area = _area(_bbox(pads)) if pads else 0.0
    side = footprint.side_prefix
    for layer in ("Fab", "CrtYd"):
        points = footprint.local_points(side + layer)
        if len(points) >= 2 and _area(_bbox(points)) >= max(1.0, 0.25 * pad_area):
            return points
    return []


def _snap(vector: Point, ratio: float = 2.0) -> Point | None:
    x, y = vector
    if abs(x) >= ratio * abs(y) and abs(x) > 0:
        return (math.copysign(1.0, x), 0.0)
    if abs(y) >= ratio * abs(x) and abs(y) > 0:
        return (0.0, math.copysign(1.0, y))
    return None


def mating_direction(footprint: Footprint, body: list[Point]) -> Point | None:
    """Board-frame unit vector the mating face points along, or None when ambiguous.

    A right-angle connector's pins sit at the back of its body and the body runs
    forward to the opening, so the opening lies on the side of the body farthest from
    the pad centroid. A "PCB Edge" note on a drawing layer (KiCad library
    convention) overrides the geometry when present.
    """
    if not footprint.pads or not body:
        return None
    px = sum(pad.x for pad in footprint.pads) / len(footprint.pads)
    py = sum(pad.y for pad in footprint.pads) / len(footprint.pads)
    for layer in ("Dwgs.User", "Cmts.User"):
        for text, tx, ty in footprint.texts.get(layer, []):
            if "edge" in text.lower():
                local = _snap((tx - px, ty - py))
                if local is not None:
                    return _snap(rotate(local, footprint.rotation))
    x0, y0, x1, y1 = _bbox(body)
    offset = ((x0 + x1) / 2 - px, (y0 + y1) / 2 - py)
    local = _snap(offset)
    if local is None:
        return None
    extent = (x1 - x0) if local[0] else (y1 - y0)
    if math.hypot(*offset) < max(0.75, 0.1 * extent):
        return None
    return _snap(rotate(local, footprint.rotation))


@dataclass(slots=True)
class EdgeGaps:
    """How far the connector body front sits inside the outline, per direction."""

    center: Point
    gaps: dict[Point, float]


def edge_gaps(footprint: Footprint, body: list[Point], outline: list[Segment]) -> EdgeGaps | None:
    """Distance from each face of the body to the board edge it faces (mm).

    Negative values mean the body overhangs the edge. None when the body centre is
    outside the outline (the connector already hangs off the board).
    """
    points = [footprint.to_board(point) for point in body]
    x0, y0, x1, y1 = _bbox(points)
    center = ((x0 + x1) / 2, (y0 + y1) / 2)
    if not point_inside(center, outline):
        return None
    half = {(1.0, 0.0): (x1 - x0) / 2, (-1.0, 0.0): (x1 - x0) / 2}
    half.update({(0.0, 1.0): (y1 - y0) / 2, (0.0, -1.0): (y1 - y0) / 2})
    gaps: dict[Point, float] = {}
    for direction, depth in half.items():
        exit_at = ray_exit(center, direction, outline)
        if exit_at is not None:
            gaps[direction] = exit_at - depth
    return EdgeGaps(center=center, gaps=gaps)


def suggested_rotation(rotation: float, facing: Point, wanted: Point) -> float:
    """Footprint rotation that turns `facing` into `wanted` (KiCad: CCW on screen)."""

    def angle(vector: Point) -> float:
        return math.degrees(math.atan2(-vector[1], vector[0]))

    result = (rotation + angle(wanted) - angle(facing)) % 360
    return result - 360 if result > 180 else result


# ----------------------------------------------------------------- 3D models

_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")
_MODEL_DIR_VAR_RE = re.compile(r"^(?:KICAD\d*_3DMODEL_DIR|KISYS3DMOD)$")
_THIRD_PARTY_VAR_RE = re.compile(r"^KICAD(\d+)_3RD_PARTY$")
_NO_BODY_RE = re.compile(
    r"mountinghole|mounting_hole|fiducial|testpoint|test_point|\btp_|logo|nettie|net_tie"
    r"|solderjumper|solder_jumper|kibuzzard|symbol:|graphic|pcb_?antenna|antenna_pcb"
    r"|^hole|^pad_|^via|castellated|ruler|tag-?connect|edge_?connector|card_?edge",
    re.IGNORECASE,
)
_NO_BODY_REF_RE = re.compile(r"^(?:H|MH|FID|TP|LOGO|G|NT|REF\*\*|kibuzzard)", re.IGNORECASE)


def footprint_needs_model(footprint: Footprint) -> bool:
    """False for footprints that have no physical body to render."""
    if not footprint.pads:
        return False
    if "board_only" in footprint.attrs:
        return False
    if _NO_BODY_RE.search(footprint.lib_id) or _NO_BODY_RE.search(footprint.value):
        return False
    return not _NO_BODY_REF_RE.match(footprint.ref)


def expand_model_path(
    path: str, variables: dict[str, str], model_dirs: list[str]
) -> tuple[list[str], list[str]]:
    """Expand KiCad path variables into candidate absolute paths.

    Returns (candidates, unknown variable names). `${KICADn_3DMODEL_DIR}` for any
    KiCad version falls back to every system 3D model directory when it is not set,
    because KiCad resolves stock library paths across versions the same way.
    """
    unknown: list[str] = []
    candidates = [path]
    for match in _VAR_RE.finditer(path):
        name = match.group(1) or match.group(2)
        token = match.group(0)
        if name in variables:
            values = [variables[name]]
        elif _MODEL_DIR_VAR_RE.match(name):
            values = model_dirs
        else:
            unknown.append(name)
            values = []
        candidates = [c.replace(token, value) for c in candidates for value in values]
    return candidates, unknown


def _system_model_dirs(kicad_cli: Path | None) -> list[Path]:
    found: list[Path] = []
    if kicad_cli is not None:
        bin_dir = kicad_cli.resolve().parent
        found += [
            bin_dir.parent / "share" / "kicad" / "3dmodels",  # Linux, Windows
            bin_dir.parent / "SharedSupport" / "3dmodels",  # macOS app bundle
        ]
    found += [
        Path("/usr/share/kicad/3dmodels"),
        Path("/usr/local/share/kicad/3dmodels"),
        Path("/app/share/kicad/3dmodels"),  # Flatpak
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"),
    ]
    found += [Path(f"C:/Program Files/KiCad/{v}/share/kicad/3dmodels") for v in ("10.0", "9.0")]
    return found


def _kicad_config_dirs() -> list[Path]:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "kicad"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Preferences" / "kicad"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad"
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)


def _third_party_dir(version: str) -> Path:
    if sys.platform == "linux":
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return data / "kicad" / f"{version}.0" / "3rdparty"
    return Path.home() / "Documents" / "KiCad" / f"{version}.0" / "3rdparty"


def kicad_path_variables(
    project_dir: Path, kicad_cli: Path | None = None
) -> tuple[dict[str, str], list[str]]:
    """Path variables KiCad would use for this project, and the system 3D model dirs.

    Precedence (lowest first): KiCad's user configuration (`kicad_common.json`),
    the process environment, then KIPRJMOD.
    """
    variables: dict[str, str] = {}
    for config_dir in reversed(_kicad_config_dirs()):
        try:
            data = json.loads((config_dir / "kicad_common.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        configured = (data.get("environment") or {}).get("vars") or {}
        if isinstance(configured, dict):
            variables.update({str(k): str(v) for k, v in configured.items() if v})
    variables.update(
        {
            name: value
            for name, value in os.environ.items()
            if value and (name.startswith(("KICAD", "KISYS")) or name in variables)
        }
    )
    variables["KIPRJMOD"] = str(project_dir)
    model_dirs: list[str] = []
    seen: set[Path] = set()
    for path in _system_model_dirs(kicad_cli):
        if path.is_dir() and path.resolve() not in seen:
            seen.add(path.resolve())
            model_dirs.append(str(path))
    for name, value in variables.items():
        if _MODEL_DIR_VAR_RE.match(name) and value not in model_dirs:
            model_dirs.append(value)
    return variables, model_dirs


def resolve_model(
    path: str, project_dir: Path, variables: dict[str, str], model_dirs: list[str]
) -> tuple[Path | None, list[str], list[str]]:
    """Resolve one footprint `(model ...)` path.

    Returns (existing file or None, candidate paths tried, unknown variables).
    """
    text = path.strip()
    names = {m.group(1) or m.group(2) for m in _VAR_RE.finditer(text)}
    local = dict(variables)
    for name in names:
        match = _THIRD_PARTY_VAR_RE.match(name)
        if match and name not in local:
            local[name] = str(_third_party_dir(match.group(1)))
    candidates, unknown = expand_model_path(text, local, model_dirs)
    tried: list[str] = []
    for candidate in candidates:
        normalized = candidate.replace("\\", "/")
        target = Path(normalized).expanduser()
        if not target.is_absolute():
            target = project_dir / target
        tried.append(str(target))
        if target.is_file():
            return target, tried, unknown
    return None, tried, unknown
