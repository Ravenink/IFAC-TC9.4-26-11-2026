import argparse
import csv
import json
import webbrowser
from pathlib import Path


WIDTH, HEIGHT = 980, 420
LEFT, RIGHT, TOP, BOTTOM = 78, 28, 48, 58


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def read_dcp_run(folder: Path, delay_s: int):
    run_folder = folder / f"dcp_delay_{delay_s}s"
    controller = read_csv(run_folder / "controller.csv")
    tank = read_csv(run_folder / "tank.csv")
    if len(controller) != len(tank) or not tank:
        raise SystemExit(f"DCP delay={delay_s} s CSV files have different or empty sample sets.")
    for controller_row, tank_row in zip(controller, tank):
        if abs(float(controller_row["time_s"]) - float(tank_row["time_s"])) > 1e-9:
            raise SystemExit(f"DCP delay={delay_s} s samples are not time-aligned.")
    return [{"time": float(row["time_s"]), "level": float(row["level_m"])} for row in tank]


def level_chart(dcp, reference_m, duration_s, level_range_cm):
    low, high = level_range_cm
    pad = max(0.6, (high - low) * 0.14)
    low, high = low - pad, high + pad
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM
    x = lambda value: LEFT + value / duration_s * plot_width
    y = lambda value: TOP + (high - value) / (high - low) * plot_height
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Tank level and setpoint under PI control over DCP">',
        '<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#34445b}.axis{stroke:#748198;stroke-width:1.2}.grid{stroke:#e5eaf1;stroke-width:1}.tick{font-size:11px;fill:#637187}</style>',
    ]
    for idx in range(5):
        value = low + (high - low) * idx / 4
        yy = y(value)
        elements.append(f'<line x1="{LEFT}" y1="{yy:.1f}" x2="{WIDTH-RIGHT}" y2="{yy:.1f}" class="grid"/>')
        elements.append(f'<text x="{LEFT-10}" y="{yy+4:.1f}" text-anchor="end" class="tick">{value:.1f}</text>')
    for idx in range(7):
        value = duration_s * idx / 6
        xx = x(value)
        elements.append(f'<line x1="{xx:.1f}" y1="{TOP}" x2="{xx:.1f}" y2="{HEIGHT-BOTTOM}" class="grid"/>')
        elements.append(f'<text x="{xx:.1f}" y="{HEIGHT-BOTTOM+21}" text-anchor="middle" class="tick">{value:g}</text>')
    elements.extend([
        f'<line x1="{LEFT}" y1="{TOP}" x2="{LEFT}" y2="{HEIGHT-BOTTOM}" class="axis"/>',
        f'<line x1="{LEFT}" y1="{HEIGHT-BOTTOM}" x2="{WIDTH-RIGHT}" y2="{HEIGHT-BOTTOM}" class="axis"/>',
        f'<text x="{(LEFT+WIDTH-RIGHT)/2:.1f}" y="{HEIGHT-15}" text-anchor="middle">Time (s)</text>',
        f'<text transform="translate(23 {HEIGHT/2:.1f}) rotate(-90)" text-anchor="middle">Tank level (cm)</text>',
        f'<line x1="{LEFT}" y1="{y(reference_m * 100):.1f}" x2="{WIDTH-RIGHT}" y2="{y(reference_m * 100):.1f}" stroke="#526277" stroke-width="1.6" stroke-dasharray="5 6"/>',
    ])
    points = " ".join(f'{x(row["time"]):.1f},{y(row["level"] * 100):.1f}' for row in dcp)
    elements.append(f'<polyline points="{points}" fill="none" stroke="#0f766e" stroke-width="4"/>')
    if len(dcp) <= 25:
        elements.extend(
            f'<circle cx="{x(row["time"]):.1f}" cy="{y(row["level"] * 100):.1f}" r="3" fill="#0f766e"/>'
            for row in dcp
        )
    elements.append("</svg>")
    return "\n".join(elements)


