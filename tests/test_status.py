"""A card has to say what is true now, not what was true when it was closed.

The PAX post was removed at 13:23, the card closed itself at 13:24 saying
removed, and the same moderator put it back at 13:25 with an unspam. The card
was still saying removed hours later, because closing it stopped anything from
looking again.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reddit_mod_from_discord.bot import status_after  # noqa: E402


class WhatTheCardShouldSay(unittest.TestCase):
    def test_a_removal_reads_as_removed(self):
        for action in ("removelink", "removecomment", "spamlink", "spamcomment"):
            self.assertEqual(status_after(action), (True, False), action)

    def test_an_approval_reads_as_approved(self):
        for action in ("approvelink", "approvecomment"):
            self.assertEqual(status_after(action), (False, True), action)

    def test_the_newest_action_is_the_one_that_counts(self):
        """The real sequence: removed, then unspammed a minute later."""
        removed, approved = status_after("removelink")
        self.assertTrue(removed)
        removed, approved = status_after("approvelink")
        self.assertTrue(approved)
        self.assertFalse(removed)

    def test_an_action_that_says_nothing_about_status_leaves_it_alone(self):
        for action in ("addremovalreason", "ignorereports", "lock", "sticky"):
            self.assertIsNone(status_after(action), action)


if __name__ == "__main__":
    unittest.main()
