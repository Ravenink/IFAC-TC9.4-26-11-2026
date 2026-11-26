"""Compare the supplied CS tank loop with a triggered FMI 3 SE PI controller."""

from __future__ import annotations

import argparse
import ctypes
import csv
import math
import os
import platform
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from fmpy import extract, platform as fmpy_platform, read_model_description, supported_platforms
from fmpy.fmi2 import FMU2Slave


HERE = Path(__file__).resolve().parent
NS = {
    "ssd": "http://ssp-standard.org/SSP1/SystemStructureDescription",
    "ssc": "http://ssp-standard.org/SSP1/SystemStructureCommon",
    "ssv": "http://ssp-standard.org/SSP1/SystemStructureParameterValues",
}
H_SCALE_M = 1.0
Q_SCALE_M3_S = 0.001  # 1 L/s


@dataclass(frozen=True)
class Link:
    source: tuple[str | None, str]
    target: tuple[str | None, str]
    factor: float = 1.0
    offset: float = 0.0

    def convert(self, value: float) -> float:
        return self.factor * value + self.offset


@dataclass
class Assembly:
    directory: Path
    components: dict[str, tuple[Path, dict[str, float], str]]
    links: list[Link]
    start: float
    stop: float


def _safe_extract_zip(archive: Path, directory: Path) -> None:
    root = directory.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe path in package: {info.filename}")
            target = (directory / Path(*relative.parts)).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe path in package: {info.filename}")
        zf.extractall(directory)


