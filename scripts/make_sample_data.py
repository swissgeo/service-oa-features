"""Rebuild the committed sample-data subset from the full MeteoSwiss OGD export.

Dev-only tool. `scripts/sample-data/` is what `init_database.py` seeds from and
it is committed, so this script only needs to run when the subset should be
refreshed -- a newer forecast run, another parameter, more points or more steps.

The full local-forecasting export is ~32 MB and is *not* committed. Put it in
`sample-data/` at the repository root (or pass the directory as the first
argument):

    sample-data/ogd-local-forecasting_meta_point.csv
    sample-data/ogd-local-forecasting_meta_parameters.csv
    sample-data/<model>.<domain>.<run>.<parameter>.csv   e.g.
    sample-data/vnut12.lssw.202608200000.tre200h0.csv

Usage::

    uv run scripts/make_sample_data.py [SOURCE_DIR]

Each output file keeps its input's name, column layout and `;` delimiter, so it
stays a drop-in subset that `init_database.py` can read the same way it would
read the full export. Two deliberate differences:

* the output is UTF-8 where the source is cp1252, so the committed files diff
  and review cleanly;
* exact duplicate rows in the point metadata are collapsed (the export ships a
  handful of them).

What gets kept is controlled by POINTS_PER_TYPE and FORECAST_STEP_COUNT below.
Points are picked by an even stride through each point type's ids rather than at
random, which keeps the choice reproducible and -- because postal-code ids run
west to east with the postal codes -- spread across the country.
"""

import csv
import logging
import sys
from pathlib import Path

logger = logging.getLogger("make_sample_data")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DIR = REPO_ROOT / "sample-data"
TARGET_DIR = Path(__file__).resolve().parent / "sample-data"

SOURCE_ENCODING = "cp1252"
TARGET_ENCODING = "utf-8"
DELIMITER = ";"

POINT_META_FILENAME = "ogd-local-forecasting_meta_point.csv"
PARAMETER_META_FILENAME = "ogd-local-forecasting_meta_parameters.csv"

# How many points to keep per point_type_id: 1 = station, 2 = postal code centre,
# 3 = point of interest. Enough of each that the seeded collection exercises the
# nullable columns (station_abbr and postal_code are populated for different
# types) without carrying all 5,627 points of a run.
POINTS_PER_TYPE = {
  1: 60,
  2: 20,
  3: 20,
}

# Hourly steps to keep, counting from the earliest step of the run. A full day is
# enough to exercise OGC API `datetime=` filtering; a run ships 220 steps.
FORECAST_STEP_COUNT = 10

# Column order is taken from the header row of each source file, so these are
# only the columns this script needs to reason about.
POINT_ID_COLUMN = "point_id"
POINT_TYPE_COLUMN = "point_type_id"
DATE_COLUMN = "Date"
PARAMETER_SHORTNAME_COLUMN = "parameter_shortname"

PointKey = tuple[int, int]


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
  """Read a source CSV whole, as its header row plus raw rows.

  Rows stay lists rather than dicts so a file can be written back out with its
  column order untouched, whatever columns it happens to have.
  """
  with path.open(encoding=SOURCE_ENCODING, newline="") as handle:
    reader = csv.reader(handle, delimiter=DELIMITER)
    header = next(reader)
    return header, list(reader)


