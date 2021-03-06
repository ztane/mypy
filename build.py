"""Facilities to build mypy programs and modules they depend on.

Parse, analyze and translate the source files of a program in the correct
order (based on file dependencies), and collect the results.

This module only directs a build, which is performed in multiple passes per
file.  The individual passes are implemented in separate modules.

The function build() is the main interface to this module.
"""

import os
import os.path
import shlex
import subprocess
import sys
from os.path import dirname, basename

from mtypes import Type
from nodes import MypyFile, Node, Import, ImportFrom, ImportAll, MODULE_REF
from nodes import SymbolTableNode
from semanal import TypeInfoMap, SemanticAnalyzer
from checker import TypeChecker
from errors import Errors
from icode import FuncIcode
import cgen
import icode
import parse
import pythongen
import transform


debug = False


# Build targets (for selecting compiler passes)
SEMANTIC_ANALYSIS = 0   # Semantic analysis only
TYPE_CHECK = 1          # Type check
PYTHON = 2              # Type check and generate Python
TRANSFORM = 3           # Type check and transform for runtime type checking
ICODE = 4               # All TRANSFORM passes + generate icode
C = 5                   # All ICODE passes + generate C and compile it


# Build flags
PYTHON2 = 'python2'           # Generate Python 2
COMPILE_ONLY = 'compile-only' # Compile only to C, do not generate binary
VERBOSE = 'verbose'           # More verbose messages (for troubleshooting)


# State ids. These describe the states a source file / module can be in a
# build.

# We aren't processing this source file yet (no associated state object).
UNSEEN_STATE = 0
# The source file has a state object, but we haven't done anything with it yet.
UNPROCESSED_STATE = 1
# We've parsed the source file.
PARSED_STATE = 2
# We've semantically analyzed the source file.
SEMANTICALLY_ANALYSED_STATE = 3
# We've type checked the source file (and all its dependencies).
TYPE_CHECKED_STATE = 4


final_state = TYPE_CHECKED_STATE


bool earlier_state(int s, int t):
    return s < t


class BuildResult:
    """The result of a successful build."""
    # Map module name to related AST node.
    dict<str, MypyFile> files
    # Map qualified type name to related TypeInfo; includes each class and
    # interface defined in all the built files.
    TypeInfoMap typeinfos
    # Map parse tree node to its inferred type.
    dict<Node, Type> types
    # Icode for functions
    dict<str, FuncIcode> icode
    # Path of generated binary file (for the C back end, None otherwise)
    str binary_path

    void __init__(self, dict<str, MypyFile> files, TypeInfoMap typeinfos,
                  dict<Node, Type> types, dict<str, FuncIcode> icode,
                  str binary_path):
        self.files = files
        self.typeinfos = typeinfos
        self.types = types
        self.icode = icode
        self.binary_path = binary_path


BuildResult build(str program_text,
                  str program_path,
                  int target,
                  bool test_builtins=False,
                  str alt_lib_path=None,
                  str mypy_base_dir=None,
                  str output_dir=None,
                  str[] flags=None):
    """Build a program represented as a string (program_text).

    A single call to build performs parsing, semantic analysis and optionally
    type checking and other build passes for the program *and* all imported
    modules, recursively.

    Return BuildResult if successful; otherwise raise CompileError.
    
    Arguments:
      program_text: the contents of the main (program) source file
      program_path: the path to the main source file, for error reporting
      target: select passes to perform (a build target constant, e.g. C)
    Optional arguments:
      test_builtins: if False, use normal builtins (default); if True, use
        minimal stub builtins (this is for test cases only)
      alt_lib_dir: an additional directory for looking up library modules
        (takes precedence over other directories)
      mypy_base_dir: directory of mypy implementation (mypy.py); if omitted,
        derived from sys.argv[0]
      output_dir: directory where the output (Python) is stored
      flags: list of build options (e.g. COMPILE_ONLY)
    """

    if target == PYTHON and not output_dir:
        raise RuntimeError('output_dir must be set for Python target')

    if not mypy_base_dir:
        # Determine location of the mypy installation.
        mypy_base_dir = dirname(sys.argv[0])
        if basename(mypy_base_dir) == '__mycache__':
            # If we have been translated to Python, the Python code is in the
            # __mycache__ subdirectory of the actual directory. Strip off
            # __mycache__.
            mypy_base_dir = dirname(mypy_base_dir)
            
    # Determine the default module search path.
    str[] lib_path = default_lib_path(mypy_base_dir, target)
    
    if test_builtins:
        # Use stub builtins (to speed up test cases and to make them easier to
        # debug).
        lib_path.insert(0, 'test/data/lib-stub')
    else:
        # Include directory of the program file in the module search path.
        lib_path.insert(
            0, remove_cwd_prefix_from_path(dirname(program_path)))
    
    # If provided, insert the caller-supplied extra module path to the
    # beginning (highest priority) of the search path.
    if alt_lib_path:
        lib_path.insert(0, alt_lib_path)
    
    # Construct a build manager object that performs all the stages of the
    # build in the correct order.
    manager = BuildManager(mypy_base_dir, lib_path, target, output_dir,
                           flags or [])
    
    # Ignore current directory prefix in error messages.
    manager.errors.set_ignore_prefix(os.getcwd())
    
    # Construct information that describes the initial file. __main__ is the
    # implicit module id and the import context is empty initially ([]).
    info = StateInfo(program_path, '__main__', [], manager)
    # Perform the build by sending the file as new file (UnprocessedFile is the
    # initial state of all files) to the manager. The manager will process the
    # file and all dependant modules recursively.
    return manager.process(UnprocessedFile(info, program_text))


