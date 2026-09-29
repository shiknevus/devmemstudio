"""Tab completion string logic (no Qt, no board)."""
import unittest

from devmem_studio import completion as c


class CompletionTests(unittest.TestCase):
    def test_split_and_escape_roundtrip(self):
        self.assertEqual(c.split_word("ls /run/me"), ("ls ", "/run/me"))
        self.assertEqual(c.split_word("cat my\\ fi"), ("cat ", "my\\ fi"))
        self.assertEqual(c.split_word(""), ("", ""))
        self.assertEqual(c.unescape(c.escape("a b$(x)")), "a b$(x)")

    def test_command_position(self):
        self.assertTrue(c.is_command_position("", "de"))
        self.assertTrue(c.is_command_position("ls | ", "gr"))
        self.assertFalse(c.is_command_position("ls ", "fo"))
        self.assertFalse(c.is_command_position("", "./ru"))

    def test_single_match_completes_with_space_or_slash(self):
        self.assertEqual(c.apply("dev", ["devmem"]), ("devmem ", None))
        self.assertEqual(c.apply("cd /run/me", ["/run/media/"]), ("cd /run/media/", None))
        self.assertEqual(c.apply("cat my", ["my file"]), ("cat my\\ file ", None))

    def test_ambiguous_extends_common_prefix_then_lists(self):
        names = ["/bin/busybox", "/bin/bunzip2"]
        self.assertEqual(c.apply("ls /bin/b", names), ("ls /bin/bu", None))
        self.assertEqual(c.apply("ls /bin/bu", names), ("ls /bin/bu", ["bunzip2", "busybox"]))

    def test_no_match_keeps_line(self):
        self.assertEqual(c.apply("zz", ["devmem"]), ("zz", None))

    def test_local_candidates_use_builtins_and_history(self):
        names = c.local_candidates("d", True, ["dmesg | tail", "devmem 0x1"])
        self.assertIn("devmem", names)
        self.assertIn("dmesg", names)
        self.assertEqual(c.local_candidates("d", False, ["dmesg"]), [])

    def test_remote_script_quotes_user_text(self):
        script = c.remote_script("/tmp/a'b", False)
        self.assertIn("/tmp/'a'\"'\"'b'*", script)
        self.assertIn('"$HOME"/', c.remote_script("~/x", False))
        self.assertIn("$PATH", c.remote_script("dev", True))
        self.assertIsNone(c.remote_script("$HO", False))

    def test_parse_remote_keeps_typed_dir(self):
        self.assertEqual(c.parse_remote("media/\nlog\n", "/run/m"), ["/run/log", "/run/media/"])
        self.assertEqual(c.parse_remote("a\n", "~/"), ["~/a"])


if __name__ == "__main__":
    unittest.main()
