import copy
import credits
import request_security
import hashlib
from flask import Flask, render_template_string, redirect, request, url_for
import subprocess
import threading
import tempfile
import time
import csv
import concurrent.futures
import json
import os
import fcntl
import re
import shutil
import signal
import stat
import urllib.request
import urllib.error
import urllib.parse
import uuid
import encoding
import movie_output
import workflow_wizard
import workflow_presentation
from werkzeug.datastructures import MultiDict
import product
import readiness
import library_connection
import storage_setup
import makemkv_runtime
import disc_compression
import disc_intelligence
import shlex
import sys
from pathlib import Path

app = Flask(__name__)
STORAGE_FORM_TOKEN = uuid.uuid4().hex

DRIVE = os.environ.get("RIPPER_DRIVE", "/dev/sr1")
DRIVE_NAME = os.environ.get(
    "RIPPER_DRIVE_NAME",
    "HL-DT-ST WH16NS40"
)
MAKEMKV = os.environ.get("MAKEMKV_CLI", "/opt/makemkv/bin/makemkvcon")
CONFIG_ROOT = Path(os.environ.get("RIPPER_CONFIG", "/config")).resolve()
OUTPUT = Path(os.environ.get("RIPPER_OUTPUT", "/output")).resolve()
MEDIA_ROOT = Path(os.environ.get("RIPPER_MEDIA", "/media")).resolve()
readiness.validate_roots(CONFIG_ROOT, OUTPUT, MEDIA_ROOT)
request_security.install(app, CONFIG_ROOT)
VIDEO_OUTPUT = OUTPUT / "video"
AUDIO_OUTPUT = OUTPUT / "audio"
MUSIC_ROOT = MEDIA_ROOT / "music"
MUSIC_IMPORT_ROOT = MEDIA_ROOT / ".dexter-music-import"
CD_OFFSET = os.environ.get("CD_OFFSET", "").strip()
JELLYFIN_URL = os.environ.get(
    "JELLYFIN_URL",
    ""
).rstrip("/")
JELLYFIN_KEY_FILE = CONFIG_ROOT / "jellyfin-api-key"
SETTINGS_SCHEMA = 1
SETTINGS_FILE = CONFIG_ROOT / "dexter-ripper-settings.json"
SETTINGS_TMP = CONFIG_ROOT / ".dexter-ripper-settings.json.tmp"


QUALITY_DEFAULTS = {"original": "Keep original", "balanced": "Balanced", "compact": "Compact"}
MOVIE_DESTINATIONS = {"jellyfin": "Jellyfin Movies", "staging": "Keep in staging"}


def default_settings():
    return {
        "setup_complete": False,
        "drive_device": DRIVE,
        "drive_name": DRIVE_NAME,
        "cd_offset": CD_OFFSET,
        "jellyfin_url": JELLYFIN_URL,
        "movie_quality": "original",
        "movie_destination": "jellyfin",
    }


def load_settings():
    values = default_settings()

    if not SETTINGS_FILE.exists():
        return values

    try:
        document = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))

        if document.get("schema") != SETTINGS_SCHEMA:
            raise ValueError("unsupported settings schema")

        saved = document.get("settings")

        if not isinstance(saved, dict):
            raise ValueError("saved settings are not a dictionary")

        for key in values:
            if key in saved:
                values[key] = saved[key]

        values["setup_complete"] = bool(values["setup_complete"])
        return values

    except Exception as exc:
        print(f"Dexter settings could not be loaded: {exc}", flush=True)
        return values


settings = load_settings()


def save_settings(values):
    document = {
        "schema": SETTINGS_SCHEMA,
        "settings": values,
    }
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"

    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with SETTINGS_TMP.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())

    os.chmod(SETTINGS_TMP, 0o600)
    os.replace(SETTINGS_TMP, SETTINGS_FILE)


def configured_drive_name():
    selected = configured_drive()

    for drive in detected_optical_drives():
        if drive["device"] == selected:
            return drive["name"]

    return str(settings.get("drive_name") or DRIVE_NAME).strip()


def configured_drive():
    return str(settings.get("drive_device") or DRIVE).strip()


def detected_optical_drives():
    drives = []

    for entry in sorted(Path("/sys/class/block").glob("sr*")):
        device = Path("/dev") / entry.name

        try:
            if not device.exists() or not stat.S_ISBLK(device.stat().st_mode):
                continue
        except OSError:
            continue

        device_path = entry / "device"
        vendor = _read_sysfs_text(device_path / "vendor")
        model = _read_sysfs_text(device_path / "model")
        firmware = _read_sysfs_text(device_path / "rev")
        name = " ".join(
            part for part in (vendor, model, firmware) if part
        ) or entry.name

        drives.append({
            "device": str(device),
            "name": re.sub(r"\s+", " ", name).strip(),
        })

    return drives


def configured_cd_offset():
    return str(settings.get("cd_offset") or "").strip()


def configured_jellyfin_url():
    return str(settings.get("jellyfin_url", JELLYFIN_URL) or "").rstrip("/")

OUTPUT.mkdir(parents=True, exist_ok=True)
(OUTPUT / "video").mkdir(exist_ok=True)
(OUTPUT / "audio").mkdir(exist_ok=True)

state = {
    "disc": None,
    "job": {
        "running": False,
        "description": "",
        "status": "",
        "log": [],
        "output_file": None,
        "verification": None,
        "outputs": [],
        "title_queue": [],
        "metadata_results": [],
        "metadata_draft": {},
    }
}

lock = threading.Lock()


def run_command(args, timeout=180, cwd=None):
    """
    Run an external command without capturing stdout/stderr through pipes.

    MakeMKV may leave inherited pipe descriptors open in helper processes.
    Using a temporary file avoids subprocess.communicate() waiting for EOF
    from those descriptors after the primary command has completed.
    """
    if args and (args[0] == MAKEMKV or Path(args[0]).name == 'makemkvcon'):
        return makemkv_runtime.run(args, CONFIG_ROOT, configured_drive(), timeout=timeout,
                                   cwd=cwd, on_line=update_makemkv_status)
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            p = subprocess.run(
                args,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                cwd=cwd
            )

            output.seek(0)
            text = output.read().decode("utf-8", errors="replace")

            return p.returncode, text

        except subprocess.TimeoutExpired:
            output.seek(0)
            text = output.read().decode("utf-8", errors="replace")

            return 124, text + "\nCommand timed out."

def makemkv_scan_timeout():
    try:
        return max(60, min(3600, int(os.environ.get('RIPPER_MAKEMKV_SCAN_TIMEOUT', '900'))))
    except ValueError:
        return 900


def update_makemkv_status(line, include_log=True):
    lower = line.lower()
    with lock:
        if not state['job'].get('running'):
            return
        if include_log:
            state['job'].setdefault('log', []).append(line.rstrip())
            state['job']['log'] = state['job']['log'][-80:]
        if 'downloading latest hk' in lower:
            state['job']['status'] = 'Updating UHD disc keys — this can take several minutes. Please wait…'
        elif 'downloading latest sdf' in lower:
            state['job']['status'] = 'Updating MakeMKV drive support data — please wait…'
        elif line.startswith(('PRGT:', 'TINFO:', 'TCOUNT:')):
            state['job']['status'] = ('Reading disc titles…' if state['job'].get('description') == 'Disc scan'
                                      else 'Extracting selected video title…')


def duration_seconds(value):
    try:
        parts = [int(x) for x in value.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    except Exception:
        pass
    return 0


def analyze_titles(titles):
    if not titles:
        return

    for t in titles:
        t["duration_seconds"] = duration_seconds(t.get("duration", ""))
        t.setdefault("segment_count", 0)
        t.setdefault("segments", "")

        seconds = t["duration_seconds"]

        if seconds >= 45 * 60:
            t["category"] = "Long-form"
        elif seconds >= 20 * 60:
            t["category"] = "Featurette"
        else:
            t["category"] = "Extra"

        t["recommendation"] = ""
        t["reason"] = ""

    long_titles = [
        t for t in titles
        if t["duration_seconds"] >= 45 * 60
    ]

    if not long_titles:
        return

    max_duration = max(t["duration_seconds"] for t in long_titles)

    # Titles within 2 minutes of the longest feature are considered
    # alternate main-feature candidates.
    candidates = [
        t for t in long_titles
        if max_duration - t["duration_seconds"] <= 120
    ]

    if not candidates:
        return

    # When runtimes are effectively identical, prefer the simpler playlist.
    # This is a recommendation, not a claim that alternate playlists are wrong.
    candidates.sort(
        key=lambda t: (
            t.get("segment_count", 999) if t.get("segment_count", 0) else 999,
            abs(max_duration - t["duration_seconds"]),
            t["id"],
        )
    )

    primary = candidates[0]
    primary["recommendation"] = "MAIN FEATURE CANDIDATE"

    if primary.get("segment_count") == 1:
        primary["reason"] = "Preferred: simple single-segment playlist"
    else:
        primary["reason"] = "Preferred main-feature candidate"

    for t in candidates[1:]:
        t["recommendation"] = "ALTERNATE MAIN FEATURE"
        if t.get("segment_count", 0) > 1:
            t["reason"] = (
                f'Branched playlist: {t["segment_count"]} segments'
            )
        else:
            t["reason"] = "Alternate full-length playlist"



def _read_sysfs_text(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def _makemkv_message_text(line):
    if not line.startswith("MSG:"):
        return line.strip()

    try:
        row = next(csv.reader([line.split(":", 1)[1]]))
        if len(row) > 3:
            return row[3].strip()
    except Exception:
        pass

    return line.strip()


def _normalized_drive_identity(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def configured_makemkv_source(drive=None):
    drive = drive or configured_drive()
    rc, text = run_command(
        [MAKEMKV, "-r", "--cache=1", "info", "disc:9999"],
        timeout=makemkv_scan_timeout(),
    )
    candidates = []
    if rc == 124:
        raise RuntimeError('MakeMKV did not finish preparing the drive within the scan limit. Check network access and the scan log before retrying; this is not a firmware diagnosis.')

    for line in text.splitlines():
        if not line.startswith("DRV:"):
            continue

        try:
            row = next(csv.reader([line.split(":", 1)[1]]))
            index = int(row[0])
            description = row[4].strip()
            reported_device = row[6].strip()
        except (IndexError, ValueError, csv.Error):
            continue

        if description:
            candidates.append({
                "index": index,
                "description": description,
                "device": reported_device,
            })

    if not candidates:
        detail = _makemkv_message_text(text.splitlines()[-1]) if text else ""
        raise RuntimeError(
            "MakeMKV could not find an optical drive."
            + (f" {detail}" if detail else "")
        )

    exact = [item for item in candidates if item["device"] == drive]

    if len(exact) == 1:
        return f'disc:{exact[0]["index"]}'

    device_path = Path("/sys/class/block") / Path(drive).name / "device"
    identity = " ".join(
        value for value in (
            _read_sysfs_text(device_path / "vendor"),
            _read_sysfs_text(device_path / "model"),
        ) if value
    )
    normalized_identity = _normalized_drive_identity(identity)
    matching = [
        item for item in candidates
        if normalized_identity
        and normalized_identity in _normalized_drive_identity(
            item["description"]
        )
    ]

    if len(matching) == 1:
        return f'disc:{matching[0]["index"]}'

    if len(candidates) == 1:
        return f'disc:{candidates[0]["index"]}'

    raise RuntimeError(
        f"MakeMKV found {len(candidates)} optical drives but could not match "
        f"the selected device {drive}."
    )


def collect_drive_diagnostics(raw="", disc=None):
    disc = disc or {}

    drive = configured_drive()
    block_name = Path(drive).name
    device_path = Path("/sys/class/block") / block_name / "device"

    vendor = _read_sysfs_text(device_path / "vendor")
    model = _read_sysfs_text(device_path / "model")
    firmware = _read_sysfs_text(device_path / "rev")

    sg_device = ""

    try:
        sg_dir = device_path / "scsi_generic"
        sg_entries = sorted(sg_dir.iterdir())

        if sg_entries:
            sg_device = f"/dev/{sg_entries[0].name}"
    except Exception:
        pass

    makemkv_version = ""

    match = re.search(
        r"MakeMKV v([^\s]+)",
        raw,
        re.IGNORECASE
    )

    if match:
        makemkv_version = match.group(1)

    libredrive_enabled = False
    libredrive_detail = ""

    match = re.search(
        r"Using LibreDrive mode\s*\(([^)]*)\)",
        raw,
        re.IGNORECASE
    )

    if match:
        libredrive_enabled = True
        libredrive_detail = match.group(1).strip()

    protection_messages = []
    java_bdjava = "Not reported"

    protection_terms = (
        "aacs",
        "bd+",
        "bd-j",
        "screen pass",
        "protection",
        "content hash",
        "hash check",
        "corrupt",
        "java",
    )

    for line in raw.splitlines():
        lower = line.lower()

        if "java" in lower or "bd-j" in lower:
            java_bdjava = _makemkv_message_text(line)

        if any(term in lower for term in protection_terms):
            message = _makemkv_message_text(line)

            if message and message not in protection_messages:
                protection_messages.append(message)

    return {
        "drive_vendor": vendor,
        "drive_model": model,
        "firmware": firmware,
        "device": drive,
        "sg_device": sg_device,
        "makemkv_version": makemkv_version,
        "libredrive_enabled": libredrive_enabled,
        "libredrive_detail": libredrive_detail,
        "media_type": disc.get("type", ""),
        "disc_title": (
            disc.get("title")
            or disc.get("label")
            or ""
        ),
        "java_bdjava": java_bdjava,
        "protection_messages": protection_messages[:8],
    }


def scan_video_hints(text):
    """Summarize what MakeMKV saw without treating a scan as an encode check."""
    streams = {}
    for line in text.splitlines():
        if not line.startswith("SINFO:"):
            continue
        try:
            title, stream, field, _, value = next(csv.reader([line[6:]]))[:5]
            if field in ("5", "6", "7", "19"):
                streams.setdefault((int(title), int(stream)), {})[field] = value
        except (ValueError, csv.Error, StopIteration):
            continue

    hints = {}
    for (title, _), fields in streams.items():
        codec = fields.get("5", "")
        if not codec.startswith("V_") or title in hints:
            continue
        description = fields.get("7") or fields.get("6") or codec
        resolution = fields.get("19", "")
        hints[title] = " · ".join(part for part in (resolution, description) if part)
    return hints


def parse_makemkv(text):
    disc = {
        "kind": "video",
        "type": "Video disc",
        "title": "",
        "label": "",
        "titles": [],
        "raw": text
    }

    title_data = {}

    for line in text.splitlines():
        try:
            if line.startswith("CINFO:"):
                row = next(csv.reader([line.split(":", 1)[1]]))
                code = int(row[0])
                value = row[2] if len(row) > 2 else ""

                if code == 1:
                    disc["type"] = value
                elif code == 2:
                    disc["title"] = value
                elif code == 32:
                    disc["label"] = value

            elif line.startswith("TINFO:"):
                row = next(csv.reader([line.split(":", 1)[1]]))
                tid = int(row[0])
                code = int(row[1])
                value = row[3] if len(row) > 3 else ""

                t = title_data.setdefault(tid, {"id": tid})

                if code == 2:
                    t["name"] = value
                elif code == 8:
                    t["chapters"] = value
                elif code == 9:
                    t["duration"] = value
                elif code == 10:
                    t["size"] = value
                elif code == 16:
                    t["source"] = value
                elif code == 25:
                    try:
                        t["segment_count"] = int(value)
                    except ValueError:
                        t["segment_count"] = 0
                elif code == 26:
                    t["segments"] = value
                elif code == 27:
                    t["filename"] = value

        except Exception:
            continue

    disc["titles"] = [
        title_data[k] for k in sorted(title_data.keys())
    ]

    analyze_titles(disc["titles"])

    disc["diagnostics"] = collect_drive_diagnostics(text, disc)

    return disc


CDROM_DRIVE_STATUS = 0x5326
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4


def optical_drive_status(device):
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
        finally:
            os.close(fd)
    except OSError:
        return CDS_DRIVE_NOT_READY


def wait_for_optical_ready(device, timeout=30):
    deadline = time.monotonic() + timeout
    consecutive_ready = 0
    last_status = CDS_NO_INFO

    while time.monotonic() < deadline:
        last_status = optical_drive_status(device)

        if last_status == CDS_DISC_OK:
            consecutive_ready += 1
            if consecutive_ready >= 2:
                return True, last_status
        else:
            consecutive_ready = 0

        with lock:
            if last_status == CDS_TRAY_OPEN:
                state["job"]["status"] = "Waiting for tray to close…"
            elif last_status == CDS_NO_DISC:
                state["job"]["status"] = "Waiting for disc…"
            else:
                state["job"]["status"] = "Waiting for optical drive to become ready…"

        time.sleep(1)

    return False, last_status


def parse_cyanrip(text):
    discid = re.search(r"^DiscID:\s*(\S+)\s*$", text, re.MULTILINE)
    count = re.search(r"^Disc tracks:\s*(\d+)\s*$", text, re.MULTILINE)
    tracks = []

    for match in re.finditer(
        r"^Track (\d+) info:\s*\n(.*?)(?=^Track \d+ info:|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        number = int(match.group(1))
        duration = re.search(
            r"^\s*Duration:\s*([\d:.]+)",
            match.group(2),
            re.MULTILINE,
        )
        title = re.search(
            r"^\s*title:\s*(.+?)\s*$",
            match.group(2),
            re.MULTILINE | re.IGNORECASE,
        )
        isrc = re.search(
            r"^\s*isrc:\s*(\S+)\s*$",
            match.group(2),
            re.MULTILINE | re.IGNORECASE,
        )
        parsed_title = title.group(1).strip() if title else ""

        if not parsed_title or parsed_title.lower() == "unknown track":
            parsed_title = f"Track {number}"

        tracks.append({
            "number": number,
            "title": parsed_title,
            "duration": duration.group(1) if duration else "",
            "isrc": isrc.group(1) if isrc else "",
        })

    if (
        not count
        or not tracks
        or len(tracks) != int(count.group(1))
        or len({track["number"] for track in tracks}) != len(tracks)
    ):
        return None

    album = re.search(
        r"^Album:\s*(.+?)\s*$",
        text,
        re.MULTILINE | re.IGNORECASE,
    )
    parsed_album = album.group(1).strip() if album else ""

    if parsed_album.lower().startswith("unknown disc"):
        parsed_album = ""

    has_cd_text = bool(parsed_album) or any(
        not track["title"].startswith("Track ") for track in tracks
    )

    return {
        "kind": "audio",
        "type": "Audio CD",
        "title": "Audio CD",
        "label": "",
        "titles": [],
        "raw": text,
        "scan_id": uuid.uuid4().hex,
        "discid": discid.group(1) if discid else "",
        "tracks": tracks,
        "selected_tracks": [track["number"] for track in tracks],
        "album": parsed_album,
        "album_artist": "",
        "year": "",
        "metadata_message": (
            "Loaded metadata stored on the CD (CD-TEXT)."
            if has_cd_text
            else ""
        ),
        "metadata_provider": "CD-TEXT" if has_cd_text else "",
    }


def _musicbrainz_artist(credits):
    return "".join(
        credit.get("name", credit.get("artist", {}).get("name", ""))
        + credit.get("joinphrase", "")
        for credit in credits
    )


def _apply_audio_metadata(disc, album, artist, year, titles, provider):
    if not album or not titles:
        return False

    disc["album"] = album
    disc["album_artist"] = artist
    disc["year"] = year
    disc["title"] = album
    disc["metadata_provider"] = provider
    disc.pop("metadata_failures", None)

    for track in disc["tracks"]:
        track["title"] = titles.get(
            track["number"], track.get("title") or f"Track {track['number']}"
        )

    disc["metadata_message"] = f"Album and track names provided by {provider}."
    return True


def _lookup_musicbrainz(disc):
    if not disc.get("discid"):
        return False, "MusicBrainz: no disc ID"

    url = (
        "https://musicbrainz.org/ws/2/discid/"
        f"{urllib.parse.quote(disc['discid'], safe='')}"
        "?inc=recordings+artist-credits&fmt=json"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DexterRipper/0.1",
            "Accept": "application/json",
        },
    )

    try:
        payload = None

        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 503 or attempt == 1:
                    raise
                time.sleep(1.1)

        physical_numbers = {track["number"] for track in disc["tracks"]}
        matches = []

        for release in payload.get("releases", []):
            for medium in release.get("media", []):
                if not any(
                    item.get("id") == disc["discid"]
                    for item in medium.get("discs", [])
                ):
                    continue

                titles = {
                    int(track["position"]): (
                        track.get("title")
                        or track.get("recording", {}).get("title", "")
                    )
                    for track in medium.get("tracks", [])
                }

                if set(titles) != physical_numbers:
                    continue

                matches.append((release, titles))

        if matches:
            release, titles = matches[0]
            matched = _apply_audio_metadata(
                disc,
                release.get("title", ""),
                _musicbrainz_artist(release.get("artist-credit", [])),
                (release.get("date") or "")[:4],
                titles,
                "MusicBrainz",
            )
            return (
                matched,
                "" if matched else "MusicBrainz: incomplete metadata",
            )

        return False, "MusicBrainz: no exact disc match"

    except Exception as exc:
        return False, f"MusicBrainz: {exc}"


def _deezer_track(isrc):
    if not isrc:
        return None

    req = urllib.request.Request(
        f"https://api.deezer.com/track/isrc:{urllib.parse.quote(isrc)}",
        headers={"User-Agent": "DexterRipper/0.1"},
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        payload = json.load(response)

    if payload.get("error") or not payload.get("title"):
        return None

    return payload


def _lookup_deezer(disc):
    tracks_with_isrc = [
        track for track in disc["tracks"] if track.get("isrc")
    ]

    if not tracks_with_isrc:
        return False, "Deezer: no track ISRCs on this CD"

    results = {}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_tracks = {
                executor.submit(_deezer_track, track["isrc"]): track
                for track in tracks_with_isrc
            }

            for future, track in future_tracks.items():
                try:
                    result = future.result()
                except Exception:
                    result = None

                if result:
                    results[track["number"]] = result

        if not results:
            return False, "Deezer: no ISRC matches"

        album_counts = {}

        for result in results.values():
            album = result.get("album") or {}
            album_id = album.get("id")

            if album_id:
                album_counts[album_id] = album_counts.get(album_id, 0) + 1

        best_album_id = (
            max(album_counts, key=album_counts.get)
            if album_counts
            else None
        )
        representative = next(
            (
                result for result in results.values()
                if (result.get("album") or {}).get("id") == best_album_id
            ),
            next(iter(results.values())),
        )
        album = representative.get("album") or {}
        artist = representative.get("artist") or {}
        titles = {
            number: result.get("title", "")
            for number, result in results.items()
        }
        matched = _apply_audio_metadata(
            disc,
            album.get("title", ""),
            artist.get("name", ""),
            (
                representative.get("release_date")
                or album.get("release_date")
                or ""
            )[:4],
            titles,
            "Deezer",
        )

        if matched:
            disc["metadata_message"] = (
                f"Album information and {len(titles)} of {len(disc['tracks'])} "
                "track names provided by Deezer."
            )
            return True, ""

        return False, "Deezer: incomplete album metadata"

    except Exception as exc:
        return False, f"Deezer: {exc}"


def lookup_audio_metadata(disc):
    cd_text_message = disc.get("metadata_message", "")
    failures = []

    for lookup in (_lookup_musicbrainz, _lookup_deezer):
        matched, message = lookup(disc)

        if matched:
            return True

        if message:
            failures.append(message)

    if cd_text_message:
        disc["metadata_message"] = (
            f"{cd_text_message} Online lookup was unavailable."
        )
    else:
        disc["metadata_message"] = (
            "No online match was available. You can try again without "
            "rescanning the disc."
        )

    disc["metadata_failures"] = failures
    return False


def scan_disc():
    drive = configured_drive()
    ready, drive_status = wait_for_optical_ready(drive)

    if not ready:
        status_names = {
            CDS_NO_INFO: "No drive status available",
            CDS_NO_DISC: "No disc detected",
            CDS_TRAY_OPEN: "Tray is open",
            CDS_DRIVE_NOT_READY: "Drive not ready",
        }

        return {
            "kind": "unknown",
            "type": status_names.get(drive_status, "Drive not ready"),
            "title": "",
            "label": "",
            "titles": [],
            "raw": f"Optical drive readiness timeout; status={drive_status}"
        }

    with lock:
        state["job"]["status"] = "Reading disc structure with MakeMKV…"

    try:
        makemkv_source = configured_makemkv_source(drive)
    except RuntimeError as exc:
        return {
            "kind": "unknown",
            "type": "Drive is in use" if isinstance(exc, makemkv_runtime.DriveBusy) else "MakeMKV could not prepare the drive",
            "title": "",
            "label": "",
            "titles": [],
            "diagnosis": str(exc),
            "resolution": (
                "Wait for any diagnostic command to finish. Check the scan log "
                "and network access before retrying. If the drive is missing, check its connection and Settings."
            ),
            "raw": str(exc),
        }

    rc, text = run_command(
        [MAKEMKV, "-r", "--cache=1", "info", makemkv_source],
        timeout=makemkv_scan_timeout()
    )

    if rc == 0 and "TCOUNT:" in text and "CINFO:" in text:
        with lock:
            state["job"]["status"] = "Analyzing video titles…"

        result = parse_makemkv(text)
        result["makemkv_source"] = makemkv_source
        # Optional identification failures must never invalidate a usable scan.
        try:
            with lock:
                state["job"]["status"] = "Identifying disc; title assignments may need your review…"
            fingerprints = []
            try:
                with makemkv_runtime.drive_lock(CONFIG_ROOT, configured_drive()):
                    fingerprints = disc_intelligence.DVDReadFingerprint().fingerprint(configured_drive(), result)
            except (OSError, ValueError, subprocess.TimeoutExpired, makemkv_runtime.DriveBusy):
                pass
            result["identity"] = disc_intelligence.enrich(result, CONFIG_ROOT, fingerprints=fingerprints,
                online=not (CONFIG_ROOT / "disc-metadata-offline").exists())
        except Exception:
            result["identity"] = disc_intelligence.base_identity(result)
            result["identity"]["provider_status"] = ["Optional identification unavailable; manual choices still work"]
        return result

    if rc == 124 or 'downloading latest hk' in text.lower():
        return {'kind': 'unknown', 'type': 'UHD support update or scan did not complete' if 'downloading latest hk' in text.lower() else 'Video scan timed out',
                'title': '', 'label': '', 'titles': [],
                'diagnosis': 'MakeMKV did not complete the video scan. A key download or lengthy scan must not be treated as a drive or firmware failure.',
                'resolution': 'Check network access and the scan details, then retry. Downloaded support data is kept in persistent application storage.',
                'raw': f'MakeMKV exit code: {rc}\n{text}'}

    with lock:
        state["job"]["status"] = "Checking for an Audio CD…"

    cyanrip_rc, cyanrip_text = run_command(
        [
            "cyanrip",
            "-d",
            drive,
            "-s",
            configured_cd_offset() or "0",
            "-N",
            "-I",
        ],
        timeout=90,
    )
    audio_disc = parse_cyanrip(cyanrip_text)

    if audio_disc:
        with lock:
            state["job"]["status"] = "Looking up CD album and track names…"

        lookup_audio_metadata(audio_disc)
        return audio_disc

    drive_index = makemkv_source.split(":", 1)[1]
    label_match = re.search(
        rf'^DRV:{re.escape(drive_index)},[^\n]*,"([^"]*)","[^"]*"$',
        text,
        re.MULTILINE,
    )
    label = label_match.group(1) if label_match else ""
    disc_type = "Disc could not be identified"
    diagnosis = "The inserted disc could not be opened as video or audio."
    resolution = "Check the disc condition, then eject and scan it again."

    if "LibreDrive compatible drive is required" in text:
        disc_type = "Blu-ray blocked by drive firmware"
        diagnosis = (
            "This Blu-ray was recognized, but MakeMKV cannot decrypt it "
            "because the drive's current AACS access was rejected."
        )
        resolution = (
            "This disc requires a LibreDrive-compatible drive or firmware. "
            "Firmware changes can permanently damage a drive, so Dexter "
            "will not attempt them automatically."
        )
    elif "Failed to open disc" in text and label:
        disc_type = "Blu-ray could not be opened"
        diagnosis = (
            "The Blu-ray was recognized, but MakeMKV could not open its "
            "video content."
        )
        resolution = (
            "Open the technical scan details for the exact MakeMKV error, "
            "then retry after checking the disc and drive."
        )

    return {
        "kind": "unknown",
        "type": disc_type,
        "title": label,
        "label": label,
        "titles": [],
        "diagnosis": diagnosis,
        "resolution": resolution,
        "raw": (
            f"MakeMKV exit code: {rc}\n{text}\n\n"
            f"cyanrip exit code: {cyanrip_rc}\n{cyanrip_text}"
        ),
    }


def safe_media_name(value):
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*]', "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .")


def staged_audio_files(disc):
    expected = {
        track["number"] for track in disc.get("tracks", [])
        if track["number"] in disc.get("selected_tracks", [])
    }

    if not expected:
        return {}

    root = AUDIO_OUTPUT.resolve()
    if "audio_job_dir" in disc:
        job_dir = disc["audio_job_dir"]
        if not re.fullmatch(r"cd-[0-9a-f]{32}", job_dir or ""):
            return {}
        root = root / job_dir
        if root.is_symlink():
            return {}

    try:
        directories = sorted(
            (path for path in root.iterdir() if path.is_dir() and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return {}

    for directory in directories:
        found = {}

        for path in directory.glob("*.flac"):
            match = re.match(r"^(\d+)\s+-\s+.*\.flac$", path.name)

            if not match or path.is_symlink() or path.stat().st_size <= 0:
                continue

            if path.resolve().parent != directory.resolve():
                continue

            found[int(match.group(1))] = path

        if set(found) == expected:
            return found

    return {}




def humanize_disc_name(value):
    """
    Convert machine-like optical-disc names to a human-friendly suggestion.

    The original disc title/label remains untouched in state.
    """
    if not value:
        return ""

    original = str(value).strip()

    if not original:
        return ""

    value = re.sub(r"[_]+", " ", original)
    value = re.sub(r"\s+", " ", value).strip()

    # Preserve already human-written mixed-case titles.
    if "_" not in original and not original.isupper():
        return value

    return value.lower().title()


def suggested_movie_title(disc):
    if not disc or disc.get("kind") != "video":
        return ""

    # MakeMKV's title is normally the best first choice. Fall back to label.
    raw = disc.get("title") or disc.get("label") or ""
    return humanize_disc_name(raw)


def job_operation_kind(job):
    recovery = job.get("recovery") or {}
    kind = recovery.get("kind")

    if kind in ("rip", "import", "encode"):
        return kind

    description = (job.get("description") or "").lower()

    if description.startswith("audio cd"):
        return "rip"

    if description.startswith("makemkv title"):
        return "rip"

    if (
        job.get("import_failed") or job.get("movie_import")
        or description.startswith("import to jellyfin")
        or description.startswith("import batch")
        or description.startswith("import music")
    ):
        return "import"

    if description == "disc scan":
        return "scan"

    return "command"


def ui_phase(disc, job):
    status = (job.get("status") or "").lower()
    kind = job_operation_kind(job)

    if job.get("running"):
        if kind == "encode":
            return {"verifying": "Verifying", "validating": "Validating", "packaging": "Packaging",
                    "inspecting": "Preparing", "testing": "Preparing"}.get(job.get("stage"), "Encoding")
        if job.get("audio_workflow") and job.get("stage") == "validating":
            return "Verifying"
        if kind == "scan":
            return "Scanning"
        if kind == "rip":
            return "Ripping"
        if kind == "import":
            return "Importing"
        return "Working"

    if status.startswith("recovered") or "recovery" in status:
        return "Recovery"

    verification = job.get("verification") or {}

    if verification.get("ok") and job.get("output_file"):
        return "Verified"

    if disc and disc.get("kind") in ("video", "audio"):
        return "Ready"

    return "Idle"


def job_visual_tone(job):
    status = (job.get("status") or "").lower()

    if job.get("running"):
        return "active"

    if job.get("import_failed") or job.get("music_import_failed") or job.get("encode_readiness") == encoding.EncodeReadiness.UNSUPPORTED.value:
        return "error"

    if status.startswith("recovered") or "recovery" in status:
        return "recovery"

    if any(
        word in status
        for word in (
            "failed",
            "refused",
            "error",
            "could not",
        )
    ):
        return "error"

    if any(
        word in status
        for word in (
            "warning",
            "stall",
            "interrupted",
        )
    ):
        return "warning"

    verification = job.get("verification") or {}

    if (
        verification.get("ok")
        or status.startswith("completed")
        or status.startswith("imported")
    ):
        return "success"

    return "neutral"


def job_display_heading(job):
    status = job.get("status") or ""
    normalized_status = status.lower()
    description = job.get("description") or ""
    kind = job_operation_kind(job)

    if job.get("music_import_failed"):
        return "Music ready — import needs attention"

    if job.get("import_failed"):
        return "Import Failed"

    if status.startswith("Recovered verified"):
        return "Recovered Verified Output"

    if status.startswith("Recovered"):
        return "Recovered Job"

    if job.get("running"):
        if kind == "encode":
            return {"packaging":"Preparing sound and subtitles", "verifying":"Checking media",
                    "validating":"Checking playback copy", "inspecting":"Inspecting source"}.get(job.get("stage"), "Making your smaller copy")
        if kind == "scan":
            return "Disc Scan"
        if kind == "rip":
            return description or "Disc Rip"
        if kind == "import":
            return "Jellyfin Import"

    if any(
        word in normalized_status
        for word in ("failed", "refused", "error", "could not")
    ):
        if kind == "rip":
            return "Rip Failed"
        if kind == "import":
            return "Import Failed"
        return "Job Failed"

    verification = job.get("verification") or {}

    if verification.get("ok") and job.get("output_file"):
        return "Verified Output"

    return description or "Job"


def job_is_cancellable(job):
    if not job.get("running"):
        return False

    return job_operation_kind(job) in ("rip", "import", "encode")


def job_cancel_label(job):
    if job_operation_kind(job) == "encode":
        return "Cancel Encoding"
    if job_operation_kind(job) == "import":
        return "Cancel Import"

    if (job.get("description") or "").lower().startswith("audio cd"):
        return "Cancel Music Rip"

    return "Cancel Rip"


def apply_library_permissions(path, is_dir=False, library_root=None):
    """Match imported content to the existing Jellyfin movie library."""
    path = Path(path)
    library_root = Path(library_root) if library_root else MEDIA_ROOT / "movies"
    root_stat = library_root.stat()

    uid = root_stat.st_uid
    gid = root_stat.st_gid

    if is_dir:
        mode = stat.S_IMODE(root_stat.st_mode)
    else:
        mode = None

        # Prefer the permissions of an existing movie file.
        for pattern in ("*/*.mkv", "*/*.mp4", "*/*.m4v"):
            sample = next(library_root.glob(pattern), None)
            if sample and sample.is_file():
                sample_stat = sample.stat()
                uid = sample_stat.st_uid
                gid = sample_stat.st_gid
                mode = stat.S_IMODE(sample_stat.st_mode)
                break

        # Safe fallback if the library has no existing movie files.
        if mode is None:
            root_mode = stat.S_IMODE(root_stat.st_mode)
            mode = 0o664 if (root_mode & stat.S_IWGRP) else 0o644

    os.chown(path, uid, gid)
    os.chmod(path, mode)


def refresh_jellyfin():
    if not JELLYFIN_KEY_FILE.exists():
        return False, "No Jellyfin API key configured"

    key = JELLYFIN_KEY_FILE.read_text().strip()

    if not key:
        return False, "Jellyfin API key file is empty"

    req = urllib.request.Request(
        f"{configured_jellyfin_url()}/Library/Refresh",
        method="POST",

    headers={
        "Authorization": f'MediaBrowser Token="{key}"',
        "Content-Length": "0",
    },

    )

    try:
        with urllib.request.build_opener(library_connection.NoRedirect).open(req, timeout=15) as response:
            return True, f"Jellyfin refresh requested (HTTP {response.status})"
    except Exception as exc:
        return False, f"Jellyfin refresh failed: {exc}"


def lookup_movie_metadata(title, year=None):
    if not JELLYFIN_KEY_FILE.exists():
        raise ValueError("No Jellyfin API key configured")

    key = JELLYFIN_KEY_FILE.read_text().strip()
    search_info = {"Name": title, "ProviderIds": {}}

    if year:
        search_info["Year"] = int(year)

    payload = json.dumps({
        "SearchInfo": search_info,
        "IncludeDisabledProviders": False,
    }).encode()
    req = urllib.request.Request(
        f"{configured_jellyfin_url()}/Items/RemoteSearch/Movie",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f'MediaBrowser Token="{key}"',
        },
    )

    with urllib.request.build_opener(library_connection.NoRedirect).open(req, timeout=25) as response:
        results = json.load(response)

    return [
        {
            "source": "Jellyfin",
            "name": item.get("Name") or title,
            "year": item.get("ProductionYear"),
            "provider": item.get("SearchProviderName") or "Metadata provider",
            "provider_ids": item.get("ProviderIds") or {},
            "overview": item.get("Overview") or "",
        }
        for item in results[:8]
    ]


def lookup_tv_metadata(title):
    query = urllib.parse.urlencode({"q": title})
    req = urllib.request.Request(
        f"https://api.tvmaze.com/search/shows?{query}",
        headers={"User-Agent": "DexterRipper/0.1"},
    )

    with urllib.request.urlopen(req, timeout=25) as response:
        results = json.load(response)

    matches = []

    for result in results[:8]:
        show = result.get("show") or {}
        premiered = show.get("premiered") or ""
        matches.append({
            "source": "TVmaze",
            "name": show.get("name") or title,
            "year": premiered[:4] if premiered else None,
            "provider": "TVmaze",
            "provider_ids": {"Tvmaze": str(show.get("id") or "")},
            "overview": re.sub(r"<[^>]+>", "", show.get("summary") or ""),
        })

    return matches


def lookup_tv_episodes(show_id, season):
    req = urllib.request.Request(
        f"https://api.tvmaze.com/shows/{int(show_id)}/episodes",
        headers={"User-Agent": "DexterRipper/0.1"},
    )

    with urllib.request.urlopen(req, timeout=25) as response:
        episodes = json.load(response)

    return {
        int(item["number"]): item.get("name") or ""
        for item in episodes
        if item.get("season") == season and item.get("number") is not None
    }


def format_duration(seconds):
    try:
        seconds = int(round(float(seconds)))
    except Exception:
        return "Unknown"

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def format_size(value):
    try:
        size = int(value)
    except Exception:
        return "Unknown"

    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(size)

    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024


def probe_media(path):
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_streams", "-show_format",
            "-of", "json",
            str(path)
        ],
        capture_output=True,
        text=True,
        timeout=60
    )

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "ffprobe failed").strip()
        }

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Unable to parse ffprobe output: {exc}"
        }

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next(
        (x for x in streams if x.get("codec_type") == "video"),
        None
    )

    audio = next(
        (x for x in streams if x.get("codec_type") == "audio"),
        None
    )

    try:
        duration = float(fmt.get("duration", 0))
    except Exception:
        duration = 0

    result = {
        "ok": bool(video and duration > 0),
        "filename": Path(fmt.get("filename", str(path))).name,
        "duration": format_duration(duration),
        "size": format_size(fmt.get("size", 0)),
        "video_codec": video.get("codec_name", "") if video else "",
        "width": video.get("width", "") if video else "",
        "height": video.get("height", "") if video else "",
        "audio_codec": audio.get("codec_name", "") if audio else "",
        "channels": audio.get("channels", "") if audio else "",
        "tracks": movie_output.track_choices(data),
    }

    if not result["ok"]:
        result["error"] = "No valid video stream or duration found."

    try:
        result["encoding_source"] = encoding.source_properties(data)
    except ValueError:
        result["encoding_source"] = {"blocked": True}

    return result



