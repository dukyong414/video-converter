"""Convert videos into small H.264 MP4 files that PowerPoint can insert.

PowerPoint often crashes on Motion-JPEG AVI files, especially at high frame
rates and very high bitrates. This app keeps the picture size, encodes with
the NVIDIA GPU when it can, and writes a normal MP4 (H.264, yuv420p, fast start).
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseWindow = TkinterDnD.Tk
except ImportError:  # Drag and drop is optional; browsing still works.
    DND_FILES = None
    BaseWindow = tk.Tk


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"
VIDEO_EXTENSIONS = {
    ".avi",
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".webm",
    ".flv",
    ".mts",
    ".m2ts",
    ".ts",
    ".3gp",
    ".vob",
}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

BG = "#eef2f5"
CARD = "#ffffff"
INK = "#1b2430"
MUTED = "#5d6b7a"
LINE = "#d5dee7"
ACCENT = "#0c6b4d"
ACCENT_ACTIVE = "#09573f"
WARN = "#9a3412"


def enable_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_fraction(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" not in value:
        try:
            return float(value)
        except ValueError:
            return 0.0
    num, den = value.split("/", 1)
    try:
        denominator = float(den)
        if denominator == 0:
            return 0.0
        return float(num) / denominator
    except ValueError:
        return 0.0


def parse_clock(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return 0.0


def run_hidden(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )


def ffmpeg_candidates() -> list[Path]:
    found: list[Path] = []
    tools = APP_DIR / "tools"
    if tools.exists():
        found.extend(sorted(tools.glob("ffmpeg-*/bin/ffmpeg.exe")))
        found.extend(sorted(tools.glob("*/bin/ffmpeg.exe")))
    on_path = shutil.which("ffmpeg")
    if on_path:
        found.append(Path(on_path))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path).lower()
        if key not in seen and path.exists():
            seen.add(key)
            unique.append(path)
    return unique


def nvenc_works(ffmpeg: Path) -> bool:
    result = run_hidden(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=256x256:r=30:d=0.05",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
        ]
    )
    return result.returncode == 0


def gpu_name() -> str:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return "NVIDIA GPU"
    result = run_hidden(
        [smi, "--query-gpu=name", "--format=csv,noheader"]
    )
    name = result.stdout.strip().splitlines()
    return name[0].strip() if name and name[0].strip() else "NVIDIA GPU"


@dataclass
class Toolbox:
    ffmpeg: Path
    ffprobe: Path
    gpu_encode: bool
    gpu_label: str


def load_toolbox() -> Toolbox:
    """Find FFmpeg without launching it, so the window can open immediately."""
    ffmpeg = next(iter(ffmpeg_candidates()), None)
    if ffmpeg is None:
        raise FileNotFoundError("FFmpeg was not found in the tools folder.")
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    if not ffprobe.exists():
        raise FileNotFoundError("FFmpeg was not found in the tools folder.")
    return Toolbox(ffmpeg, ffprobe, True, "")


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    duration: float
    size: int
    codec: str
    pix_fmt: str
    color_range: str
    has_audio: bool
    format_name: str

    @property
    def full_range(self) -> bool:
        return self.pix_fmt.startswith("yuvj") or self.color_range == "pc"

    @property
    def risk_note(self) -> str:
        notes: list[str] = []
        if self.codec in {"mjpeg", "rawvideo", "prores"} or "avi" in self.format_name:
            notes.append(self.codec.upper() if self.codec == "mjpeg" else self.format_name.split(",")[0].upper())
        if self.fps > 60:
            notes.append(f"{self.fps:.0f} fps")
        if not notes:
            return "Ready"
        return " · ".join(notes)


def probe_video(ffprobe: Path, path: Path) -> VideoInfo:
    result = run_hidden(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not read this video.")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("No video track found.")
    fmt = data.get("format") or {}
    size = int(fmt.get("size") or path.stat().st_size)
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    return VideoInfo(
        path=path,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=parse_fraction(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")),
        duration=duration,
        size=size,
        codec=str(video.get("codec_name") or "video"),
        pix_fmt=str(video.get("pix_fmt") or ""),
        color_range=str(video.get("color_range") or ""),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
        format_name=str(fmt.get("format_name") or path.suffix.lower().lstrip(".")),
    )


def output_path_for(source: Path, folder: Path, reserved: set[Path] | None = None) -> Path:
    """Same file name with an .mp4 extension. Never overwrite the source itself."""
    if source.suffix.lower() == ".mp4":
        dest = folder / f"{source.stem} (PowerPoint).mp4"
    else:
        dest = folder / f"{source.stem}.mp4"
    taken = reserved or set()
    number = 2
    while True:
        try:
            same_as_source = dest.resolve() == source.resolve()
        except OSError:
            same_as_source = False
        if not same_as_source and dest not in taken:
            return dest
        dest = folder / f"{source.stem} (PowerPoint {number}).mp4"
        number += 1


def target_fps(info: VideoInfo, choice: str) -> float:
    if choice in {"30", "60"} and info.fps > float(choice) + 0.1:
        return float(choice)
    return info.fps if info.fps > 1 else 30.0


def filter_and_size(info: VideoInfo, fps_choice: str, keep_resolution: bool) -> tuple[str, float, int, int]:
    filters: list[str] = []
    out_fps = target_fps(info, fps_choice)
    if out_fps + 0.1 < info.fps:
        filters.append(f"fps={int(out_fps)}")

    width, height = info.width, info.height
    limit = not keep_resolution and (width > 1920 or height > 1080)
    if limit:
        scale = "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"
        if info.full_range:
            scale += ":in_range=pc:out_range=tv"
        filters.append(scale)
        if width / max(height, 1) > 1920 / 1080:
            height = int(round(1920 * height / width))
            width = 1920
        else:
            width = int(round(1080 * width / max(height, 1)))
            height = 1080
        width -= width % 2
        height -= height % 2
    elif info.full_range or width % 2 or height % 2:
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        if info.full_range:
            scale += ":in_range=pc:out_range=tv"
        filters.append(scale)
        width -= width % 2
        height -= height % 2
    filters.append("format=yuv420p")
    return ",".join(filters), out_fps, max(width, 2), max(height, 2)


def bitrate_cap(width: int, height: int, fps: float, quality: str) -> int:
    """Bits per second. Tuned so a noisy 1024x400 clip stays sharp and small."""
    megapixels = (width * height) / 1_000_000
    per_megapixel = {"smaller": 6.0, "balanced": 11.0, "detail": 18.0}[quality]
    floor = {"smaller": 1_200_000, "balanced": 2_000_000, "detail": 3_500_000}[quality]
    ceiling = {"smaller": 8_000_000, "balanced": 12_000_000, "detail": 20_000_000}[quality]
    rate = megapixels * per_megapixel * 1_000_000
    if fps > 35:
        rate *= min(2.0, fps / 30) ** 0.65
    return int(max(floor, min(ceiling, rate)))


def build_command(
    toolbox: Toolbox,
    info: VideoInfo,
    dest: Path,
    *,
    fps_choice: str,
    quality: str,
    keep_resolution: bool,
    use_gpu: bool,
    hwaccel: bool,
) -> list[str]:
    filters, out_fps, width, height = filter_and_size(info, fps_choice, keep_resolution)
    cq = {"smaller": 26, "balanced": 21, "detail": 17}[quality]
    maxrate = bitrate_cap(width, height, out_fps, quality)
    gop = max(12, int(round(out_fps * 2)))
    cmd = [str(toolbox.ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if use_gpu and hwaccel:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(info.path), "-map", "0:v:0"]
    if info.has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += ["-dn", "-sn", "-map_metadata", "-1", "-vf", filters]
    if use_gpu:
        cmd += [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p6",
            "-tune",
            "hq",
            "-profile:v",
            "high",
            "-rc",
            "vbr",
            "-cq",
            str(cq),
            "-b:v",
            "0",
            "-maxrate",
            str(maxrate),
            "-bufsize",
            str(maxrate * 2),
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-bf",
            "2",
        ]
    else:
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-profile:v",
            "high",
            "-crf",
            str(cq),
            "-maxrate",
            str(maxrate),
            "-bufsize",
            str(maxrate * 2),
        ]
    cmd += [
        "-g",
        str(gop),
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "cfr",
        "-color_range",
        "tv",
        "-tag:v",
        "avc1",
        "-movflags",
        "+faststart",
    ]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-f", "mp4", str(dest)]
    return cmd


class Converter:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def convert_one(
        self,
        toolbox: Toolbox,
        info: VideoInfo,
        dest: Path,
        *,
        fps_choice: str,
        quality: str,
        keep_resolution: bool,
        on_progress,
    ) -> None:
        partial = dest.with_name(dest.stem + ".__part__.mp4")
        if partial.exists():
            partial.unlink()
        attempts: list[tuple[bool, bool]] = []
        if toolbox.gpu_encode:
            attempts.append((True, True))
            attempts.append((True, False))
        attempts.append((False, False))

        last_error = "Conversion failed."
        for index, (use_gpu, hwaccel) in enumerate(attempts):
            if self.cancel_event.is_set():
                raise RuntimeError("Cancelled.")
            cmd = build_command(
                toolbox,
                info,
                partial,
                fps_choice=fps_choice,
                quality=quality,
                keep_resolution=keep_resolution,
                use_gpu=use_gpu,
                hwaccel=hwaccel,
            )
            try:
                self._run(cmd, info.duration, on_progress)
            except RuntimeError as exc:
                last_error = str(exc)
                if partial.exists():
                    partial.unlink(missing_ok=True)
                if self.cancel_event.is_set() or index == len(attempts) - 1:
                    raise RuntimeError(last_error) from exc
                if use_gpu and not hwaccel:
                    on_progress(0, "GPU encode failed, using the CPU for this file.")
                continue
            break
        os.replace(partial, dest)

    def _run(self, cmd: list[str], duration: float, on_progress) -> None:
        errors: list[str] = []

        def drain(pipe) -> None:
            for line in pipe:
                text = line.strip()
                if text:
                    errors.append(text)
                    del errors[:-30]

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        thread = threading.Thread(target=drain, args=(self.proc.stderr,), daemon=True)
        thread.start()
        for line in self.proc.stdout:
            if self.cancel_event.is_set():
                self.proc.terminate()
                break
            if line.startswith("out_time=") and not line.startswith("out_time_"):
                seen = parse_clock(line.split("=", 1)[1].strip())
                if duration > 0:
                    on_progress(min(0.99, seen / duration), None)
            elif line.startswith("progress=end"):
                on_progress(1.0, None)
        code = self.proc.wait()
        thread.join(timeout=2)
        self.proc = None
        if self.cancel_event.is_set():
            raise RuntimeError("Cancelled.")
        if code != 0:
            detail = errors[-1] if errors else f"FFmpeg exited with code {code}."
            raise RuntimeError(detail)


class App(BaseWindow):
    def __init__(self, toolbox: Toolbox) -> None:
        super().__init__()
        self.toolbox = toolbox
        self.videos: dict[str, VideoInfo] = {}
        self.row_of: dict[str, str] = {}
        self.next_row = 0
        self.pending_probes = 0
        self.converter = Converter()
        self.ui_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.settings = self._load_settings()

        self.title("PowerPoint Video Converter")
        self.geometry("1280x1000")
        self.minsize(1100, 920)
        self.configure(bg=BG)
        self._build_style()
        self._build()
        self._apply_settings()
        if DND_FILES is not None:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_drop)
                for widget in (self.tree, self.file_card):
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
            except tk.TclError:
                pass
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_ui)
        self._set_idle()
        threading.Thread(target=self._check_gpu, daemon=True).start()

    def _gpu_text(self) -> str:
        if self.toolbox.gpu_encode:
            text = "GPU encoding is on"
        else:
            text = "GPU encoding is unavailable, so the CPU will be used"
        if self.toolbox.gpu_label:
            text = f"{text} · {self.toolbox.gpu_label}"
        return text

    def _check_gpu(self) -> None:
        self.toolbox.gpu_label = gpu_name()
        self.toolbox.gpu_encode = nvenc_works(self.toolbox.ffmpeg)
        self.ui_queue.put(("gpu",))

    def _load_settings(self) -> dict:
        defaults = {
            "fps_choice": "30",
            "quality": "balanced",
            "output_mode": "same",
            "custom_dir": "",
            "keep_resolution": True,
            "open_folder": True,
        }
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        defaults.update({key: saved[key] for key in defaults if key in saved})
        return defaults

    def _save_settings(self) -> None:
        data = {
            "fps_choice": self.fps_var.get(),
            "quality": self.quality_var.get(),
            "output_mode": self.output_mode.get(),
            "custom_dir": self.custom_dir.get(),
            "keep_resolution": bool(self.keep_res.get()),
            "open_folder": bool(self.open_folder.get()),
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background=CARD, foreground=INK)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Segoe UI", 20, "bold"))
        style.configure("Treeview", background=CARD, fieldbackground=CARD, rowheight=30, font=("Segoe UI", 10), borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI", 9), background="#f3f6f8", foreground=MUTED, relief="flat")
        style.map("Treeview", background=[("selected", "#d9efe6")], foreground=[("selected", INK)])
        style.configure("TCheckbutton", background=CARD, foreground=INK)
        style.configure("TRadiobutton", background=CARD, foreground=INK)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e1e8ee", bordercolor="#e1e8ee", lightcolor=ACCENT, darkcolor=ACCENT)

    def _build(self) -> None:
        accent = tk.Frame(self, bg=ACCENT, height=4)
        accent.pack(fill="x")

        header = ttk.Frame(self, padding=(22, 16, 22, 8))
        header.pack(fill="x")
        ttk.Label(header, text="PowerPoint Video Converter", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Same picture size, a much smaller file, and an MP4 PowerPoint can insert without crashing.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        self.gpu_label = ttk.Label(header, text=self._gpu_text(), style="Muted.TLabel")
        self.gpu_label.pack(anchor="w", pady=(2, 0))

        self.file_card = ttk.Frame(self, style="Card.TFrame", padding=12)
        self.file_card.pack(fill="both", expand=True, padx=22, pady=(8, 8))

        tools = ttk.Frame(self.file_card, style="Card.TFrame")
        tools.pack(fill="x", pady=(0, 8))
        self._button(tools, "Add videos", self.add_videos).pack(side="left")
        self._button(tools, "Add folder", self.add_folder).pack(side="left", padx=(8, 0))
        self._button(tools, "Remove", self.remove_selected).pack(side="left", padx=(8, 0))
        self._button(tools, "Clear", self.clear_videos).pack(side="left", padx=(8, 0))

        listing = ttk.Frame(self.file_card, style="Card.TFrame")
        listing.pack(fill="both", expand=True)
        columns = ("file", "size", "picture", "rate", "length", "source")
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="extended")
        headings = {
            "file": ("Video", 700),
            "size": ("Size", 90),
            "picture": ("Resolution", 120),
            "rate": ("Frame rate", 110),
            "length": ("Length", 80),
            "source": ("Source", 160),
        }
        self.tree.configure(height=6)
        for key, (label, width) in headings.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w", stretch=key == "file")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.tag_configure("risk", foreground=WARN)
        self.tree.bind("<Delete>", lambda _event: self.remove_selected())
        ttk.Label(
            self.file_card,
            text="Drop videos or folders here. The new file keeps the same name, with .mp4.",
            style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        settings = ttk.Frame(self, style="Card.TFrame", padding=14)

        columns = ttk.Frame(settings, style="Card.TFrame")
        columns.pack(fill="x")
        left = ttk.Frame(columns, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Frame rate", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.fps_var = tk.StringVar(value="30")
        for value, label in (
            ("30", "30 fps  ·  best for PowerPoint"),
            ("60", "60 fps"),
            ("original", "Keep the original frame rate"),
        ):
            ttk.Radiobutton(left, text=label, value=value, variable=self.fps_var, command=self._on_option).pack(anchor="w", pady=1)
        self.fps_hint = ttk.Label(left, text="", style="CardMuted.TLabel", wraplength=460)
        self.fps_hint.pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(columns, style="Card.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(28, 0))
        ttk.Label(right, text="File size", style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.quality_var = tk.StringVar(value="balanced")
        for value, label in (
            ("smaller", "Smaller file"),
            ("balanced", "Balanced  ·  recommended"),
            ("detail", "More detail"),
        ):
            ttk.Radiobutton(right, text=label, value=value, variable=self.quality_var, command=self._on_option).pack(anchor="w", pady=1)

        bottom = ttk.Frame(settings, style="Card.TFrame")
        bottom.pack(fill="x", pady=(12, 0))
        self.keep_res = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bottom,
            text="Keep original resolution",
            variable=self.keep_res,
            command=self._on_option,
        ).pack(side="left")
        self.open_folder = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bottom,
            text="Open the folder when finished",
            variable=self.open_folder,
            command=self._on_option,
        ).pack(side="left", padx=(18, 0))

        output = ttk.Frame(settings, style="Card.TFrame")
        output.pack(fill="x", pady=(8, 0))
        ttk.Label(output, text="Save to", style="Card.TLabel").pack(side="left")
        self.output_mode = tk.StringVar(value="same")
        ttk.Radiobutton(output, text="Same folder as the video", value="same", variable=self.output_mode, command=self._on_option).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(output, text="Choose a folder", value="custom", variable=self.output_mode, command=self._on_option).pack(side="left", padx=(12, 0))
        self.custom_dir = tk.StringVar(value="")
        self.dir_button = self._button(output, "Browse…", self.choose_output_dir)
        self.dir_button.pack(side="left", padx=(12, 0))
        self.dir_label = ttk.Label(settings, text="", style="CardMuted.TLabel", wraplength=980)
        self.dir_label.pack(anchor="w", pady=(4, 0))

        action = ttk.Frame(self, padding=(22, 4, 22, 0))
        self.convert_button = tk.Button(
            action,
            text="Convert for PowerPoint",
            command=self.start_convert,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_ACTIVE,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            font=("Segoe UI", 11, "bold"),
            cursor="hand2",
        )
        self.convert_button.pack(side="left")
        self.cancel_button = self._button(action, "Cancel", self.converter.cancel)
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.status = ttk.Label(action, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=(14, 0))

        foot = ttk.Frame(self, padding=(22, 8, 22, 14))
        self.progress = ttk.Progressbar(foot, mode="determinate", maximum=1000)
        self.progress.pack(fill="x")
        self.log = tk.Text(
            foot,
            height=4,
            bg="#f7f9fb",
            fg=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=LINE,
            font=("Segoe UI", 9),
            wrap="word",
            padx=8,
            pady=6,
        )
        self.log.pack(fill="x", pady=(8, 0))
        self.log.configure(state="disabled")
        # Keep the convert controls on screen. The video list takes the leftover space.
        foot.pack(side="bottom", fill="x")
        action.pack(side="bottom", fill="x")
        settings.pack(side="bottom", fill="x", padx=22, pady=(0, 8))

    def _button(self, parent, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#f4f7f9",
            fg=INK,
            activebackground="#e7eef3",
            activeforeground=INK,
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            font=("Segoe UI", 10),
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=LINE,
        )

    def _apply_settings(self) -> None:
        self.fps_var.set(self.settings["fps_choice"])
        self.quality_var.set(self.settings["quality"])
        self.output_mode.set(self.settings["output_mode"])
        self.custom_dir.set(self.settings["custom_dir"])
        self.keep_res.set(bool(self.settings["keep_resolution"]))
        self.open_folder.set(bool(self.settings["open_folder"]))
        self._refresh_option_labels()

    def _on_option(self) -> None:
        self._refresh_option_labels()
        self._save_settings()

    def _refresh_option_labels(self) -> None:
        custom = self.output_mode.get() == "custom"
        self.dir_button.configure(state="normal" if custom else "disabled")
        folder = self.custom_dir.get().strip()
        self.dir_label.configure(text=folder if custom and folder else "")
        high_fps = any(info.fps > 60 for info in self.videos.values())
        if self.fps_var.get() == "original" and high_fps:
            self.fps_hint.configure(text="Above 60 fps often freezes or crashes PowerPoint.")
        elif self.fps_var.get() == "30" and high_fps:
            self.fps_hint.configure(text="30 fps is the frame rate PowerPoint handles reliably.")
        else:
            self.fps_hint.configure(text="")

    def _set_idle(self) -> None:
        count = len(self.videos)
        ready = count > 0 and self.pending_probes == 0
        self.convert_button.configure(state="normal" if ready else "disabled")
        self.cancel_button.configure(state="disabled")
        if self.pending_probes:
            self.status.configure(text="Reading videos…")
        elif count == 0:
            self.status.configure(text="Add the videos you want to put in PowerPoint.")
        else:
            total = sum(info.size for info in self.videos.values())
            self.status.configure(text=f"{count} video{'s' if count != 1 else ''} · {format_bytes(total)}")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.convert_button.configure(state=state if self.videos or busy else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def add_videos(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose videos",
            filetypes=[
                ("Video files", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))),
                ("All files", "*.*"),
            ],
        )
        if paths:
            self._enqueue_paths([Path(path) for path in paths])

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder of videos")
        if folder:
            self._enqueue_paths(self._videos_in(Path(folder)))

    def choose_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="Choose where to save the MP4 files")
        if folder:
            self.custom_dir.set(folder)
            self.output_mode.set("custom")
            self._on_option()

    def _videos_in(self, folder: Path) -> list[Path]:
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                    found.append(Path(dirpath) / name)
            if len(found) >= 500:
                break
        return found

    def _on_drop(self, event) -> None:
        paths = [Path(item) for item in self.tk.splitlist(event.data)]
        expanded: list[Path] = []
        for path in paths:
            if path.is_dir():
                expanded.extend(self._videos_in(path))
            else:
                expanded.append(path)
        self._enqueue_paths(expanded)

    def _ensure_row(self, path: Path) -> str:
        key = str(path.resolve())
        if key not in self.row_of:
            self.next_row += 1
            iid = f"v{self.next_row}"
            self.row_of[key] = iid
            self.tree.insert("", "end", iid=iid, values=(path.name, "…", "…", "…", "…", "Reading…"))
        return key

    def _enqueue_paths(self, paths: list[Path]) -> None:
        fresh: list[Path] = []
        for path in paths:
            if not path.exists() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            key = str(path.resolve())
            if key in self.row_of:
                continue
            fresh.append(path)
        if not fresh:
            return
        self.pending_probes += len(fresh)
        for path in fresh:
            self._ensure_row(path)
        self.status.configure(text="Reading videos…")
        self.convert_button.configure(state="disabled")

        def work() -> None:
            for path in fresh:
                try:
                    info = probe_video(self.toolbox.ffprobe, path)
                except Exception as exc:  # Show the file and the reason instead of dropping it.
                    self.ui_queue.put(("probe-error", str(path.resolve()), path, str(exc)))
                    continue
                self.ui_queue.put(("probe", info))

        threading.Thread(target=work, daemon=True).start()

    def remove_selected(self) -> None:
        selected = set(self.tree.selection())
        for key, iid in list(self.row_of.items()):
            if iid in selected:
                self.videos.pop(key, None)
                self.row_of.pop(key, None)
                self.tree.delete(iid)
        self._refresh_option_labels()
        self._set_idle()

    def clear_videos(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.videos.clear()
        self.row_of.clear()
        self._refresh_option_labels()
        self._set_idle()

    def _note_probe_done(self) -> None:
        self.pending_probes = max(0, self.pending_probes - 1)

    def _show_video(self, info: VideoInfo) -> None:
        self._note_probe_done()
        key = str(info.path.resolve())
        if key not in self.row_of:
            if not (self.worker and self.worker.is_alive()):
                self._set_idle()
            return
        iid = self.row_of.get(key)
        values = (
            info.path.name,
            format_bytes(info.size),
            f"{info.width}×{info.height}" if info.width else "—",
            f"{info.fps:.0f} fps" if info.fps else "—",
            format_duration(info.duration),
            info.risk_note,
        )
        tags = ("risk",) if info.risk_note != "Ready" else ()
        if iid and self.tree.exists(iid):
            self.tree.item(iid, values=values, tags=tags)
        self.videos[key] = info
        self._refresh_option_labels()
        if not (self.worker and self.worker.is_alive()):
            self._set_idle()

    def start_convert(self) -> None:
        if not self.videos:
            return
        if self.output_mode.get() == "custom" and not self.custom_dir.get().strip():
            messagebox.showinfo("Choose a folder", "Choose where the MP4 files should be saved.", parent=self)
            return
        items = list(self.videos.values())
        fps_choice = self.fps_var.get()
        quality = self.quality_var.get()
        keep_resolution = bool(self.keep_res.get())
        output_mode = self.output_mode.get()
        custom_dir = self.custom_dir.get().strip()
        open_folder = bool(self.open_folder.get())
        self.converter.cancel_event.clear()
        self._set_busy(True)
        self.progress["value"] = 0
        self._log_clear()
        self._log(f"Converting {len(items)} video{'s' if len(items) != 1 else ''} with {'the GPU' if self.toolbox.gpu_encode else 'the CPU'}.")

        def work() -> None:
            saved: list[Path] = []
            failed: list[str] = []
            reserved: set[Path] = set()
            try:
                for index, info in enumerate(items):
                    if self.converter.cancel_event.is_set():
                        break
                    folder = Path(custom_dir) if output_mode == "custom" else info.path.parent
                    folder.mkdir(parents=True, exist_ok=True)
                    dest = output_path_for(info.path, folder, reserved)
                    reserved.add(dest)
                    self.ui_queue.put(("status", f"Converting {index + 1} of {len(items)}  ·  {info.path.name}"))

                    def on_progress(fraction: float, note: str | None, *, file_index=index) -> None:
                        overall = (file_index + fraction) / len(items)
                        self.ui_queue.put(("progress", overall))
                        if note:
                            self.ui_queue.put(("log", note))

                    try:
                        self.converter.convert_one(
                            self.toolbox,
                            info,
                            dest,
                            fps_choice=fps_choice,
                            quality=quality,
                            keep_resolution=keep_resolution,
                            on_progress=on_progress,
                        )
                    except RuntimeError as exc:
                        if str(exc) == "Cancelled.":
                            break
                        failed.append(f"{info.path.name}: {exc}")
                        self.ui_queue.put(("log", f"Could not convert {info.path.name}. {exc}"))
                        continue
                    saved.append(dest)
                    new_size = dest.stat().st_size if dest.exists() else 0
                    self.ui_queue.put(
                        (
                            "log",
                            f"Saved {dest.name}  ·  {format_bytes(new_size)} from {format_bytes(info.size)}",
                        )
                    )
            except Exception as exc:
                failed.append(str(exc))
                self.ui_queue.put(("log", f"Stopped. {exc}"))
            self.ui_queue.put(("done", saved, failed, open_folder))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _finish(self, saved: list[Path], failed: list[str], open_folder: bool) -> None:
        self.progress["value"] = 1000 if saved and not failed else self.progress["value"]
        self._set_busy(False)
        self._set_idle()
        if self.converter.cancel_event.is_set() and not saved:
            self.status.configure(text="Cancelled.")
            self._log("Cancelled.")
            return
        if saved:
            self.status.configure(text=f"Done. {len(saved)} file{'s' if len(saved) != 1 else ''} ready for PowerPoint.")
            self._log("These MP4 files use H.264, which PowerPoint can insert.")
            if open_folder:
                os.startfile(saved[-1].parent)  # type: ignore[attr-defined]
        if failed:
            messagebox.showwarning(
                "Some videos were not converted",
                "\n".join(failed[:8]),
                parent=self,
            )
        elif not saved:
            self.status.configure(text="Nothing was converted.")

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_clear(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _poll_ui(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "probe":
                self._show_video(event[1])
            elif kind == "probe-error":
                key, path, error = event[1], event[2], event[3]
                self._note_probe_done()
                iid = self.row_of.get(key)
                if iid and self.tree.exists(iid):
                    self.tree.item(iid, values=(path.name, "—", "—", "—", "—", "Could not read"), tags=("risk",))
                    self._log(f"Could not read {path.name}. {error}")
                if not (self.worker and self.worker.is_alive()):
                    self._set_idle()
            elif kind == "progress":
                self.progress["value"] = int(event[1] * 1000)
            elif kind == "status":
                self.status.configure(text=event[1])
            elif kind == "log":
                self._log(event[1])
            elif kind == "gpu":
                self.gpu_label.configure(text=self._gpu_text())
            elif kind == "done":
                self._finish(event[1], event[2], event[3])
        self.after(80, self._poll_ui)

    def _on_close(self) -> None:
        self._save_settings()
        self.converter.cancel()
        self.destroy()


def main() -> None:
    enable_dpi()
    try:
        toolbox = load_toolbox()
    except FileNotFoundError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PowerPoint Video Converter", str(exc))
        return
    app = App(toolbox)
    app.mainloop()


if __name__ == "__main__":
    main()
