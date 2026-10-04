"""Vehicle tracking and counting lines (traffic analysis, step 1).

Parking detection looks at one photo every few seconds and only asks "is
this space occupied?". Traffic counting needs more: it has to know that the
car in this frame is the SAME car as in the last frame, so it can tell when
that car crosses a line and in which direction. That is what this module
does.

Two parts:

* VehicleTracker -- gives every vehicle an ID and follows it from frame to
  frame. It works in the style of ByteTrack (a well-known, simple tracker):
  confident detections are matched to existing tracks first, then weaker
  detections get a second chance to keep an existing track alive (a car
  half-hidden behind a pole scores low but is still the same car). Only
  confident detections can start a new track. Each track remembers its
  speed, so it can guess where the car will be in the next frame even if a
  frame or two was missed.

* CountLine -- a line drawn on the picture. When a tracked vehicle's ground
  point (bottom-middle of its box) moves from one side of the line to the
  other, that is one crossing. Each vehicle is counted at most once per
  line, so a car idling on top of the line can't be counted twice.

Pure Python, no extra libraries and no model import, so it can be tested
without a camera or the RF-DETR model. Times are plain seconds (float), so
it works for live cameras (wall clock) and recorded video (seconds into
the video) alike.

Direction convention: stand at the line's first point and look toward its
second point. A vehicle moving from your LEFT side to your RIGHT side is
"forward"; the other way is "reverse". In picture terms, for a line drawn
left-to-right across the image, "forward" means moving DOWN the picture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

Box = tuple  # (x1, y1, x2, y2) in pixels
Point = tuple  # (x, y) in pixels

# --- Tracker tuning -----------------------------------------------------------
# Detections at or above this confidence are "confident": matched first, and
# allowed to start new tracks. Below it they can only extend an existing track.
HIGH_CONFIDENCE = 0.45
# A new track starts only from a detection at least this confident.
NEW_TRACK_CONFIDENCE = 0.45
# A track must be seen in this many frames before it counts as a real
# vehicle (filters one-frame false detections).
MIN_HITS_TO_CONFIRM = 2
# A track that hasn't been seen for this long is dropped. In seconds, not
# frames, so the same setting works at 2 fps or 15 fps.
MAX_LOST_SECONDS = 1.5
# Minimum match score (see _match_score) to link a detection to a track.
MIN_MATCH_SCORE = 0.2
# Weak (low-confidence) detections need a clearer overlap to be linked.
MIN_WEAK_MATCH_SCORE = 0.35
# How fast the speed estimate follows new measurements (0..1).
VELOCITY_SMOOTHING = 0.6

VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle")


def box_iou(a: Box, b: Box) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def box_center(box: Box) -> Point:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def ground_point(box: Box) -> Point:
    """Bottom-middle of the box: roughly where the vehicle touches the road.
    Used for line crossings because it doesn't jump around when a tall
    vehicle's box grows or shrinks at the top."""
    return ((box[0] + box[2]) / 2.0, box[3])


def normalize_class(class_name: str) -> str:
    name = (class_name or "").strip().lower()
    return name if name in VEHICLE_CLASSES else "car"


# How far (in box diagonals) a detection may be from where a track was
# expected. A brand-new track has no speed estimate yet, so it gets a wider
# search area: at 2 photos/second a car at 25 mph moves more than its own
# length between photos.
SEARCH_RADIUS = 1.0
NEW_TRACK_SEARCH_RADIUS = 2.5


def _match_score(predicted: Box, detection_box: Box, search_radius: float = SEARCH_RADIUS) -> float:
    """How well a detection fits where we expected the track to be.

    Mostly box overlap (IoU). A fast car seen at a low frame rate can move
    more than its own length between frames, so the predicted box might not
    overlap at all; a nearness bonus (center distance compared to the box
    size) keeps those matched. Also requires similar size, so a motorcycle
    next to a truck doesn't steal the truck's track."""
    iou = box_iou(predicted, detection_box)
    pw, ph = predicted[2] - predicted[0], predicted[3] - predicted[1]
    dw, dh = detection_box[2] - detection_box[0], detection_box[3] - detection_box[1]
    if min(pw, ph, dw, dh) <= 0:
        return 0.0
    size_ratio = min(pw * ph, dw * dh) / max(pw * ph, dw * dh)
    if size_ratio < 0.25:
        return 0.0
    pcx, pcy = box_center(predicted)
    dcx, dcy = box_center(detection_box)
    diag = math.hypot(max(pw, dw), max(ph, dh))
    distance = math.hypot(pcx - dcx, pcy - dcy) / diag
    nearness = max(0.0, 1.0 - distance / search_radius)  # 1 = same center, 0 = at the search edge
    return max(iou, 0.5 * nearness * size_ratio)


