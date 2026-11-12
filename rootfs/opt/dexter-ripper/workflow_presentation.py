"""Read-only, plain-language views of the persistent workflow queue."""


def label(entry):
    plan = entry.get("plan") or {}
    episode = plan.get("episode") or {}
    if episode:
        return episode.get("name") or "Episode " + str(episode.get("number", ""))
    return plan.get("album") or plan.get("title") or "Selected copy"


def activity(job):
    kind = (job.get("recovery") or {}).get("kind")
    description = job.get("description") or ""
    if kind == "rip" or description.startswith(("MakeMKV title", "Audio CD")):
        return "Copying from disc", 0
    if kind == "import" or description.startswith(("Adding ", "Import Music")):
        return "Adding to Jellyfin", 2
    stage = job.get("stage") or "queued"
    return ({"inspecting": "Checking the original copy", "testing": "Preparing your copy",
             "encoding": "Making a smaller copy", "packaging": "Preparing picture and sound",
             "verifying": "Checking picture and sound", "validating": "Checking playback"}.get(stage, "Getting ready"), 1)


def batch(queue, job, submitted=None):
    anchor = next((entry for entry in queue if entry.get("id") == job.get("queue_id")), None)
    batch_id = anchor.get("batch_id") if anchor else submitted
    if not batch_id:
        return None
    entries = [entry for entry in queue if entry.get("batch_id") == batch_id]
    if not entries:
        return None
    rows, done, current = [], 0, None
    for position, entry in enumerate(entries, 1):
        state = entry.get("status", "queued")
        plan = entry.get("plan") or {}
        destination = plan.get("destination", "staging")
        if state == "completed":
            done += 1
            status = "Added to Jellyfin" if destination in ("jellyfin", "music") else "Ready for review"
        elif state in ("starting", "running"):
            status = activity(job)[0] if job.get("queue_id") == entry.get("id") and job.get("running") else "Getting ready"
        else:
            status = {"failed": "Needs attention", "cancelled": "Cancelled", "paused": "Paused until the issue is resolved",
                      "needs_review": "Needs your review"}.get(state, "Waiting")
        row = {"id": entry.get("id"), "name": label(entry), "status": status, "state": state,
               "position": position, "episode": (plan.get("episode") or {}).get("number"),
               "destination": "Jellyfin" if destination in ("jellyfin", "music") else "Review here"}
        rows.append(row)
        if state in ("starting", "running"):
            current = row
    if current is None:
        current = next((row for row in rows if row["state"] in ("saving", "queued")), None)
    active_index = activity(job)[1] if current and current["id"] == job.get("queue_id") and job.get("running") else 0
    steps = [{"name": name, "state": "done" if done == len(rows) or index < active_index else
              "current" if current and index == active_index else "waiting"}
             for index, name in enumerate(("Copy from disc", "Prepare and check", current["destination"] if current else rows[-1]["destination"]))]
    return {"id": batch_id, "rows": rows, "done": done, "total": len(rows), "current": current,
            "steps": steps, "is_tv": all((entry.get("plan") or {}).get("episode") for entry in entries)}
