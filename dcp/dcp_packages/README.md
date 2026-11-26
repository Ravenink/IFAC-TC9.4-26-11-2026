# Local DCP tank demo packages

`DCP_Controller_Local.dcp` packages the PI controller and its DCP slave description. `DCP_Tank_Local.dcp` packages the tank and its DCP slave description. Use `run_demo.ps1` to run the master and the two FMU wrappers as separate processes over local UDP.

The runtime loads `../PIController.fmu` and `fmus/Tank.fmu`. The master configures `r`, `Kp`, `Ki`, and `uBias`. The tank wrapper converts the PI command `u` to inlet flow with `qIn = 0.001 × u` m³/s. The tank returns its level `h` to the controller as `y`.
