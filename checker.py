"""Mypy type checker."""

from errors import Errors
from nodes import (
    SymbolTable, Node, MypyFile, VarDef, LDEF, Var,
    OverloadedFuncDef, FuncDef, FuncItem, FuncBase, TypeInfo,
    TypeDef, GDEF, Block, AssignmentStmt, NameExpr, MemberExpr, IndexExpr,
    TupleExpr, ListExpr, ParenExpr, ExpressionStmt, ReturnStmt, IfStmt,
    WhileStmt, OperatorAssignmentStmt, YieldStmt, WithStmt, AssertStmt,
    RaiseStmt, TryStmt, ForStmt, DelStmt, CallExpr, IntExpr, StrExpr,
    BytesExpr, FloatExpr, OpExpr, UnaryExpr, CastExpr, SuperExpr,
    TypeApplication, DictExpr, SliceExpr, FuncExpr, TempNode, SymbolTableNode,
    Context, AccessorNode, ListComprehension, ConditionalExpr, GeneratorExpr,
    Decorator, SetExpr, PassStmt
)
from nodes import function_type, method_type
import nodes
from mtypes import (
    Type, Any, Callable, Void, FunctionLike, Overloaded, TupleType, Instance,
    NoneTyp, UnboundType, TypeTranslator, BasicTypes
)
from sametypes import is_same_type
from messages import MessageBuilder
import checkexpr
import messages
from subtypes import is_subtype, is_equivalent, map_instance_to_supertype
from semanal import self_type
from expandtype import expand_type_by_instance
from visitor import NodeVisitor


