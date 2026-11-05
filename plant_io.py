import threading
import time
import struct
import socketserver
#----------------------#

TANK_PORT = 5020

TANK_INPUTS = [                # FC 2 Read Discrete Input -> PLC
    "TANK_START",              # 0 %IX0.0
    "TANK_STOP",               # 1 %IX0.1
    "LOW_LEVEL",               # 2 %IX0.2
    "HIGH_LEVEL",              # 3 %IX0.3
]

TANK_COILS = [                 # FC 15 Write Multiple Coils <- PLC
    "PUMP",                    # 0 %QX0.0
    "FULL_LAMP",               # 1 %QX0.1
]



TANK_REGISTERS = []

#----------------- Station 2: bottling line ------------------------#

LINE_PORT = 5021


LINE_INPUTS = [               # FC 2 Read Discrete Input  -> PLC
    "LINE_START",             # 0  %IX0.4
    "LINE_STOP",              # 1  %IX0.5
    "ESTOP_OK",               # 2  %IX0.6
    "ALARM_RESET",            # 3  %IX0.7
    "BOTTLE_AT_FILLER",       # 4  %IX1.0
    "BOTTLE_AT_CAPPER",       # 5  %IX1.1
    "BOTTLE_AT_QC",           # 6  %IX1.2
    "QC_DONE",                # 7  %IX1.3
    "QC_DAMAGE",              # 8  %IX1.4
    "QC_NO_CAP",              # 9  %IX1.5
    "BOTTLE_IN_CRATE",        # 10 %IX1.6
    "BOTTLE_IN_REJECT",       # 11 %IX1.7
    "CRATE_PRESENT",         # 12 %IX2.0
]


LINE_REGISTERS = [           # FC 4 Read Input Register -> PLC
    "FILLER_ML",             # 0 %IW0
    "QC_ML",                 # 1 %IW1
    "TARGET_ML",             # 2 %IW2
] 


LINE_COILS = [               # FC 15 Write Multiple Coils <- PLC
    "BOTTLE_RELEASE",        # 0 %QX0.2
    "CONVEYOR",              # 1 %QX0.3
    "FILL_FAST",             # 2 %QX0.4
    "FILL_SLOW",             # 3 %QX0.5
    "CAPPER",                # 4 %QX0.6
    "QC_SCAN",               # 5 %QX0.7
    "DIVERTER",              # 6 %QX1.0
    "GREEN_LAMP",            # 7 %QX1.1
    "AMBER_LAMP",            # 8 %QX1.2
    "RED_LAMP",              # 9 %QX1.3
]









#---------- Step 2: Station stores the value the PLC reads and writes ------------#
COMMS_TIMEOUT = 2.0 # seconds without a PLC request before say not connected


# ---------- Step S2: a Station stores the values the PLC reads and writes ----------
COMMS_TIMEOUT = 2.0   # seconds without a PLC request before we say "not connected"


