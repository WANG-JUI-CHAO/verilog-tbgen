// Purely combinational module with signed ports (no clock / reset)
module alu (
    input  signed [3:0] a, b,
    input         [1:0] op,
    output reg signed [4:0] y,
    output zero
);
    always @(*) begin
        case (op)
            2'd0: y = a + b;
            2'd1: y = a - b;
            2'd2: y = a & b;
            default: y = a | b;
        endcase
    end

    assign zero = (y == 0);
endmodule
