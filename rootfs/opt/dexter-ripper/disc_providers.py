"""Read-only metadata adapters with isolated, attributed provider caches."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Protocol

TVMAZE_LICENSE = "https://creativecommons.org/licenses/by-sa/4.0/"


def text(value, limit=300):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit].strip()


class PhysicalDiscProvider(Protocol):
    def lookup(self, fingerprints): ...


class SeriesMetadataProvider(Protocol):
    def search(self, name): ...
    def episodes(self, show_id, order=None): ...


class FingerprintProvider(Protocol):
    def fingerprint(self, device, disc): ...


class LocalMappingCache(Protocol):
    def load(self, identity): ...
    def save(self, identity): ...


class JsonCache:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def get(self, key):
        try:
            path = self.path(key)
            if path.stat().st_size > 4 * 1024**2:
                return None
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def put(self, key, value):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class NoProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ProviderHTTP:
    gates = {}
    gates_lock = threading.Lock()
    def __init__(self, root, online=True, timeout=4, ttl=86400):
        self.cache = JsonCache(root)
        self.online, self.timeout, self.ttl = online, timeout, ttl
        self.notices = []
        self.deadline = time.monotonic() + 25

    def get(self, url, provider, license_url):
        host = urllib.parse.urlsplit(url).hostname
        if host not in ("api.tvmaze.com", "thediscdb.com"):
            raise ValueError("Unsupported metadata host")
        cached = self.cache.get(url)
        if cached and (not self.online or time.time() - cached.get("fetched_at", 0) < self.ttl):
            return cached["data"]
        if not self.online or time.monotonic() >= self.deadline:
            if cached:
                return cached["data"]
            raise ValueError("Offline or lookup time limit reached — manual choices still work")
        with self.gates_lock:
            gate = self.gates.setdefault(host, {"lock": threading.Lock(), "last": 0, "cooldown": 0})
        try:
            for attempt in range(2):
                with gate["lock"]:
                    if gate.get("cooldown", 0) > time.monotonic():
                        raise ValueError("Provider is cooling down; use saved details or retry later")
                    time.sleep(max(0, .6 - (time.monotonic() - gate["last"])))
                    gate["last"] = time.monotonic()
                    try:
                        request = urllib.request.Request(url, headers={"User-Agent": "DexterRipper/0.1 (disc metadata)", "Accept": "application/json"})
                        with urllib.request.build_opener(NoProviderRedirect).open(request, timeout=self.timeout) as response:
                            if urllib.parse.urlsplit(response.url).hostname != host:
                                raise ValueError("Metadata redirected outside its provider")
                            raw = response.read(4 * 1024**2 + 1)
                            if len(raw) > 4 * 1024**2:
                                raise ValueError("Metadata response is too large")
                            result = json.loads(raw)
                    except urllib.error.HTTPError as exc:
                        if exc.code == 404:
                            result = []
                        elif exc.code in (429, 502, 503, 504) and not attempt:
                            delay = exc.headers.get("Retry-After", "1")
                            if not delay.isdigit() or int(delay) > 2:
                                gate["cooldown"] = time.monotonic() + (int(delay) if delay.isdigit() else 60)
                                raise ValueError("Provider is busy; retry later") from exc
                            time.sleep(max(1, int(delay)))
                            continue
                        else:
                            raise
                try:
                    self.cache.put(url, {"data": result, "provider": provider, "source": url,
                        "license": license_url, "fetched_at": time.time(), "changes": "Original provider response"})
                except OSError:
                    self.notices.append(provider + " result could not be cached")
                return result
        except (OSError, ValueError) as exc:
            if cached:
                self.notices.append(provider + " unavailable; using saved metadata")
                return cached["data"]
            raise ValueError(provider + " is unavailable. Manual choices still work.") from exc


class TVmaze:
    def __init__(self, http):
        self.http = http

    def get(self, path):
        return self.http.get("https://api.tvmaze.com/" + path, "TVmaze", TVMAZE_LICENSE)

    def search(self, name):
        result = self.get("search/shows?" + urllib.parse.urlencode({"q": text(name, 150)}))
        if not isinstance(result, list):
            raise ValueError("TVmaze returned an unexpected search response")
        return [{"id": int(row["show"]["id"]), "name": text(row["show"].get("name")),
                 "premiered": text(row["show"].get("premiered")),
                 "source": "https://www.tvmaze.com/shows/" + str(int(row["show"]["id"])),
                 "image": self.image(row["show"].get("image")), "provider": "TVmaze", "license": TVMAZE_LICENSE}
                for row in result[:10] if isinstance(row, dict) and isinstance(row.get("show"), dict) and str(row["show"].get("id", "")).isdigit()]

    @staticmethod
    def image(image):
        url = (image or {}).get("medium", "") if isinstance(image, dict) else ""
        return url if urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(url).hostname == "static.tvmaze.com" else ""

    def orders(self, show_id):
        result = self.get(f"shows/{int(show_id)}/alternatelists")
        if not isinstance(result, list):
            raise ValueError("TVmaze returned an unexpected order response")
        return [{"id": int(row["id"]), "name": text(row.get("name") or row.get("type") or "Alternate order")}
                for row in result[:30] if isinstance(row, dict) and str(row.get("id", "")).isdigit()]

    def episodes(self, show_id, order=None):
        path = (f"alternatelists/{int(order)}/alternateepisodes?embed=episodes" if order
                else f"shows/{int(show_id)}/episodes?specials=1")
        result = self.get(path)
        if not isinstance(result, list):
            raise ValueError("TVmaze returned an unexpected episode response")
        episodes = []
        for row in result[:2000]:
            if not isinstance(row, dict) or not isinstance(row.get("season"), int) or not isinstance(row.get("number"), int):
                continue
            episodes.append({"id": int(row["id"]), "season": row["season"], "number": row["number"],
                "name": text(row.get("name")), "runtime": row.get("runtime"), "provider": "TVmaze",
                "source": (f"https://api.tvmaze.com/alternatelists/{int(order)}/alternateepisodes?embed=episodes" if order
                           else "https://www.tvmaze.com/episodes/" + str(int(row["id"]))),
                "license": TVMAZE_LICENSE, "order": str(order or "aired"),
                "changes": "Selected fields and normalized plain text"})
        return episodes


class TheDiscDb:
    def __init__(self, http):
        self.http = http

    def lookup(self, fingerprints):
        for fingerprint in fingerprints:
            value = fingerprint.get("value", "")
            if fingerprint.get("type") not in ("libdvdread-discid", "aacs-discid") or not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{40}", value):
                continue
            result = self.http.get("https://thediscdb.com/api/discid/" + value.upper(), "TheDiscDb", "https://github.com/TheDiscDb/data/blob/main/LICENSE")
            if not isinstance(result, list):
                raise ValueError("TheDiscDb returned an unexpected disc response")
            # Never trust the lookup alone; validate the returned exact identifier.
            return [row for row in result[:20] if isinstance(row, dict) and str(row.get("global_disc_id", "")).lower() == value.lower()]
        return []
