import ast
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from devmem_studio.catalog import DEFAULT_CATEGORIES, DEFAULT_TYPES
from devmem_studio.core import (ConfigStore, access_width, decode_fields, format_decoded_fields, default_config,
                               parse_addr, parse_int, parse_devmem_read_output,
                               validated_address, write_command, write_value, SshSession, CommandError, ReadbackError)


class RegisterTests(unittest.TestCase):
    def test_catalog_exactly_preserves_legacy_registers(self):
        legacy = Path(__file__).resolve().parent.parent / "backups/devmem_debug_legacy.py"
        tree = ast.parse(legacy.read_text(encoding="utf-8-sig"))
        definitions = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("DEFAULT_CATEGORIES", "DEFAULT_TYPES"):
                        definitions[target.id] = ast.literal_eval(node.value)
        expected = copy.deepcopy(definitions["DEFAULT_TYPES"])
        # Intentional deviation: the a-channel behavior register is unified as "a bhv id" and read-only, like b/c bhv id.
        for registers in (expected["axis"]["registers"], expected["pl bus"]["registers"]):
            for reg in registers:
                if reg["name"] in ("a bhv", "a bhv id"):
                    reg["name"] = "a bhv id"
                    reg.update({"readonly": True})
                    reg.pop("aliases", None)
                    reg.pop("value", None)
        self.assertEqual({name: DEFAULT_TYPES[name] for name in definitions["DEFAULT_TYPES"]}, expected)
        self.assertEqual(DEFAULT_CATEGORIES, ["basic", *definitions["DEFAULT_CATEGORIES"]])
        self.assertEqual(sum(len(data["registers"]) for name, data in DEFAULT_TYPES.items() if name != "basic"), 152)

    def test_basic_register_offsets_and_write_access(self):
        expected = {"EC_ID": 0x00C, "SC_ID": 0x010, "BHV_PRIORITY": 0x014, "UNIT_ID": 0x018,
                    "UNIT_ECTRL": 0x01C, "UNIT_ST": 0x020, "M_ID": 0x024, "M_ECTRL": 0x028,
                    "M_ST": 0x02C, "M_WK_MOD": 0x030, "BHV_EN": 0x034, "M_SAF_ST": 0x038,
                    "LINK_M_SAF_ST": 0x03C, "A_TASK_ID": 0x054, "A_TASK_BHV_ID": 0x058}
        registers = DEFAULT_TYPES["basic"]["registers"]
        self.assertEqual(len(registers), 15)
        self.assertEqual({reg["name"]: parse_addr(reg["offset"]) for reg in registers}, expected)
        self.assertTrue(all(not reg.get("readonly") and not reg.get("action") and access_width(reg) == 32 for reg in registers))

    def test_address_semantics(self):
        self.assertEqual(parse_addr("2200"), 0x2200)
        self.assertEqual(parse_addr("0XB0100000"), 0xB0100000)
        self.assertEqual(parse_int("010"), 10)
        self.assertIsNone(parse_addr("not an address"))
        for value in ("-1", "100000000", "", "abc; reboot"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validated_address(value)

    def test_width_and_alignment(self):
        self.assertEqual(access_width({"width": 1}), 32)
        self.assertEqual(access_width({"width": 20}), 32)
        self.assertEqual(access_width({"width": 8}), 8)
        self.assertEqual(write_command(0xB0102208, 32, 1), "devmem 0xb0102208 32 0x1")
        for address, width, value in ((0xB0102201, 32, 1), (0xB0102208, 20, 1),
                                       (0xB0102208, 8, 256), (0xB0102208, 32, -1)):
            with self.subTest(address=address, width=width, value=value), self.assertRaises(ValueError):
                write_command(address, width, value)

    def test_radix_and_bounds(self):
        self.assertEqual(write_value("FF", "H", 8), 255)
        self.assertEqual(write_value("255", "D", 8), 255)
        self.assertEqual(write_value("0xFF", "H", 8), 255)
        for value, fmt in (("1FF", "H"), ("-1", "D"), ("0xFF", "D"), ("1;reboot", "H")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                write_value(value, fmt, 8)

    def test_devmem_output_variants_and_echo_rejection(self):
        address = 0xB0102208
        for text, expected in (("0x00000001\r\n", 1), ("42\n", 42),
                               ("Value at address 0xB0102208: 0xF0", 240),
                               ("Value at address (0xB0102208) = 0x2A", 42),
                               ("0xB0102208: 0x2A", 42), ("0xB0102208", address),
                               ("devmem 0xB0102208\n0x00000001\nroot@board:~#", 1)):
            with self.subTest(text=text):
                self.assertEqual(parse_devmem_read_output(text, address)[0], expected)
        for text in ("devmem 0xB0102208", "Error 13: Permission denied", "0xB010220C: 0x2A", "", "Segmentation fault"):
            with self.subTest(text=text):
                self.assertEqual(parse_devmem_read_output(text, address), (None, None))

    def test_devmem_output_with_interactive_prompt_prefix(self):
        """The interactive PTY shell prints PS1 before each line; prompt and value
        end up on the same line, so parsing must tolerate the prefix."""
        address = 0xB0102208
        for text, expected in (("root@xilinx-zcu102-2018_3:~# 0x0000002A", 42),
                               ("board# 0x0000002A", 42),
                               ("root@board:/root# 0x2A", 42),
                               ("$ 0x2A", 42),
                               ("root@xilinx-zcu102-2018_3:~# 0x0000002A\n", 42)):
            with self.subTest(text=text):
                self.assertEqual(parse_devmem_read_output(text, address)[0], expected)

    def test_irq_and_report_field_names_and_bit_layout(self):
        self.assertEqual(decode_fields("irq1", (0x1234 << 18) | (0x2AB << 8) | 0xCD),
                         {"ec_id": 4660, "sc_id": 683, "r_a_bhv_id": 205})
        self.assertEqual(decode_fields("irq2", 0xAABB0000), {"r_a_tx_id": 170, "r_a_alm_num": 187})
        self.assertEqual(decode_fields("irq2", 0xAABBFFFF), {"r_a_tx_id": 170, "r_a_alm_num": 187})
        for name in ("a rpt", "b rpt", "c rpt"):
            self.assertEqual(decode_fields(name, 0xABCDA57F),
                             {"ack_beh_id": 171, "ack_tx_id": 205, "ack_tx_result": 165, "ack_ps_alart_num": 127})
        self.assertEqual(decode_fields("en", 1), {})

    def test_decoded_display_radix_boundaries_and_absent_readings(self):
        self.assertEqual(format_decoded_fields("irq1", 0xFFFFFFFF),
                         {"ec_id": "16383", "sc_id": "1023", "r_a_bhv_id": "255"})
        self.assertEqual(format_decoded_fields("irq1", 0x00040000), {"ec_id": "1", "sc_id": "0", "r_a_bhv_id": "0"})
        self.assertEqual(format_decoded_fields("irq1", 0x00000100), {"ec_id": "0", "sc_id": "1", "r_a_bhv_id": "0"})
        self.assertEqual(format_decoded_fields("irq2", 0xAABB0000), {"r_a_tx_id": "170", "r_a_alm_num": "187"})
        for name in ("a rpt", "b rpt", "c rpt"):
            self.assertEqual(format_decoded_fields(name, 0xABCDA57F),
                             {"ack_beh_id": "171", "ack_tx_id": "205", "ack_tx_result": "0xA5", "ack_ps_alart_num": "127"})
            self.assertEqual(format_decoded_fields(name, 0)["ack_tx_result"], "0x00")
            self.assertEqual(format_decoded_fields(name, -1)["ack_tx_result"], "0xFF")
        for name in ("irq1", "irq2", "a rpt", "b rpt", "c rpt", "EC_ID"):
            self.assertEqual(decode_fields(name, None), {})
            self.assertEqual(format_decoded_fields(name, None), {})

    def test_write_success_and_readback_failure_are_distinguished(self):
        session = SshSession()
        session.run = Mock(return_value="")
        session.read = Mock(side_effect=TimeoutError("read timeout"))
        with self.assertRaisesRegex(ReadbackError, "写入已完成，但回读失败"):
            session.write(0xB0102208, 32, 1)
        session.run.assert_called_once_with("devmem 0xb0102208 32 0x1", quiet=False)
        session.read.assert_called_once_with(0xB0102208, quiet=False)

    def test_failed_write_does_not_read_back_or_report_success(self):
        session = SshSession()
        failure = CommandError("write denied")
        session.run = Mock(side_effect=failure)
        session.read = Mock()
        with self.assertRaises(CommandError) as caught:
            session.write(0xB0102208, 32, 1)
        self.assertIs(caught.exception, failure)
        self.assertNotIsInstance(caught.exception, ReadbackError)
        session.read.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_basic_category_selection_and_write_cache_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "registers.json")
            cfg = default_config()
            cfg["last_category"] = "basic"
            cfg["write_cache"] = {"basic@0x0800": {"M_ST": {"v": "AB", "f": "H", "w": 32}}}
            store.save(cfg)
            restored = store.load()
            self.assertEqual(restored["last_category"], "basic")
            self.assertEqual(restored["write_cache"], cfg["write_cache"])

    def test_old_settings_unknown_fields_and_password_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "配置.json"
            path.write_text(json.dumps({"password": "test-password", "custom": {"keep": True},
                                        "last_category": "pl ps", "last_address": "0x10400"}), encoding="utf-8")
            store = ConfigStore(path)
            cfg = store.load()
            self.assertTrue(cfg["remember_password"])
            self.assertEqual(cfg["last_category"], "pl ps")
            cfg["remember_password"] = False
            cfg["write_cache"] = {"axis@0x0800": {"a out": {"v": "2A", "f": "H"}}}
            store.save(cfg)
            loaded = store.load()
            self.assertEqual(loaded["password"], "")
            self.assertEqual(loaded["custom"], {"keep": True})
            self.assertEqual(loaded["write_cache"], cfg["write_cache"])
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_corrupt_config_is_recovered_without_losing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registers.json"
            path.write_text("{broken", encoding="utf-8")
            store = ConfigStore(path)
            cfg = store.load()
            self.assertTrue(store.warning)
            self.assertEqual(cfg["base"], "0xb0100000")
            backups = list(Path(directory).glob("registers.json.invalid-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "{broken")
            store.save(cfg)
            self.assertEqual(store.load()["host"], "")

    def test_invalid_numeric_config_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registers.json"
            path.write_text(json.dumps({"port": "wrong", "base": None,
                                        "last_category": "missing", "write_cache": []}), encoding="utf-8")
            cfg = ConfigStore(path).load()
            self.assertEqual(cfg["port"], 22)
            self.assertEqual(cfg["last_category"], "axis")
            self.assertEqual(cfg["write_cache"], {})
            self.assertNotIn("connect_timeout", cfg)


if __name__ == "__main__":
    unittest.main()
