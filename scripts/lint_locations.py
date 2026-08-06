#!/usr/bin/env python3
"""
lint_locations.py - Validate data/locations.toml before it reaches the kneeboard.

Catches the failure modes that produce a silently wrong kneeboard: missing
elevations on newly built FARPs, coordinates that disagree with each other,
values the gomplate template would choke on, and duplicate names.

Exit codes: 0 clean, 1 problems found, 2 misuse.

Usage: lint_locations.py [OPTIONS] [path]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _missing_dependency(error: ModuleNotFoundError) -> SystemExit:
    """A traceback here means "not installed", which is worth saying plainly.

    Printing the interpreter path matters when mise is in play: the usual cause
    is that the packages went into a different Python than the one running this.
    """
    return SystemExit(
        f"Missing dependency: {error.name}\n"
        f"  running: {sys.executable}\n"
        "  install: pip install -r scripts/requirements.txt (or `just install`)"
    )


try:
    import mgrs
    from pyproj import Geod
except ModuleNotFoundError as _error:
    raise _missing_dependency(_error) from None

DEFAULT_LOCATIONS_PATH = "data/locations.toml"
VALID_TYPES = ("farp", "airport")

# DMS is stored to whole seconds, which is up to ~22 m of rounding at these
# latitudes. Anything past this means the two coordinate systems describe
# genuinely different places, which is a transcription error worth failing on.
DEFAULT_COORDINATE_TOLERANCE_M = 60.0

# MGRS latitude bands. I and O are skipped to avoid confusion with 1 and 0.
MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX"

INTEGER_RE = re.compile(r"^-?\d+$")
DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")
ICAO_RE = re.compile(r"^[A-Z]{4}$")
SHORTNAME_MAX_LENGTH = 6
SHORTNAME_RE = re.compile(rf"^[A-Z0-9]{{1,{SHORTNAME_MAX_LENGTH}}}$")
TACAN_RE = re.compile(r"^\d{1,3}[XY]$")
HEADING_RE = re.compile(r"^\d{3}$")
RUNWAY_NAME_RE = re.compile(r"^\d{2}[LCR]?$")

_MGRS = mgrs.MGRS()
_GEOD = Geod(ellps="WGS84")

log = logging.getLogger("lint_locations")


@dataclass
class Problem:
    line: int
    label: str
    message: str

    def as_annotation(self, path: Path) -> str:
        return f"::error file={path},line={self.line}::{self.label}: {self.message}"

    def as_text(self, path: Path) -> str:
        return f"{path}:{self.line}: {self.label}: {self.message}"


class Linter:
    def __init__(self, path: Path, tolerance_m: float) -> None:
        self.path = path
        self.tolerance_m = tolerance_m
        self.problems: list[Problem] = []
        self.line = 1
        self.label = str(path)

    def fail(self, message: str) -> None:
        self.problems.append(Problem(self.line, self.label, message))

    # -- field helpers ---------------------------------------------------

    def require(self, table: dict[str, Any], key: str, context: str) -> Any:
        value = table.get(key)
        if value is None:
            self.fail(f"{context} is missing `{key}`")
        return value

    def require_text(self, table: dict[str, Any], key: str, context: str) -> str | None:
        value = table.get(key)
        if value is None:
            self.fail(f"{context} is missing `{key}`")
            return None
        if not isinstance(value, str):
            self.fail(f"{context} `{key}` must be a quoted string, got {value!r}")
            return None
        if not value.strip():
            # The empty-string sentinel update_locations.py writes for an
            # unsurveyed FARP lands here, which is the point.
            self.fail(f"{context} `{key}` is empty and must be filled in")
            return None
        return value

    def require_pattern(
        self, table: dict[str, Any], key: str, pattern: re.Pattern[str], context: str, shape: str
    ) -> str | None:
        value = self.require_text(table, key, context)
        if value is None:
            return None
        if not pattern.match(value):
            self.fail(f"{context} `{key}` is {value!r}, expected {shape}")
            return None
        return value

    # -- structural checks -----------------------------------------------

    def check_dms(self, dms: dict[str, Any], context: str) -> tuple[float, float] | None:
        axes = {
            "latitude": ("NS", 90),
            "longitude": ("EW", 180),
        }
        decimal: dict[str, float] = {}
        for axis, (directions, max_degrees) in axes.items():
            table = dms.get(axis)
            if not isinstance(table, dict):
                self.fail(f"{context} is missing `coordinates.dms.{axis}`")
                return None
            axis_context = f"{context} `coordinates.dms.{axis}`"

            direction = table.get("direction")
            if direction not in tuple(directions):
                self.fail(f"{axis_context} direction is {direction!r}, expected one of {directions}")
                return None

            parts = []
            for key, limit in (("degrees", max_degrees), ("minutes", 59), ("seconds", 59)):
                value = table.get(key)
                if not isinstance(value, int) or isinstance(value, bool):
                    self.fail(f"{axis_context} `{key}` must be a bare integer, got {value!r}")
                    return None
                if not 0 <= value <= limit:
                    self.fail(f"{axis_context} `{key}` is {value}, expected 0-{limit}")
                    return None
                parts.append(value)

            value = parts[0] + parts[1] / 60 + parts[2] / 3600
            decimal[axis] = -value if direction in "SW" else value

        return decimal["latitude"], decimal["longitude"]

    def check_mgrs(self, grid: dict[str, Any], context: str) -> tuple[float, float] | None:
        zone_number = self.require_text(grid, "zone_number", context)
        zone_band = self.require_text(grid, "zone_band", context)
        square = self.require_text(grid, "grid", context)
        easting = self.require_pattern(
            grid, "easting", re.compile(r"^\d{5}$"), context, "five digits"
        )
        northing = self.require_pattern(
            grid, "northing", re.compile(r"^\d{5}$"), context, "five digits"
        )
        if not all((zone_number, zone_band, square, easting, northing)):
            return None

        if not (INTEGER_RE.match(zone_number) and 1 <= int(zone_number) <= 60):
            self.fail(f"{context} `coordinates.mgrs.zone_number` is {zone_number!r}, expected 1-60")
            return None
        if zone_band not in MGRS_BANDS:
            self.fail(
                f"{context} `coordinates.mgrs.zone_band` is {zone_band!r}, "
                f"expected one of {MGRS_BANDS}"
            )
            return None
        if not re.match(r"^[A-Z]{2}$", square):
            self.fail(f"{context} `coordinates.mgrs.grid` is {square!r}, expected two letters")
            return None

        raw = f"{zone_number}{zone_band}{square}{easting}{northing}"
        try:
            return _MGRS.toLatLon(raw)
        except Exception as error:
            self.fail(f"{context} MGRS {raw} is not a valid grid reference ({error})")
            return None

    def check_coordinates(self, location: dict[str, Any], context: str) -> None:
        coordinates = location.get("coordinates")
        if not isinstance(coordinates, dict):
            self.fail(f"{context} is missing `coordinates`")
            return

        dms_table = coordinates.get("dms")
        grid_table = coordinates.get("mgrs")
        if not isinstance(dms_table, dict):
            self.fail(f"{context} is missing `coordinates.dms`")
            dms_table = None
        if not isinstance(grid_table, dict):
            self.fail(f"{context} is missing `coordinates.mgrs`")
            grid_table = None

        from_dms = self.check_dms(dms_table, context) if dms_table else None
        from_mgrs = self.check_mgrs(grid_table, context) if grid_table else None
        if from_dms is None or from_mgrs is None:
            return

        _, _, metres = _GEOD.inv(from_dms[1], from_dms[0], from_mgrs[1], from_mgrs[0])
        if abs(metres) > self.tolerance_m:
            self.fail(
                f"{context} DMS and MGRS describe positions {abs(metres):.0f} m apart "
                f"(tolerance {self.tolerance_m:.0f} m); one of them is wrong"
            )

    def check_navaids(self, location: dict[str, Any], context: str) -> None:
        navaids = location.get("navaids")
        if navaids is None:
            if location.get("type") == "farp":
                self.fail(f"{context} is a FARP with no `navaids`; ADF and FM are required")
            return
        if not isinstance(navaids, dict):
            self.fail(f"{context} `navaids` must be a table")
            return

        if location.get("type") == "farp":
            for key in ("adf", "fm"):
                if key not in navaids:
                    self.fail(f"{context} is a FARP missing `navaids.{key}`")

        for key in ("vor", "ndb", "adf", "fm"):
            if key in navaids:
                self.require_pattern(navaids, key, DECIMAL_RE, f"{context} `navaids`", "a number")
        if "tacan" in navaids:
            self.require_pattern(
                navaids, "tacan", TACAN_RE, f"{context} `navaids`", "a channel like 98X"
            )

    def check_frequencies(self, location: dict[str, Any], context: str) -> None:
        frequencies = location.get("frequencies")
        if frequencies is None:
            return
        if not isinstance(frequencies, dict):
            self.fail(f"{context} `frequencies` must be a table")
            return
        for key in ("hf", "fm", "vhf", "uhf"):
            if key in frequencies:
                self.require_pattern(
                    frequencies, key, DECIMAL_RE, f"{context} `frequencies`", "a number"
                )

    def check_runways(self, location: dict[str, Any], context: str) -> None:
        runways = location.get("runways")
        if runways is None:
            return
        if not isinstance(runways, list):
            self.fail(f"{context} `runways` must be an array of tables")
            return

        for index, runway in enumerate(runways):
            runway_context = f"{context} runway {index + 1}"
            if not isinstance(runway, dict):
                self.fail(f"{runway_context} must be a table")
                continue
            for key in ("elevation_feet", "length_feet"):
                self.require_pattern(runway, key, INTEGER_RE, runway_context, "a whole number")
            for end in ("primary", "secondary"):
                table = runway.get(end)
                if not isinstance(table, dict):
                    self.fail(f"{runway_context} is missing `{end}`")
                    continue
                self.require_pattern(
                    table, "name", RUNWAY_NAME_RE, f"{runway_context} `{end}`", "a designator like 01L"
                )
                self.require_pattern(
                    table, "heading", HEADING_RE, f"{runway_context} `{end}`", "three digits"
                )
                heading = table.get("heading")
                if isinstance(heading, str) and HEADING_RE.match(heading) and int(heading) > 359:
                    self.fail(f"{runway_context} `{end}` heading is {heading}, expected 000-359")

    def check_location(self, location: dict[str, Any]) -> str | None:
        name = self.require_text(location, "name", "location")
        context = f"{name or '<unnamed>'}"

        location_type = self.require_text(location, "type", context)
        if location_type is not None and location_type not in VALID_TYPES:
            self.fail(f"{context} `type` is {location_type!r}, expected one of {VALID_TYPES}")

        self.require_pattern(location, "elevation_feet", INTEGER_RE, context, "a whole number")

        if location_type == "farp" or "shortname" in location:
            self.require_pattern(
                location,
                "shortname",
                SHORTNAME_RE,
                context,
                f"1-{SHORTNAME_MAX_LENGTH} uppercase letters or digits",
            )

        if "icao" in location:
            self.require_pattern(location, "icao", ICAO_RE, context, "four uppercase letters")
        if "callsign" in location:
            self.require_text(location, "callsign", context)

        self.check_coordinates(location, context)
        self.check_navaids(location, context)
        self.check_frequencies(location, context)
        self.check_runways(location, context)
        return name

    # -- driver ----------------------------------------------------------

    def line_numbers(self, text: str) -> list[int]:
        """Line of each `[[location]]` header, so problems point somewhere useful."""
        return [
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip() == "[[location]]"
        ]

    def run(self, check_format: bool) -> list[Problem]:
        text = self.path.read_text(encoding="utf-8")
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            self.problems.append(Problem(1, str(self.path), f"invalid TOML: {error}"))
            return self.problems

        locations = document.get("location")
        if not isinstance(locations, list) or not locations:
            self.problems.append(Problem(1, str(self.path), "no `[[location]]` entries found"))
            return self.problems

        headers = self.line_numbers(text)
        seen: dict[str, int] = {}
        shortnames: dict[str, str] = {}

        for index, location in enumerate(locations):
            self.line = headers[index] if index < len(headers) else 1
            self.label = f"location {index + 1}"
            if not isinstance(location, dict):
                self.fail("entry is not a table")
                continue

            name = self.check_location(location)
            if name:
                key = name.upper()
                if key in seen:
                    self.fail(f"{name} duplicates the name used at location {seen[key] + 1}")
                else:
                    seen[key] = index

                shortname = location.get("shortname")
                if isinstance(shortname, str) and shortname:
                    # A duplicate identifier in a nav database routes you to the
                    # wrong place without ever looking wrong.
                    if shortname in shortnames:
                        self.fail(
                            f"{name} shortname {shortname!r} is already used by "
                            f"{shortnames[shortname]}"
                        )
                    else:
                        shortnames[shortname] = name

        if check_format:
            self.check_canonical_format(text, locations)

        self.problems.sort(key=lambda problem: problem.line)
        return self.problems

    def check_canonical_format(self, text: str, locations: list[Any]) -> None:
        """The file is generated, so hand edits should match the generator's output."""
        try:
            from update_locations import emit_locations
        except ImportError:
            log.warning("update_locations.py not importable; skipping format check")
            return
        try:
            expected = emit_locations(sorted(locations, key=lambda item: item.get("name", "")))
        except Exception as error:
            log.warning("Could not render canonical form (%s); skipping format check", error)
            return
        if expected != text:
            self.problems.append(
                Problem(
                    1,
                    str(self.path),
                    "file is not in canonical form; run scripts/update_locations.py "
                    "(or --no-check-format to skip this check)",
                )
            )


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lint_locations.py",
        description="Validate locations.toml for missing or inconsistent data.",
    )
    parser.add_argument(
        "path", nargs="?", type=Path, default=Path(DEFAULT_LOCATIONS_PATH), help="file to check"
    )
    parser.add_argument("--tolerance-m", type=float, default=DEFAULT_COORDINATE_TOLERANCE_M)
    parser.add_argument(
        "--no-check-format",
        action="store_true",
        help="skip the check that the file matches the generator's output",
    )
    parser.add_argument(
        "--annotations",
        action="store_true",
        help="emit GitHub Actions ::error annotations in addition to plain output",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_arguments(argv)
    level_threshold = (
        logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    )
    # Level names carry their own colon so the label can be padded as one unit,
    # which lines the messages up in the same column the shell scripts use.
    for level, name in (
        (logging.DEBUG, "DEBUG:"),
        (logging.INFO, "INFO:"),
        (logging.WARNING, "WARNING:"),
        (logging.ERROR, "ERROR:"),
        (logging.CRITICAL, "CRITICAL:"),
    ):
        logging.addLevelName(level, name)
    logging.basicConfig(
        level=level_threshold,
        format="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    if not args.path.is_file():
        log.error("File not found: %s", args.path)
        return 2

    linter = Linter(args.path, args.tolerance_m)
    problems = linter.run(check_format=not args.no_check_format)

    # Annotations go to stdout because that is where Actions reads workflow
    # commands from; everything else is diagnostic and belongs on stderr.
    for problem in problems:
        if args.annotations:
            print(problem.as_annotation(args.path))
        else:
            print(problem.as_text(args.path), file=sys.stderr)

    if problems:
        log.error("%s: %d problem(s) found", args.path, len(problems))
        return 1

    log.info("%s: OK", args.path)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv[1:]))
