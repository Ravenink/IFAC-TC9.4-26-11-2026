from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def extract_fmu(filename: str, folder: str) -> Path:
    archive = ROOT / "fmus" / filename
    destination = BUILD / "fmus" / folder
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError(f"Unsafe path inside FMU: {member.filename}")
        zf.extractall(destination)
    if not (destination / "modelDescription.xml").is_file():
        raise RuntimeError(f"Invalid FMU archive: {archive.name}")
    return destination


def main() -> None:
    controller = extract_fmu("PController.fmu", "PController")
    tank = extract_fmu("Tank.fmu", "Tank")
    if __import__("os").name == "nt":
        expected = [(controller, "PController.dll"), (tank, "Tank.dll")]
        binary_folder = "win64"
    else:
        expected = [(controller, "PController.so"), (tank, "Tank.so")]
        binary_folder = "linux64"
    for folder, binary in expected:
        path = folder / "binaries" / binary_folder / binary
        if not path.is_file():
            raise RuntimeError(f"The FMU has no {binary_folder} binary: {path}")
    print("FMUs extracted and ready:")
    print(f"  Controller: {controller}")
    print(f"  Tank:       {tank}")


if __name__ == "__main__":
    main()
