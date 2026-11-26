```powershell
cd "C:\Users\ldela\OneDrive - UNED\Desktop\Webinar IFAC\DCP"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\build.ps1
.\run_demo.ps1
```

La demo carga `../PIController.fmu` y `fmus/Tank.fmu`. El maestro DCP configura el PI con `Kp = 1,5`, `Ki = 0,05 s⁻¹` y `uBias = 0,2`, los valores por defecto del FMU.

El informe `outputs/dcp_demo_results.html` muestra el nivel del tanque para los retardos DCP seleccionados. Por defecto ejecuta 0, 2, 5 y 10 s de retardo por sentido, con un paso de comunicación DCP de 1 s. Para elegir otros retardos, usa `-DelaySeconds`.

`python .\package_dcp.py` vuelve a generar los paquetes `.dcp` y sus copias `.zip`.
