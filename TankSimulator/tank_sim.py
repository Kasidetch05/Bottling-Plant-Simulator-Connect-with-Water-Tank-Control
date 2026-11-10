"""Virtual water tank for the OpenPLC water tank project.

The window shows a tank with a pump, two float switches and an outflow valve.
Behind it, this program is a Modbus TCP remote I/O module that OpenPLC
Runtime v4 polls as a Remote Device:

  Discrete inputs (FC 2)   0 START_BUTTON  1 STOP_BUTTON  2 LOW_LEVEL  3 HIGH_LEVEL
  Coils (FC 1/5/15)        0 PUMP          1 FULL_LAMP
  Input register (FC 4)    0 tank level in 0.1 % steps (0-1000)

Run:  python tank_sim.py               window + Modbus server on port 5020
      python tank_sim.py --headless    no window (for testing)
"""

import argparse
import socketserver
import struct
import threading
import time

LOW_SWITCH = 20.0     # % level where the low float switch turns TRUE
HIGH_SWITCH = 85.0    # % level where the high float switch turns TRUE
PUMP_RATE = 4.0       # % per second the pump adds
MAX_OUTFLOW = 2.5     # % per second drained with the valve fully open
COMMS_TIMEOUT = 2.0   # s without PLC traffic before the outputs drop out
TABLE_SIZE = 16


