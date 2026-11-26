"""Compare P and PI control for TankController.ssp using an explicit FMI master."""

import argparse
import csv
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import BadZipFile

from fmpy import dump, extract, platform, read_model_description, supported_platforms
from fmpy.fmi1 import FMICallException
from fmpy.fmi2 import FMU2Slave


HERE = Path(__file__).resolve().parent
NS = {
    "ssd": "http://ssp-standard.org/SSP1/SystemStructureDescription",
    "ssc": "http://ssp-standard.org/SSP1/SystemStructureCommon",
    "ssv": "http://ssp-standard.org/SSP1/SystemStructureParameterValues",
}


@dataclass(frozen=True)
class Connection:
    source: tuple[str | None, str]
    target: tuple[str | None, str]
    factor: float = 1.0
    offset: float = 0.0

    def convert(self, value):
        return self.factor * value + self.offset


@contextmanager
def open_ssd(path):
    """Open the SSD inside an SSP archive, or directly from an unpacked SSP."""
    if path.suffix.lower() == ".ssd":
        yield path
    else:
        with TemporaryDirectory(prefix="tank_ssp_") as directory:
            extract(str(path), unzipdir=directory)
            yield Path(directory) / "SystemStructure.ssd"


def referenced_file(directory, source, extension):
    path = (directory / source).resolve()
    if path.suffix.lower() != extension or not path.is_relative_to(directory):
        raise ValueError(f"Expected a relative {extension} resource in the package: {source}")
    return path


def read_assembly(ssd):
    """Read the flat, scalar Real subset of SSP 2.0 used by TankController.ssp."""
    root = ET.parse(ssd).getroot()
    system = root.find("ssd:System", NS)
    if root.get("version") != "2.0" or system is None:
        raise ValueError("Expected a flat SSP 2.0 system.")
    components = {}
    for component in system.findall("ssd:Elements/ssd:Component", NS):
        name = component.attrib["name"]
        fmu_path = referenced_file(ssd.parent, component.attrib["source"], ".fmu")
        parameters = {}
        for binding in component.findall("ssd:ParameterBindings/ssd:ParameterBinding", NS):
            ssv = referenced_file(ssd.parent, binding.attrib["source"], ".ssv")
            for parameter in ET.parse(ssv).findall("ssv:Parameters/ssv:Parameter", NS):
                value = parameter.find("ssv:Real", NS)
                if value is None:
                    raise ValueError(f"Expected a scalar Real in {ssv.name}.")
                parameters[parameter.attrib["name"]] = float(value.attrib["value"])
        components[name] = (fmu_path, parameters)
    if components.keys() != {"Controller", "Tank"}:
        raise ValueError("This demo expects the Controller and Tank instances.")

    links = []
    for connection in system.findall("ssd:Connections/ssd:Connection", NS):
        source = (connection.get("startElement"), connection.attrib["startConnector"])
        target = (connection.get("endElement"), connection.attrib["endConnector"])
        transform = connection.find("ssc:LinearTransformation", NS)
        if transform is None:
            links.append(Connection(source, target))
        else:
            if connection.get("suppressUnitConversion") not in {"true", "1"}:
                raise ValueError("This demo expects the SSD's explicit scaling on scaled links.")
            links.append(Connection(source, target, float(transform.get("factor", "1")),
                                    float(transform.get("offset", "0"))))
    experiment = root.find("ssd:DefaultExperiment", NS)
    return components, links, float(experiment.get("startTime", "0")), float(experiment.attrib["stopTime"])


def check_platform(path):
    platforms = supported_platforms(str(path))
    if platform not in platforms:
        raise ValueError(f"{path.name} is built for {', '.join(platforms)}. "
                         f"This Python process uses {platform}.")


@contextmanager
def instance(path, name, parameters, start, stop):
    """Instantiate an FMI 2.0 slave and enter initialization mode."""
    check_platform(path)
    desc = read_model_description(str(path))
    if desc.fmiVersion != "2.0" or desc.coSimulation is None:
        raise ValueError(f"{path.name} must provide FMI 2.0 Co-Simulation.")
    vr = {var.name: var.valueReference for var in desc.modelVariables if var.type == "Real"}
    with TemporaryDirectory(prefix=f"tank_{name}_") as directory:
        extract(str(path), unzipdir=directory)
        fmu = FMU2Slave(guid=desc.guid, unzipDirectory=directory,
                        modelIdentifier=desc.coSimulation.modelIdentifier, instanceName=name)
        try:
            fmu.instantiate()
            fmu.setupExperiment(startTime=start, stopTime=stop)
            for parameter, value in parameters.items():
                if parameter in vr:
                    fmu.setReal([vr[parameter]], [value])
            fmu.enterInitializationMode()
            yield fmu, vr
        finally:
            if fmu.component:
                fmu.freeInstance()
            else:
                fmu.freeLibrary()


