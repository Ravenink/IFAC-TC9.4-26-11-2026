import argparse
import csv
import json
import math
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


def read_ssp_run(folder: Path, h: int):
    index = 1 if h == 1 else 2
    path = folder / "ssp" / f"closed_loop_{index}_H{h}s.csv"
    rows = read_csv(path)
    return [{"time": float(row["time_s"]), "level": float(row["h_m"])} for row in rows]


def level_chart(dcp, ssp_h1, ssp_h10, reference_m, duration_s):
    curves = [
        ("DCP", dcp, "#0f766e", None, 4),
        ("SSP H=1 s", ssp_h1, "#ea580c", "8 5", 2.8),
        ("SSP H=10 s", ssp_h10, "#7c3aed", "4 5", 2.8),
    ]
    values = [row["level"] * 100 for _, rows, _, _, _ in curves for row in rows]
    values.append(reference_m * 100)
    low, high = min(values), max(values)
    pad = max(0.6, (high - low) * 0.14)
    low, high = low - pad, high + pad
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM
    x = lambda value: LEFT + value / duration_s * plot_width
    y = lambda value: TOP + (high - value) / (high - low) * plot_height
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Tank level with delayed DCP signals compared to local SSP H one and H ten">',
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
    for _, rows, color, dash_array, stroke_width in curves:
        points = " ".join(f'{x(row["time"]):.1f},{y(row["level"] * 100):.1f}' for row in rows)
        dash = f' stroke-dasharray="{dash_array}"' if dash_array else ""
        elements.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="{stroke_width}"{dash}/>')
        if len(rows) <= 25:
            elements.extend(
                f'<circle cx="{x(row["time"]):.1f}" cy="{y(row["level"] * 100):.1f}" r="3" fill="{color}"/>'
                for row in rows
            )
    elements.append("</svg>")
    return "\n".join(elements)


