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


if __name__ == "__main__":
    unittest.main()
