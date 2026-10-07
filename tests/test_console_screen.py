"""ScreenBuffer: the vt102 line model behind the raw serial terminal.

The readline fixtures were captured from the real ZCU102 console (bash, 80 columns)."""
import unittest

from devmem_studio.console_screen import ScreenBuffer

PROMPT = "root@xilinx-zcu102-2018_3:~# "
# 95 typed chars after the 29-col prompt: readline forces the wrap with " \r".
TYPED = "echo " + "y" * 46 + " \r" + "y" * 49
# 55 backspaces from col 129 back across the wrap (cursor-up + prompt reprint).
ERASED = ("\x08 \x08" * 49 + "\x08\x1b[A" + PROMPT + "echo " + "y" * 46 + " \r\x1b[K\x1b[A" + PROMPT + "echo "
          + "y" * 45 + "\x1b[K\r\n\r\x1b[K\x1b[A" + PROMPT + "echo " + "y" * 45 + "\x08 \x08" * 5)


class ScreenBufferTests(unittest.TestCase):
    def setUp(self):
        self.screen = ScreenBuffer()

    def test_crlf_commits_lines_and_returns_them(self):
        self.assertEqual(self.screen.feed("boot ok\r\nline 2\r\n" + PROMPT), ["boot ok", "line 2"])
        self.assertEqual(self.screen.current(), PROMPT)
        self.assertEqual(self.screen.cursor(), len(PROMPT))

    def test_bare_lf_and_split_crlf(self):
        self.assertEqual(self.screen.feed("a\nb\r"), ["a"])
        self.assertEqual(self.screen.feed("\nc"), ["b"])
        self.assertEqual(self.screen.current(), "c")

    def test_readline_wrap_keeps_one_logical_line(self):
        self.screen.feed(PROMPT)
        self.screen.feed(TYPED)
        self.assertEqual(self.screen.current(), PROMPT + "echo " + "y" * 95)
        self.assertEqual(self.screen.cursor(), 129)

    def test_backspace_across_wrap_matches_board(self):
        self.screen.feed(PROMPT + TYPED)
        self.screen.feed(ERASED)
        self.assertEqual(self.screen.current().rstrip(), PROMPT + "echo " + "y" * 40)
        self.assertEqual(self.screen.cursor(), 74)
        committed = self.screen.feed("\r\n" + "y" * 40 + "\r\n" + PROMPT)
        self.assertEqual(committed, [PROMPT + "echo " + "y" * 40, "y" * 40])

    def test_history_recall_redraw(self):
        self.screen.feed(PROMPT)
        self.screen.feed("echo " + "x" * 46 + "x\r" + "x" * 19)   # exact-margin wrap without " \r"
        self.assertEqual(self.screen.current(), PROMPT + "echo " + "x" * 65)

    def test_insert_mid_line_echo(self):
        self.screen.feed(PROMPT + "echo ac\x08bc\x08")
        self.assertEqual(self.screen.current(), PROMPT + "echo abc")
        self.assertEqual(self.screen.cursor(), len(PROMPT) + 7)

    def test_erase_and_cursor_sequences(self):
        self.screen.feed("hello world\x1b[5D\x1b[K")
        self.assertEqual(self.screen.current(), "hello ")
        self.screen.feed("\rab\x1b[2C!")
        self.assertEqual(self.screen.current(), "abll! ")
        self.screen.feed("\r\x1b[1P")
        self.assertEqual(self.screen.current(), "bll! ")
        self.screen.feed("\x1b[2@")
        self.assertEqual(self.screen.current(), "  bll! ")

    def test_escape_split_across_feeds_and_colors_ignored(self):
        self.screen.feed("\x1b[01;3")
        self.screen.feed("4mblue\x1b[0m \x1b]0;title\x07done")
        self.assertEqual(self.screen.current(), "blue done")

    def test_carriage_return_progress_overwrites(self):
        self.screen.feed("10%\r50%\r100%\r\n")
        self.assertEqual(self.screen.lines, ["100%"])

    def test_tab_and_bell(self):
        self.screen.feed("a\tb\x07")
        self.assertEqual(self.screen.current(), "a       b")

    def test_note_commits_live_line_first(self):
        self.screen.feed(PROMPT)
        self.assertEqual(self.screen.note("[note]"), [PROMPT.rstrip(), "[note]"])
        self.assertEqual(self.screen.current(), "")

    def test_clear_keeps_live_line_and_history_is_capped(self):
        screen = ScreenBuffer(max_lines=3)
        screen.feed("1\r\n2\r\n3\r\n4\r\n" + PROMPT)
        self.assertEqual(screen.lines, ["2", "3", "4"])
        screen.clear()
        self.assertEqual(screen.text(), PROMPT.rstrip())

    def test_utf8_text_and_unknown_controls(self):
        self.screen.feed("中文\x00\x01输出\r\n")
        self.assertEqual(self.screen.lines, ["中文输出"])


if __name__ == "__main__":
    unittest.main()
