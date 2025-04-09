from collections import defaultdict

import os
import re
import sys

import tempfile
import shutil
import subprocess

import pytest

sys.path += [
    os.path.join(os.environ['CMAKE_SOURCE_DIR'], 'library', 'python', 'pytest'),
    os.path.join(os.environ['CMAKE_SOURCE_DIR'], 'library', 'python', 'testing'),
    os.path.join(os.environ['CMAKE_SOURCE_DIR'], 'library', 'python', 'testing', 'yatest_common')
]

import yatest.common

import yatest_lib.ya

from . import tools
from . import *


pytest_config = None


# base path -> test_name -> list of files to canonize
results_to_canonize = defaultdict(lambda: defaultdict())


def get_canonical_name(item):
    class_name, test_name = tools.split_node_id(item.nodeid)
    filename = "{}.{}".format(class_name.split('.')[0], test_name)
    if not filename:
        filename = "test"
    not_allowed_pattern = r"[\[\]:,]"
    filename = re.sub(not_allowed_pattern, "_", filename)
    filename = tools.normalize_filename(filename)
    return filename


def get_diff_cmd_prefix(diff_tool_from_test_result):
    if diff_tool_from_test_result is None:
        if sys.platform == 'win32':
            return ['fc.exe', '/b']
        else:
            return ['diff', '-b']
    else:
        # yatest's internal mechanisms cut build dir prefix, restore it
        diff_tool_exec_path_with_build_root = os.path.join(
            os.environ['CMAKE_BINARY_DIR'],
            diff_tool_from_test_result[0]
        )
        if os.path.exists(diff_tool_exec_path_with_build_root):
            return [diff_tool_exec_path_with_build_root] + diff_tool_from_test_result[1:]
        return diff_tool_from_test_result


# return file -> {full_path, diff_cmd_prefix}
def get_files_to_check(test_result):
    if test_result is None:
        test_result = []
    elif not isinstance(test_result, list):
        test_result = [test_result]

    result = {}
    for e in test_result:
        full_path = e['uri'][7:]
        result[os.path.basename(full_path)] = {
            'full_path': full_path,
            'diff_cmd_prefix': get_diff_cmd_prefix(e.get('diff_tool', None))
        }

    return result


class CanonicalProcessor(object):
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):  # noqa
        def get_wrapper(obj):
            def wrapper(*args, **kwargs):
                global results_to_canonize

                test_dir_name = os.path.dirname(item.path)
                all_tests_canondata_dir = os.path.join(test_dir_name)
                canonical_name = get_canonical_name(item)
                test_canondata_dir = os.path.join(all_tests_canondata_dir, 'canondata', canonical_name)

                res = obj(*args, **kwargs)
                files_to_check = get_files_to_check(res)

                results_to_canonize[test_dir_name][canonical_name] = files_to_check

                for fname, spec in files_to_check.items():
                    canonical_full_path = os.path.join(test_canondata_dir, fname)
                    if not os.path.exists(canonical_full_path):
                        base_error_msg = f"Canonical data for the file '{fname}' does not exist"
                        sys.stderr.write(
                            base_error_msg + ':\n'
                            + f"Canonical file full path '{canonical_full_path}':\n"
                        )
                        pytest.fail(base_error_msg)

                    diff_cmd = spec['diff_cmd_prefix'] + [canonical_full_path, spec['full_path']]

                    if subprocess.run(diff_cmd).returncode:
                        base_error_msg = f"Difference with canonical data for the file '{fname}'"
                        sys.stderr.write(
                            base_error_msg + ':\n'
                            + f"Canonical file full path '{canonical_full_path}':\n"
                            + f"Test output full path '{spec['full_path']}':\n"
                        )
                        pytest.fail(base_error_msg)

            return wrapper

        item.obj = get_wrapper(item.obj)
        yield

class WorkdirProcessor(object):
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):  # noqa
        def get_wrapper(obj):
            def wrapper(*args, **kwargs):
                test_output_path = yatest.common.test_output_path()
                # TODO: have to create in standard tmp dir because of max path length issues on Windows
                work_dir = tempfile.mkdtemp(prefix='work_dir_')
                prev_cwd = None
                try:
                    prev_cwd = os.getcwd()
                except Exception:
                    pass
                os.chdir(work_dir)
                try:
                    obj(*args, **kwargs)
                finally:
                    os.chdir(prev_cwd if prev_cwd else test_output_path)

                # delete only if test succeeded, otherwise leave for debugging
                shutil.rmtree(work_dir)

            return wrapper

        item.obj = get_wrapper(item.obj)
        yield


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global pytest_config
    pytest_config = config
    config.ya = yatest_lib.ya.Ya(
        source_root=os.environ['CMAKE_SOURCE_DIR'],
        build_root=os.environ['CMAKE_BINARY_DIR'],
        output_dir=os.environ['TEST_OUTPUT_DIR']
    )
    config.sanitizer_extra_checks = False
    yatest.common.runtime._set_ya_config(config=config)

    config.pluginmanager.register(
        WorkdirProcessor()
    )
    config.pluginmanager.register(
        CanonicalProcessor()
    )


def pytest_runtest_setup(item):
    pytest_config.current_item_nodeid = item.nodeid
    class_name, test_name = tools.split_node_id(item.nodeid)
    test_log_path = tools.get_test_log_file_path(pytest_config.ya.output_dir, class_name, test_name)
    pytest_config.current_test_log_path = test_log_path
