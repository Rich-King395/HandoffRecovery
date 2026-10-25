import unittest

from handoff_recovery.core.coordinator import LifecycleError
from handoff_recovery.core.protocols import classify_error, error_metadata


class ErrorProtocolTests(unittest.TestCase):
    def test_top_level_classes_are_disjoint(self):
        self.assertEqual(classify_error("native_parse_error", source="executor"),
                         "gui_agent_error")
        self.assertEqual(classify_error("decision_value", source="planner"),
                         "recovery_control_error")
        self.assertEqual(classify_error("device_error", source="device"), "device_error")
        self.assertEqual(classify_error("environment_error", source="environment"),
                         "environment_error")
        self.assertEqual(classify_error("lifecycle_error", source="lifecycle"),
                         "lifecycle_error")
        self.assertEqual(classify_error("integration_error", source="executor"),
                         "lifecycle_error")

    def test_unknown_host_category_does_not_become_gui_agent_error(self):
        self.assertEqual(classify_error("unknown", source="host"),
                         "recovery_control_error")

    def test_metadata_preserves_planner_validation_subcategory(self):
        error = ValueError("bounded")
        error.category = "evidence_id_unavailable"
        self.assertEqual(error_metadata(error, default_category="planner_error",
                                         source="planner"), {
            "error_category": "evidence_id_unavailable",
            "error_class": "recovery_control_error",
            "error_type": "ValueError",
        })

    def test_lifecycle_error_has_explicit_category(self):
        metadata = error_metadata(LifecycleError("invalid order"),
                                   default_category="environment_error", source="lifecycle")
        self.assertEqual(metadata["error_category"], "lifecycle_error")
        self.assertEqual(metadata["error_class"], "lifecycle_error")


if __name__ == "__main__":
    unittest.main()
