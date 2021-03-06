from nodes import TypeInfo
from mtypes import Instance, Type, TypeVar, Any


interface MapPremise: pass


interface MapExpr: pass


class AssertClass(MapPremise):
    MapExpr i
    TypeInfo c
    
    void __init__(self, MapExpr i, TypeInfo c):
        self.i = i
        self.c = c
    
    def __str__(self):
        return str(self.i) + ' = ' + self.c.name + '<...>'


class AssertDyn(MapPremise):
    MapExpr i
    
    void __init__(self, MapExpr i):
        self.i = i
    
    def __str__(self):
        return str(self.i) + ' = dyn'


class AssertEq(MapPremise):
    MapExpr i1
    MapExpr i2
    
    void __init__(self, MapExpr i1, MapExpr i2):
        self.i1 = i1
        self.i2 = i2
    
    def __str__(self):
        return str(self.i1) + ' = ' + str(self.i2)


class TypeVarRef(MapExpr):
    int n
    
    void __init__(self, int n):
        self.n = n
    
    def __str__(self):
        return str(self.n)


class TypeArgRef(MapExpr):
    MapExpr base
    int n
    
    void __init__(self, MapExpr base, int n):
        self.base = base
        self.n = n
    
    def __str__(self):
        return str(self.base) + '.' + str(self.n)


class DefaultArg(MapExpr):
    def __str__(self):
        return 'd'


tuple<MapPremise[], MapExpr[]> compile_subclass_mapping(
                               int num_subtype_type_vars, Instance super_type):
    """Compile mapping from superclass to subclass type variables.

    Args:
      num_subtype_type_vars: number of type variables in subclass
      super_type:         definition of supertype; this may contain type
                          variable references
    """
    premises = find_eq_premises(super_type, None)
    MapExpr[] exprs = []
    
    for i in range(1, num_subtype_type_vars + 1):
        paths = find_all_paths(i, super_type, None)
        if len(paths) == 0:
            exprs.append(DefaultArg())
        else:
            exprs.append(paths[0])
            if len(paths) > 1:
                # Multiple paths; make sure they are all the same.
                for j in range(1, len(paths)):
                    premises.append(AssertEq(paths[0], paths[j]))
    
    return premises, exprs  


MapExpr[] find_all_paths(int tv, Type typ, MapExpr expr):
    if isinstance(typ, TypeVar) and ((TypeVar)typ).id == tv:
        return [expr]
    elif isinstance(typ, Instance) and ((Instance)typ).args != []:
        inst = (Instance)typ
        MapExpr[] res = []
        for i in range(len(inst.args)):
            MapExpr e
            if not expr:
                e = TypeVarRef(i + 1)
            else:
                e = TypeArgRef(expr, i + 1)
            res += find_all_paths(tv, inst.args[i], e)
        return res
    else:
        return []


MapPremise[] find_eq_premises(Type typ, MapExpr expr):
    if isinstance(typ, Instance):
        inst = (Instance)typ
        MapPremise[] res = []
        if expr:
            res.append(AssertClass(expr, inst.type))
        for i in range(len(inst.args)):
            MapExpr e
            if not expr:
                e = TypeVarRef(i + 1)
            else:
                e = TypeArgRef(expr, i + 1)
            res += find_eq_premises(inst.args[i], e)
        return res
    elif isinstance(typ, Any):
        return [AssertDyn(expr)]
    else:
        return []
