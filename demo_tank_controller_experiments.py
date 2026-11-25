r"""Run MA-HS experiments discovered through an FMI-LS-REF manifest.

Examples (PowerShell, from this folder):
    python .\demo_tank_experiments_REF.py --experiment area
    python .\demo_tank_experiments_REF.py --experiment initial-level

The script reads the FMI-LS-REF manifest in PIController_REF.fmu, finds the
related .exp and SSP system files, then reads each experiment's SSV and CSV
resources. FMPy runs the SSP's embedded FMI 2.0 Co-Simulation FMUs. REF indexes
related files; this script supplies the SSP composition execution.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from bisect import bisect_right
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from xml.etree import ElementTree as ET
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

try:
    from fmpy import extract, platform, read_model_description, supported_platforms
    from fmpy.fmi1 import FMICallException
    from fmpy.fmi2 import FMU2Slave
except ImportError as exc:  # Give a useful message before a long traceback.
    raise SystemExit(
        "FMPy is required. Install the dependencies with: python -m pip install fmpy matplotlib"
    ) from exc

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

    def convert(self, value: float) -> float:
        return self.factor * value + self.offset


@dataclass(frozen=True)
class TimeSeries:
    time: tuple[float, ...]
    value: tuple[float, ...]

    def hold(self, t: float) -> float:
        """Return the last sample at or before t (zero-order hold)."""
        return self.value[max(0, bisect_right(self.time, t) - 1)]


@dataclass(frozen=True)
class ExperimentCase:
    name: str
    description: str
    start: float
    stop: float
    step: float
    parameters: dict[str, float]
    stimulus: TimeSeries


@dataclass(frozen=True)
class RunResult:
    name: str
    time: list[float]
    reference: list[float]
    level: list[float]
    inlet_l_s: list[float]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@contextmanager
def open_ssp(ssp_path: Path) -> Iterator[Path]:
    """Extract an SSP archive for the duration of parsing and simulation."""
    if ssp_path.suffix.lower() == ".ssd":
        yield ssp_path.resolve()
        return
    with TemporaryDirectory(prefix="tank_ssp_") as directory:
        extract(str(ssp_path.resolve()), unzipdir=directory)
        ssd_path = Path(directory) / "SystemStructure.ssd"
        if not ssd_path.is_file():
            raise ValueError(f"No SystemStructure.ssd found inside {ssp_path.name}.")
        yield ssd_path


def package_file(directory: Path, source: str, suffix: str) -> Path:
    path = (directory / source).resolve()
    if path.suffix.lower() != suffix or not path.is_relative_to(directory.resolve()):
        raise ValueError(f"Expected a relative {suffix} resource inside the package: {source}")
    if not path.is_file():
        raise FileNotFoundError(f"SSP resource does not exist: {source}")
    return path


REF_DIR = "extra/org.fmi-standard.fmi-ls-ref"
REF_MANIFEST = f"{REF_DIR}/fmi-ls-manifest.xml"
REF_NS = "http://fmi-standard.org/fmi-ls-manifest"


@contextmanager
def open_ref_bundle(ref_fmu_path: Path, selector: str):
    """Resolve the selected .exp and associated SSP via the FMU's REF manifest."""
    ref_fmu_path = ref_fmu_path.resolve()
    if not ref_fmu_path.is_file():
        raise FileNotFoundError(f"REF FMU not found: {ref_fmu_path}")

    with TemporaryDirectory(prefix="tank_ref_") as directory:
        package_root = Path(directory).resolve()
        with ZipFile(ref_fmu_path) as archive:
            if REF_MANIFEST not in archive.namelist():
                raise ValueError(f"{ref_fmu_path.name} has no FMI-LS-REF manifest.")
            for member in archive.namelist():
                if not member.startswith(REF_DIR + "/") or member.endswith("/"):
                    continue
                relative = Path(*member.split("/"))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"Unsafe path in REF FMU: {member}")
                target = (package_root / relative).resolve()
                if not target.is_relative_to(package_root):
                    raise ValueError(f"Unsafe path in REF FMU: {member}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))

        manifest_path = package_root / REF_MANIFEST
        root = ET.parse(manifest_path).getroot()
        if root.attrib.get(f"{{{REF_NS}}}fmi-ls-name") != "org.fmi-standard.fmi-ls-ref":
            raise ValueError("The FMU manifest is not an FMI-LS-REF manifest.")

        manifest_base = manifest_path.parent

        def resolve_source(source: str) -> Path:
            uri = urlsplit(source)
            if uri.scheme or uri.netloc or uri.path.startswith("/"):
                raise ValueError(f"REF source must be a relative URI inside the FMU: {source}")
            target = (manifest_base / unquote(uri.path)).resolve()
            if not target.is_relative_to(package_root) or not target.is_file():
                raise FileNotFoundError(f"Missing REF resource: {source}")
            return target

        related = [node for node in root if local_name(node.tag) == "Related"]
        systems = [node for node in related if node.get("role") == "system"]
        if len(systems) != 1:
            raise ValueError("Expected exactly one role=system SSP resource in the REF manifest.")
        system_path = resolve_source(systems[0].attrib["source"])
        if system_path.suffix.lower() != ".ssp":
            raise ValueError("The related system resource must be an .ssp package.")

        experiments: dict[str, Path] = {}
        for node in related:
            role = node.get("role", "")
            if not role.startswith("experiment"):
                continue
            exp_path = resolve_source(node.attrib["source"])
            if exp_path.suffix.lower() != ".exp":
                raise ValueError(f"Expected a .exp resource, got {exp_path.name}.")
            labels = {label.get("name", "") for label in node
                      if local_name(label.tag) == "Label"}
            matches = [label.removeprefix("demo:") for label in labels
                       if label.startswith("demo:")]
            if len(matches) != 1 or matches[0] in experiments:
                raise ValueError(f"Each experiment needs one unique demo:* label: {exp_path.name}")
            experiments[matches[0]] = exp_path

        if selector not in experiments:
            available = ", ".join(sorted(experiments)) or "none"
            raise ValueError(f"Experiment '{selector}' is not listed in REF; available: {available}.")
        yield experiments[selector], system_path


def read_ssv(path: Path) -> dict[str, float]:
    root = ET.parse(path).getroot()
    values: dict[str, float] = {}
    for parameter in root.iter():
        if local_name(parameter.tag) != "Parameter":
            continue
        real = next((child for child in parameter if local_name(child.tag) == "Real"), None)
        if real is None:
            continue
        name = parameter.attrib.get("name")
        if not name:
            raise ValueError(f"Parameter without a name in {path.name}.")
        value = float(real.attrib["value"])
        if not math.isfinite(value):
            raise ValueError(f"Non-finite parameter {name} in {path.name}.")
        if name in values:
            raise ValueError(f"Duplicate parameter {name} in {path.name}.")
        values[name] = value
    return values


def read_assembly(ssd_path: Path):
    """Read the flat SSP 2.0 system used by TankController.ssp."""
    root = ET.parse(ssd_path).getroot()
    system = root.find("ssd:System", NS)
    if root.get("version") != "2.0" or system is None:
        raise ValueError("Expected an SSP 2.0 system description.")

    components: dict[str, tuple[Path, dict[str, float]]] = {}
    for component in system.findall("ssd:Elements/ssd:Component", NS):
        name = component.attrib["name"]
        fmu_path = package_file(ssd_path.parent, component.attrib["source"], ".fmu")
        parameters: dict[str, float] = {}
        for binding in component.findall("ssd:ParameterBindings/ssd:ParameterBinding", NS):
            ssv_path = package_file(ssd_path.parent, binding.attrib["source"], ".ssv")
            parameters.update(read_ssv(ssv_path))
        components[name] = (fmu_path, parameters)

    if set(components) != {"Controller", "Tank"}:
        raise ValueError("This example expects the Controller and Tank SSP components.")

    connections: list[Connection] = []
    for connection in system.findall("ssd:Connections/ssd:Connection", NS):
        source = (connection.get("startElement"), connection.attrib["startConnector"])
        target = (connection.get("endElement"), connection.attrib["endConnector"])
        transform = connection.find("ssc:LinearTransformation", NS)
        if transform is None:
            connections.append(Connection(source, target))
        else:
            if connection.get("suppressUnitConversion") not in {"true", "1"}:
                raise ValueError("Expected explicit SSP scaling on transformed connections.")
            connections.append(Connection(
                source, target,
                float(transform.get("factor", "1")),
                float(transform.get("offset", "0")),
            ))

    default_experiment = root.find("ssd:DefaultExperiment", NS)
    start = float(default_experiment.get("startTime", "0")) if default_experiment is not None else 0.0
    stop = float(default_experiment.get("stopTime", "120")) if default_experiment is not None else 120.0
    return components, connections, start, stop


def read_csv_series(path: Path, signal_name: str) -> TimeSeries:
    """Read the time,hRef CSV convention used by the supplied experiment files."""
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"Stimuli CSV has no header: {path.name}")
        headers = {name.strip().lower(): name for name in reader.fieldnames if name}
        time_key = headers.get("time") or headers.get("time_s") or headers.get("t")
        signal_key = headers.get(signal_name.lower())
        if time_key is None or signal_key is None:
            raise ValueError(
                f"{path.name} must have columns time and {signal_name}; "
                f"found {reader.fieldnames}."
            )
        times: list[float] = []
        values: list[float] = []
        for line_number, row in enumerate(reader, start=2):
            try:
                t = float(row[time_key])
                value = float(row[signal_key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric CSV row {line_number} in {path.name}.") from exc
            if not math.isfinite(t) or not math.isfinite(value):
                raise ValueError(f"Non-finite CSV value on row {line_number} in {path.name}.")
            if times and t <= times[-1]:
                raise ValueError(f"CSV times must increase strictly in {path.name}.")
            times.append(t)
            values.append(value)
    if len(times) < 2:
        raise ValueError(f"Stimuli CSV needs at least two samples: {path.name}")
    return TimeSeries(tuple(times), tuple(values))


def read_experiment_file(path: Path, default_start: float, default_stop: float) -> tuple[str, str, list[ExperimentCase]]:
    path = path.resolve()
    root = ET.parse(path).getroot()
    if local_name(root.tag) != "Experiments":
        raise ValueError(f"Not an MA-HS Experiments file: {path.name}")

    cases: list[ExperimentCase] = []
    run_nodes = [child for child in root if local_name(child.tag) in {"TransientExperiment", "Experiment"}]
    if not run_nodes:
        raise ValueError(f"No experiment setups found in {path.name}.")

    for run in run_nodes:
        name = run.attrib["name"]
        description = run.attrib.get("description", "")
        start = float(run.attrib.get("startTime", default_start))
        stop = float(run.attrib.get("stopTime", default_stop))
        step = float(run.attrib.get("stepSize", "0.1"))
        if not all(math.isfinite(v) for v in (start, stop, step)) or not start < stop or step <= 0:
            raise ValueError(f"Invalid time interval or step size in experiment {name}.")

        parameters_element = next((c for c in run if local_name(c.tag) == "Parameters"), None)
        if parameters_element is None:
            parameters = {}
        else:
            param_path = (path.parent / parameters_element.attrib["source"]).resolve()
            if not param_path.is_relative_to(path.parent.resolve()) or not param_path.is_file():
                raise FileNotFoundError(f"Missing or invalid parameter resource for {name}.")
            parameters = read_ssv(param_path)

        stimuli_element = next((c for c in run if local_name(c.tag) == "Stimuli"), None)
        if stimuli_element is None:
            raise ValueError(f"Experiment {name} has no <Stimuli> resource.")
        stimuli_path = (path.parent / stimuli_element.attrib["source"]).resolve()
        if not stimuli_path.is_relative_to(path.parent.resolve()) or not stimuli_path.is_file():
            raise FileNotFoundError(f"Missing or invalid stimuli resource for {name}.")
        stimuli = read_csv_series(stimuli_path, "hRef")
        if stimuli.time[0] > start or stimuli.time[-1] < stop:
            raise ValueError(
                f"Stimuli in {stimuli_path.name} must cover the full interval {start:g}–{stop:g} s."
            )
        cases.append(ExperimentCase(name, description, start, stop, step, parameters, stimuli))

    return root.attrib.get("name", path.stem), root.attrib.get("description", ""), cases


def apply_experiment_parameters(components, parameter_values: dict[str, float]):
    """Overlay the experiment SSV values onto the unique matching FMU parameters."""
    adjusted = {name: (path, dict(values)) for name, (path, values) in components.items()}
    destinations: dict[str, list[str]] = {}
    parameter_names: dict[str, set[str]] = {}

    for component_name, (fmu_path, _) in adjusted.items():
        description = read_model_description(str(fmu_path))
        if description.fmiVersion != "2.0" or description.coSimulation is None:
            raise ValueError(f"{fmu_path.name} must provide FMI 2.0 Co-Simulation.")
        names = {
            variable.name for variable in description.modelVariables
            if variable.type == "Real" and variable.causality in {"parameter", "calculatedParameter"}
        }
        parameter_names[component_name] = names
        for name in names:
            destinations.setdefault(name, []).append(component_name)

    for parameter, value in parameter_values.items():
        targets = destinations.get(parameter, [])
        if len(targets) != 1:
            raise ValueError(
                f"Experiment parameter '{parameter}' maps to {len(targets)} components; expected exactly one."
            )
        adjusted[targets[0]][1][parameter] = value

    return adjusted, parameter_names


def check_platform(path: Path) -> None:
    supported = supported_platforms(str(path))
    if platform not in supported:
        raise ValueError(
            f"{path.name} contains {', '.join(supported)} binaries, but this Python uses {platform}."
        )


@contextmanager
def open_fmu(path: Path, name: str, parameters: dict[str, float], start: float, stop: float):
    check_platform(path)
    description = read_model_description(str(path))
    if description.fmiVersion != "2.0" or description.coSimulation is None:
        raise ValueError(f"{path.name} must provide FMI 2.0 Co-Simulation.")
    vr = {
        variable.name: variable.valueReference
        for variable in description.modelVariables
        if variable.type == "Real"
    }
    parameter_names = {
        variable.name for variable in description.modelVariables
        if variable.type == "Real" and variable.causality in {"parameter", "calculatedParameter"}
    }
    unknown = set(parameters) - parameter_names
    if unknown:
        raise ValueError(f"Unknown parameter(s) for {name}: {', '.join(sorted(unknown))}")

    with TemporaryDirectory(prefix=f"tank_{name}_") as directory:
        extract(str(path), unzipdir=directory)
        fmu = FMU2Slave(
            guid=description.guid,
            unzipDirectory=directory,
            modelIdentifier=description.coSimulation.modelIdentifier,
            instanceName=name,
        )
        try:
            fmu.instantiate()
            fmu.setupExperiment(startTime=start, stopTime=stop)
            for parameter, value in parameters.items():
                fmu.setReal([vr[parameter]], [value])
            fmu.enterInitializationMode()
            yield fmu, vr
        finally:
            if fmu.component:
                fmu.freeInstance()
            else:
                fmu.freeLibrary()


def run_case(assembly, case: ExperimentCase) -> RunResult:
    base_components, connections, _, _ = assembly
    components, _ = apply_experiment_parameters(base_components, case.parameters)
    ports = {"hRef": case.stimulus.hold(case.start)}
    time: list[float] = []
    reference: list[float] = []
    level: list[float] = []
    inlet_l_s: list[float] = []

    with ExitStack() as stack:
        models = {
            name: stack.enter_context(open_fmu(path, name, parameters, case.start, case.stop))
            for name, (path, parameters) in components.items()
        }

        def read(port: tuple[str | None, str]) -> float:
            element, signal = port
            if element is None:
                if signal not in ports:
                    raise ValueError(f"Unbound SSP top-level signal: {signal}")
                return ports[signal]
            fmu, references = models[element]
            return fmu.getReal([references[signal]])[0]

        def write(port: tuple[str | None, str], value: float) -> None:
            element, signal = port
            if element is None:
                ports[signal] = value
            else:
                fmu, references = models[element]
                fmu.setReal([references[signal]], [value])

        # Seed each FMU's inputs from the current outputs and top-level reference.
        for source_component in (None, "Tank", "Controller"):
            for connection in connections:
                if connection.source[0] == source_component:
                    write(connection.target, connection.convert(read(connection.source)))
        for fmu, _ in models.values():
            fmu.exitInitializationMode()

        now = case.start
        index = 0
        while True:
            ports["hRef"] = case.stimulus.hold(now)
            values = {
                connection.target: connection.convert(read(connection.source))
                for connection in connections
            }
            time.append(now)
            reference.append(ports["hRef"])
            level.append(values[(None, "h")])
            # qIn is the plant input after the SSP's L/s -> m3/s scaling.
            inlet_l_s.append(1000.0 * values[("Tank", "qIn")])
            if now >= case.stop - 1e-12:
                break

            for target, value in values.items():
                write(target, value)
            index += 1
            next_time = min(case.start + index * case.step, case.stop)
            if next_time <= now:
                raise ValueError("The communication step is too small for this time grid.")
            for fmu, _ in models.values():
                fmu.doStep(currentCommunicationPoint=now, communicationStepSize=next_time - now)
            now = next_time

        for fmu, _ in models.values():
            fmu.terminate()

    return RunResult(case.name, time, reference, level, inlet_l_s)


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_")
    return value or "experiment"


def save_csv(path: Path, result: RunResult) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time_s", "hRef_m", "h_m", "qIn_L_s"))
        writer.writerows(zip(result.time, result.reference, result.level, result.inlet_l_s))


def plot_experiment(title: str, runs: list[RunResult], output: Path, show: bool) -> None:
    if not show:
        import matplotlib
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = ["#007E87", "#BC4E2F", "#4C6990", "#7C6A9A"]
    fig, (ax_level, ax_flow) = plt.subplots(
        2, 1, sharex=True, figsize=(10, 7), layout="constrained"
    )
    first = runs[0]
    ax_level.step(
        first.time, [100 * value for value in first.reference], where="post",
        color="#50636E", linestyle="--", linewidth=1.7, label="Reference hRef",
    )
    for i, run in enumerate(runs):
        color = colors[i % len(colors)]
        ax_level.plot(run.time, [100 * value for value in run.level], color=color,
                      linewidth=2.2, label=run.name)
        ax_flow.plot(run.time, run.inlet_l_s, color=color, linewidth=2.0, label=run.name)
    ax_level.set(title=title, ylabel="Water level (cm)")
    ax_flow.set(xlabel="Time (s)", ylabel="Inlet flow (L/s)")
    for axis in (ax_level, ax_flow):
        axis.grid(alpha=0.25)
        axis.legend(loc="best")
    fig.savefig(output, dpi=180)
    plt.show() if show else plt.close(fig)


def run_experiment(exp_path: Path, ssp_path: Path, output: Path, show: bool) -> None:
    with open_ssp(ssp_path) as ssd_path:
        assembly = read_assembly(ssd_path)
        _, _, default_start, default_stop = assembly
        experiment_name, description, cases = read_experiment_file(
            exp_path, default_start, default_stop
        )
        output.mkdir(parents=True, exist_ok=True)
        results = []
        print(f"\n{experiment_name}")
        if description:
            print(description)
        for case in cases:
            result = run_case(assembly, case)
            results.append(result)
            csv_path = output / f"{slug(exp_path.stem)}_{slug(case.name)}.csv"
            save_csv(csv_path, result)
            final_ref = result.reference[-1]
            final_error_cm = 100 * (final_ref - result.level[-1])
            print(
                f"  {case.name}: final level={100*result.level[-1]:.3f} cm, "
                f"final error={final_error_cm:.3f} cm; CSV: {csv_path.name}"
            )
        figure_path = output / f"{slug(exp_path.stem)}.png"
        plot_experiment(experiment_name, results, figure_path, show)
        print(f"  Plot: {figure_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=("area", "initial-level"),
                        help="Select an experiment listed in the FMI-LS-REF manifest.")
    parser.add_argument("--ref-fmu", type=Path, default=HERE / "PIController_REF.fmu",
                        help="FMU containing the FMI-LS-REF manifest and experiment resources.")
    parser.add_argument("--ssp", type=Path, default=None,
                        help="Optional SSP override; by default the system referenced by REF is used.")
    parser.add_argument("--output", type=Path, default=HERE / "results",
                        help="Folder for simulation PNG and output CSV files.")
    parser.add_argument("--no-show", action="store_true",
                        help="Save results without opening the plot window.")
    args = parser.parse_args()

    try:
        with open_ref_bundle(args.ref_fmu, args.experiment) as (exp_path, ref_ssp_path):
            ssp_path = args.ssp.resolve() if args.ssp else ref_ssp_path
            run_experiment(exp_path, ssp_path, args.output, not args.no_show)
    except (OSError, ValueError, KeyError, BadZipFile, ET.ParseError, FMICallException) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
