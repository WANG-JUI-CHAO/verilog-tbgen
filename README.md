# tbgen — Verilog Testbench 自動產生器

> 讀一個 Verilog / SystemVerilog module，一秒產生**可以直接跑的 testbench**。
> Generate a ready-to-simulate testbench skeleton from any Verilog module.

寫作業、做專題、做研究時，每個 module 都要重寫一次 testbench：宣告訊號、接 DUT、產生 clock、跑 reset、dump 波形……
`tbgen` 把這些重複的工作自動化，你只要專心寫真正的測試內容。

## 功能

- **自動解析 port 與 parameter**：支援 ANSI（`module m(input [7:0] a, ...)`）與 Verilog-1995 舊式寫法（`module m(a, b); input a; ...`）
- **自動偵測 clock 與 reset**：`clk`、`i_clk`、`aclk`… 與 `rst_n`、`rstn`、`reset`… 並判斷 active-high / active-low
- **產生完整骨架**：訊號宣告、參數化 DUT instance、clock、reset 時序、VCD 波形輸出、timeout 看門狗
- **測試向量**：`--random N` 隨機測試、`--exhaustive` 窮舉所有輸入組合（適合組合電路）
- **組合電路自動處理**：沒有 clock 時改用延遲步進
- **一鍵驗證**：`--check` 直接呼叫 Icarus Verilog 編譯並模擬
- **支援 Big5 中文註解**（Vivado 專案常見），不會亂碼報錯
- 多個檔案時自動找出 top module；預設不覆蓋已存在的 testbench
- 純 Python 標準函式庫，**不需安裝任何套件**

## 安裝

需要 Python 3.10 以上。要用 `--check` 的話另外安裝 [Icarus Verilog](https://bleyer.org/icarus/)。

```bash
git clone https://github.com/WANG-JUI-CHAO/verilog-tbgen.git
cd verilog-tbgen
python tbgen.py --help
```

## 使用方式

```bash
# 最基本：產生 tb_counter.v
python tbgen.py examples/counter.v

# 加 50 組隨機輸入，並用 iverilog 驗證能跑
python tbgen.py examples/counter.v --random 50 --check

# 組合電路：窮舉所有輸入，並用 $monitor 印出結果
python tbgen.py examples/alu.v --exhaustive --monitor

# 多檔案專案：指定 top，輸出到 sim/
python tbgen.py rtl/top.v rtl/fifo.v rtl/ctrl.v -t top -o sim/
```

| 參數 | 說明 |
|---|---|
| `-t, --top` | 指定 module（預設：自動找出沒被其他 module 例化的 top） |
| `-o, --outdir` | 輸出資料夾 |
| `--period` | clock 週期 / 組合電路步進時間（預設 10） |
| `--reset-cycles` | reset 持續週期數（預設 5） |
| `--random N` | 加入 N 組隨機輸入 |
| `--exhaustive` | 窮舉所有輸入組合（預設上限 20 bits） |
| `--monitor` | 加入 `$monitor` 印出所有 port |
| `--check` | 用 iverilog 編譯並模擬 |
| `--stdout` | 印到螢幕而不寫檔 |
| `-f, --force` | 覆蓋已存在的檔案 |

## 輸出範例

輸入 [`examples/counter.v`](examples/counter.v)：

```verilog
module counter #(parameter WIDTH = 8, parameter [WIDTH-1:0] INIT = 0) (
    input  wire             clk, rst_n, en, load,
    input  wire [WIDTH-1:0] d,
    output reg  [WIDTH-1:0] q,
    output wire             overflow
);
```

執行 `python tbgen.py examples/counter.v --random 50` 產生（節錄）：

```verilog
module tb_counter;
    localparam CLK_PERIOD     = 10;
    localparam RESET_CYCLES   = 5;
    localparam WIDTH          = 8;
    ...
    reg  [WIDTH-1:0] d;
    wire [WIDTH-1:0] q;

    counter #(
        .WIDTH (WIDTH),
        .INIT  (INIT)
    ) u_dut ( .clk (clk), .rst_n (rst_n), ... );

    initial clk = 1'b0;
    always #(CLK_PERIOD / 2.0) clk = ~clk;

    initial begin
        en = 0; load = 0; d = 0;
        rst_n = 1'b0;   // active-low
        repeat (RESET_CYCLES) @(posedge clk);
        @(negedge clk);
        rst_n = 1'b1;

        // ============ TODO: directed test sequence ============

        repeat (RANDOM_ITERS) begin
            @(negedge clk);
            en = $random; load = $random; d = $random;
        end
        ...
        $finish;
    end
endmodule
```

輸入在 clock **負緣**改變，避免與 DUT 在正緣取樣產生 race condition。

## 在 Vivado 中使用

1. 產生 testbench：`python tbgen.py path/to/your_module.v -o sim/`
2. Vivado → **Add Sources → Add or create simulation sources** → 加入 `tb_xxx.v`
3. 在 TODO 處加入你的測試，然後 **Run Simulation**

## 執行測試

```bash
python -m unittest discover -s tests -v
```

## 已知限制

- 以正規表示式解析，非完整的 Verilog parser：`` `define `` 巨集寬度、`` `ifdef `` 包住的 port、SystemVerilog interface port 尚未支援
- 多個 clock 時都使用同一個週期，需自行調整

## Roadmap

- [ ] 從 Python 參考模型自動產生 golden pattern 並比對（self-checking testbench）
- [ ] 偵測 `valid/ready` handshake 並產生對應的 task
- [ ] 輸出 cocotb (Python) 測試骨架
- [ ] SystemVerilog interface / package 支援

## License

MIT