STALL_WARNING_SECONDS = int(
    os.environ.get("RIPPER_STALL_WARNING_SECONDS", "120")
)


def parse_size_bytes(value):
    """Convert numeric or human-readable MakeMKV sizes to bytes."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        value = int(value)
        return value if value > 0 else None

    text = str(value).strip().replace(",", "")

    if text.isdigit():
        value = int(text)
        return value if value > 0 else None

    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)",
        text,
        re.I
    )

    if not match:
        return None

    number = float(match.group(1))
    unit = match.group(2).upper()

    factors = {
        "B": 1,
        "KB": 1000,
        "MB": 1000 ** 2,
        "GB": 1000 ** 3,
        "TB": 1000 ** 4,
        "KIB": 1024,
        "MIB": 1024 ** 2,
        "GIB": 1024 ** 3,
        "TIB": 1024 ** 4,
    }

    return int(number * factors[unit])


def expected_size_for_title(title_id):
    """Find scanned MakeMKV size for a title without assuming one schema."""
    with lock:
        disc = state.get("disc")

    if not isinstance(disc, dict):
        return None

    titles = disc.get("titles", [])

    if not isinstance(titles, list):
        return None

    for title in titles:
        if not isinstance(title, dict):
            continue

        candidate_id = title.get("id", title.get("title_id"))

        try:
            matches = int(candidate_id) == int(title_id)
        except (TypeError, ValueError):
            matches = False

        if not matches:
            continue

        for key in (
            "size_bytes",
            "bytes",
            "source_size_bytes",
            "size"
        ):
            size = parse_size_bytes(title.get(key))
            if size:
                return size

    return None


def update_job_progress(verify_path, before, expected_bytes, started_at):
    """Poll the growing MKV and update the in-memory Job card."""
    elapsed = max(time.time() - started_at, 0)

    current = None

    if verify_path:
        candidates = []

        for path in verify_path.glob("*.mkv"):
            try:
                st = path.stat()
            except OSError:
                continue

            previous_mtime = before.get(str(path))

            if (
                previous_mtime is None
                or st.st_mtime_ns > previous_mtime
            ):
                candidates.append((st.st_mtime_ns, path, st.st_size))

        if candidates:
            candidates.sort(reverse=True, key=lambda item: item[0])
            _, current, written_bytes = candidates[0]
        else:
            written_bytes = 0
    else:
        written_bytes = 0

    rate_bps = written_bytes / elapsed if elapsed >= 1 else 0

    now = time.time()

    with lock:
        previous_progress = state["job"].get("progress") or {}

    previous_bytes = previous_progress.get("_written_bytes", 0)
    last_growth_at = previous_progress.get("_last_growth_at", started_at)

    if written_bytes > previous_bytes:
        last_growth_at = now

    if current is not None:
        stall_seconds = max(now - last_growth_at, 0)
    else:
        stall_seconds = 0

    stalled = (
        current is not None
        and stall_seconds >= STALL_WARNING_SECONDS
    )

    percent = None
    eta_seconds = None

    if expected_bytes and expected_bytes > 0:
        # Never claim completion until MakeMKV actually exits successfully.
        percent = min((written_bytes / expected_bytes) * 100.0, 99.5)

        if rate_bps > 0 and written_bytes < expected_bytes:
            eta_seconds = (expected_bytes - written_bytes) / rate_bps

    progress = {
        "percent": round(percent, 1) if percent is not None else None,
        "written": format_size(written_bytes),
        "expected": format_size(expected_bytes) if expected_bytes else None,
        "elapsed": format_duration(elapsed),
        "rate": (
            f"{format_size(rate_bps)}/s"
            if rate_bps > 0
            else None
        ),
        "eta": (
            format_duration(eta_seconds)
            if eta_seconds is not None
            else None
        ),
        "filename": current.name if current else None,
        "stalled": stalled,
        "stall_time": (
            format_duration(stall_seconds)
            if stalled
            else None
        ),
        "_written_bytes": written_bytes,
        "_last_growth_at": last_growth_at,
    }

    with lock:
        if state["job"].get("running"):
            state["job"]["progress"] = progress

            if not state["job"].get("cancel_requested"):
                if stalled:
                    state["job"]["status"] = "Read retry / possible stall"
                elif state["job"].get("status") == "Read retry / possible stall":
                    state["job"]["status"] = "Running"


def video_staging_space_error(expected_bytes, output_dir):
    if not expected_bytes:
        return None

    try:
        free_bytes = shutil.disk_usage(output_dir).free
    except OSError:
        return (
            "Rip failed to start: Dexter could not verify free staging "
            "space."
        )

    safety_margin = max(1024 ** 3, expected_bytes // 50)
    required_bytes = expected_bytes + safety_margin

    if free_bytes >= required_bytes:
        return None

    return (
        "Rip failed to start: selected titles need about "
        f"{format_size(required_bytes)} including working space, but staging "
        f"has only {format_size(free_bytes)} free."
    )


def makemkv_reported_failure(log_lines):
    text = "\n".join(log_lines)
    failed_save = (
        "Failed to save title" in text
        or re.search(r"Copy complete\.\s+0 titles saved,\s+\d+ failed", text)
        or re.search(r"0 titles saved,\s+\d+ failed", text)
    )

    if not failed_save:
        return None

    if "there are only" in text and "free on the destination drive" in text:
        return (
            "Rip failed: staging ran out of space before MakeMKV could finish."
        )

    if "Error while reading input" in text or "HashCheck Error" in text:
        return ("Rip failed: Dexter couldn’t read part of the disc. "
                "Clean the disc and try ripping the same title again.")

    return "Rip failed: Dexter couldn’t save the selected title. See the log for details."



# ---------------------------------------------------------------------------
# Dexter Ripper persistent runtime state / restart recovery
# ---------------------------------------------------------------------------

DEXTER_STATE_SCHEMA = 1
DEXTER_STATE_FILE = CONFIG_ROOT / "dexter-ripper-state.json"
DEXTER_STATE_TMP = CONFIG_ROOT / ".dexter-ripper-state.json.tmp"

_persist_fingerprint = None
_persist_lock = threading.Lock()


def _snapshot_state():
    """Make a JSON-safe snapshot without preserving a stale process PID."""
    with lock:
        snapshot = copy.deepcopy(state)

    job = snapshot.get("job")

    if isinstance(job, dict):
        # PIDs are meaningful only to the current container/process.
        job["pid"] = None

    return snapshot


def persist_state_now(force=False):
    """Atomically persist Dexter state when something has changed."""
    global _persist_fingerprint

    with _persist_lock:
        snapshot = _snapshot_state()

        base_document = {
            "schema": DEXTER_STATE_SCHEMA,
            "state": snapshot,
        }

        fingerprint = json.dumps(
            base_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )

        if not force and fingerprint == _persist_fingerprint:
            return

        document = {
            "schema": DEXTER_STATE_SCHEMA,
            "saved_at": time.time(),
            "state": snapshot,
        }

        payload = json.dumps(
            document,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

        DEXTER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        with DEXTER_STATE_TMP.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        os.replace(DEXTER_STATE_TMP, DEXTER_STATE_FILE)
        _persist_fingerprint = fingerprint


def _persistence_worker():
    """Save background-job progress even if nobody has the web page open."""
    while True:
        try:
            persist_state_now()
        except Exception as exc:
            print(
                f"Dexter state persistence failed: {exc}",
                flush=True,
            )

        time.sleep(1.0)


def _allowed_staged_video(path):
    """Return True only for a direct child of Dexter's video staging dir."""
    try:
        resolved = Path(path).resolve()
        root = VIDEO_OUTPUT.resolve()

        return (
            resolved.parent == root
            and resolved.suffix.lower() in (".mkv", ".mp4")
            and not Path(path).is_symlink()
        )

    except Exception:
        return False


def _job_outputs(job):
    outputs = job.get("outputs")

    if isinstance(outputs, list):
        return [item for item in outputs if isinstance(item, dict)]

    filename = job.get("output_file")
    verification = job.get("verification")

    if filename and isinstance(verification, dict):
        return [{"path": filename, "verification": verification}]

    return []


def _set_job_outputs(job, outputs):
    job["outputs"] = outputs
    first = outputs[0] if outputs else None
    job["output_file"] = first.get("path") if first else None
    job["verification"] = first.get("verification") if first else None


def _remove_job_output(job, filename):
    remaining = [
        item for item in _job_outputs(job)
        if item.get("path") != filename
    ]
    _set_job_outputs(job, remaining)


def _upsert_job_output(job, filename, verification):
    outputs = _job_outputs(job)
    replacement = {"path": filename, "verification": verification}
    updated = []
    found = False

    for item in outputs:
        if item.get("path") == filename:
            updated.append(replacement)
            found = True
        else:
            updated.append(item)

    if not found:
        updated.append(replacement)

    _set_job_outputs(job, updated)


def _recover_interrupted_import(job, recovery):
    """
    Recover a known interrupted Jellyfin import.

    Only the exact .partial path recorded by Dexter is eligible for cleanup.
    The verified staged source is never deleted during recovery.
    """
    source_value = recovery.get("source")
    partial_value = recovery.get("partial_file")
    destination_value = recovery.get("destination_file")

    source = Path(source_value) if source_value else None
    partial = Path(partial_value) if partial_value else None
    destination = (
        Path(destination_value)
        if destination_value
        else None
    )

    removed_partial = False

    if partial and partial.exists() and partial.is_file():
        try:
            partial.unlink()
            removed_partial = True
        except Exception as exc:
            job["status"] = (
                f"Recovery warning: could not remove interrupted "
                f"import partial: {exc}"
            )

    # Remove only an empty destination directory created by this import.
    if partial and partial.parent.exists():
        if not destination or not destination.exists():
            try:
                partial.parent.rmdir()
            except OSError:
                pass

    job["running"] = False
    job["pid"] = None
    job["cancel_requested"] = False
    job["progress"] = None
    job["recovery"] = None

    # Final destination exists. Do not overwrite or delete anything.
    if destination and destination.exists():
        if source and source.exists():
            verification = probe_media(source)

            _upsert_job_output(job, str(source), verification)

            job["status"] = (
                "Recovery required: Jellyfin destination and staged "
                "source both exist; nothing was deleted"
            )
        else:
            if source:
                _remove_job_output(job, str(source))
            job["status"] = (
                "Recovered interrupted import: final Jellyfin file "
                "exists; library refresh may need to be repeated"
            )

        return

    # Normal interrupted-copy case: staged source survived.
    if (
        source
        and source.exists()
        and source.is_file()
        and _allowed_staged_video(source)
    ):
        verification = probe_media(source)

        _upsert_job_output(job, str(source), verification)

        if verification.get("ok"):
            suffix = (
                "; partial copy removed"
                if removed_partial
                else ""
            )

            job["status"] = (
                "Recovered interrupted import — verified staged "
                f"file kept and ready to retry{suffix}"
            )
        else:
            job["status"] = (
                "Recovered interrupted import — staged file kept, "
                "but verification failed"
            )

        return

    if source:
        _remove_job_output(job, str(source))
    job["status"] = (
        "Recovered interrupted import, but the staged source "
        "could not be found"
    )


def _recover_interrupted_batch_import(job, recovery):
    # Saved state cannot establish ownership of a destination tree. It may
    # contain completed files or another writer's media; never delete it.
    job["running"] = False
    job["pid"] = None
    job["cancel_requested"] = False
    job["progress"] = None
    job["recovery"] = None
    _set_job_outputs(job, [
        item for item in _job_outputs(job)
        if item.get("path") and Path(item["path"]).is_file()
    ])
    job["status"] = (
        "Interrupted batch import needs review. Existing library files and "
        "available staged files were kept; check the destination before retrying."
    )


def _unique_recovery_path(source):
    recovery_dir = OUTPUT / "recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")

    candidate = recovery_dir / (
        f"{source.stem}.interrupted-{stamp}{source.suffix}"
    )

    counter = 1

    while candidate.exists():
        candidate = recovery_dir / (
            f"{source.stem}.interrupted-{stamp}-{counter}"
            f"{source.suffix}"
        )
        counter += 1

    return candidate


def _recover_interrupted_rip(job, recovery):
    """
    Preserve a possible interrupted MakeMKV output.

    Dexter does not assume a partially written MKV is valid. If the exact
    filename recorded by live progress still exists, move it out of active
    staging to /output/recovery instead of deleting it.
    """
    progress = job.get("progress") or {}
    filename = progress.get("filename")

    job["running"] = False
    job["pid"] = None
    job["cancel_requested"] = False
    job["progress"] = None
    job["recovery"] = None
    _set_job_outputs(job, _job_outputs(job))

    for item in job.get("title_queue") or []:
        if item.get("status") in ("queued", "ripping"):
            item["status"] = "interrupted"

    if not filename:
        job["status"] = (
            "Previous rip was interrupted by a restart; "
            "no partial filename was recorded"
        )
        return

    work_dir = Path(recovery.get("verify_dir") or VIDEO_OUTPUT)
    if work_dir.parent == OUTPUT / "recovery" and work_dir.name.startswith("video-rip-"):
        job["status"] = "The rip was interrupted. Incomplete files are in Recovery. Try ripping the disc again."
        return
    source = VIDEO_OUTPUT / Path(str(filename)).name

    if not source.exists() or not source.is_file():
        job["status"] = (
            "Previous rip was interrupted by a restart; "
            "no partial staged file remains"
        )
        return

    target = _unique_recovery_path(source)

    try:
        shutil.move(str(source), str(target))

        job["status"] = (
            "Previous rip was interrupted; partial output preserved at "
            f"{target}"
        )

    except Exception as exc:
        job["status"] = (
            "Previous rip was interrupted; partial output was left "
            f"in place because recovery failed: {exc}"
        )


def restore_persistent_state():
    """Load and reconcile the previous Dexter runtime state."""
    if DEXTER_STATE_FILE.exists():
        try:
            document = json.loads(
                DEXTER_STATE_FILE.read_text(encoding="utf-8")
            )

            if document.get("schema") != DEXTER_STATE_SCHEMA:
                raise ValueError(
                    f"unsupported state schema "
                    f"{document.get('schema')}"
                )

            saved_state = document.get("state")

            if not isinstance(saved_state, dict):
                raise ValueError("saved state is not a dictionary")

            with lock:
                # Preserve current defaults for any fields that were not
                # present in an older saved state.
                if "disc" in saved_state:
                    state["disc"] = saved_state["disc"]
                    state["scan_requires_refresh"] = bool(saved_state["disc"])

                if isinstance(saved_state.get("wizard_draft"), dict):
                    state["wizard_draft"] = saved_state["wizard_draft"]
                if isinstance(saved_state.get("workflow_queue"), list):
                    state["workflow_queue"] = saved_state["workflow_queue"]
                    for entry in state["workflow_queue"]:
                        if entry.get("status") in ("saving", "queued", "starting", "running"):
                            entry["status"] = "needs_review"
                            entry["scan_after"] = time.time()
                            entry["detail"] = "Dexter restarted. Scan the disc and check the plan before retrying."
                history = saved_state.get("movie_history")
                if isinstance(history, list):
                    state["movie_history"] = history[-100:]
                saved_job = saved_state.get("job")

                if isinstance(saved_job, dict):
                    state.setdefault("job", {})
                    state["job"].update(saved_job)

        except Exception as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")

            corrupt = DEXTER_STATE_FILE.with_name(
                f"dexter-ripper-state.corrupt-{stamp}.json"
            )

            try:
                shutil.move(str(DEXTER_STATE_FILE), str(corrupt))
            except Exception:
                pass

            print(
                f"Dexter saved state could not be loaded: {exc}",
                flush=True,
            )

    with lock:
        job = state.setdefault("job", {})

        job.setdefault("running", False)
        job.setdefault("description", "")
        job.setdefault("status", "Idle")

        if not job.get("status"):
            job["status"] = "Idle"

        job.setdefault("log", [])
        job.setdefault("output_file", None)
        job.setdefault("verification", None)
        job.setdefault("outputs", _job_outputs(job))
        job.setdefault("title_queue", [])
        job.setdefault("metadata_results", [])
        job.setdefault("metadata_draft", {})
        job.setdefault("pid", None)
        job.setdefault("cancel_requested", False)
        job.setdefault("progress", None)
        job.setdefault("recovery", None)

        was_running = bool(job.get("running"))
        recovery = job.get("recovery") or {}

        # Never restore process-control state from another container process.
        job["running"] = False
        job["pid"] = None
        job["cancel_requested"] = False

    # Reconcile an operation that was active when the service disappeared.
    if was_running:
        kind = recovery.get("kind")

        if kind == "import":
            with lock:
                _recover_interrupted_import(job, recovery)

        elif kind == "batch_import":
            with lock:
                _recover_interrupted_batch_import(job, recovery)

        elif kind == "rip":
            with lock:
                _recover_interrupted_rip(job, recovery)

        elif kind == "encode":
            # Publication may have completed just before state was saved. Never
            # restart an encoder automatically or remove the extracted original.
            source, target = recovery.get("source"), recovery.get("target")
            job["stage"] = "interrupted"
            job["status"] = "Encoding interrupted; original retained; retry available"
            if source and target and _allowed_staged_video(source) and _allowed_staged_video(target) and Path(target).is_file():
                try:
                    original, encoded = encoding.inspect(source), encoding.inspect(target)
                    encoding.verify_preservation(original, encoded, recovery.get("codec", "h264"))
                    verification = probe_media(Path(target))
                    if not verification.get("ok"):
                        raise ValueError("Final video inspection failed")
                    _set_job_outputs(job, [
                        {"path": target, "verification": verification}
                        if item.get("path") == source else item for item in _job_outputs(job)])
                    job["status"] = "Recovered verified encoded output; original retained"
                    job["stage"] = "completed"
                except Exception as exc:
                    job.setdefault("log", []).append("Encoded recovery refused: " + str(exc))
            job["progress"] = None
            job["recovery"] = None

        else:
            with lock:
                job["status"] = (
                    "Previous job was interrupted by a service restart"
                )
                job["progress"] = None
                job["recovery"] = None

    else:
        with lock:
            saved_outputs = copy.deepcopy(_job_outputs(job))

        recovered_outputs = []

        for item in saved_outputs:
            output_value = item.get("path")

            if (
                output_value
                and _allowed_staged_video(output_value)
                and Path(output_value).is_file()
            ):
                recovered_outputs.append({
                    **copy.deepcopy(item),
                    "path": output_value,
                    "verification": probe_media(Path(output_value)),
                })

        with lock:
            _set_job_outputs(job, recovered_outputs)
            job["recovery"] = None

            if recovered_outputs:
                verified_count = sum(
                    1 for item in recovered_outputs
                    if (item.get("verification") or {}).get("ok")
                )
                recovery_message = (
                    f"Recovered {verified_count} verified staged "
                    f"file{'s' if verified_count != 1 else ''}"
                )
                if (job.get("description") == "Encode video" and job.get("queue_id")
                        and (job.get("status") or "").startswith("Encoding failed")):
                    job.setdefault("log", []).append(recovery_message)
                else:
                    job["status"] = recovery_message
            elif saved_outputs:
                job["progress"] = None

    persist_state_now(force=True)


restore_persistent_state()

threading.Thread(
    target=_persistence_worker,
    daemon=True,
    name="dexter-state-persistence",
).start()


def staging_has_media():
    for root in (OUTPUT / "video", AUDIO_OUTPUT):
        try:
            if any(root.iterdir()):
                return True
        except OSError:
            continue

    return False


def clear_staged_media():
    removed = 0
    video_root = OUTPUT / "video"
    audio_root = AUDIO_OUTPUT

    for path in video_root.iterdir():
        if path.is_symlink():
            path.unlink()
            removed += 1
        elif path.is_file() and path.suffix.lower() in (
            ".mkv",
            ".mp4",
            ".partial",
            ".tmp",
        ):
            path.unlink()
            removed += 1

    for path in audio_root.iterdir():
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            continue

        removed += 1

    return removed


def staging_storage():
    try:
        usage = shutil.disk_usage(OUTPUT)
    except OSError:
        return None

    return {
        "free": format_size(usage.free),
        "total": format_size(usage.total),
    }


def update_cyanrip_progress(line, started_at):
    match = re.search(
        r"Ripping and encoding track\s+(\d+),\s*"
        r"progress\s*-\s*([\d.]+)%,\s*ETA\s*-\s*(.+)$",
        line,
        re.IGNORECASE,
    )

    if not match:
        return False

    track_number = int(match.group(1))
    track_percent = min(100.0, max(0.0, float(match.group(2))))
    eta = match.group(3).strip()

    with lock:
        disc = state.get("disc") or {}
        tracks = disc.get("tracks") or []
        total_tracks = len(tracks)
        track = next(
            (
                item for item in tracks
                if item.get("number") == track_number
            ),
            {},
        )
        title = track.get("title") or f"Track {track_number}"
        percent = (
            ((track_number - 1) + (track_percent / 100.0))
            / total_tracks
            * 100.0
            if total_tracks
            else track_percent
        )
        state["job"]["status"] = (
            f"Ripping track {track_number} of {total_tracks}: {title}"
            if total_tracks
            else f"Ripping track {track_number}: {title}"
        )
        state["job"]["progress"] = {
            "percent": percent,
            "track_percent": track_percent,
            "written": (
                f"{track_number} of {total_tracks} tracks"
                if total_tracks
                else f"Track {track_number}"
            ),
            "expected": None,
            "elapsed": format_duration(time.time() - started_at),
            "rate": None,
            "eta": eta,
            "filename": title,
            "current_track": track_number,
            "total_tracks": total_tracks,
        }

    return True


