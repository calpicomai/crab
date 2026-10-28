"""Real local physics through the bounded browser API; no hardware adapters."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from lab.server import create_app


class ServerTests(unittest.TestCase):
    def test_reset_failure_preserves_snapshot_and_requires_explicit_recovery(self):
        app=create_app()
        with TestClient(app,raise_server_exceptions=False) as client:
            original=app.state.runner._world
            before=client.get('/api/state').json()
            def fail_reset(world,**_kwargs):
                if hasattr(world,'data'):world.data.time=9
                raise RuntimeError('injected reset failure')
            with patch('lab.server.LabWorld.reset',fail_reset):
                self.assertEqual(client.post('/api/control',json={'action':'reset'}).status_code,409)
            after=client.get('/api/state').json()
            self.assertEqual(after['sim_time_s'],before['sim_time_s'])
            self.assertFalse(after['runtime']['snapshot_valid'])
            self.assertFalse(after['runtime']['running'])
            self.assertIs(app.state.runner._world,original)
            self.assertEqual(client.post('/api/control',json={'action':'play'}).status_code,409)
            restored=client.post('/api/control',json={'action':'reset'}).json()
            self.assertTrue(restored['runtime']['snapshot_valid'])

    def test_single_step_failure_latches_error_until_reset(self):
        app=create_app()
        with TestClient(app) as client:
            before=client.get('/api/state').json()['sim_time_s']
            with patch.object(app.state.runner._world,'step',side_effect=RuntimeError('injected physics failure')):
                self.assertEqual(client.post('/api/control',json={'action':'step'}).status_code,409)
            state=client.get('/api/state').json()
            self.assertFalse(state['runtime']['running'])
            self.assertFalse(state['runtime']['snapshot_valid'])
            self.assertIn('RuntimeError',state['runtime']['error'])
            self.assertEqual(state['sim_time_s'],before)
            self.assertEqual(client.post('/api/control',json={'action':'play'}).status_code,409)
            self.assertEqual(client.post('/api/control',json={'action':'step'}).status_code,409)
            self.assertEqual(client.post('/api/control',json={'action':'mode','mode':'crawl'}).status_code,409)
            self.assertEqual(client.get('/api/state').json()['sim_time_s'],before)
            reset=client.post('/api/control',json={'action':'reset'}).json()
            self.assertTrue(reset['runtime']['snapshot_valid'])
            self.assertIsNone(reset['runtime']['error'])

    def test_read_pause_single_step_reset_and_no_hardware_route(self):
        with TestClient(create_app()) as client:
            state = client.get('/api/state').json()
            self.assertEqual(state['provenance'], 'mujoco_simulation')
            self.assertFalse(state['runtime']['running'])
            before = state['sim_time_s']
            stepped = client.post('/api/control',json={'action':'step'}).json()
            self.assertGreater(stepped['sim_time_s'], before)
            reset = client.post('/api/control',json={'action':'reset','seed':8,'scenario':'flat'}).json()
            self.assertEqual(reset['seed'],8)
            self.assertEqual(reset['sim_time_s'],0)
            self.assertIn('policy_observation',reset)
            self.assertIn('evaluation_truth',reset)
            for path in ('/stop','/resume','/control/lease','/companion/ws','/camera/frame'):
                self.assertEqual(client.post(path).status_code,404)

    def test_controls_reject_unbounded_unknown_and_cross_origin_input(self):
        with TestClient(create_app()) as client:
            for value in ({'action':'ssh'},{'action':'step','steps':1000000},
                          {'action':'reset','seed':-1},{'action':'mode','mode':'wander'},
                          {'action':'reset','scenario':'file:///etc/passwd'}):
                self.assertEqual(client.post('/api/control',json=value).status_code,422)
            self.assertEqual(client.post('/api/control',content='x'*2000).status_code,413)
            self.assertEqual(client.post('/api/control',json={'action':'play'},headers={'Origin':'https://example.com'}).status_code,403)
            self.assertEqual(client.get('/api/state',headers={'Host':'malicious.example'}).status_code,400)
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/static/app.js').status_code,200)


if __name__=='__main__':unittest.main()
