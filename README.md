# PowerPoint Video Converter

Turns videos into small H.264 MP4 files that PowerPoint can insert. The picture size stays the same, an NVIDIA GPU is used when the driver supports it, and the new file keeps the original name with an `.mp4` extension.

Motion-JPEG AVI files at high frame rates are a common reason PowerPoint crashes. This app writes a normal MP4 instead: H.264, 8-bit 4:2:0, constant frame rate, and the index at the start of the file.

## Run

Double-click `Convert for PowerPoint.bat`, or from this folder:

```
python app.py
```

Python 3 is required. Drag-and-drop needs the optional package `tkinterdnd2`:

```
pip install tkinterdnd2
```

The app still runs without that package. Use Add videos or Add folder instead.

## FFmpeg

`ffmpeg` and `ffprobe` need to be on your PATH, or in `tools/ffmpeg-*/bin/` next to the app.

GPU encoding only works when the FFmpeg build matches the installed NVIDIA driver. FFmpeg 8.0.x works with NVENC API 13.0 (NVIDIA driver 570 or newer). FFmpeg 9 needs driver 610 or newer. If the GPU encoder cannot start, the app encodes with the CPU instead.