def run_job(
    description,
    command,
    cwd=None,
    verify_dir=None,
    expected_bytes=None,
    queue_items=None,
    preflight=None,
    publish_dir=None,
    retry_token=None,
    movie_workflow=None,
    audio_workflow=None,
):
    commands = (
        command
        if command and isinstance(command[0], (list, tuple))
        else [command]
    )
    verify_path = Path(verify_dir) if verify_dir else None
    before = {}

    if verify_path:
        verify_path.mkdir(parents=True, exist_ok=True)

        for path in verify_path.glob("*.mkv"):
            try:
                before[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass

    with lock:
        if state["job"]["running"] or (retry_token and state["job"].get("video_retry_token") != retry_token):
            return False

        existing_outputs = copy.deepcopy(_job_outputs(state["job"]))
        state["job"] = {
            "running": True,
            "description": description,
            "status": "Running",
            "log": [],
            "output_file": None,
            "verification": None,
            "outputs": existing_outputs,
            "title_queue": copy.deepcopy(queue_items or []),
            "pid": None,
            "cancel_requested": False,
            "progress": {
                "percent": 0.0 if expected_bytes else None,
                "written": format_size(0),
                "expected": (
                    format_size(expected_bytes)
                    if expected_bytes
                    else None
                ),
                "elapsed": format_duration(0),
                "rate": None,
                "eta": None,
                "filename": None,
            }
        }

        active_job = state["job"]
        if movie_workflow:
            active_job["movie_workflow"] = movie_workflow
            active_job["queue_id"] = movie_workflow.get("queue_id")
        if audio_workflow:
            active_job["audio_workflow"] = audio_workflow
            active_job["queue_id"] = audio_workflow.get("queue_id")
        if publish_dir:
            state["job"]["video_retry_token"] = uuid.uuid4().hex

        state["job"]["recovery"] = {
            "kind": "rip" if verify_path else "command",
            "verify_dir": str(verify_path) if verify_path else None,
        }

    persist_state_now(force=True)

    def worker():
        logfile = CONFIG_ROOT / "dexter-ripper-job.log"
        reported_failure = None
        workflow_source = None
        extraction_started = time.monotonic()
        source_check_started = None

        try:
            if preflight:
                preflight()
            with logfile.open("w") as log:
                started_at = time.time()
                progress_stop = threading.Event()
                progress_thread = None

                if verify_path:
                    def progress_worker():
                        while not progress_stop.is_set():
                            update_job_progress(
                                verify_path,
                                before,
                                expected_bytes,
                                started_at
                            )

                            progress_stop.wait(2)

                        update_job_progress(
                            verify_path,
                            before,
                            expected_bytes,
                            started_at
                        )

                    progress_thread = threading.Thread(
                        target=progress_worker,
                        daemon=True
                    )
                    progress_thread.start()

                rc = 0

                for index, job_command in enumerate(commands, start=1):
                    command_log = []

                    with lock:
                        if state["job"].get("cancel_requested"):
                            rc = 130
                            break

                        title_queue = state["job"].get("title_queue") or []

                        if index <= len(title_queue):
                            title_queue[index - 1]["status"] = "ripping"

                        if len(commands) > 1:
                            state["job"]["status"] = (
                                f"Running title {index} of {len(commands)}"
                            )

                    def record_line(line):
                        log.write(line)
                        log.flush()
                        command_log.append(line.rstrip())

                        with lock:
                            state["job"]["log"].append(line.rstrip())
                            state["job"]["log"] = state["job"]["log"][-80:]

                        if job_command and job_command[0] == "cyanrip":
                            update_cyanrip_progress(line, started_at)
                        elif Path(job_command[0]).name == 'makemkvcon' or job_command[0] == MAKEMKV:
                            update_makemkv_status(line, include_log=False)

                    if Path(job_command[0]).name == 'makemkvcon' or job_command[0] == MAKEMKV:
                        def record_pid(proc):
                            with lock:
                                state['job']['pid'] = proc.pid
                                cancelled = state['job'].get('cancel_requested')
                            if cancelled:
                                os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            def cancelled():
                                with lock:
                                    return bool(state['job'].get('cancel_requested'))
                            rc, _ = makemkv_runtime.run(job_command, CONFIG_ROOT, configured_drive(),
                                timeout=None, cwd=cwd, on_line=record_line, on_start=record_pid, cancelled=cancelled)
                        finally:
                            with lock:
                                state['job']['pid'] = None
                    else:
                        proc = subprocess.Popen(job_command, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=cwd, start_new_session=True)
                        with lock:
                            state['job']['pid'] = proc.pid
                        for line in proc.stdout:
                            record_line(line)
                        rc = proc.wait()

                    if Path(job_command[0]).name == "makemkvcon":
                        command_failure = makemkv_reported_failure(command_log)

                        if command_failure:
                            reported_failure = command_failure
                            rc = rc or 1

                    with lock:
                        title_queue = state["job"].get("title_queue") or []

                        if index <= len(title_queue):
                            title_queue[index - 1]["status"] = (
                                "completed" if rc == 0 else "failed"
                            )

                    if rc != 0:
                        break

                if progress_thread:
                    progress_stop.set()
                    progress_thread.join(timeout=3)

            source_check_started = time.monotonic()
            outputs = copy.deepcopy(existing_outputs)
            new_outputs = []
            verification_failure = None

            with lock:
                cancelled = state["job"].get("cancel_requested", False)

                if cancelled:
                    for item in state["job"].get("title_queue") or []:
                        if item.get("status") in ("queued", "ripping", "failed"):
                            item["status"] = "cancelled"
                elif rc != 0:
                    for item in state["job"].get("title_queue") or []:
                        if item.get("status") == "queued":
                            item["status"] = "skipped"

            if cancelled and verify_path:
                for path in verify_path.glob("*.mkv"):
                    try:
                        mtime = path.stat().st_mtime_ns
                    except OSError:
                        continue

                    if (
                        str(path) not in before
                        or mtime > before[str(path)]
                    ):
                        try:
                            path.unlink()
                        except OSError:
                            pass

            if rc == 0 and verify_path and not cancelled:
                changed = []

                for path in verify_path.glob("*.mkv"):
                    try:
                        mtime = path.stat().st_mtime_ns
                    except OSError:
                        continue

                    if (
                        str(path) not in before
                        or mtime > before[str(path)]
                    ):
                        changed.append(path)

                if changed:
                    changed.sort(
                        key=lambda x: x.stat().st_mtime_ns,
                    )

                    for path in changed:
                        if publish_dir:
                            target = publish_dir / path.name
                            try:
                                os.link(path, target)
                            except FileExistsError:
                                target = publish_dir / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
                                os.link(path, target)
                            path.unlink()
                            path = target
                        new_outputs.append({"path": str(path), "verification": probe_media(path)})
                    outputs.extend(new_outputs)
                else:
                    verification_failure = (
                        "Rip failed: MakeMKV produced no new MKV."
                    )

            successful_outputs = [
                item for item in new_outputs
                if (item.get("verification") or {}).get("ok")
            ]
            failed_outputs = [
                item for item in new_outputs
                if not (item.get("verification") or {}).get("ok")
            ]

            with lock:
                state["job"]["running"] = False
                state["job"]["pid"] = None
                _set_job_outputs(state["job"], outputs)

                if cancelled:
                    state["job"]["status"] = (
                        "Cancelled; partial output removed"
                        if verify_path
                        else "Cancelled"
                    )
                elif rc != 0:
                    state["job"]["status"] = (
                        reported_failure or f"Failed (exit {rc})"
                    )
                elif verification_failure:
                    state["job"]["status"] = verification_failure

                    for item in state["job"].get("title_queue") or []:
                        if item.get("status") == "completed":
                            item["status"] = "failed"
                elif successful_outputs and not failed_outputs:
                    output_count = len(successful_outputs)

                    if output_count > 1:
                        state["job"]["status"] = (
                            f"Completed and verified — {output_count} files "
                            "ready to import"
                        )
                    else:
                        state["job"]["status"] = "Completed and verified"

                    progress = state["job"].get("progress") or {}
                    progress["percent"] = 100.0

                    if output_count == 1:
                        workflow_source = successful_outputs[0]["path"]
                        try:
                            final_size = Path(
                                successful_outputs[0]["path"]
                            ).stat().st_size
                            progress["written"] = format_size(final_size)
                        except OSError:
                            pass

                    state["job"]["progress"] = progress
                elif successful_outputs:
                    state["job"]["status"] = (
                        f"Completed — {len(successful_outputs)} verified, "
                        f"{len(failed_outputs)} failed verification"
                    )
                elif verify_path:
                    state["job"]["status"] = "Completed; verification failed"
                else:
                    state["job"]["status"] = "Completed"
                    progress = state["job"].get("progress") or {}
                    progress["percent"] = 100.0
                    progress["eta"] = None
                    state["job"]["progress"] = progress

        except Exception as exc:
            with lock:
                state["job"]["running"] = False
                state["job"]["pid"] = None
                state["job"]["status"] = f"Failed: {exc}"

                for item in state["job"].get("title_queue") or []:
                    if item.get("status") == "ripping":
                        item["status"] = "failed"

        finally:
            if publish_dir:
                try:
                    verify_path.rmdir()
                except OSError:
                    pass

        if movie_workflow and workflow_source:
            movie_workflow["source_metrics"] = {
                "extraction_seconds": source_check_started - extraction_started,
                "source_check_seconds": time.monotonic() - source_check_started,
            }
            continue_movie_workflow(active_job, workflow_source, movie_workflow, after_encode=False)
        elif movie_workflow:
            finish_workflow_entry(active_job, "cancelled" if active_job.get("cancel_requested") else "failed",
                                  active_job.get("status", "Disc extraction stopped."))
        if audio_workflow:
            if active_job.get("status") == "Completed" and not active_job.get("cancel_requested"):
                continue_audio_workflow(active_job, audio_workflow)
            else:
                finish_workflow_entry(active_job, "cancelled" if active_job.get("cancel_requested") else "failed", active_job.get("status", "CD extraction stopped."))

    threading.Thread(target=worker, daemon=True).start()
    return True


SETTINGS_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Magic Media PAL Settings</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
        sans-serif;
    background: #101010;
    color: #eeeeee;
    margin: 0;
}
main { max-width: 1100px; margin: 32px auto; padding: 0 24px; }
.card {
    background: #1b1b1b;
    border: 1px solid #333;
    border-radius: 12px;
    padding: 24px;
}
h1 { margin-top: 0; }
.muted { color: #b8b8b8; }
.error {
    background: #382020;
    border: 1px solid #865151;
    border-radius: 7px;
    color: #ffd4d4;
    padding: 10px 12px;
}
label { display: block; font-weight: 650; margin-top: 18px; }
input, select {
    width: 100%;
    margin-top: 7px;
    border: 1px solid #555;
    border-radius: 7px;
    background: #111;
    color: #eee;
    padding: 10px 12px;
}
.hint { color: #aaa; font-size: .88rem; margin-top: 6px; }
.actions { display: flex; gap: 10px; margin-top: 24px; }
button, .button {
    border: 1px solid #91b9e0;
    border-radius: 7px;
    background: #dcecff;
    color: #111;
    padding: 9px 15px;
    cursor: pointer;
    font-weight: 650;
    text-decoration: none;
}
.button { background: #efefef; border-color: #5a5a5a; }

.settings-header { margin-bottom: 22px; }
.settings-header h1 { margin: 12px 0 6px; }
.settings-header p { margin: 0; }
.settings-layout { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 18px; align-items: start; }
.settings-layout .card { margin-bottom: 16px; }
.settings-layout h2 { margin: 0 0 8px; font-size: 1.2rem; }
.settings-layout label:first-of-type { margin-top: 10px; }
.settings-layout summary { cursor: pointer; font-weight: 650; }
.settings-layout a, .settings-header a { color: #abd3ff; }
.settings-layout a.button { color: #111; }
.settings-layout code { overflow-wrap: anywhere; }
.settings-layout .actions { margin: 0; flex-wrap: wrap; }
.settings-layout button:disabled { opacity: .55; cursor: not-allowed; }
.settings-layout .check-button { margin-top: 14px; }
.storage-list { margin: 16px 0; }
.storage-list dt { font-weight: 650; }
.storage-list dd { margin: 4px 0 14px; }
.readiness-row { border-top: 1px solid #444; padding: 10px 0; }
@media (max-width: 720px) {
    main { padding: 0 16px; margin-top: 20px; }
    .settings-layout { grid-template-columns: minmax(0, 1fr); gap: 16px; }
}
</style>
</head>
<body>
<main>
<header class="settings-header">
{% if not first_run %}<a href="{{ url_for('home') }}">← Disc workspace</a>{% endif %}
<h1>{% if first_run %}Set up Magic Media PAL{% else %}Settings{% endif %}</h1>
{% if first_run %}
<p class="muted">A little magic for your media collection.</p>
<img src="{{ url_for('static', filename='pal-the-genie.png') }}"
     width="1448" height="1086" alt="Pal the Genie floats above a colorful disc."
     style="display:block;width:min(100%,360px);height:auto;border-radius:12px;margin:12px 0">
{% endif %}
<p class="muted">{% if first_run %}Choose a drive to get started. You can connect Jellyfin later.{% else %}Manage your disc drive, storage and Jellyfin connection.{% endif %}</p>
</header>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}

<div class="settings-layout">
<form method="post" action="{{ url_for('settings_page') }}">
<section class="card" aria-labelledby="drive-heading">
<h2 id="drive-heading">Disc drive</h2>
<label>
Optical drive
<select name="drive_device" required>
{% for drive in drives %}
<option value="{{ drive.device }}"{% if drive.device == values.drive_device %} selected{% endif %}>
{{ drive.name }}
</option>
{% endfor %}
</select>
</label>
<div class="hint">
Choose the drive you will use for movies, episodes and music.
{% if not drives %}No drive is connected yet. You can finish setup now and add one later. If necessary, rerun installation to give Dexter access.{% endif %}
</div>

<details style="margin-top:18px"><summary>Advanced drive settings</summary>
<label>
Audio CD sample offset
<input name="cd_offset" inputmode="numeric" value="{{ values.cd_offset }}"
       placeholder="Optional, for example: 6">
</label>
<div class="hint">
Optional. Leave blank to use a zero-sample offset. Enter your drive's known
offset for the most accurate track boundaries.
</div>
</details>
</section>

<details class="card" {% if connection or (error and values.jellyfin_url) %}open{% endif %}>
<summary>Connect Jellyfin (optional){% if has_api_key %} · Key saved{% endif %}</summary>
<p class="hint">Connect your media server when you are ready to add finished media to its library. You can save this setup without a connection.</p>
<label>
Server address
<input name="jellyfin_url" value="{{ values.jellyfin_url }}"
       placeholder="http://your-media-server:8096">
</label>

<label>
Jellyfin API key
<input name="jellyfin_api_key" type="password" autocomplete="new-password"
       placeholder="{% if has_api_key %}Configured — leave blank to keep it{% else %}Optional{% endif %}">
</label>
<div class="hint">The key is stored separately and is never shown again.</div>
<button class="check-button" type="submit" name="action" value="test-jellyfin" formnovalidate>Check connection and find libraries</button>
<p class="hint">This check does not save changes. Use Save Settings or Finish Setup to keep your address and key.</p>
{% if connection %}
<p role="status"><strong>{{ connection.message }}</strong></p>
{% for library in connection.libraries %}
<p><strong>{{ library.name }}</strong> ({{ library.kind }})<br>
{% for path in library.paths %}<code>{{ path }}</code><br>{% else %}No folder paths returned.{% endfor %}</p>
{% endfor %}
{% if connection.ok %}<p class="hint">These are Jellyfin’s server paths. The connection is verified, and shared storage access is not checked here. See Storage for Dexter’s configured destinations.</p>{% endif %}
{% else %}<p class="hint">Connection not checked in this visit. Saving an address or key does not verify library access.</p>{% endif %}
</details>

<details class="card"{% if error %} open{% endif %}>
<summary>Movie defaults</summary>
<p class="hint">These choices are suggested for each movie. You can change them during review.</p>
<label>Picture quality
<select name="movie_quality">
{% for key, label in quality_choices.items() %}
<option value="{{ key }}"{% if values.movie_quality == key %} selected{% endif %}>{{ label }}</option>
{% endfor %}
</select>
</label>
<label>Destination
<select name="movie_destination">
{% for key, label in destination_choices.items() %}
<option value="{{ key }}"{% if values.movie_destination == key %} selected{% endif %}>{{ label }}</option>
{% endfor %}
</select>
</label>
</details>

<div class="actions">
<button type="submit">{% if first_run %}Finish Setup{% else %}Save Settings{% endif %}</button>
{% if not first_run %}<a class="button" href="{{ url_for('home') }}">Cancel</a>{% endif %}
</div>
</form>

<div>
<section class="card" aria-labelledby="storage-heading">
<h2 id="storage-heading">Storage</h2>
<p class="hint">Dexter stages a copy first. You choose when to import it.</p>
<dl class="storage-list">
<dt>Temporary storage</dt>
<dd>{{ storage.free }} GiB free of {{ storage.total }} GiB<br><code>{{ storage.staging }}</code></dd>
{% if storage.movie_share %}
<dt>Movie imports to Jellyfin</dt>
<dd><code>{{ storage.movie_share }}/movies</code></dd>
<dt>Local finished-media folder</dt>
{% else %}
<dt>Finished-media folder</dt>
{% endif %}
<dd><code>{{ storage.library }}</code></dd>
</dl>
<a href="{{ url_for('storage_page') }}">Choose or change storage</a>
</section>

{% set attention = readiness_checks | rejectattr("available") | list | length %}
<details class="card" {% if attention %}open{% endif %}>
<summary>What this computer can do{% if attention %} · {{ attention }} need attention{% endif %}</summary>
<p class="hint">These checks show installed components and available storage. Scan a disc to check whether it can be read.</p>
{% for check in readiness_checks %}
<div class="readiness-row">
<strong>{{ check.label }}</strong> · {% if check.available %}Available{% else %}Needs attention{% endif %}
<div class="hint">{{ check.detail }}</div>
{% if check.tools %}<details class="hint"><summary>Software details</summary>{{ check.tools }}</details>{% endif %}
</div>
{% endfor %}
<div class="readiness-row">
<strong>NVIDIA video encoding</strong>
{% if not nvenc_enabled %}
<div class="hint">GPU access is not enabled for this installation. CPU encoding is available when HandBrake is ready.</div>
{% else %}
{% for codec, check in nvenc_results.items() %}
<div class="hint">{{ "H.264" if codec == "h264" else "HEVC" }} · {{ "Sample encode passed" if check.available else "Unavailable" }}</div>
{% if check.reason %}<details><summary>Encoder details</summary><code>{{ check.reason }}</code></details>{% endif %}
{% endfor %}
{% endif %}
</div>

</details>
</div>
</div>
</main>
</body>
</html>
"""


PAGE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Magic Media PAL</title>

{% if job.running %}
<meta http-equiv="refresh" content="3">
{% endif %}

<style>
:root {
    color-scheme: dark;
}

* {
    box-sizing: border-box;
}

body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
        sans-serif;
    background: #101010;
    color: #eeeeee;
    margin: 0;
}

header {
    background: #181818;
    border-bottom: 1px solid #333;
    padding: 24px 28px;
}

.header-inner {
    max-width: 1160px;
    margin: auto;
}

.header-title-row {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
}

.header-title-row .button {
    margin-left: auto;
    text-decoration: none;
}

header h1 {
    margin: 0;
    font-size: 1.8rem;
}

.header-meta {
    margin-top: 10px;
    color: #b8b8b8;
    display: flex;
    gap: 9px;
    flex-wrap: wrap;
    align-items: center;
}

.phase-pill,
.meta-pill {
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 4px 10px;
    border: 1px solid #444;
    background: #222;
    font-size: 0.88rem;
}

.phase-Idle {
    color: #bbb;
}

.phase-Ready,
.phase-Verified {
    border-color: #3d7253;
    background: #193023;
    color: #bde6ca;
}

.phase-Scanning,
.phase-Ripping,
.phase-Importing,
.phase-Working {
    border-color: #496d8c;
    background: #182b3a;
    color: #c6e3fb;
}

.phase-Recovery {
    border-color: #826d3d;
    background: #332a17;
    color: #f0d99d;
}

main {
    max-width: 1160px;
    margin: auto;
    padding: 28px;
}

.card {
    background: #1b1b1b;
    border: 1px solid #333;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
}

.controls {
    display: flex;
    gap: 9px;
    flex-wrap: wrap;
    align-items: center;
}

button,
.button {
    appearance: none;
    border: 1px solid #5a5a5a;
    border-radius: 7px;
    background: #efefef;
    color: #111;
    padding: 9px 15px;
    cursor: pointer;
    font-weight: 600;
}

button:hover:not(:disabled) {
    filter: brightness(0.92);
}

button:disabled {
    cursor: not-allowed;
    opacity: 0.42;
}

.button-primary {
    background: #dcecff;
    border-color: #91b9e0;
}

.button-danger {
    background: #2b2020;
    border-color: #865151;
    color: #f4cccc;
}

.button-cancel {
    background: #ead9c0;
    border-color: #aa8757;
    color: #1c160e;
}

.small {
    color: #aaa;
}

.muted {
    color: #999;
}

.help {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    margin-left: 4px;
    border: 1px solid #666;
    border-radius: 50%;
    color: #bbb;
    font-size: 12px;
    font-weight: 700;
    cursor: help;
    vertical-align: middle;
}

.help::after {
    content: attr(data-tip);
    position: absolute;
    z-index: 20;
    left: 50%;
    bottom: calc(100% + 9px);
    width: min(300px, 75vw);
    padding: 9px 11px;
    border: 1px solid #555;
    border-radius: 7px;
    background: #080808;
    color: #eee;
    box-shadow: 0 6px 18px rgba(0,0,0,.45);
    font-size: .82rem;
    font-weight: 400;
    line-height: 1.35;
    opacity: 0;
    pointer-events: none;
    transform: translate(-50%, 4px);
    transition: opacity .15s ease, transform .15s ease;
}

.help:hover::after,
.help:focus::after {
    opacity: 1;
    transform: translate(-50%, 0);
}

.section-intro {
    color: #bbb;
    margin: -4px 0 16px;
}

.technical-details > summary,
.individual-imports > summary {
    cursor: pointer;
    font-weight: 700;
    font-size: 1.05rem;
}

.form-section {
    margin: 14px 0;
    padding: 14px;
    border: 1px solid #3a3a3a;
    border-radius: 8px;
}

.form-section legend {
    padding: 0 7px;
    font-weight: 700;
}

.metadata-match {
    display: block;
    margin-top: 9px;
    padding: 10px;
    border: 1px solid #3f4d55;
    border-radius: 7px;
    background: #182026;
}

a {
    color: #8dc6ff;
}

.card h2,
.card h3 {
    margin-top: 0;
}

.disc-summary {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 16px;
}

.disc-label {
    margin-top: 5px;
}

.job-card {
    border-left-width: 4px;
}

.job-neutral {
    border-left-color: #555;
}

.job-active {
    border-left-color: #5585aa;
}

.job-success {
    border-left-color: #508061;
}

.job-recovery {
    border-left-color: #a58a4f;
    background: #201e18;
}

.job-warning {
    border-left-color: #9d7d45;
}

.job-error {
    border-left-color: #9a5353;
}

.job-title {
    margin-bottom: 8px;
}

.job-status {
    font-weight: 650;
    margin: 0 0 14px 0;
}

.job-description {
    color: #999;
    margin-top: -5px;
    margin-bottom: 14px;
    font-size: 0.92rem;
}

.queue-dashboard {
    margin: 18px 0;
    padding: 14px;
    border: 1px solid #383838;
    border-radius: 10px;
    background: #171717;
}

.queue-dashboard h3 {
    margin-bottom: 10px;
}

.queue-status {
    display: inline-flex;
    border: 1px solid #555;
    border-radius: 999px;
    padding: 3px 9px;
    font-size: .82rem;
    font-weight: 700;
    text-transform: capitalize;
}

.queue-status-ripping {
    border-color: #5585aa;
    background: #182b3a;
    color: #c6e3fb;
}

.queue-status-completed {
    border-color: #508061;
    background: #193023;
    color: #bde6ca;
}

.track-progress {
    min-width: 150px;
}

.track-progress-bar {
    width: 100%;
    height: 8px;
    margin-top: 5px;
    overflow: hidden;
    border-radius: 999px;
    background: #333;
}

.track-progress-fill {
    height: 100%;
    background: #8dc6ff;
}

.queue-status-failed,
.queue-status-cancelled,
.queue-status-interrupted,
.queue-status-skipped {
    border-color: #9a5353;
    background: #382020;
    color: #ffd4d4;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th,
td {
    padding: 9px;
    border-bottom: 1px solid #333;
    text-align: left;
    vertical-align: top;
}

th {
    color: #bbb;
    font-size: 0.88rem;
}

pre {
    white-space: pre-wrap;
    max-height: 420px;
    overflow: auto;
    background: #101010;
    padding: 14px;
    border-radius: 7px;
}

.verbose-log {
    margin-top: 18px;
}

.verbose-log summary,
.system-details summary {
    cursor: pointer;
    font-weight: 600;
    padding: 8px 0;
    user-select: none;
}

.verbose-log summary:hover,
.system-details summary:hover {
    text-decoration: underline;
}

.verbose-log pre {
    margin-top: 8px;
}

.rip-progress {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 14px 18px;
    margin: 18px 0;
}

.progress-indicator { display: grid; justify-items: center; gap: 5px; }
.job-footer { border-top: 1px solid #3c4654; margin-top: 18px; padding-top: 4px; }
.job-footer-actions { display: flex; justify-content: flex-end; margin-top: 12px; }
.progress-ring {
    --ring-fill: #8dc6ff;
    display: grid;
    place-items: center;
    width: 76px;
    aspect-ratio: 1;
    border-radius: 50%;
    background: conic-gradient(var(--ring-fill) var(--progress, 0%), #3b4350 0);
}
.progress-ring strong {
    display: grid;
    place-items: center;
    width: 62px;
    aspect-ratio: 1;
    border-radius: 50%;
    background: #1b1b1b;
    color: #edf5ff;
    font-variant-numeric: tabular-nums;
}
.progress-ring.is-complete { --ring-fill: #a5dfb3; }
.progress-ring.is-indeterminate { background: #3b4350; }
.progress-stage { color: #c7d1df; font-size: .8rem; text-align: center; max-width: 110px; }

.rip-progress-stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
    gap: 8px;
}
.rip-progress-stats > div {
    padding: 9px 11px;
    border: 1px solid #3c4654;
    border-radius: 8px;
    background: #171b21;
    overflow-wrap: anywhere;
}
.rip-progress-stats strong { color: #bac7d9; font-size: .8rem; }
.rip-progress > .rip-progress-file,
.rip-progress > .stall-warning { grid-column: 1 / -1; margin: 0; }

.stall-warning,
.verification-failed {
    margin-top: 14px;
    padding: 12px 14px;
    border: 1px solid #856c42;
    border-radius: 8px;
    background: #292417;
}

.verified-panel {
    margin-top: 18px;
    padding: 16px;
    border: 1px solid #3f6f50;
    border-radius: 9px;
    background: #17241b;
}

.verified-heading {
    font-weight: 700;
    margin-bottom: 10px;
}

.media-details {
    line-height: 1.55;
    margin-bottom: 16px;
}

.import-grid {
    display: grid;
    grid-template-columns: minmax(250px, 1fr) 120px;
    gap: 12px;
    max-width: 620px;
    margin-bottom: 12px;
}

.import-grid label {
    display: block;
    font-weight: 600;
}

.import-grid input,
.import-grid select,
.verified-panel > form > label select {
    width: 100%;
    margin-top: 5px;
    padding: 8px;
    border-radius: 6px;
    border: 1px solid #555;
    background: #fafafa;
    color: #111;
}

.verified-actions {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
}

.verified-actions form {
    margin: 0;
}

.raw-label {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    color: #aaa;
}

.system-details {
    color: #bbb;
}

.system-grid {
    margin-top: 10px;
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 5px 12px;
}

@media (max-width: 760px) {
    main {
        padding: 16px;
    }

    header {
        padding: 18px 16px;
    }

    .rip-progress-stats {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .import-grid {
        grid-template-columns: 1fr;
    }

    .table-wrap {
        overflow-x: auto;
    }
}
.diag-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 14px;
    margin-top: 14px;
}

.diag-item {
    padding: 12px 14px;
    border: 1px solid rgba(255,255,255,.10);
    border-radius: 10px;
}

.diag-label {
    display: block;
    font-size: .78rem;
    opacity: .65;
    margin-bottom: 4px;
}

.diag-value {
    font-weight: 600;
    word-break: break-word;
}

.diag-ok {
    font-weight: 700;
}

.diag-list {
    margin: 8px 0 0 18px;
    padding: 0;
}


.disc-workspace {
    background: linear-gradient(120deg, #17253a, #1c1c1c 70%);
    border-color: #354961;
}
.disc-workspace h2 { margin: 0 0 8px; font-size: clamp(1.5rem, 4vw, 2.4rem); }
.workspace-label { color: #a9cdf6; font-size: .76rem; letter-spacing: .14em; font-weight: 700; }
.workflow-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; list-style: none; padding: 0; margin: 18px 0 0; }
.workflow-steps li { border: 1px solid #526074; border-radius: 8px; padding: 10px; color: #c6cfdb; }
.workflow-steps li[aria-current="step"] { background: #213c5a; border-color: #8cc5ff; color: white; }
.workflow-steps span { font-weight: bold; margin-right: .3em; }
.disc-title-table th:first-child { width: 1%; }
.disc-title-table td:nth-child(n+3) { white-space: nowrap; }
.disc-title-table .title-group th { color: #b5d8ff; text-align: left; }
.title-details { margin-top: 4px; overflow-wrap: anywhere; }
.title-details summary { color: #b8c7d8; cursor: pointer; }
.file-actions { margin-top: 14px; }
.file-actions form { margin-top: 10px; }
@media (max-width: 540px) {
    .workflow-steps { grid-template-columns: 1fr; }
}

.header-inner, main.workspace-layout { max-width: 1540px; }
.workspace-layout {
    display: grid;
    grid-template-columns: minmax(230px, 280px) minmax(0, 1fr);
    gap: 22px;
    align-items: start;
}
.workspace-sidebar {
    position: sticky;
    top: 16px;
    max-height: calc(100vh - 32px);
    overflow: auto;
    scrollbar-width: thin;
}
.workspace-content { min-width: 0; }
.workspace-sidebar .card { margin-bottom: 12px; padding: 16px; }
.workspace-sidebar .disc-workspace h2 { font-size: 1.45rem; overflow-wrap: anywhere; }
.workspace-sidebar .workflow-steps { grid-template-columns: 1fr; }
.workspace-sidebar .controls { display: grid; grid-template-columns: 1fr 1fr; }
.workspace-sidebar .controls form:first-child,
.workspace-sidebar .controls > .small { grid-column: 1 / -1; }
.workspace-sidebar .controls button { width: 100%; }
.workspace-sidebar summary { cursor: pointer; }
.workspace-sidebar summary small { display: block; margin: 4px 0 0 16px; color: #b8c7d8; }
.workspace-sidebar .diag-grid, .workspace-sidebar .system-grid { grid-template-columns: 1fr; }
.workspace-sidebar .system-grid { margin-top: 12px; overflow-wrap: anywhere; }
.workspace-sidebar .system-grid span { margin-bottom: 5px; }
@media (max-width: 800px) {
    .workspace-layout { grid-template-columns: minmax(0, 1fr); gap: 12px; }
    .workspace-sidebar { position: static; max-height: none; overflow: visible; }
}
</style>
<link rel="stylesheet" href="/assets/dexter.css">
</head>

<body class="wizard-page dashboard-page">

<header>
<div class="header-inner">

<div class="header-title-row">
<h1>Magic Media PAL</h1>
<span class="phase-pill phase-{{ phase }}">{{ phase }}</span>
<a class="button" href="{{ url_for('movie_wizard') }}">Prepare a movie</a>
<a class="button" href="{{ url_for('workflow_queue_page') }}">Queue</a>
<a class="button" href="{{ url_for('movie_history_page') }}">History</a>
<a class="button" href="{{ url_for('settings_page') }}">Settings</a>
</div>

</div>
</header>


{% macro progress_ring(percent, stage, complete=false) %}
<div class="progress-indicator">
{% if percent is not none %}
{% set value = [[percent, 0] | max, 100] | min %}
<div class="progress-ring{% if complete %} is-complete{% endif %}" style="--progress: {{ value }}%" role="progressbar"
     aria-label="{{ stage }}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{{ value }}">
<strong aria-hidden="true">{{ value | int }}%</strong>
</div>
{% else %}
<div class="progress-ring is-indeterminate" role="progressbar" aria-label="{{ stage }}">
<strong aria-hidden="true">…</strong>
</div>
{% endif %}
<span class="progress-stage">{{ stage }}</span>
</div>
{% endmacro %}

{% macro movie_workflow_fields(suggested_title, tracks=[], staged=false) %}
<div class="import-grid">
<label>Picture quality
<select name="workflow_quality">
{% for key, label in quality_choices.items() %}
<option value="{{ key }}"{% if ("original" if staged else workflow_default_quality) == key %} selected{% endif %}>{{ "Keep current video · no new compression" if key == "original" else label }}</option>
{% endfor %}
</select>
</label>
<label>Compression format
<select name="workflow_codec">
<option value="h265">HEVC · smaller movie</option>
<option value="h264">H.264 · wider playback support</option>
</select>
</label>
</div>
<label>Encoding device
<select name="workflow_engine">
<option value="auto">Automatic · try NVIDIA, then CPU</option>
<option value="gpu">NVIDIA GPU</option>
<option value="cpu">CPU</option>
</select>
</label>
<p class="small">Automatic allows a CPU retry. To prepare an 8-bit source, Dexter may deinterlace or convert its pixel format. The original stays in staging.</p>
<fieldset>
<legend>Sound and subtitles</legend>
<label>Playback format
<select name="workflow_container">
<option value="mp4">MP4 · tested with Jellyfin and Fire TV</option>
<option value="mkv">MKV · keep selected subtitles and original sound</option>
<option value="preserve">MKV · keep every original track (advanced)</option>
</select></label>
<label>Audio language
<select name="workflow_language">
<option value="eng">English</option><option value="deu">German</option><option value="fra">French</option>
<option value="spa">Spanish</option><option value="ita">Italian</option><option value="jpn">Japanese</option>
<option value="por">Portuguese</option><option value="zho">Chinese</option><option value="und">Unspecified</option>
</select></label>
<label><input type="checkbox" name="workflow_surround" value="yes" checked> Require surround sound (5.1 or more)</label>
<label>Subtitles
<select name="workflow_subtitles"><option value="no">No</option><option value="yes">Yes · keep a selected track in MKV</option></select></label>
<label>Subtitle language
<select name="workflow_subtitle_language">
<option value="eng">English</option><option value="deu">German</option><option value="fra">French</option>
<option value="spa">Spanish</option><option value="ita">Italian</option><option value="jpn">Japanese</option>
<option value="por">Portuguese</option><option value="zho">Chinese</option><option value="und">Unspecified</option>
</select></label>
{% if tracks %}
<details><summary>Choose an exact track</summary>
<label>Sound track<select name="workflow_audio_track"><option value="">Choose from my language and surround preferences</option>
{% for track in tracks if track.kind == "audio" %}<option value="{{ track.id }}">{{ track.label }}</option>{% endfor %}
</select></label>
<label>Subtitle track<select name="workflow_subtitle_track"><option value="">Choose from my language preference</option>
{% for track in tracks if track.kind == "subtitle" %}<option value="{{ track.id }}">{{ track.label }}</option>{% endfor %}
</select></label>
</details>
{% endif %}
<p class="small">The tested MP4 profile copies one Dolby Digital sound track. Selected subtitles stay in MKV. If the matching track is missing or ambiguous, Dexter pauses with the extracted movie ready for your choice.</p>
</fieldset>
<p class="small">Dexter needs to extract and inspect this title before choosing how to encode it. You can request compression now. If encoding stops, your verified original stays in staging for review.</p>
<label>Destination
<select name="workflow_destination">
<option value="staging"{% if not workflow_import_available or default_destination == "staging" %} selected{% endif %}>Keep in staging for review</option>
{% if workflow_import_available %}
<option value="jellyfin"{% if default_destination == "jellyfin" %} selected{% endif %}>Add the verified copy to Jellyfin Movies</option>
{% endif %}
</select>
</label>
<div class="import-grid">
<label>Movie title <input name="workflow_title" value="{{ suggested_title }}"></label>
<label>Release year <input name="workflow_year" type="text" inputmode="numeric" maxlength="4" placeholder="Optional"></label>
</div>
{% endmacro %}

<main class="workspace-layout">
<aside class="workspace-sidebar" aria-label="Disc and drive controls">
<section class="card disc-workspace" aria-labelledby="disc-workspace-title">
<p class="workspace-label">DISC TO LIBRARY</p>
<h2 id="disc-workspace-title">{{ human_disc_title or "Your disc workspace" }}</h2>
{% set added = job.status.startswith(("Movie added.", "Movie copied and verified.", "Episode added.", "Episode copied and verified.", "Imported to Jellyfin:")) if job.status else false %}
{% set active_step = 3 if phase == "Importing" or added or job.import_failed else 2 if phase in ("Verified", "Encoding", "Verifying", "Recovery") and staged_outputs else 1 %}
<p class="section-intro">
{% if phase == "Ripping" %}Copying your selected titles. Follow the progress in the job panel.
{% elif phase == "Importing" %}Dexter is adding your file to the library. Follow the progress in the job panel.
{% elif added %}Your file was added to the library.
{% elif job_tone == "error" %}See what needs attention below.
{% elif phase == "Scanning" %}Dexter is reading your disc.
{% elif phase in ("Encoding", "Verifying") %}Dexter is preparing and checking your file.
{% elif staged_outputs %}Review your files and choose what to add to the library.
{% elif disc and disc.kind in ("video", "audio") %}Choose the titles or tracks you want to copy.
{% else %}Insert a disc and select Scan Disc.
{% endif %}
</p>
<ol class="workflow-steps" aria-label="Disc workflow">
{% for label in ("Choose and rip", "Review and prepare", "Add to library") %}
<li{% if loop.index == active_step %} aria-current="step"{% endif %}><span>{{ loop.index }}</span> {{ label }}</li>
{% endfor %}
</ol>
</section>

<div class="card controls">

<form
    method="post"
    action="/scan"
    {% if staging_has_media %}
    onsubmit="return confirm('Start a new scan and delete the temporary files from previous rips?')"
    {% endif %}
>
{% if staging_has_media %}<input type="hidden" name="clear_staging" value="yes">{% endif %}
<button
    type="submit"
    class="button-primary"
    {% if job.running %}disabled{% endif %}
>
{% if phase == "Scanning" %}
Scanning…
{% else %}
Scan Disc
{% endif %}
</button>

</form>

<form method="post" action="/eject">
<button
    type="submit"
    {% if job.running %}disabled{% endif %}
>
Eject
</button>
</form>

<form method="post" action="/close-tray">
<button
    type="submit"
    {% if job.running %}disabled{% endif %}
>
Close Tray
</button>
</form>

{% if job.running %}
<span class="small">
Drive controls are locked while {{ phase | lower }}.
</span>
{% endif %}

</div>


<div class="card">
<details class="system-details">
<summary>Staging storage{% if staging_storage %}<small>{{ staging_storage.free }} free</small>{% endif %}</summary>

<div class="system-grid">
<strong>Drive</strong>
<span>{{ drive_name }}</span>

<strong>Device</strong>
<span>{{ drive }}</span>

<strong>Video staging</strong>
<span>{{ video_staging }}</span>

<strong>Audio staging</strong>
<span>{{ audio_staging }}</span>

<strong>Staging space</strong>
<span>
{% if staging_storage %}
{{ staging_storage.free }} free of {{ staging_storage.total }}
{% else %}
Unavailable
{% endif %}
</span>

<strong>Recovery</strong>
<span>{{ recovery_staging }}</span>
</div>

</details>
</div>

<div class="card">
<details class="technical-details">
<summary>Drive and disc details</summary>
{% if disc and disc.kind == "video" %}
<p><button type="button" onclick="window.open('http://' + location.hostname + ':5800')" {% if job.running %}disabled{% endif %}>Open MakeMKV GUI</button></p>
{% endif %}
<p class="small">Useful for troubleshooting disc detection and read problems.</p>

<div class="diag-grid">

<div class="diag-item">
<span class="diag-label">Optical Drive</span>
<div class="diag-value">
{{ diagnostics.drive_vendor }}
{{ diagnostics.drive_model }}
</div>
</div>

<div class="diag-item">
<span class="diag-label">Firmware</span>
<div class="diag-value">
{{ diagnostics.firmware or "Unknown" }}
</div>
</div>

<div class="diag-item">
<span class="diag-label">Linux Devices <span class="help" tabindex="0" data-tip="The Linux device paths used to access the optical drive. These normally do not need to be changed.">?</span></span>
<div class="diag-value">
{{ diagnostics.device }}
{% if diagnostics.sg_device %}
· {{ diagnostics.sg_device }}
{% endif %}
</div>
</div>

<div class="diag-item">
<span class="diag-label">MakeMKV</span>
<div class="diag-value">
{% if diagnostics.makemkv_version %}
v{{ diagnostics.makemkv_version }}
{% else %}
Not scanned yet
{% endif %}
</div>
</div>

<div class="diag-item">
<span class="diag-label">LibreDrive <span class="help" tabindex="0" data-tip="A MakeMKV drive-access mode that can improve Blu-ray compatibility and bypass some firmware limitations.">?</span></span>
<div class="diag-value diag-ok">
{% if diagnostics.libredrive_enabled %}
Enabled
{% else %}
Not reported
{% endif %}
</div>

{% if diagnostics.libredrive_detail %}
<div class="small">
{{ diagnostics.libredrive_detail }}
</div>
{% endif %}
</div>

<div class="diag-item">
<span class="diag-label">Media</span>
<div class="diag-value">
{{ diagnostics.media_type or "No disc scanned" }}
</div>
</div>

<div class="diag-item">
<span class="diag-label">Disc</span>
<div class="diag-value">
{{ diagnostics.disc_title or "Not identified" }}
</div>
</div>

<div class="diag-item">
<span class="diag-label">Java / BD-J <span class="help" tabindex="0" data-tip="Some Blu-rays use Java menus and playlist logic. Missing Java can make certain discs harder to analyze, but most discs still rip normally.">?</span></span>
<div class="diag-value">
{{ diagnostics.java_bdjava }}
</div>
</div>

</div>

<div style="margin-top:16px">

<strong>Protection / Read Diagnostics</strong>

{% if diagnostics.protection_messages %}
<ul class="diag-list">
{% for message in diagnostics.protection_messages %}
<li>{{ message }}</li>
{% endfor %}
</ul>
{% else %}
<div class="small" style="margin-top:6px">
No protection or read-integrity warnings reported by MakeMKV.
</div>
{% endif %}

</div>

</div>
</details>


</aside>

<div class="workspace-content">
{% if batch_overview %}
<section class="card batch-overview" aria-label="Your planned copies">
<h2>{{ "Your episodes" if batch_overview.is_tv else "Your planned copies" }}</h2>
<p><strong>{{ batch_overview.done }} of {{ batch_overview.total }} {{ "episodes" if batch_overview.is_tv else "copies" }} ready</strong>
{% if batch_overview.current %} · Now {{ batch_overview.current.position }} of {{ batch_overview.total }}{% endif %}</p>
<ol class="batch-list">{% for item in batch_overview.rows %}
<li class="batch-row batch-{{ item.state }}" {% if batch_overview.current and item.id == batch_overview.current.id %}aria-current="step"{% endif %}>
<span class="batch-mark" aria-hidden="true">{{ "✓" if item.state == "completed" else "●" if item.state in ("running","starting") else item.position }}</span>
<span><strong>{% if item.episode %}Episode {{ item.episode }} · {% endif %}{{ item.name }}</strong><span class="batch-state">{{ item.status }}</span></span>
</li>{% endfor %}</ol>
{% if batch_overview.current %}<div class="batch-steps" aria-label="Current copy steps">{% for step in batch_overview.steps %}<span class="batch-step-{{ step.state }}">{{ "✓ " if step.state == "done" else "" }}{{ step.name }}</span>{% endfor %}</div>{% endif %}
<a class="batch-queue-link" href="/workflow-queue">Queue and recovery options</a>
</section>
{% endif %}
{% if mode != "music" %}
<div class="card job-card job-{{ job_tone }}" id="job-card">

{% if job.processing_report and job.processing_report.output_bytes %}
<details class="card"><summary>Processing results · {{ job.processing_report.saved_percent }}% less space</summary>
<p>Starting copy {{ format_size(job.processing_report.original_bytes) }} · Finished {{ format_size(job.processing_report.output_bytes) }}</p>
<p>{{ job.processing_report.validation or "Encoding and stream timing checks passed" }}</p>
{% if job.processing_report.extraction_seconds is defined %}<div class="small">Read disc · {{ format_duration(job.processing_report.extraction_seconds) }} · Source check {{ format_duration(job.processing_report.source_check_seconds) }}</div>{% endif %}
<p class="small">Backend choice {{ job.processing_report.engine }} · Picture quality {{ job.processing_report.quality }}</p>
{% for stage, seconds in job.processing_report.stage_seconds.items() %}
<div class="small">{{ stage | capitalize }} · {{ format_duration(seconds) }}</div>
{% endfor %}
<p class="small">Technical checks passed. Playback and seeking should also be checked on your player.</p>
</details>
{% endif %}
{% set processing_details %}
{% if job.running and job.movie_workflow %}
<div class="card">
<h2>Processing {{ job.movie_workflow.title }}</h2>
{% set operation = "import" if job_operation_kind(job) == "import" else "rip" if job_operation_kind(job) == "rip" else job.stage or "queued" %}
<p class="small">Current stage · {{ {"rip":"Reading disc", "queued":"Preparing", "inspecting":"Inspecting source", "testing":"Testing encoding path", "encoding":"Encoding", "verifying":"Checking encoded media", "packaging":"Packaging", "validating":"Validating playback copy", "import":"Importing to library"}.get(operation, operation | capitalize) }}</p>
<div class="processing-stages" aria-label="Processing stages">
{% for key,label in (("rip","Read disc"),("inspecting","Check source"),("encoding","Encode"),("packaging","Package"),("validating","Validate"),("import","Add to library")) %}
<span class="meta-pill"{% if operation == key %} aria-current="step" style="border-color:#b479ed;color:#e0b4ff"{% endif %}>{{ label }}</span>
{% endfor %}
</div>
<p class="small">Progress below applies to the current stage. Preparation and verification may take time without a measurable percentage.</p>
</div>
{% endif %}
{% endset %}
{% if not batch_overview %}{{ processing_details }}{% endif %}
{% set finished_movie = job.movie_import and not job.running %}
{% if finished_movie %}
<h2 class="job-title">{{ "Added to Jellyfin" if job.movie_import.item_id else "Episode copied to Jellyfin" if job.movie_import.episode else "Movie copied to Jellyfin" }}</h2>
<p><strong>{{ job.movie_import.display }}</strong></p>
{% if job.movie_import.item_id %}
<a class="button button-primary" href="{{ jellyfin_url }}/web/#/details?id={{ job.movie_import.item_id }}">Open in Jellyfin</a>
{% else %}
<p class="small">{% if job.movie_import.checked %}Jellyfin hasn’t listed this file yet. Check again after its library scan.{% else %}Check Jellyfin for this copy after its library scan.{% endif %}</p>
<form method="post" action="/jellyfin/check-movie"><button type="submit">Check Jellyfin for this {{ "episode" if job.movie_import.episode else "movie" }}</button></form>
{% endif %}
<form class="file-actions" method="post" action="/scan" {% if staging_has_media %}onsubmit="return confirm('Start a new scan and delete the temporary files from previous rips?')"{% endif %}>
{% if staging_has_media %}<input type="hidden" name="clear_staging" value="yes">{% endif %}
<button type="submit">Scan another disc</button>
</form>
{% else %}
<h2 class="job-title">{{ "Copying from disc" if phase == "Ripping" else job_heading }}</h2>
{% endif %}

{% if job.running and job.movie_workflow and not batch_overview %}
<p class="small">Planned workflow: {{ quality_choices.get(job.movie_workflow.profile, "Keep original") }} · {{ movie_destinations.get(job.movie_workflow.destination, "Keep in staging") }}</p>
{% endif %}

{% if not finished_movie and job.status and job.status != "Idle" %}
{% if not batch_overview or job.status != "Running" %}<p class="job-status">{{ job.status }}</p>{% endif %}
{% elif not job.running and not job.verification %}
<p class="job-status muted">Idle</p>
{% endif %}

{% if job.description
      and job.description != job_heading
      and not job.running
      and not job.description.startswith("MakeMKV title") %}
<div class="job-description">
Previous job: {{ job.description }}
</div>
{% endif %}



{% if job_tone == "error" and job.import_retry %}
<form method="post" action="/import-jellyfin" style="margin:14px 0">
<input type="hidden" name="output_file" value="{{ job.import_retry.source }}">
<input type="hidden" name="movie_title" value="{{ job.import_retry.title }}">
<input type="hidden" name="year" value="{{ job.import_retry.year }}">
<button class="button-primary" type="submit">Try Jellyfin import again</button>
<span class="small">Dexter will use your staged copy.</span>
</form>
{% endif %}

{% if job_tone == "error" and job.video_retry_token and disc and disc.kind == "video" %}
<form method="post" action="/rip/video/retry" style="margin:14px 0">
<input type="hidden" name="retry_token" value="{{ job.video_retry_token }}">
<button class="button-primary" type="submit">Try rip again</button>
<span class="small">Keep the same disc inserted to retry your selection.</span>
</form>
{% endif %}

{% set compact_review = not job.running and all_outputs_verified and job_tone != "error" %}
{% set rip_queue %}
{% if job.title_queue %}
<div class="queue-dashboard">
<h3>Rip Queue</h3>
<div class="table-wrap">
<table>
<tr>
<th>Title</th>
<th>Name</th>
<th>Duration</th>
<th>Size</th>
<th>Status</th>
</tr>
{% for item in job.title_queue %}
<tr>
<td>{{ item.id }}</td>
<td>{{ item.name }}</td>
<td>{{ item.duration }}</td>
<td>{{ item.size }}</td>
<td>
<span class="queue-status queue-status-{{ item.status }}">
{{ item.status }}
</span>
</td>
</tr>
{% endfor %}
</table>
</div>
</div>
{% endif %}
{% endset %}
{% if not compact_review and not batch_overview %}{{ rip_queue }}{% endif %}

{% if finished_movie and staged_outputs %}
<details class="file-actions"><summary>Staged copy and file actions</summary>
{% endif %}
{% if mode == "movie" and not job.running %}

{% if staged_outputs | length > 1 and all_outputs_verified %}
<div class="verified-panel">
<div class="verified-heading">IMPORT ALL AS ONE</div>
<p class="section-intro">
Choose how these files belong together, confirm the name, then publish the
whole group to Jellyfin in one step.
</p>
<form method="post" action="/import-jellyfin-batch">

<label>
What are you importing?
<select name="media_type" id="media-type" required onchange="updateMediaFields()">
<option value="movie" {% if metadata_draft.get('media_type') == 'movie' %}selected{% endif %}>Movie with extras</option>
<option value="tv" {% if metadata_draft.get('media_type') == 'tv' %}selected{% endif %}>TV season</option>
</select>
</label>

<fieldset class="form-section">
<legend>Name and identification</legend>
<div class="import-grid">
<label>
Title
<input type="text" name="title" required value="{{ metadata_draft.get('title') or movie_title_suggestion }}">
</label>
<label>
Release year <span class="help" tabindex="0" data-tip="Optional, but including the year helps Jellyfin distinguish titles with the same name.">?</span>
<input type="number" name="year" min="1888" max="2100" placeholder="Optional" value="{{ metadata_draft.get('year','') }}">
</label>
</div>

<button type="submit" formaction="/metadata-lookup" class="button">
Search for Title
</button>

{% if metadata_results %}
<div style="margin:14px 0">
<strong>Choose a match, or keep your typed name</strong>
{% for result in metadata_results %}
<label class="metadata-match">
<input type="radio" name="metadata_choice" value="{{ loop.index0 }}" {% if loop.first %}checked{% endif %}>
{{ result.name }}{% if result.year %} ({{ result.year }}){% endif %}
— {{ result.provider }}
{% if result.overview %}<span class="small">{{ result.overview }}</span>{% endif %}
</label>
{% endfor %}
</div>
{% endif %}
</fieldset>

<fieldset class="form-section movie-fields">
<legend>Movie files</legend>
<label>
Main movie file <span class="help" tabindex="0" data-tip="Choose the full movie. Every other file in this batch will be placed in Jellyfin's extras folder.">?</span>
<select name="primary_file">
{% for output in staged_outputs %}
<option value="{{ output.path }}">{{ output.verification.filename }}</option>
{% endfor %}
</select>
</label>
</fieldset>

<fieldset class="form-section tv-fields">
<legend>Episode numbering</legend>
<div class="import-grid">
<label>
Season <span class="help" tabindex="0" data-tip="Season 0 is normally used for specials. Standard seasons begin at 1.">?</span>
<input type="number" name="season" min="0" max="999" value="{{ metadata_draft.get('season',1) }}">
</label>
<label>
First episode number <span class="help" tabindex="0" data-tip="The first ripped file gets this episode number. Remaining files are numbered consecutively in rip order.">?</span>
<input type="number" name="start_episode" min="1" max="9999" value="{{ metadata_draft.get('start_episode',1) }}">
</label>
</div>
<p class="small">
Files are assigned consecutive episode numbers in rip order.
</p>
<p class="small">TV metadata provided by TVmaze under CC BY-SA.</p>
</fieldset>
<button type="submit" class="button-primary" {% if job.running %}disabled{% endif %}>
Import Entire Batch
</button>
</form>
</div>
{% endif %}

{% if staged_outputs | length > 1 %}
<details class="individual-imports" style="margin-top:18px">
<summary>Individual file options</summary>
<p class="small">Use these only when the files do not belong together.</p>
{% endif %}
{% for output in staged_outputs %}

{% if output.verification.ok %}

<div class="verified-panel">

<div class="verified-heading">✓ RIP VERIFIED</div>

<div class="media-details">
<strong>{{ output.verification.filename }}</strong>
<div class="small">
{{ output.verification.duration }} · {{ output.verification.size }}
{% if output.verification.width %} · {{ output.verification.width }}×{{ output.verification.height }}{% endif %}
</div>
</div>

<details style="margin:14px 0">
<summary>Choose quality and storage · {{ default_quality | capitalize if output.compression.available and default_quality in ("balanced", "compact") else "Keep original" }}</summary>
<p class="small">This copy stays as it is unless you choose to make a smaller one.</p>
<p><strong>Keep this copy as it is</strong></p>
<p class="small">No further video compression. Add this verified copy to your library using the button below.</p>
{% if output.compression.available %}
<p><strong>Prepare your playback copy</strong></p>
<form method="post" action="/workflow/staged-movie">
<input type="hidden" name="output_file" value="{{ output.path }}">
{{ movie_workflow_fields(output.suggestion, output.verification.tracks or [], staged=true) }}
<button type="submit" {% if job.running %}disabled{% endif %}>Prepare from this copy</button>
</form>
<details><summary>Advanced MKV compression controls</summary>
<p class="small">This separate preservation path keeps every original sound and subtitle track in MKV.</p>
<form method="post" action="/encode-video">
<input type="hidden" name="output_file" value="{{ output.path }}">
<label>What matters most? <select name="profile" aria-describedby="quality-tradeoffs-{{ loop.index }}">
<option value="balanced"{% if default_quality != "compact" %} selected{% endif %}>Balanced — keep more picture detail</option><option value="compact"{% if default_quality == "compact" %} selected{% endif %}>Compact — favor saving space</option>
<option value="custom">Custom — use the advanced quality setting</option></select></label>
<p id="quality-tradeoffs-{{ loop.index }}" class="small">Balanced uses less aggressive compression. Compact may show more visible changes. The final size depends on the video; savings are not guaranteed.</p>
<label>Where will you watch? <select name="codec">
{% for format in output.compression.formats %}
<option value="{{ format.id }}" {% if not format.available %}disabled{% endif %}
{% if format.id == output.compression.default_codec %}selected{% endif %}>{{ format.label }}{% if not format.available %} — unavailable{% endif %}</option>
{% endfor %}
</select></label>
{% for format in output.compression.formats %}
<p class="small"><strong>{{ format.label }}:</strong> {{ format.description }}{% if not format.available %} {{ format.reason }}{% endif %}</p>
{% endfor %}
<label>Encoding device
<select name="engine">
<option value="cpu">CPU</option>
<option value="gpu">NVIDIA GPU</option>
<option value="auto">Automatic · try NVIDIA, then CPU</option>
</select>
</label>
<p class="small">Automatic allows a CPU retry. To prepare an 8-bit source, Dexter may deinterlace or convert its pixel format. The original stays in staging.</p>
<details><summary>Advanced picture settings</summary>
<label>Custom quality (CPU RF or NVIDIA CQ) <input type="number" name="quality" min="0" max="51" step="0.5" placeholder="Required only for Custom"></label>
<p class="small">Lower quality numbers favor detail and larger files. Higher numbers favor smaller files. CPU RF and NVIDIA CQ use separate scales. Values are specific to the encoder and are not a quality percentage. Resolution is not deliberately reduced.</p>
</details>
<button type="submit" {% if job.running %}disabled{% endif %}>Make smaller copy and check it</button>
</form>
</details>
{% else %}
<p class="small" role="status">{{ output.compression.reason }}</p>
{% endif %}
</details>

<form method="post" action="/import-jellyfin">
<input type="hidden" name="output_file" value="{{ output.path }}">

<p class="small">Movie: <strong>{{ output.suggestion }}</strong></p>
<details class="file-actions">
<summary>Destination · {{ movie_destinations.get(default_destination, "Jellyfin Movies") }}</summary>
<label>Send this copy to
<select name="destination" onchange="updateMovieDestination(this)">
{% for key, label in movie_destinations.items() %}
<option value="{{ key }}"{% if default_destination == key %} selected{% endif %}>{{ label }}</option>
{% endfor %}
</select>
</label>
</details>
<details class="file-actions">
<summary>Edit title and year</summary>
<div class="import-grid">

<label>
Movie title
<input
    type="text"
    name="movie_title"
    required
    value="{{ output.suggestion }}"
>
</label>

<label>
Year
<input
    type="number"
    name="year"
    min="1888"
    max="2100"
    placeholder="Year"
>
</label>

</div>

</details>

<div class="verified-actions">
<button
    type="submit"
    class="button-primary"
    {% if job.running %}disabled{% endif %}
>
{{ "Keep in staging" if default_destination == "staging" else "Import to Jellyfin" }}
</button>
</div>

</form>


{% if not output.compression.available %}
<details class="file-actions">
<summary>Prepare a playback copy</summary>
<form method="post" action="/workflow/staged-movie">
<input type="hidden" name="output_file" value="{{ output.path }}">
{{ movie_workflow_fields(output.suggestion, output.verification.tracks or [], staged=true) }}
<button type="submit" {% if job.running %}disabled{% endif %}>Prepare from this copy</button>
</form>
</details>
{% endif %}

<details class="file-actions">
<summary>File details and actions</summary>
<p class="small">
Video {{ output.verification.video_codec or "unknown" }}
{% if output.verification.audio_codec %} · Audio {{ output.verification.audio_codec }}{% endif %}
{% if output.verification.channels %} · {{ output.verification.channels }} channels{% endif %}
</p>
<form method="post" action="/delete-output" onsubmit="return confirm('Delete this temporary staged copy?')">
<input type="hidden" name="output_file" value="{{ output.path }}">
<button
    type="submit"
    class="button-danger"
    {% if job.running %}disabled{% endif %}
>
Delete Staged File
</button>
</form>
</details>

</div>

{% else %}

<div class="verification-failed">
<strong>⚠ VERIFICATION FAILED</strong><br>
{{ output.verification.error }}
</div>

{% endif %}
{% endfor %}
{% if staged_outputs | length > 1 %}
</details>
{% endif %}

{% endif %}


{% if finished_movie and staged_outputs %}</details>{% endif %}
{% if compact_review and (job.title_queue or job.progress or job.log) %}
<details class="verbose-log">
<summary>Completed job details</summary>
{{ rip_queue }}
{% endif %}

{% if job.running and job.progress.heartbeat_at %}
{% set inactive = job.progress.inactive_seconds or 0 %}
<p class="activity-note{% if inactive >= 30 %} warning{% endif %}" role="status">
{% if inactive >= 30 %}No new media progress for {{ format_duration(inactive) }}. The process is still running and may be waiting for storage.
{% else %}Media activity reported {{ format_duration(inactive) }} ago.{% endif %}
{% if job.progress.verification_label %} {{ job.progress.verification_label }}.{% endif %}
</p>
{% endif %}
{% if job.progress or cancellable %}
<div class="job-footer" aria-label="Job information and controls">
{% endif %}
{% if job.progress and (job.running or job.verification) %}
<div class="rip-progress">

{% set checking = job.progress.checking_copy or phase in ("Verifying","Validating") %}
{% set stage = (batch_overview.current.status if batch_overview and batch_overview.current else job.progress.verification_label) or ("Checking the copy" if checking else "Complete" if not job.running else "Copying to Jellyfin" if phase == "Importing" else "Packaging media" if phase == "Packaging" else "Making a smaller copy" if phase == "Encoding" else "Copying from disc") %}
{{ progress_ring(none if job.progress.checking_copy else job.progress.get("percent"), stage, not job.running) }}

<div class="rip-progress-stats">

<div>
<strong>
{% if job.progress.media_duration %}Checked{% elif phase == "Importing" %}Copied{% elif mode == "music" %}Tracks{% else %}Copy size{% endif %}
</strong><br>
{% if job.progress.media_duration %}
{{ format_duration(job.progress.processed_seconds or 0) }} / {{ format_duration(job.progress.media_duration) }}
{% else %}{{ job.progress.written or "—" }}{% endif %}

{% if job.running and job.progress.expected %}
 / {{ job.progress.expected }}
{% endif %}
</div>

<div>
<strong title="Time spent on the current step">Time spent</strong><br>
{{ job.progress.elapsed or "—" }}
</div>

<div>
<strong>
{% if mode == "music" %}Current track{% else %}Speed{% endif %}
</strong><br>
{% if mode == "music" %}
{{ job.progress.filename or "Preparing disc…" }}
{% else %}
{{ (job.progress.rate or "—") | replace("× media speed", "× playback") }}
{% endif %}
</div>

<div>
<strong>{% if job.running and (not checking or job.progress.eta) %}Time left{% else %}Status{% endif %}</strong><br>

{% if checking %}
{{ job.progress.eta or ("Checking the copy…") }}
{% elif job.running %}
{{ job.progress.eta or "Calculating…" }}
{% else %}
Complete
{% endif %}
</div>

{% set media = namespace(current=none) %}
{% for output in staged_outputs %}
{% if output.verification.ok and output.verification.filename == job.progress.filename %}
{% set media.current = output.verification %}
{% endif %}
{% endfor %}
{% if media.current and media.current.video_codec %}
<div><strong>Video</strong><br>
{% if media.current.width and media.current.height %}{{ media.current.width }}×{{ media.current.height }} · {% endif %}{{ media.current.video_codec | upper }}
</div>
{% endif %}
{% if media.current and media.current.audio_codec %}
<div><strong>Audio</strong><br>{{ media.current.audio_codec | upper }}{% if media.current.channels %} · {{ media.current.channels }} channels{% endif %}</div>
{% endif %}
</div>


{% if job.running and job.progress.stalled %}
<div class="stall-warning">
<strong>Possible read stall</strong><br>
No output growth for {{ job.progress.stall_time }}.
MakeMKV may be retrying a dirty or unreadable area of the disc.
You can let it continue or cancel the rip.
</div>
{% endif %}


{% if job.progress.filename and mode != "music" %}
<details class="small rip-progress-file"><summary>File details</summary><div>
{% if job.running %}
{% if phase == "Importing" %}Copying{% else %}Writing{% endif %}
{% else %}
Output
{% endif %}
:
{{ job.progress.filename }}
</div></details>
{% endif %}

</div>
{% endif %}


{% if cancellable %}
<form method="post" action="/cancel-job" class="job-footer-actions">
<button type="submit" class="button-cancel">
{{ cancel_label }}
</button>
</form>
{% endif %}
{% if job.progress or cancellable %}</div>{% endif %}

{% if job.log or (batch_overview and job.movie_workflow) %}
<details class="verbose-log">
<summary>Details{% if job.log %} · {{ job.log | length }} log lines{% endif %}</summary>
{% if batch_overview %}{{ processing_details }}{{ rip_queue }}{% endif %}
<pre>{{ job.log | join('\n') }}</pre>
</details>
{% endif %}
{% if compact_review and (job.title_queue or job.progress or job.log) %}
</details>
{% endif %}


</div>
{% endif %}


{% if disc and not (job.running and mode == "movie") and ((mode == "music" and disc.kind == "audio") or (mode == "movie" and disc.kind != "audio")) %}
<div class="card">

{% set collapse_titles = disc.kind == "video" and all_outputs_verified and job_tone != "error" %}
{% if collapse_titles %}
<details class="additional-titles">
<summary>Rip more from this disc</summary>
{% endif %}

<div class="disc-summary">
<span class="meta-pill">{{ disc.type }}</span>

{% if disc.title %}
<span class="meta-pill">
{{ human_disc_title or disc.title }}
</span>
{% endif %}
</div>

{% if disc.label %}
<div class="disc-label small">
Raw disc label:
<span class="raw-label">{{ disc.label }}</span>
</div>
{% endif %}


{% if disc.kind == "video" %}
<details class="additional-titles">
<summary>Advanced disc title controls</summary>
<p><a class="button button-primary" href="/wizard">Prepare a movie step by step</a></p>
<h3>Choose What to Rip</h3>
<p class="section-intro">
Select the movie, episodes, or extras you want, then click Rip Selected.
The scan can show video format and size. Dexter checks whether it can make a smaller copy once the file is staged.
</p>

<form method="post" action="/rip/video">
<div class="table-wrap">
<table class="disc-title-table">
<thead><tr><th scope="col">Select</th><th scope="col">Title</th><th scope="col">Duration</th><th scope="col">Size</th></tr></thead>
{% for extra in (false, true) %}
{% set titles = (disc.titles | selectattr("category", "equalto", "Extra") | list) if extra else (disc.titles | rejectattr("category", "equalto", "Extra") | list) %}
{% if titles %}
<tbody>
<tr class="title-group"><th colspan="4" scope="rowgroup">{{ "Extras" if extra else "Features and episodes" }}</th></tr>
{% for t in titles %}
<tr>
<td><input type="checkbox" name="title_ids" value="{{ t.id }}" aria-label="Select title {{ t.id }}{% if t.name %}, {{ t.name }}{% endif %}" {% if job.running %}disabled{% endif %}></td>
<td>
<strong>{{ t.name or ("Title " ~ t.id) }}</strong>
{% if t.scan_video_hint %}<div class="small">{{ t.scan_video_hint }}</div>{% endif %}
{% set precheck = disc.get("compression_checks", {}).get(t.id | string) %}
{% if precheck %}<p class="small">{{ precheck }}</p>{% endif %}
{% if t.can_sample %}
<button type="submit" class="button" name="check_title" value="{{ t.id }}">Check compression</button>
{% elif not precheck and t.encode_readiness == "needs_extraction" %}
<div class="small">Dexter can check this title for compression after extraction.</div>
{% endif %}
{% if t.recommendation %}<div class="small">{{ t.recommendation | lower | capitalize }}</div>{% endif %}
<details class="title-details">
<summary>Details</summary>
<div class="small">Title {{ t.id }}{% if t.source %} · {{ t.source }}{% endif %}{% if t.reason %} · {{ t.reason }}{% endif %}</div>
{% if t.track_choices %}
<label>Sound for this title<select name="scan_audio_{{ t.id }}"><option value="">Use my language and surround preferences</option>
{% for track in t.track_choices if track.kind == "audio" %}<option value="{{ track.id }}">{{ track.label }}</option>{% endfor %}
</select></label>
<label>Subtitle track if enabled<select name="scan_subtitle_{{ t.id }}"><option value="">Use my subtitle language preference</option>
{% for track in t.track_choices if track.kind == "subtitle" %}<option value="{{ track.id }}">{{ track.label }}</option>{% endfor %}
</select></label>
{% endif %}

{% if t.segment_count %}<div class="small">{{ t.segment_count }} segments{% if t.segments %} · {{ t.segments }}{% endif %}</div>{% endif %}
</details>
</td>
<td>{{ t.duration }}</td>
<td>{{ t.size }}</td>
</tr>
{% endfor %}
</tbody>
{% endif %}
{% endfor %}
</table>
</div>

<div style="margin-top:14px">
<button
    type="submit"
    class="button-primary"
    {% if job.running %}disabled{% endif %}
>
Rip Selected
</button>

<span class="small" style="margin-left:8px">
Selected titles are ripped one at a time. Dexter checks available space before
starting{% if staging_storage %}; {{ staging_storage.free }} is free now{% endif %}.
</span>
</div>
<details class="file-actions">
<summary>Plan workflow from disc</summary>
<p class="small">Select one movie title above. This starts a new rip. To work with a file you already ripped, use its staged copy. If a requested step cannot be verified, the workflow pauses for review.</p>
{{ movie_workflow_fields(movie_title_suggestion) }}
<button type="submit" name="action" value="workflow" class="button-primary">Rip disc and start workflow</button>
</details>
</form>


</details>
{% elif disc.kind == "audio" %}

<h3>Audio CD</h3>

{% if disc.album %}
<p><strong>{{ disc.album }}</strong>{% if disc.album_artist %} by {{ disc.album_artist }}{% endif %}{% if disc.year %} ({{ disc.year }}){% endif %}</p>
{% endif %}

{% if disc.metadata_message %}
<p class="small">{{ disc.metadata_message }}</p>
{% endif %}

<div class="verified-actions" style="margin-bottom:14px">
<form method="post" action="/audio/metadata-lookup">
<button type="submit" {% if job.running %}disabled{% endif %}>
Try Music Lookup Again
</button>
</form>
<form
    method="post"
    action="/scan"
    {% if staging_has_media %}
    onsubmit="return confirm('Start a new scan and delete the temporary files from previous rips?')"
    {% endif %}
>
{% if staging_has_media %}<input type="hidden" name="clear_staging" value="yes">{% endif %}
<button type="submit" {% if job.running %}disabled{% endif %}>
Rescan CD
</button>
</form>
</div>

{% if disc.metadata_failures %}
<details class="system-details">
<summary>Lookup service details</summary>
<ul>
{% for failure in disc.metadata_failures %}<li>{{ failure }}</li>{% endfor %}
</ul>
</details>
{% endif %}

{% if job.audio_report %}
<div class="verified-panel"><strong>{{ job.audio_report.tracks }} tracks verified · {{ job.audio_report.format }}</strong><p>{{ format_size(job.audio_report.bytes) }} · File checks {{ format_duration(job.audio_report.validation_seconds) }}</p></div>
{% endif %}
{% if job.description.startswith('Audio CD') or job.description.startswith('Import Music') %}
<div class="queue-dashboard">
<h3>{{ job_heading }}</h3>
<p class="job-status">{{ job.status }}</p>

{% if cancellable %}
<form method="post" action="/cancel-job" style="margin:14px 0">
<button type="submit" class="button-cancel">{{ cancel_label }}</button>
</form>
{% endif %}

{% if job.music_import_failed %}
<p>Your verified FLAC tracks are still in temporary storage.</p>
<a class="button" href="/settings">Check music destination</a>
{% endif %}
{% if job.progress and not job.music_import_failed %}
<div class="rip-progress">
{{ progress_ring(job.progress.get("percent"), "Album progress" if job.running else "Complete", not job.running) }}
<div class="rip-progress-stats">
<div><strong>Tracks</strong><br>{{ job.progress.written or "—" }}</div>
<div><strong>Elapsed</strong><br>{{ job.progress.elapsed or "—" }}</div>
<div><strong>Now ripping</strong><br>{{ job.progress.filename or "Preparing disc…" }}</div>
<div><strong>ETA</strong><br>{{ job.progress.eta or ("Calculating…" if job.running else "Complete") }}</div>
</div>
</div>
{% endif %}

{% if job.log %}
<details class="verbose-log">
<summary>Audio rip log ({{ job.log | length }} lines)</summary>
<pre>{{ job.log | join('\n') }}</pre>
</details>
{% endif %}
</div>
{% endif %}

<div class="table-wrap">
<table>
<thead>
<tr>
<th>Track</th>
<th>Title</th>
<th>Duration</th>
<th>Progress</th>
</tr>
</thead>
<tbody>
{% for track in disc.tracks %}
<tr>
<td>{{ track.number }}</td>
<td>{{ track.title }}</td>
<td>{{ track.duration }}</td>
<td class="track-progress">
{% set current_track = (job.progress or {}).get('current_track', 0) %}
{% if not job.running and (job.status == 'Completed' or job.get('audio_imported_path') or (job.audio_report and track.number in disc.selected_tracks)) %}
<span class="queue-status queue-status-completed">Complete</span>
<div class="track-progress-bar"><div class="track-progress-fill" style="width:100%"></div></div>
{% elif track.number < current_track %}
<span class="queue-status queue-status-completed">Complete</span>
<div class="track-progress-bar"><div class="track-progress-fill" style="width:100%"></div></div>
{% elif track.number == current_track %}
<span class="queue-status queue-status-ripping">
{% if job.running %}{% if phase == 'Importing' %}Importing{% else %}Ripping{% endif %} {{ "%.1f"|format((job.progress or {}).get('track_percent', 0)) }}%{% else %}Interrupted{% endif %}
</span>
<div class="track-progress-bar"><div class="track-progress-fill" style="width:{{ (job.progress or {}).get('track_percent', 0) }}%"></div></div>
{% else %}
<span class="queue-status">Waiting</span>
<div class="track-progress-bar"><div class="track-progress-fill" style="width:0%"></div></div>
{% endif %}
</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>

<p>
{% if cd_offset %}
Configured drive offset: {{ cd_offset }} samples
{% else %}
Drive offset: 0 samples (default)
{% endif %}
<span class="help" tabindex="0" data-tip="The sample offset corrects the precise read position of an Audio CD drive for accurate track boundaries.">?</span>
</p>

<form method="post" action="/rip/audio">
<input type="hidden" name="scan_id" value="{{ disc.scan_id }}">
{% for track in disc.tracks %}
<label style="display:block"><input type="checkbox" name="track" value="{{ track.number }}"
{% if track.number in disc.selected_tracks %}checked{% endif %}
{% if job.running %}disabled{% endif %}>{{ track.number }}. {{ track.title }}</label>
{% endfor %}
<button
    type="submit"
    {% if job.running %}disabled{% endif %}
>
Rip CD to FLAC
</button>
</form>

{% if audio_ready and not job.get('audio_imported_path') %}
<form method="post" action="/import/music" style="margin-top:10px">
<input type="hidden" name="scan_id" value="{{ disc.scan_id }}">
<button type="submit" class="button-primary" {% if job.running %}disabled{% endif %}>
{{ "Retry music import" if job.music_import_failed else "Import Album to Jellyfin" }}
</button>
</form>
<p class="small">The completed FLAC tracks will be verified, tagged, and copied to Jellyfin. Staged originals are retained.</p>
{% elif job.get('audio_imported_path') %}
<div class="verified-panel">
<div class="verified-heading">✓ ALBUM IMPORTED</div>
{{ job.audio_imported_path }}
</div>
{% endif %}

<details class="system-details">
<summary>Disc scan details</summary>
<pre>{{ disc.raw }}</pre>
</details>


{% else %}

{% if disc.diagnosis %}
<div class="verification-failed">
<strong>⚠ {{ disc.type }}</strong><br>
{{ disc.diagnosis }}
{% if disc.resolution %}
<p>{{ disc.resolution }}</p>
{% endif %}
</div>
{% endif %}

<details class="system-details">
<summary>Disc scan details</summary>
<pre>{{ disc.raw }}</pre>
</details>

{% endif %}

{% if collapse_titles %}</details>{% endif %}
</div>
{% endif %}


</div>
</main>
<footer class="app-footer"><a href="/credits">Credits &amp; acknowledgments</a></footer>

<script>
function updateMovieDestination(select) {
    const label = select.value === 'staging' ? 'Keep in staging' : 'Jellyfin Movies';
    select.closest('details').querySelector('summary').textContent = 'Destination · ' + label;
    select.form.querySelector('.verified-actions button').textContent =
        select.value === 'staging' ? 'Keep in staging' : 'Import to Jellyfin';
}

function updateMediaFields() {
    const selector = document.getElementById('media-type');
    if (!selector) return;
    const isMovie = selector.value === 'movie';
    document.querySelectorAll('.movie-fields').forEach(
        element => element.hidden = !isMovie
    );
    document.querySelectorAll('.tv-fields').forEach(
        element => element.hidden = isMovie
    );
}

const pageStateKey = `dexter-page-state:${location.pathname}${location.search}`;

function detailKey(element, index) {
    const summary = element.querySelector(':scope > summary');
    return `${index}:${summary ? summary.textContent.trim() : ''}`;
}

function savePageState() {
    try {
        const openDetails = [];
        document.querySelectorAll('details').forEach((element, index) => {
            if (element.open) openDetails.push(detailKey(element, index));
        });
        sessionStorage.setItem(pageStateKey, JSON.stringify({
            openDetails,
            scrollY: window.scrollY,
        }));
    } catch (error) {}
}

function restorePageState() {
    try {
        const saved = JSON.parse(sessionStorage.getItem(pageStateKey));
        if (!saved) return;
        const openDetails = new Set(saved.openDetails || []);
        document.querySelectorAll('details').forEach((element, index) => {
            element.open = openDetails.has(detailKey(element, index));
        });
        if (Number.isFinite(saved.scrollY)) {
            window.scrollTo(0, saved.scrollY);
        }
    } catch (error) {}
}

document.addEventListener('DOMContentLoaded', () => {
    updateMediaFields();
    restorePageState();
});
window.addEventListener('pagehide', savePageState);
</script>

</body>
</html>
"""


@app.route('/storage/prepare', methods=['GET', 'POST'])
def prepare_storage_page():
    try:
        document = storage_setup.load(CONFIG_ROOT / 'install/storage-inventory.json')
        device = request.values.get('device')
        disk = next((d for d in (document or {}).get('unready', []) if d['device'] == device), None)
        if not disk or not disk.get('serial') or not disk.get('by_id'):
            return 'Refresh the host disk list before preparing this disk.', 400
        command = None
        error = None
        if request.method == 'POST':
            if request.form.get('token') != STORAGE_FORM_TOKEN:
                return 'Reload this form before continuing.', 400
            if request.form.get('confirmation', '').strip() != disk['serial']:
                error = 'Enter the exact serial number to acknowledge which disk will be erased.'
            else:
                command = shlex.join(['sudo', 'python3', 'tools/prepare-storage.py', '--device', disk['by_id'],
                    '--serial', disk['serial'], '--size-bytes', str(disk['size_bytes']),
                    '--mountpoint', '/mnt/dexter-' + Path(device).name, '--erase'])
        return render_template_string('''<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Prepare storage — Dexter</title></head>
<body style="background:#181818;color:#eee;font:18px/1.6 system-ui;max-width:800px;margin:40px auto;padding:20px">
<a style="color:#9bd0ff" href="/storage">← Storage choices</a><h1>Erase and use this disk</h1>
<p><strong>{{ disk.name }} · {{ disk.size_gib }} GiB</strong><br>Device: {{ disk.device }}<br>Serial: <code>{{ disk.serial }}</code></p>
<p>All existing partitions and files on this disk will be erased. One Linux filesystem will use its full usable capacity. Filesystem overhead means the displayed free space will be smaller than the advertised disk size.</p>
<p>Mounted disks, active swap and storage mappings are refused. This prepares a disk for reuse; it does not securely sanitize old data.</p>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
{% if command %}<h2>Administrator step</h2><p>Nothing has been erased by this page. On the Dexter host, from its project folder, run the following command to authorize and perform the erase. It checks the disk identity again before writing.</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{ command }}</pre>
<p>Then refresh the disk list with <code>sh install.sh --refresh-storage</code> and return to Storage choices. Applying choices to the app is a separate step.</p>
{% else %}<form method="post"><input type="hidden" name="device" value="{{ disk.device }}"><input type="hidden" name="token" value="{{ token }}">
<label for="confirmation">Type the disk's serial number to acknowledge erasing it</label><br><input id="confirmation" name="confirmation" required autocomplete="off" style="font:inherit;padding:10px;width:90%"><p><button style="font:inherit;padding:12px" type="submit">Show preparation command</button></p></form>{% endif %}
<p>This development version requires administrator access on the host. Disk preparation never runs automatically when a disk is connected.</p></body></html>''', disk=disk, command=command, error=error, token=STORAGE_FORM_TOKEN)
    except (OSError, ValueError, KeyError, TypeError):
        return 'Storage information could not be read. Refresh the host disk list.', 400


@app.route("/storage", methods=["GET", "POST"])
def storage_page():
    folder = CONFIG_ROOT / 'install'
    error = message = None
    document = pending = None
    try:
        document = storage_setup.load(folder / 'storage-inventory.json')
        pending = storage_setup.load(folder / 'storage-request.json')
        if request.method == 'POST':
            if request.form.get('token') != STORAGE_FORM_TOKEN:
                return 'This storage form has expired. Reload the page and try again.', 400
            with lock:
                if state['job'].get('running'):
                    raise ValueError('Finish the current job before changing storage.')
                if request.form.get('action') == 'cancel':
                    (folder / 'storage-request.json').unlink(missing_ok=True)
                    pending = None
                    message = 'Current storage kept. No files were moved.'
                elif document:
                    paths = storage_setup.selected(document, request.form.get('staging'), request.form.get('library'))
                    current = next((c['path'] for c in document['choices'] if c['role'] == 'staging' and c['current']), None)
                    if paths['staging'] != current and any(p.is_file() or p.is_symlink() for p in OUTPUT.rglob('*')):
                        raise ValueError('Temporary storage still contains files. Finish or recover them before choosing another folder.')
                    current_library = next((c['path'] for c in document['choices'] if c['role'] == 'library' and c['current']), None)
                    if paths == {'staging': current, 'library': current_library}:
                        (folder / 'storage-request.json').unlink(missing_ok=True)
                        pending = None
                        message = 'These are your current folders. No change is needed.'
                    else:
                        pending = {'schema': 1, **paths, 'staging_id': request.form['staging'],
                                   'library_id': request.form['library'], 'request_id': uuid.uuid4().hex}
                        storage_setup.atomic_json(folder / 'storage-request.json', pending)
                        message = 'Storage change requested. Your current folders remain in use until the change is applied.'
                else:
                    raise ValueError('Refresh host storage information before choosing a disk.')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never include raw file errors or untrusted host configuration in HTML.
        error = 'Storage information could not be used. Ask an administrator to refresh it from the host installer.'
        # Validation errors below are safe, plain-language messages.
        if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
            error = str(exc)
    selected_ids = {}
    for role in ('staging', 'library'):
        selected_ids[role] = pending.get(role + '_id') if pending else next((
            c['id'] for c in (document or {}).get('choices', []) if c['role'] == role and c['current']), None)
    return render_template_string(storage_setup.PAGE, inventory=document, pending=pending,
        token=STORAGE_FORM_TOKEN, error=error, message=message, selected_ids=selected_ids,
        checked=time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(document['checked_at'])) if document else '')


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    first_run = not settings.get("setup_complete")
    drives = detected_optical_drives()
    values = {
        "drive_device": configured_drive(),
        "cd_offset": configured_cd_offset(),
        "jellyfin_url": configured_jellyfin_url(),
    }
    for key, choices, fallback in (("movie_quality", QUALITY_DEFAULTS, "original"),
                                    ("movie_destination", MOVIE_DESTINATIONS, "jellyfin")):
        saved = settings.get(key)
        values[key] = saved if isinstance(saved, str) and saved in choices else fallback
    error = None
    connection = None

    if request.method == "POST":
        with lock:
            job_running = bool(state["job"].get("running"))

        if job_running:
            error = "Settings cannot be changed while a job is running."
        else:
            values = {
                **values,
                "drive_device": request.form.get(
                    "drive_device", ""
                ).strip(),
                "cd_offset": request.form.get("cd_offset", "").strip(),
                "jellyfin_url": request.form.get(
                    "jellyfin_url", ""
                ).strip().rstrip("/"),
            }
            for key in ("movie_quality", "movie_destination"):
                values[key] = request.form.get(key, values[key])
            api_key = request.form.get("jellyfin_api_key", "").strip()
            selected_drive = next(
                (
                    drive for drive in drives
                    if drive["device"] == values["drive_device"]
                ),
                None,
            )

            # No optical hardware is required to configure the application.
            # Keep the configured device as a future preference, never accept
            # arbitrary unavailable device paths from a submitted form.
            if not drives and values["drive_device"] in ("", configured_drive()):
                values["drive_device"] = configured_drive()
                selected_drive = {"device": values["drive_device"], "name": "No optical drive connected"}

            if request.form.get("action") == "test-jellyfin":
                connection = library_connection.inspect(values["jellyfin_url"], api_key or (
                    JELLYFIN_KEY_FILE.read_text().strip() if JELLYFIN_KEY_FILE.exists() else ""))
            elif values["movie_quality"] not in QUALITY_DEFAULTS or values["movie_destination"] not in MOVIE_DESTINATIONS:
                error = "Choose a supported quality and destination."
            elif selected_drive is None:
                error = "Select an available optical drive."
            elif values["cd_offset"] and not re.fullmatch(
                r"[+-]?\d+", values["cd_offset"]
            ):
                error = "Audio CD sample offset must be a whole number."
            elif values["jellyfin_url"] and not re.match(
                r"^https?://[^\s/]+(?::\d+)?(?:/.*)?$",
                values["jellyfin_url"],
                re.IGNORECASE,
            ):
                error = "Jellyfin URL must begin with http:// or https://."

            if error is None and connection is None:
                updated = {
                    "setup_complete": True,
                    "drive_name": selected_drive["name"],
                    **values,
                }

                try:
                    save_settings(updated)

                    if api_key:
                        key_tmp = JELLYFIN_KEY_FILE.with_name(
                            ".jellyfin-api-key.tmp"
                        )
                        key_tmp.write_text(api_key + "\n", encoding="utf-8")
                        os.chmod(key_tmp, 0o600)
                        os.replace(key_tmp, JELLYFIN_KEY_FILE)

                    settings.clear()
                    settings.update(updated)
                    return redirect(url_for("home"))

                except Exception as exc:
                    error = f"Settings could not be saved: {exc}"

    return render_template_string(
        SETTINGS_PAGE,
        error=error,
        first_run=first_run,
        has_api_key=(
            JELLYFIN_KEY_FILE.exists()
            and bool(JELLYFIN_KEY_FILE.read_text().strip())
        ),
        drives=drives,
        values=values,
        connection=connection,
        storage={"movie_share": os.environ.get("DEXTER_JELLYFIN_HOST") if os.environ.get("DEXTER_JELLYFIN_EXPORT") else None,
                 "staging": os.environ.get("RIPPER_STAGING_HOST", str(OUTPUT)),
                 "library": os.environ.get("RIPPER_LIBRARY_HOST", str(MEDIA_ROOT)),
                 "free": round(shutil.disk_usage(OUTPUT if OUTPUT.exists() else OUTPUT.parent).free / 1024**3, 1),
                 "total": round(shutil.disk_usage(OUTPUT if OUTPUT.exists() else OUTPUT.parent).total / 1024**3, 1)},
        quality_choices=QUALITY_DEFAULTS,
        format_size=format_size,
        format_duration=format_duration,
        destination_choices=MOVIE_DESTINATIONS,
        nvenc_results=encoding.nvenc_probe(),
        nvenc_enabled=os.environ.get("DEXTER_NVENC") == "1",
        readiness_checks=readiness.report(bool(drives), OUTPUT, MAKEMKV),
    )


@app.route("/")
def home():
    if not settings.get("setup_complete"):
        return redirect(url_for("settings_page"))

    with lock:
        disc = copy.deepcopy(state.get("disc"))
        job = copy.deepcopy(state.get("job") or {})
        queue_snapshot = copy.deepcopy(state.get("workflow_queue", []))
        submitted_batch = (state.get("wizard_draft") or {}).get("submitted")
        pending = next((copy.deepcopy(item) for item in state.get("workflow_queue", [])
                        if item.get("status") in ("saving", "queued", "starting", "running")), None)

    batch_overview = workflow_presentation.batch(queue_snapshot, job, submitted_batch)
    if pending and not job.get("running"):
        # Display-only handoff state: accepting a batch and starting its worker
        # are separate events. Never redirect back to the submitted Review page.
        job = {"running": True, "description": "Preparing your workflow",
               "status": "Your workflow was accepted. Dexter is preparing the next title…",
               "log": [], "outputs": [], "progress": None, "recovery": None,
               "cancel_requested": False}

    if ((job.get("description") or "").startswith("Import batch") and
            (job.get("status") or "").startswith("Imported ") and os.environ.get("DEXTER_JELLYFIN_EXPORT")):
        job["status"] = "The earlier batch used the local library path. Its destination on Jellyfin storage was not verified. Local files are retained for recovery."
        job["import_failed"] = True

    # Older saved failures get the same recovery view without changing the job.
    if not job.get("running") and (job.get("status") or "").startswith("Music import failed:"):
        job["music_import_failed"] = True
        job["log"] = (job.get("log", []) + [job["status"]])[-80:]
        job["status"] = "Your music could not be added to the library. Check the music destination, then retry the import. Your staged tracks are kept; no need to rip again."

    if job.get("running") and (
        job.get("description") or ""
    ).lower().startswith("audio cd"):
        mode = "music"
    elif (disc or {}).get("kind") == "audio":
        mode = "music"
    elif (disc or {}).get("kind") == "video":
        mode = "movie"
    elif disc:
        mode = "movie"
    elif _job_outputs(job):
        mode = "movie"
    else:
        mode = "neutral"

    phase = ui_phase(disc, job)
    job_tone = job_visual_tone(job)
    job_heading = job_display_heading(job)
    cancellable = job_is_cancellable(job)
    cancel_label = job_cancel_label(job)

    movie_title_suggestion = suggested_movie_title(disc)
    staged_outputs = _job_outputs(job)
    audio_ready = bool(
        disc
        and disc.get("kind") == "audio"
        and staged_audio_files(disc)
    )
    storage = staging_storage()
    all_outputs_verified = bool(staged_outputs) and all(
        (output.get("verification") or {}).get("ok")
        for output in staged_outputs
    )

    runtime = encoding.cached_probe(os.environ.get("HANDBRAKE_CLI", "HandBrakeCLI")) if staged_outputs else {}
    for output in staged_outputs:
        verification = output.get("verification") or {}
        if verification.get("ok") and "tracks" not in verification and _allowed_staged_video(output.get("path", "")):
            try:
                refreshed = probe_media(output["path"])
                if refreshed.get("ok"):
                    verification.update(refreshed)
                    output["verification"] = verification
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        output["compression"] = product.compression_choices(verification, runtime)
        filename = verification.get("filename") or Path(
            output.get("path") or ""
        ).name
        output["suggestion"] = (
            humanize_disc_name(re.sub(r"_t\d+(?:(?:-encoded|-playback)-[0-9a-f]{8})?(?:_[0-9a-f]{8})?$", "", Path(filename).stem))
        )

    diagnostics = collect_drive_diagnostics(
        (disc or {}).get("raw", ""),
        disc or {}
    )

    if disc and disc.get("kind") == "video":
        hints = scan_video_hints(disc.get("raw") or "")
        inventories = movie_output.scan_tracks(disc.get("raw") or "")
        for title in disc.get("titles", []):
            title["scan_video_hint"] = hints.get(title.get("id"), "")
            title["track_choices"] = inventories.get(title.get("id"), [])
            title["encode_readiness"] = encoding.EncodeReadiness.NEEDS_EXTRACTION.value
            title["can_sample"] = disc_compression.playlist_number(title.get("source")) is not None

    human_disc_title = ""

    if disc:
        human_disc_title = humanize_disc_name(
            disc.get("title") or disc.get("label") or ""
        )

    if disc and disc.get("kind") in ("video", "audio") and not job.get("running") and not staged_outputs and not audio_ready and not job.get("audio_imported_path") and not job.get("queue_id") and request.args.get("advanced") != "1":
        return redirect(url_for("movie_wizard"))
    return render_template_string(
        PAGE,
        batch_overview=batch_overview,
        disc=disc,
        job=job,
        drive=configured_drive(),
        drive_name=configured_drive_name(),
        cd_offset=configured_cd_offset(),
        phase=phase,
        job_tone=job_tone,
        job_heading=job_heading,
        job_operation_kind=job_operation_kind,
        cancellable=cancellable,
        cancel_label=cancel_label,
        default_quality=settings.get("movie_quality", "original"),
        workflow_default_quality=settings.get("movie_quality", "balanced"),
        quality_choices=QUALITY_DEFAULTS,
        format_size=format_size,
        format_duration=format_duration,
        workflow_import_available=bool(os.environ.get("DEXTER_JELLYFIN_EXPORT")),
        default_destination=settings.get("movie_destination", "jellyfin"),
        movie_destinations=MOVIE_DESTINATIONS,
        jellyfin_url=configured_jellyfin_url(),
        movie_title_suggestion=movie_title_suggestion,
        staged_outputs=staged_outputs,
        all_outputs_verified=all_outputs_verified,
        metadata_results=job.get("metadata_results") or [],
        metadata_draft=job.get("metadata_draft") or {},
        human_disc_title=human_disc_title,
        diagnostics=diagnostics,
        mode=mode,
        audio_ready=audio_ready,
        staging_has_media=staging_has_media(),
        staging_storage=storage,
        video_staging=str(VIDEO_OUTPUT),
        audio_staging=str(AUDIO_OUTPUT),
        recovery_staging=str(OUTPUT / "recovery"),
    )
@app.post("/scan")
def scan():
    with lock:
        if state["job"].get("running") or any(e["status"] in ("saving", "queued", "starting", "running") for e in state.get("workflow_queue", [])):
            return redirect(url_for("home"))
        if not shutil.which(MAKEMKV) or not shutil.which("cyanrip"):
            state["job"]["status"] = "Disc scanning needs setup. Run Dexter setup again; staged files were retained."
            return redirect(url_for("home"))
        if staging_has_media() and request.form.get("clear_staging") != "yes":
            state["job"]["status"] = "Starting a new scan clears staged files. Review the confirmation and try again."
            return redirect(url_for("home"))
        state["job"].update(running=True, description="Disc scan", status="Preparing for a new scan…")

    persist_state_now(force=True)
    try:
        if request.form.get("clear_staging") == "yes":
            clear_staged_media()
    except OSError:
        with lock:
            state["job"].update(running=False, status="Could not clear temporary files. Check staging storage and try again.")
            _set_job_outputs(state["job"], [
                output for output in _job_outputs(state["job"])
                if Path(output.get("path") or "").is_file()
            ])
        persist_state_now(force=True)
        return redirect(url_for("home"))

    with lock:
        state["job"] = {
            "running": True, "description": "Disc scan", "status": "Reading your disc…",
            "log": [], "outputs": [], "title_queue": [],
            "pid": None, "cancel_requested": False, "progress": None, "recovery": None,
            "output_file": None, "verification": None,
        }
        state["disc"] = None
        state.pop("wizard_draft", None)
    persist_state_now(force=True)

    def scan_worker():
        try:
            disc = scan_disc()
            if disc and disc.get("kind") == "audio":
                disc["audio_job_dir"] = ""

            with lock:
                if disc:
                    disc["scanned_at"] = time.time()
                state["scan_requires_refresh"] = not bool(disc)
                state["disc"] = disc
                state["job"]["running"] = False

                if not disc:
                    state["job"]["status"] = "No disc detected"

                elif disc.get("kind") == "unknown":
                    state["job"]["status"] = disc.get(
                        "type", "Disc could not be opened"
                    )

                elif disc.get("kind") == "audio":
                    state["job"]["status"] = "Ready — Audio CD detected"

                else:
                    count = len(disc.get("titles", []))
                    state["job"]["status"] = (
                        f"Ready — {count} title"
                        f"{'s' if count != 1 else ''} found"
                    )

        except Exception as exc:
            with lock:
                state["job"]["running"] = False
                state["job"]["status"] = f"Scan failed: {exc}"

    threading.Thread(
        target=scan_worker,
        daemon=True
    ).start()

    return redirect(url_for("movie_wizard" if request.form.get("return_to") == "wizard" else "home"))


@app.post("/rip/video")
def rip_selected_video():
    if "check_title" in request.form:
        try:
            title_id = int(request.form["check_title"])
        except ValueError:
            return "Choose a valid title to check.", 400
        return check_disc_compression([title_id])

    submitted = request.form.getlist("title_ids")

    try:
        title_ids = list(dict.fromkeys(int(value) for value in submitted))
    except (TypeError, ValueError):
        title_ids = []

    if request.form.get("action") == "check-compression":
        return check_disc_compression(title_ids)
    if request.form.get("action") == "workflow":
        plan, error = read_movie_workflow()
        if len(title_ids) != 1:
            error = "Select exactly one movie title to start the whole workflow."
        if error:
            with lock:
                if not state["job"].get("running"):
                    state["job"]["status"] = error
            return redirect(url_for("home"))
        return start_video_rip(title_ids, movie_workflow=plan)
    return start_video_rip(title_ids)


def check_disc_compression(title_ids):
    with lock:
        job = state["job"]
        disc = state.get("disc") or {}
        if job.get("running"):
            return redirect(url_for("home"))
        title = next((t for t in disc.get("titles", []) if len(title_ids) == 1 and t.get("id") == title_ids[0]), None)
        if not title:
            job["status"] = "Select one disc title to check compression."
            return redirect(url_for("home"))
        playlist = disc_compression.playlist_number(title.get("source"))
        if playlist is None:
            disc.setdefault("compression_checks", {})[str(title["id"])] = "A sample check is available for Blu-ray playlists. Dexter can inspect this title after ripping."
            return redirect(url_for("home"))
        drive = configured_drive()
        job.update(running=True, description="Check compression", status="Checking a short sample from the disc…",
                   log=[], progress=None, title_queue=[], movie_import=None, import_failed=False,
                   import_retry=None, video_retry_token=None, recovery={"kind": "command"})

    def worker():
        try:
            message, log = disc_compression.check(playlist, drive, CONFIG_ROOT)
        except (OSError, RuntimeError, ValueError) as exc:
            message, log = "Couldn’t check this disc. You can still rip the original.", str(exc)
        with lock:
            disc.setdefault("compression_checks", {})[str(title["id"])] = message
            job.update(running=False, status=message, log=log.splitlines()[-80:], recovery=None)
        persist_state_now(force=True)

    threading.Thread(target=worker, daemon=True, name="dexter-disc-sample").start()
    return redirect(url_for("home"))


def read_movie_workflow(form=None):
    form = request.form if form is None else form
    profile = form.get("workflow_quality", "original")
    codec = form.get("workflow_codec", "h264")
    engine = form.get("workflow_engine", "cpu")
    destination = form.get("workflow_destination", "staging")
    title = safe_media_name(form.get("workflow_title", ""))
    year = form.get("workflow_year", "").strip()
    if profile not in QUALITY_DEFAULTS or codec not in encoding.ENCODERS or destination not in MOVIE_DESTINATIONS:
        return None, "Choose a supported quality and destination."
    if engine not in ("cpu", "gpu", "auto"):
        return None, "Choose CPU, NVIDIA or Automatic encoding."
    if destination == "jellyfin" and (not os.environ.get("DEXTER_JELLYFIN_EXPORT") or not title):
        return None, "Automatic import needs a movie title and configured Jellyfin storage."
    if year and not re.fullmatch(r"\d{4}", year):
        return None, "Enter a four-digit release year."
    plan = {"profile": profile, "codec": codec, "engine": engine, "destination": destination, "title": title, "year": year}
    container = form.get("workflow_container", "preserve")
    if container not in ("mp4", "mkv", "preserve"):
        return None, "Choose a supported playback format."
    if container != "preserve":
        language = form.get("workflow_language", "eng")
        subtitle_language = form.get("workflow_subtitle_language", language)
        if not re.fullmatch(r"[a-z]{3}", language) or not re.fullmatch(r"[a-z]{3}", subtitle_language):
            return None, "Choose an audio and subtitle language."
        subtitles = form.get("workflow_subtitles", "no")
        if subtitles not in ("yes", "no"):
            return None, "Choose Yes or No for subtitles."
        if container == "mp4" and subtitles == "yes":
            return None, "Choose MKV to keep the selected subtitles."
        plan["output_policy"] = {
            "container": container, "language": language,
            "surround": form.get("workflow_surround") == "yes",
            "subtitles": subtitles == "yes", "subtitle_language": subtitle_language,
            "audio_track": form.get("workflow_audio_track", ""),
            "subtitle_track": form.get("workflow_subtitle_track", ""),
        }
    if plan.get("output_policy"):
        title_ids = form.getlist("title_ids")
        if len(title_ids) == 1:
            inventories = movie_output.scan_tracks((state.get("disc") or {}).get("raw", ""))
            choices = inventories.get(int(title_ids[0]), []) if title_ids[0].isdigit() else []
            for kind in ("audio", "subtitle"):
                choice = form.get("scan_" + kind + "_" + title_ids[0], "")
                if not choice:
                    continue
                selected = next((item for item in choices if item["id"] == choice and item["kind"] == kind), None)
                if selected is None:
                    return None, "The disc track selection changed. Scan again and review your choices."
                plan["output_policy"]["scan_" + kind] = selected["selector"]
    return plan, None


@app.post("/workflow/staged-movie")
def workflow_from_staged_movie():
    source = request.form.get("output_file", "")
    plan, error = read_movie_workflow()
    with lock:
        job = state["job"]
        if job.get("running"):
            return redirect(url_for("home"))
        item = next((item for item in _job_outputs(job) if item.get("path") == source), {})
        if error or not (item.get("verification") or {}).get("ok") or not _allowed_staged_video(source):
            job["status"] = error or "Choose a verified staged file to start the workflow."
            return redirect(url_for("home"))
        job.update(movie_workflow=plan, movie_import=None, import_retry=None, import_failed=False,
                   cancel_requested=False, video_retry_token=None, progress=None,
                   status="Your verified copy is ready in staging.")
    persist_state_now(force=True)
    continue_movie_workflow(job, source, plan, after_encode=False)
    return redirect(url_for("home"))


@app.post("/rip/video/retry")
def retry_video_rip():
    token = request.form.get("retry_token")
    with lock:
        job = state["job"]
        if (not token or job.get("video_retry_token") != token
                or job_visual_tone(job) != "error"
                or (state.get("disc") or {}).get("kind") != "video"):
            return "This rip is no longer available to retry. Return to Dexter and check the current disc.", 409
        selected = [item["id"] for item in job.get("title_queue", [])]
        workflow = copy.deepcopy(job.get("movie_workflow"))
    if workflow:
        return start_video_rip(selected, retry_token=token, movie_workflow=workflow)
    return start_video_rip(selected, retry_token=token)


def video_transfer_identity(episode=None):
    if not episode:
        return {"user": int(os.environ["DEXTER_JELLYFIN_UID"]), "group": int(os.environ["DEXTER_JELLYFIN_GID"]), "extra_groups": []}
    root = Path("/jellyfin-import/tv")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The Jellyfin share has no TV folder. Check its library mapping.")
    owner = root.stat()
    if owner.st_uid in (0, 65534) or owner.st_gid in (0, 65534):
        raise ValueError("The TV share needs a dedicated writer identity.")
    return {"user": owner.st_uid, "group": owner.st_gid, "extra_groups": []}


def movie_destination_preflight(source, workflow, expected_bytes=None):
    helper = Path(__file__).with_name("jellyfin_transfer.py")
    command = [sys.executable, str(helper), str(source), workflow["title"], workflow["year"], "--check"]
    if expected_bytes is not None:
        command.append(str(expected_bytes))
    if workflow.get("episode"):
        command += ["--episode", json.dumps(workflow["episode"])]
    result = subprocess.run(command, **video_transfer_identity(workflow.get("episode")),
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Jellyfin storage is not ready. " + result.stdout.strip()[-500:])


def start_video_rip(title_ids, retry_token=None, movie_workflow=None):
    with lock:
        if state["job"].get("running") or (retry_token and state["job"].get("video_retry_token") != retry_token):
            return redirect(url_for("home"))
        disc = copy.deepcopy(state.get("disc") or {})

    valid_ids = {
        title.get("id")
        for title in disc.get("titles", [])
        if isinstance(title.get("id"), int)
    }
    title_ids = [title_id for title_id in title_ids if title_id in valid_ids]

    if not title_ids:
        with lock:
            state["job"]["status"] = "Select at least one title to rip"
        return redirect(url_for("home"))

    expected_bytes = sum(
        expected_size_for_title(title_id) or 0
        for title_id in title_ids
    ) or None

    try:
        makemkv_source = disc.get("makemkv_source") or (
            configured_makemkv_source()
        )
    except RuntimeError as exc:
        with lock:
            state["job"]["status"] = f"Rip could not start: {exc}"
        return redirect(url_for("home"))

    titles_by_id = {
        title.get("id"): title
        for title in disc.get("titles", [])
    }
    queue_items = [
        {
            "id": title_id,
            "name": titles_by_id[title_id].get("name") or f"Title {title_id}",
            "duration": titles_by_id[title_id].get("duration") or "—",
            "size": titles_by_id[title_id].get("size") or "—",
            "status": "queued",
        }
        for title_id in title_ids
    ]
    description = (
        f"MakeMKV title {title_ids[0]}"
        if len(title_ids) == 1
        else f"MakeMKV titles {', '.join(map(str, title_ids))}"
    )

    space_error = video_staging_space_error(
        expected_bytes,
        VIDEO_OUTPUT,
    )

    if space_error:
        with lock:
            state["job"]["video_retry_token"] = uuid.uuid4().hex
            state["job"]["movie_workflow"] = movie_workflow
            state["job"].pop("import_retry", None)
            state["job"]["description"] = description
            state["job"]["status"] = space_error
            state["job"]["log"] = []
            state["job"]["title_queue"] = [
                {**item, "status": "failed"}
                for item in queue_items
            ]
            state["job"]["progress"] = None

        persist_state_now(force=True)
        return redirect(url_for("home"))

    recovery_root = OUTPUT / "recovery"
    recovery_root.mkdir(exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="video-rip-", dir=recovery_root))
    commands = [
        [
            MAKEMKV,
            "-r",
            "mkv",
            makemkv_source,
            str(title_id),
            str(work_dir),
        ]
        for title_id in title_ids
    ]
    run_options = {}
    if movie_workflow and movie_workflow.get("output_policy") and movie_workflow.get("destination") == "jellyfin":
        run_options["preflight"] = lambda: movie_destination_preflight(
            VIDEO_OUTPUT / ".planned-movie.mp4", movie_workflow, expected_bytes or 0)
    started = run_job(
        description,
        commands,
        **run_options,
        verify_dir=work_dir,
        publish_dir=VIDEO_OUTPUT,
        expected_bytes=expected_bytes,
        queue_items=queue_items,
        retry_token=retry_token,
        movie_workflow=movie_workflow,
    )
    if not started:
        work_dir.rmdir()
    return redirect(url_for("home"))


def selected_audio_tracks(disc, submitted):
    try:
        selected = sorted(set(int(value) for value in submitted))
    except (ValueError, TypeError):
        raise ValueError("Invalid track selection")
    valid = {track["number"] for track in disc.get("tracks", [])}
    if not selected or not set(selected) <= valid:
        raise ValueError("Select at least one valid track")
    return selected


@app.post("/rip/audio")
def rip_audio(audio_workflow=None):
    with lock:
        disc = copy.deepcopy(state.get("disc") or {})
        if state["job"].get("running") or disc.get("kind") != "audio" or request.form.get("scan_id") != disc.get("scan_id"):
            return "The disc selection is stale or another job is running. Scan again.", 409
        try:
            selected = selected_audio_tracks(disc, request.form.getlist("track"))
        except ValueError as exc:
            return str(exc), 400
    cd_offset = configured_cd_offset() or "0"
    job_dir = AUDIO_OUTPUT / f"cd-{uuid.uuid4().hex}"

    def preflight():
        rc, raw = run_command(["cyanrip", "-d", configured_drive(), "-s", cd_offset, "-N", "-I"])
        current = parse_cyanrip(raw) if rc == 0 else None
        if not current or not disc.get("discid") or current.get("discid") != disc["discid"]:
            raise ValueError("Inserted CD does not match the saved scan. Scan again.")
        job_dir.mkdir()
        with lock:
            state["disc"]["selected_tracks"] = selected
            state["disc"]["audio_job_dir"] = job_dir.name
        persist_state_now(force=True)

    started = run_job(
        "Audio CD → FLAC",
        [
            "cyanrip",
            "-d", configured_drive(),
            "-s", cd_offset,
            "-N",
            "-U",
            "-G",
            "-o", "flac",
            "-l", ",".join(map(str, selected)),
        ],
        cwd=str(job_dir),
        preflight=preflight,
        audio_workflow=audio_workflow,
    )
    if not started:
        return "Another job started. Try again after it finishes.", 409
    return redirect(url_for("home"))


@app.post("/audio/metadata-lookup")
def retry_audio_metadata():
    with lock:
        if state["job"].get("running"):
            return redirect(url_for("home"))

        disc = copy.deepcopy(state.get("disc") or {})

    if disc.get("kind") != "audio":
        with lock:
            state["job"]["status"] = "Insert and scan an Audio CD first"
        return redirect(url_for("home"))

    matched = lookup_audio_metadata(disc)

    with lock:
        current_disc = state.get("disc") or {}

        if current_disc.get("scan_id") == disc.get("scan_id"):
            state["disc"] = disc
            state["job"]["status"] = (
                "Ready — music information updated"
                if matched
                else "Ready — no music information match found"
            )

    persist_state_now(force=True)
    return redirect(url_for("home"))


def music_share_command(source, artist, album, expected_bytes=None):
    root = Path("/jellyfin-import/music")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Reconnect the Jellyfin share with its Music folder, then retry. Your staged tracks are kept.")
    owner = root.stat()
    if owner.st_uid in (0, 65534) or owner.st_gid in (0, 65534):
        raise ValueError("The Music share needs a dedicated writer identity. Check its storage configuration.")
    helper = Path(workflow_wizard.__file__).with_name("music_transfer.py")
    command = [sys.executable, str(helper), str(source), artist, album]
    if expected_bytes is not None:
        command += ["--check", str(expected_bytes)]
    return command, {"user": owner.st_uid, "group": owner.st_gid, "extra_groups": []}


def music_share_preflight(artist, album, expected_bytes):
    command, identity = music_share_command("", artist, album, expected_bytes)
    result = subprocess.run(command, **identity, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stdout.strip()[-700:] or "Jellyfin Music is unavailable. Check the share and retry.")


def music_import_destination_issue(music_root):
    if music_root.is_symlink() or not music_root.is_dir():
        return ("Your music tracks are ready, but the music library folder is not connected. "
                "Check the music destination in Settings, then retry the import. No need to rip the CD again.")
    return None


@app.post("/import/music")
def import_music():
    with lock:
        if state["job"].get("running"):
            return redirect(url_for("home"))

        disc = copy.deepcopy(state.get("disc") or {})

    if (
        disc.get("kind") != "audio"
        or request.form.get("scan_id") != disc.get("scan_id")
    ):
        with lock:
            state["job"]["status"] = "Import refused: scan the Audio CD again"
        return redirect(url_for("home"))

    sources = staged_audio_files(disc)
    album = safe_media_name(disc.get("album", ""))
    artist = safe_media_name(disc.get("album_artist", ""))

    if not sources or not album or not artist:
        with lock:
            state["job"]["status"] = (
                "Import refused: verified tracks, album, and artist are required"
            )
        return redirect(url_for("home"))

    shared_music = bool(os.environ.get("DEXTER_JELLYFIN_EXPORT"))
    prepared_root = None
    music_root = MUSIC_ROOT
    import_root = MUSIC_IMPORT_ROOT
    issue = None
    if shared_music:
        try:
            music_share_preflight(artist, album, sum(path.stat().st_size for path in sources.values()))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            issue = str(exc)
        if not issue:
            # Tag on local storage as the app user. Only newly prepared files
            # are made readable to the NFS writer; source permissions stay intact.
            prepared_root = AUDIO_OUTPUT / (".music-prepared-" + uuid.uuid4().hex)
            music_root = prepared_root
            import_root = prepared_root / ".partial"
    else:
        issue = music_import_destination_issue(music_root)
    if issue:
        with lock:
            state["job"].update(status=issue, music_import_failed=True, running=False,
                                 description=f"Import Music — {album}")
        persist_state_now(force=True)
        return redirect(url_for("home"))
    destination = music_root / artist / album
    token = uuid.uuid4().hex
    partial = import_root / token

    if (
        destination.exists()
        or destination.parent.is_symlink()
        or destination.parent.resolve().parent != music_root.resolve()
    ):
        with lock:
            state["job"]["status"] = (
                f"Import refused: album already exists at {destination}"
            )
        return redirect(url_for("home"))

    with lock:
        state["job"].update({
            "running": True,
            "description": f"Import Music — {album}",
            "status": "Preparing verified music import…",
            "music_import_failed": False,
            "log": [],
            "pid": None,
            "cancel_requested": False,
            "audio_imported_path": None,
            "progress": {
                "percent": 0.0,
                "track_percent": 0.0,
                "written": f"0 of {len(sources)} tracks",
                "expected": None,
                "elapsed": "0:00",
                "rate": None,
                "eta": None,
                "filename": None,
                "current_track": 0,
                "total_tracks": len(sources),
            },
            "recovery": None,
        })

    persist_state_now(force=True)

    def worker():
        started_at = time.time()

        try:
            if prepared_root:
                prepared_root.mkdir(mode=0o755)
                os.chmod(prepared_root, 0o755)
            music_root_stat = music_root.stat()
            import_root.mkdir(exist_ok=True)
            partial.mkdir()
            tracks = [
                track for track in disc["tracks"]
                if track["number"] in sources
            ]

            for index, track in enumerate(tracks, start=1):
                with lock:
                    if state["job"].get("cancel_requested"):
                        raise InterruptedError("Music import cancelled")

                    state["job"]["status"] = (
                        f"Tagging and verifying track {index} of {len(tracks)}: "
                        f"{track['title']}"
                    )
                    state["job"]["progress"].update({
                        "percent": ((index - 1) / len(tracks)) * 100.0,
                        "track_percent": 0.0,
                        "written": f"{index} of {len(tracks)} tracks",
                        "elapsed": format_duration(time.time() - started_at),
                        "filename": track["title"],
                        "current_track": track["number"],
                    })

                output_name = (
                    f"{track['number']:02d} - "
                    f"{safe_media_name(track['title'])}.flac"
                )
                target = partial / output_name
                command = [
                    "ffmpeg", "-v", "error", "-xerror", "-nostdin", "-n",
                    "-i", str(sources[track["number"]]),
                    "-map", "0:a:0", "-c:a", "copy", "-map_metadata", "-1",
                    "-metadata", f"album={album}",
                    "-metadata", f"album_artist={artist}",
                    "-metadata", f"artist={artist}",
                    "-metadata", f"title={track['title']}",
                    "-metadata", f"track={track['number']}",
                    "-metadata", f"tracktotal={len(disc['tracks'])}",
                ]

                if disc.get("year"):
                    command += ["-metadata", f"date={disc['year']}"]

                proc = subprocess.Popen(
                    command + [str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )

                with lock:
                    state["job"]["pid"] = proc.pid

                output, _ = proc.communicate()

                with lock:
                    state["job"]["pid"] = None
                    state["job"]["log"] = (
                        state["job"].get("log", []) + output.splitlines()
                    )[-80:]

                    if state["job"].get("cancel_requested"):
                        raise InterruptedError("Music import cancelled")

                if proc.returncode != 0 or not target.is_file():
                    raise RuntimeError(
                        f"Track {track['number']} tagging failed"
                    )

                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_name:format_tags=album,album_artist,artist,title,track,date",
                        "-of", "json", str(target),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                metadata = json.loads(probe.stdout)
                streams = metadata.get("streams") or []
                tags = {
                    key.lower(): value
                    for key, value in (metadata.get("format", {}).get("tags") or {}).items()
                }

                if not streams or streams[0].get("codec_name") != "flac":
                    raise RuntimeError(f"Track {track['number']} is not FLAC")

                for key, value in {
                    "album": album,
                    "album_artist": artist,
                    "artist": artist,
                    "title": track["title"],
                    "track": str(track["number"]),
                }.items():
                    if tags.get(key) != value:
                        raise RuntimeError(
                            f"Track {track['number']} is missing {key} metadata"
                        )

                os.chown(target, music_root_stat.st_uid, music_root_stat.st_gid)
                os.chmod(target, 0o664)

                with lock:
                    state["job"]["progress"].update({
                        "percent": (index / len(tracks)) * 100.0,
                        "track_percent": 100.0,
                        "elapsed": format_duration(time.time() - started_at),
                    })

            artist_directory = destination.parent

            if not artist_directory.exists():
                artist_directory.mkdir()
                os.chown(
                    artist_directory,
                    music_root_stat.st_uid,
                    music_root_stat.st_gid,
                )
                os.chmod(artist_directory, stat.S_IMODE(music_root_stat.st_mode))

            os.chown(partial, music_root_stat.st_uid, music_root_stat.st_gid)
            os.chmod(partial, stat.S_IMODE(music_root_stat.st_mode))
            os.rename(partial, destination)
            published_path = destination
            if shared_music:
                command, identity = music_share_command(destination, artist, album)
                with subprocess.Popen(command, **identity, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, start_new_session=True) as transfer:
                    with lock:
                        state["job"]["pid"] = transfer.pid
                        state["job"]["status"] = "Copying and verifying your album on Jellyfin storage…"
                        state["job"]["progress"].update(percent=0, written=f"0 of {len(tracks)} tracks", filename="Jellyfin Music")
                        if state["job"].get("cancel_requested"):
                            os.killpg(transfer.pid, signal.SIGTERM)
                    details = []
                    for line in transfer.stdout:
                        details.append(line.strip())
                        with lock:
                            state["job"]["log"] = (state["job"].get("log", []) + [line.strip()])[-80:]
                            if line.startswith("Track verified: "):
                                count = int(line.split(": ", 1)[1])
                                state["job"]["progress"].update(percent=100*count/len(tracks),
                                    written=f"{count} of {len(tracks)} tracks", elapsed=format_duration(time.time()-started_at))
                    transfer.wait()
                    with lock:
                        state["job"]["pid"] = None
                        if state["job"].get("cancel_requested"):
                            raise InterruptedError("Music import cancelled")
                    if transfer.returncode:
                        raise ValueError("\n".join(details)[-1000:])
                published_path = Path("/jellyfin-import/music") / artist / album
            refreshed, refresh_message = refresh_jellyfin()

            with lock:
                state["job"].update({
                    "running": False,
                    "pid": None,
                    "cancel_requested": False,
                    "status": (
                        "Album imported; Jellyfin library refresh requested"
                        if refreshed
                        else f"Album imported; {refresh_message}"
                    ),
                    "audio_imported_path": str(published_path),
                    "music_import_failed": False,
                })
                state["job"]["progress"]["percent"] = 100.0
                state["job"]["progress"]["eta"] = None

        except Exception as exc:
            if partial.exists() and partial.is_dir() and not partial.is_symlink():
                shutil.rmtree(partial)

            with lock:
                cancelled = state["job"].get("cancel_requested")
                state["job"].update({
                    "running": False,
                    "pid": None,
                    "cancel_requested": False,
                    "status": (
                        "Music import cancelled; staged FLAC files retained"
                        if cancelled
                        else "Your music could not be added to the library. Check the destination and its permissions, then retry the import. Your verified tracks are kept; no need to rip again."
                    ),
                    "music_import_failed": not cancelled,
                    "log": (state["job"].get("log", []) + [str(exc)])[-80:],
                })

        finally:
            if prepared_root and prepared_root.is_dir() and not prepared_root.is_symlink():
                shutil.rmtree(prepared_root)

        finish_workflow_entry(state["job"], "completed" if state["job"].get("audio_imported_path") else "failed", state["job"]["status"])
        persist_state_now(force=True)

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("home"))


@app.post("/metadata-lookup")
def metadata_lookup():
    media_type = request.form.get("media_type", "")
    title = safe_media_name(request.form.get("title", ""))
    year = request.form.get("year", "").strip()
    draft = {
        "media_type": media_type,
        "title": title,
        "year": year,
        "season": request.form.get("season", "1"),
        "start_episode": request.form.get("start_episode", "1"),
        "primary_file": request.form.get("primary_file", ""),
    }

    with lock:
        current_disc = copy.deepcopy(state.get("disc") or {})

    if current_disc.get("kind") == "audio":
        with lock:
            state["job"]["metadata_results"] = []
            state["job"]["metadata_draft"] = {}
            state["job"]["status"] = (
                "Video title lookup is unavailable while an Audio CD is active"
            )
        return redirect(url_for("home"))

    if media_type not in ("movie", "tv") or not title:
        with lock:
            state["job"]["status"] = "Metadata lookup requires a title"
        return redirect(url_for("home"))

    try:
        results = (
            lookup_movie_metadata(title, year or None)
            if media_type == "movie"
            else lookup_tv_metadata(title)
        )
        message = (
            f"Found {len(results)} metadata match"
            f"{'es' if len(results) != 1 else ''}"
            if results else "No metadata matches found; edit the fields manually"
        )
    except Exception as exc:
        results = []
        message = f"Metadata lookup unavailable: {exc}; manual naming is available"

    with lock:
        state["job"]["metadata_results"] = results
        state["job"]["metadata_draft"] = draft
        state["job"]["status"] = message

    persist_state_now(force=True)
    return redirect(url_for("home"))


def start_nfs_batch_import(outputs, plans, kind):
    manifest = {"kind": kind, "files": [{"source": str(source),
        "relative": target.relative_to(MEDIA_ROOT / kind).as_posix(),
        "expected_bytes": source.stat().st_size} for source, target in plans]}
    helper = Path(workflow_wizard.__file__).with_name("jellyfin_batch_transfer.py")
    command = [sys.executable, str(helper)]
    identity = video_transfer_identity({"series": "batch"} if kind == "tv" else None)
    result = subprocess.run(command + ["--check"], input=json.dumps(manifest), **identity,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stdout.strip()[-800:] or "Jellyfin storage is unavailable. Your local files are kept.")
    total = sum(source.stat().st_size for source, _ in plans)
    started = time.monotonic()
    def progress(copied):
        elapsed = max(time.monotonic()-started, .001)
        speed = copied/elapsed
        return {"percent": min(100,100*copied/total) if total else 0, "written": format_size(copied),
                "expected": format_size(total), "elapsed": format_duration(elapsed),
                "rate": format_size(speed)+"/s" if speed else None,
                "eta": format_duration(max(0,total-copied)/speed) if speed else None}
    with lock:
        if state["job"].get("running"):
            raise ValueError("Another job started. Your local files are kept.")
        job = state["job"]
        job.update(running=True, description=f"Adding {len(plans)} files to Jellyfin", status="Preparing verified batch transfer…",
                   pid=None, cancel_requested=False, recovery={"kind":"import"}, import_failed=False,
                   import_retry=None, batch_import=None, log=[], progress=progress(0))
    persist_state_now(force=True)
    def worker():
        published = False
        try:
            with subprocess.Popen(command, **identity, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, start_new_session=True) as process:
                with lock:
                    job["pid"] = process.pid
                    if job.get("cancel_requested"):
                        process.terminate()
                process.stdin.write(json.dumps(manifest));process.stdin.close()
                detail = "The transfer did not complete. Local files are kept."
                for line in process.stdout:
                    line=line.strip()
                    with lock:
                        if line.startswith("Transfer bytes: "):
                            job["progress"] = progress(int(line.split(": ",1)[1]))
                            job["status"] = "Copying your files to Jellyfin…"
                        elif line.startswith("Checking file: "):
                            job["status"] = "Checking copied file " + line.split(": ",1)[1]
                            job["progress"].update(checking_copy=True, eta=None)
                        else:
                            job["log"] = (job.get("log",[]) + [line])[-80:]
                            if line.startswith("Action: "):detail=line[8:]
                if process.wait():raise ValueError(detail)
                published=True
            refreshed, message=refresh_jellyfin()
            with lock:
                job.update(status=f"Imported to Jellyfin: {len(plans)} files copied and verified; local copies retained",
                    batch_import={"verified":True,"kind":kind,"files":[item["relative"] for item in manifest["files"]]})
                if not refreshed:job["log"].append(message)
                job["progress"].update(percent=100, checking_copy=False, eta=None)
        except Exception as exc:
            with lock:
                job["status"] = "Batch transfer cancelled; local files kept" if job.get("cancel_requested") else str(exc)
                job["log"]=(job.get("log",[]) + [str(exc)])[-80:]
        finally:
            with lock:
                job.update(running=False,pid=None,recovery=None,import_failed=not published,cancel_requested=False)
            persist_state_now(force=True)
    threading.Thread(target=worker,daemon=True).start()


@app.post("/import-jellyfin-batch")
def import_jellyfin_batch():
    with lock:
        if state["job"].get("running"):
            return redirect(url_for("home"))

        outputs = copy.deepcopy(_job_outputs(state["job"]))
        title_queue = copy.deepcopy(state["job"].get("title_queue") or [])
        metadata_results = copy.deepcopy(
            state["job"].get("metadata_results") or []
        )

    if len(outputs) < 2 or not all(
        item.get("path")
        and (item.get("verification") or {}).get("ok")
        for item in outputs
    ):
        with lock:
            state["job"]["status"] = (
                "Batch import refused: every staged file must be verified"
            )
        return redirect(url_for("home"))

    media_type = request.form.get("media_type", "")
    title = safe_media_name(request.form.get("title", ""))
    year = request.form.get("year", "").strip()
    selected_metadata = None

    try:
        choice = int(request.form.get("metadata_choice", ""))
        selected_metadata = metadata_results[choice]
    except (ValueError, IndexError):
        pass

    if selected_metadata:
        title = safe_media_name(selected_metadata.get("name") or title)
        year = str(selected_metadata.get("year") or year)

    if media_type not in ("movie", "tv") or not title:
        with lock:
            state["job"]["status"] = "Batch import refused: invalid metadata"
        return redirect(url_for("home"))

    if year and not re.fullmatch(r"\d{4}", year):
        with lock:
            state["job"]["status"] = "Batch import refused: invalid year"
        return redirect(url_for("home"))

    sources = [Path(item["path"]).resolve() for item in outputs]

    if any(not _allowed_staged_video(source) or not source.is_file()
           for source in sources):
        with lock:
            state["job"]["status"] = "Batch import refused: invalid staged file"
        return redirect(url_for("home"))

    display_name = f"{title} ({year})" if year else title
    plans = []

    if media_type == "movie":
        provider_ids = (selected_metadata or {}).get("provider_ids") or {}
        tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("TMDb")

        if tmdb_id:
            display_name += f" [tmdbid-{tmdb_id}]"

        library_root = MEDIA_ROOT / "movies"
        destination_root = library_root / display_name
        primary = request.form.get("primary_file", "")

        if primary not in {str(source) for source in sources}:
            with lock:
                state["job"]["status"] = (
                    "Batch import refused: choose a valid main feature"
                )
            return redirect(url_for("home"))

        extra_names = set()

        for source in sources:
            if str(source) == primary:
                target = destination_root / f"{display_name}{source.suffix.lower()}"
            else:
                base = safe_media_name(source.stem) or "Extra"
                candidate = base
                counter = 2

                while candidate.casefold() in extra_names:
                    candidate = f"{base} {counter}"
                    counter += 1

                extra_names.add(candidate.casefold())
                target = destination_root / "extras" / f"{candidate}{source.suffix.lower()}"

            plans.append((source, target))

    else:
        library_root = MEDIA_ROOT / "tv"
        destination_root = library_root / display_name

        try:
            season = int(request.form.get("season", ""))
            start_episode = int(request.form.get("start_episode", ""))
        except ValueError:
            season = -1
            start_episode = 0

        if season < 0 or start_episode < 1:
            with lock:
                state["job"]["status"] = (
                    "Batch import refused: invalid season or episode"
                )
            return redirect(url_for("home"))

        season_dir = destination_root / f"Season {season:02d}"
        episode_names = {}
        tvmaze_id = ((selected_metadata or {}).get("provider_ids") or {}).get(
            "Tvmaze"
        )

        if tvmaze_id and not os.environ.get("DEXTER_JELLYFIN_EXPORT"):
            try:
                episode_names = lookup_tv_episodes(tvmaze_id, season)
            except Exception:
                episode_names = {}

        for offset, source in enumerate(sources):
            if os.environ.get("DEXTER_JELLYFIN_EXPORT"):
                assignment = outputs[offset].get("episode") or {}
                if (assignment.get("evidence") or {}).get("state") != "user_confirmed":
                    with lock:
                        state["job"]["status"] = "Confirm each episode assignment before copying this batch to Jellyfin. Local files are kept."
                    return redirect(url_for("home"))
                try:
                    series = safe_media_name(assignment["series"])
                    filename = disc_intelligence.episode_filename(series, assignment["season"], assignment["number"],
                                                                  assignment.get("name", ""), source.suffix.lower())
                    target = library_root / series / f"Season {int(assignment['season']):02d}" / filename
                except (KeyError, ValueError, TypeError):
                    with lock:
                        state["job"]["status"] = "Review the saved episode assignments. Local files are kept."
                    return redirect(url_for("home"))
                plans.append((source, target))
                continue
            episode = start_episode + offset
            episode_title = safe_media_name(
                episode_names.get(episode, "")
            )
            title_suffix = f" - {episode_title}" if episode_title else ""
            target = season_dir / (
                f"{display_name} S{season:02d}E{episode:02d}"
                f"{title_suffix}{source.suffix.lower()}"
            )
            plans.append((source, target))

    if os.environ.get("DEXTER_JELLYFIN_EXPORT"):
        try:
            start_nfs_batch_import(outputs, plans, "tv" if media_type == "tv" else "movies")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            with lock:
                state["job"]["status"] = str(exc)
            persist_state_now(force=True)
        return redirect(url_for("home"))

    if destination_root.exists():
        with lock:
            state["job"]["status"] = (
                f"Batch import refused: {display_name} already exists"
            )
        return redirect(url_for("home"))

    total_bytes = sum(source.stat().st_size for source, _ in plans)

    with lock:
        state["job"] = {
            "running": True,
            "description": f"Import batch — {display_name}",
            "status": "Preparing batch import…",
            "log": [],
            "output_file": outputs[0]["path"],
            "verification": outputs[0]["verification"],
            "outputs": outputs,
            "title_queue": title_queue,
            "pid": None,
            "cancel_requested": False,
            "progress": {
                "percent": 0.0,
                "written": format_size(0),
                "expected": format_size(total_bytes),
                "elapsed": format_duration(0),
                "rate": None,
                "eta": None,
                "filename": None,
            },
            "recovery": {
                "kind": "batch_import",
                "library_root": str(library_root),
                "destination_root": str(destination_root),
            },
        }

    persist_state_now(force=True)

    def batch_worker():
        started_at = time.time()
        copied = 0

        try:
            destination_root.mkdir(parents=True, exist_ok=False)
            apply_library_permissions(
                destination_root, is_dir=True, library_root=library_root
            )

            for index, (source, target) in enumerate(plans, start=1):
                with lock:
                    if state["job"].get("cancel_requested"):
                        raise InterruptedError("Batch import cancelled")

                    state["job"]["status"] = (
                        f"Importing file {index} of {len(plans)}…"
                    )

                target.parent.mkdir(parents=True, exist_ok=True)
                apply_library_permissions(
                    target.parent, is_dir=True, library_root=library_root
                )
                partial = target.with_suffix(target.suffix + ".partial")

                digest = hashlib.sha256()
                with source.open("rb") as src, partial.open("xb") as dst:
                    before = os.fstat(src.fileno())
                    while True:
                        with lock:
                            if state["job"].get("cancel_requested"):
                                raise InterruptedError("Batch import cancelled")

                        chunk = src.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        digest.update(chunk)
                        copied += len(chunk)
                        elapsed = max(time.time() - started_at, 0.001)
                        rate = copied / elapsed

                        with lock:
                            state["job"]["progress"] = {
                                "percent": min(100.0, copied * 100.0 / total_bytes),
                                "written": format_size(copied),
                                "expected": format_size(total_bytes),
                                "elapsed": format_duration(elapsed),
                                "rate": f"{format_size(rate)}/s",
                                "eta": format_duration(
                                    (total_bytes - copied) / rate
                                ),
                                "filename": target.name,
                            }

                    dst.flush()
                    os.fsync(dst.fileno())

                apply_library_permissions(
                    partial, is_dir=False, library_root=library_root
                )
                with partial.open("rb") as check:
                    received = hashlib.file_digest(check, "sha256").hexdigest()
                after = source.stat()
                if (received != digest.hexdigest() or partial.stat().st_size != before.st_size
                        or any(getattr(before, key) != getattr(after, key)
                               for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))):
                    raise ValueError("Copied file did not verify; staged files kept")
                os.link(partial, target)
                partial.unlink()

            refreshed, refresh_message = refresh_jellyfin()

            with lock:
                state["job"]["running"] = False
                state["job"]["cancel_requested"] = False
                state["job"]["recovery"] = None
                state["job"]["progress"]["percent"] = 100.0
                state["job"]["progress"]["eta"] = format_duration(0)
                state["job"]["status"] = (
                    f"Saved {len(plans)} files in the configured local library as {display_name}; local sources retained. Jellyfin storage access was not verified. "
                    + (
                        "library refresh requested"
                        if refreshed else refresh_message
                    )
                )

        except Exception as exc:
            # Preserve destination evidence: another writer may have added media.

            with lock:
                cancelled = state["job"].get("cancel_requested")
                state["job"]["running"] = False
                state["job"]["cancel_requested"] = False
                state["job"]["recovery"] = None
                state["job"]["progress"] = None
                state["job"]["status"] = (
                    "Batch import cancelled; staged files kept"
                    if cancelled else f"Batch import failed: {exc}"
                )

    threading.Thread(target=batch_worker, daemon=True).start()
    return redirect(url_for("home"))


def start_nfs_movie_import(source, title, year, expected_job=None, episode=None):
    total = source.stat().st_size
    started = time.monotonic()

    def copy_progress(copied):
        elapsed = max(time.monotonic() - started, 0.001)
        rate = copied / elapsed
        return {
            "percent": min(100.0, copied * 100 / total) if total else 0.0,
            "written": format_size(copied),
            "expected": format_size(total),
            "elapsed": format_duration(elapsed),
            "rate": f"{format_size(rate)}/s" if rate else None,
            "eta": format_duration(max(0, total - copied) / rate) if rate else None,
            "filename": source.name,
        }

    with lock:
        job = state["job"]
        if job.get("running") or (expected_job is not None and job is not expected_job):
            return False
        job.update(running=True, description=f"Adding {title} to Jellyfin",
                   status="Preparing your episode for Jellyfin…" if episode else "Preparing your movie for Jellyfin…", log=[], progress=copy_progress(0),
                   pid=None, cancel_requested=False, video_retry_token=None,
                   import_failed=False, movie_import=None,
                   import_retry={"source": str(source), "title": title, "year": year, "episode": episode},
                   recovery={"kind": "import", "source": str(source)})
    persist_state_now(force=True)

    def worker():
        status = "Could not add the movie. Check the Jellyfin share and try Import to Jellyfin again."
        published = False
        cancelled = False
        try:
            helper = Path(__file__).with_name("jellyfin_transfer.py")
            command = [sys.executable, str(helper), str(source), title, year]
            if episode:
                command += ["--episode", json.dumps(episode)]
            with subprocess.Popen(command, **video_transfer_identity(episode), start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            ) as proc:
                with lock:
                    state["job"]["pid"] = proc.pid
                    if state["job"]["cancel_requested"]:
                        proc.terminate()
                for line in proc.stdout:
                    line = line.strip()
                    with lock:
                        job = state["job"]
                        if line.startswith("Transfer bytes: "):
                            copied = int(line.removeprefix("Transfer bytes: "))
                            job["progress"] = copy_progress(copied)
                            job["status"] = "Copying your episode to Jellyfin…" if episode else "Copying your movie to Jellyfin…"
                            continue
                        job["log"] = (job["log"] + [line])[-80:]
                        if line.startswith("Action: "):
                            status = line.removeprefix("Action: ")
                        if line.startswith("Checking the copy"):
                            job["status"] = line
                            job["progress"].update(checking_copy=True, eta=None)
                rc = proc.wait()
                with lock:
                    state["job"]["pid"] = None
                    cancelled = state["job"]["cancel_requested"]
                if rc == 0:
                    published = True
                    status = "Episode copied and verified. Scan the TV library in Jellyfin to see it." if episode else "Movie copied and verified. Scan the Movies library in Jellyfin to see it."
                    with lock:
                        state["job"]["progress"].pop("checking_copy", None)
                        state["job"]["status"] = "Updating Jellyfin’s library…"
                    refreshed, detail = refresh_jellyfin()
                    status = "Episode added. Your staged copy is still available." if episode else "Movie added. Your staged copy is still available."
                    if not refreshed:
                        status = "Episode copied and verified. Scan the TV library in Jellyfin to see it." if episode else "Movie copied and verified. Scan the Movies library in Jellyfin to see it."
                        with lock:
                            state["job"]["log"].append(detail)
                elif cancelled:
                    status = "Import cancelled. Your staged movie is ready to retry."
        except Exception as exc:
            with lock:
                state["job"]["log"].append(str(exc))
        finally:
            with lock:
                state["job"].update(running=False, pid=None, recovery=None,
                                    cancel_requested=False, status=status)
                state["job"]["import_failed"] = not published and not cancelled
                report = state["job"].get("processing_report")
                if report and report.get("output") == str(source):
                    report["transfer_seconds"] = time.monotonic() - started
                    report["imported"] = published
                    for record in state.get("movie_history", []):
                        if record.get("output") == str(source):
                            record.update(transfer_seconds=report["transfer_seconds"], imported=published)
                if published:
                    display = f"{title} ({year})" if year else title
                    state["job"]["movie_import"] = {
                        "title": title, "display": display,
                        "server_path": (f"/media/tv/{episode['series']}/Season {episode['season']:02d}/" +
                            disc_intelligence.episode_filename(episode["series"], episode["season"], episode["number"], episode["name"], source.suffix.lower())) if episode else f"/media/movies/{display}/{display}{source.suffix.lower()}",
                        "episode": episode,
                    }
                    state["job"].pop("import_retry", None)
                else:
                    state["job"]["progress"] = None
            finish_workflow_entry(state["job"], "completed" if published else "cancelled" if cancelled else "failed", status)
            persist_state_now(force=True)

    threading.Thread(target=worker, daemon=True).start()
    return True


@app.post("/jellyfin/check-movie")
def check_imported_movie():
    with lock:
        record = copy.deepcopy(state["job"].get("movie_import"))
        if not record or state["job"].get("running"):
            return redirect(url_for("home"))
    key = JELLYFIN_KEY_FILE.read_text().strip() if JELLYFIN_KEY_FILE.exists() else ""
    item_id = library_connection.find_movie(configured_jellyfin_url(), key,
        record["episode"].get("name") or record["title"] if record.get("episode") else record["title"],
        record["server_path"], **({"item_type": "Episode"} if record.get("episode") else {}))
    with lock:
        if state["job"].get("movie_import") == record and not state["job"].get("running"):
            record["item_id"] = item_id
            record["checked"] = True
            state["job"]["movie_import"] = record
    persist_state_now(force=True)
    return redirect(url_for("home"))


@app.post("/import-jellyfin")
def import_jellyfin():
    with lock:
        if state["job"].get("running"):
            return redirect(url_for("home"))

        outputs = copy.deepcopy(_job_outputs(state["job"]))
        title_queue = copy.deepcopy(state["job"].get("title_queue") or [])

    requested_file = request.form.get("output_file", "")
    selected = next(
        (item for item in outputs if item.get("path") == requested_file),
        None,
    )

    if selected is None and len(outputs) == 1 and not requested_file:
        selected = outputs[0]

    verification = (selected or {}).get("verification")
    filename = (selected or {}).get("path")

    if not verification or not verification.get("ok") or not filename:
        with lock:
            state["job"]["status"] = "Import refused: output is not verified"
        return redirect(url_for("home"))

    destination = request.form.get("destination", "jellyfin")
    if destination not in MOVIE_DESTINATIONS:
        return "Choose an available destination.", 400
    if destination == "staging":
        with lock:
            if not state["job"].get("running"):
                state["job"]["status"] = "Your verified movie is ready in staging."
                state["job"].pop("import_retry", None)
                state["job"]["import_failed"] = False
        persist_state_now(force=True)
        return redirect(url_for("home"))

    title = safe_media_name(request.form.get("movie_title", ""))
    year = request.form.get("year", "").strip()

    if not title:
        with lock:
            state["job"]["status"] = "Import refused: movie title is required"
        return redirect(url_for("home"))

    if year and not re.fullmatch(r"\d{4}", year):
        with lock:
            state["job"]["status"] = "Import refused: invalid year"
        return redirect(url_for("home"))

    source = Path(filename).resolve()
    staging_root = VIDEO_OUTPUT.resolve()

    if (
        source.parent != staging_root
        or source.suffix.lower() not in (".mkv", ".mp4")
        or not source.is_file()
    ):
        with lock:
            state["job"]["status"] = "Import refused: invalid staged file"
        return redirect(url_for("home"))

    display_name = f"{title} ({year})" if year else title

    if os.environ.get("DEXTER_JELLYFIN_EXPORT"):
        with lock:
            selected_output = next((item for item in _job_outputs(state["job"]) if item.get("path") == str(source)), {})
            retry = state["job"].get("import_retry") or {}
            episode = selected_output.get("episode") or (retry.get("episode") if retry.get("source") == str(source) else None)
        start_nfs_movie_import(source, title, year, **({"episode": episode} if episode else {}))
        return redirect(url_for("home"))

    destination_dir = MEDIA_ROOT / "movies" / display_name
    destination_file = destination_dir / f"{display_name}{source.suffix.lower()}"
    partial_file = Path(str(destination_file) + ".partial")

    if destination_dir.exists():
        with lock:
            state["job"]["status"] = (
                f"Import refused: {display_name} already exists in Jellyfin"
            )
        return redirect(url_for("home"))

    try:
        total_bytes = source.stat().st_size
        destination_dir.mkdir(parents=True, exist_ok=False)
        apply_library_permissions(destination_dir, is_dir=True)

    except FileExistsError:
        with lock:
            state["job"]["status"] = (
                f"Import refused: {display_name} already exists in Jellyfin"
            )
        return redirect(url_for("home"))

    except Exception as exc:
        with lock:
            state["job"]["status"] = f"Import failed: {exc}"
        return redirect(url_for("home"))

    with lock:
        state["job"] = {
            "running": True,
            "description": f"Import to Jellyfin — {display_name}",
            "status": "Importing to Jellyfin…",
            "log": [],
            "output_file": str(source),
            "verification": verification,
            "outputs": outputs,
            "title_queue": title_queue,
            "pid": None,
            "cancel_requested": False,
            "progress": {
                "percent": 0.0,
                "written": format_size(0),
                "expected": format_size(total_bytes),
                "elapsed": format_duration(0),
                "rate": None,
                "eta": None,
                "filename": destination_file.name,
            },
        }

        state["job"]["recovery"] = {
            "kind": "import",
            "source": str(source),
            "destination_dir": str(destination_dir),
            "destination_file": str(destination_file),
            "partial_file": str(partial_file),
        }

    persist_state_now(force=True)

    def import_worker():
        started_at = time.time()
        copied = 0
        chunk_size = 8 * 1024 * 1024

        try:
            cancelled = False

            with source.open("rb") as src, partial_file.open("xb") as dst:
                while True:
                    with lock:
                        cancelled = state["job"].get(
                            "cancel_requested", False
                        )

                    if cancelled:
                        break

                    chunk = src.read(chunk_size)

                    if not chunk:
                        break

                    dst.write(chunk)
                    copied += len(chunk)

                    elapsed = max(time.time() - started_at, 0.001)
                    rate = copied / elapsed

                    percent = (
                        min(100.0, copied * 100.0 / total_bytes)
                        if total_bytes
                        else 100.0
                    )

                    remaining = max(total_bytes - copied, 0)
                    eta = remaining / rate if rate > 0 else None

                    with lock:
                        state["job"]["status"] = "Importing to Jellyfin…"
                        state["job"]["progress"] = {
                            "percent": percent,
                            "written": format_size(copied),
                            "expected": format_size(total_bytes),
                            "elapsed": format_duration(elapsed),
                            "rate": (
                                f"{format_size(rate)}/s"
                                if rate > 0
                                else None
                            ),
                            "eta": (
                                format_duration(eta)
                                if eta is not None
                                else None
                            ),
                            "filename": destination_file.name,
                        }

                if not cancelled:
                    dst.flush()
                    os.fsync(dst.fileno())

            if cancelled:
                partial_file.unlink(missing_ok=True)

                try:
                    destination_dir.rmdir()
                except OSError:
                    pass

                with lock:
                    state["job"]["running"] = False
                    state["job"]["recovery"] = None
                    state["job"]["cancel_requested"] = False
                    state["job"]["status"] = (
                        "Import cancelled; staged file kept"
                    )
                    state["job"]["progress"] = None

                return

            if copied != total_bytes:
                raise IOError(
                    f"Copy incomplete: {copied} of {total_bytes} bytes written"
                )

            # Apply Jellyfin ownership/mode before exposing the final MKV.
            apply_library_permissions(partial_file, is_dir=False)

            # Atomic publication: Jellyfin never sees a half-written .mkv.
            os.replace(partial_file, destination_file)

            # Retain the verified source for recovery and alternate outputs.

            elapsed = max(time.time() - started_at, 0.001)
            avg_rate = total_bytes / elapsed if elapsed > 0 else 0

            with lock:
                state["job"]["status"] = "Refreshing Jellyfin…"
                state["job"]["progress"] = {
                    "percent": 100.0,
                    "written": format_size(total_bytes),
                    "expected": format_size(total_bytes),
                    "elapsed": format_duration(elapsed),
                    "rate": (
                        f"{format_size(avg_rate)}/s"
                        if avg_rate > 0
                        else None
                    ),
                    "eta": format_duration(0),
                    "filename": destination_file.name,
                }

            refreshed, refresh_message = refresh_jellyfin()

            with lock:
                state["job"]["running"] = False
                state["job"]["recovery"] = None
                state["job"]["cancel_requested"] = False
                # Source and output record remain available after publication.

                if refreshed:
                    state["job"]["status"] = (
                        f"Imported to Jellyfin: {display_name}; "
                        "library refresh requested"
                    )
                else:
                    state["job"]["status"] = (
                        f"Imported to Jellyfin: {display_name}; "
                        f"{refresh_message}"
                    )

        except Exception as exc:
            partial_file.unlink(missing_ok=True)

            if not destination_file.exists():
                try:
                    destination_dir.rmdir()
                except OSError:
                    pass

            with lock:
                state["job"]["running"] = False
                state["job"]["recovery"] = None
                state["job"]["cancel_requested"] = False
                state["job"]["status"] = f"Import failed: {exc}"

    threading.Thread(target=import_worker, daemon=True).start()

    return redirect(url_for("home"))
def wizard_audio_plan(disc, fields):
    selected = selected_audio_tracks(disc, fields.get("tracks", []))
    album = safe_media_name(fields.get("album", ""))
    artist = safe_media_name(fields.get("artist", ""))
    year = fields.get("album_year", "").strip()
    destination = fields.get("audio_destination", "staging")
    if not album or not artist:
        raise ValueError("Enter an album name and album artist.")
    if year and not re.fullmatch(r"\d{4}", year):
        raise ValueError("Enter a four-digit release year or leave it blank.")
    if destination not in ("staging", "music"):
        raise ValueError("Choose local staging or the configured music library.")
    titles = {}
    for track in disc.get("tracks", []):
        if track["number"] in selected:
            name = safe_media_name(fields.get("track_name_" + str(track["number"]), track.get("title", "")))
            if not name:
                raise ValueError("Give each selected music track a title.")
            titles[str(track["number"])] = name
    return {"title": album, "album": album, "artist": artist, "year": year,
            "tracks": selected, "track_titles": titles, "scan_id": disc["scan_id"],
            "discid": disc.get("discid"), "destination": destination, "codec": "flac",
            "profile": "lossless", "engine": "cyanrip"}


def wizard_audio_preflight(disc, fields, expected_disc):
    checks = []
    def add(name, status, message, step=3):
        checks.append({"name": name, "status": status, "message": message, "step": step})
    try:
        plan = wizard_audio_plan(disc, fields)
    except ValueError as exc:
        add("Album choices", "blocked", str(exc), 2 if not fields.get("tracks") else 3)
        return checks
    with lock:
        busy = state["job"].get("running") or any(e["status"] in ("starting", "running") for e in state.get("workflow_queue", []))
    if busy:
        add("Current job", "blocked", "Wait for the current job to finish.", 1)
    if workflow_wizard.disc_id(disc) != expected_disc or not disc.get("discid"):
        add("CD identity", "blocked", "Scan this music CD again before starting.", 1)
    else:
        add("CD and tracks", "passed", str(len(plan["tracks"])) + " selected tracks match the saved CD scan.", 2)
    drive = configured_drive()
    if not Path(drive).exists() or not os.access(drive, os.R_OK) or optical_drive_status(drive) != CDS_DISC_OK:
        add("Disc drive", "blocked", "The CD drive is not ready. Check the tray and scan again.", 1)
    else:
        add("Disc drive", "passed", "The optical drive reports a disc ready for reading.", 1)
    missing = [name for name in ("cyanrip", "ffmpeg", "ffprobe") if not shutil.which(name)]
    add("Music tools", "blocked" if missing else "passed",
        "Install the missing music tools in Settings." if missing else "CD reading, lossless FLAC and file checks are available.")
    seconds = 0
    for track in disc.get("tracks", []):
        if track["number"] in plan["tracks"]:
            try:
                parts = [float(part) for part in track["duration"].split(":")]
                seconds += sum(value * 60**index for index, value in enumerate(reversed(parts)))
            except (ValueError, KeyError):
                pass
    reserve = int(seconds * 44100 * 2 * 2 * 3) + 64*1024**2
    try:
        with tempfile.TemporaryFile(dir=AUDIO_OUTPUT) as handle:
            handle.write(b"Dexter CD plan check")
        free = shutil.disk_usage(AUDIO_OUTPUT).free
        add("Working storage", "passed" if free >= reserve else "blocked",
            format_size(free) + " available; reserve " + format_size(reserve) + " for lossless tracks and preparation.")
    except OSError:
        add("Working storage", "blocked", "Dexter cannot write to music staging. Check its folder permissions.")
    if plan["destination"] == "music":
        destination = MUSIC_ROOT / plan["artist"] / plan["album"]
        try:
            if os.environ.get("DEXTER_JELLYFIN_EXPORT"):
                music_share_preflight(plan["artist"], plan["album"], reserve)
                add("Music library", "passed", "The Jellyfin Music share is writable with its configured writer identity.")
                add("Final track verification", "pending", "Extracted FLAC tracks are checked before verified transfer to Jellyfin.")
                return checks
            if not MUSIC_ROOT.is_dir() or MUSIC_ROOT.is_symlink() or destination.parent.is_symlink():
                raise ValueError("Configure the music library folder before importing.")
            if destination.exists():
                raise ValueError("This album already exists in the music library.")
            with tempfile.TemporaryFile(dir=MUSIC_ROOT) as handle:
                handle.write(b"Dexter music destination check")
            if shutil.disk_usage(MUSIC_ROOT).free < reserve:
                raise ValueError("The music library needs more free space.")
            add("Music library", "passed", "The configured music folder is writable. Its Jellyfin mapping is managed in Storage settings.")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            add("Music library", "blocked", str(exc))
    else:
        add("Destination", "passed", "Verified FLAC tracks will remain in local staging.")
    add("Final track verification", "pending", "The CD identity is checked again before reading. Each extracted FLAC is decoded and verified before completion.")
    return checks


def launch_audio_workflow(plan):
    with lock:
        disc = state.get("disc") or {}
        if disc.get("kind") != "audio" or disc.get("scan_id") != plan["scan_id"] or disc.get("discid") != plan["discid"]:
            raise ValueError("The music CD changed. Scan it again.")
        disc.update(album=plan["album"], album_artist=plan["artist"], year=plan["year"])
        for track in disc.get("tracks", []):
            if str(track["number"]) in plan["track_titles"]:
                track["title"] = plan["track_titles"][str(track["number"])]
    with app.test_request_context("/rip/audio", method="POST",
                                  data={"scan_id": plan["scan_id"], "track": [str(n) for n in plan["tracks"]]}):
        rip_audio(audio_workflow=plan)


def continue_audio_workflow(job, plan):
    started = time.monotonic()
    with lock:
        if state["job"] is not job or job.get("running") or job.get("cancel_requested"):
            return
        job.update(running=True, stage="validating", status="Checking your music tracks…")
        disc = copy.deepcopy(state["disc"])
    def cancelled():
        with lock:
            return bool(job.get("cancel_requested"))
    def update(**fields):
        with lock:
            if "pid" in fields:
                job["pid"] = fields["pid"]
            if fields.get("log"):
                job["log"] = (job.get("log", []) + fields["log"].splitlines())[-80:]
    try:
        tracks = staged_audio_files(disc)
        if set(tracks) != set(plan["tracks"]):
            raise ValueError("The CD rip did not produce every selected track. The extracted files are kept for review.")
        for index, (number, path) in enumerate(sorted(tracks.items()), start=1):
            with lock:
                job["status"] = "Checking track " + str(number) + "…"
                job["progress"] = {"percent": 100 * (index - 1) / len(tracks), "written": str(index - 1) + " of " + str(len(tracks)),
                                  "filename": plan["track_titles"][str(number)], "elapsed": format_duration(time.monotonic()-started)}
            metadata = encoding.inspect(path)
            streams = metadata.get("streams", [])
            if len(streams) != 1 or streams[0].get("codec_name") != "flac" or float(metadata.get("format", {}).get("duration", 0)) <= 0:
                raise ValueError("A selected track did not pass its FLAC file check.")
            movie_output.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path),
                              "-map", "0:a:0", "-f", "null", "-"], cancelled, update, timeout=120)
        with lock:
            job.update(running=False, pid=None, status="Music tracks completed and verified; staged FLAC files retained",
                       audio_report={"tracks": len(tracks), "bytes": sum(p.stat().st_size for p in tracks.values()),
                                     "validation_seconds": time.monotonic()-started, "format": "Lossless FLAC"})
            job["progress"].update(percent=100, written=str(len(tracks)) + " of " + str(len(tracks)))
        persist_state_now(force=True)
        if plan["destination"] == "music":
            with app.test_request_context("/import/music", method="POST", data={"scan_id": plan["scan_id"]}):
                import_music()
            if not job.get("running"):
                finish_workflow_entry(job, "failed", job.get("status", "Music import could not start."))
        else:
            finish_workflow_entry(job, "completed", "Selected lossless FLAC tracks verified in staging.")
    except (OSError, ValueError, InterruptedError, subprocess.TimeoutExpired) as exc:
        with lock:
            job.update(running=False, pid=None, status="Music processing stopped: " + str(exc))
        finish_workflow_entry(job, "cancelled" if cancelled() else "failed", job["status"])
        persist_state_now(force=True)


