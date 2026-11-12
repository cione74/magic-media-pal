"""Check the experimental image entrypoint and restart with disposable data only."""
import argparse
import json
import subprocess
import uuid

CHECK = r"""
import hashlib, http.cookiejar, json, re, sys, time, urllib.request, urllib.parse
from pathlib import Path
base = 'http://127.0.0.1:8080'
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
for attempt in range(60):
    try:
        response = client.open(base + '/settings', timeout=2)
        html = response.read().decode()
        break
    except OSError:
        time.sleep(0.25)
else:
    raise RuntimeError('Candidate entrypoint did not serve Settings')
if sys.argv[1] == 'initial':
    assert '/static/pal-the-genie.png' in html
    with client.open(base + '/static/pal-the-genie.png', timeout=10) as asset:
        assert asset.status == 200 and asset.headers.get_content_type() == 'image/png'
        assert asset.read(8) == bytes.fromhex('89504e470d0a1a0a')
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    data = urllib.parse.urlencode(dict(_csrf=token, drive_device='', cd_offset='0',
        jellyfin_url='', movie_quality='original', movie_destination='staging')).encode()
    request = urllib.request.Request(base + '/settings', data=data, headers={'Origin':base})
    client.open(request, timeout=10).read()
    for path in ('/config/native-entrypoint-fixture.txt', '/output/native-entrypoint-fixture.txt'):
        Path(path).write_text('isolated runtime validation\n')
settings = json.loads(Path('/config/dexter-ripper-settings.json').read_text())['settings']
assert settings['setup_complete'] and settings['movie_destination'] == 'staging'
for url, expected in (('/', 'Magic Media PAL'),('/settings','Settings'),('/credits','HandBrake'),('/movie-history','History')):
    with client.open(base + url, timeout=10) as response:
        assert response.status == 200 and expected.lower() in response.read().decode().lower(), url
files = ['/config/dexter-ripper-settings.json','/config/browser-session-key',
         '/config/native-entrypoint-fixture.txt','/output/native-entrypoint-fixture.txt']
print(json.dumps({path:hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in files}, sort_keys=True))
"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='dexter-ripper:runtime-candidate')
    args = parser.parse_args()
    name = 'dexter-runtime-smoke-' + uuid.uuid4().hex[:10]
    def run(*command, **kwargs):
        return subprocess.run(['docker', *command], check=True, text=True,
                              capture_output=True, timeout=60, **kwargs).stdout.strip()
    image_id = run('image', 'inspect', args.image, '--format', '{{.Id}}')
    try:
        run('run', '-d', '--network', 'none', '--name', name, args.image)
        before = json.loads(run('exec', '-i', name, 'python3', '-', 'initial', input=CHECK))
        run('restart', name)
        after = json.loads(run('exec', '-i', name, 'python3', '-', 'restarted', input=CHECK))
        assert before == after, 'Settings, private key or marker changed across restart'
        print(json.dumps({'image':image_id, 'result':'PASS', 'checks':[
            'native entrypoint HTTP startup', 'no-hardware first-run save with CSRF',
            'Home, Settings, Credits and History HTTP', 'container restart persistence'],
            'scope':'disposable container, no network, ports, optical devices, GPU or host data mounts'}, indent=2))
    except subprocess.CalledProcessError as error:
        print(error.stderr)
        print(run("logs", name))
        raise
    except Exception:
        print(run('logs', name))
        raise
    finally:
        subprocess.run(['docker','rm','-f',name], stdout=subprocess.DEVNULL, check=False, timeout=30)

if __name__ == '__main__':
    main()
