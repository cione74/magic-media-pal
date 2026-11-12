"""Storage choices shared by the host installer and the unprivileged web flow.

The browser records choice IDs only. The host rediscovers mounts and validates
those IDs before creating dedicated folders or changing container wiring.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def atomic_json(path, value):
    path = Path(path)
    if path.parent != path.parent.resolve() or path.is_symlink():
        raise ValueError('Storage setup refuses linked configuration files.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def load(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Storage setup refuses linked configuration files.')
    if not path.exists():
        return None
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Storage information is too large. Refresh it from the installer.')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ValueError('Storage information needs to be refreshed from the installer.')
    if path.name == 'storage-inventory.json':
        if not isinstance(data.get('choices'), list) or not isinstance(data.get('unready'), list) or not isinstance(data.get('checked_at'), int):
            raise ValueError('Storage information needs to be refreshed from the installer.')
        for choice in data['choices']:
            if not isinstance(choice, dict) or not all(k in choice for k in ('id', 'role', 'path', 'current', 'source', 'available', 'system', 'free_gib', 'total_gib')):
                raise ValueError('Storage information needs to be refreshed from the installer.')
    elif path.name == 'storage-request.json':
        if not all(isinstance(data.get(k), str) for k in ('staging', 'library', 'staging_id', 'library_id', 'request_id')):
            raise ValueError('The pending storage choice could not be read. Review it before applying changes.')
    return data


def mount_for(path, mounts):
    matches = [m for m in mounts if Path(m['target']) == path or Path(m['target']) in path.parents]
    return max(matches, key=lambda m: len(m['target'])) if matches else None


def inventory(root, staging, library, mounts, disks):
    root = Path(root).resolve()
    choices = []
    seen = set()

    def add(role, path, current=False):
        path = Path(path)
        mount = mount_for(path, mounts)
        if mount is None or (role, str(path)) in seen:
            return
        seen.add((role, str(path)))
        identity = [role, str(path), mount['source'], mount['target'], mount.get('uuid'), mount['fstype']]
        choice_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        ancestor = path
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        available = ('rw' in mount.get('options', '').split(',')
                     and path == path.resolve() and os.access(ancestor, os.W_OK | os.X_OK))
        choices.append({'id': choice_id, 'role': role, 'path': str(path), 'current': current,
                        'source': mount['source'], 'mount': mount['target'],
                        'system': mount['target'] == '/', 'available': available,
                        'free_gib': round(int(mount.get('avail') or 0) / 1024**3, 1),
                        'total_gib': round(int(mount.get('size') or 0) / 1024**3, 1)})

    add('staging', staging, True)
    add('library', library, True)
    add('staging', root / 'staging')
    add('library', root / 'library')
    for mount in mounts:
        target = Path(mount['target'])
        # Offer data mounts, never system/boot filesystems or optical media.
        if target == Path('/') or target.parts[1:2] not in [('mnt',), ('media',), ('srv',), ('home',)]:
            continue
        if mount['fstype'] in ('iso9660', 'udf', 'squashfs'):
            continue
        add('staging', target / 'dexter-ripper/staging')
        add('library', target / 'dexter-ripper/library')

    unready = []
    def nodes(node):
        yield node
        for child in node.get('children', []):
            yield from nodes(child)
    for disk in disks:
        if disk.get('type') != 'disk':
            continue
        descendants = list(nodes(disk))
        if any(n.get('mountpoints') and any(p and p != '[SWAP]' for p in n['mountpoints']) for n in descendants):
            continue
        has_fs = any(n.get('fstype') for n in descendants)
        stable = next((str(p) for p in sorted(Path('/dev/disk/by-id').glob('*'))
                       if str(p.resolve()) == disk['path']), None)
        unready.append({'name': disk.get('model') or disk['path'], 'device': disk['path'],
                        'serial': str(disk.get('serial') or '').strip(), 'by_id': stable,
                        'size_bytes': int(disk.get('size') or 0),
                        'size_gib': round(int(disk.get('size') or 0) / 1024**3, 1),
                        'reason': 'Not mounted' if has_fs else 'No filesystem detected',
                        'detail': 'An administrator must review this disk before it can be used. No disk preparation is performed here.'})
    return {'schema': 1, 'checked_at': int(time.time()), 'choices': choices, 'unready': unready}


def selected(document, staging_id, library_id):
    result = {}
    for role, requested in (('staging', staging_id), ('library', library_id)):
        choice = next((c for c in document['choices'] if c['id'] == requested and c['role'] == role), None)
        if not choice or not choice['available']:
            raise ValueError('That storage choice is unavailable. Refresh the disk list and choose again.')
        result[role] = choice['path']
    first, second = Path(result['staging']), Path(result['library'])
    if first == second or first in second.parents or second in first.parents:
        raise ValueError('Temporary storage and finished media must use separate folders.')
    return result


PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storage — Magic Media PAL</title><style>
body{background:#141414;color:#eee;font:17px/1.55 system-ui;margin:0}main{max-width:860px;margin:36px auto;padding:0 20px}
.card{background:#202020;border:1px solid #444;border-radius:14px;padding:24px;margin:20px 0}h1,h2{line-height:1.2}h2{font-size:23px}
.hint{color:#bbb;font-size:15px}select,button{font:inherit;padding:12px;border-radius:7px;max-width:100%}select{display:block;width:100%;background:#151515;color:white;border:1px solid #777;margin:10px 0}
button{background:#8bc8ff;color:#101820;border:0;cursor:pointer}a{color:#9bd0ff}code{overflow-wrap:anywhere}.notice{border-left:4px solid #8bc8ff;padding:12px 18px;background:#242d35}.error{color:#ffb8b8}label{font-weight:650}
</style></head><body><main><a href="/settings">← Settings</a><h1>Choose where your media goes</h1>
<p>Use a disk with room for the original disc and, if wanted, a smaller copy. You can return here to change these choices.</p>
{% if error %}<p role="alert" class="error">{{ error }}</p>{% endif %}
{% if message %}<p role="status" class="notice">{{ message }}</p>{% endif %}
{% if pending %}<section class="card"><h2>Change waiting to be applied</h2><p>Temporary storage: <code>{{ pending.staging }}</code><br>Finished media: <code>{{ pending.library }}</code></p>
<p>The current folders below remain in use. Finish any jobs before an administrator applies this change and restarts Dexter. Existing files will stay in their original folders.</p>
<form method="post"><input type="hidden" name="token" value="{{ token }}"><button name="action" value="cancel">Keep current storage</button></form></section>{% endif %}
{% if inventory %}<form method="post"><input type="hidden" name="token" value="{{ token }}">
{% for role, title, explanation in [('staging','Temporary storage','Original disc files are saved here first. Keep these until your finished media has been checked.'),('library','Finished media','Your movie, television and music folders. Connecting Jellyfin does not automatically share this folder with its server.')] %}
<section class="card"><h2>{{ title }}</h2><p>{{ explanation }}</p>
{% for choice in inventory.choices if choice.role == role and choice.current %}<p class="hint">Currently: <code>{{ choice.path }}</code><br>{{ choice.free_gib }} GiB free of {{ choice.total_gib }} GiB on {{ choice.source }}{% if choice.system %} · System disk{% endif %}</p>{% endfor %}
<label for="{{ role }}">Save to</label><select id="{{ role }}" name="{{ role }}" required>
{% for choice in inventory.choices if choice.role == role %}<option value="{{ choice.id }}" {% if selected_ids[role] == choice.id %}selected{% endif %} {% if not choice.available %}disabled{% endif %}>{{ choice.source }} · {{ choice.free_gib }} GiB free{% if choice.system %} · System disk{% endif %} — {{ choice.path }}{% if not choice.available %} (unavailable){% endif %}</option>{% endfor %}</select></section>{% endfor %}
<button name="action" value="save">Review and request storage change</button><p class="hint">This records your choice. It does not move files, prepare disks or interrupt a job. Applying the change requires a host restart step in this development version.</p></form>
{% if inventory.unready %}<section class="card"><h2>Disks that need attention</h2>{% for disk in inventory.unready %}<p><strong>{{ disk.name }} · {{ disk.size_gib }} GiB</strong><br>{{ disk.reason }} · <code>{{ disk.device }}</code><br><span class="hint">{{ disk.detail }}</span>{% if disk.serial and disk.by_id %}<br><a href="{{ url_for('prepare_storage_page', device=disk.device) }}">Review erasing and using this whole disk</a>{% endif %}</p>{% endfor %}</section>{% endif %}
<p class="hint">Disk list checked {{ checked }}. Capacity may change. Only mounted, writable locations can be selected. Refreshing this page does not rescan host disks.</p>
{% else %}<section class="card"><h2>Disk list is not available yet</h2><p>An administrator needs to refresh storage information with the host installer. Current storage has not changed.</p></section>{% endif %}
</main></body></html>'''
