"""Unit tests for tbgen.  Run:  python -m unittest discover -s tests -v"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tbgen  # noqa: E402

EXAMPLES = ROOT / "examples"


def ports_of(src: str, idx: int = 0):
    return {p.name: p for p in tbgen.parse_modules(src)[idx].ports}


class TestParsing(unittest.TestCase):
    def test_ansi_with_params(self):
        mod = tbgen.parse_modules(
            "module m #(parameter W = 8, D = W*2) (input clk, input [W-1:0] a, b, output reg [D-1:0] y);"
            "endmodule")[0]
        self.assertEqual([p.name for p in mod.params], ["W", "D"])
        self.assertEqual(mod.params[1].value, "W*2")
        ports = {p.name: p for p in mod.ports}
        self.assertEqual(ports["b"].direction, "input")      # inherits from 'a'
        self.assertEqual(ports["b"].packed, "[W-1:0]")
        self.assertEqual(ports["y"].direction, "output")
        env = tbgen.param_env(mod.params)
        self.assertEqual(tbgen.port_width(ports["y"], env), 16)

    def test_non_ansi(self):
        ports = ports_of("""
            module top (clk, rst_n, a, y);
              parameter N = 4;
              input clk, rst_n;
              input [N-1:0] a;
              output [7:0] y;
              reg [7:0] y;
              task t; input [31:0] clk; endtask
            endmodule""")
        self.assertEqual(list(ports), ["clk", "rst_n", "a", "y"])
        self.assertEqual(ports["clk"].packed, "")           # task arg ignored
        self.assertEqual(ports["a"].packed, "[N-1:0]")
        self.assertEqual(ports["y"].direction, "output")

    def test_comments_are_ignored(self):
        ports = ports_of("module m ( // input fake,\n input /* output */ a, /* x */ output b);"
                         "endmodule")
        self.assertEqual(ports["a"].direction, "input")
        self.assertEqual(ports["b"].direction, "output")
        self.assertNotIn("fake", ports)

    def test_signed_and_systemverilog(self):
        ports = ports_of("module m (input logic signed [7:0] a, output logic [3:0][7:0] y,"
                         " input wire [7:0] mem [0:3]); endmodule")
        self.assertTrue(ports["a"].signed)
        self.assertEqual(ports["y"].packed, "[3:0][7:0]")
        self.assertEqual(ports["mem"].unpacked, "[0:3]")

    def test_multiple_modules_and_top(self):
        mods = tbgen.parse_modules("""
            module sub(input a, output b); endmodule
            module top(input x, output y); sub u0 (.a(x), .b(y)); endmodule""")
        self.assertEqual([m.name for m in tbgen.find_tops(mods)], ["top"])

    def test_big5_comments(self):
        data = "module m (input clk, // 時脈\n output y); endmodule\n".encode("cp950")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.v"
            f.write_bytes(data)
            mod = tbgen.parse_modules(tbgen.read_source(f))[0]
        self.assertEqual([p.name for p in mod.ports], ["clk", "y"])


class TestClassification(unittest.TestCase):
    def check(self, name, clock=False, reset=None):
        p = tbgen.Port(name, "input")
        self.assertEqual(tbgen.is_clock(p), clock, name)
        if not clock:
            self.assertEqual(tbgen.reset_polarity(p), reset, name)

    def test_clocks(self):
        for n in ("clk", "CLK", "i_clk", "clk_100m", "aclk", "clock"):
            self.check(n, clock=True)
        for n in ("clk_en", "clk_sel", "data"):
            self.check(n, clock=False)

    def test_resets(self):
        for n in ("rst_n", "rstn", "resetn", "aresetn", "nrst", "rst_ni", "reset_b"):
            self.check(n, reset=True)
        for n in ("rst", "reset", "srst"):
            self.check(n, reset=False)

    def test_eval_expr(self):
        self.assertEqual(tbgen.eval_expr("$clog2(DEPTH)", {"DEPTH": 16}), 4)
        self.assertEqual(tbgen.eval_expr("W/2-1", {"W": 8}), 3)
        self.assertIsNone(tbgen.eval_expr("8'd3", {}))
        self.assertIsNone(tbgen.eval_expr("__import__.x", {}))


@unittest.skipUnless(shutil.which("iverilog"), "iverilog not installed")
class TestSimulation(unittest.TestCase):
    """Generated testbenches must compile and run cleanly with Icarus Verilog."""

    def run_example(self, name, *extra):
        with tempfile.TemporaryDirectory() as d:
            rc = tbgen.main([str(EXAMPLES / name), "-o", d, "--check", "-f", *extra])
        self.assertEqual(rc, 0)

    def test_counter(self):
        self.run_example("counter.v", "--random", "50", "--monitor")

    def test_alu_exhaustive(self):
        self.run_example("alu.v", "--exhaustive")

    def test_shift_reg(self):
        self.run_example("shift_reg.v", "--random", "20")


if __name__ == "__main__":
    unittest.main()
