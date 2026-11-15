// ANSI-style module with a parameter, active-low reset and enable/load
module counter #(
    parameter WIDTH = 8,
    parameter [WIDTH-1:0] INIT = 0
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             en,
    input  wire             load,
    input  wire [WIDTH-1:0] d,
    output reg  [WIDTH-1:0] q,
    output wire             overflow
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)      q <= INIT;
        else if (load)   q <= d;
        else if (en)     q <= q + 1'b1;
    end

    assign overflow = en & (&q);
endmodule