def wizard_plans(disc, fields):
    if disc.get("kind") == "audio":
        return [{"title_id": None, "title": {}, "plan": wizard_audio_plan(disc, fields)}]
    selected = workflow_wizard.titles_for(disc, fields)
    plans = []
    names = set()
    for title in selected:
        form = MultiDict((key, value) for key, value in fields.items() if key != "title_ids" and isinstance(value, str))
        form.setlist("title_ids", [str(title["id"])])
        name = fields.get("movie_name_" + str(title["id"]), fields.get("workflow_title", ""))
        year = fields.get("movie_year_" + str(title["id"]), fields.get("workflow_year", ""))
        episode = None
        if fields.get("content_kind") == "tv":
            identity = disc.get("identity") or {}
            mapping = identity.get("mappings", {}).get(str(title["id"]), {})
            key = str(title["id"])
            series = safe_media_name(fields.get("series_name") or identity.get("series", ""))
            season = fields.get("identity_season", str(identity.get("season", 1)))
            number = fields.get("episode_number_" + key, str(mapping.get("number", "")))
            episode_name = fields.get("episode_name_" + key, mapping.get("name", ""))
            unchanged = (series == safe_media_name(identity.get("series", "")) and str(season) == str(mapping.get("season"))
                         and str(number) == str(mapping.get("number")) and episode_name == mapping.get("name", ""))
            confirmed = mapping.get("state") == "user_confirmed" and fields.get("confirm_episode_" + key, "yes") == "yes" and unchanged
            if confirmed:
                episode = {"series": series, "season": int(season), "number": int(number), "name": safe_media_name(episode_name),
                           "evidence": copy.deepcopy(mapping)}
                name = disc_intelligence.episode_filename(series, episode["season"], episode["number"], episode["name"])[:-4]
            else:
                if fields.get("workflow_destination") == "jellyfin":
                    raise ValueError("The episode names are saved as suggestions. Confirm the selected episode assignments below, or choose local staging to keep them unassigned.")
                name = (series or "Unidentified series") + " - Disc " + str(identity.get("disc_number", 1)) + " - Title " + key
        form["workflow_title"], form["workflow_year"] = name, year
        plan, error = read_movie_workflow(form)
        if plan is not None and episode:
            plan["episode"] = episode
            plan["output_filename"] = disc_intelligence.episode_filename(episode["series"], episode["season"], episode["number"], episode["name"],
                ".mp4" if plan.get("output_policy", {}).get("container") == "mp4" else ".mkv")
        if error:
            raise ValueError(error)
        if not plan["title"]:
            raise ValueError("Give each selected title a movie name.")
        if plan["title"].casefold() in names:
            raise ValueError("Assign a different episode to each selected title." if fields.get("content_kind") == "tv" else "Use a different movie name for each selected title.")
        names.add(plan["title"].casefold())
        policy = plan.get("output_policy")
        if policy and not policy.get("scan_audio") and not policy.get("audio_track"):
            choices = movie_output.scan_tracks(disc.get("raw", "")).get(title["id"], [])
            if choices:
                try:
                    selected = movie_output.resolve(workflow_wizard.inventory_document(choices), policy)[0]
                    policy["scan_audio"] = copy.deepcopy(choices[selected["index"]]["selector"])
                    policy["audio_selection"] = "automatic"
                except ValueError:
                    pass  # Test Plan reports an unavailable or incompatible match.
        plans.append({"title_id": title["id"], "title": title, "plan": plan})
    return plans


