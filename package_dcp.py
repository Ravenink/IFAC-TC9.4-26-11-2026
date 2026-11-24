from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent


def create_package(name: str, descriptor: Path, fmu: Path, asset_name: str) -> Path:
    output = ROOT / name
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(descriptor, "v1.0/dcpSlaveDescription.dcpx")
        archive.write(ROOT / "dcp_packages" / "README.md", "v1.0/documentation/README.md")
        archive.write(fmu, f"v1.0/assets/{asset_name}")
    return output


if __name__ == "__main__":
    controller = create_package(
        "DCP_Controller_Local.dcp",
        ROOT / "dcp_packages" / "controller.dcpx",
        ROOT / "fmus" / "PController.fmu",
        "PController.fmu",
    )
    tank = create_package(
        "DCP_Tank_Local.dcp",
        ROOT / "dcp_packages" / "tank.dcpx",
        ROOT / "fmus" / "Tank.fmu",
        "Tank.fmu",
    )
    print(controller)
    print(tank)
