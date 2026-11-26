from pathlib import Path
from shutil import copyfile
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent


def create_package(name: str, descriptor: Path, fmu: Path) -> Path:
    output = ROOT / name
    contents = {
        "v1.0/dcpSlaveDescription.dcpx": descriptor.read_bytes(),
        "v1.0/documentation/README.md": (ROOT / "dcp_packages" / "README.md").read_bytes(),
        f"v1.0/assets/{fmu.name}": fmu.read_bytes(),
    }
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for member, data in contents.items():
            archive.writestr(member, data)
    copyfile(output, output.with_suffix(".zip"))
    return output


if __name__ == "__main__":
    controller = create_package(
        "DCP_Controller_Local.dcp",
        ROOT / "dcp_packages" / "controller.dcpx",
        ROOT.parent / "PIController.fmu",
    )
    tank = create_package(
        "DCP_Tank_Local.dcp",
        ROOT / "dcp_packages" / "tank.dcpx",
        ROOT / "fmus" / "Tank.fmu",
    )
    print(controller)
    print(tank)
