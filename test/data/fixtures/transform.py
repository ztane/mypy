# Builtins stubs used implicitly in program transformation test cases.

class object:
    void __init__(self): pass

class type: pass

# str is handy for debugging; allows outputting messages.
class str: pass

# Primitive types int/float have special coercion behaviour (they may have
# a different representation from ordinary values).

class int: pass

class float: pass


# The functions below are special functions used in test cases; their
# implementations are actually in the __dynchk module, but they are defined
# here so that the semantic analyser and the type checker are happy without
# having to analyse the entire __dynchk module all the time.
#
# The transformation implementation has special case handling for these
# functions; it's a bit ugly but it works for now.

def __print(a1=None, a2=None, a3=None, a4=None):
    # Do not use *args since this would require list and break many test
    # cases.
    pass
