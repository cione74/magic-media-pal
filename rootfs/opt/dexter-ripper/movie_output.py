"""Select movie tracks and publish a validated playback copy."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

import encoding


LANGUAGES = {"en": "eng", "de": "deu", "ger": "deu", "fr": "fra", "fre": "fra",
             "es": "spa", "it": "ita", "ja": "jpn", "zh": "zho", "chi": "zho",
             "pt": "por", "nl": "nld", "dut": "nld"}


def language(value):
    value = (value or "und").lower()
    return LANGUAGES.get(value, value)


def track_id(stream):
    # Scoped to the exact stream inventory, so a selection cannot slide to a
    # different track after a scan, remux or source change.
    fields = {key: stream.get(key) for key in
              ("index", "codec_type", "codec_name", "channels", "channel_layout", "sample_rate")}
    fields["tags"] = {key: (stream.get("tags") or {}).get(key) for key in ("title", "language")}
    fields["disposition"] = {key: (stream.get("disposition") or {}).get(key, 0)
                             for key in ("default", "forced", "comment", "visual_impaired")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:24]


def track_choices(document):
    result = []
    counts = {"audio": 0, "subtitle": 0}
    for stream in document.get("streams", []):
        if stream.get("codec_type") not in ("audio", "subtitle"):
            continue
        counts[stream["codec_type"]] += 1
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        channels = stream.get("channels")
        sound = ("Surround" if channels and channels > 2 else "Stereo" if channels == 2 else "Mono" if channels == 1 else "")
        title = tags.get("title") or ""
        codec = {"ac3": "Dolby Digital", "truehd": "Dolby TrueHD",
                 "eac3": "Dolby Digital Plus", "dts": "DTS", "hdmv_pgs_subtitle": "Blu-ray subtitles"}.get(stream.get("codec_name"), stream.get("codec_name", "Unknown"))
        result.append({"id": track_id(stream), "kind": stream["codec_type"],
                       "language": language(tags.get("language")), "channels": channels,
                       "title": title, "disposition": disposition,
                       "channel_layout": stream.get("channel_layout"), "codec": stream.get("codec_name"),
                       "lossless": True if stream.get("codec_name") in ("truehd", "flac", "alac", "pcm_s16le", "pcm_s24le")
                       or "MA" in stream.get("profile", "") else False if stream.get("codec_name") in ("ac3", "eac3", "aac", "mp3") else None,
                       "label": " · ".join(str(v) for v in (
                           ("Sound " if stream["codec_type"] == "audio" else "Subtitle ") + str(counts[stream["codec_type"]]),
                           language(tags.get("language")), codec, sound,
                           stream.get("channel_layout"), title,
                           "Forced" if disposition.get("forced") else "") if v)})
    return result


def scan_tracks(raw):
    """Translate the MakeMKV scan inventory into reviewable track choices."""
    import csv
    fields = {}
    for line in raw.splitlines():
        if not line.startswith("SINFO:"):
            continue
        try:
            title, stream, attribute, _, value = next(csv.reader([line[6:]]))[:5]
            fields.setdefault((int(title), int(stream)), {})[attribute] = value
        except (ValueError, IndexError, StopIteration, csv.Error):
            continue
    inventories = {}
    codecs = {"A_AC3": "ac3", "A_TRUEHD": "truehd", "A_DTS": "dts", "A_EAC3": "eac3",
              "S_HDMV/PGS": "hdmv_pgs_subtitle"}
    for (title, index), tags in sorted(fields.items()):
        kind = {"Audio": "audio", "Subtitles": "subtitle"}.get(tags.get("1"))
        if not kind:
            continue
        stream = {"index": index, "codec_type": kind, "codec_name": codecs.get(tags.get("5"), tags.get("6", "")),
                  "tags": {"language": tags.get("3", "und"), "title": tags.get("2", "")}}
        flags = tags.get("39", "").lower()
        stream["disposition"] = {"default": int("default" in flags), "forced": int("forced" in flags)}

        if kind == "audio":
            try:
                stream["channels"] = int(tags.get("14", 0))
            except ValueError:
                stream["channels"] = 0
            stream["channel_layout"] = tags.get("40")
        inventories.setdefault(title, []).append(stream)
    result = {}
    for title, streams in inventories.items():
        choices = track_choices({"streams": streams})
        for stream, choice in zip(streams, choices):
            matches = [s for s in streams if all(s.get(key) == stream.get(key)
                       for key in ("codec_type", "codec_name", "channels"))
                       and language(s["tags"].get("language")) == language(stream["tags"].get("language"))]
            choice["selector"] = {
                "kind": stream["codec_type"], "codec": stream["codec_name"],
                "language": language(stream["tags"].get("language")), "channels": stream.get("channels"),
                "occurrence": matches.index(stream), "count": len(matches)}
        result[title] = choices
    return result


def from_scan(streams, selector):
    matches = [s for s in streams if s.get("codec_type") == selector["kind"]
               and s.get("codec_name") == selector["codec"]
               and language((s.get("tags") or {}).get("language")) == selector["language"]
               and s.get("channels") == selector["channels"]]
    if len(matches) != selector["count"]:
        raise ValueError("The extracted tracks differ from the disc scan. Please review the sound and subtitle choices.")
    return matches[selector["occurrence"]]


def resolve(document, policy):
    if policy.get("container") not in ("mp4", "mkv"):
        raise ValueError("Choose a supported playback format.")
    audio = [s for s in document.get("streams", []) if s.get("codec_type") == "audio"]
    subtitles = [s for s in document.get("streams", []) if s.get("codec_type") == "subtitle"]
    requested = policy.get("audio_track", "")
    if policy.get("scan_audio") and not requested:
        audio = [from_scan(audio, policy["scan_audio"])]
    elif requested:
        audio = [s for s in audio if track_id(s) == requested]
    else:
        audio = [s for s in audio if language((s.get("tags") or {}).get("language")) == policy["language"]]
        audio = [s for s in audio if not re.search(r"commentary|description|visually impaired",
                    (s.get("tags") or {}).get("title", ""), re.I)
                 and not (s.get("disposition") or {}).get("comment")
                 and not (s.get("disposition") or {}).get("visual_impaired")]
        if policy["surround"]:
            audio = [s for s in audio if s.get("channels", 0) >= 6]
        if policy["container"] == "mp4":
            audio = [s for s in audio if s.get("codec_name") == "ac3"]
        # Use one reproducible preference order. Equivalent matches keep disc
        # order, so automatic mode does not require a manual tie-break.
        audio.sort(key=lambda s: (
            s.get("codec_name") != "ac3",
            not (s.get("disposition") or {}).get("default"),
            abs(s.get("channels", 0) - 6),
        ))
        audio = audio[:1]
    if len(audio) != 1:
        raise ValueError("No matching sound track was found. Choose another language, change the surround requirement, or select a track.")
    audio = audio[0]
    if policy["surround"] and audio.get("channels", 0) < 6:
        raise ValueError("This sound track does not have 5.1 surround. Choose another track or turn off Require surround.")
    if policy["container"] == "mp4" and audio.get("codec_name") != "ac3":
        raise ValueError("The tested MP4 profile needs a Dolby Digital track. Choose one or use MKV to keep the selected sound unchanged.")
    selected_subtitles = []
    if policy["subtitles"]:
        requested = policy.get("subtitle_track", "")
        if requested:
            selected_subtitles = [s for s in subtitles if track_id(s) == requested]
        elif policy.get("scan_subtitle"):
            selected_subtitles = [from_scan(subtitles, policy["scan_subtitle"])]
        else:
            selected_subtitles = [s for s in subtitles
                                  if language((s.get("tags") or {}).get("language")) == policy["subtitle_language"]]
        if len(selected_subtitles) != 1:
            raise ValueError("Choose which subtitle track to keep from the extracted file.")
        if policy["container"] == "mp4":
            raise ValueError("Use MKV to keep the selected subtitles. The tested MP4 profile supports Subtitles off.")
    return [audio] + selected_subtitles


def rebase_policy(before, after, policy):
    """Rebind explicit choices after a verified encode changed stream indexes."""
    selected = resolve(before, policy)
    result = dict(policy)
    for chosen in selected:
        kind = chosen["codec_type"]
        old = [s for s in before["streams"] if s["codec_type"] == kind]
        new = [s for s in after["streams"] if s["codec_type"] == kind]
        # encode_file verifies retained stream order and properties before this.
        if len(old) != len(new):
            raise ValueError("Track inventory changed. Review your sound and subtitle choices.")
        rebound = new[old.index(chosen)]
        for key in ("codec_name", "channels", "sample_rate"):
            if chosen.get(key) != rebound.get(key):
                raise ValueError("Selected track properties changed. Review your choices.")
        if language((chosen.get("tags") or {}).get("language")) != language((rebound.get("tags") or {}).get("language")):
            raise ValueError("Selected track language changed. Review your choices.")
        result["audio_track" if kind == "audio" else "subtitle_track"] = track_id(rebound)
    return result


def preflight(source, folder):
    if source.is_symlink() or not source.is_file():
        raise ValueError("Choose a regular staged movie.")
    try:
        with tempfile.TemporaryFile(dir=folder) as handle:
            handle.write(b"Dexter storage check")
            handle.flush()
    except OSError as exc:
        raise ValueError("Dexter cannot write to temporary storage. Check the folder permissions and try again.") from exc
    if shutil.disk_usage(folder).free < source.stat().st_size + 64 * 1024**2:
        raise ValueError("Temporary storage needs space for another copy of this movie.")


def run(command, cancelled, update, *, timeout=None, duration=0):
    with tempfile.TemporaryDirectory(prefix="dexter-playback-log-") as scratch, \
            open(Path(scratch) / "output", "wb") as writer, \
            open(Path(scratch) / "output", "rb") as log:
        process = subprocess.Popen(command, stdout=writer, stderr=subprocess.STDOUT, start_new_session=True)
        started = last_activity = time.monotonic()
        last_report = position = 0
        progress_tail = ""
        try:
            update(pid=process.pid)
            cursor = 0
            while True:
                if cancelled():
                    raise InterruptedError("Processing cancelled. Your staged source is ready to retry.")
                log.seek(cursor)
                chunk = log.read().decode(errors="replace")
                cursor = log.tell()
                if chunk:
                    update(log=chunk[-4000:])
                    progress_tail += chunk
                    lines = progress_tail.split("\n")
                    progress_tail = lines.pop()
                    matches = re.findall(r"^out_time_us=(\d+)$", "\n".join(lines), re.M)
                    if matches and duration:
                        measured = int(matches[-1]) / 1000000
                        if measured > position:
                            last_activity = time.monotonic()
                        position = max(position, measured)
                        speed = position / max(time.monotonic()-started, 0.001)
                        update(percent=min(99, position * 100 / duration), media_speed=speed,
                               eta_seconds=max(0, duration-position)/speed if speed else None)
                    sizes = re.findall(r"^total_size=(\d+)$", "\n".join(lines), re.M)
                    if sizes:
                        update(written_bytes=int(sizes[-1]))
                now = time.monotonic()
                if now - last_report >= 1:
                    update(elapsed=now-started, heartbeat_at=time.time(),
                           activity_at=time.time()-(now-last_activity), inactive_seconds=now-last_activity)
                    last_report = now
                if process.poll() is not None:
                    break
                if timeout and time.monotonic() - started > timeout:
                    raise ValueError("The playback check took too long. Your staged source is ready to review.")
                time.sleep(0.2)
            if process.returncode:
                log.seek(0)
                detail = log.read().decode(errors="replace")[-4000:]
                update(log=detail)
                if re.search(r"permission denied|read-only file system", detail, re.I):
                    raise ValueError("Dexter cannot write this copy. Check storage permissions.")
                if "No space left" in detail:
                    raise ValueError("Temporary storage is full. Free space and try again.")
                raise ValueError("The media operation failed. Your staged source is ready to review. See Technical details.")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            update(pid=None)


def validate(before, after, selected, container):
    videos = [s for s in before["streams"] if s["codec_type"] == "video"]
    actual_video = [s for s in after["streams"] if s["codec_type"] == "video"]
    if len(videos) != 1 or len(actual_video) != 1:
        raise ValueError("Expected exactly one video track.")
    video = videos[0]
    if video["codec_name"] not in ("hevc", "h264"):
        raise ValueError("This playback profile needs HEVC or H.264 video.")
    encoding.verify_video(before, after, "h265" if video["codec_name"] == "hevc" else "h264")
    media = [s for s in after["streams"] if s["codec_type"] in ("video", "audio", "subtitle")]
    chapter_data = [s for s in after["streams"] if s["codec_type"] == "data"
                    and s.get("codec_tag_string") == "text" and after.get("chapters")]
    if len(media) != 1 + len(selected) or len(after["streams"]) != len(media) + len(chapter_data):
        raise ValueError("The output contains unexpected tracks.")
    if container == "mp4" and video["codec_name"] == "hevc" and actual_video[0].get("codec_tag_string") != "hvc1":
        raise ValueError("The HEVC playback tag was not written.")
    for original, result in zip(selected, media[1:]):
        for key in ("codec_type", "codec_name", "channels", "channel_layout", "sample_rate"):
            if original.get(key) != result.get(key):
                raise ValueError("Selected sound or subtitle properties changed: " + key)
        if language((original.get("tags") or {}).get("language")) != language((result.get("tags") or {}).get("language")):
            raise ValueError("Selected track language changed.")
        if original.get("codec_type") == "subtitle" and (original.get("disposition") or {}).get("forced", 0) != (result.get("disposition") or {}).get("forced", 0):
            raise ValueError("Selected forced subtitles changed.")
    old_chapters, new_chapters = before.get("chapters", []), after.get("chapters", [])
    if len(old_chapters) != len(new_chapters):
        raise ValueError("Chapter count changed during packaging.")
    for old, new in zip(old_chapters, new_chapters):
        if any(abs(float(old[key]) - float(new[key])) > 0.1 for key in ("start_time", "end_time")) or old.get("tags", {}).get("title") != new.get("tags", {}).get("title"):
            raise ValueError("Chapter timing or title changed during packaging.")


def prepare(source, target, policy, *, cancelled=lambda: False, update=lambda **fields: None, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    source, target = Path(source), Path(target)
    if cancelled():
        raise InterruptedError("Processing cancelled. Your staged source is ready to retry.")
    if target.suffix != "." + policy["container"] or target.exists() or target.is_symlink():
        raise ValueError("Choose a new playback-copy destination.")
    preflight(source, target.parent)
    identity = source.stat()
    before = encoding.inspect(source, ffprobe)
    selected = resolve(before, policy)
    started = time.monotonic()
    metrics = {"source_bytes": identity.st_size, "settings": policy, "selected_tracks": track_choices({"streams": selected})}
    with tempfile.TemporaryDirectory(prefix=".dexter-package-", dir=target.parent) as scratch:
        output = Path(scratch) / ("movie." + policy["container"])
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-nostats",
                   "-progress", "pipe:1", "-n", "-i", str(source), "-map", "0:v:0"]
        for stream in selected:
            command += ["-map", "0:" + str(stream["index"])]
        command += ["-c", "copy", "-map_chapters", "0", "-map_metadata", "0", "-disposition:a:0", "default"]
        if policy["container"] == "mp4":
            command += ["-sn", "-dn", "-movflags", "+faststart+write_colr"]
            video = next(s for s in before["streams"] if s["codec_type"] == "video")
            for field, option in (("color_range", "-color_range"), ("color_primaries", "-color_primaries"),
                                  ("color_transfer", "-color_trc"), ("color_space", "-colorspace")):
                if video.get(field) not in (None, "unknown", "unspecified"):
                    command += [option, video[field]]
            if next(s for s in before["streams"] if s["codec_type"] == "video")["codec_name"] == "hevc":
                command += ["-tag:v", "hvc1"]
        command += [str(output)]
        update(stage="packaging", percent=0)
        run(command, cancelled, update, duration=float(before["format"]["duration"]))
        metrics["packaging_seconds"] = time.monotonic() - started
        started = time.monotonic()
        update(stage="validating", percent=None)
        after = encoding.inspect(output, ffprobe)
        validate(before, after, selected, policy["container"])
        expected = copy.deepcopy(before)
        expected["streams"] = [s for s in before["streams"] if s["codec_type"] == "video"] + selected
        old_bounds = encoding.packet_bounds(source, ffprobe, cancelled=cancelled, update=update,
                                            duration=float(before["format"]["duration"]), label="Checking source timing")
        if cancelled():
            raise InterruptedError("Processing cancelled. Your staged source is ready to retry.")
        new_bounds = encoding.packet_bounds(output, ffprobe, cancelled=cancelled, update=update,
                                            duration=float(after["format"]["duration"]), label="Checking playback timing")
        encoding.verify_packet_timing(expected, after, old_bounds, new_bounds)
        # Exercise both decoding and seeking at three points in the finished file.
        duration = float(after["format"]["duration"])
        for sample, position in enumerate((0, duration / 2, max(0, duration - 15)), start=1):
            update(stage="validating", percent=None, verification_label=f"Checking playback sample {sample} of 3",
                   processed_seconds=None, media_duration=None)
            run([ffmpeg, "-v", "error", "-xerror", "-nostdin", "-ss", str(position),
                 "-i", str(output), "-t", "3", "-map", "0:v:0", "-map", "0:a:0",
                 "-f", "null", "-"], cancelled, update, timeout=60)
        metrics["validation_seconds"] = time.monotonic() - started
        metrics["output_bytes"] = output.stat().st_size
        metrics["validation"] = "Stream selection, surround, chapters, timing and three decode/seek samples passed"
        if metrics["output_bytes"] <= 0:
            raise ValueError("The playback copy is empty.")
        current = source.stat()
        if source.is_symlink() or any(getattr(identity, field) != getattr(current, field)
                                     for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("The staged source changed. Publication refused.")
        if cancelled():
            raise InterruptedError("Processing cancelled. Your staged source is ready to retry.")
        with output.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(output, target)
        update(report=metrics)
        return target
