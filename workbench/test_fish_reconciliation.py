import copy
import unittest
from fish_reconciliation import observe_failed_fish


class Tests(unittest.TestCase):
    def setUp(self):
        self.c = dict(commandId='c', taskId='t', step='step3', runGroupId='r', batchId='b', claimedAt='2026-09-12T03:32:25+00:00')
        self.m = dict(commandId='c', taskId='t', runGroupId='r', batchId='b', bridgePid=12)
        self.r = dict(pid=12, batchId='fish', canonicalTaskIds=['t'], taskIds=['en', 'es'], state='terminal', terminalStatus='failed', startedAt='2026-09-12T03:32:25.480Z')
        self.h = dict(canonicalTaskId='t', runGroupId='r', sourceBatchId='b', batchId='fish', taskIds=['en', 'es'], status='failed', completedAt='2026-09-12T03:34:05Z', errorCode='BRIDGE_HEARTBEAT_TIMEOUT')

    def result(self, absent=lambda pid: True):
        return observe_failed_fish(self.c, self.m, self.r, self.h, absent)

    def test_matching_failed_batch(self):
        self.assertEqual(self.result()['status'], 'failed')

    def test_live_or_unverifiable_process(self):
        self.assertEqual(self.result(lambda pid: False)['status'], 'unknown')

    def test_identity_conflicts(self):
        for source, field in [('m','commandId'),('m','bridgePid'),('h','canonicalTaskId'),('h','runGroupId'),('h','sourceBatchId'),('h','batchId'),('r','canonicalTaskIds'),('h','taskIds')]:
            with self.subTest(source=source,field=field):
                value = getattr(self, source); old = copy.deepcopy(value[field]); value[field] = [] if isinstance(old,list) else 'wrong'
                self.assertEqual(self.result()['status'], 'unknown'); value[field] = old

    def test_running_or_success_not_closed(self):
        for state in ['running', 'completed']:
            self.h['status'] = state
            self.assertEqual(self.result()['status'], 'unknown')

    def test_old_or_invalid_timestamp(self):
        for time in ['2026-09-11T03:00:00Z','bad',None]:
            self.r['startedAt'] = time
            self.assertEqual(self.result()['status'], 'unknown')

    def test_step5_not_affected(self):
        self.c['step'] = 'step5'
        self.assertEqual(self.result()['status'], 'unknown')

    def completed_fixture(self):
        self.r['terminalStatus'] = self.h['status'] = 'completed'
        self.h.update(total=2, completed=2, failed=0, completedItems=[dict(taskId=t, canonicalTaskId='t',
            copyTextHash='hash', copyId='copy', language=t, languageContractVerified=True,
            voiceVerified=True, expectedVoice='voice', actualActiveVoice='voice') for t in ['en','es']])

    def success_result(self, verify=lambda h:[{'verified': True} for i in h['completedItems']]):
        return observe_failed_fish(self.c,self.m,self.r,self.h,lambda pid:True,verify)

    def test_completed_requires_verified_artifacts(self):
        self.completed_fixture()
        self.assertEqual(self.success_result()['status'],'completed')
        def bad(h): raise ValueError('hash mismatch')
        self.assertEqual(self.success_result(bad)['status'],'unknown')

    def test_partial_duplicate_or_wrong_voice_blocks_success(self):
        for field,value in [('taskId','en'),('canonicalTaskId','other'),('voiceVerified',False),('actualActiveVoice','wrong')]:
            self.completed_fixture(); self.h['completedItems'][1][field]=value
            self.assertEqual(self.success_result()['status'],'unknown')
        self.completed_fixture(); self.h['completed']=1
        self.assertEqual(self.success_result()['status'],'unknown')


if __name__ == '__main__':
    unittest.main()
