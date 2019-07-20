import inspect
import ast
from copy import copy
import operator
from functools import reduce

from migen import *


class Isn:
    def __init__(self, immediate=None, inputs=None, outputs=None):
        if inputs is None:
            inputs = []
        if outputs is None:
            outputs = []
        self.immediate = immediate
        self.inputs = inputs
        self.outputs = outputs

    def __repr__(self):
        r = "<"
        r += self.__class__.__name__
        if self.immediate is not None:
            r += " (" + str(self.immediate) + ")"
        for inp in self.inputs:
            r += " r" + str(inp)
        if self.outputs:
            r += " ->"
            for outp in self.outputs:
                r += " r" + str(outp)
        r += ">"
        return r


class NopIsn(Isn):
    opcode = 0

class AddIsn(Isn):
    opcode = 1

class SubIsn(Isn):
    opcode = 2

class MulIsn(Isn):
    opcode = 3

class CopyIsn(Isn):
    opcode = 4

class InputIsn(Isn):
    opcode = 5

class OutputIsn(Isn):
    opcode = 6


class ASTCompiler:
    def __init__(self):
        self.program = []
        self.data = []
        self.next_ssa_reg = -1
        self.constants = dict()
        self.names = dict()
        self.globals = dict()

    def get_ssa_reg(self):
        r = self.next_ssa_reg
        self.next_ssa_reg -= 1
        return r

    def add_global(self, name):
        r = len(self.data)
        self.data.append(0)
        self.names[name] = r
        self.globals[name] = r
        return r

    def input(self, name):
        target = self.get_ssa_reg()
        self.program.append(InputIsn(outputs=[target]))
        self.names[name] = target

    def emit(self, node):
        if isinstance(node, ast.BinOp):
            left = self.emit(node.left)
            right = self.emit(node.right)
            if isinstance(node.op, ast.Add):
                cls = AddIsn
            elif isinstance(node.op, ast.Sub):
                cls = SubIsn
            elif isinstance(node.op, ast.Mult):
                cls = MulIsn
            else:
                raise NotImplementedError
            output = self.get_ssa_reg()
            self.program.append(cls(inputs=[left, right], outputs=[output]))
            return output
        elif isinstance(node, ast.Num):
            if node.n in self.constants:
                return self.constants[node.n]
            else:
                r = len(self.data)
                self.data.append(node.n)
                self.constants[node.n] = r
                return r
        elif isinstance(node, ast.Name):
            return self.names[node.id]
        elif isinstance(node, ast.Assign):
            output = self.emit(node.value)
            for target in node.targets:
                assert isinstance(target, ast.Name)
                self.names[target.id] = output
        elif isinstance(node, ast.Return):
            value = self.emit(node.value)
            self.program.append(OutputIsn(inputs=[value]))
        elif isinstance(node, ast.Global):
            pass
        else:
            raise NotImplementedError


class Processor:
    def __init__(self, data_width=32, multiplier_stages=2):
        self.data_width = data_width
        self.multiplier_stages = multiplier_stages
        self.program_rom_size = None
        self.data_ram_size = None
        self.opcode_bits = 3
        self.reg_bits = None

    def get_instruction_latency(self, isn):
        return {
            AddIsn: 2,
            SubIsn: 2,
            MulIsn: 1 + self.multiplier_stages,
            CopyIsn: 1,
            InputIsn: 1
        }[isn.__class__]

    def encode_instruction(self, isn, exit):
        opcode = isn.opcode
        if isn.immediate is not None:
            r0 = isn.immediate
            if len(isn.inputs) >= 1:
                r1 = isn.inputs[0]
            else:
                r1 = 0
        else:
            if len(isn.inputs) >= 1:
                r0 = isn.inputs[0]
            else:
                r0 = 0
            if len(isn.inputs) >= 2:
                r1 = isn.inputs[1]
            else:
                r1 = 0
        r = 0
        for value, bits in ((exit, self.reg_bits), (r1, self.reg_bits), (r0, self.reg_bits), (opcode, self.opcode_bits)):
            r <<= bits
            r |= value
        return r

    def instruction_bits(self):
        return 3*self.reg_bits + self.opcode_bits

    def implement(self, program, data):
        return ProcessorImpl(self, program, data)


