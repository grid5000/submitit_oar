# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import contextlib
import os
import signal
import subprocess
import typing as tp
from pathlib import Path
from unittest.mock import patch

import pytest
import submitit
from submitit import helpers
from submitit.core import job_environment, submission, test_core, utils
from submitit.core.core import Job

import submitit_oar
import submitit_oar.oar


class MockedSubprocess:
    """Helper for mocking subprocess calls"""

    OARSTAT_JOB = '{{"{j}" : {{"state" : "{state}"}}}}'

    def __init__(self, known_cmds: tp.Optional[tp.Sequence[str]] = None) -> None:
        self.job_oarstat: tp.Dict[str, str] = {}
        self.last_job: str = ""
        self._subprocess_check_output = subprocess.check_output
        self.known_cmds = known_cmds if known_cmds is not None else []
        self.job_count = 12

    def __call__(self, command: tp.Sequence[str], **kwargs: tp.Any) -> bytes:
        program = command[0]
        if program in ["oarstat", "oarsub", "oardel"]:
            return getattr(self, program)(command[1:]).encode()
        elif program == "tail":
            return self._subprocess_check_output(command, **kwargs)
        else:
            raise ValueError(f'Unknown command to mock "{command}".')

    def oarstat(self, _: tp.Sequence[str]) -> str:
        return "\n".join(self.job_oarstat.values())

    # pylint: disable=unused-argument
    def oarsub(self, args: tp.Sequence[str]) -> str:
        """Create a "RUNNING" job."""
        job_id = str(self.job_count)
        self.job_count += 1
        self.set_job_state(job_id, "Running", 0)
        return f"OAR_JOB_ID={job_id}\n"

    def oardel(self, _: tp.Sequence[str]) -> str:
        # TODO:should we call set_job_state ?
        return ""

    def set_job_state(self, job_id: str, state: str, array: int = 0) -> None:
        self.job_oarstat[job_id] = self._oarstat(state, job_id, array)
        self.last_job = job_id

    def _oarstat(self, state: str, job_id: str, array: int) -> str:
        if array == 0:
            lines = self.OARSTAT_JOB.format(j=job_id, state=state)
        else:
            lines = "\n".join(self.OARSTAT_JOB.format(j=f"{job_id}_{i}", state=state) for i in range(array))
        return lines

    def which(self, name: str) -> tp.Optional[str]:
        return "here" if name in self.known_cmds else None

    def mock_cmd_fn(self, *args, **_):
        # CommandFunction(cmd)() ~= subprocess.check_output(cmd)
        return lambda: self(*args)

    @contextlib.contextmanager
    def context(self) -> tp.Iterator[None]:
        with patch("submitit.core.utils.CommandFunction", new=self.mock_cmd_fn):
            with patch("subprocess.check_output", new=self):
                with patch("shutil.which", new=self.which):
                    with patch("subprocess.check_call", new=self):
                        yield None

    @contextlib.contextmanager
    def job_context(self, job_id: str) -> tp.Iterator[None]:
        with utils.environment_variables(
            _USELESS_TEST_ENV_VAR_="1", SUBMITIT_EXECUTOR="oar", OAR_JOB_ID=str(job_id)
        ):
            yield None

    @contextlib.contextmanager
    def resubmit_job_context(
        self, job_id: str, resubmit_job_id: str
    ) -> tp.Iterator[submitit_oar.oar.OarJobEnvironment]:
        with utils.environment_variables(
            _USELESS_TEST_ENV_VAR_="1",
            SUBMITIT_EXECUTOR="oar",
            OAR_JOB_ID=str(job_id),
            OAR_ARRAY_ID=str(resubmit_job_id),
        ):
            yield submitit_oar.oar.OarJobEnvironment()


def _mock_log_files(job: Job[tp.Any], prints: str = "", errors: str = "") -> None:
    """Write fake log files"""
    filepaths = [str(x).replace("%j", str(job.job_id)) for x in [job.paths.stdout, job.paths.stderr]]
    for filepath, msg in zip(filepaths, (prints, errors)):
        with Path(filepath).open("w") as f:
            f.write(msg)


