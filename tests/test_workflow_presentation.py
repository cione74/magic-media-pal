"""Plain-language summaries must reflect real queue states without mutating them."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'rootfs/opt/dexter-ripper'))
import workflow_presentation as presentation


def entry(identifier,name,status,batch='tv',destination='staging'):
    return {'id':identifier,'batch_id':batch,'status':status,'plan':{'title':'technical filename',
        'destination':destination,'episode':{'number':int(identifier[-1]),'name':name}}}


class WorkflowPresentationTests(unittest.TestCase):
    def test_finished_current_and_waiting_are_separate_from_stage_percentage(self):
        queue=[entry('old1','Old disc','completed','old'),entry('e1','Pilot','completed'),
               entry('e2','Notes from the Underground','running'),entry('e3','Dislocations','queued')]
        job={'queue_id':'e2','running':True,'description':'Encode video','stage':'encoding','progress':{'percent':25}}
        original=copy.deepcopy((queue,job))
        result=presentation.batch(queue,job)
        self.assertEqual((result['done'],result['total']),(1,3))
        self.assertEqual([r['status'] for r in result['rows']],['Ready for review','Making a smaller copy','Waiting'])
        self.assertEqual(result['current']['name'],'Notes from the Underground')
        self.assertEqual([s['state'] for s in result['steps']],['done','current','waiting'])
        self.assertNotIn('percent',result)
        self.assertEqual((queue,job),original)

    def test_failure_and_paused_copies_are_not_reported_as_finished(self):
        result=presentation.batch([entry('e1','Pilot','failed'),entry('e2','Next','paused')],{'queue_id':'e1'})
        self.assertEqual(result['done'],0)
        self.assertEqual(result['rows'][0]['status'],'Needs attention')
        self.assertEqual(result['rows'][1]['status'],'Paused until the issue is resolved')
        self.assertIsNone(result['current'])

    def test_submitted_batch_appears_during_handoff_without_previous_batch(self):
        result=presentation.batch([entry('e1','Previous','completed','old'),entry('e2','Current','queued')],{},'tv')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['current']['name'],'Current')
        self.assertEqual(result['rows'][0]['status'],'Waiting')

    def test_completed_jellyfin_copy_is_distinct_from_local_review(self):
        result=presentation.batch([entry('e1','Pilot','completed',destination='jellyfin')],{'queue_id':'e1'})
        self.assertEqual(result['rows'][0]['status'],'Added to Jellyfin')
        self.assertEqual(result['done'],result['total'])
        self.assertTrue(all(step['state']=='done' for step in result['steps']))

    def test_empty_queue_does_not_invent_a_batch(self):
        self.assertIsNone(presentation.batch([],{}))
        self.assertIsNone(presentation.batch([entry('e1','Old','completed')],{}))