str[] default_lib_path(str mypy_base_dir, int target):
    """Return default standard library search paths."""
    # IDEA: Make this more portable.
    str[] path = []
    
    # Add MYPYPATH environment variable to library path, if defined.
    path_env = os.getenv('MYPYPATH')
    if path_env is not None:
        path.append(path_env)

    if target in [ICODE, C]:
        # Add C back end library directory.
        path.append(os.path.join(mypy_base_dir, 'lib'))
    else:
        # Add library stubs directory. By convention, they are stored in the
        # stubs directory of the mypy implementation.
        path.append(os.path.join(mypy_base_dir, 'stubs'))
        path.append(os.path.join(mypy_base_dir, 'stubs-auto'))
    
    # Add fallback path that can be used if we have a broken installation.
    if sys.platform != 'win32':
        path.append('/usr/local/lib/mypy')
    
    return path


class BuildManager:
    """This is the central class for building a mypy program.

    It coordinates parsing, import processing, semantic analysis and
    type checking. It manages state objects that actually perform the
    build steps.
    """
    str mypy_base_dir     # Mypy installation directory (contains mypy.py)
    int target            # Build target; selects which passes to perform
    str[] lib_path        # Library path for looking up modules
    SemanticAnalyzer semantic_analyzer # Semantic analyzer
    TypeChecker type_checker      # Type checker
    Errors errors                 # For reporting all errors
    str output_dir                # Store output files here (Python)
    str[] flags                   # Build options
    
    # States of all individual files that are being processed. Each file in a
    # build is always represented by a single state object (after it has been
    # encountered for the first time). This is the only place where states are
    # stored.
    State[] states
    # Map from module name to source file path. There is a 1:1 mapping between
    # modules and source files.
    dict<str, str> module_files
    
    dict<str, FuncIcode> icode
    str binary_path

    # Cache for module dependencies (direct or indirect). Item (m, n)
    # indicates whether m depends on n (directly or indirectly).
    dict<tuple<str, str>, bool> module_deps
    
    void __init__(self, str mypy_base_dir, str[] lib_path, int target,
                  str output_dir, str[] flags):
        self.mypy_base_dir = mypy_base_dir
        self.errors = Errors()
        self.lib_path = lib_path
        self.target = target
        self.output_dir = output_dir
        self.flags = flags
        self.semantic_analyzer = SemanticAnalyzer(lib_path, self.errors)
        self.type_checker = TypeChecker(self.errors,
                                        self.semantic_analyzer.modules)
        self.states = []
        self.module_files = {}
        self.icode = None
        self.binary_path = None
        self.module_deps = {}
    
    BuildResult process(self, UnprocessedFile initial_state):
        """Perform a build.

        The argument is a state that represents the main program
        file. This method should only be called once per a build
        manager object.  The return values are identical to the return
        values of the build function.
        """
        self.states.append(initial_state)
        
        # Process states in a loop until all files (states) have been
        # semantically analyzer or type checked (depending on target).
        #
        # We type check all files before the rest of the passes so that we can
        # report errors and fail as quickly as possible.
        while True:
            # Find the next state that has all its dependencies met.
            next = self.next_available_state()
            if not next:
                trace('done')
                break
            
            # Potentially output some debug information.
            trace('next {} ({})'.format(next.path, next.state()))
            
            # Set the import context for reporting error messages correctly.
            self.errors.set_import_context(next.import_context)
            # Process the state. The process method is reponsible for adding a
            # new state object representing the new state of the file.
            next.process()
        
            # Raise exception if the build failed. The build can fail for
            # various reasons, such as parse error, semantic analysis error,
            # etc.
            if self.errors.is_errors():
                self.errors.raise_error()
        
        # If there were no errors, all files should have been fully processed.
        for s in self.states:
            assert s.state() == final_state, (
                '{} still unprocessed'.format(s.path))
        
        # Collect a list of all files.
        MypyFile[] trees = []
        for state in self.states:
            trees.append(((ParsedFile)state).tree)

        # Perform any additional passes after type checking for all the files.
        self.final_passes(trees, self.type_checker.type_map)
        
        return BuildResult(self.semantic_analyzer.modules,
                           self.semantic_analyzer.types,
                           self.type_checker.type_map,
                           self.icode, self.binary_path)
    
    State next_available_state(self):
        """Find a ready state (one that has all its dependencies met)."""
        i = len(self.states) - 1
        while i >= 0:
            if self.states[i].is_ready():
                num_incomplete = self.states[i].num_incomplete_deps()
                if num_incomplete == 0:
                    # This is perfect; no need to look for the best match.
                    return self.states[i]
            i -= 1
        return None
    
    bool has_module(self, str name):
        """Have we seen a module yet?"""
        return name in self.module_files
    
    int file_state(self, str path):
        """Return the state of a source file.

        In particular, return UNSEEN_STATE if the file has no associated
        state.

        This function does not consider any dependencies.
        """
        for s in self.states:
            if s.path == path:
                return s.state()
        return UNSEEN_STATE
    
    int module_state(self, str name):
        """Return the state of a module.

        In particular, return UNSEEN_STATE if the file has no associated
        state.

        This considers also module dependencies.
        """
        if not self.has_module(name):
            return UNSEEN_STATE
        state = final_state
        fs = self.file_state(self.module_files[name])
        if earlier_state(fs, state):
            state = fs
        return state

    bool is_dep(self, str m1, str m2, set<str> done=None):
        """Does m1 import m2 directly or indirectly?"""
        # Have we computed this previously?
        dep = self.module_deps.get((m1, m2))
        if dep is not None:
            return dep
        
        if not done:
            done = set([m1])
            
        # m1 depends on m2 iff one of the deps of m1 depends on m2.
        st = self.lookup_state(m1)
        for m in st.dependencies:
            if m in done:
                continue
            done.add(m)
            # Cache this dependency.
            self.module_deps[m1, m] = True
            # Search recursively.
            if m == m2 or self.is_dep(m, m2, done):
                # Yes! Mark it in the cache.
                self.module_deps[m1, m2] = True
                return True
        # No dependency. Mark it in the cache.
        self.module_deps[m1, m2] = False
        return False

    State lookup_state(self, str module):
        for state in self.states:
            if state.id == module:
                return state
        raise RuntimeError('%s not found' % str)
    
    tuple<str, int>[] all_imported_modules_in_file(self, MypyFile file):
        """Find all import statements in a file.

        Return list of tuples (module id, import line number) for all modules
        imported in file.
        """
        # TODO also find imports not at the top level of the file
        res = <tuple<str, int>> []
        for d in file.defs:
            if isinstance(d, Import):
                for id, _ in ((Import)d).ids:
                    res.append((id, d.line))
            elif isinstance(d, ImportFrom):
                imp = (ImportFrom)d
                res.append((imp.id, imp.line))
                # Also add any imported names that are submodules.
                for name, __ in imp.names:
                    sub_id = imp.id + '.' + name
                    if self.is_module(sub_id):
                        res.append((sub_id, imp.line))
            elif isinstance(d, ImportAll):
                res.append((((ImportAll)d).id, d.line))
        return res
    
    bool is_module(self, str id):
        """Is there a file in the file system corresponding to module id?"""
        return find_module(id, self.lib_path) is not None

    void final_passes(self, MypyFile[] files, dict<Node, Type> types):
        """Perform the code generation passes for type checked files."""
        if self.target == PYTHON:
            self.generate_python(files)
        elif self.target == TRANSFORM:
            self.transform(files)
        elif self.target == ICODE:
            self.transform(files)
            self.generate_icode(files, types)
        elif self.target == C:
            self.transform(files)
            self.generate_icode(files, types)
            self.generate_c_and_compile(files)
        elif self.target in [SEMANTIC_ANALYSIS, TYPE_CHECK]:
            pass # Nothing to do.
        else:
            raise RuntimeError('Unsupported target %d' % self.target)

    void generate_python(self, MypyFile[] files):
        """Translate each file to Python."""
        # TODO support packages
        for f in files:
            if not is_stub(f.path):
                out_path = os.path.join(self.output_dir, basename(f.path))
                # TODO log translation of f.path to out_path
                # TODO report compile error if failed
                ver = 3
                if PYTHON2 in self.flags:
                    ver = 2
                v = pythongen.PythonGenerator(ver)
                f.accept(v)
                self.log('translate %s to %s' % (f.path, out_path))
                outfile = open(out_path, 'w')
                outfile.write(v.output())
                outfile.close()

    void transform(self, MypyFile[] files):
        for f in files:
            # Transform parse tree and produce pretty-printed output.
            v = transform.DyncheckTransformVisitor(
                self.type_checker.type_map,
                self.semantic_analyzer.modules,
                is_pretty=True)
            f.accept(v)

    void generate_icode(self, MypyFile[] files, dict<Node, Type> types):
        builder = icode.IcodeBuilder(types)
        for f in files:
            # TODO remove ugly builtins hack
            if not f.path.endswith('/builtins.py'):
                f.accept(builder)
        self.icode = builder.generated

    void generate_c_and_compile(self, MypyFile[] files):
        gen = cgen.CGenerator()
        
        for fn, icode in self.icode.items():
            gen.generate_function('M' + fn, icode)

        program_name = os.path.splitext(basename(files[0].path))[0]
        c_file = '%s.c' % program_name

        # Write C file.
        self.log('writing %s' % c_file)
        out = open(c_file, 'w')
        out.writelines(gen.output())
        out.close()

        if COMPILE_ONLY not in self.flags:
            # Generate binary file.
            base_dir = self.mypy_base_dir
            vm_dir = os.path.join(base_dir, 'vm')
            cc = os.getenv('CC', 'gcc')
            cflags = shlex.split(os.getenv('CFLAGS', '-O2'))
            cmdline = [cc] + cflags +['-I%s' % vm_dir,
                                      '-o%s' % program_name,
                                      c_file,
                                      os.path.join(vm_dir, 'runtime.c')]
            self.log(' '.join(cmdline))
            status = subprocess.call(cmdline)
            # TODO check status
            self.log('removing %s' % c_file)
            os.remove(c_file)
            self.binary_path = os.path.join('.', program_name)

    void log(self, str message):
        if VERBOSE in self.flags:
            print('LOG: %s' % message)