@contextlib.contextmanager
def mocked_oar() -> tp.Iterator[MockedSubprocess]:
    mock = MockedSubprocess(known_cmds=["oarsub"])
    try:
        with mock.context(), patch("submitit_oar.oar.OarJob._get_resubmitted_job") as resubmitted_job:
            resubmitted_job.return_value = None
            yield mock
    finally:
        # Clear the state of the shared watcher
        submitit_oar.oar.OarJob.watcher.clear()


def test_mocked_missing_state(tmp_path: Path) -> None:
    with mocked_oar() as mock:
        mock.set_job_state("12", "")
        job: submitit_oar.oar.OarJob[None] = submitit_oar.oar.OarJob(tmp_path, "12")
        assert job.state == "UNKNOWN"
        job._interrupt(timeout=False)  # check_call is bypassed by MockedSubprocess


def test_job_environment() -> None:
    with mocked_oar() as mock:
        mock.set_job_state("12", "Running")
        with mock.job_context("12"):
            assert job_environment.JobEnvironment().cluster == "oar"


def test_oar_job_mocked(tmp_path: Path) -> None:
    with mocked_oar() as mock:
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        job = executor.submit(test_core.do_nothing, 1, 2, blublu=3)
        # First mock job always have id 12
        assert job.job_id == "12"
        assert job.state == "RUNNING"
        assert job.stdout() is None
        _mock_log_files(job, errors="This is the error log\n", prints="hop")
        job._results_timeout_s = 0
        with pytest.raises(utils.UncompletedJobError):
            job._get_outcome_and_result()
        _mock_log_files(job, errors="This is the error log\n", prints="hop")

        with mock.job_context(job.job_id):
            submission.process_job(job.paths.folder)
        assert job.result() == 12
        # logs
        assert job.stdout() == "hop"
        assert job.stderr() == "This is the error log\n"
        assert "_USELESS_TEST_ENV_VAR_" not in os.environ, "Test context manager seems to be failing"


# pylint: disable=too-many-locals
@pytest.mark.parametrize("use_batch_api", (False, True))  # type: ignore
def test_oar_job_array_mocked(use_batch_api: bool, tmp_path: Path) -> None:
    n = 5
    with mocked_oar() as mock:
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        data1, data2 = range(n), range(10, 10 + n)

        def add(x: int, y: int) -> int:
            assert x in data1
            assert y in data2
            return x + y

        jobs: tp.List[Job[int]] = []
        with patch("submitit_oar.oar.OarExecutor._get_job_id_list_from_array_id") as mock_get_job_id_list:
            mock_get_job_id_list.return_value = ["12", "13", "14", "15", "16"]

            if use_batch_api:
                with executor.batch():
                    for d1, d2 in zip(data1, data2):
                        jobs.append(executor.submit(add, d1, d2))
            else:
                jobs = executor.map_array(add, data1, data2)

        array_id = jobs[0].job_id
        assert mock_get_job_id_list.return_value == [j.job_id for j in jobs]

        for job in jobs:
            with mock.job_context(job.job_id):
                submission.process_job(job.paths.folder)
        # trying a oar specific method
        jobs[0]._interrupt(timeout=True)  # type: ignore
        assert list(map(add, data1, data2)) == [j.result() for j in jobs]
        # check submission file
        oarsub = Job(tmp_path, job_id=array_id).paths.submission_file.read_text()
        array_line = [l.strip() for l in oarsub.splitlines() if "--array" in l]
        assert array_line == ["#OAR --array 5"]


def test_get_job_id_list_from_array_id(tmp_path: Path) -> None:
    with mocked_oar() as mock:
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        job = executor.submit(test_core.do_nothing, 1, 2, error=12)
        with mock.job_context(job.job_id):
            oar_output_dict = {"12": {"state": "Running"}}
            with patch("submitit_oar.oar.OarInfoWatcher.read_info") as mock_read_info:
                mock_read_info.return_value = oar_output_dict
            result = executor._get_job_id_list_from_array_id(array_id="12")
            assert result == ["12"]
            with patch("submitit_oar.oar.subprocess.check_output") as mock_check_output:
                mock_check_output.side_effect = subprocess.CalledProcessError(
                    returncode=1, cmd="oarstat --array 12 -J"
                )
                try:
                    executor._get_job_id_list_from_array_id(array_id="12")
                except Exception as e:
                    assert isinstance(e, subprocess.CalledProcessError)
                else:
                    pytest.fail("Expected an exception but none was raised")


