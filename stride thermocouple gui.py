#!/usr/bin/env python3
"""
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
"""

import csv
import queue
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    raise SystemExit(
        "pymodbus is required. Install it with:  pip install pymodbus"
    )

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
DEVICE_IP = "192.168.1.10"
DEVICE_PORT = 502
UNIT_ID = 1                     # Modbus slave/node ID of the SIO-MB04THMS

CH_START_ADDR = 40              # 40041 -> zero-based address 40 (4 regs)
BREAK_ADDR = 49                 # 40050 -> zero-based address 49 (1 reg)

CHANNEL_NAMES = ["Channel 1 (Ch0)", "Channel 2 (Ch1)",
                  "Channel 3 (Ch2)", "Channel 4 (Ch3)"]
CHANNEL_COLORS = ["#ff5555", "#55ff88", "#5599ff", "#ffcc55"]

MAX_PLOT_POINTS = 600           # rolling window shown on the graph
DEFAULT_INTERVAL_S = 5
MIN_INTERVAL_S = 1
MAX_INTERVAL_S = 60


def raw_to_signed16(val):
    """Convert an unsigned 16-bit Modbus register value to signed."""
    return val - 65536 if val > 32767 else val


# ----------------------------------------------------------------------
# Background Modbus polling worker
# ----------------------------------------------------------------------
class ModbusWorker(threading.Thread):
    """Polls the SIO-MB04THMS at a configurable interval and pushes
    results onto a thread-safe queue for the GUI thread to consume."""

    def __init__(self, ip, port, unit_id, out_queue):
        super().__init__(daemon=True)
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self.out_queue = out_queue

        self.interval = DEFAULT_INTERVAL_S
        self._stop_event = threading.Event()
        self.client = None

    def set_interval(self, seconds):
        self.interval = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, seconds))

    def stop(self):
        self._stop_event.set()

    def _ensure_connected(self):
        if self.client is None:
            self.client = ModbusTcpClient(self.ip, port=self.port, timeout=2)
        if not self.client.connected:
            self.client.connect()
        return self.client.connected

    def run(self):
        while not self._stop_event.is_set():
            start_time = time.time()
            connected = False
            temps = [None, None, None, None]
            breaks = [False, False, False, False]
            error_msg = None

            try:
                if self._ensure_connected():
                    rr = self.client.read_holding_registers(
                        address=CH_START_ADDR, count=4, slave=self.unit_id
                    )
                    br = self.client.read_holding_registers(
                        address=BREAK_ADDR, count=1, slave=self.unit_id
                    )
                    if (not rr.isError()) and (not br.isError()):
                        connected = True
                        temps = [raw_to_signed16(v) / 10.0 for v in rr.registers]
                        break_word = br.registers[0]
                        breaks = [bool(break_word & (1 << i)) for i in range(4)]
                    else:
                        error_msg = "Modbus read error"
                else:
                    error_msg = "Not connected"
            except Exception as exc:  # noqa: BLE001 - report any comms failure
                error_msg = str(exc)
                connected = False
                try:
                    if self.client:
                        self.client.close()
                except Exception:
                    pass
                self.client = None

            self.out_queue.put({
                "timestamp": datetime.now(),
                "connected": connected,
                "temps": temps,
                "breaks": breaks,
                "error": error_msg,
            })

            # Sleep the remainder of the interval, but wake early if stopped
            elapsed = time.time() - start_time
            remaining = max(0.0, self.interval - elapsed)
            self._stop_event.wait(remaining)

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass


# ----------------------------------------------------------------------
# Main GUI application
# ----------------------------------------------------------------------
class ThermocoupleApp:
    BG = "#1e1e1e"
    PANEL_BG = "#2a2a2a"
    FG = "#e0e0e0"
    ACCENT = "#3a7bd5"

    def __init__(self, root):
        self.root = root
        self.root.title("Stride SIO-MB04THMS - Thermocouple Monitor")
        self.root.geometry("1150x720")
        self.root.configure(bg=self.BG)

        self.data_queue = queue.Queue()
        self.worker = ModbusWorker(DEVICE_IP, DEVICE_PORT, UNIT_ID, self.data_queue)

        # Rolling buffers for plotting
        self.times = deque(maxlen=MAX_PLOT_POINTS)
        self.channel_data = [deque(maxlen=MAX_PLOT_POINTS) for _ in range(4)]
        self.channel_visible = [True, True, True, True]

        # CSV recording state
        self.recording = False
        self.csv_file = None
        self.csv_writer = None

        self._build_style()
        self._build_layout()
        self._build_plot()

        self.worker.start()
        self.root.after(200, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL_BG)
        style.configure("TLabel", background=self.BG, foreground=self.FG,
                         font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=self.PANEL_BG,
                         foreground=self.FG, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=self.BG, foreground=self.FG,
                         font=("Segoe UI", 12, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("Start.TButton", foreground="#101010", background="#4caf50")
        style.configure("Stop.TButton", foreground="#101010", background="#e53935")
        style.map("Start.TButton", background=[("active", "#66bb6a")])
        style.map("Stop.TButton", background=[("active", "#ef5350")])
        style.configure("Horizontal.TScale", background=self.BG)

    def _build_layout(self):
        top = ttk.Frame(self.root, style="TFrame", padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        # --- Connection LED ---
        conn_frame = ttk.Frame(top, style="Panel.TFrame", padding=8)
        conn_frame.pack(side=tk.LEFT, padx=(0, 10))
        self.conn_led = tk.Canvas(conn_frame, width=22, height=22,
                                   bg=self.PANEL_BG, highlightthickness=0)
        self.conn_led_id = self.conn_led.create_oval(3, 3, 19, 19, fill="#772222",
                                                       outline="#111111")
        self.conn_led.grid(row=0, column=0, padx=(0, 6))
        ttk.Label(conn_frame, text="Device Connection", style="Panel.TLabel").grid(
            row=0, column=1, sticky="w")
        self.conn_status_lbl = ttk.Label(conn_frame, text=f"{DEVICE_IP} : Node {UNIT_ID} - Disconnected",
                                          style="Panel.TLabel")
        self.conn_status_lbl.grid(row=1, column=0, columnspan=2, sticky="w")

        # --- Recording LED ---
        rec_frame = ttk.Frame(top, style="Panel.TFrame", padding=8)
        rec_frame.pack(side=tk.LEFT, padx=(0, 10))
        self.rec_led = tk.Canvas(rec_frame, width=22, height=22,
                                  bg=self.PANEL_BG, highlightthickness=0)
        self.rec_led_id = self.rec_led.create_oval(3, 3, 19, 19, fill="#772222",
                                                     outline="#111111")
        self.rec_led.grid(row=0, column=0, padx=(0, 6))
        ttk.Label(rec_frame, text="Recording to CSV", style="Panel.TLabel").grid(
            row=0, column=1, sticky="w")
        self.rec_status_lbl = ttk.Label(rec_frame, text="Not recording", style="Panel.TLabel")
        self.rec_status_lbl.grid(row=1, column=0, columnspan=2, sticky="w")

        # --- Record buttons ---
        btn_frame = ttk.Frame(top, style="TFrame")
        btn_frame.pack(side=tk.LEFT, padx=(0, 10))
        self.start_btn = ttk.Button(btn_frame, text="Start Recording",
                                     style="Start.TButton", command=self._start_recording)
        self.start_btn.grid(row=0, column=0, padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="Stop Recording",
                                    style="Stop.TButton", command=self._stop_recording,
                                    state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=4)

        # --- Interval control ---
        interval_frame = ttk.Frame(top, style="Panel.TFrame", padding=8)
        interval_frame.pack(side=tk.RIGHT)
        ttk.Label(interval_frame, text="Update Interval (s)", style="Panel.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.interval_var = tk.IntVar(value=DEFAULT_INTERVAL_S)
        self.interval_scale = ttk.Scale(interval_frame, from_=MIN_INTERVAL_S,
                                         to=MAX_INTERVAL_S, orient=tk.HORIZONTAL,
                                         variable=self.interval_var,
                                         command=self._on_interval_change, length=180)
        self.interval_scale.grid(row=1, column=0, padx=(0, 8))
        self.interval_lbl = ttk.Label(interval_frame, text=f"{DEFAULT_INTERVAL_S} s",
                                       style="Panel.TLabel")
        self.interval_lbl.grid(row=1, column=1)

        # --- Status bar at bottom ---
        self.status_bar = ttk.Label(self.root, text="Starting...", style="TLabel",
                                     padding=(10, 4))
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # --- Plot container ---
        self.plot_container = ttk.Frame(self.root, style="TFrame")
        self.plot_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))

    def _build_plot(self):
        plt.style.use("dark_background")
        self.fig = Figure(figsize=(9, 5.5), dpi=100, facecolor=self.BG)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#161616")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Temperature (\u00b0C)")
        self.ax.set_title("SIO-MB04THMS Live Thermocouple Readings")
        self.ax.grid(True, color="#3a3a3a", linestyle="--", linewidth=0.5)

        self.lines = []
        for i in range(4):
            (line,) = self.ax.plot([], [], label=CHANNEL_NAMES[i],
                                    color=CHANNEL_COLORS[i], linewidth=1.6)
            self.lines.append(line)

        self.legend = self.ax.legend(loc="upper left", facecolor="#222222",
                                      edgecolor="#444444", labelcolor=self.FG)
        # Map legend lines -> plot lines, enable click-to-toggle
        self.legend_map = {}
        for leg_line, orig_line in zip(self.legend.get_lines(), self.lines):
            leg_line.set_picker(8)
            self.legend_map[leg_line] = orig_line

        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_container)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("pick_event", self._on_legend_pick)
        self.canvas.draw()

    # ------------------------------------------------------------------
    # Legend click-to-toggle channel visibility
    # ------------------------------------------------------------------
    def _on_legend_pick(self, event):
        leg_line = event.artist
        orig_line = self.legend_map.get(leg_line)
        if orig_line is None:
            return
        idx = self.lines.index(orig_line)
        visible = not orig_line.get_visible()
        orig_line.set_visible(visible)
        self.channel_visible[idx] = visible
        leg_line.set_alpha(1.0 if visible else 0.25)
        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    # Interval slider
    # ------------------------------------------------------------------
    def _on_interval_change(self, _value):
        seconds = int(round(self.interval_var.get()))
        self.interval_lbl.config(text=f"{seconds} s")
        self.worker.set_interval(seconds)

    # ------------------------------------------------------------------
    # CSV Recording
    # ------------------------------------------------------------------
    def _start_recording(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=f"thermocouple_log_{datetime.now():%Y%m%d_%H%M%S}.csv",
            title="Save temperature log as..."
        )
        if not path:
            return
        try:
            self.csv_file = open(path, "w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(["Timestamp", "Channel1_C", "Channel2_C",
                                       "Channel3_C", "Channel4_C"])
            self.csv_file.flush()
        except OSError as exc:
            messagebox.showerror("File Error", f"Could not open file:\n{exc}")
            return

        self.recording = True
        self.rec_led.itemconfig(self.rec_led_id, fill="#33dd33")
        self.rec_status_lbl.config(text=f"Recording: {path.split('/')[-1]}")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def _stop_recording(self):
        self.recording = False
        self.rec_led.itemconfig(self.rec_led_id, fill="#772222")
        self.rec_status_lbl.config(text="Not recording")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception:
                pass
        self.csv_file = None
        self.csv_writer = None

    # ------------------------------------------------------------------
    # Queue polling / GUI update loop
    # ------------------------------------------------------------------
    def _poll_queue(self):
        updated = False
        latest = None
        try:
            while True:
                latest = self.data_queue.get_nowait()
                self._handle_reading(latest)
                updated = True
        except queue.Empty:
            pass

        if updated and latest is not None:
            self._redraw_plot()
            self._update_status_bar(latest)

        self.root.after(200, self._poll_queue)

    def _handle_reading(self, reading):
        connected = reading["connected"]
        temps = reading["temps"]
        ts = reading["timestamp"]

        # Update connection LED
        if connected:
            self.conn_led.itemconfig(self.conn_led_id, fill="#33dd33")
            self.conn_status_lbl.config(
                text=f"{DEVICE_IP} : Node {UNIT_ID} - Connected")
        else:
            self.conn_led.itemconfig(self.conn_led_id, fill="#dd2222")
            err = reading.get("error") or "Unknown error"
            self.conn_status_lbl.config(
                text=f"{DEVICE_IP} : Node {UNIT_ID} - Disconnected ({err})")

        # Only append data / write CSV when we actually got a good reading
        if connected and all(t is not None for t in temps):
            self.times.append(ts)
            for i in range(4):
                self.channel_data[i].append(temps[i])

            if self.recording and self.csv_writer:
                row = [ts.strftime("%Y-%m-%d %H:%M:%S")] + [f"{t:.1f}" for t in temps]
                self.csv_writer.writerow(row)
                self.csv_file.flush()

    def _redraw_plot(self):
        if not self.times:
            return
        x_vals = list(self.times)
        for i, line in enumerate(self.lines):
            line.set_data(x_vals, list(self.channel_data[i]))

        self.ax.relim()
        self.ax.autoscale_view()
        self.fig.autofmt_xdate(rotation=25)
        self.canvas.draw_idle()

    def _update_status_bar(self, reading):
        ts = reading["timestamp"].strftime("%H:%M:%S")
        if reading["connected"]:
            temps_txt = "  ".join(
                f"{CHANNEL_NAMES[i].split(' ')[1]}: {t:5.1f}\u00b0C"
                for i, t in enumerate(reading["temps"]) if t is not None
            )
            breaks = reading.get("breaks", [False] * 4)
            broken = [str(i + 1) for i, b in enumerate(breaks) if b]
            break_txt = f"   BREAK on Ch {', '.join(broken)}!" if broken else ""
            self.status_bar.config(text=f"[{ts}] {temps_txt}{break_txt}")
        else:
            self.status_bar.config(
                text=f"[{ts}] No data - {reading.get('error', 'connection lost')}")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close(self):
        self.worker.stop()
        if self.recording:
            self._stop_recording()
        self.root.after(300, self.root.destroy)


def main():
    root = tk.Tk()
    app = ThermocoupleApp(root)  # noqa: F841 - keep reference alive
    root.mainloop()


if __name__ == "__main__":
    main()

