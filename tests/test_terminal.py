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


if __name__ == "__main__":
    unittest.main()
