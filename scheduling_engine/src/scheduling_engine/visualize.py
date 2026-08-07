"""Human-readable rendering of a calendar and what the solver did to it.

For eyeballing behaviour by hand: what time was free, what the client asked
for, where each booking landed, and — the part that actually matters when
tuning `alpha` — which leftover gaps are reusable and which are dead.

The gap report deliberately reuses the solver's own block construction rather
than recomputing gaps from the raw segments. A visualisation that quietly
disagrees with the thing it visualises is worse than none, so
`sum(gap.wasted_minutes)` is guaranteed to equal
`PlacementResult.fragmentation_minutes` (and there is a test pinning that).
"""
from __future__ import annotations

import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence

from .fragmentation import waste_minutes
from .models import CostConfig, PlacementResult
from .placement import _blocks, _origin

_LETTERS = string.ascii_lowercase + string.ascii_uppercase
_EMPTY = "·"


@dataclass(frozen=True)
class Track:
    """One named row of the chart — provider free time, a client's
    availability, anything with `.start` / `.end`. Accepts both
    `calendar_store.TimeSegment` and this package's `TimeRange`.
    """
    name: str
    segments: Sequence
    glyph: str = "█"


@dataclass(frozen=True)
class Gap:
    """A stretch of free time left over after placement."""
    start: datetime
    end: datetime
    minutes: int
    wasted_minutes: int

    @property
    def usable(self) -> bool:
        """Whether a future booking can still use all of this."""
        return self.wasted_minutes == 0


def gaps_left(
    provider_free: Sequence,
    placements: Sequence = (),
    config: Optional[CostConfig] = None,
    *,
    movable: Sequence = (),
    displacements: Sequence = (),
) -> List[Gap]:
    """Every leftover gap, with how much of it is structurally unsellable.

    Computed on the solver's grid-aligned, merged blocks so the totals agree
    with what the solver optimised.

    Pass `movable` and `displacements` when displacement was in play: a booking
    that moved leaves its old slot free and occupies a new one, and a booking
    that was offered up but stayed still occupies its own. Without them the
    figures describe a calendar that never existed.
    """
    config = config or CostConfig()
    grid = config.grid_minutes
    origin = _origin([], provider_free)

    # A movable booking's slot is part of the domain — it is only free if its
    # occupant actually moved.
    domain = list(provider_free) + [m.range for m in movable]
    stayed = {d.appointment_id for d in displacements}
    occupied = [(p.range.start, p.range.end) for p in placements]
    occupied += [(m.range.start, m.range.end) for m in movable if m.id not in stayed]
    occupied += [(d.now.start, d.now.end) for d in displacements]
    booked = sorted(occupied)

    gaps: List[Gap] = []
    for block in _blocks(domain, origin, grid):
        start = origin + timedelta(minutes=block.start_cell * grid)
        end = origin + timedelta(minutes=block.end_cell * grid)

        cursor = start
        for booked_start, booked_end in booked:
            if not start <= booked_start < end:
                continue
            if booked_start > cursor:
                gaps.append(_gap(cursor, booked_start, config))
            cursor = max(cursor, booked_end)
        if end > cursor:
            gaps.append(_gap(cursor, end, config))
    return gaps


