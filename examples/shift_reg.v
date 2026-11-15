// Verilog-1995 (non-ANSI) style: directions and widths declared in the body,
// active-high reset, width set by a body-level parameter
module shift_reg (clk, reset, din, dout);
    parameter N = 8;

    input clk, reset;
    input din;
    output [N-1:0] dout;
    reg    [N-1:0] dout;

    always @(posedge clk) begin
        if (reset) dout <= {N{1'b0}};
        else       dout <= {dout[N-2:0], din};
    end
endmodule
