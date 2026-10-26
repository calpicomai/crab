"""Stop/resume ordering, entirely in-process with explicit simulation."""
import threading
import unittest
from unittest.mock import patch

from robot.control import CommandLeases
from robot.gait import GaitEngine
from shared import ResumeCommand, StopCommand


class ControlRaceTests(unittest.TestCase):
    def setUp(self):
        from robot import server
        self.server = server
        self.engine = GaitEngine(simulate=True)
        self.engine.battery_fn = lambda: 7.4
        self.leases = CommandLeases()
        self.patches = [patch.object(server, "engine", self.engine),
                        patch.object(server, "leases", self.leases),
                        patch.object(server, "speaker", None),
                        patch.object(server, "command_lock", threading.Lock()),
                        patch.object(server, "control_transition_lock", threading.Lock()),
                        patch.object(server, "_status_with_distance", self.engine.get_status)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def command(self):
        lease = self.leases.issue()
        return ResumeCommand(lease_id=lease["lease_id"], command_id="resume-once")

    def test_stop_invalidates_lease_issued_before_it(self):
        cmd = self.command()
        self.server.stop(StopCommand())
        self.assertFalse(self.server.resume(cmd).ok)
        self.assertTrue(self.engine.motion_state()["stop_latched"])

    def test_old_resume_cannot_clear_concurrent_new_stop(self):
        cmd = self.command()
        consumed, release, stop_started, stopped = (threading.Event() for _ in range(4))
        consume = self.leases.consume
        def pause_after_consume(*args):
            reason = consume(*args)
            consumed.set()
            if not release.wait(2):
                raise AssertionError("test did not release resume")
            return reason
        self.leases.consume = pause_after_consume
        responses = []
        resumer = threading.Thread(target=lambda: responses.append(self.server.resume(cmd)))
        def stop_now():
            stop_started.set()
            self.server.stop(StopCommand())
            stopped.set()
        stopper = threading.Thread(target=stop_now)
        resumer.start()
        try:
            self.assertTrue(consumed.wait(2))
            stopper.start()
            self.assertTrue(stop_started.wait(2))
            # STOP is ordered after this tiny in-flight RESUME transaction. It
            # never waits on a movement or audio playback operation.
            self.assertFalse(stopped.wait(.05))
        finally:
            release.set()
            resumer.join(2)
            if stopper.ident is not None:
                stopper.join(2)
        self.assertFalse(resumer.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertTrue(responses[0].ok)
        self.assertTrue(stopped.is_set())
        self.assertTrue(self.engine.motion_state()["stop_latched"])


if __name__ == "__main__":
    unittest.main()