def _read_parameter_set(path: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    for parameter in ET.parse(path).findall("ssv:Parameters/ssv:Parameter", NS):
        real = parameter.find("ssv:Real", NS)
        if real is None:
            raise ValueError(f"Expected scalar Real parameters in {path.name}.")
        values[parameter.attrib["name"]] = float(real.attrib["value"])
    return values


def read_assembly(ssp_path: Path, extraction_directory: Path) -> Assembly:
    _safe_extract_zip(ssp_path, extraction_directory)
    ssd_path = extraction_directory / "SystemStructure.ssd"
    root = ET.parse(ssd_path).getroot()
    system = root.find("ssd:System", NS)
    if root.get("version") != "2.0" or system is None:
        raise ValueError(f"{ssp_path.name} must contain a flat SSP 2.0 system.")

    components: dict[str, tuple[Path, dict[str, float], str]] = {}
    for component in system.findall("ssd:Elements/ssd:Component", NS):
        name = component.attrib["name"]
        source = (extraction_directory / component.attrib["source"]).resolve()
        if extraction_directory.resolve() not in source.parents or source.suffix.lower() != ".fmu":
            raise ValueError(f"Invalid FMU resource in {ssp_path.name}: {component.get('source')}")
        parameters: dict[str, float] = {}
        for binding in component.findall("ssd:ParameterBindings/ssd:ParameterBinding", NS):
            ssv_path = (extraction_directory / binding.attrib["source"]).resolve()
            if extraction_directory.resolve() not in ssv_path.parents or ssv_path.suffix.lower() != ".ssv":
                raise ValueError(f"Invalid SSV resource in {ssp_path.name}: {binding.get('source')}")
            parameters.update(_read_parameter_set(ssv_path))
        components[name] = (source, parameters, component.get("implementation", "any"))

    if set(components) != {"Controller", "Tank"}:
        raise ValueError("This demonstration expects Controller and Tank components.")

    links: list[Link] = []
    for connection in system.findall("ssd:Connections/ssd:Connection", NS):
        source = (connection.get("startElement"), connection.attrib["startConnector"])
        target = (connection.get("endElement"), connection.attrib["endConnector"])
        transform = connection.find("ssc:LinearTransformation", NS)
        if transform is None:
            links.append(Link(source, target))
        else:
            links.append(Link(
                source,
                target,
                float(transform.get("factor", "1")),
                float(transform.get("offset", "0")),
            ))
    experiment = root.find("ssd:DefaultExperiment", NS)
    start = float(experiment.get("startTime", "0")) if experiment is not None else 0.0
    stop = float(experiment.get("stopTime", "120")) if experiment is not None else 120.0
    return Assembly(extraction_directory, components, links, start, stop)


def _check_cs_platform(path: Path) -> None:
    if fmpy_platform not in supported_platforms(str(path)):
        raise ValueError(
            f"{path.name} has no binary for {fmpy_platform}; "
            f"available: {', '.join(supported_platforms(str(path)))}"
        )


@contextmanager
def open_fmi2_cs(path: Path, name: str, parameters: dict[str, float], start: float, stop: float):
    _check_cs_platform(path)
    description = read_model_description(str(path))
    if description.fmiVersion != "2.0" or description.coSimulation is None:
        raise ValueError(f"{path.name} must be an FMI 2.0 Co-Simulation FMU.")
    variables = {
        variable.name: variable.valueReference
        for variable in description.modelVariables
        if variable.type == "Real"
    }
    with tempfile.TemporaryDirectory(prefix=f"event_{name}_") as directory:
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
                if parameter in variables:
                    fmu.setReal([variables[parameter]], [value])
            fmu.enterInitializationMode()
            yield fmu, variables
        finally:
            if fmu.component:
                try:
                    fmu.terminate()
                except Exception:
                    pass
                fmu.freeInstance()
            fmu.freeLibrary()


class ScheduledPI:
    """Small FMI 3 SE importer for the one-clock controller used in this demo."""

    VR = {"r": 0, "y": 1, "u": 2, "controlEvent": 3, "Kp": 4, "Ki": 5, "uBias": 6}

    def __init__(self, fmu_path: Path, extraction_directory: Path, name: str = "PIControllerSE"):
        _safe_extract_zip(fmu_path, extraction_directory)
        description_path = extraction_directory / "modelDescription.xml"
        root = ET.parse(description_path).getroot()
        if root.get("fmiVersion", "").split(".")[0] != "3":
            raise ValueError("The event controller must be an FMI 3 FMU.")
        se = root.find("ScheduledExecution")
        if se is None:
            raise ValueError("PIControllerSE.fmu does not declare ScheduledExecution.")
        self.clock_reference = self.VR["controlEvent"]
        library_name = se.attrib["modelIdentifier"]
        if os.name == "nt":
            binary = extraction_directory / "binaries" / "win64" / f"{library_name}.dll"
        elif platform.system() == "Darwin":
            binary = extraction_directory / "binaries" / "darwin64" / f"{library_name}.dylib"
        else:
            binary = extraction_directory / "binaries" / "linux64" / f"{library_name}.so"
        if not binary.is_file():
            if os.name == "nt":
                hint = "Run `python build_controller_se_windows.py` once, then rerun this demo."
            else:
                hint = f"Missing {binary.relative_to(extraction_directory)} in the FMU."
            raise FileNotFoundError(f"No Scheduled Execution binary for this platform. {hint}")

        self._library = ctypes.CDLL(str(binary))
        self._functions = {}
        self._bind(
            "fmi3InstantiateScheduledExecution", ctypes.c_void_p,
            [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
             ctypes.c_uint8, ctypes.c_uint8,
             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
             ctypes.c_void_p, ctypes.c_void_p],
        )
        self._bind(
            "fmi3EnterInitializationMode", ctypes.c_int32,
            [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_double,
             ctypes.c_double, ctypes.c_uint8, ctypes.c_double],
        )
        self._bind("fmi3ExitInitializationMode", ctypes.c_int32, [ctypes.c_void_p])
        self._bind(
            "fmi3SetFloat64", ctypes.c_int32,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
             ctypes.POINTER(ctypes.c_double), ctypes.c_size_t],
        )
        self._bind(
            "fmi3GetFloat64", ctypes.c_int32,
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
             ctypes.POINTER(ctypes.c_double), ctypes.c_size_t],
        )
        self._bind(
            "fmi3ActivateModelPartition", ctypes.c_int32,
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double],
        )
        self._bind("fmi3Terminate", ctypes.c_int32, [ctypes.c_void_p])
        self._bind("fmi3FreeInstance", None, [ctypes.c_void_p])

        token = root.attrib["instantiationToken"].encode("utf-8")
        self.instance = self._functions["fmi3InstantiateScheduledExecution"](
            name.encode("utf-8"), token, None, 0, 0,
            None, None, None, None, None,
        )
        if not self.instance:
            raise RuntimeError("FMI 3 Scheduled Execution controller instantiation failed.")
        self.closed = False
        self.start_time = 0.0

    def _bind(self, name, result_type, argument_types):
        function = getattr(self._library, name)
        function.restype = result_type
        function.argtypes = argument_types
        self._functions[name] = function

    @staticmethod
    def _status(status: int, operation: str) -> None:
        if status not in (0, 1):
            raise RuntimeError(f"FMI 3 controller error during {operation}: status {status}.")

    def _set_real(self, references: list[int], values: list[float]) -> None:
        vr = (ctypes.c_uint32 * len(references))(*references)
        data = (ctypes.c_double * len(values))(*values)
        status = self._functions["fmi3SetFloat64"](
            self.instance, vr, len(references), data, len(values)
        )
        self._status(status, "setting Float64 variables")

    def _get_real(self, references: list[int]) -> list[float]:
        vr = (ctypes.c_uint32 * len(references))(*references)
        data = (ctypes.c_double * len(references))()
        status = self._functions["fmi3GetFloat64"](
            self.instance, vr, len(references), data, len(references)
        )
        self._status(status, "getting Float64 variables")
        return list(data)

    def initialize(self, parameters: dict[str, float], r: float, y: float,
                   start: float, stop: float) -> None:
        self.start_time = start
        status = self._functions["fmi3EnterInitializationMode"](
            self.instance, 0, 0.0, start, 1, stop
        )
        self._status(status, "entering initialization mode")
        self._set_real(
            [self.VR["Kp"], self.VR["Ki"], self.VR["uBias"]],
            [parameters["Kp"], parameters["Ki"], parameters["uBias"]],
        )
        self.set_inputs(r, y)
        status = self._functions["fmi3ExitInitializationMode"](self.instance)
        self._status(status, "exiting initialization mode")

    def set_inputs(self, r: float, y: float) -> None:
        self._set_real([self.VR["r"], self.VR["y"]], [r, y])

    def activate(self, time: float) -> float:
        status = self._functions["fmi3ActivateModelPartition"](
            self.instance, self.clock_reference, time
        )
        self._status(status, f"activating the control partition at t={time:g} s")
        return self._get_real([self.VR["u"]])[0]

    def close(self) -> None:
        if self.closed:
            return
        library = self._library
        try:
            status = self._functions["fmi3Terminate"](self.instance)
            if status not in (0, 1):
                pass
        finally:
            self._functions["fmi3FreeInstance"](self.instance)
            self.instance = None
            self._functions.clear()
            self._library = None
            self.closed = True
            if os.name == "nt" and library is not None:
                free_library = ctypes.WinDLL("kernel32", use_last_error=True).FreeLibrary
                free_library.argtypes = [ctypes.c_void_p]
                free_library.restype = ctypes.c_int
                if not free_library(ctypes.c_void_p(library._handle)):
                    raise ctypes.WinError(ctypes.get_last_error())


