#!/usr/bin/env python3
"""Validate or import the user's DrumStation v1.2 ROM pair; never downloads ROMs."""
import argparse
import hashlib
from pathlib import Path
import zipfile

EXPECTED = {
    "v1.2.u15": (32768, "1914254c714d35031a456c71b1f9f9191df9126d"),
    "v1.2.u28": (524288, "61474c0bc6cfff3efe95527c57e4891f886b02aa"),
}


def validate(name, data):
    size, digest = EXPECTED[name]
    if len(data) != size or hashlib.sha1(data).hexdigest() != digest:
        raise ValueError(f"{name}: does not match the known v1.2 ROM")


def verify(directory):
    for name in EXPECTED:
        validate(name, (Path(directory) / name).read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-zip", type=Path)
    parser.add_argument("--rom-dir", type=Path, default=Path(__file__).resolve().parents[1] / "roms")
    args = parser.parse_args()
    if args.import_zip:
        with zipfile.ZipFile(args.import_zip) as archive:
            images = {name: archive.read(name) for name in EXPECTED}
        for name, image in images.items():
            validate(name, image)
            dest = args.rom_dir / name
            if dest.exists() and dest.read_bytes() != image:
                raise ValueError(f"Refusing to overwrite different ROM: {dest}")
        args.rom_dir.mkdir(parents=True, exist_ok=True)
        for name, image in images.items():
            (args.rom_dir / name).write_bytes(image)
    verify(args.rom_dir)
    print("Both DrumStation v1.2 ROMs verified.")


if __name__ == "__main__":
    main()