@dataclass
class TrackedDetection:
    """One detection as the tracker sees it. Accepts app.detection_core
    Detection objects too (anything with .box, .confidence, .class_name)."""

    box: Box
    confidence: float
    class_name: str = "car"


@dataclass
class Track:
    track_id: int
    box: Box
    first_seen: float
    last_seen: float
    confidence: float
    hits: int = 1
    confirmed: bool = False
    velocity: tuple = (0.0, 0.0)  # center pixels per second
    class_votes: dict = field(default_factory=dict)
    path: list = field(default_factory=list)  # [(time, ground point)], recent only

    def predicted_box(self, now: float) -> Box:
        dt = max(0.0, now - self.last_seen)
        dx, dy = self.velocity[0] * dt, self.velocity[1] * dt
        x1, y1, x2, y2 = self.box
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)

    @property
    def vehicle_class(self) -> str:
        if not self.class_votes:
            return "car"
        return max(self.class_votes.items(), key=lambda item: item[1])[0]

    def _vote(self, class_name: str, confidence: float) -> None:
        name = normalize_class(class_name)
        self.class_votes[name] = self.class_votes.get(name, 0.0) + confidence


MAX_PATH_POINTS = 60


class VehicleTracker:
    def __init__(
        self,
        *,
        high_confidence: float = HIGH_CONFIDENCE,
        new_track_confidence: float = NEW_TRACK_CONFIDENCE,
        min_hits: int = MIN_HITS_TO_CONFIRM,
        max_lost_seconds: float = MAX_LOST_SECONDS,
    ) -> None:
        self.high_confidence = high_confidence
        self.new_track_confidence = new_track_confidence
        self.min_hits = min_hits
        self.max_lost_seconds = max_lost_seconds
        self.tracks: list[Track] = []
        self._next_id = 1

    def update(self, detections: Iterable, now: float) -> list[Track]:
        """Feed one frame's detections (taken at time `now`, seconds).
        Returns the confirmed tracks that were seen in this frame."""
        dets = [
            TrackedDetection(tuple(float(v) for v in d.box), float(d.confidence), getattr(d, "class_name", "car"))
            for d in detections
        ]
        strong = [d for d in dets if d.confidence >= self.high_confidence]
        weak = [d for d in dets if d.confidence < self.high_confidence]

        # Drop tracks that have been missing too long (before matching, so a
        # long-gone track can't grab a new car that happens to be nearby).
        self.tracks = [t for t in self.tracks if now - t.last_seen <= self.max_lost_seconds]

        unmatched_tracks = list(self.tracks)
        # Pass 1: confident detections against every track.
        matches, unmatched_tracks, unmatched_strong = self._greedy_match(
            unmatched_tracks, strong, now, MIN_MATCH_SCORE
        )
        # Pass 2: weak detections only keep already-confirmed tracks alive.
        confirmed_left = [t for t in unmatched_tracks if t.confirmed]
        weak_matches, _, _ = self._greedy_match(confirmed_left, weak, now, MIN_WEAK_MATCH_SCORE)
        matches.extend(weak_matches)

        seen: list[Track] = []
        for track, det in matches:
            self._apply(track, det, now)
            if track.confirmed:
                seen.append(track)

        # Unconfirmed tracks get no second chance: one miss and they're gone.
        matched_ids = {id(t) for t, _ in matches}
        self.tracks = [t for t in self.tracks if t.confirmed or id(t) in matched_ids]

        for det in unmatched_strong:
            if det.confidence < self.new_track_confidence:
                continue
            if any(box_iou(det.box, t.box) > 0.6 for t in self.tracks):
                continue  # a leftover duplicate box of a car we already follow
            track = Track(
                track_id=self._next_id,
                box=det.box,
                first_seen=now,
                last_seen=now,
                confidence=det.confidence,
            )
            self._next_id += 1
            track._vote(det.class_name, det.confidence)
            track.path.append((now, ground_point(det.box)))
            if self.min_hits <= 1:
                track.confirmed = True
                seen.append(track)
            self.tracks.append(track)
        return seen

    def _greedy_match(self, tracks, dets, now, min_score):
        """Best-scoring pairs first. Greedy is plenty for the handful of
        vehicles near a counting line, and needs no extra libraries."""
        pairs = []
        for ti, track in enumerate(tracks):
            predicted = track.predicted_box(now)
            radius = NEW_TRACK_SEARCH_RADIUS if track.hits < 2 else SEARCH_RADIUS
            for di, det in enumerate(dets):
                score = _match_score(predicted, det.box, radius)
                if score >= min_score:
                    pairs.append((score, ti, di))
        pairs.sort(reverse=True)
        used_t, used_d, matches = set(), set(), []
        for _score, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            matches.append((tracks[ti], dets[di]))
        left_tracks = [t for i, t in enumerate(tracks) if i not in used_t]
        left_dets = [d for i, d in enumerate(dets) if i not in used_d]
        return matches, left_tracks, left_dets

    def _apply(self, track: Track, det: TrackedDetection, now: float) -> None:
        dt = now - track.last_seen
        if dt > 0:
            ocx, ocy = box_center(track.box)
            ncx, ncy = box_center(det.box)
            measured = ((ncx - ocx) / dt, (ncy - ocy) / dt)
            if track.hits == 1:
                track.velocity = measured
            else:
                a = VELOCITY_SMOOTHING
                track.velocity = (
                    a * measured[0] + (1 - a) * track.velocity[0],
                    a * measured[1] + (1 - a) * track.velocity[1],
                )
        track.box = det.box
        track.last_seen = now
        track.confidence = det.confidence
        track.hits += 1
        track._vote(det.class_name, det.confidence)
        track.path.append((now, ground_point(det.box)))
        if len(track.path) > MAX_PATH_POINTS:
            del track.path[: len(track.path) - MAX_PATH_POINTS]
        if track.hits >= self.min_hits:
            track.confirmed = True