def run_closed_loop(assembly, *, h_ref, step, kp=None, ki=None, duration=None):
    """Explicit Jacobi master: read both FMUs before writing their inputs."""
    components, links, start, stop = assembly
    if duration is not None:
        if not isfinite(duration) or duration <= 0:
            raise ValueError("The simulation duration must be positive.")
        stop = min(stop, start + duration)
    if not isfinite(step) or step <= 0 or not start < stop:
        raise ValueError("The communication step and simulation interval must be positive.")
    ports = {"hRef": h_ref}
    time, level, flow = [], [], []

    with ExitStack() as stack:
        models = {}
        for name, (path, parameters) in components.items():
            if name == "Controller":
                overrides = {}
                if kp is not None:
                    overrides["Kp"] = kp
                if ki is not None:
                    overrides["Ki"] = ki
                parameters = {**parameters, **overrides}
            models[name] = stack.enter_context(instance(path, name, parameters, start, stop))

        def read(port):
            element, signal = port
            if element is None:
                return ports[signal]
            fmu, vr = models[element]
            return fmu.getReal([vr[signal]])[0]

        def write(port, value):
            element, signal = port
            if element is None:
                ports[signal] = value
            else:
                fmu, vr = models[element]
                fmu.setReal([vr[signal]], [value])

        for source in (None, "Tank", "Controller"):
            for link in links:
                if link.source[0] == source:
                    write(link.target, link.convert(read(link.source)))
        for fmu, _ in models.values():
            fmu.exitInitializationMode()

        now = start
        index = 0
        while True:
            values = {link.target: link.convert(read(link.source)) for link in links}
            time.append(now)
            level.append(values[None, "h"])
            flow.append(values["Tank", "qIn"])
            if now >= stop:
                break

            for port, value in values.items():
                write(port, value)
            index += 1
            next_time = min(start + index * step, stop)
            if next_time <= now:
                raise ValueError("The communication step is too small for this time grid.")
            for fmu, _ in models.values():
                fmu.doStep(currentCommunicationPoint=now, communicationStepSize=next_time - now)
            now = next_time

        for fmu, _ in models.values():
            fmu.terminate()
    return time, level, flow


def print_assembly(assembly):
    components, links, start, stop = assembly
    for name, (path, values) in components.items():
        print(f"\n{name}  ({path.name})")
        dump(str(path))
        print("SSV values:", ", ".join(f"{key}={value:g}" for key, value in values.items()))
    print(f"\nSSD connections  ({start:g} to {stop:g} s)")
    for link in links:
        source = ".".join(filter(None, link.source))
        target = ".".join(filter(None, link.target))
        print(f"  {source:15s} -> {target:15s}  factor={link.factor:g}  offset={link.offset:g}")


def write_comparison_csv(file, p_run, pi_run):
    with file.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time_s", "h_P_m", "qIn_P_L_s", "h_PI_m", "qIn_PI_L_s"))
        time_p, level_p, flow_p = p_run
        time_pi, level_pi, flow_pi = pi_run
        if time_p != time_pi:
            raise ValueError("P and PI runs must use the same time grid.")
        writer.writerows((t, hp, 1000 * qp, hpi, 1000 * qpi)
                         for t, hp, qp, hpi, qpi in zip(time_p, level_p, flow_p, level_pi, flow_pi))


def write_level_csv(file, result):
    time, level, _ = result
    with file.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time_s", "h_m"))
        writer.writerows(zip(time, level))


