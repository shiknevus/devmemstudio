"""Parser and catalog regressions for the imported mix-top component directory."""
import json
from pathlib import Path
import tempfile
import unittest

from devmem_studio import top_import

FIXTURE_TOP = """
// --- flow_comp_1 --A0001_磨床1---
ec_siemens_cnc
#(
     .REG_SPACE_BIAS     ( 20'd25600)
    ,.REG_SPACE_SIZE     ( `REG_SPACE_SIZE           )
)
ec_siemens_cnc_1
(
      .clk_i                 ( clk               )
);

// // --- flow_comp_2 --A0002_已禁用---
// ec_disabled_uut
// #(
//      .REG_SPACE_BIAS     ( 20'd26624)
//     ,.REG_SPACE_SIZE     ( `REG_SPACE_SIZE           )
// )
// ec_disabled_uut_2
// (
// );

// --- flow_comp_3 --无编码组件---
ec_1di
#(
     .REG_SPACE_BIAS     ( 20'h6600)
)
ec_1di_3
(
);

// --- flow_comp_4 --A0004_离网格---
ec_1do
#(
     .REG_SPACE_BIAS     ( 20'd2049)
)
ec_1do_4
(
);
"""

CATALOG = {"schema": 1, "types": {"ec_uut": {"family": "pl_exe_io", "decode_file": "x.sv", "registers": [
    {"offset": "0x00C", "name": "EC_ID", "width": 14, "readonly": False},
    {"offset": "0x000", "name": "IRQ_REG1", "width": 32, "readonly": True},
]}}}


class ParseTopTests(unittest.TestCase):
    def setUp(self):
        self.info = top_import.parse_top(FIXTURE_TOP)

    def test_extracts_all_blocks_with_addresses(self):
        components = self.info["components"]
        self.assertEqual(len(components), 4)
        first = components[0]
        self.assertEqual((first["seq"], first["label"], first["module_type"], first["instance"]),
                         (1, "A0001_磨床1", "ec_siemens_cnc", "ec_siemens_cnc_1"))
        self.assertEqual(first["bias"], 0x6400)
        self.assertEqual((first["address"], first["index"], first["code"]), ("0x6400", 46, "A0001"))
        self.assertFalse(first["disabled"])

    def test_detects_disabled_blocks_from_commented_body(self):
        second = self.info["components"][1]
        self.assertTrue(second["disabled"])
        self.assertEqual((second["module_type"], second["bias"]), ("ec_disabled_uut", 0x6800))

    def test_hex_bias_and_missing_code(self):
        third = self.info["components"][2]
        self.assertEqual((third["bias"], third["address"], third["code"]), (0x6600, "0x6600", ""))

    def test_off_grid_bias_reports_warning(self):
        fourth = self.info["components"][3]
        self.assertIsNone(fourth["index"])
        self.assertEqual(len(self.info["warnings"]), 1)
        self.assertIn("flow_comp_4", self.info["warnings"][0])


class ComponentsParamTests(unittest.TestCase):
    def test_locates_param_file_and_base_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl" / "include_files").mkdir(parents=True)
            (root / "rtl" / "top" / "proj").mkdir(parents=True)
            top = root / "rtl" / "top" / "proj" / "emcc_mix_top.sv"
            top.write_text("// top", encoding="utf-8")
            param = root / "rtl" / "include_files" / "components_param.vh"
            param.write_text("`define REG_SPACE_SIZE 512\n`define PL_CFG_BASE_ADDR {32'hB010_0000}\n",
                             encoding="utf-8")
            self.assertEqual(top_import.find_components_param(top), param)
            self.assertEqual(top_import.parse_base_address(param.read_text(encoding="utf-8")), 0xB0100000)
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(top_import.find_components_param(Path(empty) / "x" / "top.sv"))


class CatalogTests(unittest.TestCase):
    def test_registers_for_exact_table_is_sorted_and_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(CATALOG), encoding="utf-8")
            loaded = top_import.load_type_catalog(path)
            registers, exact = top_import.registers_for(loaded, "ec_uut")
            self.assertTrue(exact)
            self.assertEqual([item["name"] for item in registers], ["IRQ_REG1", "IRQ_REG2", "EC_ID"])
            registers.append({"offset": "0x0", "name": "X", "width": 32, "readonly": True})
            self.assertEqual(len(top_import.registers_for(loaded, "ec_uut")[0]), 3)

    def test_unknown_type_falls_back_with_common_header(self):
        for catalog in (None, {"types": {}}, CATALOG):
            registers, exact = top_import.registers_for(catalog, "ec_missing")
            self.assertFalse(exact)
            names = {item["name"] for item in registers}
            self.assertIn("EC_ID", names)
            self.assertIn("A_TASK_ID", names)
            self.assertIn("IRQ_REG1", names)

    def test_exact_table_always_carries_the_irq_header(self):
        stripped = {"types": {"ec_uut": {"registers": [
            {"offset": "0x00C", "name": "EC_ID", "width": 32, "readonly": False}]}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(stripped), encoding="utf-8")
            registers, exact = top_import.registers_for(top_import.load_type_catalog(path), "ec_uut")
        self.assertTrue(exact)
        self.assertEqual([item["name"] for item in registers], ["IRQ_REG1", "IRQ_REG2", "EC_ID"])
        self.assertTrue(all(item["readonly"] for item in registers[:2]))

    def test_missing_catalog_file_returns_none(self):
        self.assertIsNone(top_import.load_type_catalog(Path(tempfile.gettempdir()) / "definitely-missing.json"))

    def test_shipped_catalog_is_well_formed(self):
        path = Path(__file__).resolve().parent.parent / "devmem_studio/data/component_catalog.json"
        if not path.exists():
            self.skipTest("catalog not generated yet")
        catalog = top_import.load_type_catalog(path)
        self.assertIsNotNone(catalog)
        self.assertEqual(catalog["schema"], 6)
        self.assertIn("位快照（低位→高位）：\no_fwd\n", catalog["types"]["ec_dv300_do"]["debug_notes"]["DEBUG_REG3"])
        self.assertIn("irq_negedge_cnt", catalog["types"]["ec_2di_2do"]["debug_notes"]["DEBUG_REG2"])
        spindle = {item["name"]: item for item in catalog["types"]["ec_slv_pul_axis"]["registers"]}
        self.assertEqual(spindle["EC_ID"]["width"], 14)
        self.assertFalse(spindle["EC_ID"]["readonly"])
        self.assertEqual(spindle["PARAM1"]["signal"], "rcfg_spd_max")
        self.assertEqual(spindle["PARAM64"]["signal"], "i_axis_limf")
        self.assertTrue(spindle["PARAM4"]["unwired"])
        self.assertIsNone(spindle["PARAM31"].get("signal"))
        annotated = sum(1 for entry in catalog["types"].values() for item in entry["registers"]
                        if item["name"].startswith("PARAM"))
        self.assertTrue(annotated >= 300)  # every implemented PARAM carries a state
        self.assertEqual(catalog["types"]["ec_slv_pul_axis"]["behaviors"]["A"][0],
                         {"name": "home", "value": 1})
        self.assertIn("fwd limit", catalog["types"]["ec_slv_pul_axis"]["notes"]["PARAM64"])
        for entry in catalog["types"].values():
            offsets = [int(item["offset"], 16) for item in entry["registers"]]
            self.assertEqual(offsets, sorted(offsets))
            names = [item["name"] for item in entry["registers"]]
            self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