def wizard_preflight(disc, fields, expected_disc):
    checks = []
    def add(name, status, message, step=3):
        checks.append({"name": name, "status": status, "message": message, "step": step})
    if not disc or workflow_wizard.disc_id(disc) != expected_disc:
        add("Disc and titles", "blocked", "The disc scan changed. Scan again before starting.", 1)
        return checks
    with lock:
        stale_scan = state.get("scan_requires_refresh", False)
    if stale_scan:
        add("Disc scan", "blocked", "These scan results came from a previous session. Rescan the disc before starting.", 1)
        return checks
    if disc.get("kind") == "audio":
        return wizard_audio_preflight(disc, fields, expected_disc)
    try:
        plans = wizard_plans(disc, fields)
    except ValueError as exc:
        add("Movie choices", "blocked", str(exc), 2 if not fields.get("title_ids") else 3)
        return checks
    with lock:
        busy = bool(state["job"].get("running")) or any(
            item.get("status") in ("starting", "running") for item in state.get("workflow_queue", []))
    if busy:
        add("Current job", "blocked", "Wait for the current job to finish before starting another workflow.", 1)
    else:
        add("Disc and titles", "passed", str(len(plans)) + " selected title(s) match the current scan.", 2)
    drive = Path(configured_drive())
    if not drive.exists() or not os.access(drive, os.R_OK):
        add("Disc drive", "blocked", "The optical drive is unavailable. Reconnect it and scan again.", 1)
    elif optical_drive_status(str(drive)) != CDS_DISC_OK:
        add("Disc drive", "blocked", "The drive is not ready with a disc inserted. Check the tray and scan again.", 1)
    else:
        add("Disc drive", "passed", "The optical drive reports a disc ready for reading.", 1)
    inventory = movie_output.scan_tracks(disc.get("raw", ""))
    total_bytes = 0
    needs_encode = any(item["plan"]["profile"] != "original" for item in plans)
    hints = scan_video_hints(disc.get("raw", ""))
    for item in plans:
        policy = item["plan"].get("output_policy") or {}
        hint = hints.get(item["title_id"], "")
        if item["plan"]["profile"] == "original" and policy.get("container") == "mp4":
            if hint and not re.search(r"AVC|HEVC|H[. ]?26[45]", hint, re.I):
                add("Video container · " + item["plan"]["title"], "blocked",
                    "This original video format needs MKV. Choose compression to create an MP4 playback copy.")
            elif not hint:
                add("Video container", "pending", "The extracted video format must be checked before MP4 packaging.")
        status, message = workflow_wizard.track_check(inventory.get(item["title_id"], []), item["plan"])
        add("Sound and subtitles · " + item["plan"]["title"], status, message)
        total_bytes += parse_size_bytes(item["title"].get("size")) or 0
    runtime = encoding.cached_probe(os.environ.get("HANDBRAKE_CLI", "HandBrakeCLI")) if needs_encode else {}
    gpu = encoding.nvenc_probe() if needs_encode and fields.get("workflow_engine") in ("auto", "gpu") else {}
    for codec in set(item["plan"]["codec"] for item in plans if item["plan"]["profile"] != "original"):
        engine = fields.get("workflow_engine", "auto")
        cpu_ready = runtime.get("available") and encoding.ENCODERS[codec][0] in runtime.get("encoders", [])
        gpu_ready = gpu.get(codec, {}).get("available")
        available = gpu_ready if engine == "gpu" else cpu_ready if engine == "cpu" else gpu_ready or cpu_ready
        detail = ("NVIDIA sample encode passed" if gpu_ready and engine != "cpu" else "CPU encoder is installed") if available else "The requested encoder is unavailable. Choose another device in Advanced encoding controls."
        add("Encoding device", "passed" if available else "blocked", detail)
    if not needs_encode:
        add("Video processing", "passed", "The selected video will be copied without new compression.")
    try:
        with tempfile.TemporaryFile(dir=VIDEO_OUTPUT) as handle:
            handle.write(b"Dexter plan check")
            handle.flush()
        free = shutil.disk_usage(VIDEO_OUTPUT).free
        required = total_bytes * (3 if needs_encode else 2) + 1024**3
        if total_bytes and free < required:
            add("Working storage", "blocked", "Free " + format_size(required) + " for the source and processing workspace.")
        else:
            add("Working storage", "passed", "Writable · " + format_size(free) + " available.")
            add("Storage estimate", "warning" if not total_bytes else "passed",
                "Source-size reservation " + format_size(required) + ". This is workspace, not a predicted final file size.")
    except OSError:
        add("Working storage", "blocked", "Dexter cannot write to staging. Check its mount and folder permissions.")
    if any(item["plan"]["destination"] == "jellyfin" for item in plans):
        for item in plans:
            if item["plan"]["destination"] != "jellyfin":
                continue
            try:
                movie_destination_preflight(VIDEO_OUTPUT / ".planned-movie.mp4", item["plan"], total_bytes)
                add("Library storage · " + item["plan"]["title"], "passed", "The Movies share is writable and the movie name is available.")
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                add("Library storage", "blocked", str(exc))
        try:
            key = JELLYFIN_KEY_FILE.read_text().strip() if JELLYFIN_KEY_FILE.exists() else ""
            connection = library_connection.inspect(configured_jellyfin_url(), key)
            add("Jellyfin connection", "passed" if connection["ok"] else "blocked", connection["message"])
        except OSError:
            add("Jellyfin connection", "blocked", "Dexter cannot read the saved connection settings. Check Jellyfin in Settings.")
    else:
        add("Destination", "passed", "The verified playback copy will stay in local staging.")
    add("Extracted media", "pending", "Decoding, exact stream properties and final output timing are checked after extraction.")
    add("Output size and time", "pending", "Final size and processing time are measured during the job.")
    return checks


