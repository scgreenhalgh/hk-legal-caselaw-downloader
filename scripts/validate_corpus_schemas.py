"""Validate a sample of on-disk artifacts against docs/schemas/*.

Run with:
    uv run --with jsonschema python scripts/validate_corpus_schemas.py [OUTPUT_DIR]

OUTPUT_DIR defaults to ./output. Samples a small number of files per
artifact type (deterministic sort) and prints a per-schema PASS/FAIL
summary. Exit 0 iff every sample validates.

The point is to keep the schemas honest: if the on-disk shape drifts
(new field, renamed field, absent optional), the next run flags it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Iterator

try:
    import jsonschema
except ImportError:
    sys.stderr.write("jsonschema not installed. Run: uv run --with jsonschema python scripts/validate_corpus_schemas.py\n")
    sys.exit(2)


REPO = Path(__file__).resolve().parent.parent
SCHEMAS = REPO / "docs" / "schemas"
SAMPLES_PER_TYPE = 5


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def take(paths: Iterator[Path], n: int) -> list[Path]:
    picked: list[Path] = []
    for p in sorted(paths):
        picked.append(p)
        if len(picked) >= n:
            break
    return picked


def find_case_metadata(root: Path) -> list[Path]:
    # {court}/{year}/{court}_{year}_{num}.json — exclude sidecar *.tc.json,
    # *.noteup.json, *.appeal_history.json by dot count in the stem, and
    # skip non-court siblings (d3/, legis/, hopt/, failure_samples/,
    # .enum_cache/, and any other dot-prefixed metadata dirs).
    out: list[Path] = []
    for court_dir in sorted(root.iterdir()):
        if not court_dir.is_dir():
            continue
        if court_dir.name.startswith("."):
            continue
        if court_dir.name in {"d3", "legis", "hopt", "failure_samples"}:
            continue
        for year_dir in sorted(court_dir.iterdir()):
            if not year_dir.is_dir():
                continue
            for f in sorted(year_dir.iterdir()):
                if f.suffix != ".json":
                    continue
                # Only primary metadata — reject sidecars by dot count.
                stem = f.name[: -len(".json")]
                if "." in stem:
                    continue
                out.append(f)
                if len(out) >= SAMPLES_PER_TYPE:
                    return out
    return out


def find_noteup(root: Path) -> list[Path]:
    return take((root / "hkcfa").rglob("*.noteup.json"), SAMPLES_PER_TYPE)


def find_appeal_history(root: Path) -> list[Path]:
    return take((root / "hkcfa").rglob("*.appeal_history.json"), SAMPLES_PER_TYPE)


def find_hopt(root: Path) -> list[Path]:
    return take((root / "hopt").rglob("*.json"), SAMPLES_PER_TYPE)


def find_d3_html(root: Path) -> list[Path]:
    out: list[Path] = []
    for fam in ("hklrccp", "hklrcr", "pcpdc"):
        out.extend(take((root / "d3" / fam).rglob("*.json"), 2))
    return out


def find_d3_pcpdaab(root: Path) -> list[Path]:
    return take((root / "d3" / "pcpdaab").rglob("pcpdaab_*.json"), SAMPLES_PER_TYPE)


def find_legis_versions(root: Path) -> list[Path]:
    return take((root / "legis").rglob("*.versions.json"), SAMPLES_PER_TYPE)


def find_legis_content(root: Path) -> list[Path]:
    # Only the current-in-force files, not the .v{vid}. variants (same shape,
    # but there are 30k of them — pick from both for coverage).
    out: list[Path] = []
    for p in sorted((root / "legis").rglob("*.content.json")):
        out.append(p)
        if len(out) >= SAMPLES_PER_TYPE:
            break
    return out


def find_legis_relatedcaps(root: Path) -> list[Path]:
    return take((root / "legis").rglob("relatedcaps_*.json"), SAMPLES_PER_TYPE)


def find_events_log(root: Path) -> list[Path]:
    p = root / "events.jsonl"
    return [p] if p.exists() else []


def find_failure_headers(root: Path) -> list[Path]:
    return take((root / "failure_samples").glob("*.headers.json"), SAMPLES_PER_TYPE)


def find_enum_cache(root: Path) -> list[Path]:
    return take((root / ".enum_cache").rglob("*_page*.json"), SAMPLES_PER_TYPE)


def validate_file(schema: dict, data) -> str | None:
    try:
        jsonschema.validate(instance=data, schema=schema)
        return None
    except jsonschema.ValidationError as exc:
        return f"{list(exc.absolute_path)}: {exc.message}"


def validate_jsonl(schema: dict, path: Path, limit: int = 200) -> list[str]:
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {i + 1}: not JSON — {exc}")
                continue
            err = validate_file(schema, obj)
            if err:
                errors.append(f"line {i + 1}: {err}")
    return errors


CHECKS: list[tuple[str, str, Callable[[Path], list[Path]]]] = [
    ("case-metadata.schema.json", "court case metadata", find_case_metadata),
    ("noteup.schema.json", "noteup", find_noteup),
    ("appeal-history.schema.json", "appeal history", find_appeal_history),
    ("hopt-entry.schema.json", "HOPT entry", find_hopt),
    ("d3-html.schema.json", "D3 HTML-in-JSON", find_d3_html),
    ("d3-pcpdaab.schema.json", "D3 pcpdaab", find_d3_pcpdaab),
    ("legis-versions.schema.json", "legis versions", find_legis_versions),
    ("legis-content.schema.json", "legis content", find_legis_content),
    ("legis-relatedcaps.schema.json", "legis relatedcaps", find_legis_relatedcaps),
    ("failure-sample-headers.schema.json", "failure sample headers", find_failure_headers),
    ("enum-cache-snapshot.schema.json", "enum cache snapshot", find_enum_cache),
]


def main(argv: list[str]) -> int:
    output_dir = Path(argv[1]).resolve() if len(argv) > 1 else REPO / "output"
    if not output_dir.exists():
        sys.stderr.write(f"output dir not found: {output_dir}\n")
        return 2

    print(f"Validating samples from: {output_dir}")
    print(f"Schemas in: {SCHEMAS}\n")

    any_fail = False

    for schema_file, label, finder in CHECKS:
        schema = load_schema(schema_file)
        samples = finder(output_dir)
        if not samples:
            print(f"  ~  {label}: no samples found (skipped)")
            continue
        fails: list[tuple[Path, str]] = []
        for path in samples:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                fails.append((path, f"not JSON: {exc}"))
                continue
            err = validate_file(schema, data)
            if err:
                fails.append((path, err))
        if fails:
            any_fail = True
            print(f"  X  {label}: {len(fails)}/{len(samples)} failed")
            for path, err in fails:
                print(f"       {path.relative_to(output_dir)}\n         {err}")
        else:
            print(f"  ok {label}: {len(samples)}/{len(samples)}")

    # events.jsonl — separate treatment (JSONL, sample first 200 lines)
    ev_schema = load_schema("events-log.schema.json")
    ev_files = find_events_log(output_dir)
    if ev_files:
        ev = ev_files[0]
        errs = validate_jsonl(ev_schema, ev, limit=200)
        if errs:
            any_fail = True
            print(f"  X  events.jsonl: {len(errs)} errors in first 200 lines")
            for e in errs[:10]:
                print(f"       {e}")
            if len(errs) > 10:
                print(f"       ... and {len(errs) - 10} more")
        else:
            print(f"  ok events.jsonl: first 200 lines valid")
    else:
        print("  ~  events.jsonl: not found (skipped)")

    print()
    if any_fail:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
