"""User-facing decisions derived from verified Dexter capabilities.

Keep PRODUCT_FEATURES.md in sync when integration or verification changes.
Upstream support alone must never enable a Dexter control.
"""

from encoding import EncodeReadiness


TOOLS = (
    {"id": "makemkv", "name": "MakeMKV", "purpose": "Read video discs without re-encoding the picture",
     "features": ("video_scan", "title_selection", "original_video"), "verification": "physical-media checks pending"},
    {"id": "handbrake", "name": "HandBrake", "purpose": "Make optional smaller video copies",
     "features": ("cpu_h264", "cpu_h265", "quality_profiles"), "verification": "generated SDR samples"},
    {"id": "ffmpeg", "name": "FFmpeg and ffprobe", "purpose": "Inspect files and preserve sound, subtitles and chapters",
     "features": ("stream_inspection", "stream_remux", "structural_verification"), "verification": "generated samples"},
    {"id": "cyanrip", "name": "cyanrip", "purpose": "Save selected music tracks as lossless FLAC",
     "features": ("cd_scan", "track_selection", "flac"), "verification": "mocked selection and scan checks"},
)


def compression_choices(verification, runtime):
    """Explain source and runtime limits before a user starts an encode."""
    source = verification.get("encoding_source") or {}
    reason = ""
    readiness = source.get("readiness", EncodeReadiness.NEEDS_PROBE.value)
    if not verification.get("ok"):
        reason = "This file needs to pass its file check first."
    elif source.get("blocked") or source.get("protected") or source.get("hdr") is True or source.get("bit_depth") not in (None, 8):
        readiness = EncodeReadiness.NEEDS_FALLBACK.value
        reason = "Keep the original to preserve this file’s video information. Dexter has no configured preservation path for it."
    elif not runtime.get("available") and not any(check.get("available") for check in runtime.get("nvenc", {}).values()):
        reason = "Compression needs setup on this computer. You can keep the original quality now."
    formats = []
    for codec, encoder, label, description in (
        ("h264", "x264", "Wider playback support (H.264)", "A common choice when your playback devices are not known."),
        ("h265", "x265", "Newer playback format (H.265)", "Can be more efficient, but playback support varies. Check your player before choosing."),
    ):
        enabled = not reason and (encoder in runtime.get("encoders", []) or runtime.get("nvenc", {}).get(codec, {}).get("available", False))
        formats.append({"id": codec, "label": label, "description": description,
                        "available": enabled,
                        "reason": reason or ("This format is unavailable on this computer." if not enabled else "")})
    available = any(item["available"] for item in formats)
    return {"readiness": readiness, "available": available, "reason": reason or ("No supported compression format is ready on this computer." if not available else ""),
            "formats": formats,
            "default_codec": next((item["id"] for item in formats if item["available"]), None)}
