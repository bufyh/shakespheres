#!/usr/bin/env python3
"""
yt-dlp GUI Downloader
----------------------
A Tkinter front-end for yt-dlp supporting:
  - MP3 (audio-only), full video, or thumbnail-only downloads
  - Live, real-time stdout streaming into the GUI (thread-safe via a queue —
    the background thread never touches Tk widgets directly)
  - Safe cancellation of an in-progress download (kills the whole process
    tree on Windows via taskkill /T, including any ffmpeg child process
    yt-dlp spawns for merging/audio extraction)
  - Automatic clipboard monitoring that detects new links and drops them
    into the URL box (stripping playlist params unless "Playlist" is on)
  - Live command preview + one-click "Copy Command" to clipboard
  - Destination folder picker

Requires: yt-dlp on PATH (and ffmpeg on PATH for mp3/merge support).
    pip install yt-dlp
"""

import os
import re
imppip show yt-dlport shutil
import platform
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


IS_WINDOWS = platform.system() == "Windows"
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def strip_playlist_param(url: str) -> str:
    """Remove &list=/?list= and &index= so a single video downloads instead of a whole playlist."""
    url = re.sub(r"([&?])list=[^&]*", "", url)
    url = re.sub(r"([&?])index=[^&]*", "", url)
    url = re.sub(r"[?&]+$", "", url)
    url = url.replace("?&", "?")
    return url


class YTDLPGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("yt-dlp GUI Downloader")
        self.geometry("840x640")
        self.minsize(720, 560)

        # --- state ---
        self.download_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.url_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="mp3")        # mp3 | video | thumbnail
        self.quality_var = tk.StringVar(value="best")     # best | 1080 | 720 | 480
        self.playlist_var = tk.BooleanVar(value=False)
        self.clipboard_monitor_var = tk.BooleanVar(value=True)
        self.command_preview_var = tk.StringVar()

        self.process = None
        self.download_thread = None
        self.output_queue = queue.Queue()
        self.cancel_requested = False
        self._last_clipboard = ""

        self._check_ytdlp_available()
        self._build_ui()
        self._wire_traces()
        self._update_command_preview()

        self._poll_output_queue()
        self._start_clipboard_monitor()

    # ---------------------------------------------------------------
    # Setup helpers
    # ---------------------------------------------------------------
    def _check_ytdlp_available(self):
        if shutil.which("yt-dlp") is None:
            messagebox.showwarning(
                "yt-dlp not found",
                "yt-dlp was not found on your PATH.\n\n"
                "Install it with:  pip install yt-dlp\n\n"
                "The app will still open, but downloads will fail until yt-dlp is installed.",
            )

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # URL row
        url_frame = ttk.LabelFrame(self, text="Video / Playlist URL")
        url_frame.pack(fill="x", **pad)

        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_var, font=("Segoe UI", 10))
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=8)

        ttk.Button(url_frame, text="Paste", command=self._paste_from_clipboard).pack(
            side="left", padx=4, pady=8
        )
        ttk.Checkbutton(
            url_frame, text="Auto-monitor clipboard", variable=self.clipboard_monitor_var
        ).pack(side="left", padx=4, pady=8)
        ttk.Checkbutton(
            url_frame, text="Playlist", variable=self.playlist_var
        ).pack(side="left", padx=(4, 8), pady=8)

        # Options row
        opt_frame = ttk.LabelFrame(self, text="Download Options")
        opt_frame.pack(fill="x", **pad)

        mode_box = ttk.Frame(opt_frame)
        mode_box.pack(side="left", padx=8, pady=8)
        for label, value in (
            ("MP3 (audio only)", "mp3"),
            ("Full video", "video"),
            ("Thumbnail only", "thumbnail"),
        ):
            ttk.Radiobutton(mode_box, text=label, variable=self.mode_var, value=value).pack(anchor="w")

        quality_box = ttk.Frame(opt_frame)
        quality_box.pack(side="left", padx=24, pady=8)
        ttk.Label(quality_box, text="Video quality:").pack(anchor="w")
        self.quality_combo = ttk.Combobox(
            quality_box,
            textvariable=self.quality_var,
            values=["best", "1080", "720", "480"],
            state="readonly",
            width=10,
        )
        self.quality_combo.pack(anchor="w", pady=(2, 0))

        # Folder row
        folder_frame = ttk.LabelFrame(self, text="Save To")
        folder_frame.pack(fill="x", **pad)
        ttk.Entry(folder_frame, textvariable=self.download_dir).pack(
            side="left", fill="x", expand=True, padx=(8, 4), pady=8
        )
        ttk.Button(folder_frame, text="Browse...", command=self._choose_folder).pack(
            side="left", padx=(4, 8), pady=8
        )

        # Command preview row
        preview_frame = ttk.LabelFrame(self, text="Command Preview")
        preview_frame.pack(fill="x", **pad)
        preview_entry = ttk.Entry(preview_frame, textvariable=self.command_preview_var, state="readonly")
        preview_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        ttk.Button(preview_frame, text="Copy Command", command=self._copy_command).pack(
            side="left", padx=(4, 8), pady=8
        )

        # Action buttons
        action_frame = ttk.Frame(self)
        action_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(action_frame, text="Start Download", command=self._start_download)
        self.start_btn.pack(side="left", padx=(8, 4))
        self.cancel_btn = ttk.Button(action_frame, text="Cancel", command=self._cancel_download, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.clear_log_btn = ttk.Button(action_frame, text="Clear Log", command=self._clear_log)
        self.clear_log_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(action_frame, textvariable=self.status_var, foreground="#555").pack(side="right", padx=8)

        # Log
        log_frame = ttk.LabelFrame(self, text="Output")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_widget = scrolledtext.ScrolledText(
            log_frame, state="disabled", font=("Consolas", 9), wrap="word"
        )
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _wire_traces(self):
        for var in (self.url_var, self.mode_var, self.quality_var, self.playlist_var, self.download_dir):
            var.trace_add("write", lambda *_: self._update_command_preview())

    # ---------------------------------------------------------------
    # Command building
    # ---------------------------------------------------------------
    def _build_command(self):
        url = self.url_var.get().strip()
        folder = self.download_dir.get().strip() or "."
        outtmpl = os.path.join(folder, "%(title)s.%(ext)s")

        cmd = ["yt-dlp", "-o", outtmpl]

        if not self.playlist_var.get():
            cmd.append("--no-playlist")

        mode = self.mode_var.get()
        if mode == "mp3":
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        elif mode == "video":
            q = self.quality_var.get()
            if q == "best":
                cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
            else:
                cmd += ["-f", f"bv*[height<={q}]+ba/b[height<={q}]", "--merge-output-format", "mp4"]
        elif mode == "thumbnail":
            cmd += ["--skip-download", "--write-thumbnail"]

        cmd += ["--newline"]  # forces yt-dlp to print progress on separate lines instead of \r overwrites
        if url:
            cmd.append(url)
        return cmd

    def _update_command_preview(self):
        cmd = self._build_command()
        preview = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        self.command_preview_var.set(preview)

    def _copy_command(self):
        preview = self.command_preview_var.get()
        self.clipboard_clear()
        self.clipboard_append(preview)
        self.status_var.set("Command copied to clipboard")

    # ---------------------------------------------------------------
    # Folder / clipboard helpers
    # ---------------------------------------------------------------
    def _choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.download_dir.get() or os.getcwd())
        if chosen:
            self.download_dir.set(chosen)

    def _paste_from_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        self._apply_clipboard_text(text)

    def _apply_clipboard_text(self, text):
        match = URL_RE.search(text or "")
        if not match:
            return
        url = match.group(0)
        if not self.playlist_var.get():
            url = strip_playlist_param(url)
        self.url_var.set(url)

    def _start_clipboard_monitor(self):
        def poll():
            if self.clipboard_monitor_var.get():
                try:
                    text = self.clipboard_get()
                except tk.TclError:
                    text = ""
                if text and text != self._last_clipboard and URL_RE.search(text):
                    self._last_clipboard = text
                    self._apply_clipboard_text(text)
            self.after(1000, poll)

        self.after(1000, poll)

    # ---------------------------------------------------------------
    # Download / subprocess management
    # ---------------------------------------------------------------
    def _start_download(self):
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showinfo("Busy", "A download is already running.")
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Please enter or paste a video/playlist URL first.")
            return

        folder = self.download_dir.get().strip()
        if not folder:
            messagebox.showerror("Missing folder", "Please choose a destination folder.")
            return
        os.makedirs(folder, exist_ok=True)

        if shutil.which("yt-dlp") is None:
            messagebox.showerror("yt-dlp not found", "yt-dlp is not installed or not on PATH.")
            return

        cmd = self._build_command()
        self.cancel_requested = False
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_var.set("Downloading...")
        self._log(f"$ {' '.join(cmd)}\n")

        self.download_thread = threading.Thread(target=self._run_download, args=(cmd,), daemon=True)
        self.download_thread.start()

    def _run_download(self, cmd):
        """Runs in a background thread. Never touches Tk widgets directly —
        all UI updates go through self.output_queue and are drained on the
        main thread by _poll_output_queue(). This is the key thread-safety
        rule for Tkinter: only the main thread may touch widgets."""
        try:
            kwargs = {}
            if IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True  # own process group so we can killpg cleanly

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                **kwargs,
            )

            for line in self.process.stdout:
                self.output_queue.put(("line", line))

            self.process.wait()
            returncode = self.process.returncode

            if self.cancel_requested:
                self.output_queue.put(("status", "Cancelled"))
            elif returncode == 0:
                self.output_queue.put(("status", "Done"))
            else:
                self.output_queue.put(("status", f"Failed (exit code {returncode})"))

        except FileNotFoundError:
            self.output_queue.put(("line", "ERROR: yt-dlp executable not found.\n"))
            self.output_queue.put(("status", "Error"))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the UI
            self.output_queue.put(("line", f"ERROR: {exc}\n"))
            self.output_queue.put(("status", "Error"))
        finally:
            self.process = None
            self.output_queue.put(("done", None))

    def _cancel_download(self):
        if not self.process:
            return
        self.cancel_requested = True
        self._log("\n--- Cancelling download... ---\n")
        pid = self.process.pid
        try:
            if IS_WINDOWS:
                # yt-dlp often spawns ffmpeg as a child process for merging/audio
                # extraction. Plain terminate()/kill() only kills yt-dlp itself and
                # leaves ffmpeg running. taskkill /T kills the entire process tree.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(os.getpgid(pid), 15)  # SIGTERM to the whole process group
        except Exception as exc:  # noqa: BLE001
            self._log(f"(cleanup warning: {exc})\n")

    # ---------------------------------------------------------------
    # Queue polling / logging (main thread only)
    # ---------------------------------------------------------------
    def _poll_output_queue(self):
        try:
            while True:
                kind, payload = self.output_queue.get_nowait()
                if kind == "line":
                    self._log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_output_queue)

    def _log(self, text):
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", text)
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _clear_log(self):
        self.log_widget.config(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.config(state="disabled")

    # ---------------------------------------------------------------
    def _on_close(self):
        if self.process:
            if messagebox.askyesno("Download in progress", "A download is still running. Cancel it and quit?"):
                self._cancel_download()
                self.after(300, self.destroy)
            return
        self.destroy()


if __name__ == "__main__":
    app = YTDLPGui()
    app.mainloop()