# ---------- Step S3: helper that packs ON/OFF values into bytes ----------
def pack_bits(bits):
    """Pack a list of True/False into bytes: 8 bits per byte, bit 0 first (Modbus order)."""
    data = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            data[i // 8] |= 1 << (i % 8)
    return bytes(data)


class Station:
    """One remote I/O module, like TankIO or BottleIO in OpenPLC."""

    def __init__(self, name, port, input_names, coil_names, register_names):
        self.name = name
        self.port = port
        self.input_names = input_names
        self.coil_names = coil_names
        self.register_names = register_names

        # Simulator -> PLC  (the PLC reads these)
        self.inputs = {}
        for n in input_names:
            self.inputs[n] = False
        self.registers = {}
        for n in register_names:
            self.registers[n] = 0

        # PLC -> simulator  (the PLC writes these)
        self.coils = {}
        for n in coil_names:
            self.coils[n] = False

        self.lock = threading.Lock()
        self.last_request = None       # clock time of the PLC's last message
        self.requests = 0              # how many messages the PLC has sent

    def connected(self):
        """True if the PLC has talked to us in the last 2 seconds."""
        if self.last_request is None:
            return False
        return time.monotonic() - self.last_request < COMMS_TIMEOUT

    def answer(self, request):
        """Answer one Modbus request from the PLC and return the reply."""
        function = request[0]
        with self.lock:
            self.last_request = time.monotonic()
            self.requests += 1

            if function == 2:                                  # FC 2: Read Discrete Inputs
                start, count = struct.unpack(">HH", request[1:5])
                if start + count > len(self.input_names):
                    return bytes([function + 0x80, 2])         # error 2: no such address
                bits = []
                for name in self.input_names[start:start + count]:
                    bits.append(self.inputs[name])
                data = pack_bits(bits)
                return bytes([function, len(data)]) + data

            if function == 4:                                  # FC 4: Read Input Registers
                start, count = struct.unpack(">HH", request[1:5])
                if start + count > len(self.register_names):
                    return bytes([function + 0x80, 2])
                data = b""
                for name in self.register_names[start:start + count]:
                    value = max(0, min(65535, int(self.registers[name])))
                    data += struct.pack(">H", value)
                return bytes([function, len(data)]) + data

            if function == 15:                                 # FC 15: Write Multiple Coils
                start, count, size = struct.unpack(">HHB", request[1:6])
                if start + count > len(self.coil_names):
                    return bytes([function + 0x80, 2])
                data = request[6:6 + size]
                for i in range(count):
                    name = self.coil_names[start + i]
                    self.coils[name] = bool(data[i // 8] >> (i % 8) & 1)
                return request[0:5]

        
        
        return bytes([function + 0x80, 1])                     # error 1: function not supported

                                      # error 1: function not supported

    
    def start(self):                                           # ← now inside Station
        """Open this station's port and answer the PLC in the background."""
        server = socketserver.ThreadingTCPServer(("0.0.0.0", self.port), ModbusHandler)
        server.daemon_threads = True
        server.station = self
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(self.name, "is listening on port", self.port)
        



def make_stations():
    """Create the two stations of the plant."""
    tank = Station("Water tank", TANK_PORT, TANK_INPUTS, TANK_COILS, TANK_REGISTERS)
    line = Station("Bottling line", LINE_PORT, LINE_INPUTS, LINE_COILS, LINE_REGISTERS)
    return tank, line


#---- Step 4 : Modbus TCP --------#

def recv_exact(sock, size):
    """Receive exactly size bytes from the PLC, or None if the PLC hung up."""
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data



class ModbusHandler(socketserver.BaseRequestHandler):
    """Talks to one connected PLC: read a requestm answer it, repeat."""

    def handle(self):
        station = self.server.station
        try:
            while True:
                header = recv_exact(self.request, 7)
                if header is None:
                    return
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                if length < 2:
                    return
                request = recv_exact(self.request, length - 1)
                if request is None:
                    return
                reply = station.answer(request)
                header = struct.pack(">HHHB", transaction, protocol, len(reply) + 1, unit)
                self.request.sendall(header + reply)
        except OSError:
            return
        
    


# ---------- Self-check: talk to the REAL PLC ----------
if __name__ == "__main__":
    tank, line = make_stations()
    tank.start()
    line.start()

    # How the plant looks before anyone presses a button
    line.inputs["ESTOP_OK"] = True          # E-stop is NOT pressed
    line.inputs["CRATE_PRESENT"] = True     # an empty crate is in place
    line.registers["TARGET_ML"] = 700       # the operator's fill target

    print("Waiting for the PLC... press Ctrl + C to stop.")
    seconds = 0
    while True:
        time.sleep(1)
        if tank.connected() and line.connected():
            seconds += 1                    # count only while the PLC is talking to us

        # A tiny test: press the start buttons like an operator would
        if seconds == 3:
            print(">>> pressing TANK_START and LINE_START")
            tank.inputs["TANK_START"] = True
            line.inputs["LINE_START"] = True
        if seconds == 4:
            print(">>> releasing the buttons")
            tank.inputs["TANK_START"] = False
            line.inputs["LINE_START"] = False

        for station in (tank, line):
            on = []
            for name in station.coil_names:
                if station.coils[name]:
                    on.append(name)
            print(station.name, "| PLC connected:", station.connected(),
                  "| requests:", station.requests, "| coils ON:", on)