class Scheduler:
    def __init__(self, processor, reserved_data, program):
        self.processor = processor
        self.reserved_data = reserved_data
        self.used_registers = set(range(self.reserved_data))
        self.exits = dict()
        self.program = program
        self.remaining = copy(program)
        self.output = []

    def allocate_register(self):
        r = min(set(range(max(self.used_registers) + 2)) - self.used_registers)
        self.used_registers.add(r)
        return r

    def free_register(self, r):
        assert r >= self.reserved_data
        self.used_registers.discard(r)

    def find_inputs(self, cycle, isn):
        mapped_inputs = []
        for inp in isn.inputs:
            if inp >= 0:
                mapped_inputs.append(inp)
            else:
                found = False
                for i in range(cycle):
                    if i in self.exits:
                        r, rm = self.exits[i]
                        if r == inp:
                            mapped_inputs.append(rm)
                            found = True
                            break
                if not found:
                    return None
        return mapped_inputs

    def schedule_one(self, isn):
        cycle = len(self.output)
        mapped_inputs = self.find_inputs(cycle, isn)
        if mapped_inputs is None:
            return False

        if isn.outputs:
            latency = self.processor.get_instruction_latency(isn)
            exit = cycle + latency
            if exit in self.exits:
                return False

        # Instruction can be scheduled

        self.remaining.remove(isn)            

        for inp, minp in zip(isn.inputs, mapped_inputs):
            can_free = inp < 0 and all(inp != rinp for risn in self.remaining for rinp in risn.inputs)
            if can_free:
                self.free_register(minp)

        if isn.outputs:
            assert len(isn.outputs) == 1
            output = self.allocate_register()
            self.exits[exit] = (isn.outputs[0], output)
        self.output.append(isn.__class__(immediate=isn.immediate, inputs=mapped_inputs))

        return True

    def schedule(self):
        while self.remaining:
            success = False
            for isn in self.remaining:
                if self.schedule_one(isn):
                    success = True
                    break
            if not success:
                self.output.append(NopIsn())
        self.output += [NopIsn()]*(max(self.exits.keys()) - len(self.output) + 1)
        return self.output


class CompiledProgram:
    def __init__(self, processor, program, exits, data, glbs):
        self.processor = processor
        self.program = program
        self.exits = exits
        self.data = data
        self.globals = glbs

    def pretty_print(self):
        for cycle, isn in enumerate(self.program):
            l = "{:4d} {:15}".format(cycle, str(isn))
            if cycle in self.exits:
                l += " -> r{}".format(self.exits[cycle])
            print(l)

    def dimension_memories(self):
        self.processor.program_rom_size = len(self.program)
        self.processor.data_ram_size = len(self.data)
        self.processor.reg_bits = (self.processor.data_ram_size - 1).bit_length()

    def encode(self):
        r = []
        for i, isn in enumerate(self.program):
            exit = self.exits.get(i, 0)
            r.append(self.processor.encode_instruction(isn, exit))
        return r


def compile(processor, function):
    node = ast.parse(inspect.getsource(function))
    assert isinstance(node, ast.Module)
    assert len(node.body) == 1
    node = node.body[0]
    assert isinstance(node, ast.FunctionDef)
    assert len(node.args.args) == 1
    arg = node.args.args[0].arg
    body = node.body
    
    astcompiler = ASTCompiler()
    for node in body:
        if isinstance(node, ast.Global):
            for name in node.names:
                astcompiler.add_global(name)
    arg_r = astcompiler.input(arg)
    for node in body:
        astcompiler.emit(node)
        if isinstance(node, ast.Return):
            break

    scheduler = Scheduler(processor, len(astcompiler.data), astcompiler.program)
    scheduler.schedule()

    max_reg = max(max(max(isn.inputs + [0]) for isn in scheduler.output), max(v[1] for k, v in scheduler.exits.items()))

    return CompiledProgram(
        processor=processor,
        program=scheduler.output,
        exits={k: v[1] for k, v in scheduler.exits.items()},
        data=astcompiler.data + [0]*(max_reg - len(astcompiler.data) + 1),
        glbs=astcompiler.globals)


class BaseUnit(Module):
    def __init__(self, data_width):
        self.stb_i = Signal()
        self.i0 = Signal(data_width)
        self.i1 = Signal(data_width)
        self.stb_o = Signal()
        self.o = Signal(data_width)


class NopUnit(BaseUnit):
    pass


class OpUnit(BaseUnit):
    def __init__(self, op, data_width, stages):
        BaseUnit.__init__(self, data_width)

        o = op(self.i0, self.i1)
        stb_o = self.stb_i
        for i in range(stages):
            n_o = Signal(data_width)
            n_stb_o = Signal()
            self.sync += [
                n_o.eq(o),
                n_stb_o.eq(stb_o)
            ]
            o = n_o
            stb_o = n_stb_o
        self.comb += [
            self.o.eq(o),
            self.stb_o.eq(stb_o)
        ]


class CopyUnit(BaseUnit):
    def __init__(self, data_width):
        BaseUnit.__init__(self, data_width)

        self.comb += [
            self.stb_o.eq(self.stb_i),
            self.o.eq(self.i0)
        ]


