"""Tkinter GUI for exporting RC-505 loop memory stems into DAW-ready folders."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import rc505

CHECKED, UNCHECKED = "\u2611", "\u2610"
TRACK_COUNT = 5


class ExportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RC-505 USB Storage Export Tool")
        self.geometry("820x620")
        self.minsize(680, 480)

        self.memories: list[rc505.Memory] = []
        self.checked: set[int] = set()  # memory numbers
        self.events: queue.Queue = queue.Queue()
        self.cancel_requested = False
        self.worker: threading.Thread | None = None

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=str(Path.home() / "Music"))
        self.bundle_var = tk.StringVar(value=self.default_bundle_name())
        self.hide_empty_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Select the RC-505's ROLAND folder to begin.")

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.browse_source)

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

        memories = ttk.LabelFrame(self, text="2. Loop memories to export (click to tick)")
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

        columns = ("check", "memory", "name", "tracks", "length", "size")
        tree_frame = ttk.Frame(memories)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="none")
        headings = {
            "check": ("", 36, "center"),
            "memory": ("Memory", 70, "center"),
            "name": ("Name", 180, "w"),
            "tracks": ("Tracks recorded", 170, "center"),
            "length": ("Longest", 80, "center"),
            "size": ("Size", 90, "e"),
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
        self.tree.bind("<space>", self.on_tree_space)

        dest = ttk.LabelFrame(self, text="3. Export destination")
        dest.pack(fill="x", **pad)
        dest.columnconfigure(1, weight=1)
        ttk.Label(dest, text="Save in:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(dest, textvariable=self.dest_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(dest, text="Browse...", command=self.browse_dest).grid(row=0, column=2, padx=6)
        ttk.Label(dest, text="Bundle folder name:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(dest, textvariable=self.bundle_var).grid(row=1, column=1, sticky="ew", pady=4)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(0, 4))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        self.export_button = ttk.Button(bottom, text="Export", command=self.start_export)
        self.export_button.pack(side="right")
        self.cancel_button = ttk.Button(bottom, text="Cancel", command=self.request_cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=6)

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
            self.memories = rc505.scan(root)
        except (rc505.RC505Error, OSError) as exc:
            messagebox.showerror("Not an RC-505 folder", str(exc), parent=self)
            return
        self.source_var.set(str(root))
        available = {m.number for m in self.memories if m.tracks}
        self.checked &= available
        self.refresh_tree()
        self.status_var.set(f"Found {len(available)} memories with recordings out of {len(self.memories)}.")

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for memory in self.memories:
            if self.hide_empty_var.get() and not memory.tracks:
                continue
            recorded = {t.number for t in memory.tracks}
            tracks = "  ".join(str(n) if n in recorded else "\u00b7" for n in range(1, TRACK_COUNT + 1))
            self.tree.insert(
                "",
                "end",
                iid=str(memory.number),
                values=(
                    CHECKED if memory.number in self.checked else UNCHECKED,
                    memory.label,
                    memory.name,
                    tracks,
                    rc505.format_duration(memory.longest_duration) if memory.tracks else "",
                    rc505.format_size(memory.total_size) if memory.tracks else "empty",
                ),
                tags=() if memory.tracks else ("empty",),
            )
        self.update_selection_label()

    # ---------- selection ----------

    def on_tree_click(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.focus(row)
            self.toggle(int(row))
        return "break"

    def on_tree_space(self, _event: tk.Event) -> None:
        row = self.tree.focus()
        if row:
            self.toggle(int(row))

    def toggle(self, number: int) -> None:
        memory = next(m for m in self.memories if m.number == number)
        if not memory.tracks:
            return
        self.checked ^= {number}
        self.tree.set(str(number), "check", CHECKED if number in self.checked else UNCHECKED)
        self.update_selection_label()

    def set_all(self, value: bool) -> None:
        self.checked = {m.number for m in self.memories if m.tracks} if value else set()
        self.refresh_tree()

    def selected_memories(self) -> list[rc505.Memory]:
        return [m for m in self.memories if m.number in self.checked]

    def update_selection_label(self) -> None:
        selected = self.selected_memories()
        stems = sum(len(m.tracks) for m in selected)
        size = sum(m.total_size for m in selected)
        self.selection_label.config(
            text=f"{len(selected)} memories, {stems} stems, {rc505.format_size(size)}" if selected else ""
        )

    # ---------- export ----------

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
        self.worker = threading.Thread(target=self.export_worker, args=(selected, bundle_dir), daemon=True)
        self.worker.start()
        self.after(100, self.poll_events)

    def export_worker(self, memories: list[rc505.Memory], bundle_dir: Path) -> None:
        try:
            copied = rc505.export(
                memories,
                bundle_dir,
                progress=lambda done, total, current: self.events.put(("progress", done, total, current)),
                should_cancel=lambda: self.cancel_requested,
            )
            self.events.put(("done", bundle_dir, len(copied), self.cancel_requested))
        except Exception as exc:  # report any failure (disk full, device unplugged, ...) to the GUI
            self.events.put(("error", exc))

    def poll_events(self) -> None:
        finished = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                _, done, total, current = event
                self.progress["value"] = 1000 * done / total if total else 1000
                if current:
                    self.status_var.set(f"Copying {Path(current).parent.name}/{Path(current).name} ...")
            elif kind == "done":
                _, bundle_dir, count, cancelled = event
                finished = True
                self.on_export_finished(bundle_dir, count, cancelled)
            elif kind == "error":
                finished = True
                self.set_busy(False)
                self.status_var.set("Export failed.")
                messagebox.showerror("Export failed", str(event[1]), parent=self)
        if not finished:
            self.after(100, self.poll_events)

    def on_export_finished(self, bundle_dir: Path, count: int, cancelled: bool) -> None:
        self.set_busy(False)
        if cancelled:
            self.status_var.set(f"Export cancelled after {count} files.")
            return
        self.status_var.set(f"Exported {count} stems to {bundle_dir}")
        self.bundle_var.set(self.default_bundle_name())
        if messagebox.askyesno("Export complete", f"Exported {count} stems to:\n{bundle_dir}\n\nOpen the folder?", parent=self):
            os.startfile(bundle_dir)

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.status_var.set("Cancelling after the current file...")

    def set_busy(self, busy: bool) -> None:
        self.export_button.config(state="disabled" if busy else "normal")
        self.cancel_button.config(state="normal" if busy else "disabled")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Export running", "An export is in progress. Cancel it and quit?", parent=self):
                return
            self.cancel_requested = True
            self.worker.join(timeout=5)
        self.destroy()


if __name__ == "__main__":
    ExportApp().mainloop()