def _load_components(stack: ExitStack, assembly: Assembly, start: float, stop: float):
    models = {}
    for name in ("Controller", "Tank"):
        path, parameters, implementation = assembly.components[name]
        if implementation != "CoSimulation":
            raise ValueError(f"CS reference component {name} is {implementation}, not CoSimulation.")
        fmu, variables = stack.enter_context(
            open_fmi2_cs(path, name, parameters, start, stop)
        )
        models[name] = (fmu, variables)
    return models


def run_cs_reference(assembly: Assembly, h_ref_m: float, step: float):
    ports = {"hRef": h_ref_m}
    time_values: list[float] = []
    levels: list[float] = []
    controls: list[float] = []

    with ExitStack() as stack:
        models = _load_components(stack, assembly, assembly.start, assembly.stop)

        def read(port):
            element, signal = port
            if element is None:
                return ports[signal]
            fmu, variables = models[element]
            return fmu.getReal([variables[signal]])[0]

        def write(port, value):
            element, signal = port
            if element is None:
                ports[signal] = value
            else:
                fmu, variables = models[element]
                fmu.setReal([variables[signal]], [value])

        for source in (None, "Tank", "Controller"):
            for link in assembly.links:
                if link.source[0] == source:
                    write(link.target, link.convert(read(link.source)))
        for fmu, _ in models.values():
            fmu.exitInitializationMode()

        now = assembly.start
        while True:
            values = {
                link.target: link.convert(read(link.source))
                for link in assembly.links
            }
            time_values.append(now)
            levels.append(values[(None, "h")])
            controls.append(read(("Controller", "u")))
            if now >= assembly.stop - 1e-12:
                break
            for port, value in values.items():
                write(port, value)
            dt = min(step, assembly.stop - now)
            for name in ("Controller", "Tank"):
                fmu, _ = models[name]
                fmu.doStep(currentCommunicationPoint=now, communicationStepSize=dt)
            now += dt
    return {"time": time_values, "h": levels, "u": controls}


