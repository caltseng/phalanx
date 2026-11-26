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
        for total in (1, 3, 10):
            with self.subTest(total=total):
                selected = [test.id() for index in range(1, total + 1)
                            for test in select(tests, index, total)]
                self.assertCountEqual(selected, [test.id() for test in tests])

    def test_shards_are_round_robin_in_test_id_order(self):
        tests = [_Test(f"test_{name}") for name in "gdbface"]
        expected = [["test_a", "test_d", "test_g"], ["test_b", "test_e"], ["test_c", "test_f"]]
        for index, ids in enumerate(expected, 1):
            with self.subTest(index=index):
                self.assertEqual([test.id() for test in select(tests, index, 3)], ids)
                self.assertEqual([test.id() for test in select(reversed(tests), index, 3)], ids)

    def test_invalid_shard_numbers_are_rejected(self):
        for index, total in ((0, 3), (4, 3), (1, 0), (1, -1)):
            with self.subTest(index=index, total=total), self.assertRaises(ValueError):
                select([], index, total)


if __name__ == "__main__":
    unittest.main()
