import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from devmem_studio.widgets import IpAddressField  # noqa: E402


class IpAddressFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.field = IpAddressField()
        self.field.show()
        self.field.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        self.field.close()
        self.field.deleteLater()

    def type(self, text):
        for ch in text:
            QTest.keyClicks(QApplication.focusWidget(), ch)

    def octets(self):
        return [edit.text() for edit in self.field.octets]

    def test_dot_after_auto_advance_does_not_skip_an_octet(self):
        self.field.octets[0].setFocus()
        self.type("192.168.1.1")
        self.assertEqual(self.octets(), ["192", "168", "1", "1"])
        self.assertEqual(self.field.text(), "192.168.1.1")

    def test_dot_in_empty_octet_is_swallowed(self):
        self.field.octets[0].setFocus()
        self.type("1..2")
        self.assertEqual(self.octets(), ["1", "2", "", ""])

    def test_backspace_at_octet_start_deletes_previous_octet(self):
        self.field.setText("10.20.30.40")
        edit = self.field.octets[2]
        edit.setFocus()
        edit.setCursorPosition(0)
        QTest.keyClick(edit, Qt.Key_Backspace)
        self.assertEqual(self.octets(), ["10", "2", "30", "40"])
        self.assertIs(QApplication.focusWidget(), self.field.octets[1])

    def test_delete_at_octet_end_deletes_next_octet(self):
        self.field.setText("10.20.30.40")
        edit = self.field.octets[1]
        edit.setFocus()
        edit.setCursorPosition(2)
        QTest.keyClick(edit, Qt.Key_Delete)
        self.assertEqual(self.octets(), ["10", "20", "0", "40"])
        self.assertIs(QApplication.focusWidget(), self.field.octets[2])


if __name__ == "__main__":
    unittest.main()