def plot_comparison(plt, p_run, pi_run, h_ref, kp, ki, output):
    time_p, level_p, flow_p = p_run
    time_pi, level_pi, flow_pi = pi_run
    colors = {"P": "#007E87", "PI": "#BC4E2F"}
    fig, (ax_level, ax_flow) = plt.subplots(2, 1, sharex=True, figsize=(10, 7), layout="constrained")
    ax_level.plot(time_p, [100 * h for h in level_p], color=colors["P"], lw=2.3,
                  label="P control (Ki = 0)")
    ax_level.plot(time_pi, [100 * h for h in level_pi], color=colors["PI"], lw=2.3,
                  label=f"PI control (Ki = {ki:g} L/s/m/s)")
    ax_level.axhline(100 * h_ref, color="#546976", linestyle="--", lw=1.6,
                     label=f"Setpoint ({100 * h_ref:g} cm)")
    ax_level.set(title=f"Single-tank level control: P vs PI (Kp = {kp:g} L/s/m)",
                 ylabel="Water level (cm)")
    ax_flow.stairs([1000 * q for q in flow_p[:-1]], time_p, baseline=None,
                   color=colors["P"], lw=2, label="P control")
    ax_flow.stairs([1000 * q for q in flow_pi[:-1]], time_pi, baseline=None,
                   color=colors["PI"], lw=2, label="PI control")
    ax_flow.set(xlabel="Time (s)", ylabel="Inlet flow (L/s)", xlim=(time_p[0], time_p[-1]))
    for ax in (ax_level, ax_flow):
        ax.grid(alpha=0.24)
        ax.legend(loc="best")
    if output:
        fig.savefig(output / "tank_P_vs_PI.png", dpi=180)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="run", choices=("run", "info"))
    parser.add_argument("--ssp", type=Path, default=HERE / "TankController.ssp",
                        help="SSP package or path to an unpacked SystemStructure.ssd")
    parser.add_argument("--step", type=float, default=1.0,
                        help="closed-loop communication step in seconds (default: 1)")
    parser.add_argument("--href-cm", type=float, default=43.0, help="closed-loop level setpoint in cm")
    parser.add_argument("--duration", type=float, help="override the SSP stop time")
    parser.add_argument("--kp", type=float, help="override Kp from Controller.ssv for both runs")
    parser.add_argument("--ki", type=float, help="override Ki from Controller.ssv for the PI run")
    parser.add_argument("--output", type=Path, metavar="DIR", help="save a PNG figure and comparison CSV")
    parser.add_argument("--ssp-reference", type=Path, metavar="CSV",
                        help="save the P-control level trace as time_s,h_m for a protocol comparison")
    parser.add_argument("--csv-only", action="store_true", help="save CSV samples without plotting")
    parser.add_argument("--no-show", action="store_true", help="run without opening a plot window")
    args = parser.parse_args()
    if (not isfinite(args.href_cm) or not isfinite(args.step) or args.step <= 0 or
            (args.kp is not None and not isfinite(args.kp)) or
            (args.ki is not None and not isfinite(args.ki)) or
            (args.duration is not None and (not isfinite(args.duration) or args.duration <= 0))):
        parser.error("Values must be finite, with positive step and duration.")
    try:
        run(args)
    except (OSError, ValueError, KeyError, BadZipFile, ET.ParseError, FMICallException) as error:
        parser.exit(1, f"Error: {error}\n")


def run(args):
    h_ref = args.href_cm / 100
    with open_ssd(args.ssp.resolve()) as ssd:
        assembly = read_assembly(ssd)
        if args.mode == "info":
            print_assembly(assembly)
            return

        controller_params = assembly[0]["Controller"][1]
        kp = controller_params["Kp"] if args.kp is None else args.kp
        ki = controller_params.get("Ki", 0.0) if args.ki is None else args.ki
        p_run = run_closed_loop(assembly, h_ref=h_ref, step=args.step, kp=kp, ki=0.0,
                                duration=args.duration)
        pi_run = run_closed_loop(assembly, h_ref=h_ref, step=args.step, kp=kp, ki=ki,
                                 duration=args.duration)
        print(f"Single tank: setpoint={args.href_cm:g} cm, Kp={kp:g} L/s/m, "
              f"PI Ki={ki:g} L/s/m/s, H={args.step:g} s")
        for label, result in (("P", p_run), ("PI", pi_run)):
            time, level, flow = result
            print(f"  {label}: h({time[-1]:g})={100 * level[-1]:.3f} cm, "
                  f"final error={100 * (h_ref - level[-1]):.3f} cm, "
                  f"initial flow={1000 * flow[0]:.3f} L/s")

    if args.output is not None:
        args.output.mkdir(parents=True, exist_ok=True)
        write_comparison_csv(args.output / "tank_P_vs_PI.csv", p_run, pi_run)
    if args.ssp_reference is not None:
        args.ssp_reference.parent.mkdir(parents=True, exist_ok=True)
        write_level_csv(args.ssp_reference, p_run)
    if args.csv_only or (args.no_show and args.output is None):
        if args.csv_only and args.output is None and args.ssp_reference is None:
            raise ValueError("--csv-only requires --output DIR or --ssp-reference CSV.")
        return
    import matplotlib

    if args.no_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plot_comparison(plt, p_run, pi_run, h_ref, kp, ki, args.output)
    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
