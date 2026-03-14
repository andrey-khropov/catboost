from collections import defaultdict

import os
import pathlib
import sys

import json
import tempfile
import shutil
import subprocess
from typing import Any, Dict, List, Union

import pytest


if not (_git_repo_root := next((p for p in pathlib.Path(__file__).parents if (p / '.git').exists()), None)):
    raise RuntimeError("Git repository root not found")

_git_repo_root_dir = str(_git_repo_root.absolute())
sys.path += [
    os.path.join(_git_repo_root_dir, 'library', 'python', 'pytest'),
    os.path.join(_git_repo_root_dir, 'library', 'python', 'testing'),
    os.path.join(_git_repo_root_dir, 'library', 'python', 'testing', 'yatest_common')
]

import yatest.common

import yatest_lib.external
import yatest_lib.ya

from . import tools
from . import *


pytest_config = None


# base path -> test_name -> list of files to canonize
results_to_canonize = defaultdict(lambda: defaultdict())


def _get_canonical_test_name(item):
    class_name, test_name = tools.split_node_id(item.nodeid)
    if class_name.endswith(".py"):
        class_name = class_name[:-len(".py")]
    return f'{class_name}.{test_name}'


def get_diff_cmd_prefix(diff_tool_from_test_result):
    if diff_tool_from_test_result is None:
        if sys.platform == 'win32':
            return ['fc.exe']
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


class CanonicalProcessor(object):
    def __init__(self):
        self._canondata = {}    # tests_dir -> test_name -> canon_data_spec

    def _get_tests_dir_canondata(self, tests_dir: str) -> Dict[str, Dict[str, Any]]:
        if tests_dir not in self._canondata:
            canondata_dir = os.path.join(tests_dir, 'canondata')
            if os.path.exists(canondata_dir):
                with open(os.path.join(canondata_dir, 'result.json')) as f:
                    self._canondata[tests_dir] = json.load(f)
            else:
                self._canondata[tests_dir] = {}
        return self._canondata[tests_dir]

    @staticmethod
    def _compare_file_with_canonical(
        canondata_base_dir: str,
        result_file: yatest_lib.external.CanonicalObject,
        canondata: Dict[str, Any]
    ):
        if not isinstance(result_file, yatest_lib.external.CanonicalObject):
            base_error_msg = "Unexpected type in result"
            sys.stderr.write(base_error_msg + f': {type(result_file)}\n')
            pytest.fail(base_error_msg)
        if not isinstance(canondata, dict):
            base_error_msg = "Unexpected type in canondata element"
            sys.stderr.write(base_error_msg + f': {type(canondata)}\n')
            pytest.fail(base_error_msg)

        result_file_path = result_file['uri'][7:]
        if not os.path.exists(result_file_path):
            base_error_msg = f"Result file '{os.path.basename(result_file_path)}' does not exist"
            sys.stderr.write(base_error_msg + f"\n: Test result full path '{result_file_path}'\n")
            pytest.fail(base_error_msg)
        canonical_file_path = os.path.join(canondata_base_dir, canondata['uri'][7:])
        if not os.path.exists(canonical_file_path):
            base_error_msg = f"Canonical data for the file '{os.path.basename(canonical_file_path)}' does not exist"
            sys.stderr.write(base_error_msg + f"\n: Canonical file full path '{canonical_file_path}'\n")
            pytest.fail(base_error_msg)

        diff_cmd_prefix = get_diff_cmd_prefix(result_file.get('diff_tool', None))
        diff_cmd = diff_cmd_prefix + [canonical_file_path, result_file_path]
        if subprocess.run(diff_cmd).returncode:
            base_error_msg = f"Difference with canonical data for the file '{os.path.basename(canonical_file_path)}'"
            sys.stderr.write(
                base_error_msg + ':\n'
                + f"Canonical file full path '{canonical_file_path}':\n"
                + f"Test output full path '{result_file_path}':\n"
            )
            pytest.fail(base_error_msg)


    @staticmethod
    def _compare_with_canondata(
        canondata_base_dir: str,
        result : Union[yatest_lib.external.CanonicalObject, List[yatest_lib.external.CanonicalObject]],
        canondata : Union[Dict[str, Any], List[Dict[str, Any]]]
    ):
        if isinstance(result, list):
            if not isinstance(canondata, list):
                pytest.fail('Result is a list but canondata is not a list')
            if len(result) != len(canondata):
                pytest.fail(f"Result has length {len(result)} but canondata has length {len(canondata)}")
            for i in range(len(result)):
                CanonicalProcessor._compare_file_with_canonical(
                    canondata_base_dir,
                    result[i],
                    canondata[i],
                )
        else:
            if not isinstance(canondata, dict):
                pytest.fail('result has a single element, but canondata does not')
            CanonicalProcessor._compare_file_with_canonical(canondata_base_dir, result, canondata)


    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):  # noqa
        def get_wrapper(obj):
            def wrapper(*args, **kwargs):
                global results_to_canonize

                test_dir_name = os.path.dirname(item.path)
                canondata_for_dir = self._get_tests_dir_canondata(test_dir_name)

                canonical_test_name = _get_canonical_test_name(item)

                maybe_canondata_for_test = canondata_for_dir.get(canonical_test_name)

                res = obj(*args, **kwargs)

                if res is None:
                    if maybe_canondata_for_test is not None:
                        pytest.fail('There is saved canonical data but the test invocation returned None')
                else:
                    if maybe_canondata_for_test is None:
                        pytest.fail(
                            'Test invocation returned some data but there is no '
                            'canonical data saved for it'
                        )
                    CanonicalProcessor._compare_with_canondata(
                        os.path.join(test_dir_name, 'canondata'),
                        res,
                        maybe_canondata_for_test,
                    )

            return wrapper

        item.obj = get_wrapper(item.obj)
        yield

class MainProcessor(object):
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):  # noqa
        global pytest_config

        pytest_config.current_item_nodeid = item.nodeid
        class_name, test_name = tools.split_node_id(item.nodeid)
        test_log_path = tools.get_test_log_file_path(pytest_config.ya.output_dir, class_name, test_name)
        pytest_config.current_test_log_path = test_log_path

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
            yield
        finally:
            os.chdir(prev_cwd if prev_cwd else test_output_path)

        # delete only if test succeeded, otherwise leave for debugging
        shutil.rmtree(work_dir)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global pytest_config
    pytest_config = config
    config.ya = yatest_lib.ya.Ya(
        source_root=_git_repo_root_dir,
        build_root=os.environ['CMAKE_BINARY_DIR'],
        output_dir=os.environ['TEST_OUTPUT_DIR']
    )
    config.sanitizer_extra_checks = False
    yatest.common.runtime._set_ya_config(config=config)

    config.pluginmanager.register(
        MainProcessor()
    )
    config.pluginmanager.register(
        CanonicalProcessor()
    )
