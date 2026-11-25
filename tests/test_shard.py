import unittest

from shard import select


class _Test:
    def __init__(self, test_id):
        self._id = test_id

    def id(self):
        return self._id


class ShardTest(unittest.TestCase):
    def test_shards_run_every_test_exactly_once(self):
        tests = [_Test(f"test_{name}") for name in "gdbface"]
        shards = [[test.id() for test in select(tests, index, 3)] for index in (1, 2, 3)]
        self.assertEqual(shards, [["test_a", "test_d", "test_g"], ["test_b", "test_e"],
                                  ["test_c", "test_f"]])

    def test_long_tests_are_balanced_before_short_tests(self):
        tests = [_Test(f"test_{name}") for name in "abcdefghi"]
        timings = {test.id(): 10 if test.id() in ("test_a", "test_d", "test_g") else 1
                   for test in tests}
        shards = [select(tests, index, 3, timings) for index in (1, 2, 3)]
        self.assertEqual([sum(timings[test.id()] for test in shard) for shard in shards],
                         [12, 12, 12])
        self.assertEqual(sorted(test.id() for shard in shards for test in shard),
                         sorted(test.id() for test in tests))
        self.assertEqual([[test.id() for test in shard] for shard in shards],
                         [[test.id() for test in select(reversed(tests), index, 3, timings)]
                          for index in (1, 2, 3)])

    def test_module_estimates_overrides_and_new_tests(self):
        tests = [_Test(name) for name in ("slow.C.a", "slow.C.b", "new.C.a", "fast.C.a")]
        timings = {"slow": 8, "slow.C.b": 2, "fast": 0}
        self.assertEqual([test.id() for test in select(tests, 1, 2, timings)], ["slow.C.a"])
        self.assertEqual([test.id() for test in select(tests, 2, 2, timings)],
                         ["fast.C.a", "new.C.a", "slow.C.b"])

    def test_zero_duration_tests_and_empty_shards(self):
        tests = [_Test(f"test_{name}") for name in "abcde"]
        timings = {test.id(): 0 for test in tests}
        self.assertEqual([len(select(tests, index, 3, timings)) for index in (1, 2, 3)],
                         [2, 2, 1])
        self.assertEqual(select([], 1, 2), [])
        self.assertEqual(select(tests[:1], 2, 2), [])

    def test_invalid_shard_is_rejected(self):
        for index, total in ((0, 2), (3, 2), (1, 0)):
            with self.subTest(index=index, total=total), self.assertRaises(ValueError):
                select([], index, total)


if __name__ == "__main__":
    unittest.main()
