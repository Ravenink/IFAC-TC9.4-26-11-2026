import os
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def extract_fmu(archive: Path) -> Path:
    destination = BUILD / "fmus" / archive.stem
    destination.mkdir(parents=True, exist_ok=True)
    binary = (f"binaries/win64/{archive.stem}.dll" if os.name == "nt"
              else f"binaries/linux64/{archive.stem}.so")
    with ZipFile(archive) as zf:
        for required in ("modelDescription.xml", binary):
            if required not in zf.namelist():
                raise RuntimeError(f"{archive}: missing {required}")
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError(f"Unsafe path inside FMU: {member.filename}")
        zf.extractall(destination)
    if not (destination / "modelDescription.xml").is_file():
        raise RuntimeError(f"Invalid FMU archive: {archive.name}")
    return destination


def main() -> None:
    controller_archive = ROOT.parent / "PIController.fmu"
    controller = extract_fmu(controller_archive)
    tank = extract_fmu(ROOT / "fmus" / "Tank.fmu")
    print("FMUs extracted and ready:")
    print(f"  Controller: {controller_archive} -> {controller}")
    print(f"  Tank:       {tank}")


if __name__ == "__main__":
    main()
