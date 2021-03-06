from parse import none
from mtypes import (
    Any, Instance, Void, TypeVar, TupleType, Callable, UnboundType
)
from nodes import IfStmt, ForStmt, WhileStmt, WithStmt, TryStmt
from nodes import function_type
from output import OutputVisitor
from typerepr import ListTypeRepr


# Names present in mypy but not in Python. Imports of these names are removed
# during translation. These are generally type names, so references to these
# generally appear in declarations, which are erased during translation.
#
# TODO for many of these a corresponding alternative exists in Python; if
#      these names are used in a non-erased context (e.g. isinstance test or
#      overloaded method signature), we should change the reference to the
#      Python alternative
removed_import_names = {'re': ['Pattern', 'Match']}


# Some names defined in mypy builtins are defined in a different module in
# Python. We translate references to these names.
renamed_types = {'builtins.Sized': 'collections.Sized',
                 'builtins.Iterable': 'collections.Iterable',
                 'builtins.Iterator': 'collections.Iterator',
                 'builtins.Sequence': 'collections.Sequence',
                 'builtins.Mapping': 'collections.Mapping',
                 'builtins.Set': 'collections.Set',
                 're.Pattern': 're._pattern_type'}


# These types are erased during translation. If they are used in overloads,
# a hasattr check is used instead of an isinstance check (the value if the name
# of the attribute).
erased_duck_types = {
    'builtins.int_t': '__int__',
    'builtins.float_t': '__float__',
    'builtins.reversed_t': '__reversed__',
    'builtins.abs_t': '__abs__',
    'builtins.round_t': '__round__'
}


# Some names need more complex logic to translate them. This dictionary maps
# qualified names to (initcode, newname) tuples. The initcode string is
# added to the module prolog.
#
# We use __ prefixes to avoid name clashes. Names starting with __ but not
# ending with _ are reserved for the implementation.
special_renamings = {
    're.Match': (['import re as __re\n',
                  'import builtins as __builtins\n',
                  '__re_Match = __builtins.type(__re.match("", ""))\n'],
                 '__re_Match')}