def _write_rows(path: Path, header: list[str], rows: list[list[str]]) -> None:
  # lineterminator is set explicitly because csv defaults to CRLF, and the source
  # files are LF.
  with path.open("w", encoding=TARGET_ENCODING, newline="") as handle:
    writer = csv.writer(handle, delimiter=DELIMITER, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
  logger.info("wrote %s (%d row(s))", path, len(rows))


def _forecast_files(source_dir: Path) -> list[Path]:
  """The value files of the export: every CSV that is not one of the two metadata files."""
  metadata = {POINT_META_FILENAME, PARAMETER_META_FILENAME}
  files = [path for path in sorted(source_dir.glob("*.csv")) if path.name not in metadata]
  if not files:
    raise RuntimeError(f"no forecast value files found in {source_dir}")
  return files


def _parameter_of(path: Path) -> str:
  """The parameter shortname a value file carries, read out of its name.

  ``vnut12.lssw.202608200000.tre200h0.csv`` -> ``tre200h0``. The values column is
  named after the parameter, so the name is what makes the file readable.
  """
  parts = path.name.split(".")
  expected_parts = 5
  if len(parts) != expected_parts:
    raise RuntimeError(f"{path.name}: not a <model>.<domain>.<run>.<parameter>.csv file name")
  return parts[-2]


def _scan_forecast(path: Path) -> tuple[set[PointKey], list[str]]:
  """Collect the points and the steps a value file covers.

  A run is ~1.2 M rows, so the file is streamed twice -- once here to learn what
  is in it, once in `_subset_forecast` to write the selection out -- rather than
  held in memory.

  Steps sort chronologically as strings: the export stamps them YYYYMMDDHHMM.
  """
  points: set[PointKey] = set()
  steps: set[str] = set()
  with path.open(encoding=SOURCE_ENCODING, newline="") as handle:
    for row in csv.DictReader(handle, delimiter=DELIMITER):
      points.add((int(row[POINT_ID_COLUMN]), int(row[POINT_TYPE_COLUMN])))
      steps.add(row[DATE_COLUMN])
  return points, sorted(steps)


def _select_points(available: set[PointKey]) -> set[PointKey]:
  """Pick the subset's points: an even stride through the ids of each point type.

  `available` is already the intersection of the point metadata and the forecast
  values, so nothing selected here can end up without coordinates.
  """
  selected: set[PointKey] = set()
  for point_type, wanted in sorted(POINTS_PER_TYPE.items()):
    ids = sorted(point_id for point_id, type_id in available if type_id == point_type)
    if not ids:
      logger.warning("no points of type %d in the source data", point_type)
      continue
    if wanted >= len(ids):
      logger.info("keeping all %d point(s) of type %d", len(ids), point_type)
      selected.update((point_id, point_type) for point_id in ids)
      continue
    stride = [ids[index * len(ids) // wanted] for index in range(wanted)]
    logger.info("keeping %d of %d point(s) of type %d", wanted, len(ids), point_type)
    selected.update((point_id, point_type) for point_id in stride)
  return selected


def _subset_points(source_dir: Path, selected: set[PointKey]) -> None:
  header, rows = _read_rows(source_dir / POINT_META_FILENAME)
  id_index = header.index(POINT_ID_COLUMN)
  type_index = header.index(POINT_TYPE_COLUMN)

  # The export ships a few rows twice over; keeping the first occurrence of each
  # key makes the subset's primary-key story obvious.
  seen: set[PointKey] = set()
  kept = []
  for row in rows:
    key = (int(row[id_index]), int(row[type_index]))
    if key not in selected or key in seen:
      continue
    seen.add(key)
    kept.append(row)

  _write_rows(TARGET_DIR / POINT_META_FILENAME, header, kept)


def _subset_parameters(source_dir: Path, parameters: set[str]) -> None:
  header, rows = _read_rows(source_dir / PARAMETER_META_FILENAME)
  shortname_index = header.index(PARAMETER_SHORTNAME_COLUMN)
  kept = [row for row in rows if row[shortname_index] in parameters]

  missing = parameters - {row[shortname_index] for row in kept}
  if missing:
    raise RuntimeError(f"{PARAMETER_META_FILENAME} has no row for parameter(s) {', '.join(sorted(missing))}")

  _write_rows(TARGET_DIR / PARAMETER_META_FILENAME, header, kept)


def _subset_forecast(path: Path, selected: set[PointKey], steps: set[str]) -> None:
  with path.open(encoding=SOURCE_ENCODING, newline="") as handle:
    reader = csv.reader(handle, delimiter=DELIMITER)
    header = next(reader)
    id_index = header.index(POINT_ID_COLUMN)
    type_index = header.index(POINT_TYPE_COLUMN)
    date_index = header.index(DATE_COLUMN)
    kept = [
      row for row in reader if row[date_index] in steps and (int(row[id_index]), int(row[type_index])) in selected
    ]

  _write_rows(TARGET_DIR / path.name, header, kept)


def main() -> int:
  logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stdout)

  source_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_SOURCE_DIR
  if not source_dir.is_dir():
    raise RuntimeError(f"source directory {source_dir} does not exist -- see this script's docstring")
  logger.info("reading the full export from %s", source_dir)

  point_header, point_rows = _read_rows(source_dir / POINT_META_FILENAME)
  id_index = point_header.index(POINT_ID_COLUMN)
  type_index = point_header.index(POINT_TYPE_COLUMN)
  documented = {(int(row[id_index]), int(row[type_index])) for row in point_rows}

  forecast_files = _forecast_files(source_dir)
  scans = {path: _scan_forecast(path) for path in forecast_files}

  # Points must appear in every value file as well as in the metadata, so that
  # each selected point has a complete set of readings. `geom` is NOT NULL, which
  # is what rules out the points the metadata does not describe.
  forecast_points = set.intersection(*(points for points, _ in scans.values()))
  undocumented = forecast_points - documented
  if undocumented:
    logger.info("ignoring %d forecast point(s) with no metadata, hence no coordinates", len(undocumented))
  selected = _select_points(forecast_points & documented)

  TARGET_DIR.mkdir(exist_ok=True)
  _subset_points(source_dir, selected)
  _subset_parameters(source_dir, {_parameter_of(path) for path in forecast_files})
  for path, (_, all_steps) in scans.items():
    steps = set(all_steps[:FORECAST_STEP_COUNT])
    logger.info("keeping %d of %d step(s) of %s", len(steps), len(all_steps), path.name)
    _subset_forecast(path, selected, steps)

  return 0


if __name__ == "__main__":
  sys.exit(main())