def _find_link(assembly: Assembly, source, target) -> Link:
    for link in assembly.links:
        if link.source == source and link.target == target:
            return link
    raise ValueError(f"Missing SSP connection {source} -> {target}.")


def run_event_triggered(assembly: Assembly, h_ref_m: float, delta_m: float,
                        monitor_step: float):
    tank_path, tank_parameters, tank_impl = assembly.components["Tank"]
    controller_path, controller_parameters, controller_impl = assembly.components["Controller"]
    if tank_impl != "CoSimulation":
        raise ValueError("The supplied Tank component must remain CoSimulation.")
    if controller_impl != "ScheduledExecution":
        raise ValueError("TankControllerSE.ssp must declare ScheduledExecution for Controller.")

    level_link = _find_link(assembly, ("Tank", "h"), ("Controller", "y"))
    flow_link = _find_link(assembly, ("Controller", "u"), ("Tank", "qIn"))
    ref_link = _find_link(assembly, (None, "hRef"), ("Controller", "r"))
    if abs(flow_link.factor - 0.001) > 1e-12 or abs(flow_link.offset) > 1e-12:
        raise ValueError("Expected the SSP's qIn = 0.001 * u flow scaling.")

    controller_unpack = tempfile.TemporaryDirectory(prefix="pi_se_")
    controller = None
    try:
        with open_fmi2_cs(tank_path, "Tank", tank_parameters,
                          assembly.start, assembly.stop) as (tank, tank_vr):
            tank.exitInitializationMode()
            initial_h = tank.getReal([tank_vr["h"]])[0]
            controller = ScheduledPI(controller_path, Path(controller_unpack.name))
            normalized_ref = ref_link.convert(h_ref_m) / H_SCALE_M
            normalized_initial_h = level_link.convert(initial_h) / H_SCALE_M
            controller.initialize(
                controller_parameters,
                normalized_ref,
                normalized_initial_h,
                assembly.start,
                assembly.stop,
            )

            u = controller.activate(assembly.start)
            tank.setReal([tank_vr["qIn"]], [flow_link.convert(u)])
            last_sent_y = normalized_initial_h
            events = [{
                "time": assembly.start,
                "source": "initial activation",
                "h_m": initial_h,
                "error": normalized_ref - normalized_initial_h,
                "u": u,
                "delta_change_m": 0.0,
            }]
            times = [assembly.start]
            levels = [initial_h]
            controls = [u]

            now = assembly.start
            while now < assembly.stop - 1e-12:
                dt = min(monitor_step, assembly.stop - now)
                tank.doStep(currentCommunicationPoint=now, communicationStepSize=dt)
                now += dt
                h = tank.getReal([tank_vr["h"]])[0]
                y = level_link.convert(h) / H_SCALE_M
                change = abs(y - last_sent_y)
                if change >= delta_m / H_SCALE_M:
                    controller.set_inputs(normalized_ref, y)
                    u = controller.activate(now)
                    tank.setReal([tank_vr["qIn"]], [flow_link.convert(u)])
                    events.append({
                        "time": now,
                        "source": "send-on-delta event",
                        "h_m": h,
                        "error": normalized_ref - y,
                        "u": u,
                        "delta_change_m": change * H_SCALE_M,
                    })
                    last_sent_y = y
                times.append(now)
                levels.append(h)
                controls.append(u)

        return {
            "time": times,
            "h": levels,
            "u": controls,
            "events": events,
            "delta_m": delta_m,
        }
    finally:
        if controller is not None:
            controller.close()
        controller_unpack.cleanup()


def settling_time(time_values, levels, setpoint: float, band: float):
    if not levels or abs(levels[-1] - setpoint) > band:
        return None
    last_outside = -1
    for index, level in enumerate(levels):
        if abs(level - setpoint) > band:
            last_outside = index
    return time_values[last_outside] if last_outside >= 0 else time_values[0]


