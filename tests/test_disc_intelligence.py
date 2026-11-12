"""Disc identity evidence, provider isolation and offline behavior."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from werkzeug.datastructures import MultiDict
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import disc_intelligence as intelligence
from disc_providers import ProviderHTTP, TVmaze, TheDiscDb, TVMAZE_LICENSE
import workflow_wizard


def tour_disc():
    return {'kind':'video','type':'DVD disc','title':'Tour of Duty - Season 1 Disc 1','label':'DVD_VIDEO',
            'titles':[{'id':i,'duration':duration,'chapters':'7','size':'1.4 GB'} for i,duration in
                      enumerate(['0:46:13','0:47:39','0:48:09','0:47:55','0:47:53'])]}


class Series:
    def search(self, name):
        return [{'id':1765,'name':'Tour of Duty','source':'https://www.tvmaze.com/shows/1765','license':TVMAZE_LICENSE}]
    def orders(self, show_id):
        return []
    def episodes(self, show_id, order=None):
        return [{'season':1,'number':i+1,'name':name,'provider':'TVmaze','source':'https://www.tvmaze.com/episodes/'+str(i),
                 'license':TVMAZE_LICENSE} for i,name in enumerate(['Pilot','Notes from the Underground','Dislocations','War Lover','Sitting Ducks'])]


class Physical:
    def lookup(self, fingerprints):
        return []


class DiscIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.disc = tour_disc()
        self.fp = [{'type':'libdvdread-discid','value':'a'*32}]

    def identify(self, **kwargs):
        return intelligence.enrich(self.disc,self.root,fingerprints=self.fp,metadata_provider=Series(),physical_provider=Physical(),**kwargs)

    def test_episode_sequence_never_becomes_verified_mapping(self):
        result = self.identify()
        self.assertEqual(result['series'],'Tour of Duty')
        self.assertEqual(len(result['mappings']),5)
        self.assertTrue(all(row['confidence']=='unverified' and row['state']=='proposed' for row in result['mappings'].values()))
        self.assertEqual(self.disc,tour_disc())
        result = self.identify(disc_number=2)
        self.assertEqual(result['mappings'],{})

    def test_confirmed_mapping_reused_only_for_same_physical_id_and_inventory(self):
        result = self.identify()
        form=MultiDict({'series_name':'Tour of Duty','identity_season':'1','episode_number_0':'2',
                        'episode_name_0':'Notes from the Underground','confirm_episode_0':'yes'})
        corrected=intelligence.correct(result,form)
        intelligence.MappingStore(self.root).save(corrected)
        restored=self.identify()
        self.assertEqual(restored['mappings']['0']['number'],2)
        self.assertEqual(restored['mappings']['0']['state'],'user_confirmed')
        self.assertTrue(restored['user_corrections'])
        self.fp[0]['value']='b'*32
        self.assertEqual(self.identify()['mappings']['0']['state'],'proposed')
        self.fp[0]['value']='a'*32
        self.disc['titles'][0]['duration']='0:01:00'
        self.assertEqual(self.identify()['mappings']['0']['state'],'proposed')

    def test_correction_can_be_removed_and_scan_only_identity_is_not_reused(self):
        result=self.identify()
        form=MultiDict({'episode_number_0':'1','episode_name_0':'Pilot','confirm_episode_0':'yes'})
        confirmed=intelligence.correct(result,form)
        removed=intelligence.correct(confirmed,MultiDict({'episode_number_0':''}))
        self.assertNotIn('0',removed['mappings'])
        intelligence.MappingStore(self.root).save(removed)
        self.assertNotIn('0',self.identify()['mappings'])
        result['fingerprints']=[]
        intelligence.MappingStore(self.root).save(result)
        self.assertIsNone(intelligence.MappingStore(self.root).load(result))

    def test_provider_failures_do_not_block_identification_or_ripping(self):
        class Broken(Series):
            def search(self,name): raise ValueError('network unavailable')
        class BadDisc(Physical):
            def lookup(self,fingerprints): return [{'titles':'malformed'}]
        result=intelligence.enrich(self.disc,self.root,fingerprints=self.fp,metadata_provider=Broken(),physical_provider=BadDisc())
        self.assertEqual(len(result['titles']),5)
        self.assertEqual(result['mappings'],{})
        self.assertTrue(result['provider_status'])
        unknown=intelligence.base_identity({'kind':'video','type':'Blu-ray','titles':[]})
        self.assertEqual(unknown['confidence'],'unknown')

    def test_ambiguous_show_match_is_not_selected_automatically(self):
        class Ambiguous(Series):
            def search(self,name): return super().search(name)*2
        result=intelligence.enrich(self.disc,self.root,fingerprints=self.fp,metadata_provider=Ambiguous(),physical_provider=Physical())
        self.assertEqual(result['episode_candidates'],[])
        self.assertEqual(result['mappings'],{})

    def test_offline_uses_saved_provider_responses_and_unknown_query_never_calls_network(self):
        http=ProviderHTTP(self.root,online=False)
        url='https://api.tvmaze.com/search/shows?q=Tour+of+Duty'
        http.cache.put(url,{'data':[{'show':{'id':1765,'name':'Tour of Duty'}}],'fetched_at':0})
        with patch('disc_providers.urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            self.assertEqual(TVmaze(http).search('Tour of Duty')[0]['id'],1765)
            with self.assertRaises(ValueError):TVmaze(http).search('Unknown')

    def test_cache_failure_and_graphql_error_shape_are_independent(self):
        http=ProviderHTTP(self.root)
        with patch.object(http,'get',return_value={'errors':[{'message':'no'}]}):
            with self.assertRaises(ValueError):TheDiscDb(http).lookup(self.fp)
        with patch.object(http,'get',return_value=[{'global_disc_id':'b'*32}]):
            self.assertEqual(TheDiscDb(http).lookup(self.fp),[])

    def test_new_season_does_not_reuse_previously_confirmed_episode_mapping(self):
        result=self.identify()
        confirmed=intelligence.correct(result,MultiDict({'episode_number_0':'1','confirm_episode_0':'yes'}))
        intelligence.MappingStore(self.root).save(confirmed)
        changed=self.identify(season=2,query='Tour of Duty')
        self.assertEqual(changed['mappings'],{})
        edited=intelligence.correct(confirmed,MultiDict({'identity_season':'2','episode_number_0':'1','confirm_episode_0':'yes'}))
        self.assertEqual(edited['mappings']['0']['state'],'proposed')

    def test_physical_provider_failure_keeps_series_metadata_available(self):
        class Down(Physical):
            def lookup(self,fingerprints): raise ValueError('unavailable')
        result=intelligence.enrich(self.disc,self.root,fingerprints=self.fp,metadata_provider=Series(),physical_provider=Down())
        self.assertEqual(len(result['episode_candidates']),5)
        self.assertTrue(all(row['confidence']=='unverified' for row in result['mappings'].values()))

    def test_rate_limit_has_one_bounded_retry_and_records_attribution(self):
        class Response(io.BytesIO):
            url='https://api.tvmaze.com/search/shows?q=Tour+of+Duty'
        http=ProviderHTTP(self.root)
        error=urllib.error.HTTPError(Response.url,429,'Busy',{'Retry-After':'1'},None)
        with patch('disc_providers.urllib.request.build_opener') as build, \
                patch('disc_providers.time.sleep'), patch.dict(ProviderHTTP.gates,{},clear=True):
            build.return_value.open.side_effect=[error,Response(b'[]')]
            self.assertEqual(http.get(Response.url,'TVmaze',TVMAZE_LICENSE),[])
            self.assertEqual(build.return_value.open.call_count,2)
        saved=http.cache.get(Response.url)
        self.assertEqual(saved['license'],TVMAZE_LICENSE)
        self.assertEqual(saved['provider'],'TVmaze')

    def test_long_retry_after_defers_without_blocking_or_repeated_requests(self):
        http=ProviderHTTP(self.root)
        url='https://api.tvmaze.com/search/shows?q=Missing'
        error=urllib.error.HTTPError(url,429,'Busy',{'Retry-After':'600'},None)
        with patch('disc_providers.urllib.request.build_opener') as build, \
                patch('disc_providers.time.sleep'), patch.dict(ProviderHTTP.gates,{},clear=True):
            build.return_value.open.side_effect=error
            for _ in range(2):
                with self.assertRaises(ValueError):http.get(url,'TVmaze',TVMAZE_LICENSE)
            self.assertEqual(build.return_value.open.call_count,1)

    def test_output_names_are_safe_and_reordering_is_explicit(self):
        filename=intelligence.episode_filename('../Series/Name',1,2,'A/B: C')
        self.assertNotIn('/',filename)
        self.assertIn('S01E02',filename)
        fields={'title_ids':['0','1'],'title_order_0':'2','title_order_1':'1'}
        self.assertEqual([t['id'] for t in workflow_wizard.titles_for(self.disc,fields)],[1,0])

    def test_old_saved_tv_scan_can_enter_episode_planning_without_rescan(self):
        draft=workflow_wizard.new_draft(self.disc,'Tour of Duty',{})
        self.assertEqual(draft['fields']['content_kind'],'tv')
        self.assertEqual(draft['fields']['series_name'],'Tour of Duty')
        self.assertEqual(draft['fields']['title_ids'],[])

    def test_unchecked_confirmation_is_cleared_in_draft(self):
        draft={'fields':{'confirm_episode_0':'yes'}}
        fields=workflow_wizard.merge(draft,MultiDict({'mapping_present':'yes','episode_number_0':'1'}))
        self.assertEqual(fields['confirm_episode_0'],'')