class TypeChecker(NodeVisitor<Type>):
    """Mypy type checker.

    Type check mypy source files that have been semantically analysed.
    """
    
    Errors errors          # Error reporting
    SymbolTable symtable   # Symbol table for the whole program
    MessageBuilder msg     # Utility for generating messages
    dict<Node, Type> type_map  # Types of type checked nodes
    checkexpr.ExpressionChecker expr_checker
    
    str[] stack # Stack of local variable definitions
                    # None separates nested functions
    Type[] return_types   # Stack of function return types
    Type[] type_context   # Type context for type inference
    bool[] dynamic_funcs # Flags; true for dynamically typed functions
    
    SymbolTable globals
    SymbolTable class_tvars
    SymbolTable locals
    dict<str, MypyFile> modules
    
    void __init__(self, Errors errors, dict<str, MypyFile> modules):
        """Construct a type checker. Use errors to report type check
        errors. Assume symtable has been populated by the semantic
        analyzer.
        """
        self.expr_checker
        self.errors = errors
        self.modules = modules
        self.msg = MessageBuilder(errors)
        self.type_map = {}
        self.expr_checker = checkexpr.ExpressionChecker(self, self.msg)
        self.stack = [None]
        self.return_types = []
        self.type_context = []
        self.dynamic_funcs = []
    
    void visit_file(self, MypyFile file_node, str path):  
        """Type check a mypy file with the given path."""
        self.errors.set_file(path)
        self.globals = file_node.names
        self.locals = None
        self.class_tvars = None
        
        for d in file_node.defs:
            self.accept(d)
    
    Type accept(self, Node node, Type type_context=None):
        """Type check a node in the given type context."""
        self.type_context.append(type_context)
        typ = node.accept(self)
        self.type_context.pop()
        self.store_type(node, typ)
        if self.is_dynamic_function():
            return Any()
        else:
            return typ
    
    #
    # Definitions
    #
    
    Type visit_var_def(self, VarDef defn):
        """Type check a variable definition (of any kind: local,
        member or global)."""
        # Type check initializer.
        if defn.init:
            # There is an initializer.
            if defn.items[0].type:
                # Explicit types.
                if len(defn.items) == 1:
                    self.check_single_assignment(defn.items[0].type, None,
                                                 defn.init, defn.init)
                else:
                    # Multiple assignment.
                    Type[] lvt = []
                    for v in defn.items:
                        lvt.append(v.type)
                    self.check_multi_assignment(
                        lvt, <IndexExpr> [None] * len(lvt),
                        defn.init, defn.init)
            else:
                init_type = self.accept(defn.init)
                if defn.kind == LDEF and not defn.is_top_level:
                    # Infer local variable type if there is an initializer
                    # except if the# definition is at the top level (outside a
                    # function).
                    self.infer_local_variable_type(defn.items, init_type, defn)
        else:
            # No initializer
            if (defn.kind == LDEF and not defn.items[0].type and
                    not defn.is_top_level and not self.is_dynamic_function()):
                self.fail(messages.NEED_ANNOTATION_FOR_VAR, defn)
    
    def infer_local_variable_type(self, x, y, z):
        # TODO
        raise RuntimeError('Not implemented')
    
    Type visit_overloaded_func_def(self, OverloadedFuncDef defn):
        for fdef in defn.items:
            self.check_func_item(fdef)
        if defn.info:
            self.check_method_override(defn)
    
    Type visit_func_def(self, FuncDef defn):
        """Type check a function definition."""
        self.check_func_item(defn)
        if defn.info:
            self.check_method_override(defn)
    
    Type check_func_item(self, FuncItem defn, Callable type_override=None):
        # We may be checking a function definition or an anonymous function. In
        # the first case, set up another reference with the precise type.
        FuncDef fdef = None
        if isinstance(defn, FuncDef):
            fdef = (FuncDef)defn
        
        self.dynamic_funcs.append(defn.type is None and not type_override)
        
        if fdef:
            self.errors.set_function(fdef.name())
        
        typ = function_type(defn)
        if type_override:
            typ = type_override
        if isinstance(typ, Callable):
            self.check_func_def(defn, typ)
        else:
            raise RuntimeError('Not supported')
        
        if fdef:
            self.errors.set_function(None)
        
        self.dynamic_funcs.pop()
    
    void check_func_def(self, FuncItem defn, Type typ):
        """Check a function definition."""
        # We may be checking a function definition or an anonymous function. In
        # the first case, set up another reference with the precise type.
        if isinstance(defn, FuncDef):
            fdef = (FuncDef)defn
        else:
            fdef = None
        
        self.enter()
        
        if fdef:
            # The cast below will work since non-method create will cause
            # semantic analysis to fail, and type checking won't be done.
            if (fdef.info and fdef.name() == '__init__' and
                    not isinstance(((Callable)typ).ret_type, Void) and
                    not self.dynamic_funcs[-1]):
                self.fail(messages.INIT_MUST_NOT_HAVE_RETURN_TYPE, defn.type)
        
        # Push return type.
        self.return_types.append(((Callable)typ).ret_type)
        
        # Store argument types.
        ctype = (Callable)typ
        nargs = len(defn.args)
        for i in range(len(ctype.arg_types)):
            arg_type = ctype.arg_types[i]
            if ctype.arg_kinds[i] == nodes.ARG_STAR:
                arg_type = self.named_generic_type('builtins.list', [arg_type])
            elif ctype.arg_kinds[i] == nodes.ARG_STAR2:
                arg_type = self.named_generic_type('builtins.dict',
                                                   [self.str_type(), arg_type])
            defn.args[i].type = arg_type
        
        # Type check initialization expressions.
        for j in range(len(defn.init)):
            if defn.init[j]:
                self.accept(defn.init[j])
        
        # Type check body.
        self.accept(defn.body)
        
        # Pop return type.
        self.return_types.pop()
        
        self.leave()
    
    void check_method_override(self, FuncBase defn):
        """Check that function definition is compatible with any overridden
        definitions defined in superclasses or implemented interfaces.
        """
        # Check against definitions in superclass.
        self.check_method_or_accessor_override_for_base(defn, defn.info.base)
        # Check against definitions in implemented interfaces.
        for iface in defn.info.interfaces:
            self.check_method_or_accessor_override_for_base(defn, iface)
    
    void check_method_or_accessor_override_for_base(self, FuncBase defn,
                                                    TypeInfo base):
        """Check that function definition is compatible with any overridden
        definition in the specified supertype.
        """
        if base:
            if defn.name() != '__init__':
                # Check method override (create is special).
                base_method = base.get_method(defn.name())
                if base_method and base_method.info == base:
                    # There is an overridden method in the supertype.
                    
                    # Construct the type of the overriding method.
                    typ = method_type(defn)
                    # Map the overridden method type to subtype context so that
                    # it can be checked for compatibility. Note that multiple
                    # types from multiple implemented interface instances may
                    # be present.
                    original_type = map_type_from_supertype(
                        method_type(base_method), defn.info, base)
                    # Check that the types are compatible.
                    # TODO overloaded signatures
                    self.check_override((FunctionLike)typ,
                                        (FunctionLike)original_type,
                                        defn.name(),
                                        base_method.info.name(),
                                        defn)
            
            # Also check interface implementations.
            for iface in base.interfaces:
                self.check_method_or_accessor_override_for_base(defn, iface)
            
            # We have to check that the member is compatible with all
            # supertypes due to the dynamic type. Otherwise we could first
            # override with dynamic and then with an arbitary type.
            self.check_method_or_accessor_override_for_base(defn, base.base)
    
    void check_override(self, FunctionLike override, FunctionLike original,
                        str name, str supertype, Context node):
        """Check a method override with given signatures.

        Arguments:
          override:  The signature of the overriding method.
          original:  The signature of the original supertype method.
          name:      The name of the subtype. This and the next argument are
                     only used for generating error messages.
          supertype: The name of the supertype.
        """
        if (isinstance(override, Overloaded) or
                isinstance(original, Overloaded) or
                len(((Callable)override).arg_types) !=
                    len(((Callable)original).arg_types) or
                ((Callable)override).min_args !=
                    ((Callable)original).min_args):
            if not is_subtype(override, original):
                self.msg.signature_incompatible_with_supertype(
                    name, supertype, node)
            return
        else:
            # Give more detailed messages for the common case of both
            # signatures having the same number of arguments and no
            # intersection types.
            
            coverride = (Callable)override
            coriginal = (Callable)original
            
            for i in range(len(coverride.arg_types)):
                if not is_equivalent(coriginal.arg_types[i],
                                     coverride.arg_types[i]):
                    self.msg.argument_incompatible_with_supertype(
                        i + 1, name, supertype, node)
            
            if not is_subtype(coverride.ret_type, coriginal.ret_type):
                self.msg.return_type_incompatible_with_supertype(
                    name, supertype, node)
    
    Type visit_type_def(self, TypeDef defn):
        """Type check a type definition (class or interface)."""
        typ = (TypeInfo)self.lookup(defn.name, GDEF).node
        self.errors.set_type(defn.name, defn.is_interface)
        self.check_unique_interface_implementations(typ)
        self.check_interface_errors(typ)
        self.check_no_constructor_if_interface(typ)
        self.accept(defn.defs)
        self.report_error_if_statements_in_class_body(defn.defs)
        self.errors.set_type(None, False)

    void check_no_constructor_if_interface(self, TypeInfo typ):
        if not typ.is_interface:
            return
        ctor = typ.get_method('__init__')
        if ctor is None:
            return
        self.msg.interface_has_constructor(ctor)
    
    void check_unique_interface_implementations(self, TypeInfo typ):
        """Check that each interface is implemented only once."""
        ifaces = typ.interfaces[:]
        
        dup = find_duplicate(ifaces)
        if dup:
            self.msg.duplicate_interfaces(typ, dup)
            return 
        
        base = typ.base
        while base:
            # Avoid duplicate error messages.
            if find_duplicate(base.interfaces):
                return 
            
            ifaces.extend(base.interfaces)
            dup = find_duplicate(ifaces)
            if dup:
                self.msg.duplicate_interfaces(typ, dup)
                return 
            base = base.base
    
    void check_interface_errors(self, TypeInfo typ):
        interfaces = typ.all_directly_implemented_interfaces()
        for iface in interfaces:
            for n in iface.methods.keys():
                if not typ.has_method(n):
                    self.msg.interface_member_not_implemented(typ, iface, n)

    void report_error_if_statements_in_class_body(self, Block defs):
        for b in defs.body:
            if (isinstance(b, ExpressionStmt) and
                    isinstance(((ExpressionStmt)b).expr, StrExpr)):
                # Just a string literal (probably a doc string); ok.
                continue
            if type(b) not in [AssignmentStmt, VarDef, FuncDef,
                               OverloadedFuncDef, PassStmt]:
                self.msg.not_implemented('statement in class body', b)
    
    #
    # Statements
    #
    
    Type visit_block(self, Block b):
        for s in b.body:
            self.accept(s)
    
    Type visit_assignment_stmt(self, AssignmentStmt s):
        """Type check an assignment statement. Handle all kinds of assignment
        statements (simple, indexed, multiple).
        """
        # TODO support chained assignment x = y = z
        if len(s.lvalues) > 1:
            self.msg.not_implemented('chained assignment', s)

        self.check_assignments(self.expand_lvalues(s.lvalues[0]), s.rvalue)

    void check_assignments(self, Node[] lvalues, Node rvalue):        
        # Collect lvalue types. Index lvalues require special consideration,
        # since we cannot typecheck them until we know the rvalue type.
        # For each lvalue, one of lvalue_types[i] or index_lvalues[i] is not
        # None.
        lvalue_types = <Type> []       # Each may be None
        index_lvalues = <IndexExpr> [] # Each may be None
        inferred = <Var> []
        is_inferred = False
        
        for lv in lvalues:
            if self.is_definition(lv):
                is_inferred = True
                if isinstance(lv, NameExpr):
                    n = (NameExpr)lv
                    inferred.append(((Var)n.node))
                else:
                    m = (MemberExpr)lv
                    self.accept(m.expr)
                    inferred.append(m.def_var)
                lvalue_types.append(None)
                index_lvalues.append(None)
            elif isinstance(lv, IndexExpr):
                ilv = (IndexExpr)lv
                lvalue_types.append(None)
                index_lvalues.append(ilv)
                inferred.append(None)
            else:
                lvalue_types.append(self.accept(lv))
                index_lvalues.append(None)
                inferred.append(None)
        
        if len(lvalues) == 1:
            # Single lvalue.
            self.check_single_assignment(lvalue_types[0],
                                         index_lvalues[0], rvalue, rvalue)
        else:
            self.check_multi_assignment(lvalue_types, index_lvalues,
                                        rvalue, rvalue)
        if is_inferred:
            self.infer_variable_type(inferred, lvalues, self.accept(rvalue),
                                     rvalue)
    
    def is_definition(self, s):
        return ((isinstance(s, NameExpr) or isinstance(s, MemberExpr)) and
                s.is_def)
    
    Node[] expand_lvalues(self, Node n):
        if isinstance(n, TupleExpr):
            return self.expr_checker.unwrap_list(((TupleExpr)n).items)
        elif isinstance(n, ListExpr):
            return self.expr_checker.unwrap_list(((ListExpr)n).items)
        elif isinstance(n, ParenExpr):
            return self.expand_lvalues(((ParenExpr)n).expr)
        else:
            return [n]
    
    void infer_variable_type(self, Var[] names, Node[] lvalues, Type init_type,
                             Context context):
        """Infer the type of initialized variables from the type of the
        initializer expression.
        """
        if isinstance(init_type, Void):
            self.check_not_void(init_type, context)
        elif not self.is_valid_inferred_type(init_type):
            # We cannot use the type of the initialization expression for type
            # inference (it's not specific enough).
            self.fail(messages.NEED_ANNOTATION_FOR_VAR, context)
        else:
            # Infer type of the target.
            
            # Make the type more general (strip away function names etc.).
            init_type = self.strip_type(init_type)
            
            if len(names) > 1:
                if isinstance(init_type, TupleType):
                    tinit_type = (TupleType)init_type
                    # Initializer with a tuple type.
                    if len(tinit_type.items) == len(names):
                        for i in range(len(names)):
                            self.set_inferred_type(names[i], lvalues[i],
                                                   tinit_type.items[i])
                    else:
                        self.msg.incompatible_value_count_in_assignment(
                            len(names), len(tinit_type.items), context)
                elif (isinstance(init_type, Instance) and
                        ((Instance)init_type).type.full_name() ==
                            'builtins.list'):
                    # Initializer with an array type.
                    item_type = ((Instance)init_type).args[0]
                    for i in range(len(names)):
                        self.set_inferred_type(names[i], lvalues[i], item_type)
                elif isinstance(init_type, Any):
                    for i in range(len(names)):
                        self.set_inferred_type(names[i], lvalues[i], Any())
                else:
                    self.fail(messages.INCOMPATIBLE_TYPES_IN_ASSIGNMENT,
                              context)
            else:
                for v in names:
                    self.set_inferred_type(v, lvalues[0], init_type)

    void set_inferred_type(self, Var var, Node lvalue, Type type):
        """Store inferred variable type.

        Store the type to both the variable node and the expression node that
        refers to the variable (lvalue). If var is None, do nothing.
        """
        if var:
            var.type = type
            self.store_type(lvalue, type)
    
    bool is_valid_inferred_type(self, Type typ):
        """Is an inferred type invalid?

        Examples include the None type or a type with a None component.
        """
        if is_same_type(typ, NoneTyp()):
            return False
        elif isinstance(typ, Instance):
            for arg in ((Instance)typ).args:
                if not self.is_valid_inferred_type(arg):
                    return False
        elif isinstance(typ, TupleType):
            for item in ((TupleType)typ).items:
                if not self.is_valid_inferred_type(item):
                    return False
        return True
    
    Type strip_type(self, Type typ):
        """Return a copy of type with all 'debugging information' (e.g. name of
        function) removed.
        """
        if isinstance(typ, Callable):
            ctyp = (Callable)typ
            return Callable(ctyp.arg_types,
                            ctyp.arg_kinds,
                            ctyp.arg_names,
                            ctyp.ret_type,
                            ctyp.is_type_obj(),
                            None,
                            ctyp.variables)
        else:
            return typ
    
    void check_multi_assignment(self, Type[] lvalue_types,
                                IndexExpr[] index_lvalues,
                                Node rvalue,
                                Context context,
                                str msg=None):
        if not msg:
            msg = messages.INCOMPATIBLE_TYPES_IN_ASSIGNMENT
        rvalue_type = self.accept(rvalue) # TODO maybe elsewhere; redundant
        # Try to expand rvalue to lvalue(s).
        if isinstance(rvalue_type, Any):
            pass
        elif isinstance(rvalue_type, TupleType):
            # Rvalue with tuple type.
            trvalue = (TupleType)rvalue_type
            Type[] items = []
            for i in range(len(lvalue_types)):
                if lvalue_types[i]:
                    items.append(lvalue_types[i])
                elif i < len(trvalue.items):
                    # TODO Figure out more precise type context, probably
                    #      based on the type signature of the _set method.
                    items.append(trvalue.items[i])
            trvalue = ((TupleType)self.accept(rvalue, TupleType(items)))
            if len(trvalue.items) != len(lvalue_types):
                self.msg.incompatible_value_count_in_assignment(
                    len(lvalue_types), len(trvalue.items), context)
            else:
                # The number of values is compatible. Check their types.
                for j in range(len(lvalue_types)):
                    self.check_single_assignment(
                        lvalue_types[j], index_lvalues[j],
                        self.temp_node(trvalue.items[j]), context, msg)
        elif (isinstance(rvalue_type, Instance) and
                ((Instance)rvalue_type).type.full_name() == 'builtins.list'):
            # Rvalue with list type.
            item_type = ((Instance)rvalue_type).args[0]
            for k in range(len(lvalue_types)):
                self.check_single_assignment(lvalue_types[k],
                                             index_lvalues[k],
                                             self.temp_node(item_type),
                                             context, msg)
        else:
            self.fail(msg, context)
    
    void check_single_assignment(self,
                          Type lvalue_type, IndexExpr index_lvalue,
                          Node rvalue, Context context,
                          str msg=messages.INCOMPATIBLE_TYPES_IN_ASSIGNMENT):
        """Type check an assignment.

        If lvalue_type is None, the index_lvalue argument must be the
        index expr for indexed assignment (__setitem__).
        Otherwise, lvalue_type is used as the type of the lvalue.
        """
        if lvalue_type:
            rvalue_type = self.accept(rvalue, lvalue_type)
            self.check_subtype(rvalue_type, lvalue_type, context, msg)
        elif index_lvalue:
            self.check_indexed_assignment(index_lvalue, rvalue, context)
    
    void check_indexed_assignment(self, IndexExpr lvalue,
                                  Node rvalue, Context context):
        """Type check indexed assignment base[index] = rvalue.

        The lvalue argument is the base[index] expression.
        """
        basetype = self.accept(lvalue.base)
        method_type = self.expr_checker.analyse_external_member_access(
            '__setitem__', basetype, context)
        lvalue.method_type = method_type
        self.expr_checker.check_call(method_type, [lvalue.index, rvalue],
                                     [nodes.ARG_POS, nodes.ARG_POS],
                                     context)
    
    Type visit_expression_stmt(self, ExpressionStmt s):
        self.accept(s.expr)
    
    Type visit_return_stmt(self, ReturnStmt s):
        """Type check a return statement."""
        if self.is_within_function():
            if s.expr:
                # Return with a value.
                typ = self.accept(s.expr, self.return_types[-1])
                # Returning a value of type dynamic is always fine.
                if not isinstance(typ, Any):
                    if isinstance(self.return_types[-1], Void):
                        self.fail(messages.NO_RETURN_VALUE_EXPECTED, s)
                    else:
                        self.check_subtype(
                            typ, self.return_types[-1], s,
                            messages.INCOMPATIBLE_RETURN_VALUE_TYPE)
            else:
                # Return without a value.
                if (not isinstance(self.return_types[-1], Void) and
                        not self.is_dynamic_function()):
                    self.fail(messages.RETURN_VALUE_EXPECTED, s)
    
    Type visit_yield_stmt(self, YieldStmt s):
        return_type = self.return_types[-1]
        if isinstance(return_type, Instance):
            inst = (Instance)return_type
            if inst.type.full_name() != 'builtins.Iterator':
                self.fail(messages.INVALID_RETURN_TYPE_FOR_YIELD, s)
                return None
            expected_item_type = inst.args[0]
        elif isinstance(return_type, Any):
            expected_item_type = Any()
        else:
            self.fail(messages.INVALID_RETURN_TYPE_FOR_YIELD, s)
            return None
        actual_item_type = self.accept(s.expr, expected_item_type)
        self.check_subtype(actual_item_type, expected_item_type, s)
    
    Type visit_if_stmt(self, IfStmt s):
        """Type check an if statement."""
        for e in s.expr:
            t = self.accept(e)
            self.check_not_void(t, e)
        for b in s.body:
            self.accept(b)
        if s.else_body:
            self.accept(s.else_body)
    
    Type visit_while_stmt(self, WhileStmt s):
        """Type check a while statement."""
        t = self.accept(s.expr)
        self.check_not_void(t, s)
        self.accept(s.body)
        if s.else_body:
            self.accept(s.else_body)
    
    Type visit_operator_assignment_stmt(self, OperatorAssignmentStmt s):
        """Type check an operator assignment statement, e.g. x += 1."""
        lvalue_type = self.accept(s.lvalue)
        rvalue_type, method_type = self.expr_checker.check_op(
            nodes.op_methods[s.op], lvalue_type, s.rvalue, s)
        
        if isinstance(s.lvalue, IndexExpr):
            lv = (IndexExpr)s.lvalue
            self.check_single_assignment(None, lv, s.rvalue, s.rvalue)
        else:
            if not is_subtype(rvalue_type, lvalue_type):
                self.msg.incompatible_operator_assignment(s.op, s)
    
    Type visit_assert_stmt(self, AssertStmt s):
        self.accept(s.expr)
    
    Type visit_raise_stmt(self, RaiseStmt s):
        """Type check a raise statement."""
        typ = self.accept(s.expr)
        self.check_subtype(typ, self.named_type('builtins.BaseException'), s,
                           messages.INVALID_EXCEPTION_TYPE)
    
    Type visit_try_stmt(self, TryStmt s):
        """Type check a try statement."""
        self.accept(s.body)
        for i in range(len(s.handlers)):
            if s.types[i]:
                t = self.exception_type(s.types[i])
                if s.vars[i]:
                    s.vars[i].type = t
            self.accept(s.handlers[i])
        if s.finally_body:
            self.accept(s.finally_body)
        if s.else_body:
            self.accept(s.else_body)
    
    Type exception_type(self, Node n):
        if isinstance(n, NameExpr):
            name = (NameExpr)n
            if isinstance(name.node, TypeInfo):
                return self.check_exception_type((TypeInfo)name.node, n)
        elif isinstance(n, MemberExpr):
            m = (MemberExpr)n
            if isinstance(m.node, TypeInfo):
                return self.check_exception_type((TypeInfo)m.node, n)
        elif isinstance(self.expr_checker.unwrap(n), TupleExpr):
            self.fail('Multiple exception types not supported yet', n)
            return Any()
        self.fail('Unsupported exception', n)
        return Any()

    Type check_exception_type(self, TypeInfo info, Context context):
        t = Instance(info, [])
        if is_subtype(t, self.named_type('builtins.BaseException')):
            return t
        else:
            self.fail(messages.INVALID_EXCEPTION_TYPE, context)
            return Any()

    Type visit_for_stmt(self, ForStmt s):
        """Type check a for statement."""
        item_type = self.analyse_iterable_item_type(s.expr)
        self.analyse_index_variables(s.index, s.is_annotated(), item_type, s)
        self.accept(s.body)

    Type analyse_iterable_item_type(self, Node expr):
        """Analyse iterable expression and return iterator item type."""
        iterable = self.accept(expr)
        
        self.check_not_void(iterable, expr)
        self.check_subtype(iterable,
                           self.named_generic_type('builtins.Iterable',
                                                   [Any()]),
                           expr, messages.ITERABLE_EXPECTED)
        
        echk = self.expr_checker
        method = echk.analyse_external_member_access('__iter__', iterable,
                                                     expr)
        iterator = echk.check_call(method, [], [], expr)[0]
        method = echk.analyse_external_member_access('__next__', iterator,
                                                     expr)
        return echk.check_call(method, [], [], expr)[0]

    void analyse_index_variables(self, NameExpr[] index, bool is_annotated,
                                 Type item_type, Context context):
        """Type check or infer for loop or list comprehension index vars."""
        if not is_annotated:
            # Create a temporary copy of variables with Node item type.
            # TODO this is ugly
            node_index = <Node> []
            for i in index:
                node_index.append(i)
            self.check_assignments(node_index,
                                   self.temp_node(item_type, context))
        elif len(index) == 1:
            v = (Var)index[0].node
            if v.type:
                self.check_single_assignment(v.type, None,
                                           self.temp_node(item_type), context,
                                           messages.INCOMPATIBLE_TYPES_IN_FOR)
        else:
            Type[] t = []
            for ii in index:
                v = (Var)ii.node
                if v.type:
                    t.append(v.type)
                else:
                    t.append(Any())
            self.check_multi_assignment(t, <IndexExpr> [None] * len(index),
                                        self.temp_node(item_type), context,
                                        messages.INCOMPATIBLE_TYPES_IN_FOR)
    
    Type visit_del_stmt(self, DelStmt s):
        if isinstance(s.expr, IndexExpr):
            e = (IndexExpr)s.expr  # Cast
            m = MemberExpr(e.base, '__delitem__')
            m.line = s.line
            c = CallExpr(m, [e.index], [nodes.ARG_POS], [None])
            c.line = s.line
            return c.accept(self)
        else:
            return None # this case is handled in semantical analysis
    
    #
    # Expressions
    #
    
    Type visit_name_expr(self, NameExpr e):
        return self.expr_checker.visit_name_expr(e)
    
    Type visit_paren_expr(self, ParenExpr e):
        return self.expr_checker.visit_paren_expr(e)
    
    Type visit_call_expr(self, CallExpr e):
        return self.expr_checker.visit_call_expr(e)
    
    Type visit_member_expr(self, MemberExpr e):
        return self.expr_checker.visit_member_expr(e)
    
    Type visit_int_expr(self, IntExpr e):
        return self.expr_checker.visit_int_expr(e)
    
    Type visit_str_expr(self, StrExpr e):
        return self.expr_checker.visit_str_expr(e)
    
    Type visit_bytes_expr(self, BytesExpr e):
        return self.expr_checker.visit_bytes_expr(e)
    
    Type visit_float_expr(self, FloatExpr e):
        return self.expr_checker.visit_float_expr(e)
    
    Type visit_op_expr(self, OpExpr e):
        return self.expr_checker.visit_op_expr(e)
    
    Type visit_unary_expr(self, UnaryExpr e):
        return self.expr_checker.visit_unary_expr(e)
    
    Type visit_index_expr(self, IndexExpr e):
        return self.expr_checker.visit_index_expr(e)
    
    Type visit_cast_expr(self, CastExpr e):
        return self.expr_checker.visit_cast_expr(e)
    
    Type visit_super_expr(self, SuperExpr e):
        return self.expr_checker.visit_super_expr(e)
    
    Type visit_type_application(self, TypeApplication e):
        return self.expr_checker.visit_type_application(e)
    
    Type visit_list_expr(self, ListExpr e):
        return self.expr_checker.visit_list_expr(e)
    
    Type visit_tuple_expr(self, TupleExpr e):
        return self.expr_checker.visit_tuple_expr(e)
    
    Type visit_dict_expr(self, DictExpr e):
        return self.expr_checker.visit_dict_expr(e)
    
    Type visit_slice_expr(self, SliceExpr e):
        return self.expr_checker.visit_slice_expr(e)
    
    Type visit_func_expr(self, FuncExpr e):
        return self.expr_checker.visit_func_expr(e)
    
    Type visit_list_comprehension(self, ListComprehension e):
        return self.expr_checker.visit_list_comprehension(e)

    Type visit_generator_expr(self, GeneratorExpr e):
        return self.expr_checker.visit_generator_expr(e)

    Type visit_temp_node(self, TempNode e):
        return e.type

    #
    # Currently unsupported features
    #

    Type visit_set_expr(self, SetExpr e):
        return self.msg.not_implemented('set literal', e)

    Type visit_conditional_expr(self, ConditionalExpr e):
        return self.msg.not_implemented('conditional expression', e)

    Type visit_decorator(self, Decorator e):
        return self.msg.not_implemented('decorator', e)
    
    Type visit_with_stmt(self, WithStmt s):
        self.msg.not_implemented('with statement', s)
    
    #
    # Helpers
    #
    
    void check_subtype(self, Type subtype, Type supertype, Context context,
                       str msg=messages.INCOMPATIBLE_TYPES):
        """Generate an error if the subtype is not compatible with
        supertype."""
        if not is_subtype(subtype, supertype):
            if isinstance(subtype, Void):
                self.msg.does_not_return_value(subtype, context)
            else:
                self.fail(msg, context)
    
    Instance named_type(self, str name):
        """Return an instance type with type given by the name and no
        type arguments. For example, named_type('builtins.object')
        produces the object type.
        """
        # Assume that the name refers to a type.
        sym = self.lookup_qualified(name)
        return Instance((TypeInfo)sym.node, [])
    
    Type named_type_if_exists(self, str name):
        """Return named instance type, or UnboundType if the type was
        not defined.
        
        This is used to simplify test cases by avoiding the need to
        define basic types not needed in specific test cases (tuple
        etc.).
        """
        try:
            # Assume that the name refers to a type.
            sym = self.lookup_qualified(name)
            return Instance((TypeInfo)sym.node, [])
        except KeyError:
            return UnboundType(name)
    
    Instance named_generic_type(self, str name, Type[] args):
        """Return an instance with the given name and type
        arguments. Assume that the number of arguments is correct.
        """
        # Assume that the name refers to a compatible generic type.
        sym = self.lookup_qualified(name)
        return Instance((TypeInfo)sym.node, args)
    
    Instance type_type(self):
        """Return instance type 'type'."""
        return self.named_type('builtins.type')
    
    Instance object_type(self):
        """Return instance type 'object'."""
        return self.named_type('builtins.object')
    
    Instance bool_type(self):
        """Return instance type 'bool'."""
        return self.named_type('builtins.bool')
    
    Instance str_type(self):
        """Return instance type 'str'."""
        return self.named_type('builtins.str')
    
    Type tuple_type(self):
        """Return instance type 'tuple'."""
        # We need the tuple for analysing member access. We want to be able to
        # do this even if tuple type is not available (useful in test cases),
        # so we return an unbound type if there is no tuple type.
        return self.named_type_if_exists('builtins.tuple')
    
    void check_type_equivalency(self, Type t1, Type t2, Context node,
                                str msg=messages.INCOMPATIBLE_TYPES):
        """Generate an error if the types are not equivalent. The
        dynamic type is equivalent with all types.
        """
        if not is_equivalent(t1, t2):
            self.fail(msg, node)
    
    void store_type(self, Node node, Type typ):
        """Store the type of a node in the type map."""
        self.type_map[node] = typ
    
    bool is_dynamic_function(self):
        return len(self.dynamic_funcs) > 0 and self.dynamic_funcs[-1]
    
    SymbolTableNode lookup(self, str name, int kind):
        """Look up a definition from the symbol table with the given name.
        TODO remove kind argument
        """
        if self.locals is not None and name in self.locals:
            return self.locals[name]
        elif self.class_tvars is not None and name in self.class_tvars:
            return self.class_tvars[name]
        elif name in self.globals:
            return self.globals[name]
        else:
            b = self.globals.get('__builtins__', None)
            if b:
                table = ((MypyFile)b.node).names
                if name in table:
                    return table[name]
            raise KeyError('Failed lookup: {}'.format(name))
    
    SymbolTableNode lookup_qualified(self, str name):
        if '.' not in name:
            return self.lookup(name, GDEF) # FIX kind
        else:
            parts = name.split('.')
            n = self.modules[parts[0]]
            for i in range(1, len(parts) - 1):
                n = (MypyFile)((n.names.get(parts[i], None).node))
            return n.names[parts[-1]]
    
    void enter(self):
        self.locals = SymbolTable()
    
    void leave(self):
        self.locals = None
    
    BasicTypes basic_types(self):
        """Return a BasicTypes instance that contains primitive types that are
        needed for certain type operations (joins, for example).
        """
        # TODO function type
        return BasicTypes(self.object_type(), self.type_type(),
                          self.named_type_if_exists('builtins.tuple'),
                          self.named_type_if_exists('builtins.function'))
    
    bool is_within_function(self):
        """Are we currently type checking within a function (i.e. not
        at class body or at the top level)?
        """
        return self.return_types != []
    
    void check_not_void(self, Type typ, Context context):
        """Generate an error if the type is Void."""
        if isinstance(typ, Void):
            self.msg.does_not_return_value(typ, context)
    
    Node temp_node(self, Type t, Context context=None):
        """Create a temporary node with the given, fixed type."""
        temp = TempNode(t)
        if context:
            temp.set_line(context.get_line())
        return temp
    
    void fail(self, str msg, Context context):
        """Produce an error message."""
        self.msg.fail(msg, context)