def test_get_job_id_from_submission_command(tmp_path: Path) -> None:
    with mocked_oar() as mock:
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        job = executor.submit(test_core.do_nothing, 1, 2, error=12)
        with mock.job_context(job.job_id):
            oarsub_output = "OAR_JOB_ID=123456"
            result = executor._get_job_id_from_submission_command(oarsub_output)
        assert result == "123456"


def test_get_job_id_from_submission_command_failure(tmp_path: Path) -> None:
    with mocked_oar() as mock:
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        job = executor.submit(test_core.do_nothing, 1, 2, error=12)
        with mock.job_context(job.job_id):
            oarsub_output = "Invalid output\n"
            with pytest.raises(utils.FailedSubmissionError):
                executor._get_job_id_from_submission_command(oarsub_output)


def test_oar_error_mocked(tmp_path: Path) -> None:
    with mocked_oar() as mock:
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        executor.update_parameters(walltime="0:0:5", queue="default")  # just to cover the function
        job = executor.submit(test_core.do_nothing, 1, 2, error=12)
        with mock.job_context(job.job_id):
            with pytest.raises(ValueError):
                submission.process_job(job.paths.folder)
        _mock_log_files(job, errors="This is the error log\n")
        with pytest.raises(utils.FailedJobError):
            job.result()
        exception = job.exception()
        assert isinstance(exception, utils.FailedJobError)


@contextlib.contextmanager
def mock_requeue(called_with: tp.Optional[int] = None, not_called: bool = False):
    assert not_called or called_with is not None
    requeue = patch("submitit_oar.oar.OarJobEnvironment._requeue", return_value=None)
    with requeue as _patch:
        try:
            yield
        finally:
            if not_called:
                _patch.assert_not_called()
            else:
                _patch.assert_called_with(called_with)


def get_signal_handler(job: Job) -> job_environment.SignalHandler:
    env = submitit_oar.oar.OarJobEnvironment()
    delayed = utils.DelayedSubmission.load(job.paths.submitted_pickle)
    sig = job_environment.SignalHandler(env, job.paths, delayed)
    return sig


def test_requeuing_checkpointable(tmp_path: Path, fast_forward_clock: tp.Callable[..., None]) -> None:
    usr_sig = submitit.JobEnvironment._usr_sig()
    fs0 = helpers.FunctionSequence()
    fs0.add(test_core._three_time, 10)
    assert isinstance(fs0, helpers.Checkpointable)

    # Start job with a 60 minutes timeout
    with mocked_oar():
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path, max_num_timeout=1)
        executor.update_parameters(walltime="1:0:0")
        job = executor.submit(fs0)
    # If the function is checkpointed, the OAR Job type should be set to idempotent,
    # in this way, the checkpointed job will be resubmitted automatically by OAR.
    text = job.paths.submission_file.read_text()
    idempotent_lines = [x for x in text.splitlines() if "#OAR -t idempotent" in x]
    assert len(idempotent_lines) == 1, f"Unexpected lines: {idempotent_lines}"

    sig = get_signal_handler(job)

    fast_forward_clock(minutes=30)
    # Preempt the job after 30 minutes
    with pytest.raises(SystemExit), mock_requeue(called_with=1):
        sig.checkpoint_and_try_requeue(usr_sig)

    # Resubmit the job
    sig = get_signal_handler(job)
    fast_forward_clock(minutes=50)

    with mocked_oar() as mock:
        mock.set_job_state("12", "Terminated")
        mock.set_job_state("13", "Running")
        with mock.resubmit_job_context("13", "12") as env:
            assert env.array_job_id == "12"
            submitted_pkl_12 = tmp_path / "12_submitted.pkl"
            assert submitted_pkl_12.exists()
            submitted_pkl_13 = tmp_path / "13_submitted.pkl"
            assert not submitted_pkl_13.exists()
            _mock_log_files(job, errors="This is the error log\n", prints="hop")
            assert job._get_logs_string(name="stdout") == "hop"

    # The job 12 is terminated, but the resubmitted job 13 is running
    # The job should not be done
    assert job.done(force_check=False) is False

    # This time the job as timed out,
    # but we have max_num_timeout=1, so we should requeue.
    # We are a little bit under the requested timedout, but close enough
    # to not consider this a preemption
    with pytest.raises(SystemExit), mock_requeue(called_with=0):
        sig.checkpoint_and_try_requeue(usr_sig)

    # Resubmit the job
    sig = get_signal_handler(job)
    fast_forward_clock(minutes=55)

    # The job has already timed out twice, we should stop here.
    usr_sig = submitit_oar.oar.OarJobEnvironment._usr_sig()
    with mock_requeue(not_called=True), pytest.raises(
        utils.UncompletedJobError, match="timed-out too many times."
    ):
        sig.checkpoint_and_try_requeue(usr_sig)
        # This time the job should be done
        assert job.done(force_check=False) is True


