"""Disc identity and evidence stay independent of the ripping/encoding pipeline."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import movie_output

from disc_providers import JsonCache, ProviderHTTP, TVmaze, TheDiscDb, text


@dataclass
class DiscIdentity:
    schema: int = 1
    disc_type: str = ""
    volume_identifier: str = ""
    fingerprints: list = field(default_factory=list)
    release_identifiers: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    titles: list = field(default_factory=list)
    mappings: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    confidence: str = "unknown"
    user_corrections: list = field(default_factory=list)
    unassigned_titles: list = field(default_factory=list)
    series: str = ""
    season: int = 1
    disc_number: int = 1
    episode_candidates: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    order: str = "aired"
    provider_status: list = field(default_factory=list)
    structure: str = ""
    updated_at: float = 0


def structure(disc):
    rows = [{k: title.get(k) for k in ("id", "source", "duration", "chapters", "size", "segments")}
            for title in sorted(disc.get("titles", []), key=lambda row: row["id"])]
    return hashlib.sha256(json.dumps({"titles": rows, "tracks": movie_output.scan_tracks(disc.get("raw", ""))}, sort_keys=True).encode()).hexdigest()


def series_hint(disc):
    match = re.match(r"^(.*?)\s*[-_:]?\s*(?:season|s)\s*(\d{1,2})\s*[-_:]?\s*(?:disc|disk|d)\s*(\d{1,2})", disc.get("title", ""), re.I)
    return (text(match[1].rstrip(" -_:")), int(match[2]), int(match[3])) if match else ("", 1, 1)


def base_identity(disc):
    series, season, number = series_hint(disc)
    inventories = movie_output.scan_tracks(disc.get("raw", ""))
    return asdict(DiscIdentity(disc_type=text(disc.get("type")), volume_identifier=text(disc.get("label")),
        titles=[{**{k: t.get(k) for k in ("id", "name", "source", "duration", "duration_seconds", "chapters", "size", "segments")},
                 "tracks": inventories.get(t["id"], [])}
                for t in sorted(disc.get("titles", []), key=lambda row: row["id"])],
        series=series, season=season, disc_number=number, structure=structure(disc), updated_at=time.time()))


class DVDReadFingerprint:
    def fingerprint(self, device, disc):
        if "dvd" not in disc.get("type", "").lower():
            return []
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("disc_fingerprint.py")), device],
                                capture_output=True, text=True, timeout=12)
        if result.returncode:
            raise ValueError("Physical DVD identifier unavailable; manual identification still works")
        records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        if not records or not re.fullmatch(r"[a-f0-9]{32}", records[-1].get("value", "")):
            raise ValueError("DVD identifier response was invalid")
        return [records[-1]]


class MappingStore:
    def __init__(self, root):
        self.cache = JsonCache(Path(root) / "disc-mappings")

    @staticmethod
    def key(identity):
        ids = sorted((item.get("type", ""), item.get("value", "")) for item in identity.get("fingerprints", []))
        return json.dumps(ids) if ids else "scan-only:" + identity["structure"]

    def load(self, identity):
        saved = self.cache.get(self.key(identity))
        if saved and saved.get("schema") == 1 and isinstance(saved.get("mappings"), dict) and saved.get("structure") == identity["structure"]:
            # Structural hints alone never establish a physical-disc identity.
            if not identity.get("fingerprints"):
                return None
            return saved
        return None

    def save(self, identity):
        self.cache.put(self.key(identity), identity)


def episode_filename(series, season, episode, name, suffix=".mkv"):
    def clean(value):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text(value)).strip(" .")[:150]
    series, name = clean(series), clean(name)
    if not series or not 0 <= int(season) <= 999 or not 1 <= int(episode) <= 9999 or suffix not in (".mkv", ".mp4"):
        raise ValueError("Enter a series name, season and episode number")
    return f"{series} - S{int(season):02d}E{int(episode):02d}" + (" - " + name if name else "") + suffix


def enrich(disc, root, *, fingerprints=None, online=True, query=None, show_id=None, order="aired", season=None,
           disc_number=None, metadata_provider=None, physical_provider=None):
    identity = base_identity(disc)
    identity["fingerprints"] = fingerprints if fingerprints is not None else (disc.get("identity") or {}).get("fingerprints", [])
    previous = MappingStore(root).load(identity)
    http = ProviderHTTP(Path(root) / "provider-cache", online=online)
    metadata = metadata_provider or TVmaze(http)
    physical = physical_provider or TheDiscDb(http)
    identity["season"] = int(season if season is not None else identity["season"])
    identity["disc_number"] = int(disc_number if disc_number is not None else identity["disc_number"])
    identity["order"] = str(order or "aired")
    if previous and query is None:
        identity.update(series=previous.get("series", identity["series"]),
            season=previous.get("season", identity["season"]), disc_number=previous.get("disc_number", identity["disc_number"]),
            order=previous.get("order", identity["order"]))
    try:
        releases = physical.lookup(identity["fingerprints"])
        identity["release_identifiers"] = [{"provider": "TheDiscDb", "release": row.get("release"), "disc": row.get("disc"),
            "source": "https://thediscdb.com", "license": "https://github.com/TheDiscDb/data/blob/main/LICENSE"} for row in releases]
        # Exact IDs identify a pressing, but title assignments still need
        # independently supported mapping evidence or explicit user confirmation.
        for release in releases:
            media = release.get("media") or {}
            identity["candidates"].append({"name": text(media.get("title")), "provider": "TheDiscDb", "type": text(media.get("type"))})
            for title in release.get("titles", []):
                item = title.get("item") or {}
                match = next((t for t in identity["titles"] if t["id"] == title.get("index")
                    and t.get("duration", "").lstrip('0:') == str(title.get("duration", "")).lstrip('0:')), None)
                if match and item.get("episode") is not None:
                    identity["mappings"][str(match["id"])] = {"number": item.get("episode"), "season": item.get("season"),
                        "name": text(item.get("title")), "confidence": "probable", "state": "proposed",
                        "evidence": "Exact physical-disc ID and matching title index/duration; provider mapping requires confirmation",
                        "provider": "TheDiscDb", "source": "https://thediscdb.com", "license": "https://github.com/TheDiscDb/data/blob/main/LICENSE"}
        identity["provider_status"].append("TheDiscDb: exact disc match" if releases else "TheDiscDb: no exact disc match")
        if releases:
            identity["confidence"] = "probable"
    except Exception as exc:
        identity["provider_status"].append(text(exc))
    lookup = text(query if query is not None else identity["series"], 150)
    if lookup:
        try:
            candidates = metadata.search(lookup)
            identity["candidates"] += candidates
            exact = [row for row in candidates if row["name"].casefold() == lookup.casefold()]
            chosen = next((row for row in candidates if str(row["id"]) == str(show_id)), None) if show_id else exact[0] if len(exact) == 1 else None
            if chosen:
                identity["series"] = chosen["name"]
                identity["orders"] = metadata.orders(chosen["id"])
                if identity["order"] != "aired" and identity["order"] not in [str(row["id"]) for row in identity["orders"]]:
                    raise ValueError("Choose a listed episode order")
                all_episodes = metadata.episodes(chosen["id"], None if identity["order"] == "aired" else identity["order"])
                episodes = [row for row in all_episodes if row["season"] == identity["season"]]
                identity["episode_candidates"] = episodes
                identity["evidence"].append({"provider": "TVmaze", "source": chosen["source"], "license": chosen["license"],
                    "reason": "Series-name match identifies episode candidates only", "fetched_at": time.time()})
                # Disc 1 may suggest an initial sequence. Never assert sequence
                # equals content or infer later-disc offsets from title count.
                if identity["disc_number"] == 1:
                    for title, episode in zip(identity["titles"], episodes):
                        identity["mappings"].setdefault(str(title["id"]), {**episode, "confidence": "unverified", "state": "proposed",
                            "evidence": "Episode-list order only; DVD title order has not been verified"})
                identity["confidence"] = identity["confidence"] if identity["confidence"] != "unknown" else "unverified"
                identity["provider_status"].append("TVmaze: episode candidates found; title assignments need confirmation")
            else:
                identity["provider_status"].append("Choose a series match or enter details manually")
        except Exception as exc:
            identity["provider_status"].append(text(exc))
    if previous:
        identity["user_corrections"] = previous.get("user_corrections", [])
        for key, mapping in previous.get("mappings", {}).items():
            if ((mapping.get("state") == "user_confirmed" or mapping.get("user_edited")) and previous.get("series") == identity["series"]
                    and previous.get("season") == identity["season"] and previous.get("disc_number") == identity["disc_number"]
                    and previous.get("order", "aired") == identity["order"]):
                identity["mappings"][key] = mapping
        if (previous.get("series") == identity["series"] and previous.get("season") == identity["season"]
                and previous.get("order", "aired") == identity["order"]):
            identity["unassigned_titles"] = previous.get("unassigned_titles", [])
            for key in identity["unassigned_titles"]:
                identity["mappings"].pop(key, None)
    identity["provider_status"] += http.notices
    return identity


def correct(identity, form):
    result = json.loads(json.dumps(identity))
    series = text(form.get("series_name", result["series"]))
    season = int(form.get("identity_season", result["season"]))
    disc_number = int(form.get("identity_disc", result["disc_number"]))
    if not 0 <= season <= 999 or not 1 <= disc_number <= 999:
        raise ValueError("Enter a valid season and disc number")
    result.update(series=series, season=season, disc_number=disc_number)
    for title in result["titles"]:
        key = str(title["id"])
        old = result["mappings"].get(key, {})
        number = str(form.get("episode_number_" + key, old.get("number", ""))).strip()
        if not number:
            result["mappings"].pop(key, None)
            if key not in result.setdefault("unassigned_titles", []):
                result["unassigned_titles"].append(key)
            if old:
                result["user_corrections"].append({"title_id": title["id"], "before": old, "after": None, "at": time.time(), "source": "Local user only"})
            continue
        if key in result.setdefault("unassigned_titles", []):
            result["unassigned_titles"].remove(key)
        if not number.isdigit() or not 1 <= int(number) <= 9999:
            raise ValueError("Enter a valid episode number or leave the title unassigned")
        name = text(form.get("episode_name_" + key, old.get("name", "")))
        confirmed = (form.get("confirm_episode_" + key) == "yes" and series == identity["series"] and season == identity["season"])
        if confirmed and not series:
            raise ValueError("Enter the series name before confirming episode assignments")
        if (str(old.get("number")) == str(number) and old.get("name", "") == name and old.get("season") == season
                and series == identity["series"] and confirmed == (old.get("state") == "user_confirmed")):
            continue
        mapping = {"number": int(number), "season": season, "name": name, "state": "user_confirmed" if confirmed else "proposed",
            "confidence": "verified" if confirmed and result["fingerprints"] else "unverified",
            "evidence": "User checked this title and confirmed its episode" if confirmed else "User-edited proposal; not confirmed",
            "provider": old.get("provider", "User"), "source": old.get("source", ""), "license": old.get("license", "User supplied"),
            "confirmed_at": time.time() if confirmed else None, "user_edited": True}
        result["mappings"][key] = mapping
        if old != mapping:
            result["user_corrections"].append({"title_id": title["id"], "before": old, "after": mapping, "at": time.time(), "source": "Local user only"})
    result["user_corrections"] = result["user_corrections"][-200:]
    result["updated_at"] = time.time()
    return result
