import unittest

from corridor_carbon.acceptance import run


class CarbonAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 初版仅完整证据的 A 行程计入
        self.assertEqual(2, result["v1_pending_trips"])
        self.assertAlmostEqual(0.108, result["v1_reductions_tco2"], places=6)
        self.assertTrue(result["v1_recompute_matches"])
        self.assertTrue(result["self_verify_blocked"])
        # 迟到数据与冲突更正后的重述版本
        self.assertAlmostEqual(0.276, result["v2_reductions_tco2"], places=6)
        # 凭证撤回吊销后，以新凭证更正重述
        self.assertEqual(["initial", "restatement", "revocation", "restatement"],
                         result["published_report_kinds"])
        # 历史初版报告仍可按原输入复算
        self.assertTrue(result["history_v1_still_recomputes"])
        self.assertAlmostEqual(0.108, result["history_v1_reductions_tco2"], places=6)
        self.assertAlmostEqual(0.276, result["current_published_tco2"], places=6)
        # 主数据版本全部保留
        self.assertEqual(2, result["segment_versions_kept"])


if __name__ == "__main__":
    unittest.main()
