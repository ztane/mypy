import os.path
import sys

import build
from myunit import Suite, run_test
from testconfig import test_temp_dir, test_data_prefix
from testdata import parse_test_cases
from testhelpers import assert_string_arrays_equal
from errors import CompileError
from testsemanal import normalize_error_messages


# List of files that contain test case descriptions.
files = ['check-basic.test',
         'check-generics.test',
         'check-classes.test',
         'check-expressions.test',
         'check-statements.test',
         'check-tuples.test',
         'check-inference.test',
         'check-inference-context.test',
         'check-dynamic.test',
         'check-functions.test',
         'check-varargs.test',
         'check-kwargs.test',
         'check-overloading.test',
         'check-interfaces.test',
         'check-super.test',
         'check-modules.test',
         'check-generic-subtyping.test',
         'check-unsupported.test']


class TypeCheckSuite(Suite):
    def cases(self):
        c = []
        for f in files:
            c += parse_test_cases(os.path.join(test_data_prefix, f),
                                  self.run_test, test_temp_dir, True)
        return c
    
    def run_test(self, testcase):
        a = []
        try:
            src = '\n'.join(testcase.input)
            build.build(src, 'main',
                        target=build.TYPE_CHECK,
                        test_builtins=True,
                        alt_lib_path=test_temp_dir)
        except CompileError as e:
            a = normalize_error_messages(e.messages)
        assert_string_arrays_equal(
            testcase.output, a,
            'Invalid type checker output ({}, line {})'.format(
                testcase.file, testcase.line))


if __name__ == '__main__':
    run_test(TypeCheckSuite(), sys.argv[1:])
