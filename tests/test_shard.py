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
        for total, timings in ((3, {}), (3, {"test_a": 10, "test_b": 0}), (10, {})):
            with self.subTest(total=total, timings=timings):
                selected = [test.id() for index in range(1, total + 1)
                            for test in select(tests, index, total, timings)]
                self.assertCountEqual(selected, [test.id() for test in tests])


if __name__ == "__main__":
    unittest.main()