def wizard_render(draft=None, *, queue_view=False):
    with lock:
        disc = copy.deepcopy(state.get("disc"))
        job = copy.deepcopy(state["job"])
        queue = copy.deepcopy(state.get("workflow_queue", []))
        stale_scan = state.get("scan_requires_refresh", False)
    disc = disc or {}
    fields = (draft or {}).get("fields", {})
    identity = disc.get("identity") or disc_intelligence.base_identity(disc)
    inventories = movie_output.scan_tracks(disc.get("raw", ""))
    hints = scan_video_hints(disc.get("raw", ""))
    titles = copy.deepcopy(disc.get("titles", []))
    for title in titles:
        title["track_choices"] = inventories.get(title["id"], [])
        title["scan_video_hint"] = hints.get(title["id"], "")
    feature_titles = [t for t in titles if t.get("category") != "Extra"]
    main_title = next((t for t in titles if t.get("recommendation") == "MAIN FEATURE CANDIDATE"), None)
    if main_title and fields.get("content_kind") != "tv":
        feature_titles.sort(key=lambda t: t["id"] != main_title["id"])
    selected = [t for t in titles if str(t["id"]) in fields.get("title_ids", [])]
    language_choices = {}
    for kind in ("audio", "subtitle"):
        codes = {choice["selector"]["language"] for title in selected for choice in title["track_choices"] if choice["kind"] == kind}
        if not codes:
            codes = {fields.get("workflow_language" if kind == "audio" else "workflow_subtitle_language", "eng")}
        language_choices[kind] = [(code, workflow_wizard.LANGUAGES.get(code, code)) for code in sorted(codes)]
    summaries = []
    try:
        plans = wizard_plans(disc, fields)
    except ValueError:
        plans = []
    for item in plans:
        plan = item["plan"]
        if disc.get("kind") == "audio":
            summaries.append({"name": plan["album"], "artist": plan["artist"], "year": plan["year"],
                              "tracks": len(plan["tracks"]), "destination": "Local staging" if plan["destination"] == "staging" else "Configured music library"})
            continue
        _, sound = workflow_wizard.track_check(inventories.get(item["title_id"], []), plan)
        summaries.append({
            "name": plan["title"] + (" (" + plan["year"] + ")" if plan["year"] else ""),
            "duration": item["title"].get("duration", "Unknown"),
            "picture": "Keep original video" if plan["profile"] == "original" else ("HEVC" if plan["codec"] == "h265" else "H.264") + " · " + ("High quality" if plan["profile"] == "balanced" else "Compact"),
            "sound": sound.replace(" · no subtitles", "").replace(" · selected subtitles", ""),
            "subtitles": "Selected track" if plan.get("output_policy", {}).get("subtitles") else "None" if plan.get("output_policy") else "All original tracks",
            "container": plan.get("output_policy", {}).get("container", "mkv").upper(),
            "destination": "Jellyfin TV" if plan.get("episode") and plan["destination"] == "jellyfin" else MOVIE_DESTINATIONS[plan["destination"]],
            "engine": {"auto": "Automatic · NVIDIA when available, CPU fallback allowed",
                       "gpu": "NVIDIA", "cpu": "CPU / HandBrake"}[plan["engine"]],
        })
    resumable_entry = next((entry["id"] for entry in queue if staged_workflow_source(job, entry)), None) if queue_view else None
    template = Path(workflow_wizard.__file__).with_name("wizard.html").read_text()
    checks = (draft or {}).get("checks", [])
    return render_template_string(template, base_style=PAGE[PAGE.index("<style>"):PAGE.index("</style>")+8],
        disc=disc, disc_name=(disc.get("album") or "Audio CD") if disc.get("kind") == "audio" else suggested_movie_title(disc), drive_name=configured_drive_name(),
        running=job.get("running") or any(e["status"] in ("saving", "queued", "starting", "running") for e in queue),
        scan_running=job.get("running") and job_operation_kind(job) == "scan",
        queue_active=any(e.get("status") in ("saving", "queued", "starting", "running") for e in queue),
        job=job, stale_scan=stale_scan, staging_has_media=staging_has_media(),
        storage=staging_storage(), step=(draft or {}).get("step", 1),
        draft=draft, fields=fields, checks=checks, errors=(draft or {}).get("errors", []),
        blocked=any(c["status"] == "blocked" for c in checks),
        main_title=main_title, main_id=main_title["id"] if main_title else None,
        identity=identity, metadata_offline=(CONFIG_ROOT / "disc-metadata-offline").exists(),
        feature_titles=feature_titles, extra_titles=[t for t in titles if t.get("category") == "Extra"],
        selected_titles=selected, audio_tracks=disc.get("tracks", []), music_library_available=bool(os.environ.get("DEXTER_JELLYFIN_EXPORT")) or MUSIC_ROOT.is_dir(),
        audio_languages=language_choices["audio"], subtitle_languages=language_choices["subtitle"],
        import_available=bool(os.environ.get("DEXTER_JELLYFIN_EXPORT")), summaries=summaries,
        queue_view=queue_view, queue=queue,
        queue_labels={entry["id"]: workflow_presentation.label(entry) for entry in queue}, resumable_entry=resumable_entry, queue_count=sum(i["status"] in ("queued", "starting", "running") for i in queue))


