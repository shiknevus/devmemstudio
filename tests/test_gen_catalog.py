"""Regressions for tools/gen_component_catalog.py against fixture RTL text."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_component_catalog", ROOT / "tools/gen_component_catalog.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

DEFINE_TEXT = """
`define IRQ_REG1 9'h000
`define EC_ID 9'h00C
`define A_EN 9'h05C
`define A_TX_OT 9'h064
`define PARAM1 9'h0D8
`define PARAM16 9'h114
`define PARAM66 9'h1B4
`define PARAM67 9'h1B8
"""

DECODE_TEXT = """
module ps_rw_pl_reg_uut #(parameter REG_SPACE_BIAS = 2000, parameter REG_SPACE_SIZE = 512)(
    input wr_task_vld, input [8:0] wr_task_addr, input [31:0] i_st_wr_data,
    input [8:0] rd_addr_d1, input [8:0] rd_addr_d2, output reg [31:0] o_st_rd_data);
    reg [13:0] ec_id; reg a_en; reg [19:0] a_tx_ot; reg param1; reg [7:0] param16; reg a_wo;
    reg [7:0] param66; reg [7:0] param67;
    always @(posedge clk_i) begin
        ec_id <= (wr_task_vld && wr_task_addr == `EC_ID) ? i_st_wr_data[13:0] : ec_id;
        a_en <= (wr_task_vld && wr_task_addr == `A_EN) ? i_st_wr_data[0] : a_en;
        a_tx_ot <= (wr_task_vld && wr_task_addr == `A_TX_OT) ? i_st_wr_data : a_tx_ot;
        param1 <= (wr_task_vld && wr_task_addr == `PARAM1) ? i_st_wr_data : param1;
        param16 <= (wr_task_vld && wr_task_addr == `PARAM16) ? i_st_wr_data : param16;
        param66 <= (wr_task_vld && wr_task_addr == `PARAM66) ? i_st_wr_data[7:0] : param66;
        param67 <= (wr_task_vld && wr_task_addr == `PARAM67) ? i_st_wr_data : param67;
        a_wo <= (wr_task_vld && wr_task_addr == `A_TX_OT) ? i_st_wr_data : a_wo;
    end
    always @(*) begin
        case ( rd_addr_d2[8:0] )
            `IRQ_REG1 : o_st_rd_data <= irq_reg1;
            `EC_ID : o_st_rd_data <= {18'd0, ec_id};
            `A_EN : o_st_rd_data <= {31'd0, a_en};
            `PARAM16 : o_st_rd_data <= {24'd0, param16};
            `PARAM1 : o_st_rd_data <= param1;
            `PARAM66 : o_st_rd_data <= {24'd0, param66};
            `PARAM67 : o_st_rd_data <= param67;
            //`A_TX_OT : o_st_rd_data <= a_tx_ot;
            default: o_st_rd_data <= 32'h7FFFFFFF;
        endcase
    end
endmodule
"""


class ParseTests(unittest.TestCase):
    def test_define_table(self):
        self.assertEqual(gen.parse_define_table(DEFINE_TEXT),
                         {"IRQ_REG1": 0x000, "EC_ID": 0x00C, "A_EN": 0x05C, "A_TX_OT": 0x064,
                          "PARAM1": 0x0D8, "PARAM16": 0x114, "PARAM66": 0x1B4, "PARAM67": 0x1B8})

    def test_decode_file_classifies_widths_and_permissions(self):
        entries = {item["name"]: item for item in gen.parse_decode_file(DECODE_TEXT)}
        self.assertEqual(entries["EC_ID"], {"name": "EC_ID", "writable": True, "readable": True, "width": 14})
        self.assertEqual(entries["A_EN"]["width"], 1)
        self.assertEqual(entries["PARAM16"]["width"], 8)          # write slice wins
        self.assertTrue(entries["PARAM1"]["writable"] and entries["PARAM1"]["readable"])
        self.assertEqual(entries["IRQ_REG1"], {"name": "IRQ_REG1", "writable": False, "readable": True, "width": 32})
        self.assertTrue(entries["A_TX_OT"]["writable"] and not entries["A_TX_OT"]["readable"])  # write-only
        self.assertNotIn("DEFAULT", entries)


class MatchTests(unittest.TestCase):
    def test_exact_prefix_and_alias_matching(self):
        files = {"sf_doo": Path("a/sf_doo.sv"), "slv_pul_axis": Path("b/slv_pul_axis.sv"),
                 "siemens_cnc": Path("c/siemens_cnc.sv"), "1di": Path("d/1di.sv")}
        matched, ambiguous = gen.match_decode_files(
            ["ec_sf_door", "ec_slv_pul_axis", "ec_siemens_cnc", "ec_1di1do", "ec_unknown"], files)
        self.assertEqual(matched["ec_sf_door"], files["sf_doo"])       # alias
        self.assertEqual(matched["ec_slv_pul_axis"], files["slv_pul_axis"])
        self.assertEqual(matched["ec_siemens_cnc"], files["siemens_cnc"])  # prefix
        self.assertEqual(matched["ec_1di1do"], files["1di"])           # underscore-insensitive exact
        self.assertNotIn("ec_unknown", matched)
        self.assertEqual(ambiguous, [])


HARVEST_TOP = """
module ec_uut();
localparam A_BHA_NUM = 200; // 1home 2jog 3move 20jog[safe] 21move[safe] 30getpos
localparam B_BHA_NUM = 105;
localparam C_BHA_NUM = 150; // beh: generic 1-128
    ps_rw_pl_reg_uut u_reg(
         .param1 (param1)
        ,.param16 (param16)
        ,.param51 (param51) // r_pf_abspos
        ,.param52 ({29'd0,b_reset,b_son,param16[0]}) //out cmd
        ,.param64 ({7'd0,i_axis_limf}) //fwd limit
        //,.param4 (param4)
        //,.param66 (param66)
        ,.param67 (param67)
    );
    proactive_beh_uut u_beh(
         .rcfg_spd_max (param1)
        ,.rcfg_home_acc (param5)
        ,.rcfg_jog_acc (param5)
        ,.rserv_dir (param16[0])
        //,.r_commented (param7)
    );
    assign o_spdx1 = param16[0];
    assign o_spdx2 = param16[1];
endmodule
"""


class HarvestTests(unittest.TestCase):
    def test_param_notes_skip_commented_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ec_uut.sv"
            path.write_text(HARVEST_TOP, encoding="utf-8")
            result = gen.harvest_type_notes(path)
        self.assertEqual(result["notes"], {"PARAM51": "r_pf_abspos", "PARAM52": "out cmd",
                                           "PARAM64": "fwd limit"})
        self.assertEqual([item["name"] for item in result["behaviors"]["A"]],
                         ["home", "jog", "move", "jog[safe]", "move[safe]", "getpos"])
        self.assertEqual(result["behaviors"]["A"][0], {"name": "home", "value": 1})
        self.assertNotIn("B", result["behaviors"])   # empty comment
        self.assertNotIn("C", result["behaviors"])   # range comment yields no pairs

    def test_signals_from_ports_and_assigns_skip_regfile_hookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ec_uut.sv"
            path.write_text(HARVEST_TOP, encoding="utf-8")
            result = gen.harvest_type_notes(path)
        self.assertEqual(result["signals"]["PARAM1"], "rcfg_spd_max")
        self.assertEqual(result["signals"]["PARAM5"], "rcfg_home_acc/rcfg_jog_acc")
        self.assertEqual(result["signals"]["PARAM16"], "rserv_dir/o_spdx1/o_spdx2")
        self.assertEqual(result["signals"]["PARAM51"], "r_pf_abspos")   # identifier comment fallback
        self.assertEqual(result["signals"]["PARAM52"], "b_reset/b_son")  # concat hookup, no cross-param leak
        self.assertEqual(result["signals"]["PARAM64"], "i_axis_limf")    # concat hookup signal
        self.assertNotIn("PARAM4", result["signals"])    # commented-out connection
        self.assertNotIn("PARAM7", result["signals"])    # commented-out port

    def test_find_top_file_matches_underscore_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decode = root / "ec_1di1do"
            decode.mkdir()
            (decode / "ps_rw_pl_reg_1di_1do.sv").write_text("module x; endmodule", encoding="utf-8")
            (decode / "ec_1di_1do.sv").write_text("module y; endmodule", encoding="utf-8")
            self.assertEqual(gen.find_top_file("ec_1di1do", decode / "ps_rw_pl_reg_1di_1do.sv").name, "ec_1di_1do.sv")
            self.assertIsNone(gen.find_top_file("ec_other", decode / "ps_rw_pl_reg_1di_1do.sv"))

    def test_debug_snapshots_derived_from_hookup_expressions(self):
        body = gen.strip_comments("""
    ps_rw_pl_reg_uut u_reg(
         ,.debug_reg1 (a_state_monitor)
        ,.debug_reg2 ({16'd0, irq_posedge_cnt, irq_negedge_cnt})
        ,.debug_reg3 ({{24{1'b0}},o_rst,i_error,o_spdx4,o_spdx3,o_spdx2,o_spdx1,o_rev,o_fwd})
        ,.debug_reg4 (debug_reg4)
    );""")
        snapshots = gen.harvest_debug_snapshots(body)
        self.assertNotIn("DEBUG_REG1", snapshots)                       # single signal: state history
        self.assertIn("位快照（低位→高位）：\nirq_negedge_cnt\nirq_posedge_cnt", snapshots["DEBUG_REG2"])
        self.assertIn("其余 16 位为常数", snapshots["DEBUG_REG2"])
        self.assertIn("位快照（低位→高位）：\no_fwd\no_rev\no_spdx1\no_spdx2\no_spdx3\no_spdx4\ni_error\no_rst",
                      snapshots["DEBUG_REG3"])
        self.assertIn("其余 24 位为常数", snapshots["DEBUG_REG3"])
        self.assertNotIn("DEBUG_REG4", snapshots)                       # bare self hookup
        registers = [{"name": name, "offset": "0x000", "width": 32, "readonly": True}
                     for name in ("DEBUG_REG1", "DEBUG_REG2", "DEBUG_REG3")]
        notes = gen.debug_notes_for("ec_uut", registers, snapshots)
        self.assertIn("状态机历史", notes["DEBUG_REG1"])
        self.assertIn("位快照", notes["DEBUG_REG3"])


class BuildTests(unittest.TestCase):
    def build_fixture_tree(self, directory):
        root = Path(directory)
        (root / "include_files").mkdir(parents=True)
        (root / "include_files/reg_addr_pl.vh").write_text(DEFINE_TEXT, encoding="utf-8")
        component = root / "emcc_ctrl" / "pl_exe_io" / "ec_uut"
        component.mkdir(parents=True)
        (component / "ps_rw_pl_reg_uut.sv").write_text(DECODE_TEXT, encoding="utf-8")
        (component / "ec_uut.sv").write_text(HARVEST_TOP, encoding="utf-8")
        (root / "top").mkdir(parents=True)
        top = root / "top" / "emcc_mix_top.sv"
        top.write_text("// --- flow_comp_1 --A0001_设备---\nec_uut\n#(\n .REG_SPACE_BIAS ( 20'd25600)\n)\n"
                       "ec_uut_1\n(\n);\n", encoding="utf-8")
        return root, top

    def test_build_produces_catalog_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root, top = self.build_fixture_tree(directory)
            catalog, report = gen.build(root, [top], stamp="fixed")
            self.assertEqual(report["unparsed_files"], [])
            self.assertEqual(report["unmatched_top_types"], [])
            self.assertEqual(catalog["schema"], 6)
            registers = {item["name"]: item for item in catalog["types"]["ec_uut"]["registers"]}
            self.assertEqual(registers["EC_ID"], {"offset": "0x00C", "name": "EC_ID", "width": 14, "readonly": False})
            self.assertEqual(registers["IRQ_REG1"]["readonly"], True)
            self.assertIsNone(registers["PARAM66"].get("signal"))
            self.assertTrue(registers["PARAM66"].get("unwired"))          # commented-out hookup
            self.assertIsNone(registers["PARAM67"].get("signal"))
            self.assertIsNone(registers["PARAM67"].get("unwired"))        # wired, unused: reserved
            self.assertEqual(catalog["generated_at"], "fixed")
            self.assertEqual(catalog["types"]["ec_uut"]["family"], "pl_exe_io")
            again, _ = gen.build(root, [top], stamp="fixed")
            self.assertEqual(json.dumps(catalog, sort_keys=False), json.dumps(again, sort_keys=False))

    def test_garbage_decode_file_is_reported_and_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root, top = self.build_fixture_tree(directory)
            garbage = root / "emcc_ctrl" / "pl_exe_io" / "ec_bad" / "ps_rw_pl_reg_bad.sv"
            garbage.parent.mkdir(parents=True)
            garbage.write_text("\x00\x01 binary or encrypted payload", encoding="utf-8", errors="ignore")
            _, report = gen.build(root, [top], stamp="fixed")
            self.assertTrue(any("bad" in item["file"] for item in report["unparsed_files"]))
            arguments = ["gen", "--rtl", str(root), "--out", str(root / "out.json"), "--check"]
            with patch("sys.argv", arguments):
                self.assertEqual(gen.main(), 1)

    def test_missing_define_map_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory)
            (empty / "include_files").mkdir()
            (empty / "include_files/reg_addr_pl.vh").write_text("garbage", encoding="utf-8")
            with self.assertRaises(SystemExit):
                gen.build(empty, [], stamp="fixed")

    def test_every_type_forced_to_carry_irq_registers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "include_files").mkdir()
            (root / "include_files/reg_addr_pl.vh").write_text("`define EC_ID 9'h00C\n", encoding="utf-8")
            component = root / "emcc_ctrl" / "pl_exe_io" / "ec_uut"
            component.mkdir(parents=True)
            # A decode file whose read case only covers EC_ID - no IRQ registers at all.
            (component / "ps_rw_pl_reg_uut.sv").write_text(
                "module m(input wr_task_vld, input [8:0] wr_task_addr, input [8:0] rd_addr_d2);\n"
                "always @(*) case (rd_addr_d2) `EC_ID: x = 1; default: x = 0; endcase endmodule\n",
                encoding="utf-8")
            catalog, report = gen.build(root, [], stamp="fixed")
            registers = catalog["types"]["ec_uut"]["registers"]
            self.assertEqual([item["name"] for item in registers][:2], ["IRQ_REG1", "IRQ_REG2"])
            self.assertTrue(all(item["readonly"] for item in registers[:2]))
            self.assertEqual(report["forced_irq_registers"], ["ec_uut:IRQ_REG1", "ec_uut:IRQ_REG2"])


class ComponentFolderTests(unittest.TestCase):
    """Runtime 导入组件: re-parsing one component folder without the CLI."""

    def build_tree(self, directory, with_defines=True, with_decode=True):
        root = Path(directory)
        if with_defines:
            (root / "include_files").mkdir(parents=True, exist_ok=True)
            (root / "include_files/reg_addr_pl.vh").write_text(DEFINE_TEXT, encoding="utf-8")
        component = root / "emcc_ctrl" / "pl_exe_io" / "ec_uut"
        component.mkdir(parents=True, exist_ok=True)
        if with_decode:
            (component / "ps_rw_pl_reg_uut.sv").write_text(DECODE_TEXT, encoding="utf-8")
            (component / "ec_uut.sv").write_text(HARVEST_TOP, encoding="utf-8")
        return component

    def test_parse_component_folder_builds_full_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            component = self.build_tree(directory)
            entry, info = gen.parse_component_folder(component)
        names = {item["name"]: item for item in entry["registers"]}
        self.assertIn("EC_ID", names)
        self.assertIn("PARAM67", names)
        self.assertEqual(entry["family"], "pl_exe_io")
        self.assertEqual(entry["decode_file"], "ps_rw_pl_reg_uut.sv")
        self.assertEqual(entry["imported_from"], str(component))
        self.assertEqual(entry["notes"]["PARAM64"], "fwd limit")
        self.assertEqual(entry["behaviors"]["A"][0], {"name": "home", "value": 1})
        self.assertEqual(names["PARAM1"]["signal"], "rcfg_spd_max")
        self.assertTrue(names["PARAM66"]["unwired"])
        self.assertEqual(info["defines"].replace("\\", "/").endswith("include_files/reg_addr_pl.vh"), True)
        self.assertTrue(info["top_file"].replace("\\", "/").endswith("ec_uut.sv"))

    def test_parse_component_folder_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ValueError, "ps_rw_pl_reg"):
                gen.parse_component_folder(Path(empty))
        with tempfile.TemporaryDirectory() as directory:
            component = self.build_tree(directory, with_defines=False)
            with self.assertRaisesRegex(ValueError, "reg_addr_pl.vh"):
                gen.parse_component_folder(component)


if __name__ == "__main__":
    unittest.main()
