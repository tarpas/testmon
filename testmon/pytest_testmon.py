
import os
from collections import defaultdict

import pytest
from _pytest.python import Function
from testmon.testmon_core import (
    Testmon,
    eval_environment,
    TestmonData,
    home_file,
    TestmonConfig,
)
from _pytest import runner


def serialize_report(rep):
    import py

    d = rep.__dict__.copy()
    if hasattr(rep.longrepr, "toterminal"):
        d["longrepr"] = str(rep.longrepr)
    else:
        d["longrepr"] = rep.longrepr
    for name in d:
        if isinstance(d[name], py.path.local):
            d[name] = str(d[name])
        elif name == "result":
            d[name] = None
    return d


def pytest_addoption(parser):
    group = parser.getgroup("testmon")

    group.addoption(
        "--testmon",
        action="store_true",
        dest="testmon",
        help="Select tests affected by changes (based on previously collected data) and collect + write new data "
        "(.testmondata file). Either collection or selection might be deactivated (sometimes automatically). "
        "See below.",
    )

    group.addoption(
        "--testmon-nocollect",
        action="store_true",
        dest="testmon_nocollect",
        help="Run testmon but deactivate the collection and writing of testmon data. Forced if you run under debugger "
        "or coverage.",
    )

    group.addoption(
        "--testmon-noselect",
        action="store_true",
        dest="testmon_noselect",
        help="Run testmon but deactivate selection, so all tests selected by other means will be collected and "
        "executed. Forced if you use -k, -l, -lf, test_file.py::test_name (to be implemented)",
    )

    group.addoption(
        "--testmon-forceselect",
        action="store_true",
        dest="testmon_forceselect",
        help="Run testmon and select only tests affected by changes and satisfying pytest selectors at the same time.",
    )

    group.addoption(
        "--no-testmon",
        action="store_true",
        dest="no-testmon",
        help="""
        Turn off (even if activated from config by default). Forced if neither read nor write is possible (debugger 
        plus test selector)
        """,
    )


    group.addoption(
        "--testmon-track-dir",
        action="append",
        dest="project_directory",
        help="Only files under this directory will be tracked. Can be repeated.",
        default=None,
    )

    group.addoption(
        "--testmon-env",
        action="store",
        type=str,
        dest="environment_expression",
        default="",
        help="""
        This allows you to have separate coverage data within one .testmondata file, e.g. when using the same source 
        code serving different endpoints or django settings.
        """,
    )

    parser.addini("environment_expression", "environment expression", default="")


def testmon_options(config):
    result = []
    for label in [
        "testmon",
        "no-testmon",
        "environment_expression",
        "project_directory",
    ]:
        if config.getoption(label):
            result.append(label.replace("testmon_", ""))
    return result


def init_testmon_data(config, read_source=True):
    if not hasattr(config, "testmon_data"):
        environment = eval_environment(config.getini("environment_expression"))
        config.project_dirs = config.getoption("project_directory") or [
            config.rootdir.strpath
        ]
        testmon_data = TestmonData(config.project_dirs[0], environment=environment)
        if read_source:
            testmon_data.determine_stable()
        config.testmon_data = testmon_data


def pytest_configure(config):
    coverage_stack = None

    testmon_config = TestmonConfig()
    message, should_collect, should_select = testmon_config.header_collect_select(
        config, coverage_stack
    )
    config.testmon_config = (message, should_collect, should_select)
    if should_select or should_collect:
        config.option.continue_on_collection_errors = True
        init_testmon_data(config)

        if should_select:
            config.pluginmanager.register(
                TestmonSelect(config, config.testmon_data), "TestmonSelect"
            )

        if should_collect:
            config.pluginmanager.register(
                TestmonCollect(
                    Testmon(
                        config.project_dirs, testmon_labels=testmon_options(config)
                    ),
                    config.testmon_data,
                ),
                "TestmonCollect",
            )


def pytest_report_header(config):
    message, should_collect, should_select = config.testmon_config

    if should_collect or should_select:
        if should_select:
            changed_files = ", ".join(config.testmon_data.unstable_files)
            if changed_files == "" or len(changed_files) > 100:
                changed_files = len(config.testmon_data.unstable_files)

            if changed_files == 0 and len(config.testmon_data.stable_files) == 0:
                message += "new DB, "
            else:
                message += "changed files: {}, skipping collection of {} files, ".format(
                    changed_files, len(config.testmon_data.stable_files)
                )

        if config.testmon_data.environment:
            message += "environment: {}".format(config.testmon_data.environment)

    return message


def pytest_unconfigure(config):
    if hasattr(config, "testmon_data"):
        config.testmon_data.close_connection()


