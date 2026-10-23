import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recovery_control import (
    RecoveryControl,
    RecoveryControlError,
    capture_process_identity,
    observe_process_identity,
    resolve_recovery_identity,
)


def identity(step="step5"):
    return resolve_recovery_identity({"canonicalTaskId": "task-1", "runGroupId": "run-1", "batchId": "batch-1", "material": "木饰面"}, {
        "step": step,
        "runGroupId": "run-1",
        "batchId": "batch-1",
        "canonicalTaskId": "task-1",
        "outputTaskId": "video-1" if step == "step5" else "",
        "audioTaskId": "audio-1" if step == "step3" else "",
        "product": "木饰面",
        "material": "木饰面",
        "language": "俄语",
        "copyTextHash": "copy-hash" if step == "step3" else "",
        "voiceContractId": "voice-contract" if step == "step3" else "",
        "sourceVideoHash": "video-hash" if step == "step5" else "",
        "outputIdentity": "output.mp4",
    })


class RecoveryControlTests(unittest.TestCase):
    def setUp(self):
        # Simulated acceptance only; separate tests verify the real gate rejects absent evidence.
        acceptance = patch.object(RecoveryControl, 'acceptance_verified', return_value=True)
        acceptance.start()
        self.addCleanup(acceptance.stop)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.control = RecoveryControl(root / "state.json", root / "state.lock", root / "mailbox.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_failure_waits_thirty_seconds_and_two_claims_are_distinct(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failed = {"failureEventId": "f1", "failureFingerprint": "same", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failed)
        not_due = self.control.claim_recovery(ident, "automatic", failure=failed)
        self.assertEqual(not_due["code"], "recovery_not_due")
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        first = self.control.claim_recovery(ident, "automatic", failure=failed)
        self.assertTrue(first["accepted"])
        self.control.record_execution(first["command"]["commandId"], {"reason": "test"}, "failed")
        second_failure = {"failureEventId": "f2", "failureFingerprint": "different", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, second_failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        second = self.control.claim_recovery(ident, "automatic", failure=second_failure)
        self.assertTrue(second["accepted"])
        self.assertNotEqual(first["command"]["commandId"], second["command"]["commandId"])
        self.assertEqual(second["command"]["automaticAttemptCount"], 2)
        self.control.record_execution(second["command"]["commandId"], {"reason": "test"}, "failed")
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.control.claim_recovery(ident, "automatic", failure=second_failure)["code"], "recovery_budget_exhausted")

    def test_same_command_is_reused_after_restart(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {"failureEventId": "f1", "failureFingerprint": "same", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        first = self.control.claim_recovery(ident, "automatic", failure=failure)
        again = self.control.claim_recovery(ident, "automatic", failure=failure)
        self.assertEqual(first["command"]["commandId"], again["command"]["commandId"])
        self.assertTrue(again["reused"])

    def test_same_failure_event_cannot_spend_a_second_budget_slot(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {
            "failureEventId": "same-event",
            "failureFingerprint": "same-fingerprint",
            "failedAt": datetime.now(timezone.utc).isoformat(),
            "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY",
        }
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        command = self.control.claim_recovery(ident, "automatic", failure=failure)
        self.control.record_execution(command["command"]["commandId"], {"result": "failed"}, "failed")
        retry = self.control.claim_recovery(ident, "automatic", failure=failure)
        self.assertEqual(retry["code"], "recovery_failure_already_claimed")

    def test_duplicate_acceptance_is_observation_not_execution(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {
            "failureEventId": "accept-event",
            "failureFingerprint": "accept-fingerprint",
            "failedAt": datetime.now(timezone.utc).isoformat(),
            "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY",
        }
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        command = self.control.claim_recovery(ident, "automatic", failure=failure)["command"]
        first = self.control.accept_command(command["commandId"], {"pid": 1, "processStartTime": 1.0})
        second = self.control.accept_command(command["commandId"], {"pid": 2, "processStartTime": 2.0})
        self.assertEqual(first["decision"], "execute_new")
        self.assertEqual(second["decision"], "observe_existing")

    def test_manual_claim_uses_same_entry_without_consuming_automatic_budget(self):
        ident = identity()
        self.control.set_session_paused({}, True)
        failure = {
            "failureEventId": "f-manual",
            "failureFingerprint": "manual-failure",
            "failedAt": datetime.now(timezone.utc).isoformat(),
            "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY",
        }
        self.control.observe_failure(ident, failure)
        claim = self.control.claim_recovery(ident, "manual", failure=failure)
        self.assertTrue(claim["accepted"])
        self.assertEqual(claim["command"]["automaticAttemptCount"], 0)
        self.assertEqual(self.control.status()["entries"][ident["stableRecoveryKey"]]["automaticAttemptCount"], 0)

    def test_corrupt_state_fails_closed(self):
        self.control.state_path.write_text("{broken", encoding="utf-8")
        status = self.control.status()
        self.assertIn("readError", status)
        with self.assertRaises(RecoveryControlError):
            self.control.set_session_paused({}, False)

    def test_missing_identity_never_falls_back_to_parent(self):
        result = resolve_recovery_identity({"taskId": "parent"}, {"step": "step5", "childTaskId": ""})
        self.assertFalse(result["verified"])
        self.assertIn("recovery_identity_unverified", result["error"])

    def test_multi_language_audio_batch_contract_is_verified_without_pending_copy_hash(self):
        result = resolve_recovery_identity({
            "canonicalTaskId": "parent-1", "runGroupId": "run-1", "batchId": "batch-1",
            "material": "碳岩板",
        }, {
            "step": "step3",
            "runGroupId": "run-1", "batchId": "batch-1", "canonicalTaskId": "parent-1",
            "audioBatchId": "9.11",
            "audioTaskId": "voice-batch-v1|parent-1|9.11|abcd",
            "audioBatchContractId": "voice-batch-v1|contract-hash",
            "product": "碳岩板", "material": "碳岩板",
            "languageSet": "es:4,pt:2,id:2",
            "voiceContractId": "voice-plan-v1|contract-hash",
            "copyContractState": "copy-plan-pending-v1|contract-hash",
        })
        self.assertTrue(result["verified"])
        self.assertEqual(result["contractScope"], "audio_batch")
        self.assertIn("voice-batch-v1|contract-hash", result["stableRecoveryKey"])

    def test_mailbox_write_failure_keeps_claim_for_reconciliation(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {"failureEventId": "f1", "failureFingerprint": "f1", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        import recovery_control
        original_write = recovery_control._write_json
        calls = {"count": 0}
        def fail_mailbox(path, payload):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk busy")
            return original_write(path, payload)
        with patch("recovery_control._write_json", side_effect=fail_mailbox):
            with self.assertRaises(RecoveryControlError) as caught:
                self.control.claim_recovery(ident, "automatic", failure=failure)
        self.assertEqual(caught.exception.code, "recovery_dispatch_pending")
        durable = self.control.status()
        entry = durable["entries"][ident["stableRecoveryKey"]]
        self.assertEqual(entry["automaticAttemptCount"], 1)
        self.assertTrue(entry["activeCommandId"])

    def test_unknown_dead_command_is_reconciliation_required(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {"failureEventId": "f1", "failureFingerprint": "f1", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        command = self.control.claim_recovery(ident, "automatic", failure=failure)["command"]
        result = self.control.reconcile_pending_commands(lambda _command: {"status": "unknown"})
        self.assertEqual(result[0]["status"], "reconciliation_required")
        self.assertEqual(self.control.status()["commands"][command["commandId"]]["state"], "reconciliation_required")

    def test_accept_is_single_execution_and_terminal_cannot_regress(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {"failureEventId": "f-accept", "failureFingerprint": "f-accept", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        command = self.control.claim_recovery(ident, "automatic", failure=failure)["command"]
        first = self.control.accept_command(command["commandId"], {"pid": 1, "processStartTime": 1.0})
        second = self.control.accept_command(command["commandId"], {"pid": 2, "processStartTime": 2.0})
        self.assertEqual(first["decision"], "execute_new")
        self.assertEqual(second["decision"], "observe_existing")
        self.control.record_execution(command["commandId"], {"result": "done"}, "completed")
        late = self.control.record_execution(command["commandId"], {"result": "late"}, "running")
        self.assertTrue(late.get("ignored"))
        self.assertEqual(self.control.status()["commands"][command["commandId"]]["state"], "completed")

    def test_reconciliation_required_blocks_new_claim(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {"failureEventId": "f-reconcile", "failureFingerprint": "f-reconcile", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        command = self.control.claim_recovery(ident, "automatic", failure=failure)["command"]
        self.control.record_execution(command["commandId"], {"reason": "unknown"}, "reconciliation_required")
        again = self.control.claim_recovery(ident, "automatic", failure=failure)
        self.assertTrue(again["reused"])
        self.assertEqual(again["command"]["commandId"], command["commandId"])
        closed = self.control.record_execution(command["commandId"], {"verified": True}, "completed")
        self.assertFalse(closed.get("ignored", False))

    def test_different_step5_child_request_survives_old_command_reconciliation(self):
        old_identity = identity()
        new_key = old_identity["stableRecoveryKey"].replace("video-1", "video-2")
        old_command = {
            "commandId": "old-capcut-command",
            "stableRecoveryKey": old_identity["stableRecoveryKey"],
            "taskId": "task-1",
            "step": "step5",
            "trigger": "manual",
            "state": "reconciliation_required",
        }
        state = self.control.status()
        state["commands"][old_command["commandId"]] = old_command
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        request = {
            "commandId": "continue-new-child",
            "taskId": "task-1",
            "step": "step5",
            "stableRecoveryKey": new_key,
            "requestedAt": datetime.now(timezone.utc).isoformat(),
        }
        self.control.enqueue_request(request)

        self.assertIsNone(self.control.deliver_next_request())
        request_path = self.control.state_path.parent / "recovery-requests" / "continue-new-child.json"
        queued = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(queued["state"], "requested")
        self.assertFalse(self.control.mailbox_path.exists())

        receipt = {"status": "failed", "processObservation": "exited"}
        self.control.record_execution(old_command["commandId"], receipt, "failed")
        delivered = self.control.deliver_next_request()
        self.assertEqual(delivered["commandId"], "continue-new-child")
        self.assertEqual(delivered["state"], "delivered")
        mailbox = json.loads(self.control.mailbox_path.read_text(encoding="utf-8"))
        self.assertEqual(mailbox["commandId"], "continue-new-child")

    def test_legacy_observed_request_is_rearmed_after_different_child_failed(self):
        old_identity = identity()
        new_key = old_identity["stableRecoveryKey"].replace("video-1", "video-2")
        old_command = {
            "commandId": "old-capcut-command",
            "stableRecoveryKey": old_identity["stableRecoveryKey"],
            "taskId": "task-1",
            "step": "step5",
            "trigger": "manual",
            "state": "failed",
            "executionReceipt": {"status": "failed", "processObservation": "exited"},
        }
        state = self.control.status()
        state["commands"][old_command["commandId"]] = old_command
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        request = {
            "commandId": "continue-observed-child",
            "taskId": "task-1",
            "step": "step5",
            "stableRecoveryKey": new_key,
            "requestedAt": datetime.now(timezone.utc).isoformat(),
        }
        self.control.enqueue_request(request)
        request_path = self.control.state_path.parent / "recovery-requests" / "continue-observed-child.json"
        observed = json.loads(request_path.read_text(encoding="utf-8"))
        observed.update({
            "state": "observed",
            "observedCommandId": old_command["commandId"],
            "observedAt": datetime.now(timezone.utc).isoformat(),
        })
        request_path.write_text(json.dumps(observed), encoding="utf-8")

        delivered = self.control.deliver_next_request()
        self.assertEqual(delivered["commandId"], "continue-observed-child")
        self.assertEqual(delivered["state"], "delivered")
        saved = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "delivered")
        self.assertEqual(saved["requeuedAfterCommandId"], old_command["commandId"])

    def _orphan_setup(self, command_state, owner):
        # 10-23: step12 resume completed at 00:49, its owner died at 00:52 and
        # the mailbox stayed "accepted", so the 08:22 request was never delivered.
        state = self.control.status()
        state["commands"]["continue-af9212dd0308"] = {
            "commandId": "continue-af9212dd0308", "taskId": "task-1", "step": "step12",
            "stableRecoveryKey": "v1|step12|old", "state": command_state,
        }
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.control.mailbox_path.write_text(json.dumps({
            "commandId": "continue-af9212dd0308", "taskId": "task-1", "state": "accepted",
            "phase": "executing_task_recovery", "ownerProcessIdentity": owner,
        }), encoding="utf-8")
        self.control.enqueue_request({
            "commandId": "continue-fc09106e185e", "taskId": "task-1", "step": "step3",
            "stableRecoveryKey": "v5|step3|new",
            "requestedAt": datetime.now(timezone.utc).isoformat(),
        })

    def test_completed_command_with_exited_owner_releases_mailbox(self):
        dead_owner = {"pid": 14028, "processStartTime": 1792628420.1456947}
        self._orphan_setup("completed", dead_owner)
        with patch("recovery_control.observe_process_identity", return_value="exited"):
            delivered = self.control.deliver_next_request()
        self.assertEqual(delivered["commandId"], "continue-fc09106e185e")
        mailbox = json.loads(self.control.mailbox_path.read_text(encoding="utf-8"))
        self.assertEqual(mailbox["commandId"], "continue-fc09106e185e")

    def test_orphan_close_requires_terminal_command_and_proved_exit(self):
        cases = (("accepted", "exited"), ("completed", "matched_alive"), ("completed", "unverifiable"))
        for command_state, observation in cases:
            with self.subTest(command_state=command_state, owner=observation):
                self.setUp()
                self._orphan_setup(command_state, {"pid": 14028, "processStartTime": 1.0})
                with patch("recovery_control.observe_process_identity", return_value=observation):
                    self.assertIsNone(self.control.deliver_next_request())
                mailbox = json.loads(self.control.mailbox_path.read_text(encoding="utf-8"))
                self.assertEqual((mailbox["commandId"], mailbox["state"]),
                                 ("continue-af9212dd0308", "accepted"))

    def test_process_identity_uses_start_time_and_rejects_missing_identity(self):
        current = capture_process_identity()
        self.assertEqual(observe_process_identity(current), "matched_alive")
        self.assertEqual(observe_process_identity({"pid": current.get("pid")}), "unverifiable")

    def test_resource_guard_is_checked_inside_claim_without_consuming_budget(self):
        ident = identity()
        self.control.set_session_paused({}, False)
        failure = {"failureEventId": "f-resource", "failureFingerprint": "f-resource", "failedAt": datetime.now(timezone.utc).isoformat(), "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY"}
        self.control.observe_failure(ident, failure)
        state = self.control.status()
        state["entries"][ident["stableRecoveryKey"]]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.control.state_path.write_text(json.dumps(state), encoding="utf-8")
        blocked = self.control.claim_recovery(ident, "automatic", failure=failure, resource_guard=lambda: True)
        self.assertFalse(blocked["accepted"])
        self.assertEqual(blocked["code"], "recovery_resources_busy")
        self.assertEqual(self.control.status()["entries"][ident["stableRecoveryKey"]]["automaticAttemptCount"], 0)


if __name__ == "__main__":
    unittest.main()
