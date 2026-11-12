"""Pure draft, scan-inventory and preflight helpers for the movie wizard."""
import hashlib
import json
import re
import uuid

import movie_output

LANGUAGES = {"eng": "English", "deu": "German", "fra": "French", "spa": "Spanish",
             "ita": "Italian", "jpn": "Japanese", "por": "Portuguese", "zho": "Chinese",
             "nld": "Dutch", "rus": "Russian", "und": "Unspecified"}


def disc_id(disc):
    return hashlib.sha256(json.dumps({
        "kind": disc.get("kind"), "discid": disc.get("discid"),
        "tracks": [{k: t.get(k) for k in ("number", "duration")} for t in disc.get("tracks", [])],
        "label": disc.get("label", ""), "type": disc.get("type"), "title": disc.get("title"),
        "inventory": movie_output.scan_tracks(disc.get("raw", "")),
        "titles": [{k: t.get(k) for k in ("id", "source", "duration", "size")}
                   for t in disc.get("titles", [])],
    }, sort_keys=True).encode()).hexdigest()


def new_draft(disc, title, settings):
    identity = disc.get("identity") or {}
    if not identity and disc.get("kind") == "video":
        from disc_intelligence import base_identity
        identity = base_identity(disc)
    return {"id": uuid.uuid4().hex, "disc_id": disc_id(disc), "step": 1, "revision": 0,
            "fields": {"workflow_quality": settings.get("movie_quality", "balanced"),
                       "workflow_codec": "h265", "workflow_engine": "auto",
                       "workflow_container": "mp4", "workflow_language": "eng",
                       "workflow_surround": "yes", "workflow_subtitles": "no",
                       "workflow_subtitle_language": "eng", "workflow_destination": "staging",
                       "workflow_title": title, "workflow_year": "", "title_ids": [],
                       "tracks": [], "album": disc.get("album") or "", "artist": disc.get("album_artist") or "",
                       "album_year": str(disc.get("year") or ""), "audio_destination": "staging",
                       "content_kind": "tv" if identity.get("series") else "movie",
                       "series_name": identity.get("series", ""), "identity_season": str(identity.get("season", 1)),
                       "identity_disc": str(identity.get("disc_number", 1)), "metadata_order": identity.get("order", "aired")},
            "checks": [], "errors": []}


def merge(draft, form):
    result = dict(draft["fields"])
    names = ("workflow_quality", "workflow_codec", "workflow_engine", "workflow_container",
             "workflow_language", "workflow_surround", "workflow_subtitles",
             "workflow_subtitle_language", "workflow_destination", "workflow_title", "workflow_year",
             "album", "artist", "album_year", "audio_destination", "content_kind", "series_name",
             "identity_season", "identity_disc", "metadata_show_id", "metadata_order")
    for name in names:
        if name in form:
            result[name] = form[name]
    if "plan_present" in form:
        result["workflow_surround"] = form.get("workflow_surround", "")
    if "tracks_present" in form:
        result["tracks"] = list(dict.fromkeys(form.getlist("tracks")))
    if "titles_present" in form:
        result["title_ids"] = list(dict.fromkeys(form.getlist("title_ids")))
    if "mapping_present" in form:
        for key in form:
            if re.fullmatch(r"episode_number_\d+", key):
                result["confirm_episode_" + key.rsplit("_", 1)[1]] = form.get("confirm_episode_" + key.rsplit("_", 1)[1], "")
        for key in list(result):
            if key.startswith("confirm_episode_"):
                result[key] = form.get(key, "")
    for key in form:
        if re.fullmatch(r"(scan_audio|scan_subtitle|movie_name|movie_year|track_name|episode_number|episode_name|confirm_episode|title_order)_\d+", key):
            result[key] = form[key]
    return result


def titles_for(disc, fields):
    ids = fields.get("title_ids", [])
    if not ids or any(not str(value).isdigit() for value in ids):
        raise ValueError("Select at least one title.")
    titles = {str(t["id"]): t for t in disc.get("titles", [])}
    if any(str(value) not in titles for value in ids):
        raise ValueError("The selected titles changed. Review the disc scan.")
    selected = [titles[str(value)] for value in ids]
    def order(title):
        raw = str(fields.get("title_order_" + str(title["id"]), ids.index(str(title["id"]))))
        return (int(raw) if raw.isdigit() and len(raw) < 6 else title["id"], title["id"])
    return sorted(selected, key=order)


def inventory_document(choices):
    streams = []
    for index, choice in enumerate(choices):
        selector = choice["selector"]
        streams.append({"index": index, "codec_type": selector["kind"],
                        "codec_name": selector["codec"], "channels": selector["channels"],
                        "tags": {"language": selector["language"], "title": choice.get("title", "")},
                        "disposition": choice.get("disposition", {})})
    return {"streams": streams}


def track_check(choices, plan):
    policy = plan.get("output_policy")
    if not policy:
        return "warning", "The advanced preservation option keeps every original track."
    if not choices:
        return "pending", "Track information needs extraction before it can be verified."
    document = inventory_document(choices)
    try:
        selected = movie_output.resolve(document, policy)
    except ValueError as exc:
        return "blocked", str(exc).replace("from the extracted file", "in Prepare Output")
    audio = selected[0]
    selected_language = movie_output.language((audio.get("tags") or {}).get("language"))
    if selected_language != policy["language"]:
        return "blocked", "The selected sound track uses another language. Change the sound track or your language preference."
    if policy["subtitles"] and movie_output.language((selected[-1].get("tags") or {}).get("language")) != policy["subtitle_language"]:
        return "blocked", "The selected subtitles use another language. Review your subtitle language."
    sound_number = sum(s["codec_type"] == "audio" for s in document["streams"][:audio["index"] + 1])
    text = ("Sound " + str(sound_number) + " · " + LANGUAGES.get(selected_language, selected_language) + " · " +
            {"ac3": "Dolby Digital", "truehd": "Dolby TrueHD"}.get(audio["codec_name"], audio["codec_name"]) +
            " · " + str(audio.get("channels") or "unknown") + " channels")
    return "passed", text + (" · selected subtitles" if policy["subtitles"] else " · no subtitles")


def fingerprint(fields):
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
