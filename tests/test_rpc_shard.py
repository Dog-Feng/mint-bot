import unittest

from mint_engine.rpc.pool import assign_shard_urls


class TestShardAssignment(unittest.TestCase):
    def test_groups_by_concurrency(self):
        urls = ["a", "b", "c"]
        assigned = assign_shard_urls(urls, count=5, concurrency=2)
        self.assertEqual(len(assigned), 5)
        self.assertEqual(assigned[0], assigned[1])
        self.assertEqual(assigned[2], assigned[3])
        self.assertNotEqual(assigned[0], assigned[2])


if __name__ == "__main__":
    unittest.main()
