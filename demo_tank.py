"""Inspect and simulate a single tank with a fixed inlet flow."""

import argparse
import csv
from pathlib import Path

from fmpy import dump, simulate_fmu


TANK = Path(__file__).resolve().with_name("Tank.fmu")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, metavar="DIR", help="save a PNG figure and CSV samples")
    parser.add_argument("--no-show", action="store_true", help="run without opening a plot window")
    args = parser.parse_args()

    # Inspect the FMI interface, then run the FMU on its own.
    dump(str(TANK))
    result = simulate_fmu(
        str(TANK), fmi_type="CoSimulation",
        start_values={"qIn": 2.45e-4},
        stop_time=80, output_interval=1,
        output=["h"],
    )
    time, level = result["time"], result["h"]
    print(f"\nWater level: {100 * level[0]:.2f} cm initially, "
          f"{100 * level[-1]:.3f} cm at {time[-1]:g} s.")
    if args.no_show and args.output is None:
        return

    import matplotlib

    if args.no_show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4.8), layout="constrained")
    ax.plot(time, 100 * level, lw=2.5, color="#007E87", label="Tank level")
    ax.axhline(49, color="#546976", linestyle="--", label="Equilibrium (49 cm)")
    ax.set(title="Tank response to a fixed inflow", xlabel="Time (s)",
           ylabel="Water level (cm)", xlim=(time[0], time[-1]))
    ax.grid(alpha=0.24)
    ax.legend(loc="lower right")

    if args.output is not None:
        args.output.mkdir(parents=True, exist_ok=True)
        fig.savefig(args.output / "open_loop.png", dpi=180)
        with (args.output / "open_loop.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("time_s", "h_m"))
            writer.writerows(zip(time, level))

    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
