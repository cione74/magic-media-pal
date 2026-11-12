"""Conservative HandBrake planning. Advertised encoders are not hardware proof."""
import re
import subprocess
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from enum import Enum

ENCODERS = {"h264": ("x264", "nvenc_h264"), "h265": ("x265", "nvenc_h265")}
_probe_cache = {}


NVENC_CODECS = {"h264": "h264_nvenc", "h265": "hevc_nvenc"}
_nvenc_cache = (0, {})


class EncodeReadiness(str, Enum):
    READY = "ready"
    NEEDS_EXTRACTION = "needs_extraction"
    NEEDS_PROBE = "needs_probe"
    NEEDS_FALLBACK = "needs_fallback"
    UNSUPPORTED = "unsupported"


class EncodePathsExhausted(ValueError):
    pass


def nvenc_probe(executable="dexter-nvenc"):
    global _nvenc_cache
    if os.environ.get("DEXTER_NVENC") != "1":
        return {}
    now = time.monotonic()
    if _nvenc_cache[1] and now - _nvenc_cache[0] < 120:
        return _nvenc_cache[1]
    results = {}
    for codec, encoder in NVENC_CODECS.items():
        try:
            result = subprocess.run([
                executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=24",
                "-frames:v", "8", "-c:v", encoder, "-f", "null", "-",
            ], capture_output=True, text=True, timeout=10)
            results[codec] = {"available": result.returncode == 0,
                              "reason": result.stderr.strip()[-500:] if result.returncode else ""}
        except (OSError, subprocess.TimeoutExpired) as exc:
            results[codec] = {"available": False, "reason": str(exc)}
    _nvenc_cache = (time.monotonic(), results)
    return results


def cached_probe(executable="HandBrakeCLI"):
    cached = _probe_cache.get(executable)
    if not cached or time.monotonic() - cached[0] >= 30:
        result = probe(executable)
        _probe_cache[executable] = (time.monotonic(), result)
        return result
    return {**cached[1], "nvenc": nvenc_probe()}