def test_requeuing_not_checkpointable(tmp_path: Path, fast_forward_clock: tp.Callable[..., None]) -> None:
    usr_sig = submitit.JobEnvironment._usr_sig()
    # Start job with a 60 minutes timeout
    with mocked_oar():
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path, max_num_timeout=1)
        executor.update_parameters(walltime="1:0:0")
        job = executor.submit(test_core._three_time, 10)
    # If the function is not checkpointed, the OAR Job type should not be set to idempotent
    text = job.paths.submission_file.read_text()
    idempotent_lines = [x for x in text.splitlines() if "#OAR -t idempotent" in x]
    assert len(idempotent_lines) == 0, f"Unexpected lines: {idempotent_lines}"

    # simulate job start
    sig = get_signal_handler(job)
    fast_forward_clock(minutes=30)

    with mock_requeue(not_called=True):
        sig.bypass(signal.Signals.SIGTERM)

    # Preempt the job after 30 minutes, the job hasn't timeout.
    with pytest.raises(SystemExit), mock_requeue(called_with=1):
        sig.checkpoint_and_try_requeue(usr_sig)

    # Restart the job from scratch
    sig = get_signal_handler(job)
    fast_forward_clock(minutes=50)

    # Wait 50 minutes, now the job as timed out.
    with mock_requeue(not_called=True), pytest.raises(
        utils.UncompletedJobError, match="timed-out and not checkpointable."
    ):
        sig.checkpoint_and_try_requeue(usr_sig)


def test_checkpoint_and_exit(tmp_path: Path) -> None:
    usr_sig = submitit.JobEnvironment._usr_sig()
    with mocked_oar():
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path, max_num_timeout=1)
        executor.update_parameters(walltime="1:0:0")
        job = executor.submit(test_core._three_time, 10)

    sig = get_signal_handler(job)
    with pytest.raises(SystemExit), mock_requeue(not_called=True):
        sig.checkpoint_and_exit(usr_sig)

    # checkpoint_and_exit doesn't modify timeout counters.
    delayed = utils.DelayedSubmission.load(job.paths.submitted_pickle)
    assert delayed._timeout_countdown == 1


def test_need_checkpointable_executor(tmp_path: Path) -> None:
    with mocked_oar():
        fs0 = helpers.FunctionSequence()
        fs0.add(test_core._three_time, 10)

        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
        delayed = utils.DelayedSubmission(fs0)

        assert isinstance(fs0, helpers.Checkpointable)
        assert executor._need_checkpointable_executor(delayed) is True


def test_num_tasks(tmp_path: Path) -> None:
    with mocked_oar():
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path, max_num_timeout=1)
        executor.update_parameters(walltime="1:0:0")
        job = executor.submit(test_core._three_time, 10)

        assert executor._num_tasks() == 1
        if len(job._tasks) > 1:
            raise NotImplementedError


