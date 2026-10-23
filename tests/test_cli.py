"""命令行入口测试。"""

from quality_trace.main import main


def test_cli_exits_zero_and_prints_status(tmp_path, capsys):
    config = tmp_path / "ok.yaml"
    config.write_text("app:\n  name: demo\n", encoding="utf-8")

    assert main(["--config", str(config)]) == 0

    out = capsys.readouterr().out
    assert "quality-trace" in out
    assert "ready" in out


def test_cli_reports_missing_config(tmp_path, capsys):
    exit_code = main(["--config", str(tmp_path / "missing.yaml")])

    assert exit_code == 2
    assert "找不到配置文件" in capsys.readouterr().err
