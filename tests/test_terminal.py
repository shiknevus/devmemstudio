"""TerminalView prompt editing: caret movement, mid-line edits, prompt guard."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from devmem_studio.widgets import TerminalView


class TerminalEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sent = []
        self.requests = []
        self.view = TerminalView(self.sent.append, lambda line, reply: self.requests.append((line, reply)))
        self.view.set_interactive(True)
        self.view.append_before_prompt("history line")
        self.view.show()

    def tearDown(self):
        self.view.close()

    def type(self, text):
        QTest.keyClicks(self.view, text)

    def test_left_arrow_then_insert_mid_line(self):
        self.type("ls /rn")
        QTest.keyClick(self.view, Qt.Key_Left)
        self.type("u")
        self.assertEqual(self.view._prompt_input(), "ls /run")

    def test_delete_and_backspace_at_caret(self):
        self.type("lsx -l")
        for _ in range(3):
            QTest.keyClick(self.view, Qt.Key_Left)
        QTest.keyClick(self.view, Qt.Key_Backspace)
        self.assertEqual(self.view._prompt_input(), "ls -l")
        QTest.keyClick(self.view, Qt.Key_Home)
        QTest.keyClick(self.view, Qt.Key_Delete)
        self.assertEqual(self.view._prompt_input(), "s -l")

    def test_prompt_marker_is_protected(self):
        self.type("ab")
        QTest.keyClick(self.view, Qt.Key_Home)
        QTest.keyClick(self.view, Qt.Key_Left)
        QTest.keyClick(self.view, Qt.Key_Backspace)
        QTest.keyClick(self.view, Qt.Key_Left, Qt.ControlModifier)
        self.type("x")
        self.assertTrue(self.view.document().lastBlock().text().startswith(TerminalView.PROMPT))
        self.assertEqual(self.view._prompt_input(), "xab")

    def test_ctrl_backspace_deletes_word_not_prompt(self):
        self.type("cat file")
        QTest.keyClick(self.view, Qt.Key_Backspace, Qt.ControlModifier)
        QTest.keyClick(self.view, Qt.Key_Backspace, Qt.ControlModifier)
        QTest.keyClick(self.view, Qt.Key_Backspace, Qt.ControlModifier)
        self.assertEqual(self.view._prompt_input(), "")
        self.assertTrue(self.view._prompt_present())

    def test_typing_with_caret_in_history_goes_to_end(self):
        self.type("ls")
        cursor = self.view.textCursor()
        cursor.setPosition(2)   # inside "history line"
        self.view.setTextCursor(cursor)
        self.type("x")
        self.assertEqual(self.view._prompt_input(), "lsx")
        self.assertTrue(self.view.toPlainText().startswith("history line"))

    def test_completion_uses_word_at_caret(self):
        self.type("cat /et -n")
        for _ in range(3):
            QTest.keyClick(self.view, Qt.Key_Left)
        QTest.keyClick(self.view, Qt.Key_Tab)
        line, reply = self.requests[-1]
        self.assertEqual(line, "cat /et")
        reply(["/etc/"])
        self.assertEqual(self.view._prompt_input(), "cat /etc/ -n")
        self.type("x")
        self.assertEqual(self.view._prompt_input(), "cat /etc/x -n")


class RawTerminalTests(unittest.TestCase):
    """Character mode used by the serial view: keys out as bytes, board text in."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sent = []
        self.view = TerminalView(lambda text: self.fail("raw mode never submits lines"))
        self.view.set_raw(self.sent.append)
        self.view.raw_reset("boot log\nroot# ", 6)
        self.view.show()

    def tearDown(self):
        self.view.close()

    def test_keys_map_to_vt102_bytes(self):
        QTest.keyClicks(self.view, "ls -l")
        for key, modifiers in ((Qt.Key_Tab, Qt.NoModifier), (Qt.Key_Backspace, Qt.NoModifier),
                               (Qt.Key_Up, Qt.NoModifier), (Qt.Key_Left, Qt.NoModifier),
                               (Qt.Key_Delete, Qt.NoModifier), (Qt.Key_Return, Qt.NoModifier),
                               (Qt.Key_D, Qt.ControlModifier), (Qt.Key_Ampersand, Qt.ShiftModifier)):
            QTest.keyClick(self.view, key, modifiers)
        self.assertEqual(b"".join(self.sent), b"ls -l\t\x7f\x1b[A\x1b[D\x1b[3~\r\x04&")
        self.assertTrue(self.view.isReadOnly())
        self.assertTrue(self.view.toPlainText().startswith("boot log"))   # nothing echoed locally

    def test_ctrl_c_copies_a_selection_otherwise_interrupts(self):
        QTest.keyClick(self.view, Qt.Key_C, Qt.ControlModifier)
        self.assertEqual(self.sent, [b"\x03"])
        cursor = self.view.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(4, cursor.MoveMode.KeepAnchor)
        self.view.setTextCursor(cursor)
        QTest.keyClick(self.view, Qt.Key_C, Qt.ControlModifier)
        self.assertEqual(self.sent, [b"\x03"])
        self.assertEqual(QApplication.clipboard().text(), "boot")

    def test_paste_sends_clipboard_with_carriage_returns(self):
        QApplication.clipboard().setText("echo 1\necho 2")
        QTest.keyClick(self.view, Qt.Key_V, Qt.ControlModifier)
        self.assertEqual(self.sent, [b"echo 1\recho 2"])

    def test_tab_stays_in_the_terminal(self):
        QTest.keyClick(self.view, Qt.Key_Tab)
        self.assertEqual(self.sent, [b"\t"])
        self.assertFalse(self.view.focusNextPrevChild(True))

    def test_update_replaces_live_line_and_keeps_selection(self):
        self.view.raw_update(["root# ls", "a  b"], "root# ", 6)
        self.assertEqual(self.view.toPlainText(), "boot log\nroot# ls\na  b\nroot# ")
        self.assertEqual(self.view.textCursor().position(), len(self.view.toPlainText()))
        cursor = self.view.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(4, cursor.MoveMode.KeepAnchor)
        self.view.setTextCursor(cursor)
        self.view.raw_update([], "root# x", 7)
        self.assertEqual(self.view.textCursor().selectedText(), "boot")   # copying is not interrupted

    def test_leaving_raw_mode_restores_prompt_terminal(self):
        self.view.set_interactive(True)
        self.assertFalse(self.view.is_raw())
        self.assertTrue(self.view.is_interactive())


if __name__ == "__main__":
    unittest.main()
