"""Wizard navigation, preflight and persistent queue integration."""
import copy
import ast
import os
import threading
import time
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_encoding_app import m


def scan_raw(duplicate=False):
    lines = ['SINFO:0,0,5,0,"V_MPEG4/ISO/AVC"', 'SINFO:0,0,7,0,"AVC H.264"',
             'SINFO:0,1,1,0,"Audio"', 'SINFO:0,1,3,0,"eng"',
             'SINFO:0,1,5,0,"A_AC3"', 'SINFO:0,1,14,0,"6"']
    if duplicate:
        lines += [line.replace('0,1,', '0,2,') for line in lines[2:]]
    return '\n'.join(lines)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.disc = {'kind': 'video', 'title': 'Example', 'label': 'EXAMPLE', 'type': 'Blu-ray',
                     'raw': scan_raw(), 'titles': [
                         {'id': 0, 'name': 'Example', 'duration': '1:40:00', 'size': '1 GB',
                          'source': '00100.mpls', 'recommendation': 'MAIN FEATURE CANDIDATE'},
                         {'id': 1, 'name': 'Extra', 'duration': '0:10:00', 'size': '100 MB',
                          'source': '00001.m2ts', 'category': 'Extra'}]}
        m['state'] = {'disc': copy.deepcopy(self.disc), 'job': {'running': False, 'log': [], 'outputs': []}}
        m['settings'] = {'setup_complete': True}
        m['CONFIG_ROOT'] = self.root
        m['VIDEO_OUTPUT'] = self.root
        m['persist_state_now'] = lambda **kwargs: None
        self.client = m['app'].test_client()
        self.client.get('/wizard')

    def submit(self, action, **fields):
        draft = m['state']['wizard_draft']
        return self.client.post('/wizard', data={
            'draft_id': draft['id'], 'revision': str(draft['revision']),
            'step': str(draft['step']), 'action': action, **fields})

    def to_review(self):
        self.submit('next')
        self.submit('next', titles_present='yes', title_ids=['0'])
        self.submit('next', plan_present='yes', workflow_surround='yes',
                    movie_name_0='My Movie', movie_year_0='2004')

    def preflight(self):
        draft = m['state']['wizard_draft']
        with patch.object(m['shutil'], 'disk_usage', return_value=SimpleNamespace(free=100*1024**3)), patch.dict(m, {'configured_drive': lambda: '/dev/null',
                            'optical_drive_status': lambda path: m['CDS_DISC_OK']}), patch.object(m['encoding'], 'cached_probe', return_value={'available': True, 'encoders': ['x265']}), patch.object(m['encoding'], 'nvenc_probe', return_value={}):
            return m['wizard_preflight'](m['state']['disc'], draft['fields'], draft['disc_id'])

    def test_episode_panel_keeps_unverified_assignments_and_manual_controls(self):
        identity=m['disc_intelligence'].base_identity(self.disc)
        identity.update(series='Example Series',season=1,disc_number=1,
            mappings={'0':{'number':1,'season':1,'name':'Pilot','state':'proposed','confidence':'unverified',
                           'evidence':'Episode-list order only'}})
        m['state']['disc']['identity']=identity
        draft=m['workflow_wizard'].new_draft(m['state']['disc'],'Example Series',m['settings'])
        draft['step']=2
        m['state']['wizard_draft']=draft
        response=m['app'].test_client().get('/wizard')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('Unverified episode suggestion',html)
        self.assertIn('episode_number_0',html)
        self.assertIn('Save episode choices',html)
        self.assertIn('Select all titles',html)
        self.assertIn('Move down',html)
        self.assertNotIn('Main feature candidate</span>',html)

    def test_saved_suggestions_can_be_explicitly_confirmed_from_plan_without_rescan(self):
        disc=copy.deepcopy(self.disc)
        identity=m['disc_intelligence'].base_identity(disc)
        identity.update(series='Example Series',season=1,disc_number=1,
            mappings={str(i):{'number':i+1,'season':1,'name':name,'state':'proposed','confidence':'unverified'}
                      for i,name in enumerate(['Pilot','Second Episode'])})
        disc['identity']=identity
        m['state']['disc']=disc
        draft=m['workflow_wizard'].new_draft(disc,'Example Series',m['settings'])
        draft['step']=2
        draft['fields'].update(title_ids=['0'],episode_number_0='1',episode_name_0='Pilot',
                              episode_number_1='2',episode_name_1='Second Episode')
        m['state']['wizard_draft']=draft
        self.submit('save-mappings',series_name='Example Series',identity_season='1',identity_disc='1',
                    episode_number_0='1',episode_name_0='Pilot',episode_number_1='2',episode_name_1='Second Episode')
        self.assertEqual(m['state']['disc']['identity']['mappings']['0']['state'],'proposed')
        self.submit('next')
        self.assertEqual(m['state']['wizard_draft']['step'],3)
        html=self.client.get('/wizard').get_data(as_text=True)
        self.assertIn('Confirm these episode assignments',html)
        self.assertIn('Prepare your episodes',html)
        with patch.dict(os.environ,{'DEXTER_JELLYFIN_EXPORT':'test:/share'}):
            self.submit('next',workflow_destination='jellyfin')
            self.assertEqual(m['state']['wizard_draft']['step'],3)
            self.assertIn('saved as suggestions',m['state']['wizard_draft']['errors'][0])
            self.submit('confirm-mappings')
            self.assertEqual(m['state']['disc']['identity']['mappings']['0']['state'],'user_confirmed')
            self.assertEqual(m['state']['disc']['identity']['mappings']['1']['state'],'proposed')
            fields=m['state']['wizard_draft']['fields']
            self.assertEqual(fields['confirm_episode_0'],'yes')
            plan=m['wizard_plans'](m['state']['disc'],fields)[0]['plan']
            self.assertEqual(plan['episode']['name'],'Pilot')
            self.assertEqual(plan['destination'],'jellyfin')
        self.assertFalse(m['state']['job']['running'])

    def test_unconfirmed_episode_can_stay_local_but_cannot_silently_enter_movies(self):
        disc=copy.deepcopy(self.disc)
        disc['identity']=m['disc_intelligence'].base_identity(disc)
        disc['identity']['series']='Example Series'
        draft=m['workflow_wizard'].new_draft(disc,'Example Series',m['settings'])
        fields=draft['fields']
        fields.update(title_ids=['0'],workflow_quality='original',workflow_container='preserve',
                      workflow_destination='staging',content_kind='tv')
        plan=m['wizard_plans'](disc,fields)[0]['plan']
        self.assertNotIn('episode',plan)
        self.assertIn('Title 0',plan['title'])
        fields['workflow_destination']='jellyfin'
        with self.assertRaisesRegex(ValueError,'episode assignment'):
            m['wizard_plans'](disc,fields)

    def test_confirmed_episode_plan_freezes_assignment_and_filename(self):
        disc=copy.deepcopy(self.disc)
        identity=m['disc_intelligence'].base_identity(disc)
        identity.update(series='Example Series',season=1,disc_number=1,
            mappings={'0':{'number':2,'season':1,'name':'Second Episode','state':'user_confirmed','confidence':'verified'}})
        disc['identity']=identity
        fields=m['workflow_wizard'].new_draft(disc,'Example Series',m['settings'])['fields']
        fields.update(title_ids=['0'],workflow_quality='original',workflow_container='preserve',workflow_destination='staging')
        plan=m['wizard_plans'](disc,fields)[0]['plan']
        self.assertEqual(plan['episode']['number'],2)
        self.assertEqual(plan['output_filename'],'Example Series - S01E02 - Second Episode.mkv')
        identity['mappings']['0']['number']=3
        self.assertEqual(plan['episode']['number'],2)

    def test_steps_keep_selections_and_form_values(self):
        self.assertIn(b'Disc scan complete', self.client.get('/wizard').data)
        self.submit('next')
        self.assertEqual(m['state']['wizard_draft']['step'], 2)
        self.submit('next', titles_present='yes')
        self.assertEqual(m['state']['wizard_draft']['step'], 2)
        self.assertTrue(m['state']['wizard_draft']['errors'])
        self.submit('next', titles_present='yes', title_ids=['0'])
        self.submit('next', plan_present='yes', workflow_surround='yes', movie_name_0='My Movie',
                    movie_year_0='2004', workflow_subtitles='no')
        html = self.client.get('/wizard').data
        self.assertIn(b'My Movie (2004)', html)
        self.assertIn(b'No', html)
        self.submit('back')
        self.assertEqual(m['state']['wizard_draft']['fields']['movie_name_0'], 'My Movie')
        self.submit('back')
        self.assertEqual(m['state']['wizard_draft']['fields']['title_ids'], ['0'])
        self.assertIn(b'checked', self.client.get('/wizard').data)

    def test_preferences_select_a_track_and_freeze_it_before_ripping(self):
        m['state']['disc']['raw'] = scan_raw(duplicate=True)
        self.client.get('/wizard')
        self.to_review()
        checks = self.preflight()
        self.assertFalse(any(c['status'] == 'blocked' for c in checks))
        self.assertTrue(any('Sound 1' in c['message'] for c in checks))
        draft = m['state']['wizard_draft']
        choices = m['movie_output'].scan_tracks(m['state']['disc']['raw'])[0]
        plans = m['wizard_plans'](m['state']['disc'], draft['fields'])
        policy = plans[0]['plan']['output_policy']
        self.assertEqual(policy['scan_audio'], choices[0]['selector'])
        self.assertEqual(policy['audio_selection'], 'automatic')
        draft['fields']['scan_audio_0'] = choices[1]['id']
        plans = m['wizard_plans'](m['state']['disc'], draft['fields'])
        self.assertEqual(plans[0]['plan']['output_policy']['scan_audio'], choices[1]['selector'])
        self.assertNotIn('audio_selection', plans[0]['plan']['output_policy'])

    def test_missing_scan_tracks_are_pending_not_fabricated_success(self):
        m['state']['disc']['raw'] = ''
        self.client.get('/wizard')
        self.to_review()
        checks = self.preflight()
        self.assertTrue(any(c['status'] == 'pending' and 'Track information' in c['message'] for c in checks))

    def test_changes_invalidate_test_plan_and_stale_pages_are_rejected(self):
        self.to_review()
        draft = m['state']['wizard_draft']
        draft['checks'] = [{'status': 'passed', 'name': 'Old', 'message': 'Old', 'step': 3}]
        revision = draft['revision']
        self.submit('back')
        self.submit('next', plan_present='yes', workflow_surround='yes', workflow_quality='original')
        self.assertEqual(m['state']['wizard_draft']['checks'], [])
        response = self.client.post('/wizard', data={
            'draft_id': draft['id'], 'revision': revision, 'action': 'start'})
        self.assertEqual(response.status_code, 409)

    def test_start_reruns_checks_and_persists_before_dispatch_and_is_idempotent(self):
        self.to_review()
        events = []
        def launch(ids, movie_workflow):
            events.append(('launch', len(m['state']['workflow_queue'])))
            m['state']['job'].update(running=True, queue_id=movie_workflow['queue_id'])
        with patch.dict(m, {'wizard_preflight': lambda *args: [],
                            'persist_state_now': lambda **kwargs: events.append(('save', len(m['state'].get('workflow_queue', [])))),
                            'start_video_rip': launch}):
            draft = copy.deepcopy(m['state']['wizard_draft'])
            self.submit('start')
            self.assertEqual(events[0], ('save', 1))
            self.assertTrue(any(event[0] == 'launch' for event in events))
            self.assertEqual(m['state']['workflow_queue'][0]['status'], 'running')
            self.client.post('/wizard', data={'draft_id': draft['id'], 'revision': draft['revision'], 'action': 'start'})
            self.assertEqual(len(m['state']['workflow_queue']), 1)

    def test_accepted_batch_shows_refreshing_progress_before_worker_starts(self):
        self.to_review()
        with patch.dict(m, {'wizard_preflight': lambda *args: [], 'dispatch_workflow_queue': lambda: None}):
            response=self.submit('start')
        self.assertEqual(response.status_code,302)
        self.assertTrue(m['state']['wizard_draft']['submitted'])
        self.assertFalse(m['state']['job']['running'])
        with patch.dict(m, {'collect_drive_diagnostics': lambda *args: {}}):
            home=self.client.get('/',follow_redirects=True)
        self.assertEqual(home.status_code,200)
        self.assertEqual(len(home.history),0)
        self.assertIn(b'Your workflow was accepted',home.data)
        self.assertIn(b'<meta http-equiv="refresh" content="3">',home.data)
        self.assertFalse(m['state']['job']['running'])
        wizard=self.client.get('/wizard')
        self.assertEqual(wizard.status_code,302)
        self.assertEqual(wizard.headers['Location'],'/')
        self.submit('start')
        self.assertEqual(len(m['state']['workflow_queue']),1)

    def test_active_queue_refreshes_and_terminal_queue_stops_refreshing(self):
        self.to_review()
        with patch.dict(m, {'wizard_preflight': lambda *args: [], 'dispatch_workflow_queue': lambda: None}):
            self.submit('start')
        self.assertIn(b'<meta http-equiv="refresh" content="3">',self.client.get('/workflow-queue').data)
        m['state']['workflow_queue'][0]['status']='completed'
        self.assertNotIn(b'<meta http-equiv="refresh" content="3">',self.client.get('/workflow-queue').data)

    def test_failed_encoding_resumes_verified_original_without_reading_disc(self):
        source=self.root/'episode.mkv'
        source.write_bytes(b'verified original')
        episode={'series':'Example','season':1,'number':1,'name':'Pilot'}
        plan={'title':'Example - S01E01 - Pilot','year':'','profile':'balanced','codec':'h265',
              'destination':'jellyfin','episode':episode}
        entry={'id':'failed-video','status':'failed','media_kind':'video','plan':plan}
        m['state']['workflow_queue']=[entry]
        job=m['state']['job']
        job.update(description='Encode video',status='Encoding failed: storage check',queue_id=entry['id'],
                   outputs=[{'path':str(source),'verification':{'ok':True},'episode':episode}])
        calls=[]
        def resume(current,path,frozen,after_encode):
            calls.append((path,frozen,after_encode))
            current['running']=True
        with patch.dict(m,{'continue_movie_workflow':resume,'_allowed_staged_video':lambda path:True,
                           'start_video_rip':lambda *a,**kw:self.fail('Read the disc again'),
                           'wizard_preflight':lambda *a:self.fail('Required another disc preflight')}):
            page=self.client.get('/workflow-queue')
            self.assertIn(b'Resume verified copy',page.data)
            response=self.client.post('/workflow-queue/failed-video/retry')
        self.assertEqual(response.status_code,302)
        self.assertEqual(calls,[(str(source),plan,False)])
        self.assertEqual(entry['status'],'running')
        self.assertEqual(source.read_bytes(),b'verified original')

    def test_restart_keeps_failed_resume_status_and_episode_assignment(self):
        import json
        source = self.root / 'retained.mkv'; source.write_bytes(b'original')
        episode = {'series':'Example','season':1,'number':1,'name':'Pilot'}
        entry = {'id':'restart-failed','status':'failed','media_kind':'video','plan':{'episode':episode}}
        job = {'running':False,'description':'Encode video','status':'Encoding failed: sample fault',
               'queue_id':entry['id'],'outputs':[{'path':str(source),'verification':{'ok':True},'episode':episode}]}
        saved = self.root / 'resume-state.json'
        saved.write_text(json.dumps({'schema':1,'state':{'job':job,'workflow_queue':[entry]}}))
        with patch.dict(m, {'DEXTER_STATE_FILE':saved, '_allowed_staged_video':lambda path:True,
                           'probe_media':lambda path:{'ok':True}}):
            m['restore_persistent_state']()
            restored=m['state']['job']
            self.assertTrue(restored['status'].startswith('Encoding failed'))
            self.assertEqual(restored['outputs'][0]['episode'],episode)
            self.assertEqual(m['staged_workflow_source'](restored,entry),source)
            entry['status']='needs_review';restored['status']='Encoding interrupted; original retained'
            self.assertEqual(m['staged_workflow_source'](restored,entry),source)
        self.assertEqual(source.read_bytes(),b'original')

    def test_blocked_preflight_cannot_start(self):
        self.to_review()
        with patch.dict(m, {'wizard_preflight': lambda *args: [{'name': 'Drive', 'status': 'blocked',
                        'message': 'Reconnect drive', 'step': 1}], 'start_video_rip': lambda *args, **kwargs: self.fail('Started a blocked job')}):
            self.submit('start')
        self.assertEqual(m['state'].get('workflow_queue', []), [])
        self.assertIn(b'Reconnect drive', self.client.get('/wizard').data)

    def test_multiple_titles_queue_separately_and_failure_pauses_siblings(self):
        self.submit('next')
        self.submit('next', titles_present='yes', title_ids=['0', '1'])
        self.submit('next', plan_present='yes', workflow_surround='yes', movie_name_0='Movie',
                    movie_name_1='Extra')
        launched = []
        def launch(ids, movie_workflow):
            launched.append(ids)
            m['state']['job'].update(running=True, queue_id=movie_workflow['queue_id'])
        with patch.dict(m, {'wizard_preflight': lambda *args: [], 'start_video_rip': launch}):
            self.submit('start')
            self.assertEqual(launched, [[0]])
            self.assertEqual([e['status'] for e in m['state']['workflow_queue']], ['running', 'queued'])
            m['finish_workflow_entry'](m['state']['job'], 'failed', 'Check this disc')
            m['state']['job']['running'] = False
            m['dispatch_workflow_queue']()
            self.assertEqual(launched, [[0]])
            self.assertEqual([e['status'] for e in m['state']['workflow_queue']], ['failed', 'paused'])

    def test_queue_advances_only_after_successful_terminal_stage(self):
        self.submit("next")
        self.submit("next", titles_present="yes", title_ids=["0", "1"])
        self.submit("next", plan_present="yes", workflow_surround="yes",
                    movie_name_0="Movie", movie_name_1="Extra")
        launched = []
        def launch(ids, movie_workflow):
            launched.append(ids)
            m["state"]["job"].update(running=True, queue_id=movie_workflow["queue_id"])
        with patch.dict(m, {"wizard_preflight": lambda *args: [], "start_video_rip": launch}):
            self.submit("start")
            m["dispatch_workflow_queue"]()
            self.assertEqual(launched, [[0]])
            m["finish_workflow_entry"](m["state"]["job"], "completed", "Verified in staging")
            m["state"]["job"]["running"] = False
            m["dispatch_workflow_queue"]()
            self.assertEqual(launched, [[0], [1]])

    def test_failed_persistence_does_not_start_or_leave_a_launchable_job(self):
        self.to_review()
        def failed_save(**kwargs):
            raise OSError("Configuration disk full")
        with patch.dict(m, {"wizard_preflight": lambda *args: [], "persist_state_now": failed_save,
                            "start_video_rip": lambda *args, **kwargs: self.fail("Started before saving")}):
            response = self.submit("start")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(m["state"]["workflow_queue"], [])
        self.assertNotIn("submitted", m["state"]["wizard_draft"])

    def test_scan_and_no_disc_states_do_not_enable_title_navigation(self):
        m["state"]["disc"] = None
        html = self.client.get("/wizard").data
        self.assertIn(b"Scan your disc", html)
        self.assertNotIn(b"Next: Choose Titles", html)
        m["state"]["job"].update(running=True, description="Disc scan", status="Reading disc")
        html = self.client.get("/wizard").data
        self.assertIn(b"Reading your disc", html)
        self.assertIn(b'http-equiv="refresh"', html)
        self.assertNotIn(b"Disc scan complete", html)

    def test_a_finished_workflow_allows_a_new_preparation(self):
        previous = m["state"]["wizard_draft"]["id"]
        m["state"]["wizard_draft"]["submitted"] = "old-batch"
        m["state"]["workflow_queue"] = [{"id": "old-job", "status": "completed"}]
        self.client.get("/wizard")
        self.assertNotEqual(m["state"]["wizard_draft"]["id"], previous)
        self.assertNotIn("submitted", m["state"]["wizard_draft"])

    def test_restored_scan_is_a_preflight_blocker_until_rescanned(self):
        self.to_review()
        m["state"]["scan_requires_refresh"] = True
        checks = self.preflight()
        self.assertTrue(any(c["status"] == "blocked" and c["name"] == "Disc scan" for c in checks))

    def test_changed_disc_replaces_draft_and_refuses_old_submission(self):
        old = copy.deepcopy(m['state']['wizard_draft'])
        m['state']['disc']['label'] = "Another disc"
        response = self.client.post('/wizard', data={'draft_id': old['id'],
                 'revision': old['revision'], 'action': 'next'})
        self.assertEqual(response.status_code, 409)
        self.assertNotEqual(m['state']['wizard_draft']['id'], old['id'])

    def test_queue_and_draft_restore_without_restarting_optical_work(self):
        self.to_review()
        entry = {'id': 'a'*32, 'batch_id': 'b'*32, 'status': 'running',
                 'plan': {'title': 'Movie'}}
        m['state']['workflow_queue'] = [entry]
        path = self.root / 'saved.json'
        path.write_text(json.dumps({'schema': m['DEXTER_STATE_SCHEMA'], 'state': m['state']}))
        m['state'] = {'job': {'running': False}}
        with patch.dict(m, {'DEXTER_STATE_FILE': path}):
            m['restore_persistent_state']()
        self.assertEqual(m['state']['workflow_queue'][0]['status'], 'needs_review')
        self.assertEqual(m['state']['wizard_draft']['step'], 4)