def main():
    parser = argparse.ArgumentParser(description="Build the interactive DCP delay / SSP comparison panel.")
    parser.add_argument("--folder", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    folder = args.folder.resolve()
    run_info = json.loads((folder / "run_info.json").read_text(encoding="utf-8"))
    duration = float(run_info["duration_s"])
    reference = float(run_info["reference_m"])
    delays = [int(run["delaySeconds"]) for run in run_info["dcpRuns"]]
    if not delays:
        raise SystemExit("No DCP delay runs were recorded.")
    if len(delays) != len(set(delays)):
        raise SystemExit("DCP delay values must be unique.")

    ssp_h1 = read_ssp_run(folder, 1)
    ssp_h10 = read_ssp_run(folder, 10)
    expected_dcp = int(duration) + 1
    expected_h1 = int(math.ceil(duration / 1)) + 1
    expected_h10 = int(math.ceil(duration / 10)) + 1
    if len(ssp_h1) != expected_h1 or len(ssp_h10) != expected_h10:
        raise SystemExit("SSP H=1 s and H=10 s data have an unexpected number of samples.")

    options = []
    panes = []
    for index, delay in enumerate(delays):
        dcp = read_dcp_run(folder, delay)
        if len(dcp) != expected_dcp:
            raise SystemExit(f"DCP delay={delay} s data must contain {expected_dcp} samples.")
        active = " selected" if index == 0 else ""
        options.append(f'<option value="{delay}"{active}>{delay} s</option>')
        chart = level_chart(dcp, ssp_h1, ssp_h10, reference, duration)
        hidden = " hidden" if index else ""
        panes.append(f'''<section class="trace" data-trace="{index}"{hidden}>
  <div class="chart">{chart}</div>
</section>''')

    page = '''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tank control | DCP delay and SSP</title>
<style>
:root{color-scheme:light;--ink:#13243a;--muted:#5e6c80;--line:#d8e0e9;--teal:#0f766e;--purple:#7c3aed;--page:#f3f6fa}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:16px/1.35 "Segoe UI",Arial,sans-serif}
main{max-width:1140px;margin:24px auto;padding:0 18px 32px}h1{font-size:clamp(36px,6vw,62px);line-height:1;margin:0 0 8px;letter-spacing:-1.8px}h2{font-size:20px;margin:0 0 14px}p{margin:0;color:var(--muted)}
.subtitle{font-size:19px;margin-bottom:18px}.cards{display:grid;grid-template-columns:1.25fr 1fr;gap:14px}.card,.panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 5px 20px #17203308}
.card-title{display:flex;justify-content:space-between;align-items:baseline;gap:10px;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:12px}.card-title strong{font-size:25px}.card-title span{font-size:14px;color:var(--muted)}
.master{max-width:360px;margin:0 auto 8px;text-align:center;background:#edf4fa;border:1px solid #bfd0e0}.box{border:1px solid var(--line);border-radius:11px;padding:12px 10px;background:#fbfdff;text-align:center;font-size:18px;font-weight:800}.box small{display:block;font-size:13px;font-weight:450;color:var(--muted);margin-top:3px}.control{text-align:center;color:var(--muted);font-size:13px;margin-bottom:7px}
.dcp-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(130px,.9fr) minmax(0,1fr);gap:9px;align-items:center}.controller{border-top:4px solid var(--teal)}.plant{border-top:4px solid #2563eb}.channel{border:1px solid #b8c8d6;border-radius:12px;background:#f7fafc;padding:10px 7px;text-align:center}.channel strong{font-size:15px;color:#21445a}.channel small{display:block;margin-top:3px;color:var(--muted);font-size:12px}
.ssp-master{text-align:center;background:#f5f0ff;border-color:#d5c6fa;margin-bottom:8px}.ssp-row{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center}.fmi{text-align:center;color:var(--purple);font-size:12px;font-weight:750;max-width:120px}
.panel{margin-top:14px}.controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px}.controls label{font-size:16px;font-weight:800}.controls select{min-width:130px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink);padding:10px 14px;font:inherit;font-size:19px;font-weight:800}.direction{font-size:20px;font-weight:800;color:var(--teal)}.meta{font-size:13px;margin:-2px 0 12px}.chart{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}.chart svg{display:block;width:100%;height:auto}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:10px}.stat{border-radius:11px;background:#f3f6fa;padding:11px 13px}.stat span{display:block;color:var(--muted);font-size:12px;font-weight:800;letter-spacing:.04em}.stat strong{display:block;font-size:22px;margin-top:2px}.stat small{color:var(--muted);font-size:13px}.legend{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:10px 0 12px;font-size:14px;font-weight:650;color:#394b61}.key{width:28px;height:4px;border-radius:3px;display:inline-block;vertical-align:middle;margin-right:6px}.key.dcp{background:var(--teal)}.key.h1{background:repeating-linear-gradient(90deg,#ea580c 0 7px,transparent 7px 11px)}.key.h10{background:repeating-linear-gradient(90deg,#7c3aed 0 4px,transparent 4px 8px)}.note{font-size:13px;margin-top:10px}
@media(max-width:790px){main{margin:16px auto;padding:0 12px 22px}.cards{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.stat:last-child{grid-column:1/-1}}
@media(max-width:520px){.card,.panel{padding:13px}.dcp-row{grid-template-columns:1fr}.ssp-row{grid-template-columns:1fr}.fmi{max-width:none}.stats{grid-template-columns:1fr}.stat:last-child{grid-column:auto}.controls{align-items:flex-start;flex-direction:column;gap:7px}.direction{font-size:17px}}
</style></head><body><main>
<h1>DCP delay</h1>
<p class="subtitle">Same tank. Same controller. Delayed signals.</p>
<div class="cards">
  <section class="card"><div class="card-title"><strong>DCP</strong><span>3 processes · UDP</span></div>
    <div class="box master">DCP MASTER<small>configure · synchronize · step</small></div>
    <div class="control">control and step commands</div>
    <div class="dcp-row">
      <div class="box controller">CONTROLLER<small>separate process</small></div>
      <div class="channel"><strong>u &rarr; · h &larr;</strong><small>data exchange<br>delay D each way</small></div>
      <div class="box plant">TANK<small>separate process</small></div>
    </div>
  </section>
  <section class="card"><div class="card-title"><strong>SSP</strong><span>one local process</span></div>
    <div class="box ssp-master">SSP MASTER<small>H = 1 s and H = 10 s</small></div>
    <div class="ssp-row"><div class="box controller">CONTROLLER FMU</div><div class="fmi">local FMI calls<br>getReal / setReal</div><div class="box plant">TANK FMU</div></div>
  </section>
</div>
<section class="panel">
  <h2>Compare tank level</h2>
  <div class="controls"><label for="delay">DCP delay · each direction</label><select id="delay">__OPTIONS__</select><span class="direction" id="direction"></span></div>
  <p class="meta">DCP step: 1 s · SSP reference: H=1 s and H=10 s · Setpoint: __REFERENCE__ cm</p>
  <div class="legend"><span><i class="key dcp"></i>DCP</span><span><i class="key h1"></i>SSP H=1 s</span><span><i class="key h10"></i>SSP H=10 s</span><span>-- setpoint</span></div>
  __PANES__
  <p class="note">D is simulated signal delay, in seconds, applied in both directions. It changes the data used by each controller step; it is not wall-clock UDP latency.</p>
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
        .replace("__REFERENCE__", f"{reference * 100:g}"))
    output = folder / "dcp_demo_results.html"
    output.write_text(target, encoding="utf-8")
    print(f"Interactive comparison panel: {output}")
    webbrowser.open(output.as_uri())


if __name__ == "__main__":
    main()
