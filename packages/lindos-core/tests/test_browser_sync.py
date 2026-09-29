"""lindos.browsers.sync_default / 'lindos-browser sync-default': the login-time step that makes the browser chosen
in Lindos Setup XFCE's preferred one once it is installed.

Review finding: with Chrome pending at Setup time (offline install) Setup leaves the personal default alone and the
silent retry later writes only /etc/xdg/mimeapps.list - but XFCE's own preferred web browser (exo-open, the browser
key, the menu's web search) reads xfce4/helpers.rc, whose system-wide copy says Firefox, so those kept opening
Firefox although Setup had promised Chrome.  Hermetic: LINDOS_ROOT / LINDOS_HOME, a fake kernel command line, no
real dpkg or xdg tools.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from lindos import browsers, config as lconfig, paths, session as lsession

INSTALLED_CMDLINE = "BOOT_IMAGE=/boot/vmlinuz-6.14.0-lindos root=UUID=1234 ro quiet splash"
LIVE_CMDLINE = "BOOT_IMAGE=/casper/vmlinuz boot=casper username=liveuser quiet splash --"


class Env:
    """A user session: config, Setup marker, install-state, installed browsers - all under the scratch dirs."""

    def __init__(self, core_env: Dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        self.core = core_env
        self.mp = monkeypatch
        self.installed: set = set()
        self.set_default_calls: List[str] = []
        self.cmdline(INSTALLED_CMDLINE)
        monkeypatch.setattr(lsession, "is_oem_temp_user", lambda user=None: False)
        monkeypatch.setattr(browsers, "is_installed", lambda bid: bid in self.installed)
        monkeypatch.setattr(browsers.shutil, "which", lambda name, *a, **k: None)      # no xdg tools, no dpkg

    # -- the session ---------------------------------------------------------------------------------------
    def cmdline(self, text: str) -> None:
        path = self.core["tmp"] / "cmdline"
        path.write_text(text, encoding="utf-8")
        self.mp.setenv(lsession.CMDLINE_ENV, str(path))

    def setup_done(self, done: bool = True) -> None:
        marker = Path(paths.setup_done())
        marker.parent.mkdir(parents=True, exist_ok=True)
        if done:
            marker.write_text("", encoding="utf-8")
        elif marker.exists():
            marker.unlink()

    def choose(self, bid: str) -> None:
        cfg = lconfig.Config.load()
        cfg["browser"] = bid
        cfg.save()

    def install_state(self, status: str) -> None:
        path = Path(paths.install_state())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "updated": "t", "online": False, "steps": {
            "browser": {"status": status, "detail": "", "time": "t"}}}), encoding="utf-8")

    def fake_set_default(self, ok: bool = True) -> None:
        def _set(bid: str) -> bool:
            self.set_default_calls.append(bid)
            return ok
        self.mp.setattr(browsers, "set_default", _set)

    # -- the user's own files ------------------------------------------------------------------------------
    @property
    def helpers_rc(self) -> Path:
        return Path(paths.resolve("~/.config/xfce4/helpers.rc"))

    @property
    def user_mimeapps(self) -> Path:
        return Path(paths.resolve("~/.config/mimeapps.list"))

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture()
def env(core_env: Dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(core_env, monkeypatch)


# --- the promise: Chrome lands later, the XFCE default follows ----------------------------------------------------
def test_chrome_that_lands_after_setup_becomes_the_xfce_default_for_the_user(env: Env) -> None:
    """The reviewed scenario end to end, with the real set_default writing the user's files."""
    env.setup_done()
    env.choose("chrome")
    env.install_state("pending")
    # Setup left everything alone; the silent retry has not run yet
    assert browsers.sync_default() == browsers.SYNC_PENDING
    assert not env.helpers_rc.exists()
    # ... the retry installs Chrome (dpkg says so)
    env.installed.add("chrome")
    assert browsers.sync_default() == browsers.SYNC_APPLIED
    text = env.helpers_rc.read_text(encoding="utf-8")
    assert "WebBrowser=google-chrome" in text.splitlines()
    helper = env.core["home"] / ".local" / "share" / "xfce4" / "helpers" / "google-chrome.desktop"
    assert helper.is_file() and "X-XFCE-Category=WebBrowser" in helper.read_text(encoding="utf-8")
    assert lconfig.Config.load()["browser"] == "chrome"
    # a second login finds it done
    assert browsers.sync_default() == browsers.SYNC_ALREADY


def test_other_keys_of_the_users_helpers_rc_survive(env: Env) -> None:
    env.setup_done()
    env.installed.add("chrome")
    env.write(env.helpers_rc, "MailReader=thunderbird\nTerminalEmulator=xfce4-terminal\n")
    assert browsers.sync_default() == browsers.SYNC_APPLIED
    lines = env.helpers_rc.read_text(encoding="utf-8").splitlines()
    assert "MailReader=thunderbird" in lines and "TerminalEmulator=xfce4-terminal" in lines
    assert "WebBrowser=google-chrome" in lines


