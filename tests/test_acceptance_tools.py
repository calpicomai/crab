"""Acceptance launchers must never silently exercise physical actuators."""
import contextlib
import io
import unittest
from unittest.mock import patch

from brain import test_movement
from shared import Action, CommandResponse, RobotStatus


class FakeRobotClient:
    simulate = False
    last = None

    def __init__(self, *args, **kwargs):
        self.base_url = 'http://fixture.invalid'
        self.commands = []
        type(self).last = self

    def health(self):
        return {'simulate': self.simulate, 'hardware_verified': False}

    def get_status(self):
        return CommandResponse(ok=True, action=Action.GET_STATUS,
                               status=RobotStatus(simulate=self.simulate))

    def stand(self):
        self.commands.append('stand')
        return CommandResponse(ok=False, action=Action.STAND, detail='battery unavailable')

    def sit(self):
        raise AssertionError('acceptance sequence continued after a failed command')

    def close(self):
        pass


class AcceptanceToolsTests(unittest.TestCase):
    def test_movement_defaults_to_read_only_on_real_status(self):
        with patch.object(test_movement, 'RobotClient', FakeRobotClient), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(test_movement.main([]), 2)
        self.assertEqual(FakeRobotClient.last.commands, [])

    def test_failed_command_is_not_reported_as_completed(self):
        with patch.object(test_movement, 'RobotClient', FakeRobotClient), \
             patch.object(FakeRobotClient, 'simulate', True), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(test_movement.main([]), 1)
        self.assertEqual(FakeRobotClient.last.commands, ['stand'])


if __name__ == '__main__':
    unittest.main()