def write_csvs(output_directory: Path, cs, event, setpoint: float, band: float):
    output_directory.mkdir(parents=True, exist_ok=True)
    with (output_directory / "cs_reference.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time_s", "level_m", "level_cm", "u"))
        writer.writerows((t, h, 100 * h, u) for t, h, u in zip(cs["time"], cs["h"], cs["u"]))
    with (output_directory / "event_triggered_trace.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time_s", "level_m", "level_cm", "u"))
        writer.writerows((t, h, 100 * h, u)
                         for t, h, u in zip(event["time"], event["h"], event["u"]))
    with (output_directory / "event_updates.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow((
            "time_s", "source", "level_cm", "error_normalized", "u",
            "measurement_change_cm", "delta_cm",
        ))
        writer.writerows((
            row["time"], row["source"], 100 * row["h_m"], row["error"],
            row["u"], 100 * row["delta_change_m"], 100 * event["delta_m"],
        ) for row in event["events"])


def plot_comparison(cs, event, h_ref_m: float, output: Path, show: bool):
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_level, ax_control) = plt.subplots(
        2, 1, sharex=True, figsize=(11.5, 7.2), layout="constrained"
    )
    cs_color = "#187A89"
    event_color = "#D65A36"
    event_times = [row["time"] for row in event["events"]]

    ax_level.plot(cs["time"], [100 * h for h in cs["h"]], color=cs_color,
                  linewidth=2.0, label="Current Co-Simulation loop (1 s steps)")
    ax_level.plot(event["time"], [100 * h for h in event["h"]], color=event_color,
                  linewidth=2.4, label="Scheduled Execution, send-on-delta")
    ax_level.axhline(100 * h_ref_m, color="#475569", linestyle="--", linewidth=1.4,
                     label=f"Setpoint ({100*h_ref_m:g} cm)")
    ax_level.scatter(
        [row["time"] for row in event["events"]],
        [100 * row["h_m"] for row in event["events"]],
        color=event_color, edgecolor="white", linewidth=0.7, s=65, zorder=5,
        label="SE activation / measurement sent",
    )
    ax_level.set_ylabel("Tank level (cm)")
    ax_level.set_title("Tank level: same plant and PI settings, event-triggered controller updates")
    ax_level.grid(alpha=0.25)
    ax_level.legend(loc="best")

    ax_control.step(cs["time"], cs["u"], where="post", color=cs_color,
                    linewidth=1.8, label="Co-Simulation control output")
    ax_control.step(event["time"], event["u"], where="post", color=event_color,
                    linewidth=2.2, label="SE output u held between events")
    ax_control.scatter(
        event_times, [row["u"] for row in event["events"]],
        color=event_color, edgecolor="white", linewidth=0.7, s=65, zorder=5,
        label="Control calculation / send",
    )
    for ax in (ax_level, ax_control):
        for event_time in event_times[1:]:
            ax.axvline(event_time, color=event_color, linestyle=":", linewidth=1.2, alpha=0.7)
    ax_control.set_xlabel("Time (s)")
    ax_control.set_ylabel("Control output u\n(numerically L/s here)")
    ax_control.set_xlim(cs["time"][0], cs["time"][-1])
    ax_control.grid(alpha=0.25)
    ax_control.legend(loc="best")

    later_times = ", ".join(f"{row['time']:.2f} s" for row in event["events"][1:])
    if later_times:
        ax_level.text(
            0.99, 0.04,
            f"Send-on-delta updates after start: {later_times}\n"
            f"δ = {100*event['delta_m']:.2f} cm; initial activation at 0 s",
            transform=ax_level.transAxes, ha="right", va="bottom", fontsize=9,
            bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "alpha": 0.9,
                  "edgecolor": "#cbd5e1"},
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    if show:
        plt.show()
    else:
        plt.close(fig)