def test_the_step_calls_set_default_with_the_chosen_browser(env: Env) -> None:
    env.fake_set_default()
    env.setup_done()
    env.choose("edge")
    env.installed.add("edge")
    assert browsers.sync_default() == browsers.SYNC_APPLIED
    assert env.set_default_calls == ["edge"]
    env.set_default_calls.clear()
    env.fake_set_default(ok=False)              # a tool complained: still done, but honest about it
    assert browsers.sync_default() == browsers.SYNC_PARTIAL


# --- never over a personal choice -----------------------------------------------------------------------------
@pytest.mark.parametrize("helpers_rc", ["WebBrowser=firefox\n", "MailReader=x\nWebBrowser=microsoft-edge\n"])
def test_a_personal_xfce_choice_is_left_alone(env: Env, helpers_rc: str) -> None:
    env.fake_set_default()
    env.setup_done()
    env.installed.add("chrome")
    env.write(env.helpers_rc, helpers_rc)
    assert browsers.sync_default() == browsers.SYNC_PERSONAL
    assert env.set_default_calls == [] and env.helpers_rc.read_text(encoding="utf-8") == helpers_rc


def test_a_personal_choice_needs_no_waiting_for_the_install(env: Env) -> None:
    env.fake_set_default()
    env.setup_done()
    env.install_state("pending")
    env.write(env.helpers_rc, "WebBrowser=firefox\n")
    sleeps: List[float] = []
    assert browsers.sync_default(wait=600, sleep=sleeps.append) == browsers.SYNC_PERSONAL
    assert sleeps == []


@pytest.mark.parametrize("mimeapps", [
    "[Default Applications]\nx-scheme-handler/https=firefox.desktop\n",
    "[Default Applications]\nx-scheme-handler/http=firefox.desktop;google-chrome.desktop;\n",
    "[Added Associations]\nx-scheme-handler/http=chrome.desktop\n[Default Applications]\nx-scheme-handler/https=org.gnome.Epiphany.desktop\n",
])
def test_a_personal_mime_choice_for_another_browser_is_left_alone(env: Env, mimeapps: str) -> None:
    env.fake_set_default()
    env.setup_done()
    env.installed.add("chrome")
    env.write(env.user_mimeapps, mimeapps)
    assert browsers.sync_default() == browsers.SYNC_PERSONAL
    assert env.set_default_calls == []


def test_a_mime_entry_that_already_names_the_chosen_browser_is_not_a_personal_choice(env: Env) -> None:
    env.fake_set_default()
    env.setup_done()
    env.installed.add("chrome")
    env.write(env.user_mimeapps, "[Default Applications]\nx-scheme-handler/https=google-chrome.desktop\n"
                                 "[Added Associations]\nx-scheme-handler/http=firefox.desktop\n")
    assert browsers.sync_default() == browsers.SYNC_APPLIED
    assert env.set_default_calls == ["chrome"]


def test_what_setup_itself_wrote_counts_as_done(env: Env) -> None:
    """Chrome was installed when Setup ran: Setup's own set_default already pointed XFCE at it."""
    env.fake_set_default()
    env.setup_done()
    env.installed.add("chrome")
    env.write(env.helpers_rc, "WebBrowser=google-chrome\n")
    assert browsers.sync_default() == browsers.SYNC_ALREADY
    assert env.set_default_calls == []


# --- when it does nothing -------------------------------------------------------------------------------------
def test_nothing_happens_in_the_live_session(env: Env) -> None:
    env.fake_set_default()
    env.cmdline(LIVE_CMDLINE)
    env.setup_done()
    env.installed.add("chrome")
    assert browsers.sync_default() == browsers.SYNC_LIVE
    assert env.set_default_calls == [] and not env.helpers_rc.exists()


