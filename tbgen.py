#!/usr/bin/env python3
"""
tbgen - Verilog / SystemVerilog testbench skeleton generator.

Parses a module's ports and parameters and writes a ready-to-simulate
testbench: signal declarations, DUT instance, clock, reset sequence,
waveform dump, timeout watchdog and optional random / exhaustive stimulus.

Usage:
    python tbgen.py counter.v
    python tbgen.py alu.v --exhaustive --monitor --check
    python tbgen.py top.v sub1.v sub2.v -t top --random 100 -o sim/
"""
from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

__version__ = "0.1.0"

DIRECTIONS = ("input", "output", "inout")


# =============================================================== data model

@dataclass
class Param:
    name: str
    value: str


@dataclass
class Port:
    name: str
    direction: str | None = None
    packed: str = ""      # e.g. "[WIDTH-1:0]"
    unpacked: str = ""    # e.g. "[0:3]"  (SystemVerilog array ports)
    signed: bool = False

    @property
    def is_input(self) -> bool:
        return self.direction == "input"

    @property
    def is_scalar(self) -> bool:
        return not self.packed and not self.unpacked


@dataclass
class Module:
    name: str
    params: list[Param] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    body: str = ""
    timescale: str | None = None
    source: Path | None = None
    warnings: list[str] = field(default_factory=list)


# =============================================================== text utils

_COMMENT_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/', re.S)
_RANGE_RE = re.compile(r"\[[^\]]*\]")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_TAIL_RE = re.compile(r"([A-Za-z_]\w*)\s*((?:\[[^\]]*\]\s*)*)$")
_MODULE_RE = re.compile(
    r"\b(?:module|macromodule)\s+(?:(?:automatic|static)\s+)?([A-Za-z_]\w*)")
_ENDMODULE_RE = re.compile(r"\bendmodule\b")
_SUBPROG_RE = re.compile(r"\b(function|task)\b.*?\bend(?:function|task)\b", re.S)
_DECL_RE = re.compile(r"\b(?:input|output|inout)\b[^;]*;")


def read_source(path: Path) -> str:
    """Read a source file; tolerate Big5 / GBK comments (common in Vivado projects)."""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp950", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def strip_comments(text: str) -> str:
    def repl(m: re.Match) -> str:
        s = m.group(0)
        if s.startswith('"'):
            return s
        return " " + "\n" * s.count("\n")
    return _COMMENT_RE.sub(repl, text)


def skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def find_close(s: str, i: int) -> int:
    """Index of the ')' matching the '(' at s[i]."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced parentheses in module header")


def split_top(s: str) -> list[str]:
    """Split on commas that are not nested inside (), [] or {}."""
    parts, cur, depth = [], [], 0
    for c in s:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


# =============================================================== parsing

def parse_params(text: str, kind: str = "parameter") -> list[Param]:
    """Parse 'parameter A = 1, B = 2' style lists (localparams are skipped)."""
    params = []
    for item in split_top(text):
        m = re.match(r"(parameter|localparam)\b\s*(.*)", item, re.S)
        if m:
            kind, item = m.group(1), m.group(2)
        if "=" not in item:
            continue
        lhs, rhs = item.split("=", 1)
        words = _IDENT_RE.findall(_RANGE_RE.sub(" ", lhs))
        if not words or "type" in words:
            continue
        if kind == "parameter":
            params.append(Param(words[-1], " ".join(rhs.split())))
    return params


def parse_port_items(items: list[str]) -> list[Port]:
    """Parse comma-separated port declarations; bare names inherit the previous one."""
    ports: list[Port] = []
    prev: Port | None = None
    for item in items:
        item = item.split("=", 1)[0].strip()   # drop SV default values
        m = _TAIL_RE.search(item)
        if not m:
            continue
        name, unpacked = m.group(1), _norm(m.group(2))
        prefix = item[:m.start()].strip()
        if not prefix:
            port = replace(prev, name=name, unpacked=unpacked) if prev else Port(name)
        else:
            words = _IDENT_RE.findall(_RANGE_RE.sub(" ", prefix))
            direction = next((w for w in words if w in DIRECTIONS), None)
            if direction is None and prev is not None:
                direction = prev.direction
            packed = "".join(_norm(r) for r in _RANGE_RE.findall(prefix))
            port = Port(name, direction, packed, unpacked, "signed" in words)
        ports.append(port)
        prev = port
    return ports


def _resolve_non_ansi(ports: list[Port], body: str, warnings: list[str]) -> list[Port]:
    """Verilog-1995 style: directions/widths are declared in the module body."""
    clean = _SUBPROG_RE.sub(" ", body)
    decl: dict[str, Port] = {}
    for m in _DECL_RE.finditer(clean):
        for p in parse_port_items(split_top(m.group(0)[:-1])):
            decl[p.name] = p
    out = []
    for p in ports:
        if p.direction:
            out.append(p)
        elif p.name in decl:
            out.append(decl[p.name])
        else:
            warnings.append(f"port '{p.name}' has no direction declaration; assuming 1-bit input")
            out.append(replace(p, direction="input"))
    return out


def parse_modules(text: str, source: Path | None = None) -> list[Module]:
    text = strip_comments(text)
    ts = re.search(r"`timescale\s+([^\n]+)", text)
    timescale = ts.group(1).strip() if ts else None

    modules = []
    pos = 0
    while (m := _MODULE_RE.search(text, pos)):
        end_m = _ENDMODULE_RE.search(text, m.end())
        end = end_m.start() if end_m else len(text)
        mod = Module(m.group(1), timescale=timescale, source=source)

        i = skip_ws(text, m.end())
        while text.startswith("import", i):              # SV package imports
            i = skip_ws(text, text.index(";", i) + 1)
        if text.startswith("#", i):                       # #( parameter list )
            i = skip_ws(text, i + 1)
            j = find_close(text, i)
            mod.params = parse_params(text[i + 1:j])
            i = skip_ws(text, j + 1)
        port_text = ""
        if text.startswith("(", i):                       # ( port list )
            j = find_close(text, i)
            port_text = text[i + 1:j]
            i = j + 1
        semi = text.find(";", i)
        mod.body = text[semi + 1:end] if semi != -1 else ""

        ports = parse_port_items(split_top(port_text))
        if any(p.direction is None for p in ports):
            ports = _resolve_non_ansi(ports, mod.body, mod.warnings)
            # body-level parameters only matter if a port width depends on them
            body_params: list[Param] = []
            for pm in re.finditer(r"\bparameter\b([^;]*);", _SUBPROG_RE.sub(" ", mod.body)):
                body_params += parse_params(pm.group(1))
            used = set(_IDENT_RE.findall(" ".join(p.packed + p.unpacked for p in ports)))
            mod.params += [bp for bp in body_params if bp.name in used]
        mod.ports = ports
        modules.append(mod)
        pos = end_m.end() if end_m else len(text)
    return modules


def find_tops(modules: list[Module]) -> list[Module]:
    """Modules that are not instantiated by any other given module."""
    names = {m.name for m in modules}
    used = set()
    for m in modules:
        for n in names - {m.name}:
            if re.search(rf"\b{re.escape(n)}\s*(?:#\s*\(|[A-Za-z_]\w*\s*\()", m.body):
                used.add(n)
    return [m for m in modules if m.name not in used]


# =============================================================== analysis

def eval_expr(expr: str, env: dict[str, int]) -> int | None:
    """Evaluate a simple constant expression (numbers, params, + - * / % ** $clog2)."""
    expr = expr.strip().replace("$clog2", "_clog2")
    if not expr or not re.fullmatch(r"[\w\s+\-*/%()<>]*", expr):
        return None
    expr = re.sub(r"(?<!/)/(?!/)", "//", expr)     # Verilog '/' is integer division
    try:
        val = eval(expr, {"__builtins__": {},     # noqa: S307 - restricted charset
                          "_clog2": lambda x: max(0, (int(x) - 1).bit_length())}, dict(env))
        return int(val)
    except Exception:
        return None


def param_env(params: list[Param]) -> dict[str, int]:
    env: dict[str, int] = {}
    for p in params:
        v = eval_expr(p.value, env)
        if v is not None:
            env[p.name] = v
    return env


def port_width(port: Port, env: dict[str, int]) -> int | None:
    total = 1
    for r in _RANGE_RE.findall(port.packed):
        inner = r[1:-1]
        if ":" not in inner:
            return None
        msb, lsb = inner.split(":", 1)
        a, b = eval_expr(msb, env), eval_expr(lsb, env)
        if a is None or b is None:
            return None
        total *= abs(a - b) + 1
    return total


def _tokens(name: str) -> list[str]:
    return [t for t in name.lower().split("_") if t]


def is_clock(p: Port) -> bool:
    if not p.is_input or not p.is_scalar:
        return False
    toks = _tokens(p.name)
    if any(t in ("en", "enable", "sel", "div", "gate", "cnt", "count", "out") for t in toks):
        return False
    return any(t in ("clk", "clock", "ck") or t.endswith("clk") for t in toks)


def reset_polarity(p: Port) -> bool | None:
    """None if `p` is not a reset; otherwise True for active-low, False for active-high."""
    if not p.is_input or not p.is_scalar:
        return None
    toks = _tokens(p.name)
    core = next((t for t in toks if "rst" in t or "reset" in t), None)
    if core is None:
        return None
    return (core.endswith("n") or core.startswith("n") or core.endswith("b")
            or any(t in ("n", "ni", "b", "l", "low", "neg") for t in toks if t != core))


# =============================================================== generation

@dataclass
class Options:
    period: int = 10
    reset_cycles: int = 5
    timeout: int = 100000
    random: int = 0
    exhaustive: bool = False
    max_exhaustive_bits: int = 20
    monitor: bool = False


def _random_expr(width: int | None) -> str:
    if width is None or width <= 32:
        return "$random"
    n = (width + 31) // 32
    return "{" + ", ".join(["$random"] * n) + "}"


def generate_tb(mod: Module, opts: Options) -> str:
    env = param_env(mod.params)
    clocks = [p for p in mod.ports if is_clock(p)]
    clock_names = {p.name for p in clocks}
    resets = [(p, reset_polarity(p)) for p in mod.ports
              if p.name not in clock_names and reset_polarity(p) is not None]
    special = clock_names | {p.name for p, _ in resets}
    data_in = [p for p in mod.ports if p.is_input and p.name not in special]
    drivable = [p for p in data_in if not p.unpacked]

    clocked = bool(clocks)
    clk = clocks[0].name if clocked else None
    period = "CLK_PERIOD" if clocked else "STEP"
    tb = f"tb_{mod.name}"
    ind = "    "

    def wait_cycles(n: str) -> str:
        return f"repeat ({n}) @(posedge {clk});" if clocked else f"#({period} * {n});"

    def step_before() -> list[str]:   # clocked: change inputs on the falling edge
        return [f"@(negedge {clk});"] if clocked else []

    def step_after() -> list[str]:    # combinational: let outputs settle
        return [] if clocked else [f"#({period});"]

    L: list[str] = []
    w = L.append

    # ---------------- header
    src = mod.source.name if mod.source else "?"
    w(f"`timescale {mod.timescale or '1ns / 1ps'}")
    w("// " + "-" * 77)
    w(f"// Testbench : {tb}")
    w(f"// DUT       : {mod.name}  (source: {src})")
    w(f"// Generated : tbgen v{__version__}, {datetime.date.today().isoformat()}")
    w("// Note      : skeleton only - add your test sequence and checks at the TODO marks")
    w("// " + "-" * 77)
    w(f"module {tb};")
    w("")

    # ---------------- settings & parameters
    settings = [(period, str(opts.period)), ("RESET_CYCLES", str(opts.reset_cycles)),
                ("TIMEOUT_CYCLES", str(opts.timeout))]
    if opts.random:
        settings.append(("RANDOM_ITERS", str(opts.random)))
    w(f"{ind}// ---------------- Testbench settings ----------------")
    nw = max(len(n) for n, _ in settings + [(p.name, "") for p in mod.params])
    for n, v in settings:
        w(f"{ind}localparam {n:<{nw}} = {v};")
    if mod.params:
        w("")
        w(f"{ind}// ---------------- DUT parameters ----------------")
        for p in mod.params:
            w(f"{ind}localparam {p.name:<{nw}} = {p.value};")
    w("")

    # ---------------- signal declarations
    w(f"{ind}// ---------------- DUT signals ----------------")
    rows = []
    for p in mod.ports:
        kw = "reg " if p.is_input else "wire"
        if p.signed:
            kw += " signed"
        note = "  // inout: drive through a reg + assign if needed" if p.direction == "inout" else ""
        rows.append((kw, p.packed, f"{p.name}{(' ' + p.unpacked) if p.unpacked else ''};{note}"))
    kw_w = max((len(r[0]) for r in rows), default=4)
    rg_w = max((len(r[1]) for r in rows), default=0)
    for kw, rg, rest in rows:
        col = f"{rg:<{rg_w}} " if rg_w else ""
        w(f"{ind}{kw:<{kw_w}} {col}{rest}")
    if opts.exhaustive:
        w(f"{ind}integer i;")
    w("")

    # ---------------- DUT instance
    w(f"{ind}// ---------------- DUT instance ----------------")
    if mod.params:
        w(f"{ind}{mod.name} #(")
        pw = max(len(p.name) for p in mod.params)
        for k, p in enumerate(mod.params):
            sep = "," if k < len(mod.params) - 1 else ""
            w(f"{ind*2}.{p.name:<{pw}} ({p.name}){sep}")
        w(f"{ind}) u_dut (")
    else:
        w(f"{ind}{mod.name} u_dut (")
    pw = max((len(p.name) for p in mod.ports), default=0)
    for k, p in enumerate(mod.ports):
        sep = "," if k < len(mod.ports) - 1 else ""
        w(f"{ind*2}.{p.name:<{pw}} ({p.name}){sep}")
    w(f"{ind});")
    w("")

    # ---------------- clocks
    if clocked:
        w(f"{ind}// ---------------- Clock ----------------")
        for c in clocks:
            w(f"{ind}initial {c.name} = 1'b0;")
            w(f"{ind}always #({period} / 2.0) {c.name} = ~{c.name};")
        if len(clocks) > 1:
            w(f"{ind}// NOTE: multiple clocks detected - all use {period}; adjust as needed")
        w("")

    # ---------------- waveform
    w(f"{ind}// ---------------- Waveform dump (GTKWave / Vivado) ----------------")
    w(f"{ind}initial begin")
    w(f'{ind*2}$dumpfile("{tb}.vcd");')
    w(f"{ind*2}$dumpvars(0, {tb});")
    w(f"{ind}end")
    w("")

    # ---------------- monitor
    if opts.monitor:
        mon_in = [p for p in mod.ports if p.is_input and p.name not in clock_names and not p.unpacked]
        mon_out = [p for p in mod.ports if not p.is_input and not p.unpacked]
        fmt = " ".join(f"{p.name}=%h" for p in mon_in)
        fmt += " | " + " ".join(f"{p.name}=%h" for p in mon_out) if mon_out else ""
        args = ", ".join(p.name for p in mon_in + mon_out)
        w(f"{ind}// ---------------- Monitor ----------------")
        w(f'{ind}initial $monitor("[%0t] {fmt.strip()}", $time{", " + args if args else ""});')
        w("")

    # ---------------- stimulus
    S = ind * 2
    w(f"{ind}// ---------------- Stimulus ----------------")
    w(f"{ind}initial begin")
    w(f"{S}// initialise inputs")
    for p in data_in:
        if p.unpacked:
            w(f"{S}// TODO: initialise array port '{p.name}'")
        else:
            w(f"{S}{p.name} = 0;")
    if resets:
        w("")
        w(f"{S}// apply reset")
        for p, low in resets:
            w(f"{S}{p.name} = 1'b{'0' if low else '1'};   // active-{'low' if low else 'high'}")
        w(f"{S}{wait_cycles('RESET_CYCLES')}")
        for line in step_before():
            w(f"{S}{line}")
        for p, low in resets:
            w(f"{S}{p.name} = 1'b{'1' if low else '0'};")
    w("")
    w(f"{S}// ============ TODO: directed test sequence ============")
    if clocked:
        w(f"{S}// e.g.  @(negedge {clk}); <input> = <value>;  @(posedge {clk}); ...")
    else:
        w(f"{S}// e.g.  <input> = <value>;  #({period});  if (<output> !== <expected>) $display(\"FAIL\");")
    w("")

    if opts.random and drivable:
        w(f"{S}// random stimulus")
        w(f"{S}repeat (RANDOM_ITERS) begin")
        for line in step_before():
            w(f"{S}{ind}{line}")
        for p in drivable:
            w(f"{S}{ind}{p.name} = {_random_expr(port_width(p, env))};")
        for line in step_after():
            w(f"{S}{ind}{line}")
        w(f"{S}end")
        w("")

    if opts.exhaustive:
        widths = [port_width(p, env) for p in drivable]
        if not drivable:
            mod.warnings.append("--exhaustive: no data inputs to sweep")
        elif None in widths:
            mod.warnings.append("--exhaustive: could not resolve all input widths; skipped")
            w(f"{S}// exhaustive sweep skipped: input widths could not be resolved")
        elif sum(widths) > opts.max_exhaustive_bits:
            mod.warnings.append(f"--exhaustive: {sum(widths)} input bits exceeds "
                                f"--max-exhaustive-bits={opts.max_exhaustive_bits}; skipped")
            w(f"{S}// exhaustive sweep skipped: {sum(widths)} input bits is too many")
        else:
            total = sum(widths)
            w(f"{S}// exhaustive sweep over all {total} input bits ({2 ** total} vectors)")
            w(f"{S}for (i = 0; i < {2 ** total}; i = i + 1) begin")
            for line in step_before():
                w(f"{S}{ind}{line}")
            w(f"{S}{ind}{{{', '.join(p.name for p in drivable)}}} = i;")
            for line in step_after():
                w(f"{S}{ind}{line}")
            w(f"{S}end")
            w("")

    w(f"{S}{wait_cycles('10')}")
    w(f'{S}$display("[%0t] {tb}: simulation finished", $time);')
    w(f"{S}$finish;")
    w(f"{ind}end")
    w("")

    # ---------------- watchdog
    w(f"{ind}// ---------------- Timeout watchdog ----------------")
    w(f"{ind}initial begin")
    w(f"{S}#({period} * TIMEOUT_CYCLES);")
    w(f'{S}$display("[%0t] ERROR: timeout after %0d cycles", $time, TIMEOUT_CYCLES);')
    w(f"{S}$finish;")
    w(f"{ind}end")
    w("")
    w("endmodule")
    return "\n".join(L) + "\n"


def describe(mod: Module) -> str:
    clocks = [p.name for p in mod.ports if is_clock(p)]
    resets = [f"{p.name}({'low' if reset_polarity(p) else 'high'})"
              for p in mod.ports if p.name not in clocks and reset_polarity(p) is not None]
    n_in = sum(p.is_input for p in mod.ports)
    n_out = len(mod.ports) - n_in
    return (f"clock={','.join(clocks) or '-'}  reset={','.join(resets) or '-'}  "
            f"in={n_in} out={n_out} params={len(mod.params)}")


# =============================================================== simulation check

def run_check(tb_path: Path, sources: list[Path], top: str) -> bool | None:
    """Compile + run with Icarus Verilog. Returns None if iverilog is unavailable."""
    iverilog, vvp = shutil.which("iverilog"), shutil.which("vvp")
    if not iverilog or not vvp:
        print("  [check] iverilog/vvp not found in PATH - skipped", file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sim.vvp"
        cmd = [iverilog, "-g2012", "-s", top, "-o", str(out), str(tb_path.resolve()),
               *(str(s.resolve()) for s in sources)]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print("  [check] COMPILE FAILED\n" + (r.stderr or r.stdout), file=sys.stderr)
            return False
        try:
            r = subprocess.run([vvp, "-n", str(out)], cwd=tmp, capture_output=True,
                               text=True, errors="replace", timeout=300)
        except subprocess.TimeoutExpired:
            print("  [check] simulation did not finish within 300 s", file=sys.stderr)
            return False
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        ok = r.returncode == 0 and not any("ERROR" in ln for ln in lines)
        print(f"  [check] {'PASS' if ok else 'FAIL'}: " + (lines[-1] if lines else "(no output)"))
        if not ok:
            print("\n".join("    " + ln for ln in lines[-15:]), file=sys.stderr)
        return ok


# =============================================================== CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tbgen",
        description="Generate a Verilog testbench skeleton from a module definition.")
    ap.add_argument("sources", nargs="+", type=Path, help="Verilog / SystemVerilog source files")
    ap.add_argument("-t", "--top", help="module to generate a testbench for "
                    "(default: every module not instantiated by another)")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("."), help="output directory")
    ap.add_argument("--period", type=int, default=10, help="clock period / step time (default 10)")
    ap.add_argument("--reset-cycles", type=int, default=5, help="reset length in cycles (default 5)")
    ap.add_argument("--timeout", type=int, default=100000, help="watchdog timeout in cycles")
    ap.add_argument("--random", type=int, default=0, metavar="N", help="add N random input vectors")
    ap.add_argument("--exhaustive", action="store_true", help="sweep every input combination")
    ap.add_argument("--max-exhaustive-bits", type=int, default=20)
    ap.add_argument("--monitor", action="store_true", help="add a $monitor of all ports")
    ap.add_argument("--check", action="store_true", help="compile & run the result with iverilog")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing files")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing testbench files")
    ap.add_argument("--version", action="version", version=f"tbgen {__version__}")
    args = ap.parse_args(argv)

    modules: list[Module] = []
    for src in args.sources:
        if not src.is_file():
            ap.error(f"file not found: {src}")
        modules += parse_modules(read_source(src), src)
    if not modules:
        print("error: no module found", file=sys.stderr)
        return 1

    if args.top:
        targets = [m for m in modules if m.name == args.top]
        if not targets:
            print(f"error: module '{args.top}' not found "
                  f"(found: {', '.join(m.name for m in modules)})", file=sys.stderr)
            return 1
    else:
        targets = find_tops(modules)

    opts = Options(args.period, args.reset_cycles, args.timeout, args.random,
                   args.exhaustive, args.max_exhaustive_bits, args.monitor)
    status = 0
    for mod in targets:
        text = generate_tb(mod, opts)
        for msg in mod.warnings:
            print(f"warning: {mod.name}: {msg}", file=sys.stderr)
        if args.stdout:
            sys.stdout.write(text)
            continue
        args.outdir.mkdir(parents=True, exist_ok=True)
        out = args.outdir / f"tb_{mod.name}.v"
        if out.exists() and not args.force:
            print(f"skip: {out} already exists (use -f to overwrite)", file=sys.stderr)
            status = 1
            continue
        out.write_text(text, encoding="utf-8")
        print(f"{out}  <-  {mod.name}   [{describe(mod)}]")
        if args.check and run_check(out, args.sources, f"tb_{mod.name}") is False:
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
