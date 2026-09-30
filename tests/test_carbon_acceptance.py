import unittest

from transport_coordination.carbon_acceptance import run


class CarbonAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 首次冻结：证据齐备的 t1 被接受，缺证的 t2 进入补证。
        self.assertEqual(1, result["first_freeze_accepted"])
        self.assertEqual(1, result["first_freeze_excluded"])
        self.assertIn("EVIDENCE_PAYLOAD_MISSING", result["first_freeze_evidence_codes"])
        # 车队与站点对同一事件的重复申报被拒绝。
        self.assertEqual("rejected_conflict", result["station_double_claim_status"])
        # 补证后重新冻结，两条行程均被接受。
        self.assertEqual(2, result["second_freeze_accepted"])
        # 所有历史版本均能按原输入复算，v1 哈希稳定。
        self.assertTrue(result["all_versions_recompute"])
        self.assertTrue(result["v1_hashes_stable"])
        # 凭证撤回后绿电收益回退为零，走廊只汇总每个族最新发布版本。
        self.assertEqual(0.0, result["v3_green_kwh"])
        self.assertEqual(3, result["progress_latest_version"])
        self.assertEqual(1, result["progress_family_count"])


if __name__ == "__main__":
    unittest.main()