def validate_matching_parameters(cs: Assembly, event: Assembly) -> None:
    for component in ("Tank", "Controller"):
        if cs.components[component][1] != event.components[component][1]:
            raise ValueError(f"{component} parameters differ between the CS and SE SSP files.")
    if abs(cs.start - event.start) > 1e-12 or abs(cs.stop - event.stop) > 1e-12:
        raise ValueError("The CS and SE SSP simulation intervals differ.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssp-cs", type=Path, default=HERE / "TankController.ssp")
    parser.add_argument("--ssp-se", type=Path, default=HERE / "TankControllerSE.ssp")
    parser.add_argument("--href-cm", type=float, default=43.0)
    parser.add_argument("--delta-cm", type=float, default=0.70,
                        help="send-on-delta threshold for the level measurement (default: 0.70 cm)")
    parser.add_argument("--cs-step", type=float, default=1.0,
                        help="Co-Simulation reference communication step (default: 1 s)")
    parser.add_argument("--monitor-step", type=float, default=0.05,
                        help="tank observation interval used by the event monitor (default: 0.05 s)")
    parser.add_argument("--settle-band-cm", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, default=HERE / "results")
    parser.add_argument("--no-show", action="store_true")
    args = parser.parse_args()

    values = [args.href_cm, args.delta_cm, args.cs_step, args.monitor_step, args.settle_band_cm]
    if not all(math.isfinite(value) for value in values):
        parser.error("All numeric values must be finite.")
    if args.delta_cm <= 0 or args.cs_step <= 0 or args.monitor_step <= 0 or args.settle_band_cm <= 0:
        parser.error("Delta, steps, and settling band must be positive.")

    try:
        with tempfile.TemporaryDirectory(prefix="tank_event_ssp_") as directory:
            workspace = Path(directory)
            cs_assembly = read_assembly(args.ssp_cs.resolve(), workspace / "cs")
            event_assembly = read_assembly(args.ssp_se.resolve(), workspace / "se")
            validate_matching_parameters(cs_assembly, event_assembly)
            h_ref_m = args.href_cm / 100.0
            delta_m = args.delta_cm / 100.0
            cs = run_cs_reference(cs_assembly, h_ref_m, args.cs_step)
            event = run_event_triggered(event_assembly, h_ref_m, delta_m, args.monitor_step)

        write_csvs(args.output_dir, cs, event, h_ref_m, args.settle_band_cm / 100)
        plot_comparison(
            cs, event, h_ref_m,
            args.output_dir / "event_control_comparison.png",
            show=not args.no_show,
        )

        tank_parameters = cs_assembly.components["Tank"][1]
        controller_parameters = cs_assembly.components["Controller"][1]
        cs_settle = settling_time(cs["time"], cs["h"], h_ref_m, args.settle_band_cm / 100)
        se_settle = settling_time(event["time"], event["h"], h_ref_m, args.settle_band_cm / 100)
        print("Tank level control: Co-Simulation reference vs event-triggered Scheduled Execution")
        print(
            f"Reference={args.href_cm:g} cm | delta={args.delta_cm:.2f} cm | "
            f"tank step={args.monitor_step:g} s | stop={cs_assembly.stop:g} s"
        )
        print(
            f"Tank: A={tank_parameters['A']:g} m^2, k={tank_parameters['k']:g} m^2/s, "
            f"h0={100*tank_parameters['h0']:g} cm"
        )
        print(
            f"PI: Kp={controller_parameters['Kp']:g}, Ki={controller_parameters['Ki']:g} 1/s, "
            f"uBias={controller_parameters['uBias']:g}"
        )
        print(
            f"CS: {max(0, len(cs['time'])-1)} doStep calls; "
            f"settling to ±{args.settle_band_cm:g} cm: "
            f"{cs_settle:.2f} s" if cs_settle is not None else
            f"CS: {max(0, len(cs['time'])-1)} doStep calls; did not settle within the selected band."
        )
        print(
            f"SE: {len(event['events'])} partition activations including startup; "
            f"send-on-delta times: " +
            (", ".join(f"{row['time']:.2f} s" for row in event["events"][1:]) or "none after startup")
        )
        print(
            f"SE final level={100*event['h'][-1]:.3f} cm; "
            f"final error={100*(h_ref_m-event['h'][-1]):.3f} cm; "
            f"settling to ±{args.settle_band_cm:g} cm: "
            f"{se_settle:.2f} s" if se_settle is not None else
            f"SE final level={100*event['h'][-1]:.3f} cm; did not settle within the selected band."
        )
        print(f"Files saved under: {args.output_dir.resolve()}")
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