def test_stderr_to_stdout(tmp_path: Path) -> None:
    with mocked_oar():
        executor = submitit.AutoExecutor(folder=tmp_path, cluster="oar")
        executor.update_parameters(stderr_to_stdout=True)
        job = executor.submit(test_core.do_nothing, 1, 2, blublu=3)
    text = job.paths.submission_file.read_text()
    stdout_lines = [x for x in text.splitlines() if "#OAR -O" in x]
    assert stdout_lines[0].endswith(".out")
    stderr_lines = [x for x in text.splitlines() if "#OAR -E" in x]
    assert stderr_lines[0].endswith(".out")
    assert not stderr_lines[0].endswith(".err")


def test_as_oar_flag(tmp_path: Path) -> None:
    assert submitit_oar.oar._as_oar_flag("queue", "production") == "#OAR --queue production"
    assert submitit_oar.oar._as_oar_flag("n", "submitit") == "#OAR -n submitit"
    with mocked_oar():
        executor = submitit.AutoExecutor(folder=tmp_path, cluster="oar")
        executor.update_parameters(oar_additional_parameters=dict({"queue": "production", "n": "submitit"}))
        job = executor.submit(test_core.do_nothing, 1, 2, blublu=3)
    text = job.paths.submission_file.read_text()
    stdout_lines = [x for x in text.splitlines() if "#OAR --queue production" in x]
    assert len(stdout_lines) == 1
    stdout_lines = [x for x in text.splitlines() if "#OAR -n submitit" in x]
    assert len(stdout_lines) == 1


def test_make_oarsub_string() -> None:
    string = submitit_oar.oar._make_oarsub_string(
        command="blublu bar",
        folder="/tmp",
        queue="default",
        additional_parameters=dict({"t": ["besteffort", "idempotent"], "p": "'chetemi AND memcore>=3337'"}),
    )
    assert "q" in string
    assert "-t besteffort -t idempotent" in string
    assert "-p 'chetemi AND memcore>=3337'" in string
    assert "nodes" not in string
    assert "gpu" not in string
    assert "--command" not in string
    record_file = Path(__file__).parent / "_oarsub_test_record.txt"
    if not record_file.exists():
        record_file.write_text(string)
    recorded = record_file.read_text()
    changes = []
    for k, (line1, line2) in enumerate(zip(string.splitlines(), recorded.splitlines())):
        if line1 != line2:
            changes.append(f'line #{k + 1}: "{line2}" -> "{line1}"')
    if changes:
        print(string)
        print("# # # # #")
        print(recorded)
        message = ["Difference with reference file:"] + changes
        message += ["", "Delete the record file if this is normal:", f"rm {record_file}"]
        raise AssertionError("\n".join(message))


def test_make_oarsub_string_gpu() -> None:
    string = submitit_oar.oar._make_oarsub_string(command="blublu", folder="/tmp", gpu=2)
    assert "-l /gpu=2" in string


def test_make_oarsub_string_core() -> None:
    string = submitit_oar.oar._make_oarsub_string(command="blublu", folder="/tmp", cores=2)
    assert "-l /core=2" in string


def test_make_oarsub_string_gpu_and_nodes() -> None:
    string = submitit_oar.oar._make_oarsub_string(command="blublu", folder="/tmp", gpu=2, nodes=1)
    assert "-l /nodes=1/gpu=2" in string


def test_make_oarsub_string_cores_and_nodes() -> None:
    string = submitit_oar.oar._make_oarsub_string(command="blublu", folder="/tmp", cores=2, nodes=1)
    assert "-l /nodes=1/core=2" in string


def test_make_oarsub_string_cores_gpu_and_nodes() -> None:
    string = submitit_oar.oar._make_oarsub_string(command="blublu", folder="/tmp", gpu=2, nodes=1, cores=4)
    assert "-l /nodes=1/gpu=2/core=4" in string


def test_update_parameters(tmp_path: Path) -> None:
    with mocked_oar():
        executor = submitit.AutoExecutor(folder=tmp_path, cluster="oar")
    executor.update_parameters(oar_walltime="2:0:0")
    assert executor._executor.parameters["walltime"] == "2:0:0"


