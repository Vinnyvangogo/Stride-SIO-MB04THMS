ver 6

Stride Field I/O SIO-MB04THMS - 4-Channel Thermocouple Monitor
=================================================================

Reads all 4 thermocouple channels from a Stride SIO-MB04THMS Modbus TCP
temperature module and displays them on a live-updating, dark-themed GUI.

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

Tested against pymodbus 3.x. Adjust IP / unit ID / port in the CONFIG
section below as needed.

Daily behavior:
    - The live plot accumulates a full day's worth of readings and clears
      itself automatically at midnight to start the next day fresh.
    - If CSV recording is active when midnight hits, the current file is
      closed and a new one is opened automatically (one file per day,
      named "thermocouple_log_YYYY-MM-DD.csv" in the folder chosen when
      recording was started).
