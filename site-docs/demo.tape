# odoo-activity terminal demo
# Showcases: the TUI homepage with a populated instance/database (via
# demo_fixture.py -- see its docstring for why -- rendered fully offscreen
# before the reveal)

# ── Output ────────────────────────────────────────────────────────────────────
Output ./docs/demo.gif

# ── Settings ──────────────────────────────────────────────────────────────────
Set Shell bash
Set TypingSpeed 35ms
Set Width 2300
Set Height 1600
Set FontSize 49
Set FontFamily "JetBrains Mono, Fira Code, monospace"
Set Theme { "name": "odoo-activity", "background": "#111111", "foreground": "#f5f5f5", "cursor": "#ea580c", "black": "#2a2a2a", "red": "#ef4444", "green": "#22c55e", "yellow": "#eab308", "blue": "#3b82f6", "magenta": "#d946ef", "cyan": "#06b6d4", "white": "#f5f5f5", "brightBlack": "#52525b", "brightRed": "#f87171", "brightGreen": "#4ade80", "brightYellow": "#facc15", "brightBlue": "#60a5fa", "brightMagenta": "#e879f9", "brightCyan": "#22d3ee", "brightWhite": "#ffffff" }
Set WindowBar Colorful
Set WindowBarSize 60
Set BorderRadius 15
Set Margin 30
Set Padding 40
Set LineHeight 1.3

# ── Bootstrap: activate the venv `make demo` pre-installed at /venv, launch the
# fixture-backed TUI, and let it fully render -- all hidden, so the reveal
# below is already the live homepage, not a shell prompt + typed command.
# Installing odoo-activity live during the recording made pip's own network
# I/O compete with VHS's screen capture for CPU and stalled for minutes on a
# single dependency -- `make demo` now installs into ./.demo-venv *before*
# invoking vhs, so this only ever has to activate it.
Hide
Type "export PATH=/venv/local/bin:$PATH PYTHONPATH=/venv/local/lib/python3.13/dist-packages PYTHONWARNINGS=ignore"
Enter
Type "export PS1='❯ '"
Enter
Type "clear"
Enter
Type "python3 demo_fixture.py"
Enter
Sleep 2s
Show

# ── Scene 1: TUI homepage, instance + database loaded ───────────────────────────
Sleep 6s
