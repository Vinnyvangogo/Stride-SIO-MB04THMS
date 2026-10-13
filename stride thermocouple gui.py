#!/usr/bin/env python3
"""
Stride Field I/O SIO-MB04THMS - 4-Channel Thermocouple Monitor
=================================================================
ver 6
Reads all 4 thermocouple channels from a Stride SIO-MB04THMS Modbus TCP
temperature module and displays them on a live-updating, dark-themed GUI.

Device register map (from the official SIO-MB04THMS User Guide):
    40041-40044   Analog Input Ch0-Ch3   (RO, signed 16-bit, x0.1 for TC)
    40050         Break Status bits      (RO, bit0=Ch0 ... bit3=Ch3)
    40031-40034   Input Type Ch0-Ch3     (R/W#!/usr/bin/env python3
"""
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
"""

import argparse
import csv
import ipaddress
import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    raise SystemExit(
        "pymodbus is required. Install it with:  pip install pymodbus"
    )

try:
    import pymodbus
    PYMODBUS_VERSION = getattr(pymodbus, "__version__", "unknown")
except Exception:
    PYMODBUS_VERSION = "unknown"

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
DEFAULT_DEVICE_IP = "192.168.1.10"   # used only if no address was saved yet
DEVICE_PORT = 502
UNIT_ID = 1                     # Modbus slave/node ID of the SIO-MB04THMS

CH_START_ADDR = 40              # 40041 -> zero-based address 40 (4 regs)
BREAK_ADDR = 49                 # 40050 -> zero-based address 49 (1 reg)
INPUT_TYPE_ADDR = 30            # 40031 -> zero-based address 30 (4 regs)

CHANNEL_NAMES = ["Channel 1 (Ch0)", "Channel 2 (Ch1)",
                  "Channel 3 (Ch2)", "Channel 4 (Ch3)"]
CHANNEL_COLORS = ["#ff5555", "#55ff88", "#5599ff", "#ffcc55"]

MAX_PLOT_POINTS = None          # None = unbounded; buffer auto-resets at
                                 # midnight so it only ever holds one day
DEFAULT_INTERVAL_S = 5
MIN_INTERVAL_S = 1
MAX_INTERVAL_S = 60
CSV_FILENAME_PREFIX = "thermocouple_log"

# Discovery settings
SCAN_CONNECT_TIMEOUT_S = 0.4    # TCP connect timeout per address while scanning
PROBE_TIMEOUT_S = 1.5           # Modbus timeout when verifying a responding host
SCAN_MAX_WORKERS = 64           # parallel connection attempts
SCAN_MAX_HOSTS = 4096           # refuse scan ranges larger than this
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".sio_mb04thms_monitor.json")

# Theme
BG = "#1e1e1e"
PANEL_BG = "#2a2a2a"
FG = "#e0e0e0"
ACCENT = "#3a7bd5"
OK_GREEN = "#33dd33"
FIELD_BG = "#333333"


def raw_to_signed16(val):
    """Convert an unsigned 16-bit Modbus register value to signed."""
    return val - 65536 if val > 32767 else val


# ----------------------------------------------------------------------
# Settings persistence (remembers the last device used)
# ----------------------------------------------------------------------
def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**kwargs):
    data = load_settings()
    data.update(kwargs)
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass  # non-fatal: we just won't remember the address


# ----------------------------------------------------------------------
# Version-agnostic Modbus read
# ----------------------------------------------------------------------
_read_strategy_idx = None


def modbus_read_holding(client, address, count, unit_id):
    """Different pymodbus releases (and even point releases within 3.x)
    have used different calling conventions for read_holding_registers -
    keyword-only 'slave', 'device_id', 'unit', or mixes of positional and
    keyword args. Try known conventions and lock onto the first one the
    installed version accepts (shared by the poller and the scanner)."""
    global _read_strategy_idx
    strategies = [
        lambda: client.read_holding_registers(address=address, count=count, slave=unit_id),
        lambda: client.read_holding_registers(address=address, count=count, device_id=unit_id),
        lambda: client.read_holding_registers(address=address, count=count, unit=unit_id),
        lambda: client.read_holding_registers(address, count=count, slave=unit_id),
        lambda: client.read_holding_registers(address, count, unit_id),
        lambda: client.read_holding_registers(address, count),
        lambda: client.read_holding_registers(address=address, count=count),
    ]

    idx = _read_strategy_idx
    if idx is not None:
        try:
            return strategies[idx]()
        except TypeError:
            pass  # fall through and re-probe

    last_exc = None
    for i, strategy in enumerate(strategies):
        try:
            result = strategy()
        except TypeError as exc:
            last_exc = exc
            continue
        _read_strategy_idx = i
        return result
    raise last_exc


# ----------------------------------------------------------------------
# Network discovery helpers
# ----------------------------------------------------------------------
def local_ipv4_addresses():
    """Best-effort list of this PC's IPv4 addresses (no packets are sent)."""
    addrs = set()
    for probe_target in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe_target, 1))   # UDP connect only picks a route
                addrs.add(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(
        (a for a in addrs
         if not a.startswith("127.") and not a.startswith("169.254.") and a != "0.0.0.0"),
        key=ipaddress.IPv4Address,
    )


def candidate_networks():
    """The /24 network around each local address - the usual LAN size."""
    nets = []
    for a in local_ipv4_addresses():
        net = ipaddress.ip_network(f"{a}/24", strict=False)
        if net not in nets:
            nets.append(net)
    return nets


def parse_scan_target(text):
    """Turn '192.168.1.0/24, 10.0.0.0/24, 172.16.5.20' into a list of host
    IP strings. Raises ValueError on bad input or an over-large range."""
    hosts, seen = [], set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            net = ipaddress.ip_network(part, strict=False)
            if net.version != 4:
                raise ValueError(f"{part} is not an IPv4 network")
            if net.num_addresses > SCAN_MAX_HOSTS + 2:
                raise ValueError(f"{part} is too large to scan "
                                 f"(limit is {SCAN_MAX_HOSTS} addresses)")
            new = [str(h) for h in net.hosts()] or [str(net.network_address)]
        else:
            new = [str(ipaddress.IPv4Address(part))]
        for h in new:
            if h not in seen:
                seen.add(h)
                hosts.append(h)
    if len(hosts) > SCAN_MAX_HOSTS:
        raise ValueError(f"Scan range covers {len(hosts)} addresses "
                         f"(limit is {SCAN_MAX_HOSTS})")
    return hosts


def tcp_port_open(ip, port, timeout):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_sio_device(ip, port, unit_id, timeout=PROBE_TIMEOUT_S):
    """Connect to ip:port and try the SIO-MB04THMS registers.
    Returns None if the address can't be reached at all, otherwise a dict:
        verified: True if every expected register read succeeded
        temps / breaks: current readings (when verified)
        detail: human-readable description"""
    client = ModbusTcpClient(ip, port=port, timeout=timeout)
    try:
        try:
            ok = client.connect()
        except Exception:
            ok = False
        if not ok:
            return None

        info = {"ip": ip, "port": port, "unit_id": unit_id, "verified": False,
                "temps": None, "breaks": None,
                "detail": f"Port {port} open - no Modbus reply"}
        try:
            rr = modbus_read_holding(client, CH_START_ADDR, 4, unit_id)
            br = modbus_read_holding(client, BREAK_ADDR, 1, unit_id)
            it = modbus_read_holding(client, INPUT_TYPE_ADDR, 4, unit_id)
        except Exception as exc:  # noqa: BLE001
            info["detail"] = f"Port {port} open - Modbus read failed ({exc.__class__.__name__})"
            return info

        responses = (rr, br, it)
        if any(r is None or r.isError() for r in responses) or \
                len(rr.registers) < 4 or len(br.registers) < 1 or len(it.registers) < 4:
            info["detail"] = "Modbus device - registers don't match SIO-MB04THMS"
            return info

        info["verified"] = True
        info["temps"] = [raw_to_signed16(v) / 10.0 for v in rr.registers[:4]]
        info["breaks"] = [bool(br.registers[0] & (1 << i)) for i in range(4)]
        info["detail"] = "Matches SIO-MB04THMS register map"
        return info
    finally:
        try:
            client.close()
        except Exception:
            pass


class NetworkScanner(threading.Thread):
    """Scans a list of IPs in parallel for Modbus/TCP, probing each host that
    answers. Reports via out_queue as tuples whose 2nd item is the scanner
    itself, so stale messages from a cancelled scan can be ignored:
        ("progress", scanner, done, total)
        ("found", scanner, info_dict)
        ("scan_done", scanner, found_count, cancelled)"""

    def __init__(self, hosts, port, unit_id, out_queue):
        super().__init__(daemon=True)
        self.hosts = hosts
        self.port = port
        self.unit_id = unit_id
        self.out_queue = out_queue
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def _check(self, ip):
        if self.cancel_event.is_set():
            return None
        if not tcp_port_open(ip, self.port, SCAN_CONNECT_TIMEOUT_S):
            return None
        if self.cancel_event.is_set():
            return None
        return probe_sio_device(ip, self.port, self.unit_id)

    def run(self):
        total = len(self.hosts)
        done = found = 0
        self.out_queue.put(("progress", self, 0, total))
        pool = ThreadPoolExecutor(max_workers=max(1, min(SCAN_MAX_WORKERS, total)))
        try:
            futures = [pool.submit(self._check, h) for h in self.hosts]
            for fut in as_completed(futures):
                done += 1
                try:
                    info = fut.result()
                except Exception:
                    info = None
                if info:
                    found += 1
                    self.out_queue.put(("found", self, info))
                if done % 4 == 0 or done == total:
                    self.out_queue.put(("progress", self, done, total))
                if self.cancel_event.is_set():
                    break
        finally:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # Python < 3.9
                pool.shutdown(wait=False)
        self.out_queue.put(("scan_done", self, found, self.cancel_event.is_set()))


# ----------------------------------------------------------------------
# Shared dark ttk styling
# ----------------------------------------------------------------------
def apply_dark_style(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL_BG)
    style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("Panel.TLabel", background=PANEL_BG, foreground=FG,
                    font=("Segoe UI", 10))
    style.configure("PanelHeader.TLabel", background=PANEL_BG, foreground=FG,
                    font=("Segoe UI", 11, "bold"))
    style.configure("Header.TLabel", background=BG, foreground=FG,
                    font=("Segoe UI", 12, "bold"))
    style.configure("TButton", font=("Segoe UI", 10), padding=6)
    style.configure("Start.TButton", foreground="#101010", background="#4caf50")
    style.configure("Stop.TButton", foreground="#101010", background="#e53935")
    style.map("Start.TButton", background=[("active", "#66bb6a")])
    style.map("Stop.TButton", background=[("active", "#ef5350")])
    style.configure("Horizontal.TScale", background=BG)
    style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG, insertcolor=FG)
    style.configure("TCombobox", fieldbackground=FIELD_BG, foreground=FG,
                    arrowcolor=FG, background=PANEL_BG)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD_BG)])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD_BG)
    style.configure("Treeview", background="#202020", fieldbackground="#202020",
                    foreground=FG, rowheight=22, font=("Segoe UI", 10))
    style.configure("Treeview.Heading", background="#333333", foreground=FG,
                    font=("Segoe UI", 10, "bold"))
    style.map("Treeview", background=[("selected", ACCENT)])