class StateWriteTests(unittest.TestCase):
    def test_concurrent_state_writes_leave_valid_atomic_snapshot(self):
        source = Path(__file__).resolve().parents[1] / "rootfs/opt/dexter-ripper/app.py"
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("persist_state_now", "_snapshot_state")]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scope = {"state": {"job": {"running": False}}, "lock": threading.Lock(),
                     "_persist_lock": threading.Lock(), "_persist_fingerprint": None,
                     "copy": copy, "json": json, "os": os, "time": time,
                     "DEXTER_STATE_SCHEMA": 1, "DEXTER_STATE_FILE": root / "state.json",
                     "DEXTER_STATE_TMP": root / ".state.tmp"}
            exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
            errors = []
            barrier = threading.Barrier(8)
            def writer():
                try:
                    barrier.wait(timeout=5)
                    for _ in range(4):
                        scope["persist_state_now"](force=True)
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=writer) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(json.loads((root / "state.json").read_text())["state"]["job"]["running"], False)


class MusicWizardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.audio = self.root / "audio"
        self.audio.mkdir()
        self.disc = {"kind": "audio", "type": "Audio CD", "title": "Audio CD", "raw": "CD scan",
                     "discid": "test-disc-id", "scan_id": "scan-1", "album": "Example Album",
                     "album_artist": "Example Artist", "year": "2004",
                     "tracks": [{"number": 2, "title": "First Song", "duration": "00:04:24.240"},
                                {"number": 4, "title": "Second Song", "duration": "00:03:00.000"}]}
        m["state"] = {"disc": copy.deepcopy(self.disc), "job": {"running": False, "outputs": [], "log": []}}
        m["settings"] = {"setup_complete": True}
        m["CONFIG_ROOT"] = self.root
        m["AUDIO_OUTPUT"] = self.audio
        m["VIDEO_OUTPUT"] = self.root
        m["MUSIC_ROOT"] = self.root / "music"
        m["persist_state_now"] = lambda **kwargs: None
        self.client = m["app"].test_client()
        self.client.get("/wizard")

    def submit(self, action, **fields):
        draft = m["state"]["wizard_draft"]
        return self.client.post("/wizard", data={"draft_id": draft["id"], "revision": draft["revision"],
                    "action": action, **fields})

    def to_review(self):
        self.submit("next")
        self.submit("next", tracks_present="yes", tracks=["4"])
        self.submit("next", album="My Album", artist="My Artist", album_year="2004",
                    track_name_4="My Song", audio_destination="staging")

    def test_cd_uses_four_steps_without_movie_encoding_controls(self):
        html = self.client.get("/wizard").data
        self.assertIn(b"Example Album", html)
        self.assertIn(b"Choose Tracks", html)
        self.submit("next")
        self.assertEqual(m["state"]["wizard_draft"]["fields"]["tracks"], [])
        self.submit("next", tracks_present="yes", tracks=["4"])
        html = self.client.get("/wizard").data
        self.assertIn(b"Prepare your album", html)
        self.assertIn(b"lossless FLAC", html)
        self.assertNotIn(b"Playback format", html)
        self.assertNotIn(b"Encoding device", html)

    def test_track_metadata_survives_navigation_and_review(self):
        self.to_review()
        html = self.client.get("/wizard").data
        self.assertIn(b"My Album", html)
        self.assertIn(b"My Artist", html)
        self.assertIn(b"Lossless FLAC", html)
        self.submit("back")
        fields = m["state"]["wizard_draft"]["fields"]
        self.assertEqual(fields["tracks"], ["4"])
        self.assertEqual(fields["track_name_4"], "My Song")
        self.assertEqual(fields["album"], "My Album")

    def test_music_preflight_checks_music_tools_and_tracks(self):
        self.to_review()
        draft = m["state"]["wizard_draft"]
        with patch.dict(m, {"configured_drive": lambda: "/dev/null",
                            "optical_drive_status": lambda path: m["CDS_DISC_OK"]}), patch.object(m["shutil"], "which", return_value="/usr/bin/tool"), patch.object(m["shutil"], "disk_usage", return_value=SimpleNamespace(free=10*1024**3)), patch.object(m["encoding"], "nvenc_probe", side_effect=AssertionError("CD checked GPU")):
            checks = m["wizard_preflight"](m["state"]["disc"], draft["fields"], draft["disc_id"])
        self.assertFalse(any(check["status"] == "blocked" for check in checks))
        self.assertTrue(any(check["name"] == "Music tools" for check in checks))
        self.assertTrue(any(check["status"] == "pending" for check in checks))
        draft["fields"]["tracks"] = ["3"]
        checks = m["wizard_preflight"](m["state"]["disc"], draft["fields"], draft["disc_id"])
        self.assertTrue(any(check["status"] == "blocked" for check in checks))

    def test_music_queue_dispatches_existing_cd_backend_with_physical_numbers(self):
        self.to_review()
        def launch(plan):
            self.assertEqual(plan["tracks"], [4])
            self.assertEqual(plan["codec"], "flac")
            m["state"]["job"].update(running=True, queue_id=plan["queue_id"])
        with patch.dict(m, {"wizard_preflight": lambda *args: [], "launch_audio_workflow": launch,
                            "start_video_rip": lambda *args, **kwargs: self.fail("CD launched a movie rip")}):
            self.submit("start")
        self.assertEqual(m["state"]["workflow_queue"][0]["media_kind"], "audio")
        self.assertEqual(m["state"]["workflow_queue"][0]["status"], "running")

    def test_audio_launch_passes_existing_scan_identity_and_metadata(self):
        self.to_review()
        fields = m["state"]["wizard_draft"]["fields"]
        plan = m["wizard_audio_plan"](m["state"]["disc"], fields)
        calls = []
        def rip(audio_workflow):
            calls.append((m["request"].form.get("scan_id"), m["request"].form.getlist("track")))
        with patch.dict(m, {"rip_audio": rip}):
            m["launch_audio_workflow"](plan)
        self.assertEqual(calls, [("scan-1", ["4"])])
        self.assertEqual(m["state"]["disc"]["album"], "My Album")
        self.assertEqual(m["state"]["disc"]["tracks"][1]["title"], "My Song")

    def test_generated_flac_is_verified_and_retained_in_staging(self):
        plan = {"title": "Album", "album": "Album", "artist": "Artist", "year": "",
                "tracks": [2], "track_titles": {"2": "Song"}, "destination": "staging",
                "queue_id": "a"*32, "scan_id": "scan-1"}
        directory = self.audio / ("cd-" + "b"*32) / "album"
        directory.mkdir(parents=True)
        source = directory / "02 - Song.flac"
        m["subprocess"].run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
              "anullsrc=r=44100:cl=stereo", "-t", "1", "-c:a", "flac", str(source)],
              check=True, capture_output=True, timeout=30)
        m["state"]["disc"].update(selected_tracks=[2], audio_job_dir="cd-" + "b"*32)
        m["state"]["job"].update(queue_id=plan["queue_id"], status="Completed", audio_workflow=plan)
        m["state"]["workflow_queue"] = [{"id": plan["queue_id"], "batch_id": "c"*32, "status": "running"}]
        m["continue_audio_workflow"](m["state"]["job"], plan)
        self.assertTrue(source.is_file())
        self.assertEqual(m["state"]["job"]["audio_report"]["tracks"], 1)
        self.assertEqual(m["state"]["workflow_queue"][0]["status"], "completed")