def probe(executable="HandBrakeCLI", run=subprocess.run):
    try:
        result = run([executable, "--help"], capture_output=True, text=True,
                     timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "encoders": [], "diagnostic": str(exc)}
    output = result.stdout + result.stderr
    encoders = sorted(name for pair in ENCODERS.values() for name in pair
                      if re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", output))
    return {"available": result.returncode == 0, "encoders": encoders,
            "hardware_verified": False,
            "diagnostic": "NVENC requires a successful sample encode on this host."}


def plan(source, target, capabilities, profile="original", codec="h264",
         hardware=False, verified_hardware=(), quality=None, media=None,
         executable="HandBrakeCLI"):
    if profile == "original":
        return {"command": None, "encoder": None, "source": str(source)}
    if profile not in ("balanced", "compact", "custom") or codec not in ENCODERS:
        raise ValueError("Unsupported quality profile or codec")
    # HDR/10-bit and uninspected inputs must never be silently converted to SDR.
    if not media or media.get("hdr") is True or media.get("bit_depth") != 8:
        raise ValueError("Encoding currently requires inspected 8-bit SDR media")
    if not capabilities.get("available"):
        raise ValueError("HandBrakeCLI is unavailable")
    cpu, gpu = ENCODERS[codec]
    encoder = gpu if hardware and gpu in verified_hardware else cpu
    if encoder not in capabilities.get("encoders", []):
        raise ValueError("Requested encoder is unavailable")
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve() or target.exists():
        raise ValueError("Output must be a new file separate from the original")
    if profile == "custom" and quality is None:
        raise ValueError("Custom quality is required")
    # NVENC quality is not interchangeable with software RF; tune only after testing.
    if encoder == gpu and quality is None:
        raise ValueError("Verified NVENC requires an explicit tested quality")
    quality = float(quality if quality is not None else (20 if profile == "balanced" else 24))
    if not 0 <= quality <= 51:
        raise ValueError("Quality must be between 0 and 51")
    command = [executable, "-i", str(source), "-o", str(target), "-f", "av_mkv",
               "-e", encoder, "-q", str(quality), "--all-audio", "-E", "copy",
               "--audio-fallback", "none", "--all-subtitles", "--markers"]
    return {"command": command, "encoder": encoder,
            "fallback": bool(hardware and encoder == cpu),
            "requires_stream_verification": True}


def progress(line):
    match = re.search(r"Encoding:.*?([0-9]+(?:\.[0-9]+)?)\s*%", line)
    return min(100.0, max(0.0, float(match.group(1)))) if match else None


def inspect(path, executable="ffprobe"):
    result = subprocess.run([executable, "-v", "error", "-show_streams",
                             "-show_format", "-show_chapters", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError("Media inspection failed: " + result.stderr[-2000:])
    return json.loads(result.stdout)


def source_properties(document):
    videos = [s for s in document.get("streams", []) if s.get("codec_type") == "video"]
    if len(videos) != 1 or any(s.get("codec_type") not in ("video", "audio", "subtitle")
                               for s in document.get("streams", [])):
        raise ValueError("Additional video, attachments or data require original-quality output")
    video = videos[0]
    transfer = video.get("color_transfer")
    if transfer in ("bt709", "smpte170m", "bt470bg", "gamma22", "gamma28"):
        hdr = False
    elif transfer in ("smpte2084", "arib-std-b67"):
        hdr = True
    else:
        hdr = None
    pixel_format = video.get("pix_fmt")
    raw_depth = video.get("bits_per_raw_sample")
    depth = int(raw_depth) if str(raw_depth).isdigit() and int(raw_depth) else None
    if pixel_format in ("yuv420p", "yuv422p", "yuv444p", "nv12"):
        depth = 8
    elif depth is None:
        match = re.search(r"p(9|10|12|14|16)(?:le|be)$", pixel_format or "")
        depth = int(match.group(1)) if match else None
    interlaced = video.get("field_order") in ("tt", "bb", "tb", "bt")
    # MPEG-2 CPB describes the old encoder's buffer model, not picture metadata.
    picture_side_data = any(item.get("side_data_type") != "CPB properties"
                            for item in video.get("side_data_list", []))
    protected = picture_side_data or hdr is True or depth not in (None, 8)
    conversion = interlaced or pixel_format != "yuv420p"
    return {"hdr": hdr, "bit_depth": depth, "protected": protected,
            "readiness": (EncodeReadiness.NEEDS_FALLBACK if conversion or protected
                          else EncodeReadiness.NEEDS_PROBE if hdr is None else EncodeReadiness.READY).value,
            "conversion": conversion, "interlaced": interlaced,
            **{key: video.get(key) for key in (
                "codec_name", "pix_fmt", "width", "height", "avg_frame_rate", "r_frame_rate",
                "field_order", "color_range", "color_transfer", "color_primaries", "color_space")},
            "stream_layout": [stream.get("codec_type") for stream in document.get("streams", [])]}


def verify_video(before, after, codec, *, converted=False):
    old = before.get("streams", [])
    new = after.get("streams", [])
    def streams(items, kind):
        return [s for s in items if s.get("codec_type") == kind]
    a, b = streams(old, "video"), streams(new, "video")
    if len(a) != 1 or len(b) != 1 or b[0].get("codec_name") != {"h264": "h264", "h265": "hevc"}[codec]:
        raise ValueError("Encoded video codec or stream count differs from plan")
    original_video, encoded_video = a[0], b[0]
    for key in ("width", "height", "sample_aspect_ratio", "pix_fmt", "color_transfer", "color_primaries", "color_space"):
        if key == "pix_fmt" and converted and encoded_video.get(key) == "yuv420p":
            continue
        if key.startswith("color_") and original_video.get(key) in (None, "unknown") and encoded_video.get(key) not in ("smpte2084", "arib-std-b67"):
            continue
        if original_video.get(key) != encoded_video.get(key):
            raise ValueError("Video property changed: " + key)
    original_range = original_video.get("color_range")
    encoded_range = encoded_video.get("color_range")
    if original_range != encoded_range and (original_range not in (None, "unknown", "unspecified") or encoded_range not in (None, "unknown", "unspecified", "tv")):
        raise ValueError("Video property changed: color_range")
    # HEVC inspection may omit a progressive field-order tag. Reject explicit
    # interlacing, but do not equate a missing container tag with conversion.
    if any(stream.get("field_order") in ("tt", "bb", "tb", "bt") for stream in (b if converted else a + b)):
        raise ValueError("Interlaced video requires original-quality output")


def packet_bounds(path, executable="ffprobe", *, cancelled=lambda: False, update=lambda **fields: None,
                  duration=None, label="Checking media timing"):
    """Read packet timing incrementally and report this file's measured timeline."""
    if duration is None:
        duration = float(inspect(path, executable).get("format", {}).get("duration") or 0)
    command = [executable, "-v", "error", "-show_packets",
               "-show_entries", "packet=stream_index,pts_time,duration_time",
               "-of", "compact=p=0", str(path)]
    bounds = {}
    position = 0.0
    with tempfile.TemporaryDirectory(prefix="dexter-timing-") as scratch:
        packet_file = Path(scratch) / "packets"
        with packet_file.open("wb") as output, packet_file.open("rb") as reader, tempfile.TemporaryFile(mode="w+t") as errors:
            process = subprocess.Popen(command, stdout=output, stderr=errors, start_new_session=True)
            started = last_activity = time.monotonic()
            last_report = 0
            pending = b""
            def consume(line):
                nonlocal position, last_activity
                fields = dict(field.split("=", 1) for field in line.decode(errors="replace").strip().split("|") if "=" in field)
                if fields.get("pts_time") in (None, "N/A") or "stream_index" not in fields:
                    return
                index = int(fields["stream_index"])
                start = float(fields["pts_time"])
                packet_duration = fields.get("duration_time", "N/A")
                end = start + (float(packet_duration) if packet_duration != "N/A" else 0)
                first, last = bounds.get(index, (start, end))
                bounds[index] = (min(first, start), max(last, end))
                position = max(position, start)
                last_activity = time.monotonic()
            try:
                update(pid=process.pid, percent=None, verification_label=label, filename=Path(path).name,
                       elapsed=0, activity_at=time.time(), heartbeat_at=time.time())
                while True:
                    if cancelled():
                        raise InterruptedError("Verification cancelled; original retained")
                    chunk = reader.read(1024 * 1024)
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        consume(line)
                    ended = process.poll() is not None
                    # Drain bytes written just before process exit before finishing.
                    if ended and not chunk:
                        # poll may observe exit after the preceding read reached EOF.
                        tail = reader.read()
                        if tail:
                            pending += tail
                            lines = pending.split(b"\n")
                            pending = lines.pop()
                            for line in lines:
                                consume(line)
                        if pending:
                            consume(pending)
                        break
                    now = time.monotonic()
                    if now - last_report >= 1:
                        update(verification_label=label, processed_seconds=position, media_duration=duration,
                               percent=min(99, position * 100 / duration) if duration > 0 else None,
                               elapsed=now-started, media_speed=position/max(now-started,0.001),
                               eta_seconds=max(0,duration-position)/(position/(now-started)) if position > 0 and now > started else None,
                               activity_at=time.time()-(now-last_activity),
                               heartbeat_at=time.time(), inactive_seconds=now-last_activity)
                        last_report = now
                    if now - started > 600:
                        raise ValueError("Stream timing check timed out")
                    if not chunk:
                        time.sleep(0.2)
                if process.returncode:
                    errors.seek(0)
                    raise ValueError("Could not check stream timing: " + errors.read()[-2000:])
                update(percent=100, processed_seconds=position, media_duration=duration,
                       elapsed=time.monotonic()-started, heartbeat_at=time.time(),
                       activity_at=time.time(), inactive_seconds=0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                update(pid=None)
    return bounds


def verify_packet_timing(before, after, old_bounds, new_bounds):
    for kind in ("video", "audio", "subtitle"):
        old = [old_bounds.get(s["index"]) for s in before["streams"] if s["codec_type"] == kind]
        new = [new_bounds.get(s["index"]) for s in after["streams"] if s["codec_type"] == kind]
        for index, (original, encoded) in enumerate(zip(old, new), start=1):
            if original is None and encoded is None and kind == "subtitle":
                continue
            if original is None or encoded is None or any(
                abs(a - b) > tolerance
                for a, b, tolerance in zip(original, encoded, (0.25, 1.0))
            ):
                raise ValueError(f"{kind.title()} track {index} timing differs from original")


def verify_preservation(before, after, codec, *, converted=False, timing=None):
    """Fail closed before publication; structural checks are not perceptual QA."""
    verify_video(before, after, codec, converted=converted)

    def streams(document, kind):
        return [stream for stream in document.get("streams", []) if stream.get("codec_type") == kind]

    for kind, keys in (("audio", ("codec_name", "channels", "sample_rate")),
                       ("subtitle", ("codec_name",))):
        def signature(stream):
            tags = stream.get("tags", {})
            disposition = stream.get("disposition", {})
            return ([stream.get(key) for key in keys], tags.get("language", "und"),
                    tags.get("title", ""), disposition.get("forced", 0), disposition.get("default", 0))
        if [signature(s) for s in streams(before, kind)] != [signature(s) for s in streams(after, kind)]:
            raise ValueError(kind.title() + " streams or metadata changed")
    old_duration = float(before.get("format", {}).get("duration", 0))
    new_duration = float(after.get("format", {}).get("duration", 0))
    if old_duration <= 0 or new_duration <= 0:
        raise ValueError("Could not verify media duration")
    if timing is not None:
        verify_packet_timing(before, after, *timing)
    elif abs(old_duration - new_duration) > 1:
        raise ValueError("Encoded duration differs from original")
    old_chapters, new_chapters = before.get("chapters", []), after.get("chapters", [])
    if len(old_chapters) != len(new_chapters):
        raise ValueError("Chapter count changed")
    for original, encoded in zip(old_chapters, new_chapters):
        if any(abs(float(original.get(key, 0)) - float(encoded.get(key, 0))) > 0.1
               for key in ("start_time", "end_time")) or original.get("tags", {}) != encoded.get("tags", {}):
            raise ValueError("Chapter timing or metadata changed")


def encode_file(source, target, capabilities, *, profile, codec="h264", quality=None,
                cancelled=lambda: False, update=lambda **fields: None,
                executable="HandBrakeCLI", ffprobe="ffprobe", ffmpeg="ffmpeg", engine="cpu", nvenc="dexter-nvenc"):
    """Encode in private staging, verify, publish without replacing; retain source."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file() or target.is_symlink():
        raise ValueError("Encoding requires a regular staged original and new destination")
    source_identity = source.stat()
    if cancelled():
        raise InterruptedError("Encoding cancelled; original retained")
    update(stage="inspecting", percent=None)
    original = inspect(source, ffprobe)
    properties = source_properties(original)
    update(readiness=properties["readiness"])
    # Conservative capacity reservation; encoding is still stopped if the filesystem fills.
    required = source.stat().st_size * 2 + 1024 ** 3
    if shutil.disk_usage(target.parent).free < required:
        raise ValueError("Insufficient workspace: reserve twice the original size plus 1 GiB")
    if engine not in ("cpu", "gpu", "auto"):
        raise ValueError("Choose CPU, NVIDIA or Automatic encoding")
    if profile == "original":
        return source
    if profile not in ("balanced", "compact", "custom") or codec not in ENCODERS:
        raise ValueError("Choose a supported quality profile and codec")
    if (profile == "custom" and quality is None) or (quality is not None and not 0 <= quality <= 51):
        raise ValueError("Choose a quality between 0 and 51")
    if source.resolve() == target.resolve() or target.exists():
        raise ValueError("Output must be a new file separate from the original")
    if properties["protected"]:
        raise ValueError("Keep the original to preserve HDR, higher bit depth or video side metadata. No preservation path is configured.")
    if properties["conversion"] and properties["bit_depth"] != 8:
        raise ValueError("Dexter could not establish a safe pixel conversion. Your original is ready to review.")

    with tempfile.TemporaryDirectory(prefix=".dexter-encode-", dir=target.parent) as directory:
        temporary = Path(directory) / "encoded.mkv"
        video = next(s for s in original["streams"] if s.get("codec_type") == "video")
        candidates = []

        def ffmpeg_command(gpu, converted):
            quality_value = quality if quality is not None else ((21 if gpu else 20) if profile == "balanced" else (27 if gpu else 24))
            command = [nvenc if gpu else ffmpeg, "-hide_banner", "-nostdin", "-nostats",
                       "-progress", "pipe:1", "-n", "-copyts", "-start_at_zero",
                       "-hwaccel", "none"]
            if converted:
                command += ["-threads", "2"]
            command += ["-i", str(source), "-map", "0:v:0",
                       "-an", "-sn", "-dn", "-fps_mode", "passthrough"]
            if converted:
                filters = ["bwdif=mode=send_frame:parity=auto:deint=all"] if properties["interlaced"] else []
                command += ["-vf", ",".join(filters + ["format=yuv420p"])]
            command += ["-pix_fmt", "yuv420p"]
            if gpu:
                command += ["-c:v", NVENC_CODECS[codec], "-preset", "p5", "-rc", "vbr",
                            "-cq", str(max(1, quality_value)), "-b:v", "0"]
            else:
                command += ["-c:v", "lib" + ENCODERS[codec][0], "-preset", "medium", "-crf", str(quality_value)]
            for field, option in (("color_primaries", "-color_primaries"),
                                  ("color_transfer", "-color_trc"), ("color_space", "-colorspace"),
                                  ("color_range", "-color_range")):
                if video.get(field) not in (None, "unknown", "unspecified"):
                    command += [option, video[field]]
            return command + [str(temporary)]

        gpu_check = capabilities.get("nvenc", {}).get(codec, {})
        if engine in ("gpu", "auto") and gpu_check.get("available"):
            if not properties["conversion"]:
                candidates.append(("NVIDIA", ffmpeg_command(True, False), False, False))
            candidates.append(("NVIDIA with software preparation", ffmpeg_command(True, True), True, True))
        elif engine in ("gpu", "auto"):
            update(log="NVIDIA sample unavailable: " + gpu_check.get("reason", "GPU access is disabled"))
            if engine == "gpu":
                raise ValueError("NVIDIA encoding is unavailable. Choose Automatic to allow a CPU retry.")

        if engine in ("cpu", "auto"):
            if not properties["conversion"] and capabilities.get("available") and ENCODERS[codec][0] in capabilities.get("encoders", []):
                command = plan(source, temporary, capabilities, profile=profile, codec=codec,
                               quality=quality, media=properties, executable=executable)["command"]
                command += ["--crop", "0:0:0:0", "--vfr", "--subtitle-burned=none"]
                threads = int(os.environ.get("RIPPER_ENCODE_THREADS", max(1, (os.cpu_count() or 2) // 2)))
                if not 1 <= threads <= max(1, os.cpu_count() or 1):
                    raise ValueError("Encoding thread count must be within the available CPU count")
                options = ["threads=" + str(threads)] if codec == "h264" else ["pools=" + str(threads), "frame-threads=2"]
                for field, option in (("color_primaries", "colorprim"), ("color_transfer", "transfer"),
                                      ("color_space", "colormatrix")):
                    value = video.get(field)
                    if value not in (None, "unknown", "unspecified") and re.fullmatch(r"[a-zA-Z0-9]+", value):
                        options.append(option + "=" + value)
                candidates.append(("HandBrake", command + ["-x", ":".join(options)], False, False))
            candidates.append(("FFmpeg CPU", ffmpeg_command(False, properties["conversion"]),
                               properties["conversion"], True))

        def run_encoder(command, name, sample=False, stage="encoding"):
            started = time.monotonic()
            diagnostic = ""
            last_activity = started
            last_position = -1
            last_report = 0
            if cancelled():
                raise InterruptedError("Encoding cancelled; original retained")
            # File logging avoids blocked pipe readers during cancellation or a silent encoder.
            with (Path(directory) / "handbrake.log").open("wb") as writer, (Path(directory) / "handbrake.log").open("rb") as log:
                try:
                    process = subprocess.Popen(command, stdout=writer, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                except FileNotFoundError as exc:
                    raise ValueError(f"{name} executable is missing") from exc
                try:
                    update(stage="testing" if sample else stage, pid=process.pid, percent=0,
                           log=f"{name}: {'sample' if sample else 'full encode'}")
                    cursor = 0
                    while True:
                        if cancelled():
                            raise InterruptedError("Encoding cancelled; original retained")
                        log.seek(cursor)
                        chunk = log.read().decode("utf-8", errors="replace")
                        cursor = log.tell()
                        if chunk:
                            diagnostic = (diagnostic + chunk)[-8000:]
                            percentages = [progress(line) for line in chunk.replace("\r", "\n").splitlines()]
                            measured = [p for p in percentages if p is not None]
                            if "-progress" in command:
                                duration = float(original.get("format", {}).get("duration") or 0)
                                times = re.findall(r"^out_time_us=(\d+)$", chunk, re.MULTILINE)
                                if duration > 0 and times:
                                    position = int(times[-1]) / 1000000
                                    measured = [min(100, position * 100 / duration)]
                                    if position > last_position:
                                        last_activity = time.monotonic()
                                        last_position = position
                                    sizes = re.findall(r"^total_size=(\d+)$", chunk, re.MULTILINE)
                                    elapsed = max(time.monotonic()-started, 0.001)
                                    update(written_bytes=int(sizes[-1]) if sizes else None,
                                           media_speed=position/elapsed,
                                           eta_seconds=max(0,duration-position)/(position/elapsed) if position > 0 else None)
                            if "-progress" not in command and measured and measured[-1] > last_position:
                                last_activity = time.monotonic()
                                last_position = measured[-1]
                            update(log=chunk[-8000:], percent=measured[-1] if measured else None,
                                   elapsed=time.monotonic() - started)
                        now = time.monotonic()
                        if now - last_report >= 1:
                            update(elapsed=now-started, heartbeat_at=time.time(),
                                   activity_at=time.time()-(now-last_activity), inactive_seconds=now-last_activity)
                            last_report = now
                        if process.poll() is not None:
                            break
                        if sample and time.monotonic() - started > 30:
                            raise ValueError(f"{name} sample timed out")
                        time.sleep(0.2)
                    if process.returncode:
                        category = "encoding"
                        for label, pattern in (
                            ("storage or permissions", r"permission denied|no space left|read-only file system|input/output error"),
                            ("filtering", r"filter|pixel format"),
                            ("decoding", r"decod|invalid data"),
                            ("stream mapping", r"matches no streams|stream map"),
                            ("container", r"mux|write header"),
                        ):
                            if re.search(pattern, diagnostic, re.I):
                                category = label
                                break
                        if category == "storage or permissions":
                            raise OSError(f"{name} could not read or write the file. Check storage and permissions.")
                        raise ValueError(f"{name} failed during {category} (exit {process.returncode})")
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                    update(pid=None)
        failures = []
        for name, command, converted, sample_first in candidates:
            try:
                if sample_first:
                    update(readiness=EncodeReadiness.NEEDS_FALLBACK.value)
                    sample = Path(directory) / "sample.mkv"
                    run_encoder(command[:-1] + ["-frames:v", "48", str(sample)], name, sample=True)
                    if not sample.is_file() or not sample.stat().st_size:
                        raise ValueError(f"{name} sample produced no video")
                    verify_video(original, inspect(sample, ffprobe), codec, converted=converted)
                    sample.unlink()
                run_encoder(command, name)
                update(encoding_backend=name, encoding_command=command)
                if not temporary.is_file() or not temporary.stat().st_size:
                    raise ValueError(f"{name} produced no video")
                verify_video(original, inspect(temporary, ffprobe), codec, converted=converted)
                break
            except ValueError as exc:
                failures.append(str(exc))
                update(log=str(exc))
                temporary.unlink(missing_ok=True)
                (Path(directory) / "sample.mkv").unlink(missing_ok=True)
        else:
            raise EncodePathsExhausted("The configured encoding paths failed. Your original is ready to review. " + "; ".join(failures))
        update(stage="packaging", percent=0)
        if cancelled():
            raise InterruptedError("Encoding cancelled; original retained")
        # Recover original audio/subtitle packets and metadata rather than depending
        # on HandBrake's track-name/default-disposition normalization.
        remuxed = Path(directory) / "preserved.mkv"
        remux = [ffmpeg, "-v", "error", "-nostdin", "-nostats", "-progress", "pipe:1", "-n", "-i", str(temporary),
                 "-i", str(source), "-map", "0:v:0"]
        retained = [s for s in original["streams"] if s.get("codec_type") in ("audio", "subtitle")]
        for stream in retained:
            remux += ["-map", "1:" + str(stream["index"])]
        remux += ["-c", "copy", "-map_metadata", "1", "-map_chapters", "1"]
        for index, stream in enumerate(retained, start=1):
            flags = "+".join(key for key, value in stream.get("disposition", {}).items() if value) or "0"
            remux += [f"-disposition:{index}", flags,
                      f"-map_metadata:s:{index}", "1:s:" + str(stream["index"])]
        remux += [str(remuxed)]
        if cancelled():
            raise InterruptedError("Encoding cancelled; original retained")
        run_encoder(remux, "Preserving original sound and subtitles", stage="packaging")
        update(stage="verifying", percent=None)
        temporary = remuxed
        inspected = inspect(temporary, ffprobe)
        original_timing = packet_bounds(source, ffprobe, cancelled=cancelled, update=update,
                                        duration=float(original["format"]["duration"]), label="Checking original timing")
        if cancelled():
            raise InterruptedError("Encoding cancelled; original retained")
        encoded_timing = packet_bounds(temporary, ffprobe, cancelled=cancelled, update=update,
                                       duration=float(inspected["format"]["duration"]), label="Checking encoded timing")
        verify_preservation(original, inspected, codec, converted=converted,
                            timing=(original_timing, encoded_timing))
        current_identity = source.stat()
        if source.is_symlink() or any(getattr(source_identity, field) != getattr(current_identity, field)
                                     for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("Original changed during encoding; publication refused")
        if cancelled():
            raise InterruptedError("Encoding cancelled; original retained")
        # Atomic hard-link publication fails if the destination appeared meanwhile.
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, target)
        update(stage="completed", percent=100, readiness=EncodeReadiness.READY.value)
        return target
