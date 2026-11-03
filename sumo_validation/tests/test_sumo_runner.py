import os
import stat
import textwrap

from runners.sumo_runner import run_sumo


def _write_fake_sumo_bin(path):
    """A fake `sumo` executable that only succeeds if invoked with `-c <bare-filename>`
    from inside the config's own directory -- i.e. it fails exactly the way the real
    `sumo` binary fails when handed a path that gets double-resolved against an already
    chdir'd cwd. This reproduces the real bug (full/relative path passed as -c while cwd
    is already set to the config's parent dir) without needing the real SUMO binary
    installed, so the test is meaningful in any environment.
    """
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys, os
            args = sys.argv[1:]
            cfg = args[args.index("-c") + 1]
            # Only succeed when handed the bare filename that actually exists in cwd.
            if cfg != os.path.basename(cfg) or not os.path.isfile(cfg):
                sys.stderr.write(f"Error: Could not access configuration '{cfg}'.\\n")
                sys.exit(1)
            sys.exit(0)
            """
        )
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_run_sumo_invokes_with_bare_filename_relative_to_cwd(tmp_path):
    """Regression test: run_sumo must pass sumocfg's *filename* (not its full/relative
    path) as the -c argument, since it already sets cwd=sumocfg.parent. Passing the full
    path here causes SUMO to double-resolve it against the chdir'd cwd and exit 1 with
    "Could not access configuration" -- which silently leaves any pre-existing FCD output
    file on disk unrefreshed, making a stale prior run look like current ground truth.
    """
    sumo_dir = tmp_path / "nested" / "sumo"
    sumo_dir.mkdir(parents=True)
    sumocfg = sumo_dir / "scenario.sumocfg"
    sumocfg.write_text("<configuration/>")

    fake_sumo = tmp_path / "fake_sumo.py"
    _write_fake_sumo_bin(fake_sumo)

    # Pass sumocfg as a path relative to tmp_path (not sumo_dir) -- the shape run_validation.py
    # actually uses (paths built relative to the harness's own cwd, not the sumo output dir).
    relative_sumocfg = sumocfg.relative_to(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = run_sumo(
            sumocfg=relative_sumocfg,
            fcd_output=sumo_dir / "scenario_fcd.xml",
            sumo_bin=str(fake_sumo),
        )
    finally:
        os.chdir(old_cwd)

    assert result.returncode == 0, (
        f"run_sumo failed with a relative sumocfg path: stderr={result.stderr!r}"
    )


def test_run_sumo_reports_nonzero_exit_and_stderr_on_genuine_failure(tmp_path):
    """Sanity check on the other branch: run_sumo must still surface a real failure
    (e.g. a missing/invalid config) rather than swallowing it, so callers can detect it.
    """
    sumo_dir = tmp_path / "sumo"
    sumo_dir.mkdir()
    fake_sumo = tmp_path / "fake_sumo.py"
    _write_fake_sumo_bin(fake_sumo)

    result = run_sumo(
        sumocfg=sumo_dir / "does_not_exist.sumocfg",
        fcd_output=sumo_dir / "scenario_fcd.xml",
        sumo_bin=str(fake_sumo),
    )

    assert result.returncode != 0
    assert "Could not access configuration" in result.stderr
