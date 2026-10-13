ver 7

Stride Field I/O SIO-MB04THMS - 4-Channel Thermocouple Monitor
=================================================================
ver 7
Reads all 4 thermocouple channels from a Stride SIO-MB04THMS Modbus TCP
temperature module and displays them on a live-updating, dark-themed GUI.
 
NEW in ver 7 - device discovery:
    - On startup a "Find SIO-MB04THMS" window opens before any connection
      is made. It first checks the last-used address (remembered between
      runs), and if the unit isn't there it scans the PC's local subnet(s)
      for hosts with Modbus/TCP port 502 open. Each responding host is
      probed by reading the SIO-MB04THMS registers (40041-40044, 40050,
      40031-40034); hosts that answer all of them are marked as a match.
    - If nothing is found, type the unit's IP address (and port / node ID)
      into the manual-entry fields, optionally click "Test", then Connect.
    - The scan range is editable (e.g. "192.168.1.0/24" or a comma-separated
      list of networks / single IPs) for when the unit is on another subnet.
    - A "Change Device..." button in the main window reopens the finder.
    - Command line: --ip 192.168.1.50 [--port 502] [--unit 1] skips the
      finder and connects directly.
 
Device register map (from the official SIO-MB04THMS User Guide):
    40041-40044   Analog Input Ch0-Ch3   (RO, signed 16-bit, x0.1 for TC)
    40050         Break Status bits      (RO, bit0=Ch0 ... bit3=Ch3)
    40031-40034   Input Type Ch0-Ch3     (R/W)
    41218-41221   Degree Units Ch0-Ch3   (R/W)  0=C, 1=F, 2=K
 
Modbus holding-register reads use function code 03. Register 40041 maps to
zero-based protocol address 40 (40041 - 40001), so channels are read with:
    read_holding_registers(address=40, count=4, slave=UNIT_ID)
 
Requirements:
    pip install pymodbus matplotlib
 
Tested against pymodbus 3.x.
 
Daily behavior:
    - The live plot accumulates a full day's worth of readings and clears
      itself automatically at midnight to start the next day fresh.
    - If CSV recording is active when midnight hits, the current file is
      closed and a new one is opened automatically (one file per day,
      named "thermocouple_log_YYYY-MM-DD.csv" in the folder chosen when
      recording was started).