class InputUnit(BaseUnit):
    def __init__(self, data_width, input_stb, input):
        BaseUnit.__init__(self, data_width)

        # TODO
        self.comb += [
            self.stb_o.eq(self.stb_i),
            self.o.eq(42)
        ]


class OutputUnit(BaseUnit):
    def __init__(self, data_width, output_stb, output):
        BaseUnit.__init__(self, data_width)

        self.sync += [
            output_stb.eq(self.stb_i),
            output.eq(self.i0)
        ]


class ProcessorImpl(Module):
    def __init__(self, pd, program, data):
        self.input_stb = Signal()
        self.input = Signal(pd.data_width)

        self.output_stb = Signal()
        self.output = Signal(pd.data_width)

        # # #

        program_mem = Memory(pd.instruction_bits(), pd.program_rom_size, init=program)
        data_mem0 = Memory(pd.data_width, pd.data_ram_size, init=data)
        data_mem1 = Memory(pd.data_width, pd.data_ram_size, init=data)
        self.specials += program_mem, data_mem0, data_mem1

        pc = Signal(pd.instruction_bits())
        pc_next = Signal.like(pc)
        pc_en = Signal()
        self.sync += pc.eq(pc_next)
        self.comb += [
            If(pc_en,
                pc_next.eq(pc + 1)
            ).Else(
                pc_next.eq(0)
            )
        ]
        program_mem_port = program_mem.get_port()
        self.specials += program_mem_port
        self.comb += program_mem_port.adr.eq(pc_next)

        # TODO
        self.comb += pc_en.eq(1)

        s = 0
        opcode = Signal(pd.opcode_bits)
        self.comb += opcode.eq(program_mem_port.dat_r[s:s+pd.opcode_bits])
        s += pd.opcode_bits
        r0 = Signal(pd.reg_bits)
        self.comb += r0.eq(program_mem_port.dat_r[s:s+pd.reg_bits])
        s += pd.reg_bits
        r1 = Signal(pd.reg_bits)
        self.comb += r1.eq(program_mem_port.dat_r[s:s+pd.reg_bits])
        s += pd.reg_bits
        exit = Signal(pd.reg_bits)
        self.comb += exit.eq(program_mem_port.dat_r[s:s+pd.reg_bits])

        data_read_port0 = data_mem0.get_port()
        data_read_port1 = data_mem1.get_port()
        self.specials += data_read_port0, data_read_port1
        self.comb += [
            data_read_port0.adr.eq(r0),
            data_read_port1.adr.eq(r1)
        ]

        data_write_port = data_mem0.get_port(write_capable=True)
        data_write_port_dup = data_mem1.get_port(write_capable=True)
        self.specials += data_write_port, data_write_port_dup
        self.comb += [
            data_write_port_dup.we.eq(data_write_port.we),
            data_write_port_dup.adr.eq(data_write_port.adr),
            data_write_port_dup.dat_w.eq(data_write_port.dat_w),
            data_write_port.adr.eq(exit)
        ]

        nop = NopUnit(pd.data_width)
        adder = OpUnit(operator.add, pd.data_width, 1)
        subtractor = OpUnit(operator.sub, pd.data_width, 1)
        multiplier = OpUnit(operator.mul, pd.data_width, pd.multiplier_stages)
        copier = CopyUnit(pd.data_width)
        inu = InputUnit(pd.data_width, self.input_stb, self.input)
        outu = OutputUnit(pd.data_width, self.output_stb, self.output)
        units = [nop, adder, subtractor, multiplier, copier, inu, outu]
        self.submodules += units

        for n, unit in enumerate(units):
            self.sync += unit.stb_i.eq(opcode == n)
            self.comb += [
                unit.i0.eq(data_read_port0.dat_r),
                unit.i1.eq(data_read_port1.dat_r),
                If(unit.stb_o,
                    data_write_port.we.eq(1),
                    data_write_port.dat_w.eq(unit.o)
                )
            ]


a = 0
b = 0
c = 0

def foo(x):
    global a, b, c
    c = b
    b = a
    a = x
    return 4748*a + 259*b - 155*c 


def simple_test(x):
    a = 5 + 3
    return a*4


if __name__ == "__main__":
    proc = Processor()
    cp = compile(proc, simple_test)
    cp.pretty_print()
    cp.dimension_memories()
    print(cp.encode())
    proc_impl = proc.implement(cp.encode(), cp.data)

    def wait_result():
        while not (yield proc_impl.output_stb):
            yield
        result = yield proc_impl.output
        print(result)
    run_simulation(proc_impl, [wait_result()], vcd_name="test.vcd")
