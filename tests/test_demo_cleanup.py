"""Failed demonstrations clean up their own process instances without hiding errors."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

sdk = pytest.importorskip("camunda_orchestration_sdk")
from camunda_orchestration_sdk.errors import NotFoundError

SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cleanup_demo", SOURCE / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


@pytest.mark.parametrize("failure,cleanup_error", [
    (RuntimeError("verification failed"), None),
    (KeyboardInterrupt(), None),
    (RuntimeError("verification failed"), RuntimeError("API unavailable")),
])
def test_failed_demo_stops_worker_and_cancels_its_instance(tmp_path, monkeypatch, capsys,
                                                          failure, cleanup_error):
    client = Mock()
    client.create_process_instance.return_value = SimpleNamespace(process_instance_key="123")
    manager = MagicMock()
    manager.__enter__.return_value = client
    worker = Mock()
    events = []
    worker.terminate.side_effect = lambda: events.append("stop")

    def cancel(key, **kwargs):
        events.append(("cancel", key))
        if cleanup_error:
            raise cleanup_error

    client.cancel_process_instance.side_effect = cancel
    monkeypatch.setattr(sdk, "CamundaClient", Mock(return_value=manager))
    monkeypatch.setattr(demo.subprocess, "Popen", Mock(return_value=worker))
    monkeypatch.setattr(demo, "exercise", lambda root, invoke: invoke("demo"))
    monkeypatch.setattr(demo, "wait_for", Mock(side_effect=failure))

    with pytest.raises(type(failure)) as caught:
        demo.run_camunda(tmp_path, 99)

    assert caught.value is failure
    assert events == ["stop", ("cancel", "123")]
    worker.wait.assert_called_once_with(timeout=10)
    warning = capsys.readouterr().err
    if cleanup_error:
        assert "Could not cancel demo process 123" in warning
        assert "RuntimeError" in warning
    else:
        assert not warning


def test_cleanup_ignores_already_finished_instances(capsys):
    client = Mock()
    client.cancel_process_instance.side_effect = [
        NotFoundError(status_code=404, content=b"Already finished"), None,
    ]
    demo.cancel_demo_instances(client, ["finished", "active"])
    calls = client.cancel_process_instance.call_args_list
    assert [call.args[0] for call in calls] == ["finished", "active"]
    assert all(call.kwargs["data"].to_dict() == {} for call in calls)
    assert not capsys.readouterr().err


def test_cleanup_continues_after_an_api_failure(capsys):
    client = Mock()
    client.cancel_process_instance.side_effect = [RuntimeError("offline"), None]
    demo.cancel_demo_instances(client, ["first", "second"])
    assert [call.args[0] for call in client.cancel_process_instance.call_args_list] == ["first", "second"]
    warning = capsys.readouterr().err
    assert "Could not cancel demo process first" in warning
    assert "RuntimeError" in warning
    assert "second" not in warning
