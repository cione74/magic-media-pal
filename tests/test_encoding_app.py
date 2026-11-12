"""Flask route tests without startup workers, devices, or production paths."""
import ast
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper/app.py'
sys.path.insert(0, str(SOURCE.parent))
tree = ast.parse(SOURCE.read_text())
tree.body = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))]
m = {'__name__': 'dexter_app_test'}
exec(compile(tree, str(SOURCE), 'exec'), m)


class EncodingAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'movie.mkv'
        self.source.write_bytes(b'original')
        self.output = {'path': str(self.source), 'verification': {'ok': True, 'filename': 'movie.mkv',
                       'encoding_source': {'hdr': False, 'bit_depth': 8}}}
        m['CONFIG_ROOT'] = Path(self.tmp.name)
        m['state'] = {'disc': None, 'job': {'running': False, 'outputs': [copy.deepcopy(self.output)], 'log': []}}
        m['persist_state_now'] = lambda **kwargs: None
        m['_allowed_staged_video'] = lambda path: Path(path).parent == self.source.parent
        self.client = m['app'].test_client()

    def post(self, **fields):
        return self.client.post('/encode-video', data={'output_file': str(self.source), **fields})


    def test_interrupted_batch_recovery_never_deletes_destination(self):
        library = Path(self.tmp.name) / 'library'
        destination = library / 'Series'
        destination.mkdir(parents=True)
        existing = destination / 'existing.mp4'
        existing.write_bytes(b'existing library media')
        job = {'running': True, 'outputs': [copy.deepcopy(self.output)]}
        m['_recover_interrupted_batch_import'](job, {'library_root': str(library), 'destination_root': str(destination)})
        self.assertEqual(existing.read_bytes(), b'existing library media')
        self.assertTrue(self.source.exists())
        self.assertFalse(job['running'])
        self.assertIn('needs review', job['status'])

    def test_local_batch_refresh_failure_retains_sources(self):
        other = self.source.with_name('second.mkv')
        other.write_bytes(b'second original')
        m['state']['job']['outputs'].append({'path': str(other), 'verification': {'ok': True}})
        class InlineThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        with patch.dict(m['os'].environ, {}, clear=True), \
                patch.dict(m, {'MEDIA_ROOT': Path(self.tmp.name) / 'library',
                               'apply_library_permissions': lambda *a, **k: None,
                               'refresh_jellyfin': lambda: (_ for _ in ()).throw(OSError('refresh failed'))}), \
                patch.object(m['threading'], 'Thread', InlineThread):
            response = self.client.post('/import-jellyfin-batch', data={'media_type': 'movie', 'title': 'Movie',
                                        'primary_file': str(self.source)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.source.read_bytes(), b'original')
        self.assertEqual(other.read_bytes(), b'second original')
        self.assertEqual(len(m['state']['job']['outputs']), 2)

    def test_local_batch_does_not_delete_folder_created_by_another_writer(self):
        other = self.source.with_name('second.mkv'); other.write_bytes(b'second')
        m['state']['job']['outputs'].append({'path': str(other), 'verification': {'ok': True}})
        library = Path(self.tmp.name) / 'library'
        destination = library / 'movies' / 'Movie'
        existing = destination / 'existing.mkv'
        class RacingThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self):
                destination.mkdir(parents=True)
                existing.write_bytes(b'other writer')
                self.target()
        with patch.dict(m['os'].environ, {}, clear=True), \
                patch.dict(m, {'MEDIA_ROOT': library}), patch.object(m['threading'], 'Thread', RacingThread):
            self.client.post('/import-jellyfin-batch', data={'media_type': 'movie', 'title': 'Movie',
                             'primary_file': str(self.source)})
        self.assertEqual(existing.read_bytes(), b'other writer')
        self.assertTrue(self.source.exists())
        self.assertTrue(other.exists())

    def test_authenticated_refresh_and_search_use_no_redirect_opener(self):
        import io
        key = Path(self.tmp.name) / 'test-key'
        key.write_text('isolated-test-token')
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(self, request, **kwargs): return Response(b'[]')
        handlers = []
        def build(*args): handlers.extend(args); return Opener()
        with patch.dict(m, {'JELLYFIN_KEY_FILE': key, 'configured_jellyfin_url': lambda: 'https://example.invalid'}), \
                patch.object(m['urllib'].request, 'build_opener', side_effect=build), \
                patch.object(m['urllib'].request, 'urlopen', side_effect=AssertionError('redirecting opener used')):
            self.assertTrue(m['refresh_jellyfin']()[0])
            self.assertEqual(m['lookup_movie_metadata']('Movie'), [])
        self.assertEqual(handlers, [m['library_connection'].NoRedirect] * 2)

    def test_home_and_wizard_link_complete_credits_and_keep_old_url(self):
        self.assertIn('href="/credits"', m['PAGE'])
        self.assertGreater(m['PAGE'].index('href="/credits"'), m['PAGE'].index('</main>'))
        wizard = Path(m['workflow_wizard'].__file__).with_name('wizard.html').read_text()
        self.assertGreater(wizard.index('href="/credits"'), wizard.index('</main>'))
        self.assertNotIn('href="/credits"', wizard[:wizard.index('</nav>')])
        self.assertIn('href="/credits"', Path(m['workflow_wizard'].__file__).with_name('wizard.html').read_text())
        for route in ('/credits', '/data-sources'):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            for name in ('MakeMKV', 'HandBrake', 'FFmpeg', 'ffprobe', 'cyanrip', 'Jellyfin',
                         'MusicBrainz', 'Deezer', 'TVmaze', 'TheDiscDb', 'libdvdread', 'libbluray',
                         'Python', 'Flask', 'Docker', 'NVIDIA', 'jlesage', 'CC BY-SA 4.0'):
                self.assertIn(name.encode(), response.data)
            self.assertIn(b'Back to Dexter', response.data)
            import re
            external = re.findall(rb'<a href="https://[^>]+>', response.data)
            self.assertTrue(external)
            for link in external:
                self.assertIn(b'target="_blank"', link)
                self.assertIn(b'rel="noopener noreferrer"', link)
            self.assertIn(b'<a class="button" href="/">Back to Dexter</a>', response.data)

    def test_history_shows_three_recent_jobs_and_more_with_status_filters(self):
        records = []
        for index in range(7):
            records.append({'id':f'{index:032x}', 'title':f'History item {index}', 'completed_at':index,
                            'original_bytes':100, 'output_bytes':50 if index % 2 == 0 else 0,
                            'success':index % 2 == 0, 'stage_seconds':{}, 'engine':'cpu', 'quality':'balanced'})
        m['state']['movie_history'] = records
        before = copy.deepcopy(records)
        page = self.client.get('/movie-history').data
        for index in (4, 5, 6): self.assertIn(f'History item {index}'.encode(), page)
        for index in (0, 1, 2, 3): self.assertNotIn(f'History item {index}'.encode(), page)
        self.assertIn(b'limit=6', page)
        self.assertIn(b'More', page)
        more = self.client.get('/movie-history?limit=6').data
        self.assertIn(b'History item 1',more); self.assertNotIn(b'History item 0',more)
        completed = self.client.get('/movie-history?status=completed').data
        self.assertIn(b'History item 6',completed); self.assertNotIn(b'History item 5',completed)
        attention = self.client.get('/movie-history?status=attention').data
        self.assertIn(b'History item 5',attention); self.assertNotIn(b'History item 6',attention)
        invalid = self.client.get('/movie-history?limit=bad&status=unknown')
        self.assertEqual(invalid.status_code,200)
        self.assertIn(b'Showing 3 of 7',invalid.data)
        self.assertEqual(m['state']['movie_history'],before)

    def test_first_run_settings_can_finish_without_optical_hardware(self):
        saved = []
        with patch.dict(m, {'settings':{'setup_complete':False},
                           'detected_optical_drives':lambda:[], 'save_settings':lambda values:saved.append(values.copy())}):
            page = self.client.get('/settings')
            self.assertIn(b'You can finish setup now', page.data)
            self.assertNotIn(b'<button type="submit" disabled>Finish Setup', page.data)
            response = self.client.post('/settings', data={'drive_device':'', 'cd_offset':'37',
                                        'movie_quality':'balanced', 'movie_destination':'staging'})
            self.assertEqual(response.status_code,302)
            self.assertTrue(saved[0]['setup_complete'])
            self.assertEqual(saved[0]['cd_offset'],'37')
            self.assertEqual(saved[0]['drive_name'],'No optical drive connected')
            self.assertTrue(saved[0]['drive_device'].startswith('/dev/'))

    def test_no_drive_setup_rejects_arbitrary_device_path(self):
        saved = []
        with patch.dict(m, {'settings':{'setup_complete':False},
                           'detected_optical_drives':lambda:[], 'save_settings':lambda values:saved.append(values)}):
            response=self.client.post('/settings',data={'drive_device':'/dev/not-authorized',
                                      'movie_quality':'original','movie_destination':'staging'})
            self.assertEqual(response.status_code,200)
            self.assertIn(b'Select an available optical drive',response.data)
            self.assertEqual(saved,[])

    def test_dashboard_and_wizard_share_theme_and_active_verification_heading(self):
        css = self.client.get('/assets/dexter.css')
        self.assertEqual(css.status_code, 200)
        self.assertIn('text/css', css.content_type)
        self.assertIn(b'.dashboard-page', css.data)
        self.assertIn('/assets/dexter.css', m['PAGE'])
        wizard = Path(m['workflow_wizard'].__file__).with_name('wizard.html').read_text()
        self.assertIn('/assets/dexter.css', wizard)
        heading = m['job_display_heading']
        self.assertEqual(heading({'running': True, 'recovery': {'kind': 'encode'}, 'stage': 'verifying'}), 'Checking media')
        self.assertEqual(heading({'running': True, 'recovery': {'kind': 'encode'}, 'stage': 'packaging'}), 'Preparing sound and subtitles')

    def test_cd_rip_does_not_request_automatic_ejection(self):
        m['state']['disc'] = {'kind': 'audio', 'scan_id': 'cd-test', 'tracks': [{'number': 1}]}
        with patch.dict(m, {'run_job': lambda *args, **kwargs: captured.append((args, kwargs)) or True}):
            captured = []
            response = self.client.post('/rip/audio', data={'scan_id': 'cd-test', 'track': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(captured), 1)
        command = captured[0][0][1]
        self.assertEqual(command[0], 'cyanrip')
        self.assertNotIn('-Q', command)
        self.assertIn('-l', command)

    def test_missing_music_destination_preserves_verified_tracks_and_offers_retry(self):
        m['state']['disc'] = {'kind': 'audio', 'scan_id': 'cd-test', 'album': 'Album',
                              'album_artist': 'Artist', 'tracks': [{'number': 1, 'title': 'Song'}]}
        report = {'tracks': 1, 'format': 'Lossless FLAC', 'bytes': 8, 'validation_seconds': 1}
        m['state']['job']['audio_report'] = report.copy()
        with patch.dict(m, {'MUSIC_ROOT': Path(self.tmp.name) / 'missing',
                            'staged_audio_files': lambda disc: {1: self.source}}), \
                patch.object(m['threading'], 'Thread') as worker:
            response = self.client.post('/import/music', data={'scan_id': 'cd-test'})
        self.assertEqual(response.status_code, 302)
        worker.assert_not_called()
        self.assertTrue(m['state']['job']['music_import_failed'])
        self.assertEqual(m['state']['job']['audio_report'], report)
        self.assertIn('No need to rip', m['state']['job']['status'])
        self.assertEqual(self.source.read_bytes(), b'original')
        self.assertFalse((Path(self.tmp.name) / 'missing').exists())

    def test_shared_music_import_prepares_local_tracks_and_uses_writer_helper(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()
        source = Path(self.tmp.name) / 'song.flac'
        m['subprocess'].run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                             '-t', '0.1', '-c:a', 'flac', str(source)], check=True, capture_output=True)
        original = source.read_bytes()
        m['state']['disc'] = {'kind': 'audio', 'scan_id': 'cd-test', 'album': 'Album', 'album_artist': 'Artist',
                              'tracks': [{'number': 1, 'title': 'Song'}], 'selected_tracks': [1]}
        commands = []
        def helper(prepared, artist, album):
            prepared = Path(prepared)
            self.assertTrue((prepared / '01 - Song.flac').is_file())
            commands.append((prepared, artist, album))
            return [m['sys'].executable, '-c', "print('Track verified: 1'); print('Album published and verified')"], {}
        with patch.dict(m['os'].environ, {'DEXTER_JELLYFIN_EXPORT': 'test:/share'}), \
                patch.dict(m, {'AUDIO_OUTPUT': Path(self.tmp.name), 'music_share_preflight': lambda *args: None,
                               'music_share_command': helper, 'staged_audio_files': lambda disc: {1: source},
                               'refresh_jellyfin': lambda: (True, 'requested')}), \
                patch.object(m['threading'], 'Thread', InlineThread):
            response = self.client.post('/import/music', data={'scan_id': 'cd-test'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(m['state']['job']['audio_imported_path'], '/jellyfin-import/music/Artist/Album')
        self.assertFalse(m['state']['job']['music_import_failed'])
        self.assertEqual(len(commands), 1)
        self.assertFalse(list(Path(self.tmp.name).glob('.music-prepared-*')))

    def test_current_job_shows_batch_names_and_keeps_step_progress(self):
        m['settings']={'setup_complete':True}
        m['state']['disc']={'kind':'video','title':'Series','type':'DVD disc','titles':[],'raw':''}
        m['state']['workflow_queue']=[
            {'id':'e1','batch_id':'batch','status':'completed','plan':{'destination':'staging','episode':{'number':1,'name':'Pilot'}}},
            {'id':'e2','batch_id':'batch','status':'running','plan':{'destination':'staging','episode':{'number':2,'name':'Second Episode'}}},
            {'id':'e3','batch_id':'batch','status':'queued','plan':{'destination':'staging','episode':{'number':3,'name':'Third Episode'}}}]
        m['state']['job'].update(running=True,description='Encode video',stage='encoding',queue_id='e2',
            recovery={'kind':'encode'},status='Encoding',progress={'percent':25,'elapsed':'1:30','written':'712.2 MiB',
                                                                'rate':'18.9× media speed','eta':'1:02'})
        with patch.dict(m,{'collect_drive_diagnostics':lambda *args:{},'staging_storage':lambda:{'free':'100 GiB','total':'200 GiB'}}):
            response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        for content in ('1 of 3 episodes ready','Pilot','Second Episode','Third Episode','Ready for review',
                        'Making a smaller copy','Waiting','25%','Copy size','Time spent','Time left','18.9× playback'):
            self.assertIn(content,html)
        self.assertNotIn('18.9× media speed',html)

    def test_tv_batch_uses_shared_transfer_and_confirmed_names_without_local_move(self):
        outputs=[]
        for i in (1,2):
            source=Path(self.tmp.name)/f'episode{i}.mp4';source.write_bytes(b'verified episode')
            outputs.append({'path':str(source),'verification':{'ok':True},'episode':{
                'series':'Example Series','season':1,'number':i,'name':f'Episode {i}',
                'evidence':{'state':'user_confirmed'}}})
        m['state']['job']['outputs']=outputs
        captured=[]
        with patch.dict(m['os'].environ,{'DEXTER_JELLYFIN_EXPORT':'test:/share'}), \
                patch.dict(m,{'start_nfs_batch_import':lambda *args:captured.append(args)}):
            response=self.client.post('/import-jellyfin-batch',data={'media_type':'tv','title':'Wrong disc label',
                                                                    'season':'1','start_episode':'1'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(captured[0][2],'tv')
        self.assertIn('Example Series/Season 01/Example Series - S01E01 - Episode 1.mp4',str(captured[0][1][0][1]))
        self.assertNotIn('Wrong disc label',str(captured[0][1][0][1]))
        self.assertTrue(all(Path(item['path']).is_file() for item in outputs))
        self.assertEqual(len(m['state']['job']['outputs']),2)

    def test_nfs_batch_worker_keeps_sources_and_records_verified_publication(self):
        import io
        class InlineThread:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        class Process:
            pid=999999
            def __init__(self,*args,**kwargs):
                self.stdin=io.StringIO();self.stdout=iter(['Transfer bytes: 8\n','Batch published and verified: 1\n'])
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def wait(self):return 0
        target=m['MEDIA_ROOT']/'tv/Example/Season 01/Example - S01E01.mkv'
        with patch.dict(m,{'video_transfer_identity':lambda *args:{},'refresh_jellyfin':lambda:(True,'requested')}), \
                patch.object(m['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout='')), \
                patch.object(m['subprocess'],'Popen',Process), patch.object(m['threading'],'Thread',InlineThread):
            m['start_nfs_batch_import']([self.output],[(self.source,target)],'tv')
        self.assertTrue(m['state']['job']['batch_import']['verified'])
        self.assertIn('Imported to Jellyfin',m['state']['job']['status'])
        self.assertEqual(self.source.read_bytes(),b'original')
        self.assertTrue(m['state']['job']['outputs'])

    def test_invalid_custom_and_paths(self):
        for fields in ({'profile': 'bad'}, {'profile': 'custom'}, {'codec': 'bad'},
                       {'profile': 'custom', 'quality': 'nan'}, {'output_file': '/etc/passwd'}):
            self.assertEqual(self.post(**fields).status_code, 400)
        self.assertFalse(m['state']['job']['running'])

    def test_playback_workflow_runs_from_staged_copy_and_records_report(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()

        m['subprocess'].run([
            'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=size=96x64:rate=24:duration=1', '-f', 'lavfi', '-i',
            'anullsrc=cl=5.1:r=48000', '-t', '1', '-map', '0:v', '-map', '1:a',
            '-c:v', 'libx264', '-c:a', 'ac3', '-metadata:s:a:0', 'language=eng',
            str(self.source),
        ], check=True, capture_output=True, timeout=30)
        original = self.source.read_bytes()
        with patch.object(m['threading'], 'Thread', InlineThread):
            response = self.client.post('/workflow/staged-movie', data={
                'output_file': str(self.source), 'workflow_quality': 'original',
                'workflow_codec': 'h265', 'workflow_engine': 'auto',
                'workflow_destination': 'staging', 'workflow_title': 'A movie',
                'workflow_container': 'mp4', 'workflow_surround': 'yes',
            })
        self.assertEqual(response.status_code, 302)
        job = m['state']['job']
        self.assertFalse(job['running'])
        self.assertIn('prepared and verified', job['status'])
        self.assertEqual(Path(job['outputs'][0]['path']).suffix, '.mp4')
        self.assertEqual(self.source.read_bytes(), original)
        report = job['processing_report']
        self.assertEqual(len(m['state']['movie_history']), 1)
        self.assertIn('packaging', report['stage_seconds'])
        self.assertEqual(self.client.get('/movie-history').status_code, 200)
        self.assertEqual(self.client.get('/movie-report/' + report['id']).status_code, 200)
        self.assertEqual(self.client.get('/movie-log/' + report['id']).status_code, 200)

    def test_packaging_failure_keeps_verified_intermediate_available(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()
        def encode(source, target, *args, **kwargs):
            target.write_bytes(b"verified encoded movie")
            return target
        policy = {"container": "mp4", "language": "eng", "surround": True, "subtitles": False}
        with patch.object(m["threading"], "Thread", InlineThread), patch.object(m["encoding"], "probe", return_value={}), patch.object(m["encoding"], "encode_file", side_effect=encode), patch.object(m["encoding"], "inspect", return_value={}), patch.object(m["movie_output"], "preflight"), patch.object(m["movie_output"], "resolve"), patch.object(m["movie_output"], "rebase_policy", return_value=policy), patch.object(m["movie_output"], "prepare", side_effect=ValueError("Packaging failed")), patch.dict(m, {"probe_media": lambda path: {"ok": True}}):
            m["start_video_encode"](str(self.source), "balanced", "h265",
                                    workflow={"output_policy": policy, "title": "Example"})
        outputs = m["state"]["job"]["outputs"]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(Path(outputs[1]["path"]).read_bytes(), b"verified encoded movie")
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertIn("Packaging failed", m["state"]["job"]["status"])
        self.assertFalse(m["state"]["movie_history"][0]["success"])

    def test_mp4_subtitles_are_rejected_without_starting_work(self):
        response = self.client.post('/workflow/staged-movie', data={
            'output_file': str(self.source), 'workflow_container': 'mp4',
            'workflow_subtitles': 'yes',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('Choose MKV', m['state']['job']['status'])
        self.assertFalse(m['state']['job']['running'])

    def test_busy_job_refused(self):
        m['state']['job']['running'] = True
        self.assertEqual(self.post().status_code, 409)

    def test_missing_scanning_tools_do_not_clear_staged_media(self):
        original_outputs = copy.deepcopy(m['state']['job']['outputs'])
        with patch.object(m['shutil'], 'which', return_value=None), patch.dict(m, {'clear_staged_media': lambda: self.fail('Staging was cleared')}):
            self.assertEqual(self.client.post('/scan').status_code, 302)
        self.assertFalse(m['state']['job']['running'])
        self.assertEqual(m['state']['job']['outputs'], original_outputs)
        self.assertIn('retained', m['state']['job']['status'])

    def test_scan_requires_confirmation_then_clears_previous_job(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        root = self.source.parent
        video = root / "video"
        video.mkdir()
        source = self.source.rename(video / self.source.name)
        self.output["path"] = str(source)
        m["state"]["job"]["outputs"] = [self.output]
        audio = root / "audio"
        album = audio / "previous-album"
        album.mkdir(parents=True)
        track = album / "01 - Track.flac"
        track.write_bytes(b"previous album")
        disc = {"kind": "audio", "tracks": [{"number": 1}], "selected_tracks": [1]}
        with patch.object(m["shutil"], "which", return_value="/usr/bin/test"), patch.object(m["threading"], "Thread", InlineThread), patch.dict(m, {"scan_disc": lambda: disc, "AUDIO_OUTPUT": audio, "OUTPUT": self.source.parent}):
            self.assertEqual(self.client.post("/scan").status_code, 302)
            self.assertTrue(source.is_file())
            self.assertTrue(track.is_file())
            self.assertEqual(self.client.post("/scan", data={"clear_staging": "yes"}).status_code, 302)
            self.assertEqual(m["staged_audio_files"](m["state"]["disc"]), {})
        self.assertEqual(m["state"]["job"]["outputs"], [])
        self.assertEqual(m["state"]["job"]["title_queue"], [])
        self.assertFalse(source.exists())
        self.assertFalse(track.exists())

    def test_failed_video_retry_reuses_selection_and_rejects_stale_request(self):
        m["state"]["disc"] = {"kind": "video"}
        m["state"]["job"].update(status="Rip failed: read error", video_retry_token="current",
                                title_queue=[{"id": 3}, {"id": 7}])
        with patch.dict(m, {"start_video_rip": lambda ids, retry_token: (str(ids), 202)}):
            self.assertEqual(self.client.post("/rip/video/retry", data={"retry_token": "old"}).status_code, 409)
            response = self.client.post("/rip/video/retry", data={"retry_token": "current"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.data, b"[3, 7]")
            m["state"]["job"]["running"] = True
            self.assertEqual(self.client.post("/rip/video/retry", data={"retry_token": "current"}).status_code, 409)

    def test_staged_movie_workflow_uses_verified_file_without_starting_rip(self):
        from unittest.mock import Mock
        continue_job = Mock()
        with patch.dict(m, {"continue_movie_workflow": continue_job,
                            "start_video_rip": lambda *a, **kw: self.fail("Disc rip started")}):
            self.assertEqual(self.client.post("/workflow/staged-movie", data={
                "output_file": str(self.source), "workflow_quality": "balanced",
            }).status_code, 302)
            continue_job.assert_called_once()
            self.assertEqual(continue_job.call_args.args[1], str(self.source))
            self.assertEqual(continue_job.call_args.args[2]["profile"], "balanced")
            continue_job.reset_mock()
            self.client.post("/workflow/staged-movie", data={"output_file": "/etc/passwd"})
            continue_job.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_disc_compression_check_uses_selected_playlist_and_retains_staging(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        disc = {"kind": "video", "titles": [{"id": 0, "source": "00007.mpls"},
                                           {"id": 1, "source": "00100.mpls"}]}
        m["state"]["disc"] = disc
        with patch.object(m["threading"], "Thread", InlineThread), patch.object(
            m["disc_compression"], "check", return_value=("Color settings are missing in the sample.", "")
        ) as sample:
            response = self.client.post("/rip/video", data={"check_title": "1", "title_ids": ["0", "1"]})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(sample.call_args.args[0], 100)
        self.assertIn("Color settings are missing", disc["compression_checks"]["1"])
        self.assertEqual(m["state"]["job"]["outputs"], [self.output])
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_movie_plan_defers_unknown_color_to_encoder_without_importing_original(self):
        job = m["state"]["job"]
        job["outputs"][0]["verification"]["encoding_source"]["hdr"] = None
        plan = {"profile": "balanced", "codec": "h264", "engine": "auto",
                "destination": "jellyfin", "title": "Example", "year": "2004"}
        from unittest.mock import Mock
        encode, publish = Mock(return_value=None), Mock()
        with patch.dict(m, {"start_video_encode": encode, "start_nfs_movie_import": publish}):
            m["continue_movie_workflow"](job, str(self.source), plan, after_encode=False)
        encode.assert_called_once_with(str(self.source), "balanced", "h264",
                                       workflow=plan, expected_job=job, engine="auto")
        publish.assert_not_called()
        self.assertTrue(self.source.is_file())

    def test_movie_plan_uses_verified_encoded_file_for_import(self):
        job = m["state"]["job"]
        plan = {"profile": "balanced", "codec": "h264", "destination": "jellyfin",
                "title": "Example", "year": "2004"}
        from unittest.mock import Mock
        encode, publish = Mock(return_value=None), Mock(return_value=True)
        with patch.dict(m, {"start_video_encode": encode, "start_nfs_movie_import": publish}), patch.dict(m["os"].environ, {"DEXTER_JELLYFIN_EXPORT": "server:/media"}), patch.object(m["encoding"], "cached_probe", return_value={"available": True, "encoders": ["x264"]}):
            m["continue_movie_workflow"](job, str(self.source), plan, after_encode=False)
            encode.assert_called_once_with(str(self.source), "balanced", "h264", workflow=plan, expected_job=job, engine="cpu")
            publish.assert_not_called()
            encoded = self.source.with_name("encoded.mkv")
            encoded.write_bytes(b"encoded")
            job["outputs"][0]["path"] = str(encoded)
            m["continue_movie_workflow"](job, str(encoded), plan, after_encode=True)
            publish.assert_called_once_with(encoded, "Example", "2004", expected_job=job)

    def test_queued_job_owns_worker_and_can_cancel(self):
        with patch.object(m['threading'], 'Thread'):
            self.assertEqual(self.post().status_code, 302)
        job = m['state']['job']
        self.assertTrue(job['running'])
        self.assertEqual(job['recovery']['kind'], 'encode')
        self.assertTrue(m['job_is_cancellable'](job))
        self.assertEqual(m['ui_phase'](None, job), 'Encoding')
        self.assertEqual(self.client.post('/cancel-job').status_code, 302)
        self.assertTrue(job['cancel_requested'])

    def test_completed_and_failed_worker_retain_original(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()
        def encode(source, target, *args, **kwargs):
            target.write_bytes(b'encoded')
            kwargs['update'](stage='verifying', percent=None)
            return target
        m['probe_media'] = lambda path: {'ok': True}
        with patch.object(m['threading'], 'Thread', InlineThread), patch.object(m['encoding'], 'probe', return_value={}), patch.object(m['encoding'], 'encode_file', side_effect=encode):
            self.assertEqual(self.post().status_code, 302)
        job = m['state']['job']
        self.assertFalse(job['running'])
        self.assertNotEqual(job['outputs'][0]['path'], str(self.source))
        self.assertEqual(self.source.read_bytes(), b'original')
        selected = job['outputs'][0]['path']
        with patch.object(m['threading'], 'Thread', InlineThread), patch.object(m['encoding'], 'probe', return_value={}), patch.object(m['encoding'], 'encode_file', side_effect=ValueError('unsupported')):
            self.assertEqual(self.post(output_file=selected).status_code, 302)
        self.assertIn('failed', job['status'])
        self.assertEqual(job['outputs'][0]['path'], selected)
        self.assertFalse(job['running'])

    def test_template_compiles(self):
        m['app'].jinja_env.from_string(m['PAGE'])

    def test_verified_output_page_renders_encoding_form(self):
        m['settings']['setup_complete'] = True
        m['state']['job'].update(description='', status='Completed', title_queue=[], progress=None)
        replacements = {'staging_has_media': lambda: True, 'staging_storage': lambda: None,
                        'collect_drive_diagnostics': lambda *args: {}, 'configured_drive_name': lambda: 'Test drive'}
        with patch.dict(m, replacements), patch.object(m['encoding'], 'cached_probe', return_value={'available': True, 'encoders': ['x264']}):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Choose quality and storage', response.data)
        self.assertIn(b'/encode-video', response.data)
        self.assertIn(b'Make smaller copy and check it', response.data)

    def test_finish_view_links_to_movie_only_after_jellyfin_confirms_it(self):
        m["settings"].update(setup_complete=True, jellyfin_url="https://jelly.example")
        record = {"title": "Example Movie", "display": "Example Movie",
                  "server_path": "/media/movies/Example Movie/Example Movie.mkv"}
        m["state"]["job"].update(status="Movie added. Your staged copy is still available.",
                                 description="Adding Example Movie to Jellyfin", movie_import=record)
        replacements = {"staging_has_media": lambda: True, "staging_storage": lambda: None,
                        "collect_drive_diagnostics": lambda *args: {}}
        with patch.dict(m, replacements), patch.object(m["encoding"], "cached_probe", return_value={}), patch.object(m["library_connection"], "find_movie", return_value="a" * 32) as lookup:
            page = self.client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Check Jellyfin for this movie", page.data)
            self.assertNotIn(b"Open in Jellyfin", page.data)
            self.assertEqual(self.client.post("/jellyfin/check-movie").status_code, 302)
            self.assertEqual(lookup.call_args.args[-1], record["server_path"])
            page = self.client.get("/")
            self.assertIn(b"Open in Jellyfin", page.data)
            self.assertIn(b"Staged copy and file actions", page.data)

    def test_restart_retains_original_and_does_not_start_worker(self):
        state_file = Path(self.tmp.name) / 'state.json'
        m['DEXTER_STATE_FILE'] = state_file
        m['state']['job'].update(running=True, recovery={'kind': 'encode', 'source': str(self.source),
                                'target': str(self.source.parent / 'missing.mkv')})
        with patch.object(m['threading'], 'Thread') as thread:
            m['restore_persistent_state']()
            thread.assert_not_called()
        self.assertFalse(m['state']['job']['running'])
        self.assertIn('interrupted', m['state']['job']['status'])
        self.assertEqual(m['state']['job']['outputs'][0]['path'], str(self.source))

    def test_first_run_can_skip_jellyfin_and_shows_readiness(self):
        m['settings']['setup_complete'] = False
        replacements = {'detected_optical_drives': lambda: [{'device': '/dev/sr0', 'name': 'Family disc drive'}],
                        'save_settings': lambda values: None,
                        'JELLYFIN_KEY_FILE': Path(self.tmp.name) / 'key'}
        with patch.dict(m, replacements), patch.object(m['readiness'], 'report', return_value=[
                {'label': 'Read movie and TV discs', 'available': False, 'detail': 'Run setup again.', 'tools': 'MakeMKV'}]):
            page = self.client.get('/settings')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'What this computer can do', page.data)
            self.assertIn(b'Connect Jellyfin (optional)', page.data)
            self.assertIn(b'Needs attention', page.data)
            response = self.client.post('/settings', data={'drive_device': '/dev/sr0', 'cd_offset': '', 'jellyfin_url': ''})
            self.assertEqual(response.status_code, 302)
        self.assertTrue(m['settings']['setup_complete'])
        self.assertEqual(m['configured_jellyfin_url'](), '')

    def test_jellyfin_check_displays_paths_without_saving_or_requiring_drive(self):
        replacements = {'detected_optical_drives': lambda: [],
                        'OUTPUT': self.source.parent,
                        'JELLYFIN_KEY_FILE': Path(self.tmp.name) / 'missing-key'}
        with patch.dict(m, replacements), patch.object(m['readiness'], 'report', return_value=[]), patch.object(m['library_connection'], 'inspect', return_value={
                'ok': True, 'message': 'Connected to Jellyfin. Found 1 libraries.',
                'libraries': [{'name': '<Movies>', 'kind': 'movies', 'paths': ['/server/movies']}]}), patch.dict(m, {'save_settings': lambda values: self.fail('Connection check saved settings')}):
            response = self.client.post('/settings', data={'action': 'test-jellyfin', 'jellyfin_url': 'http://media', 'jellyfin_api_key': 'secret'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/server/movies', response.data)
        self.assertIn(b'&lt;Movies&gt;', response.data)
        self.assertIn(b'shared storage access is not', response.data)
        self.assertNotIn(b'value="secret"', response.data)