def test_update_parameters_error(tmp_path: Path) -> None:
    with mocked_oar():
        executor = submitit_oar.oar.OarExecutor(folder=tmp_path)
    with pytest.raises(ValueError):
        executor.update_parameters(blublu=12)


def test_make_command() -> None:
    watcher = submitit_oar.oar.OarInfoWatcher()
    watcher._registered = {"1", "2", "3"}
    watcher._finished = {"2", "4"}
    result = watcher._make_command()
    assert result == ["oarstat", "-f", "-J", "-j", "1", "-j", "3"]


def test_make_command_empty_to_check() -> None:
    watcher = submitit_oar.oar.OarInfoWatcher()
    watcher._registered = {"1", "2"}
    watcher._finished = {"1", "2"}
    result = watcher._make_command()
    assert result is None


def test_read_info() -> None:
    example = """{
        "1924697" : {
            "state" : "Running"
        }
    }"""
    output = submitit_oar.oar.OarInfoWatcher().read_info(example)
    assert output["1924697"] == {"JobID": "1924697", "NodeList": None, "State": "RUNNING"}


def test_read_info_empty() -> None:
    example = ""
    output = submitit_oar.oar.OarInfoWatcher().read_info(example)
    assert not output


def test_get_state() -> None:
    with mocked_oar() as mock:
        mock.set_job_state("12", "Running")
        state = submitit_oar.oar.OarInfoWatcher().get_state(job_id="12")
        assert state == "RUNNING"


def test_watcher() -> None:
    with mocked_oar() as mock:
        watcher = submitit_oar.oar.OarInfoWatcher()
        mock.set_job_state("12", "Running")
        assert watcher.num_calls == 0
        state = watcher.get_state(job_id="11")
        assert set(watcher._info_dict.keys()) == {"12"}
        assert watcher._registered == {"11"}

        assert state == "UNKNOWN"
        mock.set_job_state("12", "Error")
        state = watcher.get_state(job_id="12", mode="force")
        assert state == "FAILED"
        # TODO: this test is implementation specific. Not sure if we can rewrite it another way.
        assert watcher._registered == {"11", "12"}
        assert watcher._finished == {"12"}


def test_get_default_parameters() -> None:
    defaults = submitit_oar.oar._get_default_parameters()
    assert defaults["n"] == "submitit"


def test_name() -> None:
    assert submitit_oar.oar.OarExecutor.name() == "oar"


@contextlib.contextmanager
def with_oar_nodefile(node_list: str) -> tp.Iterator[submitit_oar.oar.OarJobEnvironment]:
    node_file_path = Path(__file__).parent / "_oar_node_file.txt"
    _mock_oar_node_file(node_file_path, node_list)
    os.environ["OAR_JOB_ID"] = "1"
    os.environ["OAR_NODEFILE"] = str(Path.joinpath(node_file_path))
    yield submitit_oar.oar.OarJobEnvironment()
    del os.environ["OAR_NODEFILE"]
    del os.environ["OAR_JOB_ID"]


def _mock_oar_node_file(node_file_path, node_list: str) -> None:
    """Write fake oar node file"""
    with open(node_file_path, "w+") as file:
        file.write(node_list)


def test_hostnames() -> None:
    with with_oar_nodefile("chetemi-7.lille.grid5000.fr\n") as env:
        assert env.hostnames == ["chetemi-7.lille.grid5000.fr"]
    with with_oar_nodefile(
        "chetemi-8.lille.grid5000.fr\nchetemi-8.lille.grid5000.fr\nchetemi-7.lille.grid5000.fr\nchetemi-7.lille.grid5000.fr\n"
    ) as env:
        assert ["chetemi-7.lille.grid5000.fr", "chetemi-8.lille.grid5000.fr"] == env.hostnames


def test_num_nodes() -> None:
    with with_oar_nodefile("chetemi-7.lille.grid5000.fr\n") as env:
        assert env.num_nodes == 1
    with with_oar_nodefile(
        "chetemi-8.lille.grid5000.fr\nchetemi-8.lille.grid5000.fr\nchetemi-7.lille.grid5000.fr\nchetemi-7.lille.grid5000.fr\n"
    ) as env:
        assert env.num_nodes == 2


