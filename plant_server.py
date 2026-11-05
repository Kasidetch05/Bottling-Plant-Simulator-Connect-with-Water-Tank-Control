"""Bottling plant simulator: the physics and the web page.

Your plant_io.py is the connection to the PLC (Modbus TCP on ports 5020 and 5021).
This program moves the water and the bottles, and shows the plant in your browser
at http://localhost:8090

    Station 1  water tank     20 L tank, pump, LOW (20 %) and HIGH (85 %) float switches
    Station 2  bottling line  feeder, filler (fast + slow valve), capper, QC camera and
                              check-weigher, diverter gate, good crate (12), reject bin (10)

The filler takes its water from the tank, so the two stations depend on each other.
The simulator only measures. Your PLC program decides what is good and what is bad.

Run:  python plant_server.py          (or double-click "Start Plant.bat")
"""

import argparse
import collections
import json
import os
import random
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import plant_io

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "plant_view.html")

# ---------------------------------------------------------------- Station 1: water tank
TANK_LITERS = 20.0
LOW_SWITCH = 20.0        # % where LOW_LEVEL turns TRUE
HIGH_SWITCH = 85.0       # % where HIGH_LEVEL turns TRUE
PUMP_RATE = 4.0          # % of the tank per second (0.8 L/s)
MAX_OTHER_USE = 2.5      # % per second with "other water use" at 100 %

# ---------------------------------------------------------------- Station 2: bottling line
BOTTLE_CAPACITY = 750    # ml
FAST_RATE = 100.0        # ml/s through FILL_FAST
SLOW_RATE = 20.0         # ml/s through FILL_SLOW (both open = 120 ml/s)
VALVE_CLOSE_DELAY = 0.05 # s a valve keeps running after its coil turns off
CLOG_FACTOR = 0.25       # a clogged nozzle fills at a quarter of the speed
LEAK_RATE = 3.0          # ml/s lost by a cracked bottle
QC_TOLERANCE = 10        # ml either side of the target that counts as a good fill
SCAN_TIME = 0.6          # s the QC camera needs for one scan
CAP_TRAVEL = 0.3         # s for the capper head to go down (or up)
GATE_TRAVEL = 0.15       # s for the diverter gate to swing
PULSE_TIME = 0.5         # s the crate / reject bin photo-eyes stay TRUE
CRATE_SIZE = 12
BIN_SIZE = 10
CRATE_SWAP_TIME = 2.0    # s with no crate while you swap it

# Line geometry in picture units. The web page draws with the same numbers.
GEOMETRY = {
    "belt_start": 300, "belt_end": 1030, "belt_y": 470,
    "spawn": 330, "filler": 460, "capper": 590, "qc": 720, "diverter": 850, "end": 1015,
    "bottle_w": 34, "eye_offset": 12, "capacity": BOTTLE_CAPACITY,
    "crate_size": CRATE_SIZE, "bin_size": BIN_SIZE,
    "low_switch": LOW_SWITCH, "high_switch": HIGH_SWITCH, "tank_liters": TANK_LITERS,
}
BELT_SPEED = 80.0        # picture units per second
WORK_WINDOW = 24         # a station works on a bottle whose centre is this close

# Every name the physics uses. They must match the lists in your plant_io.py.
NEEDED = {
    "TANK_INPUTS": ["TANK_START", "TANK_STOP", "LOW_LEVEL", "HIGH_LEVEL"],
    "TANK_COILS": ["PUMP", "FULL_LAMP"],
    "LINE_INPUTS": ["LINE_START", "LINE_STOP", "ESTOP_OK", "ALARM_RESET",
                    "BOTTLE_AT_FILLER", "BOTTLE_AT_CAPPER", "BOTTLE_AT_QC",
                    "QC_DONE", "QC_DAMAGE", "QC_NO_CAP",
                    "BOTTLE_IN_CRATE", "BOTTLE_IN_REJECT", "CRATE_PRESENT"],
    "LINE_REGISTERS": ["FILLER_ML", "QC_ML", "TARGET_ML"],
    "LINE_COILS": ["BOTTLE_RELEASE", "CONVEYOR", "FILL_FAST", "FILL_SLOW", "CAPPER",
                   "QC_SCAN", "DIVERTER", "GREEN_LAMP", "AMBER_LAMP", "RED_LAMP"],
}