class Plant:
    """Tank physics plus the Modbus I/O image, shared by the GUI and server threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.level = 50.0
        self.valve = 40.0
        self.start_pressed = False
        self.stop_pressed = False
        self.coils = [False] * TABLE_SIZE
        self.holding = [0] * TABLE_SIZE
        self.last_request = None
        self.requests = 0

    def discrete_inputs(self):
        bits = [self.start_pressed, self.stop_pressed,
                self.level >= LOW_SWITCH, self.level >= HIGH_SWITCH]
        return bits + [False] * (TABLE_SIZE - len(bits))

    def input_registers(self):
        return [int(round(self.level * 10))] + [0] * (TABLE_SIZE - 1)

    def connected(self):
        return self.last_request is not None and time.monotonic() - self.last_request < COMMS_TIMEOUT

    def step(self, dt):
        with self.lock:
            if not self.connected():
                # A real I/O module switches its outputs off when the PLC stops talking to it.
                self.coils[0] = self.coils[1] = False
            if self.coils[0]:
                self.level += PUMP_RATE * dt
            self.level -= MAX_OUTFLOW * self.valve / 100.0 * dt
            self.level = min(100.0, max(0.0, self.level))


# ---------------------------------------------------------------- Modbus TCP

def pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def exception(fc, code):
    return bytes([fc | 0x80, code])


def process(plant, pdu):
    """Answer one Modbus request PDU (function code + data) and return the reply PDU."""
    fc = pdu[0]
    with plant.lock:
        plant.last_request = time.monotonic()
        plant.requests += 1
        try:
            if fc in (1, 2, 3, 4):
                addr, qty = struct.unpack(">HH", pdu[1:5])
                if qty < 1:
                    return exception(fc, 3)
                if fc == 1:
                    table = plant.coils
                elif fc == 2:
                    table = plant.discrete_inputs()
                elif fc == 3:
                    table = plant.holding
                else:
                    table = plant.input_registers()
                if addr + qty > len(table):
                    return exception(fc, 2)
                values = table[addr:addr + qty]
                if fc in (1, 2):
                    data = pack_bits(values)
                else:
                    data = struct.pack(f">{qty}H", *values)
                return bytes([fc, len(data)]) + data

            if fc == 5:
                addr, value = struct.unpack(">HH", pdu[1:5])
                if addr >= len(plant.coils):
                    return exception(fc, 2)
                if value not in (0x0000, 0xFF00):
                    return exception(fc, 3)
                plant.coils[addr] = value == 0xFF00
                return pdu[:5]

            if fc == 6:
                addr, value = struct.unpack(">HH", pdu[1:5])
                if addr >= len(plant.holding):
                    return exception(fc, 2)
                plant.holding[addr] = value
                return pdu[:5]

            if fc == 15:
                addr, qty, count = struct.unpack(">HHB", pdu[1:6])
                if addr + qty > len(plant.coils):
                    return exception(fc, 2)
                data = pdu[6:6 + count]
                for i in range(qty):
                    plant.coils[addr + i] = bool(data[i // 8] >> (i % 8) & 1)
                return pdu[:5]

            if fc == 16:
                addr, qty, count = struct.unpack(">HHB", pdu[1:6])
                if addr + qty > len(plant.holding):
                    return exception(fc, 2)
                plant.holding[addr:addr + qty] = struct.unpack(f">{qty}H", pdu[6:6 + count])
                return pdu[:5]
        except (struct.error, IndexError):
            return exception(fc, 3)
    return exception(fc, 1)


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class ModbusHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        try:
            while True:
                header = recv_exact(sock, 7)
                if header is None:
                    return
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                if length < 2:
                    return
                pdu = recv_exact(sock, length - 1)
                if pdu is None:
                    return
                reply = process(self.server.plant, pdu)
                sock.sendall(struct.pack(">HHHB", tid, pid, len(reply) + 1, unit) + reply)
        except OSError:
            return


class ModbusServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, address, plant):
        super().__init__(address, ModbusHandler)
        self.plant = plant


def run_physics(plant, stop_event):
    last = time.monotonic()
    while not stop_event.is_set():
        time.sleep(0.05)
        now = time.monotonic()
        plant.step(now - last)
        last = now


# ---------------------------------------------------------------- Window

def run_window(plant, port):
    import tkinter as tk

    TANK_X0, TANK_X1, TANK_TOP, TANK_BOTTOM = 210, 370, 70, 410
    TANK_H = TANK_BOTTOM - TANK_TOP
    WATER = "#4a90d9"
    ON, OFF = "#3cb043", "#c8c8c8"

    def level_y(percent):
        return TANK_BOTTOM - TANK_H * percent / 100.0

    root = tk.Tk()
    root.title("Virtual water tank - Modbus TCP port %d" % port)
    root.resizable(False, False)

    c = tk.Canvas(root, width=600, height=470, bg="white", highlightthickness=0)
    c.pack(padx=10, pady=(10, 0))

    # Supply pipe, pump and inlet pipe into the top of the tank.
    c.create_line(10, 40, 116, 40, width=8, fill="#888888")
    c.create_text(48, 22, text="Water supply", font=("Segoe UI", 9))
    pump = c.create_oval(116, 16, 164, 64, width=2, fill=OFF)
    c.create_polygon(130, 28, 130, 52, 152, 40, fill="#555555")
    c.create_text(140, 80, text="Pump", font=("Segoe UI", 10, "bold"))
    c.create_line(164, 40, 290, 40, 290, TANK_TOP, width=8, fill="#888888")
    stream = c.create_line(290, TANK_TOP, 290, TANK_TOP, width=6, fill=WATER, state="hidden")

    # Tank, water and level scale.
    water = c.create_rectangle(TANK_X0, TANK_BOTTOM, TANK_X1, TANK_BOTTOM, fill=WATER, outline="")
    c.create_rectangle(TANK_X0, TANK_TOP, TANK_X1, TANK_BOTTOM, width=3)
    for p in (0, 25, 50, 75, 100):
        y = level_y(p)
        c.create_line(TANK_X0 - 10, y, TANK_X0, y)
        c.create_text(TANK_X0 - 14, y, text="%d%%" % p, anchor="e", font=("Segoe UI", 9))
    level_text = c.create_text((TANK_X0 + TANK_X1) / 2, TANK_TOP + 20, text="", font=("Segoe UI", 16, "bold"))

    # Float switches on the right wall.
    def switch(percent, name):
        y = level_y(percent)
        c.create_line(TANK_X1, y, TANK_X1 + 18, y, width=2)
        dot = c.create_oval(TANK_X1 + 18, y - 8, TANK_X1 + 34, y + 8, fill=OFF)
        label = c.create_text(TANK_X1 + 42, y, anchor="w", font=("Segoe UI", 10), text=name)
        return dot, label, name

    high = switch(HIGH_SWITCH, "HIGH_LEVEL (%d%%)" % HIGH_SWITCH)
    low = switch(LOW_SWITCH, "LOW_LEVEL (%d%%)" % LOW_SWITCH)

    # Outlet pipe and valve.
    c.create_line(290, TANK_BOTTOM, 290, 440, 580, 440, width=8, fill="#888888")
    c.create_polygon(430, 428, 430, 452, 470, 428, 470, 452, fill="#555555")
    outflow = c.create_line(470, 440, 580, 440, width=4, fill=WATER, state="hidden")
    valve_text = c.create_text(450, 418, text="", font=("Segoe UI", 9))

    # Indicator lamps.
    def lamp(y, name):
        dot = c.create_oval(20, y - 10, 40, y + 10, fill=OFF)
        c.create_text(48, y, anchor="w", text=name, font=("Segoe UI", 10))
        return dot

    pump_lamp = lamp(150, "PUMP (coil 0)")
    full_lamp = lamp(190, "FULL_LAMP (coil 1)")
    link_lamp = lamp(250, "PLC link")
    link_text = c.create_text(20, 280, anchor="w", font=("Segoe UI", 9), text="")

    # Controls.
    controls = tk.Frame(root)
    controls.pack(fill="x", padx=10, pady=10)

    def momentary(text, colour, attr):
        button = tk.Button(controls, text=text, width=12, height=2, bg=colour, fg="white",
                           activebackground=colour, font=("Segoe UI", 10, "bold"))

        def press(_event):
            with plant.lock:
                setattr(plant, attr, True)

        def release(_event):
            with plant.lock:
                setattr(plant, attr, False)

        button.bind("<ButtonPress-1>", press)
        button.bind("<ButtonRelease-1>", release)
        return button

    momentary("START", "#2e8b3d", "start_pressed").grid(row=0, column=0, rowspan=2, padx=(0, 6))
    momentary("STOP", "#c0392b", "stop_pressed").grid(row=0, column=1, rowspan=2, padx=(0, 16))

    tk.Label(controls, text="Outflow valve %").grid(row=0, column=2, sticky="w")
    valve = tk.Scale(controls, from_=0, to=100, orient="horizontal", length=200)
    valve.set(plant.valve)
    valve.grid(row=1, column=2, padx=(0, 16))

    def set_level(value):
        with plant.lock:
            plant.level = value

    tk.Button(controls, text="Drain to 10%", command=lambda: set_level(10.0)).grid(row=0, column=3, sticky="ew")
    tk.Button(controls, text="Reset to 50%", command=lambda: set_level(50.0)).grid(row=1, column=3, sticky="ew")

    def refresh():
        with plant.lock:
            plant.valve = float(valve.get())
            level = plant.level
            di = plant.discrete_inputs()
            pump_on, full_on = plant.coils[0], plant.coils[1]
            connected = plant.connected()
            requests = plant.requests

        y = level_y(level)
        c.coords(water, TANK_X0 + 2, y, TANK_X1 - 1, TANK_BOTTOM - 1)
        c.itemconfigure(level_text, text="%.1f %%" % level)
        c.coords(stream, 290, TANK_TOP, 290, y)
        c.itemconfigure(stream, state="normal" if pump_on else "hidden")
        c.itemconfigure(pump, fill=ON if pump_on else OFF)
        c.itemconfigure(pump_lamp, fill=ON if pump_on else OFF)
        c.itemconfigure(full_lamp, fill="#f1c40f" if full_on else OFF)
        for (dot, label, name), state in ((high, di[3]), (low, di[2])):
            c.itemconfigure(dot, fill=ON if state else OFF)
            c.itemconfigure(label, text="%s = %s" % (name, "TRUE" if state else "FALSE"))
        flowing = plant.valve > 0 and level > 0
        c.itemconfigure(outflow, state="normal" if flowing else "hidden")
        c.itemconfigure(valve_text, text="Outflow valve %d%%" % plant.valve)
        c.itemconfigure(link_lamp, fill=ON if connected else "#e74c3c")
        if connected:
            c.itemconfigure(link_text, text="Connected - %d requests" % requests)
        else:
            c.itemconfigure(link_text, text="Waiting for PLC on port %d" % port)
        root.after(50, refresh)

    refresh()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default all)")
    parser.add_argument("--port", type=int, default=5020, help="Modbus TCP port (default 5020)")
    parser.add_argument("--headless", action="store_true", help="run without a window")
    args = parser.parse_args()

    plant = Plant()
    stop = threading.Event()
    server = ModbusServer((args.host, args.port), plant)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=run_physics, args=(plant, stop), daemon=True).start()
    print("Modbus TCP remote I/O listening on %s:%d" % (args.host, args.port), flush=True)

    try:
        if args.headless:
            while True:
                time.sleep(1)
                with plant.lock:
                    print("level %.1f%%  pump %s  plc %s" % (
                        plant.level, plant.coils[0], "connected" if plant.connected() else "waiting"), flush=True)
        else:
            run_window(plant, args.port)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
