<img src="macos/BeingsGrowthIcon.png" alt="Beings Growth icon" width="72">

# Beings Growth

Discover companies, rank them against your prospecting goals, and find public business contacts.

## Get started

Copy and paste this prompt into **Codex, Grok Build, or Claude Code**. Use a session that can run terminal commands on the computer where you want Beings Growth installed.

```text
Set up Beings Growth on this computer. Build it, install it, and launch it for me.

Use the official source at https://github.com/cameoed/growth. Reuse my existing
checkout of that repository if I have one. Otherwise, clone it into a folder
I can keep. Preserve existing changes, lead data, and settings. Keep the
checkout after installation because the Mac app uses it at runtime.

Check for Git and Python 3.11 or newer. On macOS, also check for macOS 13 or
newer and the Xcode Command Line Tools. Install missing prerequisites using
standard installers where you can, and tell me if a system prompt needs my input.
Create or reuse the repository's .venv and install the package into it with
python -m pip install -e . using that environment's Python.

On macOS, run scripts/install_beings_growth_app.sh from the checkout. It builds
and signs Beings Growth.app, installs it in my Applications folder
(~/Applications by default), and opens it. If an existing installation is
running a scan, wait for it to finish before replacing or restarting the app.
On Windows or Linux, start the installed beings-growth entry point from .venv
and open the local dashboard in my browser. On Windows, set PYTHONUTF8=1 for
the launch process.

Verify the Beings Growth window or dashboard is ready, then tell me where to
find it next time. Leave storage and mission choices for me in the app. Ask
before configuring paid providers or running scans.
```

[Growth tactics](docs/GROWTH_TACTICS.md) · [Oracle VM setup](deploy/oracle/README.md) · [Product Use License](LICENSE)