def test_nothing_happens_in_the_temporary_oem_account(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.fake_set_default()
    monkeypatch.setattr(lsession, "is_oem_temp_user", lambda user=None: True)
    env.setup_done()
    env.installed.add("chrome")
    assert browsers.sync_default() == browsers.SYNC_LIVE and env.set_default_calls == []


def test_nothing_happens_before_setup_has_finished(env: Env) -> None:
    """Until then the choice is not made (the wizard may still change it)."""
    env.fake_set_default()
    env.installed.add("chrome")
    assert browsers.sync_default() == browsers.SYNC_SETUP_PENDING
    assert env.set_default_calls == [] and not env.helpers_rc.exists()


@pytest.mark.parametrize("state, chosen, expected", [
    ("pending", "chrome", browsers.SYNC_PENDING),         # the silent retry will still add it
    ("failed", "chrome", browsers.SYNC_PENDING),
    ("", "chrome", browsers.SYNC_PENDING),                # no record at all: the retry treats it the same
    ("skipped", "chrome", browsers.SYNC_UNAVAILABLE),     # the installer left it out on purpose
    ("done", "chrome", browsers.SYNC_UNAVAILABLE),        # nothing is going to install it again
    ("pending", "edge", browsers.SYNC_UNAVAILABLE),       # Lindos never installs Edge
])
def test_a_browser_that_is_not_installed_is_waited_for_only_when_something_will_install_it(
        env: Env, state: str, chosen: str, expected: str) -> None:
    env.fake_set_default()
    env.setup_done()
    env.choose(chosen)
    if state:
        env.install_state(state)
    assert browsers.sync_default() == expected
    assert env.set_default_calls == []


# --- waiting ---------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: List[float] = []
        self.on_sleep: Callable[[int], None] = lambda n: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.on_sleep(len(self.sleeps))


def test_without_a_wait_it_looks_once(env: Env) -> None:
    env.fake_set_default()
    clock = Clock()
    assert browsers.sync_default(sleep=clock.sleep, clock=clock.monotonic) == browsers.SYNC_SETUP_PENDING
    assert clock.sleeps == []


def test_the_first_session_setup_finishes_then_chrome_lands_and_the_default_follows(env: Env) -> None:
    """The login-time process starts BEFORE Setup is done; it keeps looking until both have happened."""
    env.fake_set_default()
    env.choose("chrome")
    env.install_state("pending")
    clock = Clock()

    def events(n: int) -> None:
        if n == 2:
            env.setup_done()                 # the user finishes the wizard (Chrome still pending)
        if n == 5:
            env.installed.add("chrome")      # the silent retry installed it meanwhile

    clock.on_sleep = events
    log: List[str] = []
    outcome = browsers.sync_default(wait=3600, poll=30, log=log.append, sleep=clock.sleep, clock=clock.monotonic)
    assert outcome == browsers.SYNC_APPLIED
    assert clock.sleeps == [30.0] * 5
    assert env.set_default_calls == ["chrome"]
    assert any("making it the default" in line for line in log)


def test_the_wait_is_bounded(env: Env) -> None:
    env.fake_set_default()
    env.setup_done()
    env.choose("chrome")
    env.install_state("failed")
    clock = Clock()
    outcome = browsers.sync_default(wait=100, poll=30, sleep=clock.sleep, clock=clock.monotonic)
    assert outcome == browsers.SYNC_PENDING
    assert clock.sleeps == [30.0, 30.0, 30.0, 10.0] and sum(clock.sleeps) == 100
    assert env.set_default_calls == []


def test_waiting_stops_as_soon_as_nothing_can_change_any_more(env: Env) -> None:
    env.fake_set_default()
    env.setup_done()
    env.choose("chrome")
    env.install_state("pending")
    clock = Clock()

    def events(n: int) -> None:
        env.install_state("skipped")         # e.g. the user picked another browser system-wide meanwhile

    clock.on_sleep = events
    assert browsers.sync_default(wait=3600, sleep=clock.sleep, clock=clock.monotonic) == browsers.SYNC_UNAVAILABLE
    assert len(clock.sleeps) == 1


# --- the CLI the autostart entry runs ---------------------------------------------------------------------------
def _cli(run_cli, *args: str, cmdline: str = INSTALLED_CMDLINE, tmp: Path) -> "object":
    path = tmp / "cli-cmdline"
    path.write_text(cmdline, encoding="utf-8")
    return run_cli("lindos-browser", "sync-default", *args, env={lsession.CMDLINE_ENV: str(path)})


def test_cli_sync_default_reports_the_outcome(core_env: Dict[str, Path], run_cli) -> None:
    proc = _cli(run_cli, "--json", tmp=core_env["tmp"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["outcome"] == browsers.SYNC_SETUP_PENDING
    text = _cli(run_cli, tmp=core_env["tmp"])
    assert text.returncode == 0 and text.stdout.strip() == "sync-default: setup-pending"


def test_cli_the_exact_autostart_arguments_parse_and_stop_at_once_in_the_live_session(core_env: Dict[str, Path],
                                                                                        run_cli) -> None:
    """'lindos-browser sync-default --wait' (no value) is the Exec line of the lindos-desktop autostart entry."""
    proc = _cli(run_cli, "--wait", cmdline=LIVE_CMDLINE, tmp=core_env["tmp"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "sync-default: live"


def test_cli_wait_takes_seconds_and_help_explains_it(run_cli) -> None:
    proc = run_cli("lindos-browser", "sync-default", "--help")
    assert proc.returncode == 0 and "--wait" in proc.stdout and "Setup" in proc.stdout
    assert run_cli("lindos-browser", "sync-default", "--wait", "soon").returncode == 2


def test_the_default_wait_of_the_cli_is_two_hours() -> None:
    assert browsers.SYNC_MAX_WAIT_SECONDS == 7200.0 and browsers.SYNC_POLL_SECONDS == 30.0