def sort_items_by_duration(items, reports):
    durations = defaultdict(lambda: {"node_count": 0, "duration": 0})
    for item in items:
        if item.nodeid in reports:
            item.duration = sum(
                [report["duration"] for report in reports[item.nodeid].values()]
            )
        else:
            item.duration = 0
        item.module_name = item.location[0]
        item_hierarchy = item.location[2].split(".")
        item.node_name = item_hierarchy[-1]
        item.class_name = item_hierarchy[0]

        durations[item.class_name]["node_count"] += 1
        durations[item.class_name]["duration"] += item.duration
        durations[item.module_name]["node_count"] += 1
        durations[item.module_name]["duration"] += item.duration

    for key, stats in durations.items():
        durations[key]["avg_duration"] = stats["duration"] / stats["node_count"]

    items.sort(key=lambda item: item.duration)
    items.sort(key=lambda item: durations[item.class_name]["avg_duration"])
    items.sort(key=lambda item: durations[item.module_name]["avg_duration"])


class TestmonCollect(object):
    def __init__(self, testmon, testmon_data):
        self.testmon_data = testmon_data
        self.testmon = testmon

        self.reports = defaultdict(lambda: {})
        self.raw_nodeids = []

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_pycollect_makeitem(self, collector, name, obj):
        makeitem_result = yield
        items = makeitem_result.get_result() or []
        try:
            self.raw_nodeids.extend(
                [item.nodeid for item in items if isinstance(item, pytest.Item)]
            )
        except TypeError:
            pass

    def pytest_collection_modifyitems(self, session, config, items):
        _, should_collect, should_select = config.testmon_config
        if should_select or should_collect:
            config.testmon_data.sync_db_fs_nodes(retain=set(self.raw_nodeids))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if isinstance(item, Function) and item.config.testmon_config[1]:
            self.testmon.start()
            result = yield
            if result.excinfo and issubclass(result.excinfo[0], BaseException):
                self.testmon.stop()
            else:
                self.testmon.stop_and_save(
                    self.testmon_data,
                    item.config.rootdir.strpath,
                    item.nodeid,
                    self.reports[item.nodeid],
                )
        else:
            yield

    def pytest_runtest_logreport(self, report):
        assert report.when not in self.reports, "{} {} {}".format(
            report.nodeid, report.when, self.reports
        )
        self.reports[report.nodeid][report.when] = serialize_report(report)

    def pytest_sessionfinish(self, session):
        self.testmon_data.remove_unused_fingerprints()
        self.testmon.close()


def did_fail(reports):
    return bool(
        [True for report in reports.values() if report.get("outcome") == u"failed"]
    )


def get_failing(all_nodes):
    failing_files, failing_nodes = set(), {}
    for nodeid, result in all_nodes.items():
        if did_fail(all_nodes[nodeid]):
            failing_files.add(home_file(nodeid))
            failing_nodes[nodeid] = result
    return failing_files, failing_nodes


class TestmonSelect:
    def __init__(self, config, testmon_data):
        self.testmon_data = testmon_data
        self.config = config

        self.deselected_files = testmon_data.stable_files
        self.deselected_nodes = testmon_data.stable_nodeids

        failing_files, failing_nodes = get_failing(testmon_data.all_nodes)


        self.failing_nodes = failing_nodes

    def report_from_db(self, nodeid):
        node_reports = self.failing_nodes.get(nodeid, {})
        if node_reports:
            for phase in ("setup", "call", "teardown"):
                if phase in node_reports:
                    test_report = runner.TestReport(**node_reports[phase])
                    self.config.hook.pytest_runtest_logreport(report=test_report)

    def pytest_ignore_collect(self, path, config):
        strpath = os.path.relpath(path.strpath, config.rootdir.strpath)
        if strpath in self.deselected_files:
            return True

    @pytest.mark.trylast
    def pytest_collection_modifyitems(self, session, config, items):
        for item in items:
            assert item.nodeid not in self.deselected_files, (
                item.nodeid,
                self.deselected_files,
            )

        selected = []
        for item in items:
            if item.nodeid not in self.deselected_nodes:
                selected.append(item)
        items[:] = selected

        if self.testmon_data.all_nodes:
            sort_items_by_duration(items, self.testmon_data.all_nodes)

        session.config.hook.pytest_deselected(
            items=([FakeItemFromTestmon(session.config)] * len(self.deselected_nodes))
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtestloop(self, session):
        yield
        for nodeid in sorted(self.deselected_nodes):
            self.report_from_db(nodeid)


class FakeItemFromTestmon(object):
    def __init__(self, config):
        self.config = config