@app.route("/wizard", methods=["GET", "POST"])
def movie_wizard():
    with lock:
        busy = state["job"].get("running")
        scanning = busy and job_operation_kind(state["job"]) == "scan"
        disc = copy.deepcopy(state.get("disc"))
        draft = state.get("wizard_draft")
        if not busy and disc and disc.get("kind") in ("video", "audio"):
            batch_finished = draft and draft.get("submitted") and not any(
                item["status"] in ("saving", "queued", "starting", "running")
                for item in state.get("workflow_queue", []))
            if not draft or draft.get("disc_id") != workflow_wizard.disc_id(disc) or (batch_finished and request.method == "GET"):
                draft = workflow_wizard.new_draft(disc, suggested_movie_title(disc), settings)
                state["wizard_draft"] = draft
            draft = copy.deepcopy(draft)
        else:
            draft = None
    if busy and not scanning:
        return redirect(url_for("home"))
    if draft is None:
        if request.method == "POST":
            return "Finish scanning a supported disc before preparing it.", 409
        return wizard_render()
    if request.method == "GET":
        if draft.get("submitted"):
            return redirect(url_for("home"))
        return wizard_render(draft)
    if request.form.get("draft_id") != draft["id"] or request.form.get("revision") != str(draft["revision"]):
        return "This preparation page changed. Reload it to keep the latest choices.", 409
    if draft.get("submitted"):
        return redirect(url_for("home"))
    fields = workflow_wizard.merge(draft, request.form)
    changed = workflow_wizard.fingerprint(fields) != workflow_wizard.fingerprint(draft["fields"])
    draft["fields"], draft["errors"] = fields, []
    if changed:
        draft["checks"] = []
    action = request.form.get("action", "")
    current = draft["step"]
    if action in ("identify", "save-mappings", "confirm-mappings", "metadata-offline", "metadata-online"):
        if disc.get("kind") != "video":
            return "Episode identification is available for video discs.", 400
        try:
            identity = disc.get("identity") or disc_intelligence.base_identity(disc)
            if action in ("metadata-offline", "metadata-online"):
                flag = CONFIG_ROOT / "disc-metadata-offline"
                if action == "metadata-offline":
                    flag.touch(mode=0o600)
                else:
                    flag.unlink(missing_ok=True)
            elif action == "identify":
                # Reserve the optical/app operation slot while the optional
                # bounded metadata lookup runs. No additional disc reads here.
                with lock:
                    if state["job"].get("running"):
                        return "Wait for the current operation to finish.", 409
                    state["job"]["running"] = True
                try:
                    identity = disc_intelligence.enrich(disc, CONFIG_ROOT,
                        query=fields.get("series_name", ""), show_id=fields.get("metadata_show_id"),
                        order=fields.get("metadata_order", "aired"), season=int(fields.get("identity_season", 1)),
                        disc_number=int(fields.get("identity_disc", 1)),
                        online=not (CONFIG_ROOT / "disc-metadata-offline").exists())
                finally:
                    with lock:
                        state["job"]["running"] = False
                # A different series/order must not retain old form assignments.
                for key in list(fields):
                    if re.fullmatch(r"(episode_number|episode_name|confirm_episode)_\d+", key):
                        fields.pop(key, None)
            else:
                assignment_form = request.form
                if action == "confirm-mappings":
                    if fields.get("content_kind") != "tv":
                        raise ValueError("Choose TV episodes before confirming episode assignments.")
                    selected_titles = workflow_wizard.titles_for(disc, fields)
                    assignment_form = MultiDict((key, value) for key, value in fields.items() if isinstance(value, str))
                    # Confirm only the explicitly selected titles. Omitted titles
                    # retain their previous confirmation state.
                    for title in identity["titles"]:
                        key = str(title["id"])
                        mapping = identity.get("mappings", {}).get(key, {})
                        assignment_form["confirm_episode_" + key] = "yes" if mapping.get("state") == "user_confirmed" else ""
                    for title in selected_titles:
                        key = str(title["id"])
                        if not str(fields.get("episode_number_" + key, "")).strip():
                            raise ValueError("Assign an episode number to each selected title in Select Titles before confirming.")
                        assignment_form["confirm_episode_" + key] = "yes"
                    if (fields.get("series_name", identity["series"]) != identity["series"] or
                            str(fields.get("identity_season", identity["season"])) != str(identity["season"])):
                        raise ValueError("Save the new series and season in Select Titles first, then confirm the assignments.")
                identity = disc_intelligence.correct(identity, assignment_form)
                disc_intelligence.MappingStore(CONFIG_ROOT).save(identity)
            with lock:
                if workflow_wizard.disc_id(state.get("disc") or {}) != draft["disc_id"]:
                    return "The disc scan changed. Review the current scan.", 409
                state["disc"]["identity"] = identity
            fields.update(series_name=identity.get("series", ""), identity_season=str(identity.get("season", 1)),
                          identity_disc=str(identity.get("disc_number", 1)))
            for key, mapping in identity.get("mappings", {}).items():
                fields["episode_number_" + key] = str(mapping.get("number") or "")
                fields["episode_name_" + key] = mapping.get("name", "")
                fields["confirm_episode_" + key] = "yes" if mapping.get("state") == "user_confirmed" else ""
            draft["checks"] = []
        except (OSError, ValueError, TypeError) as exc:
            draft["errors"] = [str(exc)]
    elif action == "back":
        draft["step"] = max(1, current - 1)
    elif action.startswith("step:"):
        try:
            draft["step"] = max(1, min(current, int(action[5:])))
        except ValueError:
            return "Choose a valid step", 400
    elif action == "next":
        try:
            if current == 2 and disc.get("kind") == "video" and fields.get("content_kind") == "tv" and "mapping_present" in request.form:
                identity = disc_intelligence.correct(disc.get("identity") or disc_intelligence.base_identity(disc), request.form)
                disc_intelligence.MappingStore(CONFIG_ROOT).save(identity)
                disc["identity"] = identity
                with lock:
                    state["disc"]["identity"] = identity
            if current >= 2:
                if disc.get("kind") == "audio":
                    selected_audio_tracks(disc, fields.get("tracks", []))
                else:
                    workflow_wizard.titles_for(disc, fields)
            if current >= 3:
                wizard_plans(disc, fields)
            draft["step"] = min(4, current + 1)
        except ValueError as exc:
            draft["errors"] = [str(exc)]
    elif action in ("test", "start") and current == 4:
        checks = wizard_preflight(disc, fields, draft["disc_id"])
        draft["checks"] = checks
        if action == "start" and not any(check["status"] == "blocked" for check in checks):
            plans = wizard_plans(disc, fields)
            batch_id = uuid.uuid4().hex
            now = time.time()
            entries = []
            for item in plans:
                entry_id = uuid.uuid4().hex
                plan = item["plan"]
                plan["queue_id"] = entry_id
                entries.append({"id": entry_id, "batch_id": batch_id, "disc_id": draft["disc_id"],
                                "title_id": item["title_id"], "media_kind": disc["kind"], "plan": plan, "status": "saving",
                                "created_at": now, "fields": copy.deepcopy(fields)})
            with lock:
                live = state.get("wizard_draft", {})
                if live.get("revision") != draft["revision"] or live.get("submitted") or state["job"].get("running"):
                    return redirect(url_for("home"))
                if any(e["status"] in ("saving", "queued", "starting", "running") for e in state.get("workflow_queue", [])):
                    return "Another workflow is already queued. Open Queue to review it.", 409
                state.setdefault("workflow_queue", []).extend(entries)
                draft["submitted"] = batch_id
                state["wizard_draft"] = draft
            try:
                persist_state_now(force=True)
            except OSError:
                with lock:
                    state["workflow_queue"] = [item for item in state["workflow_queue"] if item["batch_id"] != batch_id]
                    state["wizard_draft"].pop("submitted", None)
                return "Dexter could not save this workflow. Check configuration storage and try again.", 503
            with lock:
                for entry in state["workflow_queue"]:
                    if entry["batch_id"] == batch_id and entry["status"] == "saving":
                        entry["status"] = "queued"
            persist_state_now(force=True)
            dispatch_workflow_queue()
            return redirect(url_for("home"))
    else:
        return "Choose a valid preparation action", 400
    draft["revision"] += 1
    with lock:
        live = state.get("wizard_draft", {})
        if live.get("revision") != draft["revision"] - 1 or live.get("submitted"):
            return "This preparation page changed. Reload it to keep the latest choices.", 409
        state["wizard_draft"] = draft
    persist_state_now(force=True)
    return redirect(url_for("movie_wizard"))


