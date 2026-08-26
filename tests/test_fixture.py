import unittest

from scripts.verify_template import (
    FIXTURE_PATH,
    canonical_template_json,
)


class CanonicalTemplateTests(unittest.TestCase):
    def test_canonical_fixture_matches_builder(self):
        self.assertEqual(
            FIXTURE_PATH.read_text(encoding="utf-8"),
            canonical_template_json(),
        )


if __name__ == "__main__":
    unittest.main()
