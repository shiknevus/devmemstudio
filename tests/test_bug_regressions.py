"""Regression cases for defects found during the 2026-10-06 audit."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from devmem_studio.core import ConfigStore, DemoSession, SshSession, default_config
from devmem_studio import top_import


class AddressParsingRegressions(unittest.TestCase):
    def test_base_address_ignores_line_and_block_comments(self):
        text = """// `define PL_CFG_BASE_ADDR {32'hA0000000}
/* `define PL_CFG_BASE_ADDR {32'hC0000000} */
`define PL_CFG_BASE_ADDR {32'hB010_0000}
"""
        self.assertEqual(top_import.parse_base_address(text), 0xB0100000)
        self.assertIsNone(top_import.parse_base_address(text.split("`define PL_CFG_BASE_ADDR {32'hB010")[0]))

    def test_base_address_requires_exact_macro_and_complete_constant(self):
        for text in ("`define OLD_PL_CFG_BASE_ADDR {32'hA0000000}",
                     "`define PL_CFG_BASE_ADDR_COPY {32'hA0000000}",
                     "`define PL_CFG_BASE_ADDR {32'hB0100000 + 32'h1000}"):
            with self.subTest(text=text):
                self.assertIsNone(top_import.parse_base_address(text))
        self.assertEqual(top_import.parse_base_address("`define PL_CFG_BASE_ADDR {32'HB010_0000}"),
                         0xB0100000)

    def test_bias_expression_is_not_silently_truncated(self):
        for header in ("", "// --- flow_comp_1 --test---\n"):
            text = header + "ec_uut\n#(.REG_SPACE_BIAS(20'h800 + 20'h200))\nec_uut_1\n();\n"
            with self.subTest(header=bool(header)):
                info = top_import.parse_top(text)
                self.assertEqual(info["components"], [])
                self.assertTrue(info["warnings"])

    def test_malformed_bias_is_skipped_without_losing_valid_components(self):
        text = ("ec_bad #(.REG_SPACE_BIAS(20'dBAD)) ec_bad_1 ();\n"
                "ec_good #(.REG_SPACE_BIAS(20'hA00)) ec_good_2 ();\n")
        info = top_import.parse_top(text)
        self.assertEqual([comp["instance"] for comp in info["components"]], ["ec_good_2"])
        self.assertTrue(info["warnings"])


class SettingsRegressions(unittest.TestCase):
    def test_non_text_addresses_recover_to_safe_defaults(self):
        for value in (4096, True, [], {}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_text(json.dumps({"base": value, "last_address": value}), encoding="utf-8")
                cfg = ConfigStore(path).load()
                for field in ("base", "last_address"):
                    self.assertIsInstance(cfg[field], str)
                    self.assertEqual(cfg[field], default_config()[field])

    def test_invalid_remember_flags_do_not_load_passwords(self):
        for flag in ("false", "true", 1, None, {"enabled": True}):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_text(json.dumps({"remember_password": flag, "password": "secret",
                                            "remember_serial_password": flag, "serial_password": "uart-secret"}),
                                encoding="utf-8")
                cfg = ConfigStore(path).load()
                self.assertIs(cfg["remember_password"], False)
                self.assertIs(cfg["remember_serial_password"], False)
                self.assertEqual(cfg["password"], "")
                self.assertEqual(cfg["serial_password"], "")

    def test_invalid_remember_flags_do_not_save_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            cfg = default_config()
            cfg.update(remember_password="false", password="secret",
                       remember_serial_password="false", serial_password="uart-secret")
            ConfigStore(path).save(cfg)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(saved["remember_password"], False)
            self.assertIs(saved["remember_serial_password"], False)
            self.assertEqual(saved["password"], "")
            self.assertEqual(saved["serial_password"], "")


class CatalogRegressions(unittest.TestCase):
    def test_malformed_type_entries_use_fallback_and_empty_metadata(self):
        for entry in ("invalid", [1], 7):
            with self.subTest(entry=entry):
                catalog = {"types": {"ec_uut": entry}}
                registers, exact = top_import.registers_for(catalog, "ec_uut")
                self.assertFalse(exact)
                self.assertTrue(registers)
                self.assertEqual(top_import.type_metadata(catalog, "ec_uut")["behaviors"],
                                 {channel: [] for channel in "ABC"})

    def test_invalid_register_names_widths_and_offsets_are_skipped(self):
        catalog = {"types": {"ec_uut": {"registers": [
            {"offset": "0x00C", "name": "EC_ID", "width": "14"},
            {"offset": "0x010", "width": 32},
            {"offset": "0x014", "name": ["BAD_NAME"], "width": 32},
            {"offset": "0x018", "name": "BAD_WIDTH", "width": "invalid"},
            {"offset": "-4", "name": "NEGATIVE_OFFSET", "width": 32},
            {"offset": "0x01C", "name": "ZERO_WIDTH", "width": 0},
        ]}}}
        registers, exact = top_import.registers_for(catalog, "ec_uut")
        self.assertTrue(exact)
        self.assertEqual([reg["name"] for reg in registers], ["IRQ_REG1", "IRQ_REG2", "EC_ID"])
        self.assertEqual(registers[-1]["width"], 14)

    def test_invalid_presets_and_field_layouts_do_not_reach_gui(self):
        for malformed in ("invalid", [None, {}, {"name": "missing value"}]):
            with self.subTest(malformed=malformed):
                catalog = {"types": {"ec_uut": {"registers": [
                    {"offset": "0x00C", "name": "EC_ID", "width": 32,
                     "aliases": malformed, "buttons": malformed, "fields": malformed}]}}}
                registers, exact = top_import.registers_for(catalog, "ec_uut")
                self.assertTrue(exact)
                self.assertEqual(registers[-1]["aliases"], [])
                self.assertEqual(registers[-1]["buttons"], [])
                self.assertEqual(registers[-1]["fields"], [])

    def test_nested_register_data_is_isolated_and_validated(self):
        catalog = {"types": {"ec_uut": {"registers": [
            {"offset": "0x00C", "name": "EC_ID", "width": 32,
             "aliases": [{"name": "run", "value": "0x1"}, {"name": "bad", "value": "NaN"}],
             "fields": [{"name": "id", "low": 0, "width": 14},
                        {"name": "bad", "low": -1, "width": 2},
                        {"name": "overflow", "low": 31, "width": 2}]}]}}}
        registers, exact = top_import.registers_for(catalog, "ec_uut")
        self.assertTrue(exact)
        self.assertEqual(registers[-1]["aliases"], [{"name": "run", "value": "0x1"}])
        self.assertEqual(registers[-1]["fields"], [{"name": "id", "low": 0, "width": 14}])
        registers[-1]["aliases"][0]["name"] = "changed"
        self.assertEqual(catalog["types"]["ec_uut"]["registers"][0]["aliases"][0]["name"], "run")

    def test_malformed_behavior_metadata_is_ignored(self):
        catalog = {"types": {"ec_uut": {"behaviors": {
            "A": None, "B": 7, "C": [None, {}, {"name": "move"}, {"name": "run", "value": 1}]}}}}
        meta = top_import.type_metadata(catalog, "ec_uut")
        self.assertEqual(meta["behaviors"], {"A": [], "B": [], "C": [{"name": "run", "value": 1}]})

    def test_invalid_override_does_not_shadow_valid_bundled_type(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "catalog.json"
            bundled.write_text(json.dumps({"types": {"ec_uut": {"registers": [
                {"offset": "0x00C", "name": "EC_ID", "width": 32}]}}}), encoding="utf-8")
            overrides = root / "component_overrides"
            overrides.mkdir()
            (overrides / "ec_uut.json").write_text(json.dumps({"registers": [
                {"offset": "0x010", "width": 32}]}), encoding="utf-8")
            with patch.dict(os.environ, {"DEVMEMSTUDIO_IGNORE_OVERRIDES": ""}), \
                    patch("devmem_studio.top_import.resource_path", return_value=bundled), \
                    patch("devmem_studio.top_import.user_data_dir", return_value=root):
                catalog = top_import.load_type_catalog()
            registers, exact = top_import.registers_for(catalog, "ec_uut")
            self.assertTrue(exact)
            self.assertIn("EC_ID", [reg["name"] for reg in registers])


class DownloadRegressions(unittest.TestCase):
    def test_failed_ssh_download_preserves_existing_file_and_cleans_partial(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                local = Path(directory) / "saved.log"
                if existing:
                    local.write_bytes(b"previous download")
                sftp = Mock()
                sftp.stat.return_value = SimpleNamespace(st_size=100)
                def interrupted(remote, target, callback=None):
                    Path(target).write_bytes(b"partial")
                    raise OSError("connection lost")
                sftp.get.side_effect = interrupted
                session = SshSession()
                session.client = Mock()
                with patch.object(SshSession, "alive", new_callable=PropertyMock, return_value=True), \
                        patch("devmem_studio.core.paramiko.SFTPClient.from_transport", return_value=sftp):
                    with self.assertRaisesRegex(OSError, "connection lost"):
                        session.download_file("/run/media/sda/sunny.log", local)
                self.assertEqual(local.exists(), existing)
                if existing:
                    self.assertEqual(local.read_bytes(), b"previous download")
                self.assertEqual(list(Path(directory).iterdir()), [local] if existing else [])
                sftp.close.assert_called_once()

    def test_demo_download_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            board = root / "board"
            local = root / "saved.log"
            local.write_bytes(b"previous download")

            def fail_progress(done, total):
                raise RuntimeError("cancelled transfer")

            # remote_root is created lazily on the first file operation (inside the patch).
            with patch("devmem_studio.core.tempfile.mkdtemp", return_value=str(board)):
                session = DemoSession()
                session.alive = True
                with self.assertRaisesRegex(RuntimeError, "cancelled transfer"):
                    session.download_file("/run/media/sda/sunny.log", local, progress=fail_progress)
            self.assertEqual(local.read_bytes(), b"previous download")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["board", "saved.log"])

    def test_successful_download_replaces_file_only_after_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "saved.log"
            local.write_bytes(b"previous download")
            sftp = Mock()
            sftp.stat.return_value = SimpleNamespace(st_size=3)
            received = []
            def download(remote, target, callback=None):
                self.assertNotEqual(Path(target), local)
                Path(target).write_bytes(b"new")
                callback(3, 3)
                self.assertEqual(local.read_bytes(), b"previous download")
            sftp.get.side_effect = download
            session = SshSession()
            session.client = Mock()
            with patch.object(SshSession, "alive", new_callable=PropertyMock, return_value=True), \
                    patch("devmem_studio.core.paramiko.SFTPClient.from_transport", return_value=sftp):
                session.download_file("/run/media/sda/sunny.log", local,
                                      progress=lambda done, total: received.append((done, total)))
            self.assertEqual(local.read_bytes(), b"new")
            self.assertEqual(received, [(3, 3)])
            self.assertEqual(list(Path(directory).iterdir()), [local])
            sftp.close.assert_called_once()

    def test_failed_local_replace_keeps_previous_download(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "saved.log"
            local.write_bytes(b"previous download")
            sftp = Mock()
            sftp.stat.return_value = SimpleNamespace(st_size=3)
            sftp.get.side_effect = lambda remote, target, callback=None: Path(target).write_bytes(b"new")
            session = SshSession()
            session.client = Mock()
            with patch.object(SshSession, "alive", new_callable=PropertyMock, return_value=True), \
                    patch("devmem_studio.core.paramiko.SFTPClient.from_transport", return_value=sftp), \
                    patch("devmem_studio.core.os.replace", side_effect=PermissionError("file is in use")):
                with self.assertRaises(PermissionError):
                    session.download_file("/run/media/sda/sunny.log", local)
            self.assertEqual(local.read_bytes(), b"previous download")
            self.assertEqual(list(Path(directory).iterdir()), [local])


class DemoValidationRegressions(unittest.TestCase):
    def test_quiet_invalid_writes_do_not_mutate_memory(self):
        cases = ((0x1001, 32, 1), (0x1000, 7, 1), (0x1000, 32, -1),
                 (0x1000, 32, 1 << 32), (-4, 32, 1), (1 << 32, 32, 1))
        with tempfile.TemporaryDirectory() as directory, \
                patch("devmem_studio.core.tempfile.mkdtemp", return_value=directory):
            session = DemoSession()
            session.alive = True
            for address, width, value in cases:
                with self.subTest(address=address, width=width, value=value):
                    before = dict(session.memory)
                    with self.assertRaises(ValueError):
                        session.write(address, width, value, quiet=True)
                    self.assertEqual(session.memory, before)

    def test_quiet_reads_reject_out_of_range_addresses(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("devmem_studio.core.tempfile.mkdtemp", return_value=directory):
            session = DemoSession()
            session.alive = True
            for address in (-4, 1 << 32):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    session.read(address, quiet=True)
            self.assertEqual(session.memory, {})


class Review2026Fixes(unittest.TestCase):
    """Regressions for the 2026 review fixes: output parsing, DPAPI migration,
    parameterless ec_ instances, binary/octal biases, and read widths."""

    def test_devmem_output_diagnostic_is_not_read_as_the_value(self):
        from devmem_studio.core import parse_devmem_read_output
        # A trailing diagnostic must never replace the real value.
        value, _ = parse_devmem_read_output("0x0000002A\nread error: 13", 0xB0102208)
        self.assertEqual(value, 0x2A)
        # A line whose only number is a diagnostic is not a value at all.
        for text in ("devmem: read error: 13", "Bus error: data = 0x0",
                     "Error 13: Permission denied", "read error: 9"):
            with self.subTest(text=text):
                self.assertEqual(parse_devmem_read_output(text), (None, None))
        # Legitimate forms keep working.
        self.assertEqual(parse_devmem_read_output("Value at address 0xB0102208: 0xF0")[0], 0xF0)
        self.assertEqual(parse_devmem_read_output("0xB0102208: 0x2A")[0], 0x2A)

    def test_legacy_dpapi_ciphertext_is_blanked_not_used_as_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registers.json"
            path.write_text(json.dumps({"password": "dpapi:AAAA", "remember_password": True}),
                            encoding="utf-8")
            store = ConfigStore(path)
            cfg = store.load()
            self.assertEqual(cfg["password"], "")
            self.assertIn("无法", store.warning)

    def test_legacy_serial_password_is_migrated_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registers.json"
            path.write_text(json.dumps({"serial_password": "uart-secret",
                                        "password": "ssh-secret", "remember_password": True}),
                            encoding="utf-8")
            cfg = ConfigStore(path).load()
            self.assertEqual(cfg["serial_password"], "uart-secret")
            self.assertIs(cfg["remember_serial_password"], True)

    def test_parameterless_ec_instance_is_not_silently_lost(self):
        info = top_import.parse_top("module top; ec_a ec_a_1 ( .clk_i(clk) ); endmodule")
        self.assertEqual([c["instance"] for c in info["components"]], ["ec_a_1"])
        self.assertTrue(info["warnings"])   # bias unknown: flagged, not dropped

    def test_binary_and_octal_bias_are_accepted(self):
        for text, expected in (("module top; ec_a #(.REG_SPACE_BIAS(20'b0000_1000_0000_0000)) ec_a_1 (); endmodule", 0x800),
                               ("module top; ec_a #(.REG_SPACE_BIAS(20'o4000)) ec_a_1 (); endmodule", 0x800)):
            with self.subTest(text=text):
                info = top_import.parse_top(text)
                self.assertEqual(info["components"][0]["bias"], expected)

    def test_define_expression_is_not_silently_truncated(self):
        from devmem_studio import component_parse
        warnings = []
        table = component_parse.parse_define_table("`define REG_X 9'h0D8 + 9'h004\n", warnings)
        self.assertEqual(table, {})
        self.assertTrue(any("REG_X" in item for item in warnings))
        self.assertEqual(component_parse.parse_define_table("`define REG_B 12'b0000_1000_0000\n"),
                         {"REG_B": 0x80})
        self.assertEqual(component_parse.parse_define_table("`define REG_O 12'o1000\n"),
                         {"REG_O": 0x200})

    def test_define_z_and_x_unknown_bits_are_parseable_not_silently_dropped(self):
        from devmem_studio import component_parse
        # x/X (unknown) and z/Z (high impedance) are not base digits; they must
        # be substituted with 0 so the constant parses (never a silent ValueError
        # drop) and the value is the lower known bits.
        self.assertEqual(component_parse.parse_define_table("`define REG_Z 12'h0z0\n"),
                         {"REG_Z": 0})
        self.assertEqual(component_parse.parse_define_table("`define REG_H 12'h1xZ0\n"),
                         {"REG_H": 0x1000})
        self.assertEqual(component_parse.parse_define_table("`define REG_B 12'bzz0\n"),
                         {"REG_B": 0})

    def test_define_expression_warns_once_for_macro_arithmetic(self):
        from devmem_studio import component_parse
        # A line matching both the macro-reference and the arithmetic shape must
        # produce exactly one warning (previously duplicated).
        warnings = []
        component_parse.parse_define_table("`define REG_M `BASE + 9'h100\n", warnings)
        self.assertEqual(len([w for w in warnings if "REG_M" in w]), 1)

    def test_duplicate_parameterless_instance_is_not_listed_twice(self):
        info = top_import.parse_top(
            "module top; ec_a ec_a_1 ( .clk_i(clk) ); ec_a ec_a_1 ( .clk_i(clk) ); endmodule")
        self.assertEqual([c["instance"] for c in info["components"]], ["ec_a_1"])

    def test_block_commented_bias_is_not_read_as_live(self):
        text = """module top;
ec_a
#(
 /* .REG_SPACE_BIAS(20'hA00)
    .REG_SPACE_BIAS(20'hB00) */
 .REG_SPACE_BIAS(20'h800)
)
ec_a_1
(
);
endmodule"""
        info = top_import.parse_top(text)
        self.assertEqual([(c["instance"], c["bias"]) for c in info["components"]], [("ec_a_1", 0x800)])

    def test_read_respects_access_width(self):
        from devmem_studio.core import DemoSession
        session = DemoSession()
        session.alive = True
        session.memory[0xB0100000] = 0x1FF
        self.assertEqual(session.read(0xB0100000, width=8), 0xFF)
        self.assertEqual(session.read(0xB0100000, width=16), 0x1FF)
        self.assertEqual(session.read(0xB0100000, width=32), 0x1FF)


if __name__ == "__main__":
    unittest.main()
