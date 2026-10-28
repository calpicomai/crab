import json
from pathlib import Path
import tempfile
import unittest

from lab.episodes import run_episode,write_episode


class EpisodeTests(unittest.TestCase):
    def test_reproducible_data_and_privileged_truth_are_separate(self):
        first=run_episode(seed=5,seconds=.5)
        second=run_episode(seed=5,seconds=.5)
        self.assertEqual(first['steps'],second['steps'])
        self.assertEqual(first['manifest']['provenance'],'simulation_only')
        self.assertFalse(first['manifest']['learned_policy'])
        self.assertGreater(len(first['steps']),1)
        row=first['steps'][-1]
        self.assertNotIn('joint_positions_rad',row['policy_observation'])
        self.assertIn('joint_positions_rad',row['evaluation_truth'])
        self.assertIn('requested_targets',row)
        self.assertEqual(first['summary']['hardware_acceptance'],False)

    def test_bounds_and_explicit_nonoverwriting_export(self):
        for kwargs in ({'seconds':float('nan')},{'seconds':31},{'seed':-1},{'scenario':'../../robot'}):
            with self.assertRaises(ValueError):run_episode(**kwargs)
        result=run_episode(seconds=.1)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'episode.json'
            write_episode(result,path)
            self.assertEqual(json.loads(path.read_text())['manifest']['seed'],1)
            with self.assertRaises(FileExistsError):write_episode(result,path)


if __name__=='__main__':unittest.main()