# ----------------------------------------------------------------------
# Device finder / manual-entry dialog
# ----------------------------------------------------------------------
class DeviceSelectDialog:
    """Modal window: searches for the SIO-MB04THMS and lets the user pick
    a result or type an IP by hand. After it closes, self.result is
    (ip, port, unit_id) or None if cancelled."""

    def __init__(self, parent, ip="", port=DEVICE_PORT, unit_id=UNIT_ID,
                 auto_search=True):
        self.parent = parent
        self.result = None
        self.scanner = None
        self.found = {}          # ip -> info dict
        self._closed = False
        self.msg_queue = queue.Queue()

        self.top = tk.Toplevel(parent)
        self.top.title("Find SIO-MB04THMS on the Network")
        self.top.configure(bg=BG)
        self.top.geometry("780x560")
        self.top.minsize(660, 480)
        if parent.winfo_viewable():
            self.top.transient(parent)
        self.top.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.ip_var = tk.StringVar(value=ip or "")
        self.port_var = tk.StringVar(value=str(port))
        self.unit_var = tk.StringVar(value=str(unit_id))
        self.status_var = tk.StringVar(value="")

        nets = candidate_networks()
        default_net = ipaddress.ip_network(f"{DEFAULT_DEVICE_IP}/24", strict=False)
        choices = [str(n) for n in nets]
        if len(nets) > 1:
            choices.insert(0, ", ".join(str(n) for n in nets))
        if str(default_net) not in choices:
            choices.append(str(default_net))
        self.target_var = tk.StringVar(value=choices[0])
        self._target_choices = choices

        self._build()
        self.top.after(100, self._poll)
        if auto_search:
            self.top.after(150, lambda: self._auto_search(ip))
        else:
            self.status_var.set("Click Scan to search, or enter the IP address below.")

        try:
            self.top.wait_visibility()
            self.top.grab_set()
        except tk.TclError:
            pass
        self.ip_entry.focus_set()

    # -------------------------- UI --------------------------
    def _build(self):
        # --- 1. Search ---
        search = ttk.Frame(self.top, style="Panel.TFrame", padding=10)
        search.pack(fill=tk.X, padx=10, pady=(10, 5))
        ttk.Label(search, text="1.  Search the network", style="PanelHeader.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(search, text="Scan range:", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.target_combo = ttk.Combobox(search, textvariable=self.target_var,
                                         values=self._target_choices, width=44)
        self.target_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        self.scan_btn = ttk.Button(search, text="Scan", width=10, command=self._toggle_scan)
        self.scan_btn.grid(row=1, column=2, pady=(8, 0))
        self.progress = ttk.Progressbar(search, mode="determinate",
                                        style="Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        ttk.Label(search, textvariable=self.status_var, style="Panel.TLabel",
                  wraplength=720, justify=tk.LEFT).grid(
            row=3, column=0, columnspan=3, sticky="w")
        search.columnconfigure(1, weight=1)

        # --- Results list ---
        results = ttk.Frame(self.top, style="TFrame")
        results.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        cols = ("ip", "status", "reading")
        self.tree = ttk.Treeview(results, columns=cols, show="headings",
                                 height=7, selectmode="browse")
        self.tree.heading("ip", text="IP Address")
        self.tree.heading("status", text="Status")
        self.tree.heading("reading", text="Current Reading (\u00b0C)")
        self.tree.column("ip", width=130, stretch=False)
        self.tree.column("status", width=300)
        self.tree.column("reading", width=300)
        self.tree.tag_configure("verified", foreground=OK_GREEN)
        self.tree.tag_configure("other", foreground="#999999")
        sb = ttk.Scrollbar(results, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _e: self._on_connect())

        # --- 2. Manual entry ---
        manual = ttk.Frame(self.top, style="Panel.TFrame", padding=10)
        manual.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(manual, text="2.  Or enter the unit's address manually",
                  style="PanelHeader.TLabel").grid(row=0, column=0, columnspan=7, sticky="w")
        ttk.Label(manual, text="IP Address:", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0))
        self.ip_entry = ttk.Entry(manual, textvariable=self.ip_var, width=18)
        self.ip_entry.grid(row=1, column=1, padx=(6, 14), pady=(8, 0))
        ttk.Label(manual, text="Port:", style="Panel.TLabel").grid(
            row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(manual, textvariable=self.port_var, width=7).grid(
            row=1, column=3, padx=(6, 14), pady=(8, 0))
        ttk.Label(manual, text="Node ID:", style="Panel.TLabel").grid(
            row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(manual, textvariable=self.unit_var, width=5).grid(
            row=1, column=5, padx=(6, 14), pady=(8, 0))
        self.test_btn = ttk.Button(manual, text="Test", width=8, command=self._on_test)
        self.test_btn.grid(row=1, column=6, pady=(8, 0))

        # --- Bottom buttons ---
        bottom = ttk.Frame(self.top, style="TFrame", padding=(10, 5, 10, 10))
        bottom.pack(fill=tk.X)
        ttk.Button(bottom, text="Cancel", command=self._on_cancel).pack(side=tk.RIGHT)
        self.connect_btn = ttk.Button(bottom, text="Connect", style="Start.TButton",
                                      command=self._on_connect)
        self.connect_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self.top.bind("<Return>", lambda _e: self._on_connect())
        self.top.bind("<Escape>", lambda _e: self._on_cancel())

    # ---------------------- input parsing -----------------------
    def _get_port_unit(self, show_errors=True):
        try:
            port = int(self.port_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            if show_errors:
                messagebox.showerror("Invalid Port", "Port must be a number from 1 to 65535 "
                                     "(Modbus/TCP default is 502).", parent=self.top)
            return None
        try:
            unit = int(self.unit_var.get().strip())
            if not 0 <= unit <= 247:
                raise ValueError
        except ValueError:
            if show_errors:
                messagebox.showerror("Invalid Node ID",
                                     "Node ID must be a number from 0 to 247.", parent=self.top)
            return None
        return port, unit

    def _get_ip(self, show_errors=True):
        ip = self.ip_var.get().strip()
        try:
            return str(ipaddress.IPv4Address(ip))
        except ValueError:
            if show_errors:
                messagebox.showerror(
                    "Invalid IP Address",
                    f"\"{ip}\" is not a valid IPv4 address.\n\nExample: 192.168.1.10",
                    parent=self.top)
                self.ip_entry.focus_set()
                self.ip_entry.select_range(0, tk.END)
            return None

    # ------------------------ searching -------------------------
    def _auto_search(self, last_ip):
        try:
            last_ip = str(ipaddress.IPv4Address((last_ip or "").strip()))
        except ValueError:
            last_ip = None
        if last_ip:
            self.status_var.set(f"Checking last-used address {last_ip}...")
            self._run_probe(last_ip, "auto")
        else:
            self._start_scan()

    def _toggle_scan(self):
        if self.scanner is not None:
            self.scanner.cancel()
            self.scan_btn.config(state=tk.DISABLED)
            self.status_var.set("Stopping scan...")
        else:
            self._start_scan()

    def _start_scan(self):
        if self.scanner is not None:
            return
        pu = self._get_port_unit()
        if pu is None:
            return
        target = self.target_var.get().strip()
        try:
            hosts = parse_scan_target(target)
        except ValueError as exc:
            self.status_var.set(f"Invalid scan range: {exc}")
            return
        if not hosts:
            self.status_var.set("No local network detected. Enter a scan range such as "
                                "192.168.1.0/24, or type the IP address below.")
            return
        port, unit = pu
        self.scanner = NetworkScanner(hosts, port, unit, self.msg_queue)
        self.progress.config(maximum=len(hosts), value=0)
        self.scan_btn.config(text="Stop", state=tk.NORMAL)
        self.status_var.set(f"Scanning {len(hosts)} addresses ({target}) "
                            f"for Modbus/TCP on port {port}...")
        self.scanner.start()

    def _run_probe(self, ip, purpose, port=None, unit=None):
        """Probe one address in the background. purpose is 'auto', 'test'
        or 'connect' and decides what happens with the result."""
        if port is None or unit is None:
            pu = self._get_port_unit(show_errors=False) or (DEVICE_PORT, UNIT_ID)
            port, unit = pu

        def worker():
            try:
                info = probe_sio_device(ip, port, unit)
            except Exception:  # noqa: BLE001
                info = None
            self.msg_queue.put(("probe_result", ip, port, unit, info, purpose))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------- result handling ---------------------
    def _add_result(self, info):
        ip = info["ip"]
        self.found[ip] = info
        if info["verified"]:
            parts = []
            for i, t in enumerate(info["temps"]):
                txt = f"Ch{i}: {t:.1f}"
                if info["breaks"] and info["breaks"][i]:
                    txt += " (open)"
                parts.append(txt)
            reading = "   ".join(parts)
        else:
            reading = "-"
        values = (ip, info["detail"], reading)
        tag = ("verified",) if info["verified"] else ("other",)
        if self.tree.exists(ip):
            self.tree.item(ip, values=values, tags=tag)
        else:
            # keep rows sorted numerically by IP
            key = ipaddress.IPv4Address(ip)
            index = 0
            for child in self.tree.get_children():
                if ipaddress.IPv4Address(child) < key:
                    index += 1
            self.tree.insert("", index, iid=ip, values=values, tags=tag)

    def _select_ip(self, ip):
        if self.tree.exists(ip):
            self.tree.selection_set(ip)
            self.tree.see(ip)
        self.ip_var.set(ip)

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if sel:
            self.ip_var.set(sel[0])

    def _on_scan_done(self, found_count, cancelled):
        self.scanner = None
        self.scan_btn.config(text="Scan", state=tk.NORMAL)
        verified = [i for i in self.found.values() if i["verified"]]
        others = [i for i in self.found.values() if not i["verified"]]
        if verified:
            if len(verified) == 1:
                self._select_ip(verified[0]["ip"])
                self.status_var.set(f"Found SIO-MB04THMS at {verified[0]['ip']}. "
                                    "Click Connect.")
            else:
                self.status_var.set(f"Found {len(verified)} matching units. Select one "
                                    "(check the readings to tell them apart) and click Connect.")
        elif cancelled:
            self.status_var.set("Scan stopped. Select a result, scan again, or enter "
                                "the IP address manually below.")
        elif others:
            self.status_var.set(
                f"No SIO-MB04THMS confirmed. {len(others)} Modbus/TCP host(s) answered but "
                "didn't match (check the Node ID). Select one, or enter the IP address manually below.")
        else:
            self.status_var.set(
                "No SIO-MB04THMS found. Enter the unit's IP address manually below and click "
                "Test or Connect. If the unit is on a different subnet than this PC, either add "
                "that subnet to the scan range (the PC must be able to route to it) or give the "
                "PC an address on the unit's subnet.")
            self.ip_entry.focus_set()
            self.ip_entry.select_range(0, tk.END)

    def _on_probe_result(self, ip, port, unit, info, purpose):
        if info:
            self._add_result(info)

        if purpose == "auto":
            if info and info["verified"]:
                self._select_ip(ip)
                self.status_var.set(f"Found SIO-MB04THMS at last-used address {ip}. "
                                    "Click Connect, or Scan to look for other units.")
            else:
                self.status_var.set(f"Last-used address {ip} did not respond as an "
                                    "SIO-MB04THMS - scanning the network...")
                self._start_scan()
                if self.scanner is not None:
                    self.status_var.set(f"{ip} did not respond - " + self.status_var.get())

        elif purpose == "test":
            self.test_btn.config(state=tk.NORMAL)
            if info and info["verified"]:
                self._select_ip(ip)
                temps = ", ".join(f"{t:.1f}" for t in info["temps"])
                self.status_var.set(f"Test OK - SIO-MB04THMS at {ip}:{port}, node {unit} "
                                    f"(readings: {temps} \u00b0C).")
            elif info:
                self.status_var.set(f"Test: {ip}:{port} answered, but {info['detail'].lower()}. "
                                    "Check the Node ID.")
            else:
                self.status_var.set(f"Test failed - nothing answered at {ip}:{port}. Check the "
                                    "address, cabling, and that the PC is on the same subnet.")

        elif purpose == "connect":
            self._set_busy(False)
            if info and info["verified"]:
                self._finish(ip, port, unit)
                return
            if info:
                msg = (f"{ip}:{port} answered but did not match the SIO-MB04THMS register "
                       f"map ({info['detail']}).\n\nConnect anyway?")
            else:
                msg = (f"Could not reach {ip}:{port} right now.\n\nConnect anyway? The monitor "
                       "will keep retrying, and you can pick a different device later with "
                       "\"Change Device...\".")
            if messagebox.askyesno("Device Not Verified", msg, parent=self.top):
                self._finish(ip, port, unit)
            else:
                self.status_var.set("Connection cancelled. Check the address and try again.")

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind in ("progress", "found", "scan_done") and msg[1] is not self.scanner:
                    continue  # stale message from an earlier scan
                if kind == "progress":
                    self.progress.config(maximum=msg[3], value=msg[2])
                elif kind == "found":
                    self._add_result(msg[2])
                elif kind == "scan_done":
                    self._on_scan_done(msg[2], msg[3])
                elif kind == "probe_result":
                    self._on_probe_result(*msg[1:])
        except queue.Empty:
            pass
        self.top.after(100, self._poll)

    # ------------------------- buttons --------------------------
    def _on_test(self):
        ip = self._get_ip()
        pu = self._get_port_unit()
        if ip is None or pu is None:
            return
        self.test_btn.config(state=tk.DISABLED)
        self.status_var.set(f"Testing {ip}:{pu[0]} (node {pu[1]})...")
        self._run_probe(ip, "test", *pu)

    def _on_connect(self):
        if str(self.connect_btn.cget("state")) == tk.DISABLED:
            return
        ip = self._get_ip()
        pu = self._get_port_unit()
        if ip is None or pu is None:
            return
        port, unit = pu
        info = self.found.get(ip)
        if info and info["verified"] and info["port"] == port and info["unit_id"] == unit:
            self._finish(ip, port, unit)
            return
        self._set_busy(True)
        self.status_var.set(f"Verifying {ip}:{port} (node {unit})...")
        self._run_probe(ip, "connect", port, unit)

    def _set_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        self.connect_btn.config(state=state)
        self.test_btn.config(state=state)

    def _on_cancel(self):
        self.result = None
        self._close()

    def _finish(self, ip, port, unit):
        self.result = (ip, port, unit)
        self._close()

    def _close(self):
        self._closed = True
        if self.scanner is not None:
            self.scanner.cancel()
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()


# ----------------------------------------------------------------------
# Background Modbus polling worker
# ----------------------------------------------------------------------
class ModbusWorker(threading.Thread):
    """Polls the SIO-MB04THMS at a configurable interval and pushes
    results onto a thread-safe queue for the GUI thread to consume.
    'generation' tags each reading so that, after switching devices,
    late readings from the old worker can be ignored."""

    def __init__(self, ip, port, unit_id, out_queue, generation=0):
        super().__init__(daemon=True)
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self.out_queue = out_queue
        self.generation = generation

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
                    rr = modbus_read_holding(self.client, CH_START_ADDR, 4, self.unit_id)
                    br = modbus_read_holding(self.client, BREAK_ADDR, 1, self.unit_id)
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

            if self._stop_event.is_set():
                break

            self.out_queue.put({
                "gen": self.generation,
                "timestamp": datetime.now(),
                "connected": connected,
                "temps": temps,
                "breaks": breaks,
                "error": error_msg,
            })

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
    BG = BG
    PANEL_BG = PANEL_BG
    FG = FG
    ACCENT = ACCENT

    def __init__(self, root, device_ip, device_port=DEVICE_PORT, unit_id=UNIT_ID):
        self.root = root
        self.device_ip = device_ip
        self.device_port = device_port
        self.unit_id = unit_id
        self._update_title()
        self.root.geometry("1150x720")
        self.root.configure(bg=self.BG)

        self.data_queue = queue.Queue()
        self.worker = None
        self.worker_gen = 0

        # Rolling buffers for plotting - hold one full day, reset at midnight
        self.times = deque(maxlen=MAX_PLOT_POINTS)
        self.channel_data = [deque(maxlen=MAX_PLOT_POINTS) for _ in range(4)]
        self.channel_visible = [True, True, True, True]
        self.current_day = date.today()

        # CSV recording state
        self.recording = False
        self.csv_file = None
        self.csv_writer = None
        self.record_dir = None

        apply_dark_style(self.root)
        self._build_layout()
        self._build_plot()

        self._start_worker()
        self.root.after(200, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _device_text(self):
        port = "" if self.device_port == DEVICE_PORT else f":{self.device_port}"
        return f"{self.device_ip}{port} : Node {self.unit_id}"

    def _update_title(self):
        self.root.title(f"Stride SIO-MB04THMS - Thermocouple Monitor  [{self.device_ip}]  "
                        f"(pymodbus {PYMODBUS_VERSION})")

    def _start_worker(self):
        self.worker_gen += 1
        self.worker = ModbusWorker(self.device_ip, self.device_port, self.unit_id,
                                   self.data_queue, self.worker_gen)
        self.worker.set_interval(int(round(self.interval_var.get())))
        self.worker.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
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
        ttk.Button(conn_frame, text="Change Device...", command=self._change_device).grid(
            row=0, column=2, padx=(12, 0))
        self.conn_status_lbl = ttk.Label(conn_frame, text=f"{self._device_text()} - Connecting...",
                                          style="Panel.TLabel")
        self.conn_status_lbl.grid(row=1, column=0, columnspan=3, sticky="w")

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
        self.ax.set_title(f"SIO-MB04THMS Live Thermocouple Readings - {self.current_day:%Y-%m-%d}")
        self.ax.grid(True, color="#3a3a3a", linestyle="--", linewidth=0.5)

        self.ax.xaxis_date()
        self.ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        for label in self.ax.get_xticklabels():
            label.set_rotation(25)
            label.set_ha("right")

        self.lines = []
        for i in range(4):
            (line,) = self.ax.plot([], [], label=CHANNEL_NAMES[i],
                                    color=CHANNEL_COLORS[i], linewidth=1.6)
            self.lines.append(line)

        self.legend = self.ax.legend(loc="upper left", facecolor="#222222",
                                      edgecolor="#444444", labelcolor=self.FG)
        self.legend_map = {}
        for leg_line, orig_line in zip(self.legend.get_lines(), self.lines):
            leg_line.set_picker(8)
            self.legend_map[leg_line] = orig_line

        self.fig.subplots_adjust(bottom=0.22, left=0.08, right=0.98, top=0.92)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_container)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("pick_event", self._on_legend_pick)
        self.canvas.draw()

    # ------------------------------------------------------------------
    # Change device (re-run discovery / manual entry)
    # ------------------------------------------------------------------
    def _change_device(self):
        dlg = DeviceSelectDialog(self.root, ip=self.device_ip, port=self.device_port,
                                 unit_id=self.unit_id, auto_search=True)
        self.root.wait_window(dlg.top)
        if not dlg.result:
            return
        self.worker.stop()
        self.device_ip, self.device_port, self.unit_id = dlg.result
        save_settings(ip=self.device_ip, port=self.device_port, unit_id=self.unit_id)
        self._update_title()
        self.conn_led.itemconfig(self.conn_led_id, fill="#772222")
        self.conn_status_lbl.config(text=f"{self._device_text()} - Connecting...")
        self.status_bar.config(text=f"Switched to {self.device_ip} - connecting...")
        self._start_worker()

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
        if self.worker is not None:
            self.worker.set_interval(seconds)

    # ------------------------------------------------------------------
    # CSV Recording
    # ------------------------------------------------------------------
    def _dated_csv_path(self, for_date):
        filename = f"{CSV_FILENAME_PREFIX}_{for_date:%Y-%m-%d}.csv"
        return os.path.join(self.record_dir, filename)

    def _open_csv_for_day(self, for_date):
        path = self._dated_csv_path(for_date)
        self.csv_file = open(path, "a", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        if self.csv_file.tell() == 0:
            self.csv_writer.writerow(["Timestamp", "Channel1_C", "Channel2_C",
                                       "Channel3_C", "Channel4_C"])
        self.csv_file.flush()
        self.rec_status_lbl.config(text=f"Recording: {os.path.basename(path)}")

    def _start_recording(self):
        directory = filedialog.askdirectory(
            title="Choose folder to save daily temperature logs in"
        )
        if not directory:
            return
        self.record_dir = directory

        try:
            self._open_csv_for_day(self.current_day)
        except OSError as exc:
            messagebox.showerror("File Error", f"Could not open file:\n{exc}")
            self.record_dir = None
            return

        self.recording = True
        self.rec_led.itemconfig(self.rec_led_id, fill=OK_GREEN)
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
        latest = None
        try:
            while True:
                reading = self.data_queue.get_nowait()
                if reading.get("gen") != self.worker_gen:
                    continue  # late reading from a previous device
                self._handle_reading(reading)
                latest = reading
        except queue.Empty:
            pass

        if latest is not None:
            self._redraw_plot()
            self._update_status_bar(latest)

        self.root.after(200, self._poll_queue)

    def _handle_reading(self, reading):
        connected = reading["connected"]
        temps = reading["temps"]
        ts = reading["timestamp"]

        if ts.date() != self.current_day:
            self._rollover_day(ts.date())

        if connected:
            self.conn_led.itemconfig(self.conn_led_id, fill=OK_GREEN)
            self.conn_status_lbl.config(text=f"{self._device_text()} - Connected")
        else:
            self.conn_led.itemconfig(self.conn_led_id, fill="#dd2222")
            err = reading.get("error") or "Unknown error"
            self.conn_status_lbl.config(
                text=f"{self._device_text()} - Disconnected ({err})")

        if connected and all(t is not None for t in temps):
            self.times.append(ts)
            for i in range(4):
                self.channel_data[i].append(temps[i])

            if self.recording and self.csv_writer:
                row = [ts.strftime("%Y-%m-%d %H:%M:%S")] + [f"{t:.1f}" for t in temps]
                self.csv_writer.writerow(row)
                self.csv_file.flush()

    def _rollover_day(self, new_day):
        self.current_day = new_day

        self.times.clear()
        for dq in self.channel_data:
            dq.clear()
        for line in self.lines:
            line.set_data([], [])
        self.ax.set_title(f"SIO-MB04THMS Live Thermocouple Readings - {new_day:%Y-%m-%d}")
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

        if self.recording:
            if self.csv_file:
                try:
                    self.csv_file.close()
                except Exception:
                    pass
            try:
                self._open_csv_for_day(new_day)
            except OSError as exc:
                messagebox.showerror(
                    "File Error",
                    f"Could not open new day's log file:\n{exc}\n\nRecording stopped."
                )
                self._stop_recording()

    def _redraw_plot(self):
        if not self.times:
            return
        x_vals = mdates.date2num(list(self.times))
        for i, line in enumerate(self.lines):
            line.set_data(x_vals, list(self.channel_data[i]))

        self.ax.relim()
        self.ax.autoscale_view()
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
        if self.worker is not None:
            self.worker.stop()
        if self.recording:
            self._stop_recording()
        self.root.after(300, self.root.destroy)


def main():
    parser = argparse.ArgumentParser(description="Stride SIO-MB04THMS thermocouple monitor")
    parser.add_argument("--ip", help="connect directly to this IP (skips the device finder)")
    parser.add_argument("--port", type=int, help=f"Modbus/TCP port (default {DEVICE_PORT})")
    parser.add_argument("--unit", type=int, help=f"Modbus node ID (default {UNIT_ID})")
    args = parser.parse_args()

    settings = load_settings()
    port = args.port or settings.get("port", DEVICE_PORT)
    unit = args.unit if args.unit is not None else settings.get("unit_id", UNIT_ID)

    root = tk.Tk()
    root.withdraw()
    apply_dark_style(root)

    if args.ip:
        try:
            ip = str(ipaddress.IPv4Address(args.ip.strip()))
        except ValueError:
            parser.error(f"invalid IP address: {args.ip}")
    else:
        dlg = DeviceSelectDialog(root, ip=settings.get("ip", DEFAULT_DEVICE_IP),
                                 port=port, unit_id=unit)
        root.wait_window(dlg.top)
        if dlg.result is None:
            root.destroy()
            return
        ip, port, unit = dlg.result

    save_settings(ip=ip, port=port, unit_id=unit)
    root.deiconify()
    app = ThermocoupleApp(root, ip, port, unit)  # noqa: F841 - keep reference alive
    root.mainloop()


if __name__ == "__main__":
    main()
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
"""

import csv
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime, date
from tkinter import filedialog, messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    raise SystemExit(
        "pymodbus is required. Install it with:  pip install pymodbus"
    )

try:
    import pymodbus
    PYMODBUS_VERSION = getattr(pymodbus, "__version__", "unknown")
except Exception:
    PYMODBUS_VERSION = "unknown"

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

MAX_PLOT_POINTS = None          # None = unbounded; buffer auto-resets at
                                 # midnight so it only ever holds one day
DEFAULT_INTERVAL_S = 5
MIN_INTERVAL_S = 1
MAX_INTERVAL_S = 60
CSV_FILENAME_PREFIX = "thermocouple_log"


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
        self._id_kwarg = None  # resolved lazily to the working strategy index

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

    def _read_holding_registers(self, address, count):
        """Version-agnostic wrapper. Different pymodbus releases (and even
        different point releases within 3.x) have used different calling
        conventions for read_holding_registers - keyword-only 'slave',
        keyword-only 'device_id', keyword-only 'unit', or various mixes of
        positional/keyword args. Rather than guess once, try a list of
        known-good calling conventions and lock onto whichever one this
        installed version actually accepts."""
        uid = self.unit_id
        strategies = [
            lambda: self.client.read_holding_registers(address=address, count=count, slave=uid),
            lambda: self.client.read_holding_registers(address=address, count=count, device_id=uid),
            lambda: self.client.read_holding_registers(address=address, count=count, unit=uid),
            lambda: self.client.read_holding_registers(address, count=count, slave=uid),
            lambda: self.client.read_holding_registers(address, count, uid),
            lambda: self.client.read_holding_registers(address, count),
            lambda: self.client.read_holding_registers(address=address, count=count),
        ]

        # If a strategy has already proven to work, use it directly first.
        if self._id_kwarg is not None:
            try:
                return strategies[self._id_kwarg]()
            except TypeError:
                pass  # fall through and re-probe below

        last_exc = None
        for idx, strategy in enumerate(strategies):
            try:
                result = strategy()
            except TypeError as exc:
                last_exc = exc
                continue
            self._id_kwarg = idx
            return result

        raise last_exc

    def run(self):
        while not self._stop_event.is_set():
            start_time = time.time()
            connected = False
            temps = [None, None, None, None]
            breaks = [False, False, False, False]
            error_msg = None

            try:
                if self._ensure_connected():
                    rr = self._read_holding_registers(CH_START_ADDR, 4)
                    br = self._read_holding_registers(BREAK_ADDR, 1)
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
        self.root.title(f"Stride SIO-MB04THMS - Thermocouple Monitor  (pymodbus {PYMODBUS_VERSION})")
        self.root.geometry("1150x720")
        self.root.configure(bg=self.BG)

        self.data_queue = queue.Queue()
        self.worker = ModbusWorker(DEVICE_IP, DEVICE_PORT, UNIT_ID, self.data_queue)

        # Rolling buffers for plotting - hold one full day, reset at midnight
        self.times = deque(maxlen=MAX_PLOT_POINTS)
        self.channel_data = [deque(maxlen=MAX_PLOT_POINTS) for _ in range(4)]
        self.channel_visible = [True, True, True, True]
        self.current_day = date.today()

        # CSV recording state
        self.recording = False
        self.csv_file = None
        self.csv_writer = None
        self.record_dir = None   # directory chosen when recording started;
                                  # daily rollover files are written here

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
        self.ax.set_title(f"SIO-MB04THMS Live Thermocouple Readings - {self.current_day:%Y-%m-%d}")
        self.ax.grid(True, color="#3a3a3a", linestyle="--", linewidth=0.5)

        # Explicit date handling on the x-axis so real clock times are shown
        # (rather than matplotlib's default numeric/ordinal tick labels).
        self.ax.xaxis_date()
        self.ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        for label in self.ax.get_xticklabels():
            label.set_rotation(25)
            label.set_ha("right")

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

        self.fig.subplots_adjust(bottom=0.22, left=0.08, right=0.98, top=0.92)

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
    def _dated_csv_path(self, for_date):
        """Build the log file path for a given date inside the chosen
        recording directory, e.g. thermocouple_log_2026-08-05.csv"""
        filename = f"{CSV_FILENAME_PREFIX}_{for_date:%Y-%m-%d}.csv"
        return os.path.join(self.record_dir, filename)

    def _open_csv_for_day(self, for_date):
        """(Re)open the CSV writer pointed at the log file for the given
        day, writing the header row. Used both on Start Recording and on
        the automatic midnight rollover."""
        path = self._dated_csv_path(for_date)
        self.csv_file = open(path, "a", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        # Only write the header if this is a brand-new (empty) file, so
        # resuming recording later in the same day doesn't duplicate it.
        if self.csv_file.tell() == 0:
            self.csv_writer.writerow(["Timestamp", "Channel1_C", "Channel2_C",
                                       "Channel3_C", "Channel4_C"])
        self.csv_file.flush()
        self.rec_status_lbl.config(text=f"Recording: {os.path.basename(path)}")

    def _start_recording(self):
        directory = filedialog.askdirectory(
            title="Choose folder to save daily temperature logs in"
        )
        if not directory:
            return
        self.record_dir = directory

        try:
            self._open_csv_for_day(self.current_day)
        except OSError as exc:
            messagebox.showerror("File Error", f"Could not open file:\n{exc}")
            self.record_dir = None
            return

        self.recording = True
        self.rec_led.itemconfig(self.rec_led_id, fill="#33dd33")
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

        # Detect the calendar day rolling over (checked on every reading,
        # even if this particular poll failed to connect, since the
        # worker always timestamps its result with the current time).
        if ts.date() != self.current_day:
            self._rollover_day(ts.date())

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

    def _rollover_day(self, new_day):
        """Called automatically the first time a reading's timestamp
        crosses into a new calendar day (i.e. at/after midnight). Clears
        the plot so the graph starts fresh for the new day, and - if a
        recording is in progress - closes the previous day's CSV file and
        opens a new dated one so each day gets its own file."""
        self.current_day = new_day

        # Clear plotted data/buffers for the new day.
        self.times.clear()
        for dq in self.channel_data:
            dq.clear()
        for line in self.lines:
            line.set_data([], [])
        self.ax.set_title(f"SIO-MB04THMS Live Thermocouple Readings - {new_day:%Y-%m-%d}")
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

        # Roll the CSV file over too, if we're currently recording.
        if self.recording:
            if self.csv_file:
                try:
                    self.csv_file.close()
                except Exception:
                    pass
            try:
                self._open_csv_for_day(new_day)
            except OSError as exc:
                messagebox.showerror(
                    "File Error",
                    f"Could not open new day's log file:\n{exc}\n\nRecording stopped."
                )
                self._stop_recording()

    def _redraw_plot(self):
        if not self.times:
            return
        x_vals = mdates.date2num(list(self.times))
        for i, line in enumerate(self.lines):
            line.set_data(x_vals, list(self.channel_data[i]))

        self.ax.relim()
        self.ax.autoscale_view()
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