Type map_type_from_supertype(Type typ, TypeInfo sub_info, TypeInfo super_info):
    """Map type variables in a type defined in a supertype context to be valid
    in the subtype context. Assume that the result is unique; if more than
    one type is possible, return one of the alternatives.
    
    For example, assume
    
      class D<S> ...
      class C<T> is D<E<T>> ...
    
    Now S in the context of D would be mapped to E<T> in the context of C.
    """
    # Create the type of self in subtype, of form t<a1, ...>.
    inst_type = self_type(sub_info)
    # Map the type of self to supertype. This gets us a description of the
    # supertype type variables in terms of subtype variables, i.e. t<t1, ...>
    # so that any type variables in tN are to be interpreted in subtype
    # context.
    inst_type = map_instance_to_supertype(inst_type, super_info)
    # Finally expand the type variables in type with those in the previously
    # constructed type. Note that both type and inst_type may have type
    # variables, but in type they are interpreterd in supertype context while
    # in inst_type they are interpreted in subtype context. This works even if
    # the names of type variables in supertype and subtype overlap.
    return expand_type_by_instance(typ, inst_type)


T find_duplicate<T>(T[] list):
    """If the list has duplicates, return one of the duplicates.

    Otherwise, return None.
    """
    for i in range(1, len(list)):
        if list[i] in list[:i]:
            return list[i]
    return None
