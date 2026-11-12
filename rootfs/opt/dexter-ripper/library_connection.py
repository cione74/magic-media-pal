"""Read-only Jellyfin connection checks; never change server libraries."""
import json
import re
import urllib.request
import urllib.error
import urllib.parse


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def find_movie(server, key, title, expected_path, *, item_type="Movie"):
    """Return an item ID only when the server reports the exact movie file."""
    if not server or not key:
        return None
    query = urllib.parse.urlencode({
        "Recursive": "true", "IncludeItemTypes": "Episode" if item_type == "Episode" else "Movie", "SearchTerm": title,
        "Fields": "Path", "Limit": 50,
    })
    request = urllib.request.Request(
        server.rstrip("/") + "/Items?" + query,
        headers={"Authorization": f'MediaBrowser Token="{key}"'},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return None
        result = json.loads(data)
        if not isinstance(result, dict) or not isinstance(result.get("Items"), list):
            return None
        for item in result["Items"]:
            if not isinstance(item, dict) or item.get("Path") != expected_path:
                continue
            item_id = item.get("Id")
            if isinstance(item_id, str) and re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}", item_id):
                return item_id
    except (OSError, ValueError):
        pass
    return None


def inspect(server, key):
    if not server:
        return {'ok': False, 'message': 'Jellyfin is not connected. Enter its server address.', 'libraries': []}
    if not key:
        return {'ok': False, 'message': 'Enter a Jellyfin API key to check access and discover libraries.', 'libraries': []}
    try:
        parsed = urllib.parse.urlsplit(server)
    except ValueError:
        return {'ok': False, 'message': 'Check the Jellyfin server address.', 'libraries': []}
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        return {'ok': False, 'message': 'Use an http:// or https:// server address without embedded credentials.', 'libraries': []}
    request = urllib.request.Request(server.rstrip('/') + '/Library/VirtualFolders',
                                     headers={'Authorization': f'MediaBrowser Token="{key}"'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('Response too large')
        folders = json.loads(data)
        if not isinstance(folders, list):
            raise ValueError('Invalid library response')
        libraries = []
        for folder in folders:
            if not isinstance(folder, dict) or not isinstance(folder.get('Locations', []), list):
                raise ValueError('Invalid library entry')
            libraries.append({'name': str(folder.get('Name') or 'Unnamed library'),
                              'kind': str(folder.get('CollectionType') or 'mixed'),
                              'paths': [p for p in folder.get('Locations', []) if isinstance(p, str)]})
        return {'ok': True, 'message': f'Connected to Jellyfin. Found {len(libraries)} libraries.', 'libraries': libraries}
    except urllib.error.HTTPError as exc:
        message = ('Jellyfin rejected this API key or its permissions.' if exc.code in (401, 403)
                   else f'Jellyfin returned HTTP {exc.code}. Check the server address; redirects are not followed.')
    except (OSError, ValueError):
        message = 'Could not read Jellyfin libraries. Check the server address, network connection and API key.'
    return {'ok': False, 'message': message, 'libraries': []}