def test_array_task_id() -> None:
    with with_oar_nodefile("chetemi-7.lille.grid5000.fr\n") as env:
        assert env.array_task_id is None


def test_node() -> None:
    with with_oar_nodefile("chetemi-7.lille.grid5000.fr\n") as env:
        assert env.node == 0


@contextlib.contextmanager
def with_oar_environment(
    raw_job_id: str, array_job_id: str, array_task_id: str
) -> tp.Iterator[submitit_oar.oar.OarJobEnvironment]:
    os.environ["OAR_JOB_ID"] = raw_job_id
    os.environ["OAR_ARRAY_ID"] = array_job_id
    os.environ["OAR_ARRAY_INDEX"] = array_task_id
    yield submitit_oar.oar.OarJobEnvironment()
    del os.environ["OAR_ARRAY_INDEX"]
    del os.environ["OAR_ARRAY_ID"]
    del os.environ["OAR_JOB_ID"]


def test_paths() -> None:
    with with_oar_environment("1", "1", "0") as env:
        paths = env.paths
        assert paths.job_id == "1"
    with with_oar_environment("2", "1", "0") as env:
        paths = env.paths
        assert paths.job_id == "2"


@pytest.mark.parametrize("params", [{}, {"timeout_min": None}])  # type: ignore
def test_oar_through_auto(params: tp.Dict[str, int], tmp_path: Path) -> None:
    with mocked_oar():
        executor = submitit.AutoExecutor(folder=tmp_path, cluster="oar")
        executor.update_parameters(**params, oar_additional_parameters={"t": "besteffort"})
        job = executor.submit(test_core.do_nothing, 1, 2, blublu=3)
    text = job.paths.submission_file.read_text()
    best_effort_lines = [x for x in text.splitlines() if "#OAR -t besteffort" in x]
    assert len(best_effort_lines) == 1, f"Unexpected lines: {best_effort_lines}"


def test_timeout_min_to_oar_walltime(tmp_path: Path) -> None:
    assert submitit_oar.oar._timeout_min_to_oar_walltime(90) == "01:30"
    with mocked_oar():
        executor = submitit.AutoExecutor(folder=tmp_path, cluster="oar")
        executor.update_parameters(timeout_min=90)
        job = executor.submit(test_core.do_nothing, 1, 2, blublu=3)
    text = job.paths.submission_file.read_text()
    walltime_lines = [x for x in text.splitlines() if "#OAR -l walltime=01:30" in x]
    assert len(walltime_lines) == 1, f"Unexpected lines: {walltime_lines}"


def test_oar_walltime_to_timeout_min() -> None:
    assert submitit_oar.oar._oar_walltime_to_timeout_min("1:30:00") == 90


def test_oar_walltime_wins_over_timeout_min(tmp_path: Path) -> None:
    with mocked_oar():
        executor = submitit.AutoExecutor(folder=tmp_path, cluster="oar")
        executor.update_parameters(timeout_min=90, oar_walltime="2:0:0")
        job = executor.submit(test_core.do_nothing, 1, 2, blublu=3)
    text = job.paths.submission_file.read_text()
    walltime_lines = [x for x in text.splitlines() if "#OAR -l walltime=2:0:0" in x]
    assert len(walltime_lines) == 1, f"Unexpected lines: {walltime_lines}"


def test_get_cancel_command_without_resubmitted_job(tmp_path: Path) -> None:
    job: submitit_oar.oar.OarJob[None] = submitit_oar.oar.OarJob(tmp_path, "12")
    command = job._get_cancel_command()
    assert command == ["oardel"]


def test_get_cancel_command_with_resubmitted_job(tmp_path: Path) -> None:
    job: submitit_oar.oar.OarJob[None] = submitit_oar.oar.OarJob(tmp_path, "12")
    job._resubmitted_job = submitit_oar.oar.OarJob(tmp_path, "11")
    command = job._get_cancel_command()
    assert command == ["oardel", "--array"]