def _gap(start: datetime, end: datetime, config: CostConfig) -> Gap:
    minutes = int((end - start).total_seconds() // 60)
    return Gap(
        start=start,
        end=end,
        minutes=minutes,
        wasted_minutes=waste_minutes(minutes, config.service_durations),
    )


def render(
    provider_free: Sequence,
    result: Optional[PlacementResult] = None,
    *,
    tracks: Sequence[Track] = (),
    config: Optional[CostConfig] = None,
    movable: Sequence = (),
    minutes_per_cell: int = 15,
    show_gaps: bool = True,
) -> str:
    """A day-by-day chart, one line per track, plus a legend and gap report."""
    config = config or CostConfig()
    placements = list(result.placements) if result else []
    displacements = list(result.displacements) if result else []

    all_tracks = [Track("free", list(provider_free))] + list(tracks)
    if movable:
        all_tracks.append(Track("booked", [m.range for m in movable], glyph="▓"))
    gaps = (
        gaps_left(provider_free, placements, config,
                  movable=movable, displacements=displacements)
        if show_gaps else []
    )

    days = sorted(
        {seg.start.date() for track in all_tracks for seg in track.segments}
        | {p.range.start.date() for p in placements}
        | {d.now.start.date() for d in displacements}
        | {d.was.start.date() for d in displacements}
    )
    if not days:
        return "(nothing to show)"

    # 2 leading spaces + longest name + at least 1 trailing, so no label ever
    # runs into its own row.
    # Include the rows that are not tracks, or their labels run into the chart.
    label_width = max(
        len(t.name) for t in all_tracks + [Track("placed", ()), Track("moved in", ())]
    ) + 3
    out: List[str] = []
    for day in days:
        out.extend(
            _render_day(
                day, all_tracks, placements, gaps, minutes_per_cell, label_width,
                displacements,
            )
        )
        out.append("")

    if result is not None:
        out.append(_summary(result))
    return "\n".join(out)


def _render_day(
    day: date,
    tracks: Sequence[Track],
    placements: Sequence,
    gaps: Sequence[Gap],
    minutes_per_cell: int,
    label_width: int,
    displacements: Sequence = (),
) -> List[str]:
    spans = [
        (seg.start, seg.end)
        for track in tracks
        for seg in track.segments
        if seg.start.date() == day
    ]
    spans += [(p.range.start, p.range.end) for p in placements if p.range.start.date() == day]
    spans += [(d.now.start, d.now.end) for d in displacements if d.now.start.date() == day]
    if not spans:
        return []

    # Round outward to whole hours so the axis reads naturally.
    first = min(s for s, _ in spans).replace(minute=0)
    last = max(e for _, e in spans)
    if last.minute:
        last = last.replace(minute=0) + timedelta(hours=1)
    cells = max(1, int((last - first).total_seconds() // 60) // minutes_per_cell)

    pad = " " * label_width
    lines = [
        f"{day:%a %Y-%m-%d}",
        pad + _axis(first, cells, minutes_per_cell),
    ]
    for track in tracks:
        row = _row(track.segments, first, cells, minutes_per_cell, lambda _: track.glyph)
        lines.append(f"  {track.name:<{label_width - 2}}" + row)

    today = [p for p in placements if p.range.start.date() == day]
    if today:
        today = sorted(today, key=lambda p: p.range.start)
        letters = {id(p): _LETTERS[i % len(_LETTERS)] for i, p in enumerate(today)}
        row = _row(
            [p.range for p in today],
            first,
            cells,
            minutes_per_cell,
            lambda i: letters[id(today[i])],
        )
        lines.append(f"  {'placed':<{label_width - 2}}" + row)
        lines.append("")
        for placement in today:
            minutes = int(
                (placement.range.end - placement.range.start).total_seconds() // 60
            )
            lines.append(
                f"{pad}{letters[id(placement)]}  "
                f"{placement.range.start:%H:%M}–{placement.range.end:%H:%M}  "
                f"{minutes:>3}m  {placement.request_id} ({placement.client_id})"
            )

    arriving = [d for d in displacements if d.now.start.date() == day]
    leaving = [d for d in displacements if d.was.start.date() == day]
    if arriving:
        row = _row([d.now for d in arriving], first, cells, minutes_per_cell,
                   lambda _: "~")
        lines.append(f"  {'moved in':<{label_width - 2}}" + row)
    if arriving or leaving:
        lines.append("")
        seen = []
        for moved in sorted(leaving + arriving, key=lambda d: d.was.start):
            if moved.appointment_id in seen:
                continue          # a same-day move appears in both lists
            seen.append(moved.appointment_id)
            lines.append(
                f"{pad}~  rebook {moved.appointment_id} ({moved.client_id})  "
                f"{moved.was.start:%a %H:%M} → {moved.now.start:%a %H:%M}"
                f"  ({moved.shift_minutes}m)"
            )

    today_gaps = [g for g in gaps if g.start.date() == day]
    if today_gaps:
        lines.append("")
        for gap in today_gaps:
            note = "reusable" if gap.usable else f"{gap.wasted_minutes}m wasted"
            lines.append(
                f"{pad}gap {gap.start:%H:%M}–{gap.end:%H:%M}  "
                f"{gap.minutes:>3}m  {note}"
            )
    return lines


def _axis(first: datetime, cells: int, minutes_per_cell: int) -> str:
    cells_per_hour = max(1, 60 // minutes_per_cell)
    # Label only as often as "|HH " (4 chars) will fit without colliding.
    step = max(1, -(-4 // cells_per_hour))
    axis = [" "] * cells
    for cell in range(0, cells, cells_per_hour * step):
        stamp = f"|{(first + timedelta(minutes=cell * minutes_per_cell)):%H}"
        for offset, char in enumerate(stamp):
            if cell + offset < cells:
                axis[cell + offset] = char
    return "".join(axis)


def _row(segments, first: datetime, cells: int, minutes_per_cell: int, glyph) -> str:
    """Mark a cell when its midpoint falls inside a segment — avoids the
    off-by-one artefacts that plague boundary-based rendering.
    """
    row = [_EMPTY] * cells
    for index, segment in enumerate(segments):
        for cell in range(cells):
            midpoint = first + timedelta(minutes=cell * minutes_per_cell + minutes_per_cell / 2)
            if segment.start <= midpoint < segment.end:
                row[cell] = glyph(index)
    return "".join(row)


def _summary(result: PlacementResult) -> str:
    total = len(result.placements) + len(result.unplaced)
    parts = [f"placed {len(result.placements)}/{total}"]
    if result.unplaced:
        parts.append("unplaced: " + ", ".join(result.unplaced))
    if result.displacements:
        parts.append(f"rebooked {len(result.displacements)} "
                     f"({result.shift_minutes}m shifted)")
    parts.append(f"fragmentation {result.fragmentation_minutes}m")
    parts.append(f"off-preference {result.preference_gap_minutes}m")
    return " · ".join(parts)