def check_names(tank, line):
    """Stop with a clear message if plant_io.py is missing a name the physics needs."""
    have = {
        "TANK_INPUTS": tank.input_names, "TANK_COILS": tank.coil_names,
        "LINE_INPUTS": line.input_names, "LINE_REGISTERS": line.register_names,
        "LINE_COILS": line.coil_names,
    }
    problems = []
    for list_name, names in NEEDED.items():
        missing = [n for n in names if n not in have[list_name]]
        if missing:
            problems.append("%s in plant_io.py is missing: %s" % (list_name, ", ".join(missing)))
    if problems:
        print("\n".join(problems))
        print("Fix the spelling in plant_io.py, then start again.")
        sys.exit(1)


def approach(value, target, step):
    """Move value towards target by at most step."""
    if value < target:
        return min(target, value + step)
    return max(target, value - step)


class Bottle:
    def __init__(self, number, x, defect):
        self.number = number
        self.x = x
        self.ml = 0.0
        self.capped = False
        self.defect = defect                       # None, "crack" or "dent"
        self.scale_error = random.uniform(-1.0, 1.0)
        self.tag = None                            # what QC saw, shown above the bottle
        self.tag_bad = False

    def to_json(self):
        return {"n": self.number, "x": round(self.x, 1), "ml": round(self.ml),
                "capped": self.capped, "defect": self.defect,
                "tag": self.tag, "bad": self.tag_bad}