# --- Counting lines -----------------------------------------------------------


def _side(p1: Point, p2: Point, point: Point) -> float:
    """> 0: point is to the RIGHT of p1->p2 (in picture coordinates, where y
    grows downward); < 0: to the left; 0: on the line."""
    return (p2[0] - p1[0]) * (point[1] - p1[1]) - (p2[1] - p1[1]) * (point[0] - p1[0])


def segments_cross(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """True when segment a1-a2 crosses segment b1-b2 (touching counts)."""
    d1 = _side(b1, b2, a1)
    d2 = _side(b1, b2, a2)
    d3 = _side(a1, a2, b1)
    d4 = _side(a1, a2, b2)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    def on_segment(p, q, r):  # q lies on segment p-r (given collinear)
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])

    if d1 == 0 and on_segment(b1, a1, b2):
        return True
    if d2 == 0 and on_segment(b1, a2, b2):
        return True
    if d3 == 0 and on_segment(a1, b1, a2):
        return True
    if d4 == 0 and on_segment(a1, b2, a2):
        return True
    return False


@dataclass
class CountLine:
    line_id: str
    name: str
    p1: Point
    p2: Point
    forward_label: str = "forward"
    reverse_label: str = "reverse"

    def direction_of(self, start: Point, end: Point) -> Optional[str]:
        """'forward' / 'reverse' if moving start->end crosses this line,
        else None."""
        if not segments_cross(start, end, self.p1, self.p2):
            return None
        s0, s1 = _side(self.p1, self.p2, start), _side(self.p1, self.p2, end)
        if s0 == s1:
            return None
        if s0 < 0 <= s1 or s0 <= 0 < s1:
            return "forward"
        return "reverse"

    def label_for(self, direction: str) -> str:
        return self.forward_label if direction == "forward" else self.reverse_label


@dataclass
class Crossing:
    line_id: str
    line_name: str
    track_id: int
    direction: str  # "forward" / "reverse"
    direction_label: str
    vehicle_class: str
    time: float
    point: Point


