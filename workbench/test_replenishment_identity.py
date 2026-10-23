import unittest

from task_inbox import inherit_replenishment_identity

PARENT_ID = "formal-batch-1792651512044-f9150342b5-07"
STATUSES = {"items": {PARENT_ID: {
    "taskId": PARENT_ID,
    "batchId": "formal-batch-1792651512044-f9150342b5",
    "runGroupId": "run-20261022144436-260a970d",
    "batchKind": "formal",
    "selectedProjects": ["ProjectA", "ProjectB"],
    "batchSequence": 7,
    "batchSize": 9,
}}}


def legacy_child(**extra):
    # Shape of the 10-22 23:43 children admitted before PR #38.
    child = {
        "taskId": "inbox-20261022234305-2e6ade6e",
        "batchId": "web-inbox-20261022234305-2e6ade6e",
        "runGroupId": "",
        "canonicalTaskId": PARENT_ID,
        "replenishForTaskId": PARENT_ID,
        "isReplenishment": True,
    }
    child.update(extra)
    return child


class InheritReplenishmentIdentityTests(unittest.TestCase):
    def test_blank_run_identity_is_inherited_from_parent(self):
        child = inherit_replenishment_identity(legacy_child(), STATUSES)
        self.assertEqual("run-20261022144436-260a970d", child["runGroupId"])
        self.assertEqual("formal-batch-1792651512044-f9150342b5", child["replenishmentParentBatchId"])
        self.assertEqual("formal", child["batchKind"])
        self.assertEqual(["ProjectA", "ProjectB"], child["selectedProjects"])
        self.assertEqual((7, 9), (child["batchSequence"], child["batchSize"]))
        self.assertNotIn("identityConflicts", child)

    def test_child_transport_batch_and_task_ids_are_kept(self):
        child = inherit_replenishment_identity(legacy_child(), STATUSES)
        self.assertEqual("web-inbox-20261022234305-2e6ade6e", child["batchId"])
        self.assertEqual("inbox-20261022234305-2e6ade6e", child["taskId"])
        self.assertEqual(PARENT_ID, child["canonicalTaskId"])

    def test_conflicting_run_group_is_kept_and_reported(self):
        child = inherit_replenishment_identity(legacy_child(runGroupId="run-other"), STATUSES)
        self.assertEqual("run-other", child["runGroupId"])
        self.assertEqual(["runGroupId"], child["identityConflicts"])

    def test_post_38_child_with_full_identity_is_unchanged(self):
        complete = legacy_child(runGroupId="run-20261022144436-260a970d",
                                replenishmentParentBatchId="formal-batch-1792651512044-f9150342b5",
                                batchKind="formal", selectedProjects=["ProjectA", "ProjectB"],
                                batchSequence=7, batchSize=9)
        self.assertEqual(complete, inherit_replenishment_identity(complete, STATUSES))

    def test_missing_parent_or_plain_task_is_unchanged(self):
        orphan = legacy_child(replenishForTaskId="unknown-parent")
        self.assertEqual(orphan, inherit_replenishment_identity(orphan, STATUSES))
        plain = {"taskId": "formal-1", "runGroupId": ""}
        self.assertEqual(plain, inherit_replenishment_identity(plain, STATUSES))
        self.assertEqual(legacy_child(), inherit_replenishment_identity(legacy_child(), {"items": []}))

    def test_input_payload_is_not_mutated(self):
        original = legacy_child()
        inherit_replenishment_identity(original, STATUSES)
        self.assertEqual("", original["runGroupId"])


if __name__ == "__main__":
    unittest.main()