class Plant:
    def __init__(self, tank, line, defect_rate):
        self.tank = tank
        self.line = line
        self.lock = threading.Lock()
        self.t = 0.0

        # Station 1
        self.level = 50.0
        self.other_use = 0.0
        self.pump_fail = False
        self.buttons = {"TANK_START": False, "TANK_STOP": False,
                        "LINE_START": False, "LINE_STOP": False, "ALARM_RESET": False}

        # Station 2 operator panel
        self.estop_pressed = False
        self.target_ml = 700

        # Station 2 machines
        self.bottles = []
        self.next_number = 1
        self.prev_release = False
        self.belt_offset = 0.0
        self.fast_timer = 0.0
        self.slow_timer = 0.0
        self.flow = 0.0
        self.dry = False
        self.capper_pos = 0.0
        self.gate_pos = 0.0
        self.prev_scan = False
        self.scan_timer = 0.0
        self.qc_done = False
        self.qc_damage = False
        self.qc_no_cap = False
        self.last_scan = ""
        self.crate_present = True
        self.crate_away = 0.0
        self.crate_count = 0
        self.crates_done = 0
        self.bin_count = 0
        self.crate_pulse = 0.0
        self.reject_pulse = 0.0

        # Faults and defects you can switch on from the web page
        self.jam = False
        self.clog = False
        self.no_caps = False
        self.defect_rate = defect_rate
        self.next_defect = None

        # Score: the simulator knows the truth about every bottle
        self.score = {"good_shipped": 0, "bad_rejected": 0, "bad_shipped": 0,
                      "good_rejected": 0, "broken": 0}
        self.spilled = 0.0
        self.crate_times = collections.deque()
        self.events = collections.deque(maxlen=8)

        self.tank_coils = {n: False for n in NEEDED["TANK_COILS"]}
        self.line_coils = {n: False for n in NEEDED["LINE_COILS"]}

    # ------------------------------------------------------------ helpers
    def log(self, text):
        minutes, seconds = divmod(int(self.t), 60)
        self.events.appendleft("%02d:%02d  %s" % (minutes, seconds, text))

    def eye(self, station_x):
        """A photo-eye just after the station: TRUE while a bottle blocks its beam."""
        beam = station_x + GEOMETRY["eye_offset"]
        half = GEOMETRY["bottle_w"] / 2
        return any(abs(b.x - beam) < half for b in self.bottles)

    def bottle_at(self, station_x):
        best = None
        for b in self.bottles:
            if abs(b.x - station_x) <= WORK_WINDOW and (best is None or abs(b.x - station_x) < abs(best.x - station_x)):
                best = b
        return best

    def problems(self, b):
        found = []
        if b.defect:
            found.append(b.defect)
        if not b.capped:
            found.append("no cap")
        if b.ml < self.target_ml - QC_TOLERANCE:
            found.append("underfilled")
        elif b.ml > self.target_ml + QC_TOLERANCE:
            found.append("overfilled")
        return found

    def qc_reading(self):
        b = self.bottle_at(GEOMETRY["qc"])
        if b is None:
            return 0
        return max(0, int(round(b.ml + b.scale_error)))

    def pick_defect(self):
        if self.next_defect:
            defect, self.next_defect = self.next_defect, None
            return defect
        if random.random() * 100 < self.defect_rate:
            return random.choice(["crack", "dent"])
        return None

    @staticmethod
    def read_coils(station):
        """What the PLC wants. A real I/O module switches its outputs off when the PLC is silent."""
        with station.lock:
            coils = dict(station.coils)
        if not station.connected():
            coils = {n: False for n in coils}
        return coils

    # ------------------------------------------------------------ physics
    def step(self, dt):
        tank_coils = self.read_coils(self.tank)
        line_coils = self.read_coils(self.line)
        with self.lock:
            self.t += dt
            self.tank_coils = tank_coils
            self.line_coils = line_coils
            self.step_tank(dt, tank_coils)
            self.step_line(dt, line_coils)
            self.level = min(100.0, max(0.0, self.level))

            tank_inputs = {
                "TANK_START": self.buttons["TANK_START"],
                "TANK_STOP": self.buttons["TANK_STOP"],
                "LOW_LEVEL": self.level >= LOW_SWITCH,
                "HIGH_LEVEL": self.level >= HIGH_SWITCH,
            }
            b = self.bottle_at(GEOMETRY["filler"])
            line_inputs = {
                "LINE_START": self.buttons["LINE_START"],
                "LINE_STOP": self.buttons["LINE_STOP"],
                "ESTOP_OK": not self.estop_pressed,
                "ALARM_RESET": self.buttons["ALARM_RESET"],
                "BOTTLE_AT_FILLER": self.eye(GEOMETRY["filler"]),
                "BOTTLE_AT_CAPPER": self.eye(GEOMETRY["capper"]),
                "BOTTLE_AT_QC": self.eye(GEOMETRY["qc"]),
                "QC_DONE": self.qc_done,
                "QC_DAMAGE": self.qc_damage,
                "QC_NO_CAP": self.qc_no_cap,
                "BOTTLE_IN_CRATE": self.crate_pulse > 0,
                "BOTTLE_IN_REJECT": self.reject_pulse > 0,
                "CRATE_PRESENT": self.crate_present,
            }
            line_registers = {
                "FILLER_ML": int(round(b.ml)) if b else 0,
                "QC_ML": self.qc_reading(),
                "TARGET_ML": self.target_ml,
            }

        with self.tank.lock:
            self.tank.inputs.update(tank_inputs)
        with self.line.lock:
            self.line.inputs.update(line_inputs)
            self.line.registers.update(line_registers)

    def step_tank(self, dt, coils):
        if coils["PUMP"] and not self.pump_fail:
            self.level += PUMP_RATE * dt
        self.level -= MAX_OTHER_USE * self.other_use / 100.0 * dt

    def step_line(self, dt, c):
        g = GEOMETRY

        # Feeder: one new bottle on each rising edge of BOTTLE_RELEASE
        release = c["BOTTLE_RELEASE"]
        if release and not self.prev_release:
            if all(b.x > g["spawn"] + g["bottle_w"] + 6 for b in self.bottles):
                self.bottles.append(Bottle(self.next_number, g["spawn"], self.pick_defect()))
                self.next_number += 1
            else:
                self.log("Feeder: no room on the belt, no bottle released")
        self.prev_release = release

        # Filler: water comes out of the tank. Closing a valve takes a moment.
        self.fast_timer = VALVE_CLOSE_DELAY if c["FILL_FAST"] else max(0.0, self.fast_timer - dt)
        self.slow_timer = VALVE_CLOSE_DELAY if c["FILL_SLOW"] else max(0.0, self.slow_timer - dt)
        rate = (FAST_RATE if self.fast_timer > 0 else 0.0) + (SLOW_RATE if self.slow_timer > 0 else 0.0)
        if self.clog:
            rate *= CLOG_FACTOR
        dry = rate > 0 and self.level <= 0.5
        if dry and not self.dry:
            self.log("The filler is open but the tank is empty")
        self.dry = dry
        if dry:
            rate = 0.0
        self.flow = rate
        if rate > 0:
            ml = rate * dt
            self.level -= ml / 1000.0 / TANK_LITERS * 100.0
            b = self.bottle_at(g["filler"])
            if b and not b.capped:
                into = min(BOTTLE_CAPACITY - b.ml, ml)
                b.ml += into
                self.spilled += ml - into
            else:
                self.spilled += ml

        for b in self.bottles:
            if b.defect == "crack" and b.ml > 0:
                b.ml = max(0.0, b.ml - LEAK_RATE * dt)

        # Conveyor
        if c["CONVEYOR"] and not self.jam:
            dx = BELT_SPEED * dt
            self.belt_offset = (self.belt_offset + dx) % 24
            for b in list(self.bottles):
                old = b.x
                b.x += dx
                if old < g["diverter"] <= b.x and self.gate_pos > 0.5:
                    self.bottles.remove(b)
                    self.to_bin(b)
                elif b.x >= g["end"]:
                    self.bottles.remove(b)
                    self.to_crate(b)

        # Capper: the cap goes on when the head reaches the bottom
        self.capper_pos = approach(self.capper_pos, 1.0 if c["CAPPER"] else 0.0, dt / CAP_TRAVEL)
        if self.capper_pos >= 0.99:
            b = self.bottle_at(g["capper"])
            if b and not b.capped and not self.no_caps:
                b.capped = True

        # Diverter gate
        self.gate_pos = approach(self.gate_pos, 1.0 if c["DIVERTER"] else 0.0, dt / GATE_TRAVEL)

        # QC camera: starts on the rising edge of QC_SCAN, results stay until QC_SCAN goes off
        scan = c["QC_SCAN"]
        if scan and not self.prev_scan:
            self.scan_timer = SCAN_TIME
            self.qc_done = self.qc_damage = self.qc_no_cap = False
        if not scan:
            self.scan_timer = 0.0
            self.qc_done = self.qc_damage = self.qc_no_cap = False
        elif self.scan_timer > 0:
            self.scan_timer -= dt
            if self.scan_timer <= 0:
                self.scan_timer = 0.0
                self.finish_scan()
        self.prev_scan = scan

        # Collector photo-eyes and the crate swap
        self.crate_pulse = max(0.0, self.crate_pulse - dt)
        self.reject_pulse = max(0.0, self.reject_pulse - dt)
        if not self.crate_present:
            self.crate_away -= dt
            if self.crate_away <= 0:
                self.crate_present = True
                self.log("A new empty crate is in place")
        while self.crate_times and self.t - self.crate_times[0] > 60:
            self.crate_times.popleft()

    def finish_scan(self):
        b = self.bottle_at(GEOMETRY["qc"])
        self.qc_done = True
        if b is None:
            self.qc_damage = self.qc_no_cap = False
            self.last_scan = "Scan: no bottle in front of the camera"
            return
        self.qc_damage = b.defect is not None
        self.qc_no_cap = not b.capped
        found = self.problems(b)
        b.tag = ", ".join(found) if found else "OK"
        b.tag_bad = bool(found)
        self.last_scan = "Bottle %d: %d ml, %s" % (b.number, self.qc_reading(), b.tag)

    def to_crate(self, b):
        if not self.crate_present or self.crate_count >= CRATE_SIZE:
            self.score["broken"] += 1
            self.log("Bottle %d fell on the floor (crate %s)" % (
                b.number, "missing" if not self.crate_present else "full"))
            return
        self.crate_count += 1
        self.crate_pulse = PULSE_TIME
        self.crate_times.append(self.t)
        found = self.problems(b)
        if found:
            self.score["bad_shipped"] += 1
            self.log("Bad bottle %d went into the crate (%s)" % (b.number, ", ".join(found)))
        else:
            self.score["good_shipped"] += 1
            if self.crate_count == CRATE_SIZE:
                self.log("The crate is full. Click Swap crate")

    def to_bin(self, b):
        self.reject_pulse = PULSE_TIME
        if self.bin_count >= BIN_SIZE:
            self.score["broken"] += 1
            self.log("Bottle %d fell out of the full reject bin" % b.number)
            return
        self.bin_count += 1
        found = self.problems(b)
        if found:
            self.score["bad_rejected"] += 1
            self.log("Bottle %d rejected (%s)" % (b.number, ", ".join(found)))
        else:
            self.score["good_rejected"] += 1
            self.log("Good bottle %d went into the reject bin" % b.number)

    # ------------------------------------------------------------ web page
    def command(self, data):
        cmd = data.get("cmd")
        value = data.get("value")
        with self.lock:
            if cmd == "button" and data.get("name") in self.buttons:
                self.buttons[data["name"]] = bool(value)
            elif cmd == "estop":
                self.estop_pressed = bool(value)
                self.log("E-stop pressed" if self.estop_pressed else "E-stop released")
            elif cmd == "target":
                self.target_ml = int(min(BOTTLE_CAPACITY, max(0, int(value))))
            elif cmd == "other_use":
                self.other_use = float(min(100, max(0, float(value))))
            elif cmd == "defect_rate":
                self.defect_rate = float(min(100, max(0, float(value))))
            elif cmd in ("jam", "clog", "pump_fail", "no_caps"):
                setattr(self, cmd, bool(value))
                names = {"jam": "Conveyor jam", "clog": "Clogged nozzle",
                         "pump_fail": "Supply pump failure", "no_caps": "Capper out of caps"}
                self.log("%s %s" % (names[cmd], "ON" if value else "cleared"))
            elif cmd == "next_defect" and value in ("crack", "dent"):
                self.next_defect = value
                self.log("The next bottle will arrive %s" % ("cracked" if value == "crack" else "dented"))
            elif cmd == "swap_crate":
                if self.crate_present:
                    if self.crate_count:
                        self.crates_done += 1
                    self.crate_present = False
                    self.crate_away = CRATE_SWAP_TIME
                    self.crate_count = 0
                    self.log("Crate taken away")
            elif cmd == "empty_bin":
                self.bin_count = 0
                self.log("Reject bin emptied. Press ALARM RESET so the PLC counts from 0 again")
            elif cmd == "clear_belt":
                self.bottles.clear()
                self.log("Belt cleared by hand")
            elif cmd == "tank_level":
                self.level = float(min(100, max(0, float(value))))
            elif cmd == "reset_score":
                for k in self.score:
                    self.score[k] = 0
                self.spilled = 0.0
                self.crate_times.clear()
            else:
                return False
        return True

    def snapshot(self):
        with self.tank.lock:
            tank_inputs = dict(self.tank.inputs)
        with self.line.lock:
            line_inputs = dict(self.line.inputs)
            line_registers = dict(self.line.registers)
        with self.lock:
            return {
                "t": round(self.t, 2),
                "links": {
                    "tank": {"up": self.tank.connected(), "requests": self.tank.requests, "port": self.tank.port},
                    "line": {"up": self.line.connected(), "requests": self.line.requests, "port": self.line.port},
                },
                "level": round(self.level, 2),
                "other_use": self.other_use,
                "pump_fail": self.pump_fail,
                "buttons": dict(self.buttons),
                "estop": self.estop_pressed,
                "target": self.target_ml,
                "tank_inputs": tank_inputs,
                "tank_coils": dict(self.tank_coils),
                "line_inputs": line_inputs,
                "line_registers": line_registers,
                "line_coils": dict(self.line_coils),
                "bottles": [b.to_json() for b in self.bottles],
                "belt_offset": round(self.belt_offset, 1),
                "flow": self.flow,
                "dry": self.dry,
                "capper_pos": round(self.capper_pos, 3),
                "gate_pos": round(self.gate_pos, 3),
                "scanning": self.scan_timer > 0,
                "last_scan": self.last_scan,
                "crate": {"present": self.crate_present, "count": self.crate_count, "done": self.crates_done},
                "bin": self.bin_count,
                "faults": {"jam": self.jam, "clog": self.clog, "pump_fail": self.pump_fail,
                           "no_caps": self.no_caps},
                "defect_rate": self.defect_rate,
                "next_defect": self.next_defect,
                "score": dict(self.score),
                "spilled": round(self.spilled),
                "per_minute": len(self.crate_times),
                "events": list(self.events),
            }