class LineCounter:
    """Watches confirmed tracks and reports each line crossing once."""

    def __init__(self, lines: Iterable[CountLine]) -> None:
        self.lines = list(lines)
        # (track_id, line_id) pairs already counted
        self._counted: set = set()
        # track_id -> ground point at the last frame we checked it
        self._last_point: dict = {}
        self.totals: dict = {}  # (line_id, direction) -> count

    def update(self, tracks: Iterable[Track], now: float) -> list[Crossing]:
        crossings: list[Crossing] = []
        for track in tracks:
            if not track.confirmed:
                continue
            current = ground_point(track.box)
            previous = self._last_point.get(track.track_id)
            if previous is None:
                # First time we see this track confirmed: check the whole
                # path it took while it was still being confirmed.
                previous = track.path[0][1] if track.path else current
            for line in self.lines:
                key = (track.track_id, line.line_id)
                if key in self._counted:
                    continue
                direction = line.direction_of(previous, current)
                if direction is None:
                    continue
                self._counted.add(key)
                total_key = (line.line_id, direction)
                self.totals[total_key] = self.totals.get(total_key, 0) + 1
                crossings.append(
                    Crossing(
                        line_id=line.line_id,
                        line_name=line.name,
                        track_id=track.track_id,
                        direction=direction,
                        direction_label=line.label_for(direction),
                        vehicle_class=track.vehicle_class,
                        time=now,
                        point=current,
                    )
                )
            self._last_point[track.track_id] = current
        return crossings

    def forget_missing(self, live_track_ids: Iterable[int]) -> None:
        """Free memory for tracks the tracker has dropped."""
        alive = set(live_track_ids)
        for track_id in list(self._last_point):
            if track_id not in alive:
                del self._last_point[track_id]
        self._counted = {key for key in self._counted if key[0] in alive}


# --- Cropping to the lines ----------------------------------------------------
# Same idea as crop_region_for_polygons in detection_core: spend the model's
# fixed working size on the area around the lines instead of the whole
# frame. The zone has to be generous -- the tracker needs to see a car for a
# few frames BEFORE it reaches the line to be sure of it.
LINE_ZONE_RATIO = 0.6  # of the line's length, on each side
LINE_ZONE_MIN_PX = 160
LINE_CROP_SKIP_IF_AREA_FRACTION = 0.80


def crop_region_for_lines(lines: Iterable[CountLine], image_size: tuple) -> Optional[tuple]:
    points = [p for line in lines for p in (line.p1, line.p2)]
    if not points:
        return None
    width, height = image_size
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    length = max(math.hypot(line.p2[0] - line.p1[0], line.p2[1] - line.p1[1]) for line in lines)
    pad = max(LINE_ZONE_MIN_PX, length * LINE_ZONE_RATIO)
    left = int(max(0, min_x - pad))
    top = int(max(0, min_y - pad))
    right = int(min(width, max_x + pad + 1))
    bottom = int(min(height, max_y + pad + 1))
    if right - left < 2 or bottom - top < 2:
        return None
    if (right - left) * (bottom - top) >= LINE_CROP_SKIP_IF_AREA_FRACTION * width * height:
        return None
    return (left, top, right, bottom)


def parse_line_spec(spec: str, image_size: tuple, index: int = 1) -> CountLine:
    """Parses a command-line line description:

        "x1,y1,x2,y2"                          (pixels, or 0-1 fractions of the frame)
        "Main St:x1,y1,x2,y2"                  (with a name)
        "Main St:x1,y1,x2,y2:northbound/southbound"   (with direction names)

    Fractions are used when all four numbers are between 0 and 1."""
    parts = spec.split(":")
    if len(parts) == 1:
        name, coords, labels = f"Line {index}", parts[0], None
    elif len(parts) == 2:
        name, coords, labels = parts[0], parts[1], None
    elif len(parts) == 3:
        name, coords, labels = parts
    else:
        raise ValueError(f"can't read line {spec!r}")
    try:
        values = [float(v) for v in coords.split(",")]
    except ValueError:
        raise ValueError(f"line {spec!r}: coordinates must be numbers") from None
    if len(values) != 4:
        raise ValueError(f"line {spec!r}: need exactly 4 numbers x1,y1,x2,y2")
    width, height = image_size
    if all(0.0 <= v <= 1.0 for v in values):
        values = [values[0] * width, values[1] * height, values[2] * width, values[3] * height]
    x1, y1, x2, y2 = values
    if math.hypot(x2 - x1, y2 - y1) < 5:
        raise ValueError(f"line {spec!r} is too short")
    forward, reverse = "forward", "reverse"
    if labels:
        bits = labels.split("/")
        if len(bits) != 2 or not all(b.strip() for b in bits):
            raise ValueError(f"line {spec!r}: direction names must look like in/out")
        forward, reverse = bits[0].strip(), bits[1].strip()
    return CountLine(
        line_id=f"line_{index}",
        name=name.strip() or f"Line {index}",
        p1=(x1, y1),
        p2=(x2, y2),
        forward_label=forward,
        reverse_label=reverse,
    )