class PythonGenerator(OutputVisitor):
    """Python backend.

    Translate semantically analyzed parse trees to Python.  Reuse most
    of the generation logic from the mypy pretty printer implemented
    in OutputVisitor.
    """

    str[] prolog

    def __init__(self, pyversion=3):
        super().__init__()
        self.pyversion = pyversion
        self.prolog = []

    def output(self):
        """Return a string representation of the output."""
        # TODO add the prolog after the first comment and docstring
        return ''.join(self.prolog) + super().output()
    
    def visit_import_from(self, o):
        if o.id in removed_import_names:
            r = o.repr
            
            # Filter out any names not defined in Python from a
            # from ... import statement.
            
            toks = []
            comma = none
            for i in range(len(o.names)):
                if o.names[i][0] not in removed_import_names[o.id]:
                    toks.append(comma)
                    toks.extend(r.names[i][0])
                    comma = r.names[i][1]
            
            # If everything was filtered out, omit the statement.
            if toks != []:
                # Output the filtered statement.
                self.token(r.from_tok)
                self.tokens(r.components)
                self.token(r.import_tok)
                self.token(r.lparen)
                self.tokens(toks)
                self.token(r.rparen)
                self.token(r.br)
        else:
            super().visit_import_from(o)
    
    def visit_func_def(self, o, name_override=None):
        r = o.repr
        
        if r.def_tok and r.def_tok.string:
            self.token(r.def_tok)
        else:
            self.string(self.get_pre_whitespace(o.type.ret_type) + 'def')
        
        if name_override is None:
            self.token(r.name)
        else:
            self.string(' ' + name_override)
        self.function_header(o, r.args, o.arg_kinds, None, True, True)
        if not o.body.body:
            self.string(': pass' + '\n')
        else:
            self.node(o.body)
    
    def get_pre_whitespace(self, t):
        """Return whitespace before the first token of a type."""
        if isinstance(t, Any):
            return t.repr.any_tok.pre
        elif isinstance(t, Instance):
            if isinstance(t.repr, ListTypeRepr):
                return self.get_pre_whitespace(t.args[0])
            else:
                return t.repr.components[0].pre
        elif isinstance(t, Void):
            return t.repr.void.pre
        elif isinstance(t, TypeVar):
            return t.repr.name.pre
        elif isinstance(t, TupleType):
            return t.repr.components[0].pre
        elif isinstance(t, Callable):
            return t.repr.func.pre
        else:
            raise RuntimeError('Unsupported type {}'.format(t))
    
    def visit_var_def(self, o):
        r = o.repr
        if r:
            self.string(self.get_pre_whitespace(o.items[0].type))
            self.omit_next_space = True
            for v in o.items:
                self.node(v)
            if o.init:
                self.token(r.assign)
                self.node(o.init)
            else:
                self.string(' = {}'.format(', '.join(['None'] * len(o.items))))
            self.token(r.br)

    def visit_name_expr(self, o):
        # Rename some type references (e.g. Iterable -> collections.Iterable).
        renamed = self.get_renaming(o.full_name)
        if renamed:
            self.string(o.repr.id.pre)
            self.string(renamed)
        else:
            super().visit_name_expr(o)
    
    def visit_cast_expr(self, o):
        # Erase cast.
        self.string(o.repr.lparen.pre)
        self.node(o.expr)

    def visit_type_application(self, o):
        self.node(o.expr)
    
    def visit_for_stmt(self, o):
        r = o.repr
        self.token(r.for_tok)
        for i in range(len(o.index)):
            self.node(o.index[i])
            self.token(r.commas[i])
        self.token(r.in_tok)
        self.node(o.expr)
        
        self.node(o.body)
        if o.else_body:
            self.token(r.else_tok)
            self.node(o.else_body)
    
    def visit_type_def(self, o):
        r = o.repr
        self.string(r.class_tok.pre)
        self.string('class')
        self.token(r.name)

        # Erase references to base types such as int_t which do not exist in
        # Python.
        commas = []
        bases = []
        for i, base in enumerate(o.base_types):
            if (base.type.full_name() not in erased_duck_types
                    and base.repr):
                bases.append(base)
                if i < len(r.commas):
                    commas.append(r.commas[i])

        if bases:
            # Generate base types within parentheses.
            self.token(r.lparen)
            for i, base in enumerate(bases):
                self.string(self.erased_type(base))
                if i < len(bases) - 1:
                    self.token(commas[i])
            self.token(r.rparen)
        if not r.lparen.string and self.pyversion == 2:
            self.string('(object)')
        self.node(o.defs)
    
    def erased_type(self, t):
        """Return Python representation of a type (as string).

        Examples:
          - C -> 'C'
          - foo.Bar -> 'foo.Bar'
          - dict<x, y> -> 'dict'
          - Iterable<x> -> '__collections.Iterable' (also add import)
        """
        if isinstance(t, Instance) or isinstance(t, UnboundType):
            if isinstance(t.repr, ListTypeRepr):
                self.generate_import('builtins')
                return '__builtins.list'
            else:
                # Some types need to be translated (e.g. Iterable).
                renamed = self.get_renaming(t.type.full_name())
                if renamed:
                    pre = t.repr.components[0].pre
                    return pre + renamed
                else:
                    a = []
                    if t.repr:
                        for tok in t.repr.components:
                            a.append(tok.rep())
                    return ''.join(a)
        elif isinstance(t, TupleType):
            return 'tuple' # FIX: aliasing?
        elif isinstance(t, TypeVar):
            return 'object' # Type variables are erased to "object"
        else:
            raise RuntimeError('Cannot translate type {}'.format(t))
    
    def visit_func_expr(self, o):
        r = o.repr
        self.token(r.lambda_tok)
        self.function_header(o, r.args, o.arg_kinds, None, True, False)
        self.token(r.colon)
        self.node(o.body.body[0].expr)
    
    def visit_overloaded_func_def(self, o):
        """Translate overloaded function definition.

        Overloaded functions are transformed into a single Python function that
        performs argument type checks and length checks to dispatch to the
        right implementation.
        """
        indent = self.indent * ' '
        first = o.items[0]
        r = first.repr
        if r.def_tok and r.def_tok.string:
            self.token(r.def_tok)
        else:
            # TODO omit (some) comments; now comments may be duplicated
            self.string(self.get_pre_whitespace(first.type.ret_type) +
                        'def')
        self.string(' {}('.format(first.name()))
        self.extra_indent += 4
        fixed_args, is_more = self.get_overload_args(o)
        self.string(', '.join(fixed_args))
        rest_args = None
        if is_more:
            rest_args = self.make_unique('args', fixed_args)
            if len(fixed_args) > 0:
                self.string(', ')
            self.string('*{}'.format(rest_args))
        self.string('):\n' + indent)
        n = 1
        for f in o.items:
            self.visit_func_def(f, '{}{}'.format(f.name(), n))
            n += 1
        self.string('\n')
        
        n = 1
        for fi in o.items:
            c = self.make_overload_check(fi, fixed_args, rest_args)
            self.string(indent)
            if n == 1:
                self.string('if ')
            else:
                self.string('elif ')
            self.string(c)
            self.string(':' + '\n' + indent)
            self.string('    return {}'.format(self.make_overload_call(
                fi, n, fixed_args, rest_args)) + '\n')
            n += 1
        self.string(indent + 'else:' + '\n')
        self.string(indent + '    raise TypeError("Invalid argument types")')
        self.extra_indent -= 4
        last_stmt = o.items[-1].body.body[-1]
        self.token(self.find_break_after_statement(last_stmt))
    
    def find_break_after_statement(self, s):
        if isinstance(s, IfStmt):
            blocks = s.body + [s.else_body]
        elif isinstance(s, ForStmt) or isinstance(s, WhileStmt):
            blocks = [s.body, s.else_body]
        elif isinstance(s, WithStmt):
            blocks = [s.body]
        elif isinstance(s, TryStmt):
            blocks = s.handlers + [s.else_body, s.finally_body]
        else:
            return s.repr.br
        for b in reversed(blocks):
            if b:
                return self.find_break_after_statement(b.body[-1])
        raise RuntimeError('Could not find break after statement')
    
    def make_unique(self, n, others):
        if n in others:
            return self.make_unique('_' + n, others)
        else:
            return n
    
    def get_overload_args(self, o):
        fixed = []
        min_fixed = 100000
        max_fixed = 0
        for f in o.items:
            if len(f.args) > len(fixed):
                for v in f.args[len(fixed):]:
                    fixed.append(v.name())
            min_fixed = min(min_fixed, f.min_args)
            max_fixed = max(max_fixed, len(f.args))
        return fixed[:min_fixed], max_fixed > min_fixed
    
    def make_overload_check(self, f, fixed_args, rest_args):
        a = []
        i = 0
        if rest_args:
            a.append(self.make_argument_count_check(f, len(fixed_args),
                                                    rest_args))
        for t in function_type(f).arg_types:
            if not isinstance(t, Any) and (t.repr or
                                           isinstance(t, Callable)):
                a.append(self.make_argument_check(
                    self.argument_ref(i, fixed_args, rest_args), t))
            i += 1
        if len(a) > 0:
            return ' and '.join(a)
        else:
            return 'True'
    
    def make_argument_count_check(self, f, num_fixed, rest_args):
        return 'len({}) == {}'.format(rest_args, f.min_args - num_fixed)
    
    def make_argument_check(self, name, typ):
        if isinstance(typ, Callable):
            return 'callable({})'.format(name)
        if (isinstance(typ, Instance) and
                typ.type.full_name() in erased_duck_types):
            return "hasattr({}, '{}')".format(
                                name, erased_duck_types[typ.type.full_name()])
        else:
            cond = 'isinstance({}, {})'.format(name, self.erased_type(typ))
            return cond.replace('  ', ' ')
    
    def make_overload_call(self, f, n, fixed_args, rest_args):
        a = []
        for i in range(len(f.args)):
            a.append(self.argument_ref(i, fixed_args, rest_args))
        return '{}{}({})'.format(f.name(), n, ', '.join(a))
    
    def argument_ref(self, i, fixed_args, rest_args):
        if i < len(fixed_args):
            return fixed_args[i]
        else:
            return '{}[{}]'.format(rest_args, i - len(fixed_args))
    
    def visit_list_expr(self, o):
        r = o.repr
        self.token(r.lbracket)
        self.comma_list(o.items, r.commas)
        self.token(r.rbracket)
    
    def visit_dict_expr(self, o):
        r = o.repr
        self.token(r.lbrace)
        i = 0
        for k, v in o.items:
            self.node(k)
            self.token(r.colons[i])
            self.node(v)
            if i < len(r.commas):
                self.token(r.commas[i])
            i += 1
        self.token(r.rbrace)

    def visit_super_expr(self, o):
        if self.pyversion > 2:
            super().visit_super_expr(o)
        else:
            r = o.repr
            self.tokens([r.super_tok, r.lparen])
            # TODO do not hard code 'self'
            self.string('%s, self' % o.info.name())
            self.tokens([r.rparen, r.dot, r.name])

    def generate_import(self, modid):
        """Generate an import in the file prolog.

        When importing, the module is given a '__' prefix. For example, an
        import of module 'foo' is generated as 'import foo as __foo'.
        """
        # TODO make sure that there is no name clash
        last_component = modid.split('.')[-1]
        self.add_to_prolog('import {} as __{}\n'.format(modid, last_component))

    def generate_import_from_name(self, fullname):
        """Use module portion of a qualified name to generate an import."""
        modid = fullname[:fullname.rfind('.')]
        self.generate_import(modid)

    def add_to_prolog(self, string):
        """Add a line to the file prolog unless it already exists."""
        if not string in self.prolog:
            self.prolog.append(string)

    def get_renaming(self, fullname):
        """Determine the renaming target name of a qualified mypy name.

        Return None if the name needs no renaming; otherwise return the new
        name as a string.

        Also add any required imports, etc. to the file prolog.
        """
        renamed = renamed_types.get(fullname)
        if renamed:
            # Ordinary renaming. Import a module that defines the name and
            # rename the reference.
            self.generate_import_from_name(renamed)
            return '__' + renamed
        else:
            special = special_renamings.get(fullname)
            if special:
                # Special renaming. Add custom code to prolog and rename the
                # reference.
                prolog, renamed = special
                for line in prolog:
                    self.add_to_prolog(line)
                return renamed
            else:
                return None
