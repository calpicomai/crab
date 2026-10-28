"""Deterministic research regression reports, not hardware or training claims."""
import unittest
from lab.evaluation import run_evaluation


class EvaluationTests(unittest.TestCase):
    def test_repeatable_case_fingerprint_and_truthful_scope(self):
        first=run_evaluation(seeds=[3],seconds=.8)
        second=run_evaluation(seeds=[3],seconds=.8)
        self.assertEqual(first['fingerprint'],second['fingerprint'])
        self.assertEqual(len(first['cases']),3)
        self.assertTrue(first['passed'])
        self.assertFalse(first['hardware_acceptance'])
        self.assertFalse(first['learned_policy'])
        dropout=next(x for x in first['cases'] if x['scenario']=='sonar_dropout')
        self.assertGreater(dropout['summary']['inhibited_samples'],0)
        self.assertIn('controller_code_sha256',first['cases'][0]['manifest'])

    def test_bounded_evaluation_inputs(self):
        for seeds in ([],list(range(6)),[True],[-1]):
            with self.assertRaises(ValueError):run_evaluation(seeds=seeds,seconds=.8)


if __name__=='__main__':unittest.main()