def main():
    parser = argparse.ArgumentParser(description="Build the interactive DCP results page.")
    parser.add_argument("--folder", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    folder = args.folder.resolve()
    run_info = json.loads((folder / "run_info.json").read_text(encoding="utf-8"))
    duration = float(run_info["duration_s"])
    reference = float(run_info["reference_m"])
    step = float(run_info["dcpCommunicationStep_s"])
    delays = [int(run["delaySeconds"]) for run in run_info["dcpRuns"]]
    if not delays:
        raise SystemExit("No DCP delay runs were recorded.")
    if len(delays) != len(set(delays)):
        raise SystemExit("DCP delay values must be unique.")

    expected_dcp = int(duration) + 1
    dcp_runs = {delay: read_dcp_run(folder, delay) for delay in delays}
    for delay, dcp in dcp_runs.items():
        if len(dcp) != expected_dcp:
            raise SystemExit(f"DCP delay={delay} s data must contain {expected_dcp} samples.")
    options = []
    panes = []
    for index, (delay, dcp) in enumerate(dcp_runs.items()):
        levels = [row["level"] * 100 for row in dcp]
        levels.append(reference * 100)
        level_range = min(levels), max(levels)
        active = " selected" if index == 0 else ""
        options.append(f'<option value="{delay}"{active}>{delay} s</option>')
        chart = level_chart(dcp, reference, duration, level_range)
        hidden = " hidden" if index else ""
        panes.append(f'''<section class="trace" data-trace="{index}"{hidden}>
  <div class="chart">{chart}</div>
</section>''')

    page = '''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tank control | DCP</title>
<style>
:root{color-scheme:light;--ink:#13243a;--muted:#5e6c80;--line:#d8e0e9;--teal:#0f766e;--page:#f3f6fa}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:16px/1.35 "Segoe UI",Arial,sans-serif}
main{max-width:1140px;margin:24px auto;padding:0 18px 32px}h1{font-size:clamp(36px,6vw,62px);line-height:1;margin:0 0 18px;letter-spacing:-1.8px}h2{font-size:20px;margin:0 0 14px}p{margin:0;color:var(--muted)}
.card,.panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 5px 20px #17203308}
.card-title{display:flex;justify-content:space-between;align-items:baseline;gap:10px;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:12px}.card-title strong{font-size:25px}.card-title span{font-size:14px;color:var(--muted)}
.master{max-width:360px;margin:0 auto 8px;text-align:center;background:#edf4fa;border:1px solid #bfd0e0}.box{border:1px solid var(--line);border-radius:11px;padding:12px 10px;background:#fbfdff;text-align:center;font-size:18px;font-weight:800}.box small{display:block;font-size:13px;font-weight:450;color:var(--muted);margin-top:3px}.control{text-align:center;color:var(--muted);font-size:13px;margin-bottom:7px}
.dcp-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(130px,.9fr) minmax(0,1fr);gap:9px;align-items:center}.controller{border-top:4px solid var(--teal)}.plant{border-top:4px solid #2563eb}.channel{border:1px solid #b8c8d6;border-radius:12px;background:#f7fafc;padding:10px 7px;text-align:center}.channel strong{font-size:15px;color:#21445a}.channel small{display:block;margin-top:3px;color:var(--muted);font-size:12px}
.panel{margin-top:14px}.controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px}.controls label{font-size:16px;font-weight:800}.controls select{min-width:130px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink);padding:10px 14px;font:inherit;font-size:19px;font-weight:800}.direction{font-size:20px;font-weight:800;color:var(--teal)}.meta{font-size:13px;margin:-2px 0 12px}.chart{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}.chart svg{display:block;width:100%;height:auto}.legend{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:10px 0 12px;font-size:14px;font-weight:650;color:#394b61}.key{width:28px;display:inline-block;vertical-align:middle;margin-right:6px}.key.dcp{border-top:4px solid var(--teal);border-radius:3px}.key.setpoint{border-top:2px dashed #526277}.note{font-size:13px;margin-top:10px}
@media(max-width:790px){main{margin:16px auto;padding:0 12px 22px}}
@media(max-width:520px){.card,.panel{padding:13px}.dcp-row{grid-template-columns:1fr}.controls{align-items:flex-start;flex-direction:column;gap:7px}.direction{font-size:17px}}
</style></head><body><main>
<h1>DCP tank control</h1>
<section class="card"><div class="card-title"><strong>Local DCP setup</strong><span>3 processes · UDP</span></div>
    <div class="box master">DCP MASTER<small>configure · synchronize · step</small></div>
    <div class="control">control and step commands</div>
    <div class="dcp-row">
      <div class="box controller">PI CONTROLLER<small>PIController.fmu</small></div>
      <div class="channel"><strong>u &rarr; · h &larr;</strong><small>data exchange<br>delay D each way</small></div>
      <div class="box plant">TANK<small>Tank.fmu</small></div>
    </div>
</section>
<section class="panel">
  <h2>Tank level by signal delay</h2>
  <div class="controls"><label for="delay">One-way signal delay</label><select id="delay">__OPTIONS__</select><span class="direction" id="direction"></span></div>
  <p class="meta">DCP communication step __STEP__ s · Setpoint __REFERENCE__ cm</p>
  <div class="legend"><span><i class="key dcp"></i>Tank level</span><span><i class="key setpoint"></i>Setpoint</span></div>
  __PANES__
  <p class="note">The vertical scale adjusts to the selected delay. Signal delay is simulated in both directions, on the level measurement and on the PI command.</p>
</section>
</main><script>
const delayInput = document.getElementById("delay");
const traces = [...document.querySelectorAll("[data-trace]")];
function selectDelay(index) {
  const option = delayInput.options[index];
  traces.forEach(trace => { trace.hidden = trace.dataset.trace !== String(index); });
  const delay = Number(option.value);
  document.getElementById("direction").textContent = delay ? `${delay} s / direction` : "0 s · no delay";
}
delayInput.addEventListener("change", () => selectDelay(delayInput.selectedIndex));
selectDelay(0);
</script></body></html>'''

    target = (page.replace("__OPTIONS__", "".join(options))
        .replace("__PANES__", "".join(panes))
        .replace("__STEP__", f"{step:g}")
        .replace("__REFERENCE__", f"{reference * 100:g}"))
    output = folder / "dcp_demo_results.html"
    output.write_text(target, encoding="utf-8")
    print(f"Interactive DCP results: {output}")
    webbrowser.open(output.as_uri())


if __name__ == "__main__":
    main()