def run_physics(plant):
    last = time.monotonic()
    while True:
        time.sleep(0.02)
        now = time.monotonic()
        plant.step(min(0.1, now - last))
        last = now


def make_web_handler(plant):
    class WebHandler(BaseHTTPRequestHandler):
        def send_body(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    self.send_body(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/state":
                self.send_body(200, json.dumps(plant.snapshot()).encode(), "application/json")
            elif self.path == "/geometry":
                self.send_body(200, json.dumps(GEOMETRY).encode(), "application/json")
            else:
                self.send_body(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/cmd":
                self.send_body(404, b"not found", "text/plain")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(size) or b"{}")
                ok = plant.command(data)
            except (ValueError, TypeError):
                ok = False
            self.send_body(200 if ok else 400, json.dumps({"ok": ok}).encode(), "application/json")

        def log_message(self, *args):
            pass

    return WebHandler


def main():
    parser = argparse.ArgumentParser(description="Bottling plant simulator")
    parser.add_argument("--web-port", type=int, default=8090, help="web page port (default 8090)")
    parser.add_argument("--defect-rate", type=float, default=5.0, help="%% of bottles that arrive damaged")
    parser.add_argument("--tank-port", type=int, help="use another Modbus port for the tank (testing)")
    parser.add_argument("--line-port", type=int, help="use another Modbus port for the line (testing)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = parser.parse_args()

    tank, line = plant_io.make_stations()
    if args.tank_port:
        tank.port = args.tank_port
    if args.line_port:
        line.port = args.line_port
    check_names(tank, line)

    try:
        tank.start()
        line.start()
    except OSError:
        print("Port %d or %d is already in use." % (tank.port, line.port))
        print("Close the other program that uses it (plant_io.py, or the old Start Tank.bat) and try again.")
        return 1

    plant = Plant(tank, line, args.defect_rate)
    threading.Thread(target=run_physics, args=(plant,), daemon=True).start()

    try:
        web = ThreadingHTTPServer(("127.0.0.1", args.web_port), make_web_handler(plant))
    except OSError:
        print("Web port %d is already in use. Is the simulator already running?" % args.web_port)
        return 1
    web.daemon_threads = True
    url = "http://localhost:%d" % args.web_port
    print("Plant picture:", url)
    print("Press Ctrl + C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