str remove_cwd_prefix_from_path(str p):
    """Remove current working directory prefix from p, if present.

    If the result would be empty, return '.' instead.
    """
    cur = os.getcwd()
    # Add separator to the end of the path, unless one is already present.
    if basename(cur) != '':
        cur += os.sep
    # Remove current directory prefix from the path, if present.
    if p.startswith(cur):
        p = p[len(cur):]
    # Avoid returning an empty path; replace that with '.'.
    if p == '':
        p = '.'
    return p


bool is_stub(str path):
    """Does path refer to a stubs file?

    Currently check if there is a 'stubs' directory component somewhere
    in the path."""
    # TODO more precise check
    dirname, basename = os.path.split(path)
    if basename == '':
        return False
    else:
        stubnames = ['stubs', 'stubs-auto']
        return (basename in stubnames) or is_stub(dirname)


class StateInfo:
    """Description of a source file that is being built."""
    # Path to the file
    str path
    # Module id, such as 'os.path' or '__main__' (for the main program file)
    str id
    # The import trail that caused this module to be imported (path, line)
    # tuples
    tuple<str, int>[] import_context
    # The manager that manages this build
    BuildManager manager
    
    void __init__(self, str path, str id, tuple<str, int>[] import_context,
                  BuildManager manager):
        self.path = path
        self.id = id
        self.import_context = import_context
        self.manager = manager


