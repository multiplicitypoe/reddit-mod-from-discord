"""The action that decides a card's status is the newest one, not the first.

modlog lines arrive oldest first, because that is the order they read in on the
card. The resolver walked them front to back and returned the oldest action that
said anything, so a post removed at 13:23 and put back at 13:25 resolved as
removed, and the card said removed for as long as it existed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.bot import newest_resolving_action  # noqa: E402

REMOVED = "2026-08-20 13:23 UTC - u/TestSubject1: removelink [modlog] (remove)"
APPROVED = "2026-08-20 13:25 UTC - u/TestSubject1: approvelink [modlog] (unspam)"
REASON = "2026-08-20 13:24 UTC - u/TestSubject1: addremovalreason [modlog] (14)"


class TheNewestActionWins(unittest.TestCase):
    def test_the_pax_post_reads_as_approved(self):
        # the real sequence, oldest first, which is how the store hands it over
        self.assertEqual(newest_resolving_action([REMOVED, APPROVED]), "approvelink")

    def test_a_post_put_back_and_removed_again_reads_as_removed(self):
        self.assertEqual(newest_resolving_action([REMOVED, APPROVED, REMOVED]), "removelink")

    def test_a_removal_reason_after_the_removal_does_not_hide_it(self):
        self.assertEqual(newest_resolving_action([REMOVED, REASON]), "removelink")

    def test_nothing_conclusive_returns_nothing(self):
        self.assertIsNone(newest_resolving_action([REASON]))
        self.assertIsNone(newest_resolving_action([]))


if __name__ == "__main__":
    unittest.main()
