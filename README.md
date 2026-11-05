# Bottling-Plant-Simulator-Connect-with-Water-Tank-Control

My second PLC project. A water bottling line fills, caps and checks bottles, then sorts them into a good crate or a reject bin.
It takes its water from the tank in my first project, so one PLC program controls both.
A Python simulator acts as the real plant and shows it in your web browser.


```
OpenPLC Runtime (Docker)
   |-- Modbus TCP port 5020 --> Water tank     \
   |-- Modbus TCP port 5021 --> Bottling line  /  Plant Simulator (Python)
                                                  view it at http://localhost:8090
```

## How it works

1. Press **LINE START**. The green lamp turns on and the feeder releases one bottle.
2. **Filler:** fills fast, then slows down 50 ml before the target (default 700 ml).
   It only fills when the tank is above 20%.
3. **Capper:** presses a cap on for 1 second.
4. **QC:** a camera checks for damage and a missing cap, and a scale measures the water.
   A good bottle is within ±10 ml of the target, has no damage and has a cap.
5. Good bottles go into the **crate** (12 bottles). Bad bottles are pushed into the **reject bin** (10 bottles).
6. Then the next bottle starts (one bottle at a time).

The water tank works the same as in my first project: the pump keeps it between 20% and 85%.

### Stack light

| Lamp  | Meaning |
|-------|---------|
| Green | Line is running |
| Amber | Waiting: crate full or missing, reject bin full, or tank water below 20% |
| Red   | E-stop pressed, or the conveyor jammed (ran more than 8 s without stopping) |

If filling takes more than 20 s, the bottle moves on and QC rejects it.


## Files

The files are all in one place here. After downloading, put them in these folders.

**PLC project** (open the `BottlingPlant` folder in OpenPLC Editor):

```
BottlingPlant/
├── project.json
├── pous/programs/main.ld        (37 rungs of ladder logic)
└── devices/
    ├── configuration.json
    ├── pin-mapping.json
    └── remote/
        ├── TankIO.json
        └── BottleIO.json
```

**Plant Simulator** (keep these 4 files together in a `PlantSimulator` folder):

- `plant_io.py` – the Modbus TCP connection to the PLC
- `plant_server.py` – moves the water and the bottles, and runs the web page
- `plant_view.html` – the plant picture and operator buttons
- `Start Plant.bat` – double-click to start

<details>
<summary><b>Full I/O list</b></summary>

**Water tank (port 5020)**

| PLC address | Tag | What it is |
|---|---|---|
| %IX0.0 | TANK_START | Tank start button |
| %IX0.1 | TANK_STOP | Tank stop button |
| %IX0.2 | LOW_LEVEL | TRUE when water is above 20% |
| %IX0.3 | HIGH_LEVEL | TRUE when water reaches 85% |
| %QX0.0 | PUMP | Tank pump |
| %QX0.1 | FULL_LAMP | Tank full lamp |

**Bottling line (port 5021)**

| PLC address | Tag | What it is |
|---|---|---|
| %IX0.4 | LINE_START | Line start button |
| %IX0.5 | LINE_STOP | Line stop button |
| %IX0.6 | ESTOP_OK | TRUE when the E-stop is NOT pressed |
| %IX0.7 | ALARM_RESET | Alarm reset button |
| %IX1.0 | BOTTLE_AT_FILLER | Photo-eye at the filler |
| %IX1.1 | BOTTLE_AT_CAPPER | Photo-eye at the capper |
| %IX1.2 | BOTTLE_AT_QC | Photo-eye at QC |
| %IX1.3 | QC_DONE | QC scan finished |
| %IX1.4 | QC_DAMAGE | Camera found a crack or dent |
| %IX1.5 | QC_NO_CAP | Camera found no cap |
| %IX1.6 | BOTTLE_IN_CRATE | Bottle entered the crate |
| %IX1.7 | BOTTLE_IN_REJECT | Bottle entered the reject bin |
| %IX2.0 | CRATE_PRESENT | Crate is in place |
| %IW0 | FILLER_ML | ml in the bottle at the filler |
| %IW1 | QC_ML | ml measured at QC |
| %IW2 | TARGET_ML | Fill target from the operator |
| %QX0.2 | BOTTLE_RELEASE | Feeder gate: release one bottle |
| %QX0.3 | CONVEYOR | Conveyor motor |
| %QX0.4 | FILL_FAST | Fast fill valve |
| %QX0.5 | FILL_SLOW | Slow fill valve |
| %QX0.6 | CAPPER | Cap press |
| %QX0.7 | QC_SCAN | Start the QC scan |
| %QX1.0 | DIVERTER | Push the bottle into the reject bin |
| %QX1.1 | GREEN_LAMP | Running |
| %QX1.2 | AMBER_LAMP | Waiting |
| %QX1.3 | RED_LAMP | Fault |


</details>

## What you need

- OpenPLC Editor 4.3.3 or newer
- Docker Desktop (runs OpenPLC Runtime v4)
- Python 3 and a web browser

## How to run

1. Open Docker Desktop, then start the OpenPLC Runtime:
   ```
   docker run -d --name openplc-runtime -p 8443:8443 --cap-add=SYS_NICE --cap-add=SYS_RESOURCE -v openplc-runtime-data:/var/run/runtime ghcr.io/autonomy-logic/openplc-runtime:latest
   ```
   Next time, just run `docker start openplc-runtime`.
2. Close the Water Tank simulator if it is open (it uses the same port 5020).
3. Double-click `PlantSimulator/Start Plant.bat`. The plant opens in your browser at http://localhost:8090.
4. Open the `BottlingPlant` folder in OpenPLC Editor. Click **Build and upload**, then **▶ Start PLC**.
5. On the web page, hold **START** in the **Water tank** panel, then hold **START** in the **Bottling line** panel.
   The green lamp turns on and the first bottle starts moving.


**Try it:**
- Change the fill target with the **−** and **+** buttons.
- Click **Next bottle cracked** and watch QC reject it.
- When the crate is full, click **Swap crate**. When the reject bin is full, click **Empty reject bin**, then **RESET**.
- Turn on **Jam the conveyor**. After 8 s the red lamp lights up.
  To recover: turn the jam off, click **Clear belt**, press **RESET**, then **START**.

> **Note:** Keep the project in a folder path with English letters only (for example `C:\PLC Project`).
> OpenPLC can fail to build from other paths.
   