@app.get("/workflow-queue")
def workflow_queue_page():
    return wizard_render(queue_view=True)


def finish_workflow_entry(job, status, detail):
    queue_id = job.get("queue_id")
    if not queue_id:
        return
    with lock:
        entry = next((item for item in state.get("workflow_queue", []) if item["id"] == queue_id), None)
        if not entry or entry["status"] not in ("starting", "running"):
            return
        entry.update(status=status, detail=detail, finished_at=time.time())
        if status != "completed":
            for sibling in state["workflow_queue"]:
                if sibling["batch_id"] == entry["batch_id"] and sibling["status"] == "queued":
                    sibling.update(status="paused", detail="An earlier title needs attention. Check this plan before retrying.")
    persist_state_now(force=True)


def dispatch_workflow_queue():
    with lock:
        queue = state.get("workflow_queue", [])
        if state["job"].get("running") or any(i["status"] in ("starting", "running") for i in queue):
            return
        entry = next((item for item in queue if item["status"] == "queued"), None)
        if entry is None:
            return
        if not state.get("disc") or workflow_wizard.disc_id(state["disc"]) != entry["disc_id"]:
            entry.update(status="needs_review", detail="The disc scan changed. Scan and check the plan again.")
            return
        entry["status"] = "starting"
        item = copy.deepcopy(entry)
    persist_state_now(force=True)
    try:
        with app.test_request_context("/wizard/start"):
            if item.get("media_kind") == "audio":
                launch_audio_workflow(item["plan"])
            else:
                start_video_rip([item["title_id"]], movie_workflow=item["plan"])
        failure = None
        with lock:
            job = state["job"]
            live = next(i for i in state["workflow_queue"] if i["id"] == item["id"])
            if live["status"] == "starting":
                if job.get("queue_id") == item["id"]:
                    live["status"] = "running"
                else:
                    failure = job.get("status") or "The job could not start."
        if failure:
            finish_workflow_entry({"queue_id": item["id"]}, "failed", failure)
    except Exception as exc:
        finish_workflow_entry({"queue_id": item["id"]}, "failed", str(exc))
    persist_state_now(force=True)


def workflow_queue_worker():
    while True:
        try:
            dispatch_workflow_queue()
        except Exception as exc:
            print("Dexter queue dispatch failed: " + str(exc), flush=True)
        time.sleep(1)


def staged_workflow_source(job, item):
    if (not item or item.get("media_kind") != "video" or item.get("status") not in ("failed", "needs_review")
            or job.get("running") or job.get("queue_id") != item.get("id")
            or job.get("description") != "Encode video" or not (job.get("status") or "").startswith(("Encoding failed", "Encoding interrupted"))):
        return None
    outputs = _job_outputs(job)
    if len(outputs) != 1:
        return None
    output = outputs[0]
    source = Path(output.get("path") or "")
    if (not (output.get("verification") or {}).get("ok") or not _allowed_staged_video(str(source))
            or source.is_symlink() or not source.is_file() or output.get("episode") != item.get("plan", {}).get("episode")):
        return None
    return source


@app.post("/workflow-queue/<entry_id>/retry")
def retry_workflow_entry(entry_id):
    with lock:
        item = next((copy.deepcopy(i) for i in state.get("workflow_queue", []) if i["id"] == entry_id), None)
        disc = copy.deepcopy(state.get("disc"))
    if not item or item["status"] not in ("failed", "cancelled", "paused", "needs_review"):
        return "This job is not available to retry", 409
    with lock:
        job = state["job"]
        source = staged_workflow_source(job, item)
        if source:
            live = next(i for i in state["workflow_queue"] if i["id"] == entry_id)
            if live["status"] != item["status"]:
                return "This job changed. Reload Queue.", 409
            live.update(status="starting", detail="Resuming the verified staged original", retried_at=time.time())
            job["cancel_requested"] = False
    if source:
        persist_state_now(force=True)
        continue_movie_workflow(job, str(source), copy.deepcopy(item["plan"]), after_encode=False)
        with lock:
            if live["status"] == "starting" and job.get("running"):
                live["status"] = "running"
        persist_state_now(force=True)
        return redirect(url_for("home"))
    if not disc or workflow_wizard.disc_id(disc) != item["disc_id"] or (
            item.get("scan_after") and disc.get("scanned_at", 0) <= item["scan_after"]):
        return "Scan the expected disc before retrying this job.", 409
    fields = copy.deepcopy(item["fields"])
    if item.get("media_kind") == "audio":
        fields["tracks"] = [str(number) for number in item["plan"]["tracks"]]
    else:
        fields["title_ids"] = [str(item["title_id"])]
        fields["movie_name_" + str(item["title_id"])] = item["plan"]["title"]
    checks = wizard_preflight(disc, fields, item["disc_id"])
    if any(check["status"] == "blocked" for check in checks):
        draft = workflow_wizard.new_draft(disc, suggested_movie_title(disc), settings)
        draft.update(fields=fields, step=4, checks=checks)
        with lock:
            state["wizard_draft"] = draft
        persist_state_now(force=True)
        return redirect(url_for("movie_wizard"))
    with lock:
        live = next(i for i in state["workflow_queue"] if i["id"] == entry_id)
        if live["status"] != item["status"] or state["job"].get("running"):
            return "Another job is running", 409
        if item.get("media_kind") == "audio":
            live["plan"].update(scan_id=disc["scan_id"], discid=disc["discid"])
        live.update(status="queued", detail="", retried_at=time.time())
    persist_state_now(force=True)
    dispatch_workflow_queue()
    return redirect(url_for("home"))


@app.get("/credits")
@app.get("/data-sources")
def data_sources():
    template = Path(credits.__file__).with_name("credits.html").read_text()
    return render_template_string(template, sections=credits.SECTIONS)


@app.get("/assets/dexter.css")
def interface_stylesheet():
    path = Path(workflow_wizard.__file__).with_name("static") / "dexter.css"
    return app.response_class(path.read_text(), mimetype="text/css")


@app.get("/movie-history")
def movie_history_page():
    with lock:
        records = copy.deepcopy(state.get("movie_history", []))
    status = request.args.get("status", "all")
    if status not in ("all", "completed", "attention"):
        status = "all"
    try:
        limit = max(3, min(100, int(request.args.get("limit", "3"))))
    except (TypeError, ValueError):
        limit = 3
    records.reverse()
    completed = lambda record: record.get("success") is not False and bool(record.get("output_bytes"))
    counts = {"all": len(records), "completed": sum(completed(record) for record in records)}
    counts["attention"] = counts["all"] - counts["completed"]
    if status != "all":
        records = [record for record in records if completed(record) == (status == "completed")]
    total = len(records)
    visible = records[:limit]
    return render_template_string(
        """<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Magic Media PAL · History</title>{{ style | safe }}</head><body class="wizard-page">
        <header><div class="header-inner"><h1>Processing history</h1><a class="button" href="/">Back to Dexter</a></div></header>
        <main style="max-width:1000px;margin:24px auto;padding:16px">
        <section class="card"><p>The latest recorded video jobs. Choose a view or show older jobs below.</p>
        <nav aria-label="Filter job history">
        {% for key, label in [("all", "All"), ("completed", "Completed"), ("attention", "Needs attention")] %}
        <a class="button" href="{{ url_for('movie_history_page', status=key) }}" {% if status == key %}aria-current="page"{% endif %}>{{ label }} · {{ counts[key] }}</a>
        {% endfor %}</nav><p>Showing {{ records | length }} of {{ total }} jobs in this view.</p></section>
        {% for record in records %}
        <section class="card"><h2>{{ record.title }}</h2>
        <p>{{ date(record.completed_at) }} · {{ size(record.original_bytes) }}
        {% if record.output_bytes %} → {{ size(record.output_bytes) }} · {{ record.saved_percent }}% less space
        {% else %} · No finished playback copy{% endif %}</p>
        <p>{{ record.validation or "Encoding and stream timing checks passed" }}</p>
        {% if record.output_bytes %}<p>{{ "Imported to Jellyfin" if record.imported else "Prepared in staging" }}</p>{% endif %}
        {% if record.extraction_seconds is defined %}<div>Read disc · {{ duration(record.extraction_seconds) }} · Source check {{ duration(record.source_check_seconds) }}</div>{% endif %}
        <details><summary>Processing details</summary>
        {% for stage, seconds in record.stage_seconds.items() %}<div>{{ stage | capitalize }} · {{ duration(seconds) }}</div>{% endfor %}
        {% if record.transfer_seconds %}<div>Copy and library refresh · {{ duration(record.transfer_seconds) }}</div>{% endif %}
        <p>Backend {{ record.get("encode_details", {}).get("backend") or record.engine }} · {{ record.quality }}</p>
        <a class="button" href="/movie-report/{{ record.id }}">Download report</a>
        <a class="button" href="/movie-log/{{ record.id }}">Download processing log</a>
        </details></section>
        {% else %}<section class="card"><p>No recorded video jobs in this view. Earlier jobs were not recorded in this history.</p></section>{% endfor %}
        {% if total > records | length %}<a class="button" href="{{ url_for('movie_history_page', status=status, limit=limit+3) }}">More · show older jobs</a>{% endif %}
        {% if limit > 3 %}<a class="button" href="{{ url_for('movie_history_page', status=status) }}">Back to latest 3</a>{% endif %}
        <p class="muted">Up to 100 recorded video jobs are retained. Processing details and downloads remain available for each job.</p>
        </main></body></html>""",
        records=visible, total=total, counts=counts, status=status, limit=limit, style=PAGE[PAGE.index("<style>"):PAGE.index("</style>") + len("</style>")],
        size=format_size, duration=format_duration,
        date=lambda stamp: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp)))


@app.get("/movie-report/<record_id>")
@app.get("/movie-log/<record_id>")
def movie_report_download(record_id):
    if not re.fullmatch(r"[0-9a-f]{32}", record_id):
        return "Report unavailable", 404
    with lock:
        record = next((copy.deepcopy(item) for item in state.get("movie_history", []) if item.get("id") == record_id), None)
    if record is None:
        return "Report unavailable", 404
    if request.path.startswith("/movie-log/"):
        path = CONFIG_ROOT / "movie-jobs" / (record_id + ".log")
        if not path.is_file() or path.is_symlink():
            return "Log unavailable", 404
        payload, content_type, suffix = path.read_text(), "text/plain", ".log"
    else:
        payload, content_type, suffix = json.dumps(record, indent=2), "application/json", ".json"
    return app.response_class(payload, mimetype=content_type,
                              headers={"Content-Disposition": 'attachment; filename="dexter-' + record_id + suffix + '"'})


@app.post("/encode-video")
def encode_video():
    profile = request.form.get("profile", "balanced")
    codec = request.form.get("codec", "h264")
    if profile not in ("balanced", "compact", "custom") or codec not in encoding.ENCODERS:
        return "Unsupported encoding choice", 400
    try:
        quality = float(request.form.get("quality", "")) if profile == "custom" else None
        if quality is not None and not 0 <= quality <= 51:
            raise ValueError()
    except (ValueError, TypeError):
        return "Custom quality must be a number between 0 and 51", 400
    selected = request.form.get("output_file", "")
    result = start_video_encode(selected, profile, codec, quality, engine=request.form.get("engine", "cpu"))
    return result if result else redirect(url_for("home"))


def start_video_encode(selected, profile, codec, quality=None, workflow=None, expected_job=None, engine="cpu"):
    if engine not in ("cpu", "gpu", "auto"):
        return "Choose CPU, NVIDIA or Automatic encoding.", 400
    policy = (workflow or {}).get("output_policy")
    with lock:
        job = state["job"]
        if job.get("running") or (expected_job is not None and job is not expected_job):
            return "Another job is running", 409
        outputs = copy.deepcopy(_job_outputs(job))
        item = next((item for item in outputs if item.get("path") == selected), None)
        if not item or not (item.get("verification") or {}).get("ok") or not _allowed_staged_video(selected):
            return "Choose a verified staged video", 400
        source = Path(selected)
        target = source.with_name(source.stem + "-encoded-" + uuid.uuid4().hex[:8] + ".mkv")
        job.update({"running": True, "description": "Encode video", "stage": "queued",
                    "status": "Encoding queued; original retained", "cancel_requested": False,
                    "encode_readiness": encoding.EncodeReadiness.NEEDS_PROBE.value,
                    "processing_report": None,
                    "pid": None, "log": [], "progress": {"percent": 0},
                    "video_retry_token": None, "import_failed": False, "movie_import": None,
                    "recovery": {"kind": "encode", "source": str(source), "target": str(target), "codec": codec}})
    persist_state_now(force=True)

    def cancelled():
        with lock:
            return bool(state["job"].get("cancel_requested"))

    report_id = uuid.uuid4().hex
    log_path = CONFIG_ROOT / "movie-jobs" / (report_id + ".log")
    stage_clock = {"name": None, "started": time.monotonic()}
    measurements = {}
    encode_details = {}
    package_report = {}
    def update(**fields):
        if "stage" in fields and fields["stage"] != stage_clock["name"]:
            now = time.monotonic()
            if stage_clock["name"]:
                name = stage_clock["name"]
                measurements[name] = measurements.get(name, 0) + now - stage_clock["started"]
            stage_clock.update(name=fields["stage"], started=now)
        if fields.get("log") and log_path.parent.is_dir():
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(fields["log"] + "\n")
        if fields.get("report"):
            package_report.update(fields["report"])
        if fields.get("encoding_backend"):
            encode_details.update(backend=fields["encoding_backend"], command=fields["encoding_command"])
        with lock:
            current = state["job"]
            progress = current.setdefault("progress", {})
            stage_changed = "stage" in fields and fields["stage"] != current.get("stage")
            label_changed = "verification_label" in fields and fields["verification_label"] != progress.get("verification_label")
            if (stage_changed and fields["stage"] != "completed") or label_changed:
                current["progress"] = {"percent":None, "elapsed":"0:00"}
                progress = current["progress"]
            for key in ("verification_label", "filename", "processed_seconds", "media_duration", "heartbeat_at", "activity_at", "inactive_seconds"):
                if key in fields:
                    progress[key] = fields[key]
            if fields.get("written_bytes") is not None:
                progress["written"] = format_size(fields["written_bytes"])
            if fields.get("media_speed") is not None:
                progress["rate"] = str(round(fields["media_speed"],1)) + "× media speed"
            if "eta_seconds" in fields:
                progress["eta"] = format_duration(fields["eta_seconds"]) if fields["eta_seconds"] is not None else None
            if "readiness" in fields:
                current["encode_readiness"] = fields["readiness"]
            if "pid" in fields:
                current["pid"] = fields["pid"]
            if "stage" in fields:
                current["stage"] = fields["stage"]
                current["status"] = {
                    "inspecting": "Inspecting the extracted video",
                    "testing": "Testing a way to encode this video",
                }.get(fields["stage"], fields["stage"].capitalize()) + "; original retained"
                if fields["stage"] in ("inspecting", "verifying", "validating"):
                    current["progress"]["percent"] = None
            if "percent" in fields:
                current["progress"]["percent"] = fields["percent"]
            if "elapsed" in fields:
                current["progress"]["elapsed"] = format_duration(fields["elapsed"])
            if fields.get("log"):
                current["log"] = (current.get("log", []) + fields["log"].splitlines())[-80:]

    def worker():
        encoded_path = None
        encoded_candidate = None
        try:
            log_path.parent.mkdir(exist_ok=True)
            with log_path.open("x", encoding="utf-8") as handle:
                handle.write("Dexter movie processing log\n")
            executable = os.environ.get("HANDBRAKE_CLI", "HandBrakeCLI")
            capabilities = encoding.probe(executable)
            if engine in ("gpu", "auto"):
                capabilities["nvenc"] = encoding.nvenc_probe()
            if policy:
                movie_output.preflight(source, source.parent)
                selected_source = encoding.inspect(source)
                movie_output.resolve(selected_source, policy)
                if workflow.get("destination") == "jellyfin":
                    movie_destination_preflight(source, workflow)
            encoded = encoding.encode_file(source, target, capabilities,
                                           profile=profile, codec=codec, quality=quality, engine=engine,
                                           cancelled=cancelled, update=update, executable=executable)
            if str(encoded) != str(source):
                encoded_candidate = Path(encoded)
            if policy:
                playback_target = source.with_name(source.stem + "-playback-" + uuid.uuid4().hex[:8] + "." + policy["container"])
                rebound_policy = movie_output.rebase_policy(selected_source, encoding.inspect(encoded), policy)
                encoded = movie_output.prepare(encoded, playback_target, rebound_policy, cancelled=cancelled, update=update)
            verification = probe_media(encoded)
            if not verification.get("ok"):
                raise ValueError("Encoded output failed final inspection; original retained")
            update(stage="completed", percent=100)
            with lock:
                updated = [{"path": str(encoded), "verification": verification}
                           if entry.get("path") == selected else entry for entry in outputs]
                _set_job_outputs(state["job"], updated)
                state["job"]["status"] = "Playback copy prepared and verified; original retained" if policy else "Encoding completed and verified; original retained"
                report = dict(package_report)
                report.update(id=report_id, success=True, original_bytes=source.stat().st_size, output_bytes=Path(encoded).stat().st_size,
                              stage_seconds=measurements, engine=engine, codec=codec,
                              quality=profile, source=str(source), output=str(encoded),
                              title=(workflow or {}).get("title") or source.stem, completed_at=time.time())
                report.update((workflow or {}).get("source_metrics", {}))
                report["stage_seconds"].pop("completed", None)
                report["encode_details"] = encode_details
                report["hardware"] = os.environ.get("DEXTER_GPU_MODEL", "Not recorded")
                report["saved_percent"] = round(100 * (1 - report["output_bytes"] / report["original_bytes"]), 1)
                state["job"]["processing_report"] = report
                state.setdefault("movie_history", []).append(copy.deepcopy(report))
                state["movie_history"] = state["movie_history"][-100:]
                encoded_path = str(encoded)
        except InterruptedError:
            with lock:
                state["job"]["status"] = "Encoding cancelled; original retained; retry available"
        except encoding.EncodePathsExhausted as exc:
            with lock:
                state["job"]["encode_readiness"] = encoding.EncodeReadiness.UNSUPPORTED.value
                state["job"]["status"] = "Dexter tried the configured encoding paths. Your original is ready to review. See Technical details."
                state["job"].setdefault("log", []).append(str(exc))
        except Exception as exc:
            try:
                update(log=str(exc))
            except OSError:
                pass
            with lock:
                state["job"]["status"] = f"Encoding failed: {exc}"
        finally:
            retained = None
            if encoded_path is None and encoded_candidate is not None and encoded_candidate.is_file():
                try:
                    check = probe_media(encoded_candidate)
                    if check.get("ok"):
                        retained = {"path": str(encoded_candidate), "verification": check}
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            with lock:
                if retained:
                    _set_job_outputs(state["job"], outputs + [retained])
                    state["job"]["status"] += " The encoded MKV is also ready to review."
                if encoded_path is None:
                    stage = stage_clock["name"]
                    if stage:
                        measurements[stage] = measurements.get(stage, 0) + time.monotonic() - stage_clock["started"]
                    failure_report = {
                        "id": report_id, "success": False, "title": (workflow or {}).get("title") or source.stem,
                        "completed_at": time.time(), "original_bytes": source.stat().st_size if source.is_file() else 0,
                        "output_bytes": 0, "saved_percent": None, "stage_seconds": measurements,
                        "engine": engine, "quality": profile, "codec": codec, "encode_details": encode_details,
                        "validation": state["job"]["status"], "source": str(source),
                    }
                    state.setdefault("movie_history", []).append(failure_report)
                    state["movie_history"] = state["movie_history"][-100:]
                state["job"].update({"running": False, "pid": None, "recovery": None})
            persist_state_now(force=True)
        if workflow and encoded_path:
            continue_movie_workflow(job, encoded_path, workflow, after_encode=True)
        elif workflow:
            finish_workflow_entry(job, "cancelled" if job.get("cancel_requested") else "failed", job.get("status", "Processing stopped."))

    threading.Thread(target=worker, daemon=True, name="dexter-encode").start()
    return None


def continue_movie_workflow(job, source, plan, *, after_encode):
    def pause(message):
        with lock:
            if state["job"] is not job or job.get("running") or job.get("cancel_requested"):
                return
            job["status"] = message
        finish_workflow_entry(job, "failed", message)
        persist_state_now(force=True)

    try:
        with lock:
            if state["job"] is not job or job.get("running") or job.get("cancel_requested"):
                return
            output = next((item for item in _job_outputs(job) if item.get("path") == source), {})
            verification = output.get("verification") or {}
            if plan.get("episode") and output:
                output["episode"] = copy.deepcopy(plan["episode"])
        if not verification.get("ok") or not _allowed_staged_video(source):
            return
        if not after_encode and (plan["profile"] != "original" or plan.get("output_policy")):
            if start_video_encode(source, plan["profile"], plan["codec"], workflow=plan,
                                  expected_job=job, engine=plan.get("engine", "cpu")):
                pause("Workflow paused. Your verified original is ready to review.")
        elif plan["destination"] == "jellyfin":
            if not os.environ.get("DEXTER_JELLYFIN_EXPORT"):
                pause("Workflow paused. Jellyfin storage is unavailable. Your verified copy is ready to review.")
                return
            if not start_nfs_movie_import(Path(source), plan["title"], plan["year"], expected_job=job,
                    **({"episode": plan["episode"]} if plan.get("episode") else {})):
                pause("Workflow paused. Another job is running.")
        else:
            finish_workflow_entry(job, "completed", "Verified playback copy is ready in staging.")
    except Exception as exc:
        with lock:
            if state["job"] is job:
                job.setdefault("log", []).append(str(exc))
        pause("Workflow paused. Your verified copy is ready to review. See Technical details.")


@app.post("/delete-output")
def delete_output():
    requested_file = request.form.get("output_file", "")

    with lock:
        if state["job"].get("running"):
            return redirect(url_for("home"))

        outputs = _job_outputs(state["job"])
        selected = next(
            (item for item in outputs if item.get("path") == requested_file),
            None,
        )

        if selected is None and len(outputs) == 1 and not requested_file:
            selected = outputs[0]

        filename = (selected or {}).get("path")

    if filename:
        path = Path(filename)

        try:
            resolved = path.resolve()
            allowed = {
                VIDEO_OUTPUT.resolve(),
                AUDIO_OUTPUT.resolve()
            }

            if resolved.parent in allowed:
                resolved.unlink(missing_ok=True)

                with lock:
                    _remove_job_output(state["job"], filename)
                    state["job"]["status"] = "Staged output deleted"

        except Exception as exc:
            with lock:
                state["job"]["status"] = f"Delete failed: {exc}"

    return redirect(url_for("home"))


@app.post("/cancel-job")
def cancel_job():
    with lock:
        if not state["job"].get("running"):
            return redirect(url_for("home"))

        pid = state["job"].get("pid")
        state["job"]["cancel_requested"] = True
        state["job"]["status"] = "Cancelling..."

    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            with lock:
                state["job"]["status"] = f"Cancel failed: {exc}"

    return redirect(url_for("home"))


@app.post("/close-tray")
def close_tray():
    with lock:
        if state["job"].get("running") or any(e["status"] in ("saving", "queued", "starting", "running") for e in state.get("workflow_queue", [])):
            return redirect(url_for("home"))

    drive = configured_drive()

    try:
        subprocess.run(
            ["/usr/bin/eject", "-t", drive],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        print(f"Close tray failed: {exc}", flush=True)

    return redirect(url_for("home"))


@app.post("/eject")
def eject():
    with lock:
        if state["job"].get("running") or any(e["status"] in ("saving", "queued", "starting", "running") for e in state.get("workflow_queue", [])):
            return redirect(url_for("home"))

    run_command(["eject", configured_drive()], timeout=15)
    state["disc"] = None
    return redirect(url_for("home"))


if __name__ == "__main__":
    threading.Thread(target=workflow_queue_worker, daemon=True, name="dexter-workflow-queue").start()
    app.run(
        host=os.environ.get("RIPPER_BIND", "0.0.0.0"),
        port=int(os.environ.get("RIPPER_PORT", "8080")),
        debug=False,
        threaded=True
    )