class State:
    """Abstract base class for build states.

    There is always at most one state per source file.
    """

    # The StateInfo attributes are duplicated here for convenience.
    str path
    str id   # Module id
    tuple<str, int>[] import_context
    BuildManager manager
    # Modules that this file directly depends on (in no particular order).
    str[] dependencies
    
    void __init__(self, StateInfo info):
        self.path = info.path
        self.id = info.id
        self.import_context = info.import_context
        self.manager = info.manager
        self.dependencies = []
    
    StateInfo info(self):
        return StateInfo(self.path, self.id, self.import_context, self.manager)
    
    void process(self):
        raise RuntimeError('Not implemented')
    
    bool is_ready(self):
        """Return True if all dependencies are at least in the same state
        as this object (but not in the initial state).
        """
        for module in self.dependencies:
            state = self.manager.module_state(module)
            if earlier_state(state,
                             self.state()) or state == UNPROCESSED_STATE:
                return False
        return True

    int num_incomplete_deps(self):
        """Return the number of dependencies that are ready but incomplete."""
        return 0 # Does not matter in this state
    
    int state(self):
        raise RuntimeError('Not implemented')
    
    void switch_state(self, State state_object):
        """Called by state objects to replace the state of the file.

        Also notify the manager.
        """
        for i in range(len(self.manager.states)):
            if self.manager.states[i].path == state_object.path:
                self.manager.states[i] = state_object
                return 
        raise RuntimeError('State for {} not found'.format(state_object.path))
    
    Errors errors(self):
        return self.manager.errors
    
    SemanticAnalyzer semantic_analyzer(self):
        return self.manager.semantic_analyzer
    
    TypeChecker type_checker(self):
        return self.manager.type_checker
    
    void fail(self, str path, int line, str msg):
        """Report an error in the build (e.g. if could not find a module)."""
        self.errors().set_file(path)
        self.errors().report(line, msg)


