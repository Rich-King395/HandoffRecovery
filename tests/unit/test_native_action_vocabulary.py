"""New native device actions pass through the existing counted local loop."""
from copy import deepcopy
import unittest

from handoff_recovery.core.coordinator import RecoveryCoordinator
from tests.unit.test_coordinator import FakeExecutor, FakePlanner, GOAL, frame, slim_execute_reply


class NativeActionVocabularyTests(unittest.TestCase):
    def test_native_actions_are_forwarded_once_in_original_and_execution(self):
        actions = [
            {'action': 'replace_text', 'coords': [], 'input_text': 'synthetic'},
            {'action': 'type_and_enter', 'coords': [], 'input_text': 'synthetic'},
            {'action': 'append_text', 'coords': [], 'input_text': 'synthetic'},
            {'action': 'save_screenshot', 'coords': []},
        ]
        for scope in ('original', 'execution'):
            for action in actions:
                with self.subTest(scope=scope, action=action['action']):
                    executor = FakeExecutor([{'action': 'wait'}, deepcopy(action), {'action': 'done'}])
                    planner = FakePlanner([slim_execute_reply()] if scope == 'execution' else [])
                    coordinator = RecoveryCoordinator(
                        task_id='synthetic-native-action', original_goal=GOAL,
                        planner=planner, executor=executor, completion_policy='native',
                        condition_updates_enabled=False)
                    self.assertEqual(coordinator.step(frame('prefix')).kind, 'action')
                    coordinator.acknowledge_action(success=True)
                    if scope == 'execution':
                        coordinator.pause_for_handoff()
                        coordinator.return_control(frame('returned'))
                    result = coordinator.step(frame('work'))
                    self.assertEqual(result.kind, 'action')
                    self.assertEqual(result.action, action)
                    self.assertEqual(result.scope, scope)
                    coordinator.acknowledge_action(success=True)
                    self.assertEqual(coordinator.step(frame('done')).kind, 'task_complete')
                    self.assertEqual(coordinator.decisions_used, 3)
                    self.assertEqual(len(executor.requests), 3)
                    self.assertEqual(len(planner.requests), int(scope == 'execution'))

    def test_unknown_action_remains_an_error(self):
        coordinator = RecoveryCoordinator(
            task_id='synthetic-native-action', original_goal=GOAL,
            planner=FakePlanner(), executor=FakeExecutor([{'action': 'not_a_public_action'}]))
        result = coordinator.step(frame('invalid'))
        self.assertEqual(result.kind, 'agent_error')
        self.assertEqual(coordinator.decisions_used, 1)
