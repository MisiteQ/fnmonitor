"""JSON contract: keys + types of the main endpoints must match tests/golden/shapes.json.
Regenerate intentionally with `python3 tests/update_golden.py` and commit the diff."""
import json
import os
import unittest

import fnm_support as S


class JsonContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(S.GOLDEN):
            raise unittest.SkipTest("golden file missing: run python3 tests/update_golden.py")
        with open(S.GOLDEN, encoding="utf-8") as f:
            cls.golden = json.load(f)
        cls.srv = S.contract_server().start()
        cls.actual = S.capture_shapes(cls.srv)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "srv", None):
            cls.srv.stop()

    def test_endpoint_set(self):
        self.assertEqual(sorted(self.golden), sorted(S.CONTRACT_ENDPOINTS))

    def test_shapes(self):
        for ep in S.CONTRACT_ENDPOINTS:
            with self.subTest(endpoint=ep):
                self.assertIn(ep, self.golden)
                diffs = S.compare_shape(self.golden[ep], self.actual[ep],
                                        optional=S.OPTIONAL_KEYS.get(S.endpoint_key(ep), ()))
                self.assertEqual(diffs, [], "\n" + "\n".join(diffs))

    def test_comparator_detects_drift(self):
        g = {"a": "number", "b": {"c": "str"}, "l": [{"x": "bool"}]}
        self.assertEqual(S.compare_shape(g, {"a": "null", "b": {"c": "str"}, "l": []}), [])
        self.assertTrue(S.compare_shape(g, {"a": "str", "b": {"c": "str"}, "l": []}))
        self.assertTrue(S.compare_shape(g, {"a": "number", "b": {"c": "str", "d": "str"}, "l": []}))
        self.assertTrue(S.compare_shape(g, {"a": "number", "b": {}, "l": []}))
        self.assertTrue(S.compare_shape(g, {"a": "number", "b": {"c": "str"}, "l": [{"x": "str"}]}))
        self.assertTrue(S.compare_shape(g, {"a": "number", "b": "list", "l": []}))
        self.assertEqual(S.compare_shape({"o": "dict"}, {"o": {"k": "str"}}), [])


if __name__ == "__main__":
    unittest.main()