class UnprocessedFile(State):
    str program_text # Program text (or None to read from file)
    
    void __init__(self, StateInfo info, str program_text):
        super().__init__(info)
        self.program_text = program_text
        trace('waiting {}'.format(info.path))
        
        # Add surrounding package(s) as dependencies.
        for p in super_packages(self.id):
            if not self.import_module(p):
                # Could not find a module. Typically the reason is a misspelled
                # module name, or the module has not been installed.
                self.fail(self.path, 1, "No module named '{}'".format(p))
            self.dependencies.append(p)
    
    void process(self):
        """Parse the file, store global names and advance to the next state."""
        tree = self.parse(self.program_text, self.path)

        # Store the parsed module in the shared module symbol table.
        self.manager.semantic_analyzer.modules[self.id] = tree
        
        if '.' in self.id:
            # Include module in the symbol table of the enclosing package.
            c = self.id.split('.')
            p = '.'.join(c[:-1])
            sem_anal = self.manager.semantic_analyzer
            sem_anal.modules[p].names[c[-1]] = SymbolTableNode(
                MODULE_REF, tree, p)
        
        if self.id != 'builtins':
            # The builtins module is imported implicitly in every program (it
            # contains definitions of int, print etc.).
            trace('import builtins')
            if not self.import_module('builtins'):
                self.fail(self.path, 1, 'Could not find builtins')

        # Add all directly imported modules to be processed (however they are
        # not processed yet, just waiting to be processed).
        for id, line in self.manager.all_imported_modules_in_file(tree):
            self.errors().push_import_context(self.path, line)
            try:
                res = self.import_module(id)
            finally:
                self.errors().pop_import_context()
            if not res:
                self.fail(self.path, line, "No module named '{}'".format(id))

        # Do the first pass of semantic analysis: add top-level definitions in
        # the file to the symbol table.
        self.semantic_analyzer().anal_defs(tree.defs, self.path, self.id)
        # Initialize module symbol table, which was populated by the semantic
        # analyzer.
        tree.names = self.semantic_analyzer().globals

        # Replace this state object with a parsed state in BuildManager.
        self.switch_state(ParsedFile(self.info(), tree))
    
    bool import_module(self, str id):
        """Schedule a module to be processed.

        Add an unprocessed state object corresponding to the module to the
        manager, or do nothing if the module already has a state object.
        """
        if self.manager.has_module(id):
            # Do nothing:f already being compiled.
            return True
        
        path, text = read_module_source_from_file(id, self.manager.lib_path)
        if text is not None:
            info = StateInfo(path, id, self.errors().import_context(),
                             self.manager)
            self.manager.states.append(UnprocessedFile(info, text))
            self.manager.module_files[id] = path
            return True
        else:
            return False
    
    MypyFile parse(self, str source_text, str fnam):
        """Parse the source of a file with the given name.

        Raise CompileError if there is a parse error.
        """
        num_errs = self.errors().num_messages()
        tree = parse.parse(source_text, fnam, self.errors())
        tree._full_name = self.id
        if self.errors().num_messages() != num_errs:
            self.errors().raise_error()
        return tree
    
    int state(self):
        return UNPROCESSED_STATE


