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

    def test_unheaded_ec_instances_fall_back_directly(self):
        """Mix-top sources without flow_comp_N headers still yield components:
        each active ``ec_*`` instantiation is matched and its REG_SPACE_BIAS
        taken as the grid address. CRLF line endings must be tolerated."""
        text = ("// The following is the flow components\r\n"
                "ec_superisys_485_modbus_rtu\r\n"
                "#(\r\n"
                "        .REG_SPACE_BIAS         (20'h3000                 ), //地址\r\n"
                "        .REG_SPACE_SIZE         (`REG_SPACE_SIZE          )\r\n"
                ")\r\n"
                "ec_superisys_485_modbus_rtu_27\r\n"
                "(\r\n"
                "        .o_st_rd_data           ( sub_comp_rd_dat[20]    ),\r\n"
                "        .o_st_rd_vld            ( sub_comp_rd_vld[20]    ),\r\n"
                "        .o_intr_irq             ( map_irq[20]            )\r\n"
                ");\r\n")
        info = top_import.parse_top(text)
        self.assertEqual(len(info["components"]), 1)
        comp = info["components"][0]
        self.assertEqual((comp["module_type"], comp["instance"], comp["label"]),
                         ("ec_superisys_485_modbus_rtu", "ec_superisys_485_modbus_rtu_27",
                          "ec_superisys_485_modbus_rtu_27"))
        self.assertEqual(comp["bias"], 0x3000)
        self.assertEqual((comp["address"], comp["index"], comp["seq"]), ("0x3000", 20, 27))
        self.assertFalse(comp["disabled"])
        self.assertEqual(info["warnings"], [])

    def test_unheaded_skips_non_ec_instances(self):
        """Loose instances that do not start with ``ec_`` are left out."""
        text = """slave_rs485_arbiter
#(
        .REG_SPACE_BIAS         ( 20'h3000)
)
u_slave_rs485_arbiter
(
        .o_st_rd_data           ( sub_comp_rd_dat[0]    )
);
"""
        info = top_import.parse_top(text)
        self.assertEqual(len(info["components"]), 0)
        self.assertEqual(info["warnings"], [])

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

    def test_label_after_dash_run_without_double_dash_separator(self):
        """Template tops write the label directly after the dash run: -----标签."""
        text = """// --- flow_comp_1 -----进料1
    ec_siemens_cnc
    #(
         .REG_SPACE_BIAS     ( 20'd25600)
    )
    ec_siemens_cnc_1
    (
    );
// --- flow_comp_2 -----河南思睿2#爪
//    ec_slv_pul_axis
//    #(
//         .REG_SPACE_BIAS     ( 20'd26624)
//    )
//    ec_slv_pul_axis_2
//    (
//    );
// --- flow_comp_3 -----
//    ec_1di
//    #(
//         .REG_SPACE_BIAS     ( 20'd28160)
//    )
//    ec_1di_3
//    (
//    );
"""
        info = top_import.parse_top(text)
        components = info["components"]
        self.assertEqual(len(components), 3)
        first = components[0]
        self.assertEqual((first["seq"], first["label"], first["module_type"]),
                         (1, "进料1", "ec_siemens_cnc"))
        self.assertFalse(first["disabled"])
        second = components[1]
        self.assertEqual((second["label"], second["module_type"], second["disabled"]),
                         ("河南思睿2#爪", "ec_slv_pul_axis", True))
        third = components[2]
        self.assertEqual((third["label"], third["disabled"]), ("ec_1di_3", True))
        self.assertEqual([c["seq"] for c in components], [1, 2, 3])
        self.assertEqual(info["warnings"], [])

    def test_empty_header_label_falls_back_to_instance_name(self):
        """A component whose header carries no label must not render as a blank row:
        the parser fills the label with the instance name so every consumer
        (tree, title, export) sees a non-empty name."""
        text = """// --- flow_comp_32 -----
//    ec_1di
//    #(
//         .REG_SPACE_BIAS     ( 20'd101888)
//    )
//    ec_1di_32
//    (
//    );
// --- flow_comp_33 -----
//    ec_2di
//    #(
//         .REG_SPACE_BIAS     ( 20'd102400)
//    )
//    ec_2di_33
//    (
//    );
"""
        info = top_import.parse_top(text)
        components = info["components"]
        self.assertEqual(len(components), 2)
        self.assertEqual([c["label"] for c in components], ["ec_1di_32", "ec_2di_33"])
        self.assertTrue(all(c["label"] for c in components))
        self.assertEqual(components[0]["instance"], "ec_1di_32")
        self.assertEqual(info["warnings"], [])

    def test_double_comment_module_lines_are_still_disabled(self):
        """Blocks whose module line is nested inside another comment (//    // ec_x) parse as disabled."""
        text = """// --- flow_comp_125 -----EMCC60背板
//    // ec_emcc60_board
//    // #(
//    //      .REG_SPACE_BIAS     (20'd100864)
//    // )
//    // ec_emcc60_board_125
//    // (
//    // );
"""
        info = top_import.parse_top(text)
        components = info["components"]
        self.assertEqual(len(components), 1)
        comp = components[0]
        self.assertEqual((comp["label"], comp["module_type"], comp["instance"], comp["disabled"]),
                         ("EMCC60背板", "ec_emcc60_board", "ec_emcc60_board_125", True))
        self.assertEqual(comp["bias"], 100864)
        self.assertEqual(info["warnings"], [])

    def test_stale_reference_name_does_not_shadow_real_ec_component(self):
        """A nested comment reference (//    // ec_x) must not win over the real ec_* instantiation.

        Template tops carry an outdated reference template followed by the actual
        module below it; the ec_* declaration at the shallowest comment depth is
        the real component."""
        text = """// --- flow_comp_27 -----河南思睿1#爪RFID读写一
//    // ec_sp_rfid
//    // #(
//    //      .REG_SPACE_BIAS     (20'd54272)
//    // )
//    // ec_sp_rfid_27
//    // (
//    // );
//
ec_superisys_485_modbus_rtu
#(
    //  .REG_SPACE_BIAS         (20'd54272                ), //寄存器地址
    .REG_SPACE_BIAS         (20'h3000                 ), //寄存器地址
    .REG_SPACE_SIZE         (\`REG_SPACE_SIZE          ), //地址偏移
    .CLK_FREQ               (156250000                )
)
ec_superisys_485_modbus_rtu_27
(
    .clk_i                  ( clk                     ),
    .rst                    ( reset                   ),
    .ps_reg_clk             ( ps_reg_clk              ),
    .ps_reg_reset           ( ps_reg_reset            ),
    .i_st_wr_en             ( ps_reg_we               ),
    .i_st_wr_addr           ( ps_reg_addr             ),
    .i_st_rd_en             ( ps_reg_re               ),
    .i_st_rd_addr           ( ps_reg_rd_addr          )
);
"""
        info = top_import.parse_top(text)
        components = info["components"]
        self.assertEqual(len(components), 1)
        comp = components[0]
        self.assertEqual((comp["label"], comp["module_type"], comp["instance"], comp["disabled"]),
                         ("河南思睿1#爪RFID读写一", "ec_superisys_485_modbus_rtu",
                          "ec_superisys_485_modbus_rtu_27", False))
        self.assertEqual(comp["bias"], 0x3000)
        self.assertEqual(info["warnings"], [])

    def test_hidden_comment_reference_only_block_stays_disabled(self):
        """Reference-only blocks (nested // + real commented ec_* at depth 1) stay disabled
        but still report the real ec_* module name instead of the stale reference."""
        text = """// --- flow_comp_26 -----回库交换平台_RFID读写一
//    // ec_sp_rfid
//    // #(
//    //      .REG_SPACE_BIAS     (20'd56320)
//    // )
//    // ec_sp_rfid_26
//    // (
//    // );
//
//ec_superisys_485_modbus_rtu
//#(
//        .REG_SPACE_BIAS         (20'd56320                ), //寄存器地址
//        .REG_SPACE_SIZE         (\`REG_SPACE_SIZE          ),
//        .CLK_FREQ               (156250000                )
//)
//ec_superisys_485_modbus_rtu_26
//(
//        .clk_i                  ( clk                     ),
//        .rst                    ( reset                   ),
//        .ps_reg_clk             ( ps_reg_clk              ),
//        .ps_reg_reset           ( ps_reg_reset            ),
//        .i_st_wr_en             ( ps_reg_we               ),
//        .i_st_wr_addr           ( ps_reg_addr             ),
//        .i_st_rd_en             ( ps_reg_re               ),
//        .i_st_rd_addr           ( ps_reg_rd_addr          )
//);
"""
        info = top_import.parse_top(text)
        components = info["components"]
        self.assertEqual(len(components), 1)
        comp = components[0]
        self.assertEqual((comp["module_type"], comp["instance"], comp["disabled"]),
                         ("ec_superisys_485_modbus_rtu", "ec_superisys_485_modbus_rtu_26", True))
        self.assertEqual(comp["bias"], 56320)
        self.assertEqual(info["warnings"], [])


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

    def test_runtime_component_overrides_merge_into_bundled_catalog(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            overrides = Path(directory) / "component_overrides"
            overrides.mkdir()
            (overrides / "ec_custom.json").write_text(json.dumps(
                {"family": "runtime", "decode_file": "custom.sv", "registers": [
                    {"offset": "0x000", "name": "IRQ_REG1", "width": 32, "readonly": True}]}),
                encoding="utf-8")
            (overrides / "broken.json").write_text("{broken", encoding="utf-8")
            with patch("devmem_studio.top_import.user_data_dir", return_value=Path(directory)):
                catalog = top_import.load_type_catalog()
        self.assertIn("ec_custom", catalog["types"])
        self.assertNotIn("broken", catalog["types"])
        registers, exact = top_import.registers_for(catalog, "ec_custom")
        self.assertTrue(exact)
        self.assertEqual([item["name"] for item in registers], ["IRQ_REG1", "IRQ_REG2"])

    def test_shipped_catalog_is_well_formed(self):
        path = Path(__file__).resolve().parent.parent / "devmem_studio/data/component_catalog.json"
        if not path.exists():
            self.skipTest("catalog not generated yet")
        catalog = top_import.load_type_catalog(path)
        self.assertIsNotNone(catalog)
        self.assertEqual(catalog["schema"], 8)
        self.assertIn("位快照（低位→高位）：\no_fwd\n", catalog["types"]["ec_dv300_do"]["debug_notes"]["DEBUG_REG3"])
        self.assertIn("irq_negedge_cnt", catalog["types"]["ec_2di_2do"]["debug_notes"]["DEBUG_REG2"])
        spindle = {item["name"]: item for item in catalog["types"]["ec_slv_pul_axis"]["registers"]}
        self.assertEqual(spindle["EC_ID"]["width"], 14)
        self.assertFalse(spindle["EC_ID"]["readonly"])
        self.assertEqual(spindle["PARAM1"]["signal"], "rcfg_spd_max")
        self.assertEqual(spindle["PARAM64"]["signal"], "i_axis_limf")
        self.assertTrue(spindle["PARAM4"]["unwired"])
        # 最大/最小位置（软限位）与触摸速度：与速度/位置族一样带信号与有符号换算。
        self.assertEqual(spindle["PARAM31"]["signal"], "rcfg_pos_max")
        self.assertTrue(spindle["PARAM31"].get("signed"))
        self.assertEqual(spindle["PARAM32"]["signal"], "rcfg_pos_min")
        self.assertTrue(spindle["PARAM32"].get("signed"))
        self.assertEqual(spindle["PARAM33"]["signal"], "rcfg_touch_spd")
        # 目标/步进脉冲同属位置族：带信号且按补码显示。
        self.assertEqual(spindle["PARAM36"]["signal"], "rserv_target_pulse")
        self.assertTrue(spindle["PARAM36"].get("signed"))
        self.assertEqual(spindle["PARAM37"]["signal"], "rserv_step_pulse")
        self.assertTrue(spindle["PARAM37"].get("signed"))
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
