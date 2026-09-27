"""Tkinter GUI for exporting RC-505 loop memory stems into DAW-ready folders."""

from __future__ import annotations

import math
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import rc505

try:
    import player
except Exception as exc:  # numpy/sounddevice missing, or no PortAudio library
    player = None
    PREVIEW_ERROR = f"Preview unavailable ({exc}). Run: pip install -r requirements.txt"
else:
    PREVIEW_ERROR = ""

CHECKED, UNCHECKED = "\u2611", "\u2610"
TRACK_COUNT = 5
POLL_MS = 50


class ExportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RC-505 USB Storage Export Tool")
        self.geometry("880x820")
        self.minsize(760, 660)

        self.memories: list[rc505.Memory] = []
        self.checked: set[int] = set()  # memory numbers
        self.events: queue.Queue = queue.Queue()
        self.cancel_requested = False
        self.worker: threading.Thread | None = None

        self.player = player.LoopPlayer() if player else None
        self.loaded_memory: rc505.Memory | None = None  # audio currently in the player
        self.muted_tracks: dict[int, set[int]] = {}  # memory number -> tracks left out of preview and mixdown
        self.track_gains: dict[int, dict[int, float]] = {}  # memory number -> {track: gain} for preview and mixdown
        self.last_export_dir: Path | None = None
        self.load_token = 0  # ignores stale loads when the user switches memories quickly
        self.devices: list[tuple[int, str]] = []

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=str(Path.home() / "Music"))
        self.bundle_var = tk.StringVar(value=self.default_bundle_name())
        self.hide_empty_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Click Browse... to select the RC-505's ROLAND folder.")
        self.preview_var = tk.StringVar(value=PREVIEW_ERROR or "Select a memory, then press Play (or double-click it).")
        self.device_var = tk.StringVar()
        self.volume_var = tk.DoubleVar(value=80)
        self.track_vars = [tk.BooleanVar(value=True) for _ in range(TRACK_COUNT)]
        self.gain_vars = [tk.DoubleVar(value=100) for _ in range(TRACK_COUNT)]  # percent
        self.repeats_var = tk.IntVar(value=1)
        self.export_mode_var = tk.StringVar(value="stems")  # "stems" | "mixdown" | "both"
        self.mono_var = tk.BooleanVar(value=False)
        self.balance_var = tk.BooleanVar(value=False)
        self.loaded_balance: dict[int, float] = {}  # auto-balance gains of the memory in the player
        self.mix_hint_var = tk.StringVar()

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(POLL_MS, self.poll_events)

    # ---------- layout ----------

    def build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        source = ttk.LabelFrame(self, text="1. RC-505 storage (ROLAND folder)")
        source.pack(fill="x", **pad)
        ttk.Entry(source, textvariable=self.source_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6, pady=6
        )
        ttk.Button(source, text="Rescan", command=self.rescan).pack(side="right", padx=(0, 6))
        ttk.Button(source, text="Browse...", command=self.browse_source).pack(side="right", padx=6)

        memories = ttk.LabelFrame(
            self, text="2. Loop memories to export (tick \u2610 to export, double-click to preview)"
        )
        memories.pack(fill="both", expand=True, **pad)

        toolbar = ttk.Frame(memories)
        toolbar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(toolbar, text="Select all", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(toolbar, text="Select none", command=lambda: self.set_all(False)).pack(side="left", padx=6)
        ttk.Checkbutton(
            toolbar, text="Hide empty memories", variable=self.hide_empty_var, command=self.refresh_tree
        ).pack(side="left", padx=6)
        self.selection_label = ttk.Label(toolbar, text="")
        self.selection_label.pack(side="right")

        columns = ("check", "memory", "name", "bpm", "tracks", "length", "size")
        tree_frame = ttk.Frame(memories)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "check": ("", 36, "center"),
            "memory": ("Memory", 70, "center"),
            "name": ("Name", 160, "w"),
            "bpm": ("BPM", 60, "center"),
            "tracks": ("Tracks  —  (n) muted, n* level set", 200, "center"),
            "length": ("Longest", 70, "center"),
            "size": ("Size", 80, "e"),
        }
        for col, (text, width, anchor) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        self.tree.tag_configure("empty", foreground="gray")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-Button-1>", self.on_tree_double_click)
        self.tree.bind("<space>", self.on_tree_space)
        self.tree.bind("<Return>", lambda _e: self.play_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_preview_label())

        self.build_preview()

        dest = ttk.LabelFrame(self, text="3. Export")
        dest.pack(fill="x", **pad)
        dest.columnconfigure(1, weight=1)

        ttk.Label(dest, text="Export as:").grid(row=0, column=0, sticky="nw", padx=6, pady=4)
        modes = ttk.Frame(dest)
        modes.grid(row=0, column=1, columnspan=2, sticky="w", pady=4)
        self.mode_buttons = {}
        for row, (value, text) in enumerate(
            (
                ("stems", "Stems — one WAV per track, in a folder per memory (for your DAW)"),
                ("mixdown", "Mixdown — one MP3 per memory with its tracks mixed (for listening/sharing)"),
                ("both", "Both"),
            )
        ):
            button = ttk.Radiobutton(
                modes, text=text, value=value, variable=self.export_mode_var, command=self.on_export_mode_changed
            )
            button.grid(row=row, column=0, sticky="w")
            self.mode_buttons[value] = button
        cycles = ttk.Frame(modes)
        cycles.grid(row=3, column=0, sticky="w", padx=(22, 0), pady=(2, 0))
        ttk.Label(cycles, text="Mixdown length:").pack(side="left")
        self.repeats_spin = ttk.Spinbox(
            cycles, from_=1, to=16, width=4, textvariable=self.repeats_var, state="disabled"
        )
        self.repeats_spin.pack(side="left", padx=4)
        ttk.Label(cycles, text="full loop cycle(s). Uses each memory's Preview mutes and levels.").pack(side="left")

        options = ttk.Frame(modes)
        options.grid(row=4, column=0, sticky="w", padx=(22, 0), pady=(2, 0))
        self.mono_check = ttk.Checkbutton(
            options, text="Mono", variable=self.mono_var, command=self.apply_mix_options, state="disabled"
        )
        self.mono_check.pack(side="left")
        self.balance_check = ttk.Checkbutton(
            options,
            text="Auto-balance layers & normalize",
            variable=self.balance_var,
            command=self.apply_mix_options,
            state="disabled",
        )
        self.balance_check.pack(side="left", padx=(12, 0))
        ttk.Label(options, text="(the Preview plays with these too)", foreground="gray").pack(side="left", padx=(8, 0))
        if player is None:
            for value in ("mixdown", "both"):
                self.mode_buttons[value].config(state="disabled")

        ttk.Label(dest, text="Save in:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(dest, textvariable=self.dest_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(dest, text="Browse...", command=self.browse_dest).grid(row=1, column=2, padx=6)
        ttk.Label(dest, text="Bundle folder name:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(dest, textvariable=self.bundle_var).grid(row=2, column=1, sticky="ew", pady=4)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        self.export_button = ttk.Button(bottom, text="Export", command=self.start_export)
        self.export_button.pack(side="right")
        self.export_to_button = ttk.Button(bottom, text="Export to...", command=self.export_to)
        self.export_to_button.pack(side="right", padx=(0, 6))
        self.cancel_button = ttk.Button(bottom, text="Cancel", command=self.request_cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text="Open folder", command=self.open_export_folder).pack(side="right", padx=(0, 12))

    def build_preview(self) -> None:
        preview = ttk.LabelFrame(self, text="Preview")
        preview.pack(fill="x", padx=8, pady=4)
        preview.columnconfigure(1, weight=1)

        self.play_button = ttk.Button(preview, text="\u25b6 Play", width=10, command=self.toggle_playback)
        self.play_button.grid(row=0, column=0, padx=6, pady=(6, 2), sticky="w")
        ttk.Label(preview, textvariable=self.preview_var).grid(row=0, column=1, sticky="w")
        device_frame = ttk.Frame(preview)
        device_frame.grid(row=0, column=2, padx=6, sticky="e")
        ttk.Label(device_frame, text="Output:").pack(side="left")
        self.device_combo = ttk.Combobox(device_frame, textvariable=self.device_var, state="readonly", width=32)
        self.device_combo.pack(side="left", padx=(4, 0))
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)

        controls = ttk.Frame(preview)
        controls.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=2)
        self.track_buttons, self.gain_scales, self.gain_labels = [], [], []
        for i, (on_var, gain_var) in enumerate(zip(self.track_vars, self.gain_vars), start=1):
            strip = ttk.Frame(controls)
            strip.pack(side="left", padx=(0, 14))
            button = ttk.Checkbutton(
                strip, text=f"Track {i}", variable=on_var, command=lambda n=i: self.on_track_toggled(n), state="disabled"
            )
            button.grid(row=0, column=0, columnspan=2, sticky="w")
            scale = ttk.Scale(
                strip, from_=0, to=150, variable=gain_var, length=90, state="disabled",
                command=lambda _v, n=i: self.on_gain_changed(n),
            )
            scale.grid(row=1, column=0)
            scale.bind("<Double-Button-1>", lambda _e, n=i: self.reset_gain(n))
            label = ttk.Label(strip, text="100%", width=5, anchor="e")
            label.grid(row=1, column=1)
            self.track_buttons.append(button)
            self.gain_scales.append(scale)
            self.gain_labels.append(label)

        master = ttk.Frame(controls)
        master.pack(side="right")
        self.reset_levels_button = ttk.Button(master, text="Reset levels", command=self.reset_levels, state="disabled")
        self.reset_levels_button.grid(row=0, column=0, columnspan=2, sticky="e")
        ttk.Label(master, text="Listening volume").grid(row=1, column=0, padx=(0, 4))
        self.volume_scale = ttk.Scale(
            master, from_=0, to=100, variable=self.volume_var, command=self.on_volume_changed, length=110
        )
        self.volume_scale.grid(row=1, column=1)

        ttk.Label(preview, textvariable=self.mix_hint_var, foreground="gray").grid(
            row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6)
        )
        self.update_mix_hint()

        self.playhead = ttk.Progressbar(preview, mode="determinate", maximum=1000)
        self.playhead.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(4, 2))

        if self.player is None:
            for widget in (self.play_button, self.device_combo, self.volume_scale):
                widget.config(state="disabled")
            return
        try:
            self.devices = player.output_devices()
        except Exception as exc:
            self.preview_var.set(f"No audio output available: {exc}")
            self.play_button.config(state="disabled")
            return
        self.device_combo["values"] = [name for _, name in self.devices]
        default = player.default_output_device()
        for index, name in self.devices:
            if index == default:
                self.device_var.set(name)
                self.player.device = index
                break

    @staticmethod
    def default_bundle_name() -> str:
        return f"RC-505 Export {datetime.now():%Y-%m-%d %H%M}"

    # ---------- source / scanning ----------

    def browse_source(self) -> None:
        initial = self.source_var.get() or ("D:\\" if os.path.isdir("D:\\") else str(Path.home()))
        chosen = filedialog.askdirectory(
            parent=self, title="Select the RC-505 ROLAND folder (e.g. D:\\ROLAND)", initialdir=initial
        )
        if chosen:
            self.load_source(chosen)

    def rescan(self) -> None:
        if self.source_var.get():
            self.load_source(self.source_var.get())

    def load_source(self, path: str) -> None:
        try:
            root = rc505.find_roland_root(path)
            memories = rc505.scan(root)
        except (rc505.RC505Error, OSError) as exc:
            messagebox.showerror("Not an RC-505 folder", str(exc), parent=self)
            return
        self.unload_preview()
        self.memories = memories
        self.source_var.set(str(root))
        available = {m.number for m in self.memories if m.tracks}
        self.checked &= available
        self.refresh_tree()
        self.status_var.set(f"Found {len(available)} memories with recordings out of {len(self.memories)}.")

    def refresh_tree(self) -> None:
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for memory in self.memories:
            if self.hide_empty_var.get() and not memory.tracks:
                continue
            self.tree.insert(
                "",
                "end",
                iid=str(memory.number),
                values=(
                    CHECKED if memory.number in self.checked else UNCHECKED,
                    memory.label,
                    memory.name,
                    f"{memory.tempo:g}" if memory.tempo and memory.tracks else "",
                    self.tracks_cell(memory),
                    rc505.format_duration(memory.longest_duration) if memory.tracks else "",
                    rc505.format_size(memory.total_size) if memory.tracks else "empty",
                ),
                tags=() if memory.tracks else ("empty",),
            )
        still_there = [iid for iid in selected if self.tree.exists(iid)]
        if still_there:
            self.tree.selection_set(still_there)
        self.update_selection_label()
        self.update_preview_label()

    def tracks_cell(self, memory: rc505.Memory) -> str:
        recorded = {t.number for t in memory.tracks}
        muted = self.muted_tracks.get(memory.number, set())
        gains = self.track_gains.get(memory.number, {})
        cells = []
        for n in range(1, TRACK_COUNT + 1):
            if n not in recorded:
                cells.append("·")
            else:
                cell = str(n) + ("*" if n in gains else "")
                cells.append(f"({cell})" if n in muted else cell)
        return "  ".join(cells)

    def refresh_tracks_cell(self, memory: rc505.Memory) -> None:
        if self.tree.exists(str(memory.number)):
            self.tree.set(str(memory.number), "tracks", self.tracks_cell(memory))

    def memory_by_number(self, number: int) -> rc505.Memory:
        return next(m for m in self.memories if m.number == number)

    # ---------- selection ----------

    def on_tree_click(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) == "#1":
            self.tree.focus(row)
            self.tree.selection_set(row)
            self.toggle(int(row))
            return "break"
        return None  # let the Treeview select the row as the preview target

    def on_tree_double_click(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        if self.tree.identify_column(event.x) == "#1":
            self.toggle(int(row))  # a fast second click on the box is just another tick
        else:
            self.tree.selection_set(row)
            self.play_selected()
        return "break"

    def on_tree_space(self, _event: tk.Event) -> str:
        row = self.tree.focus()
        if row:
            self.toggle(int(row))
        return "break"

    def toggle(self, number: int) -> None:
        if not self.memory_by_number(number).tracks:
            return
        self.checked ^= {number}
        self.tree.set(str(number), "check", CHECKED if number in self.checked else UNCHECKED)
        self.update_selection_label()

    def set_all(self, value: bool) -> None:
        self.checked = {m.number for m in self.memories if m.tracks} if value else set()
        self.refresh_tree()

    def selected_memories(self) -> list[rc505.Memory]:
        return [m for m in self.memories if m.number in self.checked]

    def highlighted_memory(self) -> rc505.Memory | None:
        selection = self.tree.selection()
        return self.memory_by_number(int(selection[0])) if selection else None

    def update_selection_label(self) -> None:
        selected = self.selected_memories()
        stems = sum(len(m.tracks) for m in selected)
        size = sum(m.total_size for m in selected)
        self.selection_label.config(
            text=f"{len(selected)} memories, {stems} stems, {rc505.format_size(size)}" if selected else ""
        )

    # ---------- preview ----------

    @staticmethod
    def describe(memory: rc505.Memory) -> str:
        parts = [f"Memory {memory.label}"]
        if memory.has_custom_name:
            parts.append(memory.name)
        if memory.tempo:
            parts.append(f"{memory.tempo:g} BPM")
        parts.append(rc505.format_duration(memory.longest_duration))
        return " \u00b7 ".join(parts)

    def update_preview_label(self) -> None:
        if self.player is None:
            return
        if self.player.is_playing and self.loaded_memory:
            self.preview_var.set(f"Playing {self.describe(self.loaded_memory)}")
            return
        memory = self.highlighted_memory()
        if memory and memory.tracks:
            self.preview_var.set(f"Selected: {self.describe(memory)}")
        elif memory:
            self.preview_var.set(f"Memory {memory.label} is empty.")
        else:
            self.preview_var.set("Select a memory, then press Play (or double-click it).")

    def toggle_playback(self) -> None:
        if self.player and self.player.is_playing:
            self.stop_playback()
        else:
            self.play_selected()

    def play_selected(self) -> None:
        if self.player is None or str(self.play_button["state"]) == "disabled":
            return
        memory = self.highlighted_memory()
        if memory is None or not memory.tracks:
            self.preview_var.set("Select a memory with recordings to preview.")
            return
        if memory is self.loaded_memory:
            self.start_playback()
            return

        self.player.stop()
        self.load_token += 1
        token = self.load_token
        self.preview_var.set(f"Loading {self.describe(memory)} ...")
        self.play_button.config(text="\u25b6 Play")

        def load() -> None:
            try:
                tracks, sample_rate = player.load_memory_audio(memory)
                balance = player.balance_gains(tracks)  # cheap, and ready if auto-balance gets switched on
                self.events.put(("loaded", token, memory, tracks, sample_rate, balance))
            except Exception as exc:
                self.events.put(("load_error", token, memory, exc))

        threading.Thread(target=load, daemon=True).start()

    def on_audio_loaded(self, memory: rc505.Memory, tracks: dict, sample_rate: int, balance: dict) -> None:
        self.player.set_tracks(tracks, sample_rate)
        self.loaded_memory = memory
        self.loaded_balance = balance
        self.apply_mix_options()
        recorded = set(tracks)
        muted = self.muted_tracks.get(memory.number, set())
        for number, (var, button) in enumerate(zip(self.track_vars, self.track_buttons), start=1):
            var.set(number in recorded and number not in muted)
            button.config(state="normal" if number in recorded else "disabled")
        self.show_gains(memory, recorded)
        self.start_playback()

    def start_playback(self) -> None:
        # Honour the current track toggles (they persist across Stop/Play of the same memory).
        for number, var in enumerate(self.track_vars, start=1):
            self.player.set_muted(number, not var.get())
        self.player.set_volume(self.volume_var.get() / 100)
        try:
            self.player.play()
        except Exception as exc:
            self.preview_var.set(f"Couldn't open the audio output: {exc}")
            return
        self.play_button.config(text="\u25a0 Stop")
        self.update_preview_label()

    def stop_playback(self) -> None:
        if self.player:
            self.player.stop()
        self.play_button.config(text="\u25b6 Play")
        self.playhead["value"] = 0
        self.update_preview_label()

    def unload_preview(self) -> None:
        self.stop_playback()
        self.loaded_memory = None
        self.loaded_balance = {}
        self.update_mix_hint()
        self.load_token += 1
        for var, button in zip(self.track_vars, self.track_buttons):
            var.set(True)
            button.config(state="disabled")
        self.show_gains(None, set())

    def on_track_toggled(self, number: int) -> None:
        muted = not self.track_vars[number - 1].get()
        if self.player:
            self.player.set_muted(number, muted)
        memory = self.loaded_memory
        if memory is None:
            return
        # Remember per memory, so the mixdown export leaves the same tracks out.
        memory_muted = self.muted_tracks.setdefault(memory.number, set())
        if muted:
            memory_muted.add(number)
        else:
            memory_muted.discard(number)
        self.refresh_tracks_cell(memory)

    def on_gain_changed(self, number: int) -> None:
        percent = round(self.gain_vars[number - 1].get())
        self.gain_vars[number - 1].set(percent)  # snap to whole percent
        self.gain_labels[number - 1].config(text=f"{percent}%")
        if self.player:
            self.player.set_gain(number, percent / 100)
        memory = self.loaded_memory
        if memory is None:
            return
        gains = self.track_gains.setdefault(memory.number, {})
        if percent == 100:
            gains.pop(number, None)
        else:
            gains[number] = percent / 100
        if not gains:
            del self.track_gains[memory.number]
        self.refresh_tracks_cell(memory)

    def reset_gain(self, number: int) -> str:
        if str(self.gain_scales[number - 1]["state"]) != "disabled":
            self.gain_vars[number - 1].set(100)
            self.on_gain_changed(number)
        return "break"

    def reset_levels(self) -> None:
        for number in range(1, TRACK_COUNT + 1):
            self.reset_gain(number)

    def show_gains(self, memory: rc505.Memory | None, recorded: set[int]) -> None:
        """Load a memory's stored levels into the faders (and the player)."""
        gains = self.track_gains.get(memory.number, {}) if memory else {}
        for number, (var, scale, label) in enumerate(
            zip(self.gain_vars, self.gain_scales, self.gain_labels), start=1
        ):
            percent = round(gains.get(number, 1.0) * 100)
            var.set(percent)
            label.config(text=f"{percent}%")
            scale.config(state="normal" if number in recorded else "disabled")
            if self.player and memory:
                self.player.set_gain(number, percent / 100)
        self.reset_levels_button.config(state="normal" if recorded else "disabled")

    def on_volume_changed(self, _value: str) -> None:
        if self.player:
            self.player.set_volume(self.volume_var.get() / 100)

    def on_device_selected(self, _event: tk.Event) -> None:
        name = self.device_var.get()
        self.player.device = next((index for index, n in self.devices if n == name), None)
        if self.player.is_playing:
            self.start_playback()  # reopen on the new device

    # ---------- export ----------

    def on_export_mode_changed(self) -> None:
        wants_mixdown = self.mixdown_selected()
        self.repeats_spin.config(state="readonly" if wants_mixdown else "disabled")
        for check in (self.mono_check, self.balance_check):
            check.config(state="normal" if wants_mixdown else "disabled")
        self.apply_mix_options()

    def mixdown_selected(self) -> bool:
        return self.export_mode_var.get() in ("mixdown", "both")

    def apply_mix_options(self) -> None:
        """Make the preview play the way the mixdown will sound (only while a mixdown is being exported)."""
        mixdown = self.mixdown_selected()
        if self.player:
            self.player.mono = mixdown and self.mono_var.get()
            self.player.set_balance(self.loaded_balance if mixdown and self.balance_var.get() else None)
        self.update_mix_hint()

    def update_mix_hint(self) -> None:
        mixdown = self.mixdown_selected()
        if mixdown and self.balance_var.get() and self.loaded_memory and self.loaded_balance:
            changes = [
                f"T{n} {20 * math.log10(g):+.1f} dB"
                for n, g in sorted(self.loaded_balance.items())
                if abs(20 * math.log10(g)) >= 0.1
            ]
            text = "Auto-balance: " + (" · ".join(changes) if changes else "layers already even")
            text += ". Your faders apply on top."
        else:
            text = (
                "Track levels and mutes are remembered per memory and applied to its Mixdown only; "
                "stems are never changed. Double-click a fader to reset it."
            )
        if mixdown and self.mono_var.get():
            text = "Mono. " + text
        self.mix_hint_var.set(text)

    def browse_dest(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Choose where to save the export", initialdir=self.dest_var.get())
        if chosen:
            self.dest_var.set(chosen)

    def start_export(self) -> None:
        selected = self.selected_memories()
        if not selected:
            messagebox.showwarning("Nothing selected", "Tick at least one loop memory to export.", parent=self)
            return
        dest = self.dest_var.get().strip()
        bundle_name = rc505.sanitize_filename(self.bundle_var.get().strip())
        if not dest or not bundle_name:
            messagebox.showwarning("Missing destination", "Choose a destination folder and bundle name.", parent=self)
            return

        mode = self.export_mode_var.get()
        if mode in ("mixdown", "both"):
            silent = [m.label for m in selected if not self.audible_tracks(m)]
            if silent:
                messagebox.showwarning(
                    "Nothing to mix",
                    f"Every track is muted or at 0% in memory {', '.join(silent)}.\n\n"
                    "Preview it and bring at least one track back, or untick the memory.",
                    parent=self,
                )
                return

        bundle_dir = Path(dest) / bundle_name
        source = Path(self.source_var.get())
        if bundle_dir.resolve() == source.resolve() or source.resolve() in bundle_dir.resolve().parents:
            messagebox.showerror("Invalid destination", "Don't export into the RC-505's own storage.", parent=self)
            return
        if bundle_dir.exists() and any(bundle_dir.iterdir()):
            if not messagebox.askyesno(
                "Folder exists",
                f"'{bundle_dir}' already exists and is not empty.\n\n"
                "Export into it anyway? Files with the same name will be overwritten.",
                parent=self,
            ):
                return

        self.cancel_requested = False
        self.set_busy(True)
        self.progress["value"] = 0
        # Snapshots, so changes made while exporting don't affect the running export.
        muted = {number: set(tracks) for number, tracks in self.muted_tracks.items()}
        gains = {number: dict(levels) for number, levels in self.track_gains.items()}
        self.last_export_dir = bundle_dir
        self.worker = threading.Thread(
            target=self.export_worker,
            args=(
                selected, bundle_dir, mode, muted, gains, self.repeats_var.get(),
                self.mono_var.get(), self.balance_var.get(),
            ),
            daemon=True,
        )
        self.worker.start()

    def export_to(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Export to...", initialdir=self.dest_var.get())
        if chosen:
            self.dest_var.set(chosen)
            self.start_export()

    def open_export_folder(self) -> None:
        for folder in (self.last_export_dir, Path(self.dest_var.get().strip() or ".")):
            if folder and folder.is_dir():
                os.startfile(folder)
                return
        messagebox.showinfo("Nothing to open", "The export folder doesn't exist yet.", parent=self)

    def audible_tracks(self, memory: rc505.Memory) -> set[int]:
        muted = self.muted_tracks.get(memory.number, set())
        gains = self.track_gains.get(memory.number, {})
        return {t.number for t in memory.tracks if t.number not in muted and gains.get(t.number, 1.0) > 0}

    def export_worker(
        self,
        memories: list[rc505.Memory],
        bundle_dir: Path,
        mode: str,
        muted: dict[int, set[int]],
        gains: dict[int, dict[int, float]],
        repeats: int,
        mono: bool,
        balance: bool,
    ) -> None:
        do_stems, do_mixdowns = mode in ("stems", "both"), mode in ("mixdown", "both")
        phases = do_stems + do_mixdowns
        should_cancel = lambda: self.cancel_requested  # noqa: E731
        copied, mixed = [], []
        try:
            if do_stems:
                copied = rc505.export(
                    memories,
                    bundle_dir,
                    progress=lambda done, total, current: self.events.put((
                        "progress",
                        (done / total if total else 1) / phases,
                        f"Copying {Path(current).parent.name}/{Path(current).name} ..." if current else "",
                    )),
                    should_cancel=should_cancel,
                )
            if do_mixdowns and not self.cancel_requested:
                base = 1 if do_stems else 0
                mixed = player.export_mixdowns(
                    memories,
                    bundle_dir,
                    muted,
                    repeats=repeats,
                    progress=lambda fraction, name: self.events.put(
                        ("progress", (base + fraction) / phases, f"Mixing {name} ..." if name else "")
                    ),
                    should_cancel=should_cancel,
                    gains_by_memory=gains,
                    mono=mono,
                    balance=balance,
                )
            self.events.put(("done", bundle_dir, len(copied), len(mixed), self.cancel_requested))
        except Exception as exc:  # report any failure (disk full, device unplugged, ...) to the GUI
            self.events.put(("error", exc))

    def on_export_finished(self, bundle_dir: Path, stems: int, mixdowns: int, cancelled: bool) -> None:
        self.set_busy(False)
        parts = []
        if stems:
            parts.append(f"{stems} stem{'s' * (stems != 1)}")
        if mixdowns:
            parts.append(f"{mixdowns} mixdown{'s' * (mixdowns != 1)}")
        summary = " and ".join(parts) or "nothing"
        if cancelled:
            self.status_var.set(f"Export cancelled after {summary}.")
            return
        self.progress["value"] = 1000
        self.status_var.set(f"Exported {summary} to {bundle_dir}")
        self.bundle_var.set(self.default_bundle_name())
        if messagebox.askyesno("Export complete", f"Exported {summary} to:\n{bundle_dir}\n\nOpen the folder?", parent=self):
            os.startfile(bundle_dir)

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.status_var.set("Cancelling after the current file...")

    def set_busy(self, busy: bool) -> None:
        self.export_button.config(state="disabled" if busy else "normal")
        self.export_to_button.config(state="disabled" if busy else "normal")
        self.cancel_button.config(state="normal" if busy else "disabled")

    # ---------- background events ----------

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                _, fraction, text = event
                self.progress["value"] = 1000 * fraction
                if text and not self.cancel_requested:
                    self.status_var.set(text)
            elif kind == "done":
                self.on_export_finished(*event[1:])
            elif kind == "error":
                self.set_busy(False)
                self.status_var.set("Export failed.")
                messagebox.showerror("Export failed", str(event[1]), parent=self)
            elif kind == "loaded" and event[1] == self.load_token:
                self.on_audio_loaded(*event[2:])
            elif kind == "load_error" and event[1] == self.load_token:
                self.preview_var.set(f"Couldn't load Memory {event[2].label}: {event[3]}")

        if self.player and self.player.is_playing:
            position, length = self.player.position()
            self.playhead["value"] = 1000 * position / length if length else 0
        self.after(POLL_MS, self.poll_events)

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Export running", "An export is in progress. Cancel it and quit?", parent=self):
                return
            self.cancel_requested = True
            self.worker.join(timeout=5)
        if self.player:
            self.player.close()
        self.destroy()


if __name__ == "__main__":
    ExportApp().mainloop()