class ParsedFile(State):
    MypyFile tree
    
    void __init__(self, StateInfo info, MypyFile tree):
        super().__init__(info)
        self.tree = tree

        # Build a list all directly imported moules (dependencies).
        str[] imp = []
        for id, line in self.manager.all_imported_modules_in_file(tree):
            imp.append(id)
        if self.id != 'builtins':
            imp.append('builtins')
        
        if imp != []:
            trace('{} dependencies: {}'.format(info.path, imp))

        # Record the dependencies. Note that the dependencies list also
        # contains any superpackages and we must preserve them (e.g. os for
        # os.path).
        self.dependencies.extend(imp)
    
    void process(self):
        """Semantically analyze file and advance to the next state."""
        self.semantic_analyzer().visit_file(self.tree, self.tree.path)
        self.switch_state(SemanticallyAnalyzedFile(self.info(), self.tree))
    
    int state(self):
        return PARSED_STATE


class SemanticallyAnalyzedFile(ParsedFile):
    void process(self):
        """Type check file and advance to the next state."""
        if self.manager.target >= TYPE_CHECK:
            self.type_checker().visit_file(self.tree, self.tree.path)
        
        # FIX remove from active state list to speed up processing
        
        self.switch_state(TypeCheckedFile(self.info(), self.tree))
        
    int num_incomplete_deps(self):        
        """Return the number of dependencies that are incomplete.

        Here complete means that their state is *later* than this module.
        Cyclic dependencies are omitted to break cycles forcibly (and somewhat
        arbitrarily).
        """
        incomplete = 0
        for module in self.dependencies:
            state = self.manager.module_state(module)
            if (not earlier_state(self.state(), state) and
                    not self.manager.is_dep(module, self.id)):
                incomplete += 1
        return incomplete
    
    int state(self):
        return SEMANTICALLY_ANALYSED_STATE


class TypeCheckedFile(SemanticallyAnalyzedFile):
    void process(self):
        """Finished, so cannot process."""
        raise RuntimeError('Cannot process TypeCheckedFile')
    
    bool is_ready(self):
        """Finished, so cannot ever become ready."""
        return False
    
    int state(self):
        return TYPE_CHECKED_STATE


def trace(s):
    if debug:
        print(s)


tuple<str, str> read_module_source_from_file(str id, str[] lib_path):
    """Find and read the source file of a module.

    Return a pair (path, file contents). Return (None, None) if the module
    could not be found or read.

    Args:
      id: module name, a string of form 'foo' or 'foo.bar'
      lib_path: library search path
    """
    path = find_module(id, lib_path)
    if path is not None:
        str text
        try:
            f = open(path)
            try:
                text = f.read()
            finally:
                f.close()
        except IOError:
            return None, None
        return path, text
    else:
        return None, None


str find_module(str id, str[] lib_path):
    """Return the path of the module source file, or None if not found."""
    for pathitem in lib_path:
        comp = id.split('.')
        path = os.path.join(pathitem, os.sep.join(comp[:-1]), comp[-1] + '.py')
        str text
        if not os.path.isfile(path):
            path = os.path.join(pathitem, os.sep.join(comp), '__init__.py')
        if os.path.isfile(path) and verify_module(id, path):
            return path
    return None


bool verify_module(str id, str path):
    """Check that all packages containing id have a __init__ file."""
    if path.endswith('__init__.py'):
        path = dirname(path)
    for i in range(id.count('.')):
        path = dirname(path)
        if not os.path.isfile(os.path.join(path, '__init__.py')):
            return False
    return True


str[] super_packages(str id):
    """Return the surrounding packages of a module, e.g. ['os'] for os.path."""
    c = id.split('.')
    str[] res = []
    for i in range(1, len(c)):
        res.append('.'.join(c[:i]))
    return res
