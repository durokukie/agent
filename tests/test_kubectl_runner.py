from types import SimpleNamespace

from kukie.kubectl import runner


def test_kubectl_결과는_실제_exit_code를_보존한다(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="not found\n", returncode=7
        ),
    )

    result = runner.run_kubectl(
        ["delete", "pod", "missing"], context="kind-dev"
    )

    assert result.success is False
    assert result.exit_code == 7


def test_dry_run은_yaml_원문_대신_kubectl_요약을_반환한다(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            stdout="pod/nginx configured (server dry run)\n",
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    manifest = "kind: Pod\nmetadata:\n  name: nginx\n"

    result = runner.run_kubectl(
        ["apply", "-f", "-"],
        context="kind-dev",
        dry_run=True,
        stdin=manifest,
    )

    assert calls == [
        (
            [
                "kubectl",
                "--context",
                "kind-dev",
                "apply",
                "-f",
                "-",
                "--dry-run=server",
            ],
            {
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "input": manifest,
            },
        )
    ]
    assert result.stdout == "pod/nginx configured (server dry run)\n"
